# -*- coding: utf-8 -*-
"""数据采集层：GitHub REST/Search API、Trending RSS、npm / PyPI / crates.io 下载统计。

设计原则：
1. 只使用 Python 标准库，不依赖 pip 包，也不需要大模型 API 密钥。
2. 每个数字都携带来源 URL、查询时间与统计口径；采集失败写入 limitations，绝不补造。
3. Stars / Forks 是累计快照；Release download_count 是附件累计下载次数；
   包下载量是包获取次数。三者互不等价，报告层分开呈现。
"""
from __future__ import annotations

import base64
import datetime as dt
import html
import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request

GITHUB_API = "https://api.github.com"
TZ = dt.timezone(dt.timedelta(hours=8))
UTC = dt.timezone.utc


def _iso_z(d: dt.datetime) -> str:
    return d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Net:
    """统一的 HTTP 客户端，所有失败都转为 limitations 记录，不抛出到主流程。"""

    def __init__(self, cfg, limitations, log):
        self.cfg = cfg
        self.limitations = limitations
        self.log = log
        import os
        self.token = os.environ.get(cfg.get("github_token_env", "GITHUB_TOKEN"), "").strip()
        self.timeout = int(cfg.get("http_timeout_sec", 25))

    def _open(self, url, accept="application/json"):
        h = {"User-Agent": "github-open-resource-digest/1.0", "Accept": accept}
        if self.token and url.startswith(GITHUB_API):
            h["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(url, headers=h)
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return r.read().decode("utf-8", "replace")

    def json(self, url, label):
        try:
            return json.loads(self._open(url, "application/vnd.github+json" if url.startswith(GITHUB_API) else "application/json"))
        except urllib.error.HTTPError as e:
            self.limitations.append("- 数据源 `%s` 返回 HTTP %s（%s）。" % (label, e.code, url))
            self.log.warning("%s HTTP %s", label, e.code)
            return None
        except Exception as e:
            self.limitations.append("- 数据源 `%s` 访问失败（%s）：%s" % (label, type(e).__name__, str(e)[:120]))
            self.log.warning("%s failed: %s", label, e)
            return None

    def text(self, url, label, accept="text/html,application/xml,*/*"):
        try:
            return self._open(url, accept)
        except Exception as e:
            self.limitations.append("- 数据源 `%s` 访问失败（%s）：%s" % (label, type(e).__name__, str(e)[:120]))
            self.log.warning("%s failed: %s", label, e)
            return None


class ApiBudget:
    """无 token 时 GitHub core 限额 60 次/小时；据此裁剪采集条数，避免中途失败。"""

    def __init__(self, net, limitations, log):
        self.net, self.limitations, self.log = net, limitations, log
        self.core_remaining = None
        self.search_remaining = None
        self.reset_local = None
        d = net.json(GITHUB_API + "/rate_limit", "GitHub rate_limit API")
        if isinstance(d, dict) and "resources" in d:
            core = d["resources"].get("core", {})
            search = d["resources"].get("search", {})
            self.core_remaining = core.get("remaining")
            self.search_remaining = search.get("remaining")
            if core.get("reset"):
                self.reset_local = dt.datetime.fromtimestamp(core["reset"], TZ).strftime("%Y-%m-%d %H:%M:%S")
            self.log.info("GitHub API core=%s search=%s reset=%s token=%s",
                          self.core_remaining, self.search_remaining, self.reset_local, bool(self.net.token))
        else:
            self.limitations.append("- 未能读取 GitHub API 速率限额，采集按保守方式继续。")

    def affordable_repos(self, wanted, per_repo=3, reserve=2):
        if self.core_remaining is None:
            return wanted
        cap = max(1, (int(self.core_remaining) - reserve) // per_repo)
        if cap < wanted:
            self.limitations.append(
                "- GitHub core API 剩余额度 %s 次，本次最多完整采集 %s 个仓库（原计划 %s 个），"
                "其余条目未纳入。额度重置时间（北京时间）：%s。"
                % (self.core_remaining, cap, wanted, self.reset_local or "未知")
            )
            self.log.warning("trim repos %s -> %s by budget", wanted, cap)
        return min(wanted, cap)


def parse_rss(text, label):
    """返回 (条目列表, pubDate)。Trending RSS 为第三方聚合，pubDate 必须随报告披露。"""
    if not text:
        return [], None
    pub = re.search(r"<pubDate>(.*?)</pubDate>", text, re.S)
    items = []
    for block in re.findall(r"<item>(.*?)</item>", text, re.S):
        m = re.search(r"<title>(.*?)</title>", block, re.S)
        l = re.search(r"<link>(.*?)</link>", block, re.S)
        if m:
            name = html.unescape(m.group(1)).strip()
            if re.fullmatch(r"[\w.\-]+/[\w.\-]+", name):
                items.append({"repo": name, "url": html.unescape(l.group(1)) if l else "", "label": label})
    return items, (pub.group(1).strip() if pub else None)


def try_official_trending(net, cfg, limitations):
    """官方周榜可访问时提取 stars this week；不可访问则记入限制，不补造。"""
    url = cfg.get("official_trending_url", "https://github.com/trending?since=weekly")
    s = net.text(url, "GitHub 官方周榜")
    if not s:
        limitations.append(
            "- GitHub 官方周榜 `%s` 本次访问失败，无法取得官方「stars this week」数值；"
            "报告中的关注度上升线索来自第三方 Trending RSS，不等同于官方增量数据。" % url
        )
        return {}
    found = {}
    for art in re.findall(r'<article class="Box-row"(.*?)</article>', s, re.S):
        # GitHub 已把 data-hydro-click 等属性移到 href 之前，不能再用 <a href=" 匹配；
        # 只在仓库名所在的 <h2> 块内取 owner/repo 形式的链接，避免误取 sponsors / login 链接。
        h2 = re.search(r"<h2[^>]*>(.*?)</h2>", art, re.S)
        m = re.search(r'href="/([\w.\-]+/[\w.\-]+)"', h2.group(1)) if h2 else None
        w = re.search(r"([\d,]+)\s+stars this week", art)
        if m:
            found[m.group(1).lower()] = int(w.group(1).replace(",", "")) if w else None
    if not found:
        limitations.append(
            "- GitHub 官方周榜页面已返回，但按当前 HTML 结构未解析出条目，本次不采用其数据；"
            "HTML 结构变化是常见原因，需检查 `try_official_trending` 的解析规则。")
    return found


def search_created(net, budget, start_date, end_date, per_page, limitations):
    """本周新建仓库检索。不引用 total_count（该值与 created 过滤条件不匹配）。"""
    if budget.search_remaining is not None and budget.search_remaining < 1:
        limitations.append("- GitHub Search API 额度不足，本次未执行本周新建仓库检索。")
        return []
    q = "created:%s..%s" % (start_date, end_date)
    url = GITHUB_API + "/search/repositories?" + urllib.parse.urlencode(
        {"q": q, "sort": "stars", "order": "desc", "per_page": per_page})
    d = net.json(url, "GitHub 本周新建仓库检索")
    out = []
    if isinstance(d, dict) and isinstance(d.get("items"), list):
        for r in d["items"]:
            out.append({
                "repo": r.get("full_name"),
                "stars": r.get("stargazers_count", 0),
                "forks": r.get("forks_count", 0),
                "created_at": r.get("created_at"),
                "description": r.get("description") or "",
                "license": (r.get("license") or {}).get("spdx_id"),
            })
        limitations.append(
            "- GitHub 搜索 API 对 `created:` 条件返回的 `total_count` 与过滤条件明显不匹配，"
            "本报告不引用该计数，只以逐仓库 `created_at` 校验是否落在窗口内。"
        )
    else:
        limitations.append("- 本周新建仓库检索未返回有效结果。")
    return out


def _license_summary(spdx, text):
    """返回 (显示名, 限制说明)。自定义许可只引用原文命中片段，不作法律解释。"""
    known = {
        "MIT": "MIT：再分发须保留版权与许可声明，软件不附带担保。",
        "Apache-2.0": "Apache-2.0：须保留许可与版权声明、对修改文件标注变更，并含专利授权与终止条款。",
        "BSD-3-Clause": "BSD-3-Clause：须保留版权与许可文本，禁止未经书面许可使用贡献者名称为衍生产品背书。",
        "GPL-3.0": "GPL-3.0：衍生作品分发时须采用相同许可证并提供源代码。",
        "CC-BY-4.0": "CC-BY-4.0：须署名；许可证原文声明 Creative Commons 不提供法律服务或法律意见。",
        "CC-BY-SA-4.0": "CC-BY-SA-4.0：须署名，衍生作品须以相同许可共享；原文声明不提供法律意见。",
    }
    if spdx in known:
        return spdx, known[spdx]
    if text:
        head = re.sub(r"\s+", " ", text[:180]).strip()
        notes = ["GitHub 未识别到标准 SPDX 许可证（%s），须阅读许可证原文。" % (spdx or "无")]
        notes.append("许可证文件原文开头：「%s……」" % head[:150])
        patterns = [
            r"[^.]{0,160}Excluded Territories[^.]{0,200}\.",
            r"[^.]{0,160}million US dollars[^.]{0,200}\.",
            r"[^.]{0,120}may not use[^.]{0,220}\.",
            r"[^.]{0,120}dual-licensed[^.]{0,160}\.",
        ]
        for p in patterns:
            m = re.search(p, text, re.I | re.S)
            if m:
                frag = re.sub(r"\s+", " ", m.group(0)).strip()
                notes.append("原文命中限制条款片段：「%s」" % frag[:260])
        notes.append("以上为原文片段，未作法律解释；使用前须自行核对完整条款。")
        return (spdx or "NOASSERTION"), " ".join(notes)
    if spdx in (None, "NOASSERTION"):
        return "未识别", "GitHub 仓库未提供可识别的许可证文件；公开可见不等于可自由复制、商用或再分发。"
    return str(spdx), "许可证为标准 SPDX 标识 %s，具体条款本次未逐条核验。" % spdx


def collect_repo(net, budget, full, since_utc, cfg, limitations):
    """单仓库采集：元数据、本周提交、Releases、许可证。"""
    per_page = int(cfg.get("commits_per_page", 100))
    rel_per_page = int(cfg.get("releases_per_page", 5))
    r = {"repo": full, "ok": False}
    d = net.json(GITHUB_API + "/repos/" + full, full + " 仓库元数据")
    if not isinstance(d, dict) or "full_name" not in d:
        limitations.append("- 仓库 `%s` 的元数据未取得，该条目未纳入报告。" % full)
        return r
    r.update({
        "ok": True,
        "url": d.get("html_url"),
        "description": d.get("description") or "",
        "created_at": d.get("created_at"),
        "pushed_at": d.get("pushed_at"),
        "stars": d.get("stargazers_count", 0),
        "forks": d.get("forks_count", 0),
        "watchers": d.get("subscribers_count"),
        "language": d.get("language"),
        "topics": d.get("topics") or [],
        "license_spdx": (d.get("license") or {}).get("spdx_id"),
        "default_branch": d.get("default_branch") or "main",
        "meta_src": GITHUB_API + "/repos/" + full,
    })
    # 本周提交
    u = GITHUB_API + "/repos/%s/commits?%s" % (full, urllib.parse.urlencode(
        {"since": since_utc, "per_page": per_page}))
    c = net.json(u, full + " 本周提交")
    if isinstance(c, list):
        r["commits_week"] = len(c)
        r["commits_truncated"] = len(c) >= per_page
        r["last_commit"] = (c[0].get("commit", {}).get("committer", {}) or {}).get("date") if c else None
    else:
        r["commits_week"] = None
        r["commits_truncated"] = False
        limitations.append("- 仓库 `%s` 的本周提交数未取得，该项标注为未核实。" % full)
    r["commits_src"] = "https://github.com/%s/commits" % full
    # Releases
    u = GITHUB_API + "/repos/%s/releases?per_page=%d" % (full, rel_per_page)
    rel = net.json(u, full + " Releases")
    r["releases"] = []
    if isinstance(rel, list):
        for x in rel:
            assets = x.get("assets") or []
            r["releases"].append({
                "tag": x.get("tag_name"),
                "published_at": x.get("published_at"),
                "prerelease": bool(x.get("prerelease")),
                "asset_count": len(assets),
                "asset_downloads": sum(a.get("download_count", 0) for a in assets),
                "top_assets": sorted(
                    [{"name": a.get("name"), "dl": a.get("download_count", 0),
                      "url": a.get("browser_download_url")} for a in assets],
                    key=lambda z: -z["dl"])[:5],
                "page": "https://github.com/%s/releases/tag/%s" % (full, urllib.parse.quote(str(x.get("tag_name") or ""))),
            })
    else:
        limitations.append("- 仓库 `%s` 的 Release 列表未取得，Release 下载量标注为未核实。" % full)
    r["releases_src"] = GITHUB_API + "/repos/%s/releases" % full
    # 许可证原文（仅当自动识别失败时）
    lic_text = None
    if r["license_spdx"] in (None, "NOASSERTION"):
        lic = net.json(GITHUB_API + "/repos/" + full + "/license", full + " 许可证原文")
        if isinstance(lic, dict) and lic.get("content"):
            try:
                lic_text = base64.b64decode(lic["content"]).decode("utf-8", "replace")
            except Exception:
                lic_text = None
        r["license_endpoint_status"] = "200" if isinstance(lic, dict) and lic.get("content") else "未返回许可证文件内容"
        if not lic_text:
            limitations.append("- 仓库 `%s` 的 `/license` 端点未返回许可证文件内容；"
                               "该仓库按「无可识别的许可证文件」处理，不推断授权范围。" % full)
    r["license_name"], r["license_limits"] = _license_summary(r["license_spdx"], lic_text)
    r["license_src"] = GITHUB_API + "/repos/" + full + "/license"
    # 下载地址只按 GitHub 固定 URL 规则、由 API 返回的 tag 与默认分支构造，不猜测文件名
    links = []
    for x in (r["releases"] or [])[:2]:
        if x.get("tag") and x.get("page"):
            links.append({"label": "%s Release 附件页" % x["tag"], "url": x["page"]})
    links.append({"label": "%s 分支源码 ZIP" % r["default_branch"],
                  "url": "https://github.com/%s/archive/refs/heads/%s.zip" % (full, r["default_branch"])})
    r["download_links"] = links
    return r


def verify_all_links(records, cfg, limitations, log):
    """并发验证下载地址。github.com 主站在部分代理环境下 urllib 会返回 502，
    因此优先用系统 curl（Windows 10+ 自带）探测；两者都不可用时如实标注未验证。"""
    if not cfg.get("verify_download_links", True):
        limitations.append("- 本次按配置未逐一验证下载地址可访问性；链接中的 tag 与默认分支来自 GitHub API，按官方固定 URL 规则构造。")
        return
    import shutil
    import subprocess
    from concurrent.futures import ThreadPoolExecutor
    todo = [(r, x) for r in records for x in (r.get("download_links") or [])]
    timeout = int(cfg.get("link_timeout_sec", 10))
    curl = shutil.which("curl")

    def one(item):
        _, x = item
        if curl:
            try:
                p = subprocess.run(
                    [curl, "-sS", "-I", "-L", "-w", "\n%{http_code}",
                     "--max-time", str(timeout), x["url"]],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    timeout=timeout + 5, text=True)
                nums = [l.strip() for l in (p.stdout or "").splitlines() if l.strip().isdigit()]
                if nums:
                    code = nums[-1]
                    if code == "000":
                        return "本次未完成探测（curl 返回 000，通常为代理或网络不可达）"
                    return code
            except Exception:
                pass
        try:
            req = urllib.request.Request(x["url"], method="HEAD",
                                         headers={"User-Agent": "github-open-resource-digest/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return str(resp.status)
        except urllib.error.HTTPError as e:
            return str(e.code)
        except Exception:
            return "未验证（本环境无法直连 github.com 主站）"

    try:
        with ThreadPoolExecutor(max_workers=8) as ex:
            statuses = list(ex.map(one, todo))
    except Exception as e:
        limitations.append("- 下载地址并发验证未执行（%s），报告中链接状态标注为未验证。" % type(e).__name__)
        log.warning("link verify failed: %s", e)
        return
    failed, unresolved = 0, 0
    for (_, x), st in zip(todo, statuses):
        x["status"] = st
        if st.startswith("本次未完成探测"):
            unresolved += 1
        elif st != "200":
            failed += 1
    if failed or unresolved:
        limitations.append(
            "- 本次共探测 %d 个下载地址：%d 个返回 HTTP 200，%d 个返回非 200 状态，%d 个因本环境网络未能完成探测"
            "（curl 返回 000）。本环境的 Python 客户端直连 github.com 主站会返回代理 502，探测改由系统 curl 完成；"
            "**未能完成探测不等于链接无效**，这些链接的状态已逐条标注。所有链接中的 tag 与默认分支均来自 "
            "GitHub API 返回值，按 GitHub 官方固定 URL 规则构造，未作猜测。"
            % (len(todo), len(todo) - failed - unresolved, failed, unresolved))
    log.info("verified %d links: 200=%d non200=%d unresolved=%d (curl=%s)",
             len(todo), len(todo) - failed - unresolved, failed, unresolved, bool(curl))


def collect_package(net, mapping, start_date, end_date, limitations):
    """关联包下载量。只有在 config.seed_repos 中显式登记的包名才被采信。"""
    out = {}
    if mapping.get("npm"):
        pkg = mapping["npm"]
        url = "https://api.npmjs.org/downloads/point/%s:%s/%s" % (
            start_date, end_date, urllib.parse.quote(pkg, safe=""))
        d = net.json(url, "npm 下载统计 " + pkg)
        if isinstance(d, dict) and isinstance(d.get("downloads"), int):
            out["npm"] = {"pkg": pkg, "downloads": d["downloads"],
                          "start": d.get("start"), "end": d.get("end"), "src": url}
        else:
            limitations.append("- npm 包 `%s` 的下载统计未取得，标注为未找到可靠的关联包下载量数据。" % pkg)
    if mapping.get("pypi"):
        pkg = mapping["pypi"]
        url = "https://pypistats.org/api/packages/%s/recent" % urllib.parse.quote(pkg, safe="")
        d = net.json(url, "pypistats " + pkg)
        if isinstance(d, dict) and isinstance(d.get("data"), dict):
            out["pypi"] = {"pkg": pkg, "data": d["data"], "src": url}
        else:
            limitations.append("- PyPI 包 `%s` 的下载统计未取得，标注为未找到可靠的关联包下载量数据。" % pkg)
    if mapping.get("crate"):
        crate = mapping["crate"]
        url = "https://crates.io/api/v1/crates/%s" % urllib.parse.quote(crate, safe="")
        d = net.json(url, "crates.io " + crate)
        c = (d or {}).get("crate") if isinstance(d, dict) else None
        if isinstance(c, dict):
            out["crate"] = {"pkg": crate, "downloads": c.get("downloads"),
                            "recent": c.get("recent_downloads"), "src": url}
        else:
            limitations.append("- crates.io 包 `%s` 的下载统计未取得，标注为未找到可靠的关联包下载量数据。" % crate)
    return out


def build_candidates(cfg, created, trending_items, official_trending, window_start, run_date):
    """合并种子仓库、本周新建检索、Trending 榜条目，计算可核验的综合分。"""
    w = cfg.get("weights", {})
    pool = {}

    def add(repo, **kw):
        key = repo.lower()
        if key not in pool:
            pool[key] = {"repo": repo, "category": "", "mapping": {}, "trending": [],
                         "new_created": False, "pre_stars": 0, "pre_forks": 0,
                         "official_week_stars": None}
        pool[key].update({k: v for k, v in kw.items() if v not in (None, "", [], {})})

    for s in cfg.get("seed_repos", []):
        add(s["repo"], category=s.get("category", ""),
            mapping={k: s[k] for k in ("npm", "pypi", "crate") if s.get(k)})
    for x in created:
        if not x.get("repo"):
            continue
        add(x["repo"], pre_stars=x.get("stars", 0), pre_forks=x.get("forks", 0),
            new_created=bool(x.get("created_at") and x["created_at"][:10] >= window_start),
            category=pool.get(x["repo"].lower(), {}).get("category", "本周新建 / 待分类"))
    for t in trending_items:
        add(t["repo"])
        if t["label"] not in pool[t["repo"].lower()]["trending"]:
            pool[t["repo"].lower()]["trending"].append(t["label"])
    for v in pool.values():
        v["official_week_stars"] = official_trending.get(v["repo"].lower())
        if not v["category"]:
            v["category"] = ("本周发现 / Trending 榜收录" if v["trending"]
                             else "本周发现 / 本周新建检索候选")
    return pool


def score(rec, cfg, window_start):
    """综合分只用于排序；报告会说明它不是本周新增 Stars 排名。"""
    w = cfg.get("weights", {})
    stars = rec.get("stars") or rec.get("pre_stars") or 0
    forks = rec.get("forks") or rec.get("pre_forks") or 0
    commits = rec.get("commits_week") or 0
    s = (float(w.get("stars", 2)) * math.log1p(stars)
         + float(w.get("forks", 1)) * math.log1p(forks)
         + float(w.get("commits", 8)) * min(commits, 50) / 50.0)
    rels = rec.get("releases") or []
    in_window = [x for x in rels if not x.get("prerelease") and x.get("published_at")
                 and x["published_at"][:10] >= window_start]
    if in_window:
        s += float(w.get("release_in_window", 6))
    if rec.get("trending"):
        s += float(w.get("trending", 4))
    if rec.get("created_at") and rec["created_at"][:10] >= window_start:
        s += float(w.get("created_in_window", 3))
    return round(s, 3)


def collect_all(cfg, run_date, log):
    """主采集流程，返回完整数据字典。"""
    limitations = []
    net = Net(cfg, limitations, log)
    days = int(cfg.get("window_days", 7))
    window_start_dt = dt.datetime.combine(run_date - dt.timedelta(days=days), dt.time(0, 0), TZ)
    now_local = dt.datetime.now(TZ)
    since_utc = _iso_z(window_start_dt)
    start_date = window_start_dt.strftime("%Y-%m-%d")
    end_date = run_date.strftime("%Y-%m-%d")
    npm_end = (run_date - dt.timedelta(days=1)).strftime("%Y-%m-%d")

    meta = {
        "run_date": end_date,
        "query_time": now_local.strftime("%Y-%m-%d %H:%M:%S"),
        "window_start": window_start_dt.strftime("%Y-%m-%d %H:%M"),
        "window_end": now_local.strftime("%Y-%m-%d %H:%M"),
        "since_utc": since_utc,
        "npm_range": "%s:%s" % (start_date, npm_end),
        "timezone": "UTC+8（北京时间）",
        "days": days,
    }
    log.info("window %s -> %s, since(UTC)=%s", meta["window_start"], meta["window_end"], since_utc)

    budget = ApiBudget(net, limitations, log)
    official = try_official_trending(net, cfg, limitations)
    created = search_created(net, budget, start_date, end_date,
                             int(cfg.get("search_per_page", 30)), limitations)
    trending_items, rss_meta = [], {}
    for url in cfg.get("trending_rss", []):
        label = "周榜 RSS" if "weekly" in url else "日榜 RSS"
        items, pub = parse_rss(net.text(url, label), label)
        trending_items.extend(items)
        rss_meta[label] = {"pubDate": pub, "count": len(items), "url": url,
                           "sample": [x["repo"] for x in items[:20]]}
        if not items:
            limitations.append("- %s 未返回条目或访问失败，该线索本次不可用。" % label)
    meta["rss"] = rss_meta
    meta["official_trending_count"] = len(official)
    meta["created_search_count"] = len(created)

    pool = build_candidates(cfg, created, trending_items, official, start_date, end_date)

    # 预排序（未采集详情时用搜索结果里的累计快照），再按预算裁剪
    pre_rank = {k: i for i, k in enumerate(sorted(pool, key=lambda k: -(
        math.log1p(pool[k].get("pre_stars") or 0) * 2
        + math.log1p(pool[k].get("pre_forks") or 0)
        + 4 * len(pool[k]["trending"]))))}
    # 种子仓库保证领域覆盖，但预留名额给本周新建 / Trending 发现的仓库
    seeds = {s["repo"].lower() for s in cfg.get("seed_repos", [])}
    seed_sorted = sorted([k for k in pool if k in seeds], key=lambda k: pre_rank[k])
    other_sorted = sorted([k for k in pool if k not in seeds], key=lambda k: pre_rank[k])
    max_items = int(cfg.get("max_items", 14))
    seed_quota = min(len(seed_sorted), max(1, int(max_items * 0.65)))
    selected = seed_sorted[:seed_quota] + other_sorted[:max(0, max_items - seed_quota)]
    affordable = budget.affordable_repos(len(selected))
    selected = selected[:affordable]
    log.info("selected %d repos", len(selected))

    records = []
    for key in selected:
        c = pool[key]
        rec = collect_repo(net, budget, c["repo"], since_utc, cfg, limitations)
        if not rec.get("ok"):
            continue
        rec.update({k: c[k] for k in ("category", "mapping", "trending", "official_week_stars")})
        rec["created_in_window"] = bool(rec.get("created_at") and rec["created_at"][:10] >= start_date)
        rec["packages"] = collect_package(net, c["mapping"], start_date, npm_end, limitations)
        rec["sources"] = {
            "repo": rec["meta_src"], "commits": rec["commits_src"],
            "releases": rec["releases_src"], "license": rec["license_src"],
        }
        rec["score"] = score(rec, cfg, start_date)
        records.append(rec)

    records.sort(key=lambda x: -x["score"])
    verify_all_links(records, cfg, limitations, log)
    stars_rank = {r["repo"]: i + 1 for i, r in enumerate(sorted(records, key=lambda x: -(x.get("stars") or 0)))}
    for i, r in enumerate(records, 1):
        r["rank"] = i
        r["stars_rank"] = stars_rank.get(r["repo"], i)

    limitations.extend([
        "- Stars、Forks、Watchers 为查询时点的累计快照，不是本周新增数，也不是下载量；"
        "GitHub 公开 API 不提供「本周新增 Stars」的可靠口径，本报告不给出该数字。",
        "- Release 下载量是指定版本全部附件的 `download_count` 累计之和，不是本周下载数、不是独立用户数；"
        "附件常含安装脚本、校验和、增量文件与签名元数据，合计数会被这些文件抬高。",
        "- npm 下载量是明确日期区间内的包获取次数；PyPI `last_week` 是滚动 7 日窗口；"
        "crates.io `downloads` 为累计、`recent_downloads` 为近 90 天。三者都不是独立用户数或安装量。",
        "- 本周提交按 `commits?since=<UTC>&per_page=%d` 统计默认分支，单页上限 100 条；"
        "返回满页时标注「≥100（受单页上限截断）」，不推算实际总数；其他分支或标签的推送未计入。"
        % int(cfg.get("commits_per_page", 100)),
        "- 所有数字均为 %s 的查询时点快照，GitHub 页面与 API 数据会随时间变化。" % end_date,
    ])
    return {"meta": meta, "records": records, "limitations": limitations,
            "budget": {"core_remaining": budget.core_remaining,
                       "search_remaining": budget.search_remaining,
                       "reset_local": budget.reset_local,
                       "has_token": bool(net.token)}}
