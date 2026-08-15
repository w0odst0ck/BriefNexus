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
  python -m intel check [--domain d] [--timeout N] [--interval S] [--output path]
                                         # 健康巡检：实测各采集器可用性
"""
import argparse
import json
import logging
import os
import random
import sys
import threading
import time
from datetime import datetime

# load .env
from scripts._dotenv import load_project_env

load_project_env()

from intel.core.base import CST
from intel.core.dedup import DedupStore
from intel.core.registry import get_collector_classes, instantiate_collectors
from intel.pipeline.classifier import classify
from intel.pipeline.reporter import build_report

logger = logging.getLogger("intel")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(PROJECT_ROOT, "intel", "config", "sources.yaml")

UA = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/128.0.0.0 Safari/537.36",
]


def _load_config(path: str | None = None) -> dict:
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


def _sess(render: bool = False, render_timeout: float = 30.0):
    """构造会话; render=True 时包一层 RenderAwareSession(渲染感知)

    默认 render=False 行为与改动前逐字节一致(裸 requests.Session)。
    渲染仅对 sources.yaml 声明 render: true 的源启用。
    """
    import requests
    s = requests.Session()
    s.headers["User-Agent"] = random.choice(UA)
    s.headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    if render:
        # 延迟 import: 不渲染路径零开销, 且 render 模块不可用时不影响静态源
        from intel.core.render import RenderAwareSession, RenderExecutor
        return RenderAwareSession(
            s, RenderExecutor(default_timeout=render_timeout), timeout=render_timeout
        )
    return s


def cmd_list(domains: str | None = None):
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
            config_path: str | None = None, output_dir: str | None = None,
            domains: str | None = None):
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
    sources_cfg = (config or {}).get("sources", {}) or {}
    plain_sess = _sess()  # 非渲染源共享同一 plain Session(与现状一致)
    all_items = []

    # 持久化去重
    dedup = DedupStore()
    today = datetime.now(CST).strftime("%Y-%m-%d")

    for collector in collectors:
        logger.info("[%s] 采集...", collector.display_name)
        try:
            src_cfg = sources_cfg.get(collector.source_name, {}) or {}
            # 严格布尔判定: 仅 render is True 走渲染, 其余一律 plain(与现状一致)
            sess = _sess(render=True) if src_cfg.get("render") is True else plain_sess
            items = collector.crawl(sess)
            # 跨天去重（标题 MD5）
            new_titles = dedup.filter_new([it.title for it in items])
            seen_titles = set()
            for it in items:
                if it.title not in seen_titles and it.title in new_titles:
                    seen_titles.add(it.title)
                    all_items.append(it)
            logger.info("  → %d 条（跨天去重后）", len(items))
        except Exception as e:
            logger.error("  [FAIL] %s: %s", collector.display_name, e)
        time.sleep(random.uniform(0.5, 1.5))

    # 标记本次采集的标题到持久化存储
    dedup.mark_seen_batch([it.title for it in all_items], today)
    dedup.save()
    logger.info("采集完成: %d 条（跨天去重后）", len(all_items))

    # 3. 分类
    classify(all_items)
    logger.info("分类完成: %d 个板块", len({it.sector for it in all_items if it.sector}))

    # 4. 输出报告
    if output_dir is None:
        output_dir = os.path.join(PROJECT_ROOT, "intel", "output")

    # 归档输出（按日期分目录，含时间戳，永不覆盖）
    ts = datetime.now(CST).strftime("%Y%m%d_%H%M%S")
    archive_dir = os.path.join(output_dir, "archive", today)
    os.makedirs(archive_dir, exist_ok=True)
    archive_path = os.path.join(archive_dir, f"{ts}.json")
    archive_content = build_report(all_items, fmt="json")  # 不传 output_dir → 返回字符串
    with open(archive_path, "w", encoding="utf-8") as f:
        f.write(archive_content)
    logger.info("归档: %s", archive_path)

    # JSON 报告（最新快照，覆盖）
    json_path = os.path.join(output_dir, f"report_{today}.json")
    build_report(all_items, fmt="json", output_dir=output_dir)
    logger.info("最新快照: %s", json_path)

    # MD 报告（附带）
    if fmt == "md":
        md_path = os.path.join(output_dir, f"report_{today}.md")
        build_report(all_items, fmt="md", output_dir=output_dir)
        logger.info("MD 报告: %s", md_path)

    # 去重存储清理
    dedup.cleanup()
    dedup.save()

    print(f"\n>>> 完成: {len(all_items)} 条 | JSON: {json_path} | 归档: {archive_path}")


def _error_summary(exc: BaseException) -> str:
    """异常摘要：类名 + 消息截断，避免表格被超长消息撑爆"""
    msg = str(exc).strip() or type(exc).__name__
    return f"{type(exc).__name__}: {msg[:200]}"


def _crawl_once(collector, sess, timeout: float):
    """在 daemon 线程中调用 crawl，外层硬超时兜底

    返回 (items, None) 正常；超时返回 (None, "TimeoutError: 超过 Ns")；
    crawl 抛任意异常则返回 (None, 异常摘要)。daemon 线程不阻止进程退出，
    单源超时后即放弃，不串扰后续巡检。
    """
    box = {}

    def _target():
        try:
            box["items"] = collector.crawl(sess)
        except BaseException as e:  # 采集器可能抛任意异常，均记为 failed
            box["error"] = _error_summary(e)

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return None, f"TimeoutError: crawl 超过 {timeout}s"
    return box.get("items"), box.get("error")


def cmd_check(domain: str | None = None, timeout: float = 20.0, interval: float = 2.0,
              config_path: str | None = None, output_dir: str | None = None) -> int:
    """健康巡检：逐个启用采集器实测可用性，输出表格 + 记录时间线

    退出码: 全部 ok → 0；任一 failed → 1（参数错误由 argparse 以 2 退出）
    """
    if timeout <= 0 or interval < 0:
        print(f"参数错误: --timeout 必须 > 0（收到 {timeout}）, --interval 必须 >= 0（收到 {interval}）")
        return 2

    config = _load_config(config_path)
    classes = get_collector_classes(config, domains=domain)
    collectors = instantiate_collectors(config, domains=domain)

    if not collectors:
        logger.error("无可用采集器，退出")
        return 1

    if output_dir is None:
        output_dir = os.path.join(PROJECT_ROOT, "intel", "output")
    os.makedirs(output_dir, exist_ok=True)

    today = datetime.now(CST).strftime("%Y-%m-%d")
    results = []
    any_failed = False

    print(f"健康巡检 — {len(collectors)} 个启用采集器 | 每源超时 {timeout}s | 间隔 {interval}s")
    print(f"{'采集器':<20} {'display_name':<28} {'域':<16} {'状态':<7} {'条目':>4} {'耗时':>8}  错误")
    print("-" * 100)

    for i, collector in enumerate(collectors):
        name = collector.source_name or collector.__class__.__name__
        display = collector.display_name or name
        cls = classes.get(name) or collector
        domains = ",".join(getattr(cls, "domains", []) or [])
        src_cfg = (config or {}).get("sources", {}).get(name, {}) or {}
        sess = _sess(render=(src_cfg.get("render") is True))
        start = time.monotonic()
        items, error = _crawl_once(collector, sess, timeout)
        duration = time.monotonic() - start

        if error:
            status, n_items = "failed", 0
            any_failed = True
        else:
            status, n_items = "ok", len(items or [])

        results.append({
            "source": name, "display_name": display, "domain": domains,
            "status": status, "items": n_items,
            "duration": round(duration, 3), "error": error,
        })
        print(f"{name:<20} {display:<28} {domains:<16} {status:<7} {n_items:>4} {duration:>7.2f}s  {error or ''}")

        if interval > 0 and i < len(collectors) - 1:
            time.sleep(interval)

    # 写当日汇总 + 时间线追加
    daily_path = os.path.join(output_dir, f"health-{today}.json")
    summary = {
        "date": today,
        "total": len(results),
        "ok": sum(1 for r in results if r["status"] == "ok"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "results": results,
    }
    with open(daily_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    history_path = os.path.join(output_dir, "health-history.jsonl")
    with open(history_path, "a", encoding="utf-8") as f:
        for r in results:
            line = {
                "timestamp": datetime.now(CST).isoformat(timespec="seconds"),
                "source": r["source"], "status": r["status"],
                "items": r["items"], "duration": r["duration"], "error": r["error"],
            }
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    print(f"\n记录: {daily_path} | 时间线: {history_path}")
    return 1 if any_failed else 0


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

    p_check = sub.add_parser("check", help="健康巡检：实测各采集器可用性")
    p_check.add_argument("--domain", help="按领域过滤（逗号组合）")
    p_check.add_argument("--timeout", type=float, default=20.0,
                         help="每源外层硬超时秒数（默认 20）")
    p_check.add_argument("--interval", type=float, default=2.0,
                         help="源间间隔秒数（默认 2，礼貌抓取）")
    p_check.add_argument("--output", help="输出目录（默认 intel/output/）")

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
    elif args.command == "check":
        sys.exit(cmd_check(
            domain=args.domain,
            timeout=args.timeout,
            interval=args.interval,
            output_dir=args.output,
        ))


if __name__ == "__main__":
    main()
