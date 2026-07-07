#!/usr/bin/env python3
"""
行业标准采集 — CLI 入口

用法:
    # 采集
    python -m standards.crawler.main collect

    # 搜索（FTS5 全文检索）
    python -m standards.crawler.main search "LED 筒灯"
    python -m standards.crawler.main search "GB/T 39394"
    python -m standards.crawler.main search "照明" --status 现行 --limit 10

    # 精确过滤
    python -m standards.crawler.main list --status 现行 --category 国标
    python -m standards.crawler.main list --date-from 2024-01-01

    # 统计
    python -m standards.crawler.main stats

    # 兼容旧语法（直接运行 = collect）
    python -m standards.crawler.main
"""

import sys
import os

_script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_project_root = os.path.dirname(_script_dir)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from standards.engine.collector import run_cli

if __name__ == "__main__":
    run_cli()
