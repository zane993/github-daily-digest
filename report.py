# -*- coding: utf-8 -*-
"""报告层：由采集数据生成中文 Markdown 与自包含 HTML。

所有句子由采集到的数据驱动；没有数据的字段一律写「未找到可靠的下载量数据」或
「本次未核实」，不生成推测性描述。趋势章节显式标注为「基于本报告数据的自动归纳」。
"""
from __future__ import annotations

import html as _html
import re

NO_DL = "未找到可靠的下载量数据"


def f(n):
    return "{:,}".format(n) if isinstance(n, int) else str(n)


def band_date(rec, start):
    """窗口内发生的、可核验的事件，拼成「本周值得关注原因」。"""
    parts = []
    if rec.get("created_in_window"):
        parts.append("仓库于 %s 创建" % (rec.get("created_at") or "")[:10])
    cw = rec.get("commits_week")
    if cw is None:
        parts.append("本周提交数本次未核实")
    elif cw:
        parts.append("默认分支本周 %d 次提交%s" % (cw, "（达到单页上限，实际可能更多）" if rec.get("commits_truncated") else ""))
    else:
        parts.append("默认分支本周提交 0 次")
    rels = [x for x in (rec.get("releases") or [])
            if x.get("published_at") and x["published_at"][:10] >= start and not x.get("prerelease")]
    if rels:
        parts.append("窗口内发布正式版 %s" % "、".join("%s（%s）" % (x["tag"], x["published_at"][:10]) for x in rels[:3]))
    if rec.get("trending"):
        parts.append("出现在 %s" % "、".join(rec["trending"]))
    if rec.get("official_week_stars") is not None:
        parts.append("GitHub 官方周榜本周新增 Stars %s" % f(rec["official_week_stars"]))
    if rec.get("pushed_at"):
        parts.append("最近推送 %s" % rec["pushed_at"].replace("T", " ").replace("Z", " UTC"))
    return "；".join(parts) + "。" if parts else "本次未取得可核验的本周事件。"


def release_lines(rec):
    rels = rec.get("releases") or []
    if not rels:
        return ["  - Release 下载量：仓库没有可统计的 Release，%s。" % NO_DL]
    out = []
    for x in rels[:3]:
        if x["asset_count"] == 0:
            out.append("  - Release 下载量：`%s`（%s，%s）的附件数为 0，该版本没有可统计的附件；%s。"
                       % (x["tag"], (x.get("published_at") or "")[:10],
                          "预发布" if x.get("prerelease") else "正式版", NO_DL))
        else:
            tops = "、".join("`%s` %s" % (a["name"], f(a["dl"])) for a in x["top_assets"][:3])
            out.append("  - Release 下载量：`%s`（%s，%s）的 %d 个附件累计 **%s 次**（截至查询日，非本周下载、非独立用户）；下载量最高的附件为 %s。"
                       % (x["tag"], (x.get("published_at") or "")[:10],
                          "预发布" if x.get("prerelease") else "正式版",
                          x["asset_count"], f(x["asset_downloads"]), tops or "无"))
            meta_names = [a["name"] for a in x["top_assets"]
                          if any(k in a["name"].lower() for k in (".zsync", "sha256", ".sig", "sigstore", "install.", "manifest"))]
            if meta_names:
                out.append("    其中含非安装包附件（`%s` 等），附件合计数会被这些文件抬高。" % meta_names[0])
    return out


def package_lines(rec, npm_range):
    pkgs = rec.get("packages") or {}
    out = []
    if pkgs.get("npm"):
        p = pkgs["npm"]
        out.append("  - 关联包下载量 · npm：包 `%s` 在 **%s 至 %s** 下载 **%s 次**（包获取次数，含 CI 与镜像重复，不是用户数）。"
                   % (p["pkg"], p.get("start"), p.get("end"), f(p["downloads"])))
    if pkgs.get("pypi"):
        p = pkgs["pypi"]
        dd = p["data"]
        out.append("  - 关联包下载量 · PyPI：包 `%s` 的 `last_week` **%s 次**、`last_month` %s 次、`last_day` %s 次（pypistats 滚动窗口，与自然周不完全重合）。"
                   % (p["pkg"], f(dd.get("last_week", 0)), f(dd.get("last_month", 0)), f(dd.get("last_day", 0))))
    if pkgs.get("crate"):
        p = pkgs["crate"]
        out.append("  - 关联包下载量 · crates.io：crate `%s` 累计下载 %s 次、近 90 天 %s 次（crates.io 口径，不是本周下载量）。"
                   % (p["pkg"], f(p.get("downloads") or 0), f(p.get("recent") or 0)))
    if not out:
        out.append("  - 关联包下载量：%s（未在配置中登记可核验的包名对应关系；同名包未采信）。" % NO_DL)
    return out


def rank_note(rec):
    r, sr = rec.get("rank", 0), rec.get("stars_rank", 0)
    if r < sr:
        return "排序说明：综合排名高于其 Stars 排名（第 %d 位），因为本周提交、窗口内发布或可核验分发数据更完整。" % sr
    if r > sr:
        return "排序说明：综合排名低于其 Stars 排名（第 %d 位），因为本周活跃度较低、缺少窗口内发布或可核验分发数据。" % sr
    return "排序说明：综合排名与社区规模排名一致（第 %d 位）。" % sr


def download_lines(rec):
    links = rec.get("download_links") or []
    if not links:
        return "本次未生成可核验的下载地址。"
    parts = []
    for x in links:
        status = x.get("status")
        if not status:
            tail = "（本次未验证可访问性）"
        elif status == "200":
            tail = "（已验证 HTTP 200）"
        elif status.startswith("本次未完成探测"):
            tail = "（%s；不代表链接无效）" % status
        else:
            tail = "（本次探测返回 HTTP %s）" % status
        parts.append("[%s](%s)%s" % (x["label"], x["url"], tail))
    return "；".join(parts) + "。下载源码不等于取得许可证之外的复用、商用或再分发授权。"


def source_line(rec, query_date):
    s = rec.get("sources") or {}
    bits = ["[仓库 API](%s)" % s.get("repo", ""), "[提交记录](%s)" % s.get("commits", ""),
            "[Releases API](%s)" % s.get("releases", ""), "[许可证端点](%s)" % s.get("license", "")]
    for v in (rec.get("packages") or {}).values():
        if v.get("src"):
            bits.append("[包下载 API](%s)" % v["src"])
    return " · ".join(bits) + "。查询日期 **%s**。" % query_date


def build_markdown(data, cfg):
    meta, recs, lims = data["meta"], data["records"], data["limitations"]
    start = meta["window_start"][:10]
    qdate = meta["run_date"]
    L = []
    L.append("# GitHub 开源资源日报（%s）" % qdate)
    L.append("")
    L.append("**统计窗口：** %s 至 %s（%s，统计截至本次运行查询时点）。GitHub 提交查询使用等价 UTC 起点 `%s`。"
             % (meta["window_start"], meta["window_end"], meta["timezone"], meta["since_utc"]))
    L.append("**查询日期：** %s（查询时点 %s）。" % (qdate, meta["query_time"]))
    L.append("**生成方式：** 本地独立程序 `run_daily.py` 会尝试采集 GitHub REST/Search API、Trending RSS 与 npm / PyPI / crates.io 下载统计，全程不需要大模型 API 密钥；本次哪些来源实际可用、哪些失败，见第一节逐条说明。程序参照 Horizon 的「每日调度 + 多源采集 + 生成 Markdown/HTML」骨架，但不使用其 AI 分析层，输出字段按日报规范单独设计。")
    L.append("**本次额度：** GitHub core API 剩余 %s 次、search 剩余 %s 次；%s额度重置时间（北京时间）%s。"
             % (data["budget"]["core_remaining"], data["budget"]["search_remaining"],
                "已使用 GITHUB_TOKEN；" if data["budget"]["has_token"] else "未使用 GITHUB_TOKEN；",
                data["budget"]["reset_local"] or "未知"))
    L.append("")
    L.append("---")
    L.append("")
    L.append("## 一、指标口径与数据限制（先读）")
    L.append("")
    for x in lims:
        L.append(x if x.startswith("- ") else "- " + x)
    L.append("")
    for label, info in (meta.get("rss") or {}).items():
        L.append("- %s：`pubDate` 为 **%s**，共解析 %d 条；来源为第三方聚合站 %s。"
                 % (label, info.get("pubDate") or "未取得", info.get("count", 0), info["url"]))
    L.append("- 本次检索：本周新建仓库返回 %d 条候选；GitHub 官方周榜解析 %d 条。"
             % (meta.get("created_search_count", 0), meta.get("official_trending_count", 0)))
    L.append("")
    L.append("**排序依据：** 综合分 = ln(Stars+1)×2 + ln(Forks+1) + 8×min(本周提交,50)/50 + 6×(窗口内有正式 Release) + 4×(Trending 榜收录) + 3×(本周新建)。"
             "Stars 与 Forks 是累计快照、存在滞后；排序是综合判断，不是本周新增 Stars 排名，也不是下载量排名。每条在「排序说明」里给出上调或下调理由。")
    L.append("")
    L.append("---")
    L.append("")
    L.append("## 二、本周关注资源（按综合热度与影响力排序）")
    L.append("")
    for rec in recs:
        L.append("### %d. [%s](%s) — %s" % (rec["rank"], rec["repo"], rec.get("url") or "", rec.get("category") or "未分类"))
        L.append("")
        L.append("- **类型与用途：** %s%s"
                 % ((rec.get("description") or "仓库未提供描述。").replace("\n", " "),
                    " 主要语言：%s。" % rec["language"] if rec.get("language") else ""))
        L.append("- **本周值得关注原因：** %s" % band_date(rec, start))
        L.append("- **热度指标与统计口径：**")
        L.append("  - Stars **%s**、Forks **%s**、Watchers **%s**（GitHub API 累计快照，%s 查询时点；非本周新增、非下载量）。"
                 % (f(rec.get("stars") or 0), f(rec.get("forks") or 0),
                    f(rec.get("watchers") or 0) if rec.get("watchers") is not None else "未取得", qdate))
        cw = rec.get("commits_week")
        L.append("  - 本周提交 **%s**（`since=%s`，北京时间 %s 00:00 起%s）。"
                 % (("≥%d 次（受单页上限截断）" % cw) if (cw and rec.get("commits_truncated")) else ("%d 次" % cw if cw is not None else "本次未核实"),
                    meta["since_utc"], meta["window_start"][:10],
                    "；单页上限 %d 条" % int(cfg.get("commits_per_page", 100))))
        L.extend(release_lines(rec))
        L.extend(package_lines(rec, meta["npm_range"]))
        L.append("  - %s 综合分 **%s**。" % (rank_note(rec), rec.get("score")))
        L.append("- **许可证与页面注明的使用限制：** GitHub API 标记为 **%s**。%s"
                 % (rec.get("license_spdx") or "未识别", rec.get("license_limits") or ""))
        L.append("- **可下载地址：** %s" % download_lines(rec))
        L.append("- **数据来源与查询日期：** %s" % source_line(rec, qdate))
        L.append("")
    L.append("---")
    L.append("")
    L.append("## 三、本周趋势与值得关注项目")
    L.append("")
    L.append("> 本章是基于本报告采集数据的自动归纳，不是可核验的外部事实；判断与推测不超出下列数据范围。")
    L.append("")
    cats = sorted({(r.get("category") or "未分类").split(" / ")[0] for r in recs})
    new_cnt = sum(1 for r in recs if r.get("created_in_window"))
    rel_cnt = sum(1 for r in recs if any(x.get("published_at") and x["published_at"][:10] >= start and not x.get("prerelease")
                                         for x in (r.get("releases") or [])))
    busy = sum(1 for r in recs if r.get("commits_truncated"))
    nodl = sum(1 for r in recs if not (r.get("releases") and any(x["asset_count"] for x in r["releases"]))
               and not r.get("packages"))
    nolic = sum(1 for r in recs if r.get("license_spdx") in (None, "NOASSERTION"))
    L.append("1. 本次收录 **%d** 条资源，覆盖 %d 个一级类别（%s）。其中本周新建 %d 条、窗口内有正式 Release %d 条、本周提交触及单页上限的 %d 条。"
             % (len(recs), len(cats), "、".join(cats), new_cnt, rel_cnt, busy))
    L.append("2. 有 **%d** 条既没有可统计的 Release 附件、也没有登记可核验的关联包，报告中已逐条标注「%s」；有 **%d** 条许可证未被 GitHub 自动识别或为自定义许可，复用前须阅读原文。"
             % (nodl, NO_DL, nolic))
    if recs:
        top = recs[:3]
        L.append("3. 按综合分排在前三的是 %s。该排序同时考虑社区规模与本周活跃度，不能单独当作热度或下载量排名。"
                 % "、".join("**%s**（%s 分）" % (r["repo"], r["score"]) for r in top))
    stars_sorted = sorted(recs, key=lambda x: -(x.get("stars") or 0))
    if len(stars_sorted) >= 2:
        hi, lo = stars_sorted[0], stars_sorted[-1]
        L.append("4. 社区规模跨度：%s 有 %s Stars，%s 有 %s Stars。Stars 是累计快照，与本周提交数、Release 附件下载、包下载属于不同量纲，本报告不把它们互相换算。"
                 % (hi["repo"], f(hi.get("stars") or 0), lo["repo"], f(lo.get("stars") or 0)))
    L.append("5. 关注度线索来自第三方 Trending RSS（见第一节 `pubDate`）。上榜反映讨论或收藏行为，不能替代提交与发布记录；本次数据中，%s。"
             % ("有 %d 条仓库本周提交为 0 次" % sum(1 for r in recs if r.get("commits_week") == 0)
                if any(r.get("commits_week") == 0 for r in recs) else "没有出现本周提交为 0 的条目"))
    L.append("")
    L.append("---")
    L.append("")
    L.append("## 四、本次运行说明")
    L.append("")
    L.append("- 程序：`run_daily.py`（Python 标准库，无第三方依赖、无大模型密钥）；配置：`config.json`。")
    L.append("- 统计窗口：%s 往前 %d 天；查询时点：%s。" % (qdate, meta["days"], meta["query_time"]))
    L.append("- 输出：`GitHub开源资源日报-%s.md` 与 `.html`，本次实际写入目录 %s。"
             % (qdate, meta.get("output_dir_actual") or cfg.get("output_dir")))
    L.append("- 报告中的事实均标注来源链接与查询日期；第三章的归纳已显式标注为基于本报告数据的判断，不是原始事实。")
    L.append("- 本次采集遇到的访问限制与口径说明见第一节，未对缺失数据作任何推算或补造。")
    L.append("")
    return "\n".join(L)


# ------------------------------ Markdown -> HTML ------------------------------

def _inline(s):
    s = _html.escape(s, quote=False)
    s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)",
               lambda m: '<a href="%s" target="_blank" rel="noopener">%s</a>'
               % (_html.escape(m.group(2), quote=True), m.group(1)), s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    return s


def _md_to_html_body(md):
    lines = md.split("\n")
    out, toc, stack, buf = [], [], [], []
    i, n = 0, len(lines)

    def close(to=0):
        while len(stack) > to:
            out.append("</%s>" % stack.pop()[0])

    def para():
        nonlocal buf
        if buf:
            out.append("<p>" + "<br>\n".join(_inline(x) for x in buf) + "</p>")
            buf = []

    while i < n:
        ln, s = lines[i], lines[i].strip()
        if s == "---":
            para(); close(); out.append("<hr>"); i += 1; continue
        m = re.match(r"^(#{1,4})\s+(.*)$", s)
        if m:
            para(); close()
            lv, txt = len(m.group(1)), m.group(2)
            a = "s%d" % len(toc)
            if lv == 1:
                i += 1; continue
            if lv in (2, 3):
                toc.append((lv, txt, a))
            out.append('<h%d id="%s">%s</h%d>' % (lv, a, _inline(txt), lv)); i += 1; continue
        if s.startswith("> "):
            para(); close(); out.append("<blockquote>" + _inline(s[2:]) + "</blockquote>"); i += 1; continue
        m = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", ln)
        if m:
            para()
            indent = len(m.group(1)) // 2
            is_ol = bool(re.match(r"\d+\.", m.group(2)))
            kind = "ol" if is_ol else "ul"
            st = int(m.group(2)[:-1]) if is_ol else None
            while len(stack) > indent + 1:
                out.append("</%s>" % stack.pop()[0])
            if len(stack) == indent + 1 and stack[-1][0] != kind:
                out.append("</%s>" % stack.pop()[0])
            if len(stack) < indent + 1:
                out.append('<ol start="%d">' % st if (is_ol and st and st > 1) else "<%s>" % kind)
                stack.append((kind, indent))
            out.append("<li>" + _inline(m.group(3)) + "</li>"); i += 1; continue
        if not s:
            para(); close(); i += 1; continue
        buf.append(s); i += 1
    para(); close()
    return "\n".join(out), toc


CSS = """
:root{--bg:#f7f8fa;--card:#fff;--ink:#1f2328;--muted:#5b6572;--line:#e3e7ed;--accent:#1a5fb4;--warnbg:#fff8e6;--codebg:#f0f2f5}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;font-size:16px;line-height:1.85;-webkit-font-smoothing:antialiased}
.wrap{max-width:980px;margin:0 auto;padding:32px 20px 80px}
header.top{background:linear-gradient(135deg,#1a5fb4,#0b6b58);color:#fff;border-radius:14px;padding:30px 34px;margin-bottom:22px;box-shadow:0 6px 22px rgba(20,50,90,.13)}
header.top h1{margin:0 0 10px;font-size:27px;letter-spacing:.5px}
header.top .meta{font-size:13.5px;opacity:.94;line-height:1.9}
header.top .chips{margin-top:14px;display:flex;flex-wrap:wrap;gap:8px}
header.top .chip{background:rgba(255,255,255,.18);border:1px solid rgba(255,255,255,.32);border-radius:999px;padding:3px 12px;font-size:12.5px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:8px 34px 30px;box-shadow:0 2px 10px rgba(20,40,70,.045)}
h2{font-size:21px;margin:38px 0 16px;padding-bottom:9px;border-bottom:2px solid var(--accent);color:#123e7c}
h3{font-size:17.5px;margin:30px 0 12px;padding:10px 14px;background:#eef4fc;border-left:4px solid var(--accent);border-radius:0 8px 8px 0;line-height:1.6}
h3 a{color:#123e7c}h4{font-size:16px;margin:22px 0 10px}
p{margin:11px 0}ul,ol{margin:9px 0 14px;padding-left:26px}li{margin:6px 0}ul ul,ol ul{margin:5px 0 6px}
blockquote{margin:14px 0;padding:10px 16px;background:#f4f7fb;border-left:4px solid #9db8d8;border-radius:0 8px 8px 0;color:#42536a;font-size:14px}
a{color:var(--accent);text-decoration:none;border-bottom:1px solid rgba(26,95,180,.28);word-break:break-word}
a:hover{border-bottom-color:var(--accent);background:#eef4fc}
strong{font-weight:650;color:#111820}
code{background:var(--codebg);border:1px solid #e1e5ea;border-radius:4px;padding:1px 5px;font-size:13px;font-family:"SFMono-Regular",Consolas,monospace;word-break:break-all}
hr{border:0;border-top:1px dashed var(--line);margin:30px 0}
nav.toc{background:#fff;border:1px solid var(--line);border-radius:14px;padding:18px 24px;margin-bottom:22px}
nav.toc b{display:block;font-size:14px;color:var(--muted);margin-bottom:10px;letter-spacing:1px}
nav.toc a{display:block;font-size:13.3px;padding:3px 0;border:0;color:#31404f}
nav.toc a.lv3{padding-left:18px;color:var(--muted);font-size:12.8px}
nav.toc a:hover{background:none;color:var(--accent)}
.note{background:var(--warnbg);border:1px solid #f0d9a0;border-left:4px solid #d99a20;border-radius:8px;padding:12px 16px;margin:18px 0;font-size:14px;color:#5c4300}
footer{margin-top:26px;text-align:center;color:var(--muted);font-size:12.5px;line-height:1.9}
@media(max-width:700px){.wrap{padding:16px 10px 50px}.card{padding:6px 18px 24px}header.top{padding:22px 20px}header.top h1{font-size:21px}h2{font-size:18px}h3{font-size:15.5px}body{font-size:15px}}
"""


def build_html(md, data):
    body, toc = _md_to_html_body(md)
    toc_html = "\n".join('<a class="t%s" href="#%s">%s</a>'
                         % (" lv3" if lv == 3 else "", a, re.sub(r"<[^>]+>", "", t))
                         for lv, t, a in toc)
    meta = data["meta"]
    doc = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GitHub 开源资源日报（%s）</title><style>%s</style></head><body><div class="wrap">
<header class="top"><h1>GitHub 开源资源日报（%s）</h1>
<div class="meta">统计窗口：%s — %s（%s）｜查询日期：%s<br>
本地独立程序生成（Python 标准库，无大模型密钥）｜收录 %d 项资源｜数据来源与口径见正文</div>
<div class="chips"><span class="chip">一手来源优先</span><span class="chip">Stars / Forks ≠ 下载量</span><span class="chip">口径逐项标注</span><span class="chip">限制如实说明</span></div></header>
<nav class="toc"><b>目录</b>%s</nav>
<div class="note">阅读提示：Stars、Forks、提交数、Release 附件下载、关联包下载是五种不同量纲。Stars / Forks 为累计快照，不代表本周新增，也不是下载量。无法核实的下载量一律标注「%s」。</div>
<article class="card">%s</article>
<footer>本文件由本地程序 run_daily.py 生成，与同名 Markdown 文件内容一致。<br>数据为 %s 查询时点快照，来源链接与统计口径见正文各条目。</footer>
</div></body></html>""" % (
        meta["run_date"], CSS, meta["run_date"],
        meta["window_start"], meta["window_end"], meta["timezone"], meta["run_date"],
        len(data["records"]), toc_html, NO_DL, body, meta["run_date"])
    return doc
