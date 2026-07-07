#!/usr/bin/env python3
"""
情报采集与分析 — 模块入口

封装的子命令：
  crawl      采集情报（原 news_crawler.py）
  export     导出 prompt（原 run_pipeline.py export）
  generate   生成报告（原 run_pipeline.py generate）
  all        全流程（crawl → export → generate）

LLM 默认禁用，通过 --llm 启用。

用法:
  python -m intel.main crawl                         # 仅规则分类
  python -m intel.main crawl --llm                   # 启用 LLM 分类
  python -m intel.main crawl --max-age 14
  python -m intel.main export                        # LLM 禁用
  python -m intel.main generate --llm               # 启用 LLM 生成
  python -m intel.main all --llm                     # 全流程带 LLM
"""

import sys, os

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from intel.collector.main import run_crawl_cli as crawl_fn
from intel.pipeline.main import cmd_export, cmd_generate


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="情报采集与分析 — BriefNexus 情报模块",
    )
    # 全局 --llm 标志
    parser.add_argument("--llm", action="store_true",
                        help="启用 LLM（默认禁用，避免因 LLM 崩溃）")

    sub = parser.add_subparsers(dest="command", help="子命令")

    p_crawl = sub.add_parser("crawl", help="采集情报")
    p_crawl.add_argument("--max-age", type=int, default=7,
                         help="采集最近 N 天的数据")
    p_crawl.add_argument("--verbose", action="store_true")

    p_export = sub.add_parser("export", help="导出 prompt")
    p_generate = sub.add_parser("generate", help="生成报告和话题帖")
    p_all = sub.add_parser("all", help="全流程：采集 → 导出 → 生成")

    args = parser.parse_args()
    use_llm = args.llm

    if args.command == "crawl":
        crawl_fn(args)
    elif args.command == "export":
        cmd_export(use_llm=use_llm)
    elif args.command == "generate":
        cmd_generate(use_llm=use_llm)
    elif args.command == "all":
        crawl_fn(args)
        cmd_export(use_llm=use_llm)
        cmd_generate(use_llm=use_llm)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
