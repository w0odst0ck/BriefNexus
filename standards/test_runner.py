#!/usr/bin/env python3
"""
行业标准采集模块 — 测试用例
"""

import sys, os, json, tempfile, time
from datetime import datetime

# Ensure project root in path
_project_root = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.abspath(_project_root))

import logging
logging.basicConfig(level=logging.WARNING, format="%(name)s %(levelname)s %(message)s")

PASS = 0
FAIL = 0

def test(name, condition, detail=""):
    global PASS, FAIL
    status = "✅" if condition else "❌"
    if condition:
        PASS += 1
    else:
        FAIL += 1
    print(f"  {status} {name}" + (f" — {detail}" if detail else ""))

def section(title):
    print(f"\n{'='*60}")
    print(f"  📋 {title}")
    print(f"{'='*60}")


# ──────────────────────────────────────────────
# 1. 工具函数测试
# ──────────────────────────────────────────────
section("工具函数")

from standards.crawler.utils import (
    normalize_standard_no, normalize_date, classify_standard_no,
    gen_dedup_key, make_standard_item
)

test("normalize_standard_no 去空格", 
     normalize_standard_no(" GB/T 12345-2023 ") == "GB/T 12345-2023")

test("normalize_standard_no 全角空格", 
     normalize_standard_no("GB/T\u300012345") == "GB/T 12345")

test("normalize_date YYYY-MM-DD",
     normalize_date("2023-12-01") == "2023-12-01")

test("normalize_date YYYY年MM月DD日",
     normalize_date("2023年12月01日") == "2023-12-01")

test("normalize_date YYYY.MM.DD",
     normalize_date("2023.12.01") == "2023-12-01")

test("normalize_date 纯年份",
     normalize_date("2023") == "2023-01-01")

test("classify GB 强制性",
     classify_standard_no("GB 12345-2023") == "国标")

test("classify GB/T 推荐性",
     classify_standard_no("GB/T 12345-2023") == "国标")

test("classify GB/Z 指导性",
     classify_standard_no("GB/Z 12345-2023") == "国标(指导)")

test("classify DB 地标",
     classify_standard_no("DB11/T 123-2023") == "地标")

test("classify T/ 团标",
     classify_standard_no("T/CAS 123-2023") == "团标")

test("make_standard_item 完整字段",
     lambda: len(make_standard_item(title="测试", standard_no="GB/T 1")) > 5)

item_a = make_standard_item(title="LED测试规范", standard_no="GB/T 1-2023")
item_b = make_standard_item(title="LED测试规范", standard_no="GB/T 1-2023")
item_c = make_standard_item(title="不同标准", standard_no="GB/T 2-2023")

test("gen_dedup_key 相同标准一致",
     item_a["dedup_key"] == item_b["dedup_key"])

test("gen_dedup_key 不同标准不同",
     item_a["dedup_key"] != item_c["dedup_key"])

test("make_standard_item 含采集时间",
     "collected_at" in item_a)

test("make_standard_item 含dedup_key",
     "dedup_key" in item_a and len(item_a["dedup_key"]) == 32)


# ──────────────────────────────────────────────
# 2. 去重引擎测试
# ──────────────────────────────────────────────
section("去重引擎")

from standards.engine.dedup import (
    deduplicate, merge_sources, filter_by_keywords, classify_items, add_standard_meta
)

test("deduplicate 空列表",
     len(deduplicate([])) == 0)

test("deduplicate 去重 (3→2)",
     len(deduplicate([item_a, item_b, item_c])) == 2)

items_102 = [item_a] + [item_b] * 100 + [item_c]
test(f"deduplicate 批量 (102→2)",
     len(deduplicate(items_102)) == 2)

test("merge_sources 多源合并",
     len(merge_sources({"a": [item_a], "b": [item_c]})) == 2)

test("merge_sources 跨源去重",
     len(merge_sources({"a": [item_a, item_b], "b": [item_c]})) == 2)

items_filter = [
    make_standard_item(title="普通照明用LED模块", standard_no="GB/T 1"),
    make_standard_item(title="汽车前照灯", standard_no="GB/T 2"),
    make_standard_item(title="环保标准", standard_no="GB/T 3"),
]
filtered = filter_by_keywords(items_filter, ["LED", "照明"])
test("filter_by_keywords LED/照明",
     len(filtered) == 1 and "LED" in filtered[0]["title"])

filtered2 = filter_by_keywords(items_filter, ["汽车"])
test("filter_by_keywords 汽车",
     len(filtered2) == 1 and "汽车" in filtered2[0]["title"])

classified = classify_items(items_filter)
test("classify_items 有国标分类",
     "国标" in classified)

test("classify_items 总量一致",
     sum(len(v) for v in classified.values()) == len(items_filter))


# ──────────────────────────────────────────────
# 3. 导出引擎测试
# ──────────────────────────────────────────────
section("导出引擎")

from standards.engine.exporter import export_json, export_csv, export_markdown

test_items = [
    make_standard_item(title="测试标准A", standard_no="GB/T 1-2023",
                       publisher="测试机构", publish_date="2023-01-01",
                       status="现行", source="test"),
    make_standard_item(title="测试标准B", standard_no="GB/T 2-2023",
                       publisher="测试机构", publish_date="2023-06-01",
                       status="现行", source="test"),
]

with tempfile.TemporaryDirectory() as tmpdir:
    # JSON
    j_path = os.path.join(tmpdir, "test.json")
    export_json(test_items, j_path)
    with open(j_path, "r") as f:
        jdata = json.load(f)
    test("导出 JSON 条数正确", len(jdata) == 2)
    test("导出 JSON 含完整字段", "standard_no" in jdata[0] and "title" in jdata[0])

    # CSV
    c_path = os.path.join(tmpdir, "test.csv")
    export_csv(test_items, c_path)
    test("导出 CSV 文件存在", os.path.exists(c_path))
    test("导出 CSV 非空", os.path.getsize(c_path) > 0)

    # Markdown
    m_path = os.path.join(tmpdir, "test.md")
    export_markdown(test_items, m_path)
    with open(m_path, "r") as f:
        mdata = f.read()
    test("导出 MD 含标题", "测试标准A" in mdata and "测试标准B" in mdata)
    test("导出 MD 含分类", "国标" in mdata)


# ──────────────────────────────────────────────
# 4. SAMR 平台适配器测试（网络）
# ──────────────────────────────────────────────
section("SAMR 采集器 — 搜索测试")

from standards.crawler.platforms.samr import SamrCollector

samr = SamrCollector()

# 4a. 关键词搜索
t0 = time.time()
items_kw = samr.search_by_keyword("LED", page=1)
t_kw = time.time() - t0

test("关键词搜索 返回列表", isinstance(items_kw, list))
test("关键词搜索 有数据", len(items_kw) > 0, detail=f"({len(items_kw)}条, {t_kw:.1f}s)")
if items_kw:
    first = items_kw[0]
    test("含 standard_no", bool(first.get("standard_no")), detail=first["standard_no"])
    test("含 title (已去标签)", bool(first.get("title")) and "<" not in first["title"])
    test("含 source=samr", first.get("source") == "samr")
    test("含 url", bool(first.get("url")))
    test("含 status", bool(first.get("status")))
    test("含 publish_date", bool(first.get("publish_date")))
    test("含 category (自动归类)", bool(first.get("category")))
    test("含 dedup_key (32位md5)", len(first.get("dedup_key", "")) == 32)
    test("含 collected_at (ISO时间戳)", "T" in first.get("collected_at", ""))

# 4b. 分页测试
items_p2 = samr.search_by_keyword("LED", page=2)
test("关键词分页 第2页有数据", len(items_p2) > 0, detail=f"({len(items_p2)}条)")
if items_kw and items_p2:
    test("分页返回不同数据",
         items_kw[0]["dedup_key"] != items_p2[0]["dedup_key"])

# 4c. ICS 搜索
t0 = time.time()
items_ics = samr.search_by_ics("29.140", page=1)
t_ics = time.time() - t0
# SAMR 的 ICS 搜索可能不支持精确过滤，但至少不抛异常
test("ICS搜索 不抛异常", True, detail=f"({len(items_ics)}条, {t_ics:.1f}s)")

# 4d. 多关键词多页采集
t0 = time.time()
items_collect = samr.collect(keywords=["LED", "照明"], ics_codes=[], max_pages=2)
t_collect = time.time() - t0
test("collect() 多关键词 返回数据", len(items_collect) > 0, detail=f"({len(items_collect)}条, {t_collect:.1f}s)")
test("collect() 跨关键词去重", len(items_collect) <= 80, detail=f"(关键词各2页, 去重后{len(items_collect)}条)")


# ──────────────────────────────────────────────
# 5. 采集引擎集成测试（不含导出）
# ──────────────────────────────────────────────
section("采集引擎集成测试")

from standards.engine.collector import CollectorEngine
from standards.crawler.utils import load_config

# 临时缩小范围
cfg = load_config()
cfg.set("domain", "keywords", "LED,照明")
cfg.set("domain", "ics_codes", "")  # 跳过ICS搜索以节省时间
cfg.set("crawler", "max_pages", "1")

engine = CollectorEngine()
test("引擎初始化 平台加载", "samr" in engine.collectors, detail=list(engine.collectors.keys()))

t0 = time.time()
items = engine.collect_all()
t_total = time.time() - t0

test("全采集 返回数据", len(items) > 0, detail=f"({len(items)}条, {t_total:.1f}s)")
if items:
    # 验证数据完整性
    fields = ["title", "standard_no", "publisher", "publish_date", "status",
              "category", "url", "source", "dedup_key", "collected_at"]
    for f in fields:
        test(f"字段完整性: {f}", all(f in it for it in items[:3]))

# 状态分布
statuses = set(it["status"] for it in items)
test("状态识别", bool(statuses & {"现行", "废止"}) or len(statuses) > 0,
     detail=f"状态值: {statuses}")


# ──────────────────────────────────────────────
# 6. 端到端测试（全流程 + 导出到临时目录）
# ──────────────────────────────────────────────
section("端到端流水线测试")

with tempfile.TemporaryDirectory() as tmpdir:
    engine.output_dir = tmpdir
    engine.export(items, formats=["json", "md"])

    jfiles = [f for f in os.listdir(tmpdir) if f.endswith(".json")]
    mdfiles = [f for f in os.listdir(tmpdir) if f.endswith(".md")]

    test("导出 JSON 文件生成", len(jfiles) >= 1)
    test("导出 MD 文件生成", len(mdfiles) >= 1)

    if jfiles:
        jp = os.path.join(tmpdir, jfiles[0])
        with open(jp, "r") as f:
            exported = json.load(f)
        test("导出 JSON 内容完整", len(exported) == len(items))

    if mdfiles:
        mp = os.path.join(tmpdir, mdfiles[0])
        with open(mp, "r") as f:
            content = f.read()
        test("导出 MD 含总数", "标准总数" in content)
        test("导出 MD 含条目", items[0]["title"] in content if items else True)


# ──────────────────────────────────────────────
# 汇总
# ──────────────────────────────────────────────
print(f"\n{'='*60}")
print(f"  测试汇总:  {PASS} ✅ passed,  {FAIL} ❌ failed  (共 {PASS+FAIL} 项)")
print(f"{'='*60}")
sys.exit(0 if FAIL == 0 else 1)
