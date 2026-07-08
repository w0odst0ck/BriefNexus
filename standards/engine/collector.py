"""
采集调度引擎 — 协调多平台采集、去重、导出、SQLite 入库
"""

import logging
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional

# 将项目根加入 path（方便直接运行）
_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from standards.crawler.utils import load_config, reload_config, logger
# 动态加载，不再需要静态 import
# 平台通过 config.ini 中的 enable_X 标志和 PLATFORM_MAP 自动加载
from standards.engine.dedup import (
    deduplicate, merge_sources, filter_by_keywords, add_standard_meta
)
from standards.engine.exporter import auto_export
from standards.engine.storage import StandardDB

CST = timezone(timedelta(hours=8))


class CollectorEngine:
    """采集调度引擎"""

    def __init__(self, config_path: str = None):
        self.cfg = load_config(config_path)
        # 输出目录（绝对路径）
        rel_output = self.cfg.get("output", "output_dir", fallback="standards/output")
        self.output_dir = os.path.join(_project_root, rel_output)
        os.makedirs(self.output_dir, exist_ok=True)

        self.collectors = {}
        self._init_collectors()

    def _init_collectors(self):
        """初始化启用的采集器（从配置动态加载）"""
        # 配置格式: enable_X = true  → 映射为 X:intel...:Class
        PLATFORM_MAP = {
            "samr":  "standards.crawler.platforms.samr:SamrCollector",
            "spc":   "standards.crawler.platforms.spc:SpcCollector",
            "csres": "standards.crawler.platforms.csres:CsresCollector",
        }
        import importlib
        for key, module_class in PLATFORM_MAP.items():
            enabled = self.cfg.getboolean("crawler", f"enable_{key}", fallback=False)
            if not enabled:
                continue
            module_path, class_name = module_class.rsplit(":", 1)
            try:
                mod = importlib.import_module(module_path)
                cls = getattr(mod, class_name)
                self.collectors[key] = cls()
                logger.info("加载平台: %s → %s", key, class_name)
            except Exception as e:
                logger.error("加载平台失败 [%s]: %s", key, e)

    @property
    def domain_name(self) -> str:
        return self.cfg.get("domain", "name", fallback="行业标准")

    @property
    def keywords(self) -> list:
        raw = self.cfg.get("domain", "keywords", fallback="")
        return [kw.strip() for kw in raw.split(",") if kw.strip()]

    @property
    def ics_codes(self) -> list:
        raw = self.cfg.get("domain", "ics_codes", fallback="")
        codes = [c.strip() for c in raw.split(",") if c.strip()]

        # 展开子代码
        expanded = []
        for code in codes:
            section = f"ics.{code}"
            if self.cfg.has_section(section):
                subcodes = self.cfg.get(section, "subcodes", fallback="")
                expanded.extend([c.strip() for c in subcodes.split(",") if c.strip()])
            else:
                expanded.append(code)
        return expanded

    @property
    def max_pages(self) -> int:
        return int(self.cfg.get("crawler", "max_pages", fallback="20"))

    def collect_all(self, save_to_db: bool = True) -> List[dict]:
        """
        全量采集：所有平台 + 关键词 + ICS 代码

        Args:
            save_to_db: 是否自动写入 SQLite

        Returns:
            合并去重后的标准条目列表
        """
        logger.info("=" * 50)
        logger.info("领域: %s", self.domain_name)
        logger.info("关键词: %s", self.keywords)
        logger.info("ICS代码: %s", self.ics_codes)
        logger.info("启用平台: %s", list(self.collectors.keys()))
        logger.info("最大页数: %d", self.max_pages)
        logger.info("=" * 50)

        source_results = {}

        for name, collector in self.collectors.items():
            logger.info("[%s] 开始采集...", collector.display_name)
            try:
                items = collector.collect(
                    keywords=self.keywords,
                    ics_codes=self.ics_codes,
                    max_pages=self.max_pages,
                )
                source_results[name] = items
                logger.info("[%s] 完成 → %d 条", collector.display_name, len(items))
            except Exception as e:
                logger.error("[%s] 采集异常: %s", collector.display_name, e,
                             exc_info=True)
                source_results[name] = []

        # 合并去重
        merged = merge_sources(source_results)
        merged = add_standard_meta(merged)

        # 关键词过滤（二次过滤，确保精确匹配）
        filtered = filter_by_keywords(merged, self.keywords)

        # 过滤废止标准
        before_status = len(filtered)
        filtered = [it for it in filtered if it.get("status", "") != "废止"]
        if len(filtered) < before_status:
            logger.info("剔除废止标准: %d 条 → %d 条", before_status, len(filtered))

        logger.info("=" * 50)
        logger.info("采集汇总: %d 条(去重) → %d 条(关键词过滤)",
                    len(merged), len(filtered))
        logger.info("=" * 50)

        # 写入 SQLite
        if save_to_db and filtered:
            try:
                db = StandardDB()
                n = db.save_items(filtered)
                db.close()
                logger.info("SQLite 入库: %d 条 → %s", n, os.path.join(_project_root, "standards.db"))
            except Exception as e:
                logger.error("SQLite 入库失败: %s", e)

        return filtered

    def export(self, items: List[dict], formats: List[str] = None):
        """导出结果"""
        auto_export(items, self.output_dir, self.domain_name, formats)


# ── CLI 入口 ──────────────────────────────────────────────
def run_cli():
    import argparse

    parser = argparse.ArgumentParser(
        description="行业标准采集器 — BriefNexus 模块"
    )

    # 主命令
    sub = parser.add_subparsers(dest="command", help="子命令")

    # collect: 采集
    p_collect = sub.add_parser("collect", help="采集标准数据")
    p_collect.add_argument("--config", help="配置文件路径")
    p_collect.add_argument("--reload", action="store_true", help="重载配置")
    p_collect.add_argument("--dry-run", action="store_true", help="模拟运行（不导出）")
    p_collect.add_argument("--format", nargs="+", default=["json", "md"],
                           help="输出格式 (json/csv/md)")
    p_collect.add_argument("--no-db", action="store_true",
                           help="不写入 SQLite")
    p_collect.add_argument("--verbose", action="store_true", help="详细日志")

    # search: 全文搜索
    p_search = sub.add_parser("search", help="全文搜索标准数据")
    p_search.add_argument("query", nargs="?", help="搜索关键词")
    p_search.add_argument("--status", help="过滤状态: 现行/废止/即将实施")
    p_search.add_argument("--category", help="过滤分类: 国标/行标/团标")
    p_search.add_argument("--source", help="过滤数据源: samr/spc/csres")
    p_search.add_argument("--limit", type=int, default=20, help="返回条数")
    p_search.add_argument("--db", help="数据库路径")
    p_search.add_argument("--json", action="store_true", help="JSON 格式输出")

    # list: 精确过滤
    p_list = sub.add_parser("list", help="精确过滤查询")
    p_list.add_argument("--status", help="状态")
    p_list.add_argument("--category", help="分类")
    p_list.add_argument("--source", help="数据源")
    p_list.add_argument("--standard-no", help="标准号")
    p_list.add_argument("--date-from", help="发布日期 >= (YYYY-MM-DD)")
    p_list.add_argument("--date-to", help="发布日期 <= (YYYY-MM-DD)")
    p_list.add_argument("--limit", type=int, default=20)
    p_list.add_argument("--db", help="数据库路径")
    p_list.add_argument("--json", action="store_true", help="JSON 格式输出")

    # stats: 统计
    p_stats = sub.add_parser("stats", help="数据库统计")
    p_stats.add_argument("--db", help="数据库路径")

    # tree: ICS 分类树
    p_tree = sub.add_parser("tree", help="ICS 分类树浏览")
    p_tree.add_argument("code", nargs="?", default="",
                        help="ICS 节点代码（留空=根节点）")
    p_tree.add_argument("--standards", action="store_true",
                        help="列出当前节点下的标准")
    p_tree.add_argument("--limit", type=int, default=20,
                        help="列出标准条数")
    p_tree.add_argument("--path", action="store_true",
                        help="显示节点完整路径")
    p_tree.add_argument("--db", help="数据库路径")
    p_tree.add_argument("--json", action="store_true",
                        help="JSON 格式输出")

    # enrich: 补充 ICS 代码
    p_enrich = sub.add_parser("enrich", help="从详情页补充 ICS 代码")
    p_enrich.add_argument("--limit", type=int, default=0,
                          help="处理条数上限（0=全部）")
    p_enrich.add_argument("--workers", type=int, default=3,
                          help="并发数")
    p_enrich.add_argument("--db", help="数据库路径")

    # detail: 查看标准详情
    p_detail = sub.add_parser("detail", help="查看标准详情（含链接）")
    p_detail.add_argument("query", help="标准号或关键词")
    p_detail.add_argument("--db", help="数据库路径")

    # fetch: 批量下载全文
    p_fetch = sub.add_parser("fetch", help="下载标准全文 PDF（通过 openstd 平台）")
    p_fetch.add_argument("query", nargs="?", help="指定标准号（留空=全部）")
    p_fetch.add_argument("--limit", type=int, default=0,
                         help="下载上限")
    p_fetch.add_argument("--workers", type=int, default=3,
                         help="并发数")
    p_fetch.add_argument("--db", help="数据库路径")
    p_fetch.add_argument("--alt", action="store_true",
                         help="从 bzxz.net 抓取标准正文文本")
    p_fetch.add_argument("--scan-pages", type=int, default=30,
                         help="bzxz.net 列表页扫描页数 (默认 30)")
    p_fetch.add_argument("--domestic-only", action="store_true",
                         help="仅处理非采标 (85 条)")
    p_fetch.add_argument("--playwright", action="store_true",
                         help="使用 Playwright 浏览器自动化下载夸克 PDF")
    p_fetch.add_argument("--headless", action="store_true",
                         help="Playwright 无头模式")
    p_fetch.add_argument("--save-auth", action="store_true",
                         help="Playwright: 保存夸克登录态")
    p_fetch.add_argument("--bzxz-map",
                         help="Playwright: bzxz_id 映射 JSON")

    # read: 打开本地 PDF

    # note: 添加/更新备注
    p_note = sub.add_parser("note", help="添加标准备注（全文摘要/本地路径等）")
    p_note.add_argument("query", help="标准号")
    p_note.add_argument("--text", help="备注内容")
    p_note.add_argument("--path", help="本地文件路径")
    p_note.add_argument("--db", help="数据库路径")

    # 兼容旧用法（无子命令 = collect）
    args = parser.parse_args()

    # 无子命令 -> 默认 collect
    if args.command is None:
        _run_collect(parser.parse_args(["collect"] + sys.argv[1:]))
        return

    # 日志
    verbose = getattr(args, "verbose", False)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.command == "collect":
        _run_collect(args)
    elif args.command == "search":
        _run_search(args)
    elif args.command == "list":
        _run_list(args)
    elif args.command == "stats":
        _run_stats(args)
    elif args.command == "tree":
        _run_tree(args)
    elif args.command == "enrich":
        _run_enrich(args)
    elif args.command == "detail":
        _run_detail(args)
    elif args.command == "note":
        _run_note(args)
    elif args.command == "fetch":
        _run_fetch(args)


# ── 子命令实现 ──────────────────────────────────────────

def _run_collect(args):
    if args.reload:
        reload_config()

    engine = CollectorEngine(args.config)
    items = engine.collect_all(save_to_db=not args.no_db)

    if not args.dry_run and items:
        engine.export(items, args.format)

    print(f"\n>>> 完成: {len(items)} 条标准 | 输出: {engine.output_dir}/")
    return items


def _run_search(args):
    db = StandardDB(args.db)
    query = args.query

    if query:
        # 检查是否是标准号精确搜索
        if re.match(r'^[A-Za-z0-9/.\-—–\s]{3,}$', query.strip()):
            # 尝试精确匹配
            results = db.filter(standard_no=query.strip(), limit=args.limit)
            if not results:
                results = db.search(query, limit=args.limit,
                                    status=args.status,
                                    category=args.category,
                                    source=args.source)
        else:
            results = db.search(query, limit=args.limit,
                                status=args.status,
                                category=args.category,
                                source=args.source)

        total = db.search_count(query, status=args.status,
                                category=args.category,
                                source=args.source)
    else:
        results = db.filter(limit=args.limit, status=args.status,
                            category=args.category, source=args.source)
        total = db.filter_count(status=args.status,
                                category=args.category,
                                source=args.source)

    db.close()

    if args.json:
        import json as _json
        print(_json.dumps(results, ensure_ascii=False, indent=2))
        return results

    print(f"{'='*60}")
    print(f"  搜索: \"{query or '(全部)'}\"  |  命中: {total} 条  |  显示: {min(len(results), args.limit)} 条")
    if args.status:
        print(f"  过滤状态: {args.status}")
    if args.category:
        print(f"  过滤分类: {args.category}")
    if args.source:
        print(f"  过滤来源: {args.source}")
    print(f"{'='*60}")

    for i, r in enumerate(results, 1):
        status_tag = f"[{r['status']}]" if r.get("status") else ""
        cat_tag = f"({r['category']})" if r.get("category") else ""
        print(f"\n  {i}. {r['standard_no']} {r['title']} {status_tag} {cat_tag}")
        if r.get("publisher"):
            print(f"      发布: {r['publisher']}  |  日期: {r.get('publish_date', '')}")
        if r.get("scopes"):
            print(f"      范围: {r['scopes'][:80]}{'...' if len(r.get('scopes', '')) > 80 else ''}")
        if r.get("url"):
            print(f"      来源: {r['url']}")

    return results


def _run_list(args):
    db = StandardDB(args.db)
    results = db.filter(
        status=args.status,
        category=args.category,
        source=args.source,
        standard_no=getattr(args, "standard_no", None),
        date_from=getattr(args, "date_from", None),
        date_to=getattr(args, "date_to", None),
        limit=args.limit,
    )
    total = db.filter_count(
        status=args.status,
        category=args.category,
        source=args.source,
        standard_no=getattr(args, "standard_no", None),
        date_from=getattr(args, "date_from", None),
        date_to=getattr(args, "date_to", None),
    )
    db.close()

    if args.json:
        import json as _json
        print(_json.dumps(results, ensure_ascii=False, indent=2))
        return results

    print(f"{'='*60}")
    print(f"  精确过滤  |  命中: {total} 条  |  显示: {min(len(results), args.limit)} 条")
    if args.status:
        print(f"  状态: {args.status}")
    if args.category:
        print(f"  分类: {args.category}")
    if args.source:
        print(f"  来源: {args.source}")
    if getattr(args, "standard_no", None):
        print(f"  标准号: {args.standard_no}")
    print(f"{'='*60}")

    for i, r in enumerate(results, 1):
        print(f"\n  {i}. {r['standard_no']} {r['title']}")
        print(f"      状态: {r['status']}  |  分类: {r['category']}  |  来源: {r['source']}")
        print(f"      日期: {r['publish_date']}  |  发布: {r['publisher']}")

    return results


def _run_stats(args):
    db = StandardDB(args.db)
    s = db.stats()
    db.close()

    print(f"{'='*60}")
    print(f"  行业标准数据库 — 统计")
    print(f"{'='*60}")
    print(f"  总条数:     {s['total']}")
    print(f"  最后更新:   {s['last_update']}")
    print(f"  数据库:     {s['db_path']}")
    print()
    if s["by_status"]:
        print(f"  ── 按状态 ──")
        for k, v in s["by_status"].items():
            print(f"    {k}: {v}")
    if s["by_category"]:
        print(f"  ── 按分类 ──")
        for k, v in s["by_category"].items():
            print(f"    {k}: {v}")
    if s["by_source"]:
        print(f"  ── 按来源 ──")
        for k, v in s["by_source"].items():
            print(f"    {k}: {v}")


def _run_tree(args):
    db = StandardDB(args.db)

    if not args.code:
        # 根节点列表
        children = db.tree_children("")
        db.close()

        if args.json:
            import json as _json
            print(_json.dumps(children, ensure_ascii=False, indent=2))
            return

        print(f"{'='*60}")
        print(f"  ICS 分类树 — 根节点")
        print(f"{'='*60}")
        for n in children:
            print(f"  {n['code']:12s} {n['name']:<25s} ({n['std_count']} 条标准)")
        print(f"\n  共 {len(children)} 个大类")
        print(f"  浏览子节点: tree <ICS代码>", )
        return

    # 具体节点
    node = db.tree_get_node(args.code)

    if args.standards:
        # 列出标准
        standards = db.tree_standards(args.code, limit=args.limit)
        db.close()

        if args.json:
            import json as _json
            print(_json.dumps(standards, ensure_ascii=False, indent=2))
            return

        path = db.tree_get_path(args.code) if hasattr(db, 'tree_get_path') else args.code
        print(f"{'='*60}")
        print(f"  {path}")
        print(f"  标准 ({len(standards)} 条)")
        print(f"{'='*60}")
        for i, r in enumerate(standards, 1):
            print(f"  {i:2d}. {r['standard_no']:25s} {r['title'][:50]:50s} [{r['status']}]")
        return

    children = db.tree_children(args.code)
    db.close()

    if args.json:
        import json as _json
        node_info = node or {"code": args.code, "children": children}
        if node:
            node["children"] = children
        print(_json.dumps(node_info, ensure_ascii=False, indent=2))
        return

    if not node:
        print(f"未找到 ICS 节点: {args.code}")
        return

    path = node.get("path", "")
    print(f"{'='*60}")
    print(f"  {path}")
    print(f"  ICS: {node['code']:10s} 深度: {node['level']}  标准: {node['std_count']} 条")
    if args.code != "" and node["level"] < 2:
        print(f"{'='*60}")
        print(f"  子分类")
        for n in children:
            print(f"  {n['code']:15s} {n['name']:<30s} ({n['std_count']} 条)")
        print(f"\n  浏览子节点: tree <ICS代码>")
        print(f"  列出标准:   tree <ICS代码> --standards")


def _run_enrich(args):
    """从 SAMR 详情页补充 ICS 代码"""
    from standards.crawler.platforms.samr import SamrCollector

    db = StandardDB(args.db)

    if args.limit > 0:
        rows = db._conn_execute(
            "SELECT * FROM standards WHERE (ics_code IS NULL OR ics_code = '') ORDER BY publish_date DESC LIMIT ?",
            (args.limit,)
        ).fetchall()
    else:
        rows = db._conn_execute(
            "SELECT * FROM standards WHERE (ics_code IS NULL OR ics_code = '') ORDER BY publish_date DESC"
        ).fetchall()

    if not rows:
        print("所有标准已有 ICS 代码，无需补充")
        db.close()
        return

    items = [dict(r) for r in rows]
    print(f"需要补充 ICS 的标准: {len(items)} 条")

    sampler = SamrCollector()
    enriched = sampler.enrich_ics_codes(items, max_workers=args.workers)

    updated = 0
    for item in enriched:
        ics = item.get("ics_code", "")
        if ics:
            db._conn_execute(
                "UPDATE standards SET ics_code = ? WHERE id = ?",
                (ics, item["id"])
            )
            db._link_ics(item["id"], ics)
            updated += 1

    db._connect().commit()
    db.close()

    print(f"补充完成: {updated}/{len(items)} 条获得 ICS 代码")


def _run_detail(args):
    """查看标准详情"""
    db = StandardDB(args.db)
    q = args.query.strip()

    # 先按标准号精确查找
    row = db.get_by_standard_no(q)
    if not row:
        # 再按 dedup_key 或 title 搜索
        results = db.search(q, limit=5)
        if len(results) == 1:
            row = results[0]
        elif len(results) > 1:
            print(f"找到 {len(results)} 条，请指定标准号:")
            for r in results[:10]:
                print(f"  {r['standard_no']:25s} {r['title'][:50]}")
            db.close()
            return

    if not row:
        db.close()
        print(f"未找到: {q}")
        return

    print(f"{'='*60}")
    print(f"  标准详情")
    print(f"{'='*60}")
    print(f"  标准号:      {row['standard_no']}")
    print(f"  名称:        {row['title']}")
    print(f"  状态:        {row['status']}")
    print(f"  分类:        {row['category']}")
    print(f"  发布机构:    {row['publisher']}")
    print(f"  发布日期:    {row['publish_date']}")
    print(f"  ICS 代码:    {row['ics_code'] or '(未补充)'}")
    print(f"  数据源:      {row['source']}")
    print(f"  采集时间:    {row['collected_at']}")
    if row.get('scopes'):
        print(f"  备注/范围:   {row['scopes'][:200]}")
    print(f"  详情页:      {row['url']}")

    # 检查本地文件
    local_file = ""
    if row.get('raw_data'):
        import json
        try:
            raw = json.loads(row['raw_data'])
            if raw.get('local_path'):
                local_file = raw['local_path']
                print(f"  本地文件:    {local_file}")
        except:
            pass

    # 查询 openstd 可下载状态
    print()
    print(f"  ── openstd 全文状态 ──")
    try:
        from standards.crawler.downloader import _get_local_path as _glp
        from standards.crawler.platforms.openstd import OpenStdCollector
        collector = OpenStdCollector()
        hcno = collector.find_hcno(row["standard_no"], row.get("title", ""))
        if hcno:
            print(f"  收录:        ✅ 是 (hcno={hcno[:16]}...)")
            local_f = _glp(row["standard_no"])
            if os.path.exists(local_f):
                print(f"  本地 PDF:    ✅ {local_f}")
            else:
                print(f"  可下载:      ⏳ 运行 fetch 命令")
                print(f"    python -m standards.crawler.main fetch \"{row['standard_no']}\"")
        else:
            print(f"  收录:        ❌ 未收录（可能是 2026 新标准，等待公开）")
    except Exception as e:
        print(f"  查询失败:    {e}")

    db.close()
    print()
    print(f"  ▶ 在浏览器打开详情页:")
    print(f"    {row['url']}")
    if local_file:
        print()
        print(f"  ▶ 本地 PDF:")
        print(f"    {local_file}")


def _run_note(args):
    """添加/更新标准备注"""
    db = StandardDB(args.db)
    q = args.query.strip()

    row = db.get_by_standard_no(q)
    if not row:
        # 模糊匹配
        results = db.search(q, limit=3)
        if results:
            row = results[0]
            print(f"匹配: {row['standard_no']} {row['title'][:50]}")
        else:
            print(f"未找到: {q}")
            db.close()
            return

    if args.text:
        db.update_note(row["id"], args.text)
        print(f"备注已更新: {row['standard_no']}")

    if args.path:
        db.update_local_path(row["id"], args.path)
        print(f"本地路径已记录: {args.path}")

    db.close()


def _run_fetch_playwright(args):
    """使用 Playwright 浏览器自动化下载夸克网盘 PDF"""
    import asyncio
    from standards.downloader.download import (
        batch_download, find_bzxz_ids_from_db
    )

    bzxz_map = {}
    if args.bzxz_map:
        import json
        with open(args.bzxz_map) as f:
            bzxz_map = json.load(f)
        print(f"加载 bzxz 映射: {len(bzxz_map)} 条")

    if not bzxz_map and args.domestic_only:
        print("正在扫描 bzxz.net 列表页找标准 ID...")
        found = find_bzxz_ids_from_db(domestic_only=True, limit=args.limit)
        bzxz_map.update(found)
        print(f"找到 {len(found)} 条")

    if not bzxz_map:
        print("错误: 没有找到可下载的标准")
        print("提示: 可通过 --bzxz-map 指定 ID 映射文件")
        print("或先用 --alt --scan-pages 50 --domestic-only 扫描")
        return

    print(f"即将下载 {len(bzxz_map)} 条标准...")
    loop = asyncio.new_event_loop()
    loop.run_until_complete(batch_download(bzxz_map))


def _run_fetch_alt(args):
    """从 bzxz.net 等替代来源抓取标准正文文本"""
    from datetime import datetime, timezone, timedelta
    CST = timezone(timedelta(hours=8))
    TIME_FMT = "%Y-%m-%d %H:%M:%S"
    now_str = datetime.now(CST).strftime(TIME_FMT)

    from standards.engine.storage import StandardDB as db_class

    db = db_class(args.db)

    # ── 确定要处理的标准列表 ──
    if args.query:
        # 单个标准
        row = db.get_by_standard_no(args.query)
        if not row:
            results = db.search(args.query, limit=1)
            row = results[0] if results else None
        if not row:
            print(f"未找到: {args.query}")
            db.close()
            return
        standards_list = [row]
    else:
        # 全部或部分
        if args.domestic_only:
            print("仅处理非采标 (85 条)...")
            # 从数据库查询非采标
            conn = db._conn
            cur = conn.execute(
                "SELECT id, standard_no, title, raw_data FROM standards "
                "WHERE is_adopted = 0 OR is_adopted IS NULL "
                "ORDER BY standard_no"
            )
        else:
            cur = conn = db._conn
            cur = conn.execute(
                "SELECT id, standard_no, title, raw_data FROM standards "
                "ORDER BY standard_no"
            ) if hasattr(db, '_conn') else []

        standards_list = []
        for row in cur.fetchall() if hasattr(cur, 'fetchall') else cur:
            d = dict(row)
            if d.get("raw_data"):
                try:
                    d["raw_data"] = json.loads(d["raw_data"])
                except:
                    d["raw_data"] = {}
            else:
                d["raw_data"] = {}
            standards_list.append(d)

        if args.limit > 0:
            standards_list = standards_list[:args.limit]

    db.close()

    total = len(standards_list)
    print(f"{'=' * 60}")
    print(f"  替代来源抓取 (bzxz.net)")
    print(f"  待处理: {total} 条标准")
    print(f"  列表页扫描: {args.scan_pages} 页")
    print(f"{'=' * 60}")

    # ── 第一步：批量扫描 bzxz.net 列表页 ──
    print("\n[步骤 1] 扫描 bzxz.net 列表页...")
    from standards.crawler.platforms.search_finder import search_on_bzxz_list

    target_nos = [s["standard_no"] for s in standards_list]
    scan_result = search_on_bzxz_list(target_nos, max_pages=args.scan_pages)

    found_map = scan_result["found"]
    print(f"  扫描 {scan_result['pages_scanned']} 页")
    print(f"  找到 {len(found_map)}/{total} 条")
    if scan_result['not_found']:
        print(f"  未找到 {len(scan_result['not_found'])} 条")

    if not found_map:
        print("❌ 未在 bzxz.net 上找到任何匹配标准，退出")
        return

    # ── 第二步：依次抓取正文 ──
    print("\n[步骤 2] 抓取标准正文...")
    from standards.crawler.platforms.alt_sources import fetch_from_sharing_sites

    # 构建 bzxz_id_map
    bzxz_id_map = {}
    for std_no, entry in found_map.items():
        bzxz_id_map[std_no] = entry["bzxz_id"]

    result = fetch_from_sharing_sites(
        standards_list=standards_list,
        limit=0,
        bzxz_id_map=bzxz_id_map,
    )

    # ── 汇总 ──
    print(f"\n{'=' * 60}")
    print(f"  完成 @{now_str}")
    print(f"  成功: {result['success']} / 失败: {result['failed']} / 跳过: {result.get('skipped', 0)}")
    print(f"  未找到: {result['not_found']}")
    print(f"  耗时: {result.get('duration_s', 0):.1f}s")
    print(f"{'=' * 60}")


def _run_fetch(args):
    """下载标准全文"""
    TIME_FMT = "%Y-%m-%d %H:%M:%S"
    from datetime import datetime, timezone, timedelta
    CST = timezone(timedelta(hours=8))
    now_str = datetime.now(CST).strftime(TIME_FMT)

    # ── Playwright 浏览器自动化模式 ────────────────────
    if args.playwright or args.save_auth:
        if args.save_auth:
            import subprocess, sys
            subprocess.run([sys.executable, "-m", "standards.downloader.auth", "--save"])
            return
        _run_fetch_playwright(args)
        return

    # ── 替代来源模式 (bzxz.net) ────────────────────────
    if args.alt:
        _run_fetch_alt(args)
        return

    from standards.crawler.downloader import (
        download_standard, batch_download, _get_local_path, DOWNLOAD_DIR
    )
    from standards.crawler.platforms.openstd import OpenStdCollector

    # ── 单个标准下载 ─────────────────────────────────
    if args.query:
        db = StandardDB(args.db)
        row = db.get_by_standard_no(args.query)
        if not row:
            results = db.search(args.query, limit=1)
            row = results[0] if results else None
        if not row:
            print(f"未找到: {args.query}")
            db.close()
            return

        std_no = row["standard_no"]
        title = row.get("title", "")
        print(f"正在查询: {std_no} {title[:40]}...")

        # 查 hcno
        collector = OpenStdCollector()
        hcno = collector.find_hcno(std_no, title)
        if hcno:
            avail = collector.check_availability(hcno)
            if avail.get("available"):
                print(f"  ✅ 查到 hcno, 全文可下载")
            else:
                print(f"  ⚠️ 查到 hcno, 但该标准为采标，无公开全文")
                db.close()
                return
        else:
            print(f"  ⚠️ openstd 未收录该标准")

        print(f"正在下载: {std_no}...")
        path = download_standard(std_no, row.get("url", ""), hcno=hcno or None)
        if path:
            print(f"✅ 已下载: {path}")
            db.update_local_path(row["id"], path)
        else:
            print(f"❌ 下载失败，该标准可能未公开全文")
            print(f"   详情页: {row.get('url', '')}")
        db.close()
        return

    # ── 批量下载 ───────────────────────────────────
    db = StandardDB(args.db)
    # 按发布日期升序（旧标准更容易在 openstd 上找到）
    rows = db._conn_execute(
        "SELECT id, standard_no, title, url, raw_data FROM standards ORDER BY publish_date ASC"
        + (" LIMIT ?" if args.limit > 0 else ""),
        (args.limit,) if args.limit > 0 else ()
    ).fetchall()

    items = []
    for r in rows:
        d = dict(r)
        # 从 raw_data 中提取已有 hcno（如果有的话）
        if d.get("raw_data"):
            try:
                raw = json.loads(d["raw_data"])
                if "_hcno" in raw:
                    d["_hcno"] = raw["_hcno"]
                if "_is_adopted" in raw:
                    d["_is_adopted"] = raw["_is_adopted"]
                if "_has_fulltext" in raw:
                    d["_has_fulltext"] = raw["_has_fulltext"]
            except:
                pass
        items.append(d)
    db.close()

    print(f"{'='*60}")
    print(f"  标准全文下载")
    print(f"  数据库: {len(items)} 个标准")
    print(f"  保存目录: {DOWNLOAD_DIR}")
    print(f"{'='*60}")
    print()

    success, failed = batch_download(items, max_workers=args.workers)

    print()
    print(f"{'='*60}")
    print(f"  下载完成 @{now_str}")
    print(f"  成功: {success} / 失败: {failed}")
    print(f"  保存目录: {DOWNLOAD_DIR}")
    print(f"{'='*60}")

    # 将下载路径注册到数据库
    if success > 0:
        print("\n注册本地路径到数据库...")
        db2 = StandardDB(args.db)
        registered = 0
        for it in items:
            std_no = it.get("standard_no", "")
            local_path = _get_local_path(std_no)
            if os.path.exists(local_path):
                row = db2.get_by_standard_no(std_no)
                if row:
                    db2.update_local_path(row["id"], local_path)
                    registered += 1
        db2.close()
        print(f"已注册 {registered} 条本地路径到数据库")


if __name__ == "__main__":
    run_cli()
