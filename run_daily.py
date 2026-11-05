# -*- coding: utf-8 -*-
"""GitHub 开源资源日报 —— 本地独立入口（仅用 Python 标准库，无需 pip 依赖、无需大模型密钥）。

用法示例：
    python run_daily.py                          # 以运行当天为截止，覆盖过去一周
    python run_daily.py --date 2026-11-05       # 指定报告日期
    python run_daily.py --output-dir D:\\tmp     # 试运行，输出到别处
    python run_daily.py --dry-run               # 只采集，不写报告
    python run_daily.py --max-items 8           # 临时减少收录条数（API 额度紧张时用）

输出：
    <output_dir>/GitHub开源资源日报-YYYY-MM-DD.md
    <output_dir>/GitHub开源资源日报-YYYY-MM-DD.html

调度：
    本脚本只负责「跑一次」。每日触发由外部调度器负责，见 install_task.ps1。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
import traceback
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import collector  # noqa: E402
import report  # noqa: E402

TZ = dt.timezone(dt.timedelta(hours=8))


def setup_logger(log_dir, verbose):
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    lg = logging.getLogger("gh_digest")
    lg.setLevel(logging.DEBUG if verbose else logging.INFO)
    lg.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(log_dir / (dt.datetime.now(TZ).strftime("%Y-%m") + ".log"), encoding="utf-8")
    fh.setFormatter(fmt)
    lg.addHandler(fh)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    lg.addHandler(sh)
    return lg


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    return {k: v for k, v in cfg.items() if not k.startswith("_")}


def main(argv=None):
    ap = argparse.ArgumentParser(description="生成 GitHub 开源资源日报（Markdown + HTML）")
    ap.add_argument("--config", default=str(BASE / "config.json"), help="配置文件路径")
    ap.add_argument("--date", help="报告日期 YYYY-MM-DD，默认运行当天（北京时间）")
    ap.add_argument("--output-dir", help="输出目录，覆盖配置中的 output_dir")
    ap.add_argument("--max-items", type=int, help="最多收录条数，覆盖配置中的 max_items")
    ap.add_argument("--dry-run", action="store_true", help="只采集并打印摘要，不写文件")
    ap.add_argument("--verbose", action="store_true", help="输出调试日志")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    if args.max_items:
        cfg["max_items"] = args.max_items
    log = setup_logger(cfg.get("log_dir", BASE / "logs"), args.verbose)

    if args.date:
        try:
            run_date = dt.datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            log.error("--date 格式不正确，应为 YYYY-MM-DD")
            return 2
    else:
        run_date = dt.datetime.now(TZ).date()

    log.info("=== 开始生成 %s 的日报 ===", run_date)
    try:
        data = collector.collect_all(cfg, run_date, log)
    except Exception:
        log.error("采集阶段异常：\n%s", traceback.format_exc())
        return 1

    if not data["records"]:
        log.error("没有采集到任何仓库，未生成报告。限制信息：%s", " | ".join(data["limitations"][:5]))
        return 2

    out_dir = Path(args.output_dir or cfg["output_dir"])
    md = report.build_markdown(data, cfg)
    htmldoc = report.build_html(md, data)

    if args.dry_run:
        print("收录 %d 条；Markdown %d 字符；HTML %d 字符；限制说明 %d 条。"
              % (len(data["records"]), len(md), len(htmldoc), len(data["limitations"])))
        for r in data["records"]:
            print("  #%-2d %-45s 综合分 %-8s Stars %s" % (r["rank"], r["repo"], r["score"], r.get("stars")))
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = "%s-%s" % (cfg.get("filename_prefix", "GitHub开源资源日报"), run_date.strftime("%Y-%m-%d"))
    p_md, p_html = out_dir / (stem + ".md"), out_dir / (stem + ".html")
    p_md.write_text(md, encoding="utf-8")
    p_html.write_text(htmldoc, encoding="utf-8")
    log.info("已写出 %s (%d bytes)", p_md, p_md.stat().st_size)
    log.info("已写出 %s (%d bytes)", p_html, p_html.stat().st_size)
    print("OK|%s|%s|items=%d|limitations=%d" % (p_md, p_html, len(data["records"]), len(data["limitations"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
