"""
汽车照明标准专项采集

用 SAMR 平台，以汽车照明相关关键词 + ICS 代码搜索，结果打标
automotive_lighting 并入库（与现有 355 条照明标准同库）。

用法:
    python3 scripts/collect_automotive_lighting.py

流程:
  1. 遍历汽车照明关键词搜索 SAMR
  2. 遍历相关 ICS 代码搜索 SAMR
  3. 去重合并 → 打标 sector=automotive_lighting
  4. 入库（UPSERT，已存在的自动更新 raw_data）
  5. 同步更新现有库中已匹配的汽车照明标准 sector 标识
"""

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("automotive-lightning")
CST = timezone(timedelta(hours=8))

# ── 汽车照明关键词（覆盖各子领域） ──────────────────────
KEYWORDS = [
    "前照灯", "信号灯", "车灯",
    "汽车照明", "道路车辆照明", "机动车灯",
    "摩托车灯", "摩托车照明",
    "雾灯", "转向灯", "制动灯", "倒车灯",
    "示廓灯", "牌照灯", "日间行车灯",
    "标志灯具",
    "远光", "近光",
    "回复反射", "光信号装置",
    "道路照明装置",
    "全地形车照明", "三轮汽车照明",
]

# ── 相关 ICS 代码 ──────────────────────────────────────
ICS_CODES = [
    "43.040.20",  # 照明、信号和警报设备 (核心)
    "43.040",     # 道路车辆装置
    "43.140",     # 摩托车和机动自行车
]

# ── 标题关键词（匹配则判定为 automotive_lighting） ──────
AUTO_TITLE_MARKS = [
    "汽车", "车辆", "机动车", "摩托车",
    "三轮汽车", "全地形车",
    "消防车", "警车", "救护车", "工程救险车",
    "前照灯", "信号灯", "雾灯", "转向灯",
    "制动灯", "倒车灯", "示廓灯", "牌照灯",
    "日间行车灯", "标志灯具",
    "道路车辆", "道路照明装置", "光信号装置",
    "远光", "近光", "回复反射",
    "电动自行车", "自行车照明",
]

# ── 非汽车领域排除关键词（匹配则不算汽车照明） ──────
EXCLUDE_TITLE_MARKS = [
    "飞机", "航空", "船舶", "船用", "舰船", "航海",
    "防爆", "矿灯", "矿井", "煤矿",
    "舞台", "影视", "摄影", "投影",
    "医疗", "手术", "内窥镜",
    "植物", "园艺", "养殖",
]


def is_automotive_lighting(item: dict) -> bool:
    """判断一条标准是否属于汽车照明领域"""
    title = (item.get("title") or "").upper()

    # 排除非汽车领域
    for ex in EXCLUDE_TITLE_MARKS:
        if ex.upper() in title:
            return False

    # 标题关键词匹配
    for mark in AUTO_TITLE_MARKS:
        if mark.upper() in title:
            return True

    # ICS 代码匹配
    ics = (item.get("ics_code") or "").strip()
    if ics in ("43.040.20", "43.040", "43.140"):
        return True
    if ics and ics.startswith(("43.040", "43.140")):
        return True

    return False


def set_sector_in_raw(item: dict, sector: str = "automotive_lighting") -> dict:
    """在 item 的 raw_data 中设置 sector 字段"""
    # save_item 只会存 _ 开头的 key
    # 所以用 _sector 存进去，之后再 rename
    item["_sector"] = sector
    return item


def rename_sector_in_db(db):
    """将 raw_data 中的 _sector 重命名为 sector（保持外部兼容）"""
    conn = db._connect()
    rows = conn.execute(
        "SELECT id, raw_data FROM standards WHERE raw_data LIKE '%_sector%'"
    ).fetchall()
    fixed = 0
    for row in rows:
        try:
            d = json.loads(row["raw_data"])
            if "_sector" in d:
                d["sector"] = d.pop("_sector")
                conn.execute(
                    "UPDATE standards SET raw_data = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(d, ensure_ascii=False),
                     datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"), row["id"])
                )
                fixed += 1
        except:
            pass
    conn.commit()
    return fixed


def update_existing_automotive_sectors(db):
    """遍历现有 DB，给未标记 sector 的汽车照明标准添加标识"""
    conn = db._connect()
    updated = 0
    rows = conn.execute("SELECT id, standard_no, title, raw_data FROM standards").fetchall()
    for row in rows:
        d = dict(row)
        try:
            raw = json.loads(d["raw_data"]) if d["raw_data"] else {}
        except:
            raw = {}

        # 已标记 automotive_lighting 的跳过
        if raw.get("sector") is not None:
            continue

        test_item = {"standard_no": d.get("standard_no", ""), "title": d.get("title", ""), "ics_code": ""}
        if is_automotive_lighting(test_item):
            raw["sector"] = "automotive_lighting"
            conn.execute(
                "UPDATE standards SET raw_data = ?, updated_at = ? WHERE id = ?",
                (json.dumps(raw, ensure_ascii=False),
                 datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"), d["id"])
            )
            updated += 1
    conn.commit()
    return updated


def collect():
    """主力采集函数"""
    from standards.crawler.platforms.samr import SamrCollector
    from standards.engine.storage import StandardDB

    collector = SamrCollector()
    all_items = []
    seen_keys = set()
    total_new = 0

    # 控制台直接显示进度
    print(f"\n{'='*60}")
    print(f"  BriefNexus — 汽车照明标准采集")
    print(f"  关键词: {len(KEYWORDS)} 个")
    print(f"  ICS: {len(ICS_CODES)} 个")
    print(f"  每个搜索跑 5 页 (每页 20 条)")
    print(f"{'='*60}\n")

    # ── 关键词搜索 ────────────────────────────────────
    for kw in KEYWORDS:
        for p in range(1, 6):
            try:
                items = collector.search_by_keyword(kw, page=p)
                if not items:
                    break
                for item in items:
                    dk = item.get("dedup_key", "")
                    if dk and dk not in seen_keys:
                        seen_keys.add(dk)
                        if is_automotive_lighting(item):
                            set_sector_in_raw(item)
                            all_items.append(item)
                            total_new += 1
                print(f"  [关键词] {kw:12s} 第{p}页 → {len(items):2d}条  累计汽车类: {total_new}")
                time.sleep(1)
            except Exception as e:
                print(f"  [关键词] {kw:12s} 第{p}页 ✗ {e}")
                break

    # ── ICS 代码搜索 ──────────────────────────────────
    for ics in ICS_CODES:
        for p in range(1, 6):
            try:
                items = collector.search_by_ics(ics, page=p)
                if not items:
                    break
                for item in items:
                    dk = item.get("dedup_key", "")
                    if dk and dk not in seen_keys:
                        seen_keys.add(dk)
                        if is_automotive_lighting(item):
                            set_sector_in_raw(item)
                            all_items.append(item)
                            total_new += 1
                print(f"  [ICS]     {ics:12s} 第{p}页 → {len(items):2d}条  累计汽车类: {total_new}")
                time.sleep(1)
            except Exception as e:
                print(f"  [ICS]     {ics:12s} 第{p}页 ✗ {e}")
                break

    print(f"\n{'='*60}")
    print(f"  采集完成: 找到 {total_new} 条汽车照明标准")
    print(f"{'='*60}")

    if total_new == 0:
        print("⚠️  未找到任何汽车照明标准，检查关键词或网络连接")
        return

    # ── 入库 ──────────────────────────────────────────
    print(f"\n入库中 ...")
    db = StandardDB()
    saved = db.save_items(all_items)
    print(f"入库: {saved} 条 (UPSERT，已存在的更新 raw_data)")

    # 重命名 _sector → sector
    fixed = rename_sector_in_db(db)
    if fixed:
        print(f"raw_data 字段名修复: {fixed} 条")

    # 更新现有库
    updated = update_existing_automotive_sectors(db)
    if updated:
        print(f"现有库 sector 标识更新: {updated} 条")

    # 验证
    conn = db._connect()
    try:
        auto = conn.execute(
            "SELECT COUNT(*) FROM standards WHERE raw_data LIKE '%\"sector\": \"automotive_lighting\"%'"
        ).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM standards").fetchone()[0]
        sectors_raw = conn.execute(
            "SELECT raw_data FROM standards WHERE raw_data LIKE '%\"sector\"%'"
        ).fetchall()
        sector_counts = {}
        for (r,) in sectors_raw:
            try:
                d = json.loads(r)
                s = d.get("sector", "unknown")
                sector_counts[s] = sector_counts.get(s, 0) + 1
            except:
                pass
        print(f"\n{'='*60}")
        print(f"  验证结果")
        print(f"  总标准: {total} 条")
        print(f"  汽车照明: {auto} 条")
        for s, c in sorted(sector_counts.items(), key=lambda x: -x[1]):
            print(f"    {s}: {c}")
        print(f"{'='*60}")
    except Exception as e:
        print(f"验证异常: {e}")
    db.close()

    # 打印摘要
    print(f"\n新采集的汽车照明标准 ({len(all_items)} 条):")
    for item in sorted(all_items, key=lambda x: x.get("standard_no", "")):
        print(f"  {item.get('standard_no', ''):25s} {item.get('title', '')[:60]}")


if __name__ == "__main__":
    collect()
