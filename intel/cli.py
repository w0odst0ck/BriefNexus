#!/usr/bin/env python3
"""
情报采集框架 — 统一入口

用法:
  python -m intel run                    # 采集 + 分类 + 默认 JSON 报告
  python -m intel run --format md        # Markdown 简报
  python -m intel run --llm              # LLM 增强
  python -m intel run --max-age 14       # 采集近 14 天
  python -m intel run --config path      # 指定配置
  python -m intel list                   # 列出已注册采集器
"""
import argparse
import logging
import os
import random
import sys
import time
from datetime import datetime
from typing import List

# load .env
from scripts._dotenv import load_project_env; load_project_env()

from intel.core.base import NewsItem, CST
from intel.core.registry import register, get_collector_classes, instantiate_collectors
from intel.pipeline.classifier import classify
from intel.pipeline.reporter import build_report

logger = logging.getLogger("intel")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(PROJECT_ROOT, "intel", "config", "sources.yaml")

UA = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/128.0.0.0 Safari/537.36",
]


def _load_config(path: str = None) -> dict:
    """加载 YAML 配置"""
    path = path or DEFAULT_CONFIG
    if not os.path.exists(path):
        return {}
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        logger.warning("yaml 未安装，使用默认配置")
        return {}
    except Exception as e:
        logger.warning("配置加载失败: %s", e)
        return {}


def _sess():
    import requests
    s = requests.Session()
    s.headers["User-Agent"] = random.choice(UA)
    s.headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    return s


def cmd_list(domains: str = None):
    """列出已注册采集器（可按领域筛选）"""
    all_classes = get_collector_classes()
    if domains:
        classes = get_collector_classes(domains=domains)
        print(f"已注册采集器 — 领域: {domains} ({len(classes)}):")
        for name, cls in sorted(classes.items()):
            ds = ", ".join(getattr(cls, "domains", []))
            print(f"  {name:20s} [{ds}]")
    else:
        # 按领域分组展示
        domain_map = {}
        for name, cls in all_classes.items():
            ds = getattr(cls, "domains", [])
            for d in ds:
                domain_map.setdefault(d, []).append(name)
        print(f"已注册采集器 ({len(all_classes)}):")
        for domain in sorted(domain_map):
            names = sorted(domain_map[domain])
            print(f"  领域: {domain:15s} → {len(names)} 源 — {', '.join(names)}")
        print()
        print("用法: python -m intel.cli run -d <领域>")
        print("       python -m intel.cli list -d <领域>")


def cmd_run(max_age: int = 7, fmt: str = "json", use_llm: bool = False,
            config_path: str = None, output_dir: str = None, domains: str = None):
    """全流程：采集 → 分类 → 输出报告"""

    # 1. 加载配置
    config = _load_config(config_path)
    collectors = instantiate_collectors(config, domains=domains)

    if not collectors:
        logger.error("无可用采集器，退出")
        return

    logger.info("=" * 50)
    logger.info("情报采集 — 近 %d 天", max_age)
    logger.info("数据源: %s", ", ".join(c.display_name for c in collectors))
    logger.info("=" * 50)

    # 2. 采集
    sess = _sess()
    all_items = []
    seen = set()

    for collector in collectors:
        logger.info("[%s] 采集...", collector.display_name)
        try:
            items = collector.crawl(sess)
            # 去重
            for it in items:
                import hashlib
                dk = hashlib.md5(it.title.encode()).hexdigest()
                if dk not in seen:
                    seen.add(dk)
                    all_items.append(it)
            logger.info("  → %d 条", len(items))
        except Exception as e:
            logger.error("  [FAIL] %s: %s", collector.display_name, e)
        time.sleep(random.uniform(0.5, 1.5))

    logger.info("采集完成: %d 条（去重后）", len(all_items))

    # 3. 分类
    classify(all_items)
    logger.info("分类完成: %d 个板块", len(set(it.sector for it in all_items if it.sector)))

    # 4. 输出报告
    if output_dir is None:
        output_dir = os.path.join(PROJECT_ROOT, "intel", "output")

    # JSON 报告（主输出）
    json_path = os.path.join(output_dir, f"report_{datetime.now(CST).strftime('%Y-%m-%d')}.json")
    build_report(all_items, fmt="json", output_dir=output_dir)
    logger.info("JSON 报告: %s", json_path)

    # MD 报告（附带）
    if fmt == "md":
        md_path = os.path.join(output_dir, f"report_{datetime.now(CST).strftime('%Y-%m-%d')}.md")
        build_report(all_items, fmt="md", output_dir=output_dir)
        logger.info("MD 报告: %s", md_path)

    print(f"\n>>> 完成: {len(all_items)} 条 | JSON: {json_path}")


def main():
    parser = argparse.ArgumentParser(description="BriefNexus 情报采集框架")
    sub = parser.add_subparsers(dest="command", help="子命令")

    p_run = sub.add_parser("run", help="采集指定领域的数据")
    p_run.add_argument("-d", "--domain", required=True,
                       help="领域: finance / self_driving / semiconductor (支持逗号组合)")
    p_run.add_argument("--max-age", type=int, default=7, help="采集近 N 天数据（默认 7）")
    p_run.add_argument("--format", choices=["json", "md"], default="json",
                       help="输出格式（默认 json）")
    p_run.add_argument("--llm", action="store_true", help="启用 LLM 增强（需 API Key）")
    p_run.add_argument("--config", help="配置文件路径")
    p_run.add_argument("--output", help="输出目录")

    p_list = sub.add_parser("list", help="列出已注册采集器")
    p_list.add_argument("-d", "--domain", help="按领域筛选")

    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        return

    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.command == "list":
        cmd_list(domains=args.domain)
    elif args.command == "run":
        cmd_run(
            max_age=args.max_age,
            fmt=args.format,
            use_llm=args.llm,
            config_path=args.config,
            output_dir=args.output,
            domains=args.domain,
        )


if __name__ == "__main__":
    main()
