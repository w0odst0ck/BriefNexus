"""
DB 完整性迁移脚本

1. 新增 local_path 列
2. scopes ← raw_data.sector（406 条已有 sector 标签）
3. 补标 24 条缺失 sector 的照明标准
4. 108 PDF 路径匹配写入 local_path
"""

import json
import logging
import os
import glob
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from standards.engine.storage import StandardDB

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
logger = logging.getLogger("migrate")

PDF_DIR = os.path.join(PROJECT_ROOT, "standards", "downloads")

# ── 24 条缺失 sector 的标准 → 人工打标规则 ──────────────
# 按标题关键词判定领域
MISSING_TAG_RULES = [
    # (关键词, sector)
    (r"植物照明|植物.*LED", "plant_lighting"),
    (r"应急照明|消防救生", "emergency_lighting"),
    (r"显微镜.*照明", "microscope_lighting"),
    (r"显微镜.*颜色.*照明", "microscope_lighting"),
    (r"室内LED显示屏", "display"),
    (r"装饰照明", "decorative_lighting"),
    (r"舞台LED", "stage_lighting"),
    (r"工作场所照明|室外作业场所", "workplace_lighting"),
    (r"LED灯、LED灯具.*测试", "led_testing"),
    (r"LED照明应用.*接口", "led_interface"),
    (r"数字可寻址照明接口", "digital_lighting_interface"),
    (r"LD用稀土荧光片|LED外延芯片", "led_material"),
    (r"LED灯罩|光扩散", "led_material"),
    (r"LED行业.*氨气", "led_manufacturing"),
    (r"LED加速寿命", "led_testing"),
    (r"LED灯串|LED灯丝灯|LED筒灯|LED投光灯具|嵌入式LED|LED.*性能", "led_lighting"),
    (r"植物照明术语", "plant_lighting"),
]


def tag_missing_sector(title: str) -> str:
    """对 24 条缺失 sector 的记录按规则打标"""
    for pattern, sector in MISSING_TAG_RULES:
        if re.search(pattern, title):
            return sector
    return "lighting"  # 兜底


def pdf_filename_to_standard_no(filename: str) -> str:
    """PDF 文件名 → 标准号

    规则:
      GB_17945-2024.pdf  →  GB 17945-2024
      GB_T_38539-2020.pdf → GB/T 38539-2020
      GB_Z_23153-2008.pdf → GB/Z 23153-2008
      GB_7000.203-2013.pdf → GB 7000.203-2013
    """
    name = filename.replace(".pdf", "")
    # GB_T_xxx → GB/T_xxx → then replace remaining _ with space
    name = re.sub(r"^GB_(T|Z|B)", r"GB/\1", name)
    name = name.replace("_", " ")
    return name.strip()


def migrate():
    db = StandardDB()
    conn = db._conn

    print("=" * 60)
    print("  BriefNexus — DB 完整性迁移")
    print("=" * 60)

    # ── 第 1 步：新增 local_path 列 ────────────────────
    print("\n[1/4] 新增 local_path 列...")
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(standards)")]
    if "local_path" not in cols:
        conn.execute("ALTER TABLE standards ADD COLUMN local_path TEXT DEFAULT ''")
        print("  → local_path 列已新增")
    else:
        print("  → local_path 列已存在，跳过")

    # ── 第 2 步：scopes ← raw_data.sector ───────────────
    print("\n[2/4] 填充 scopes 字段（从 raw_data.sector）...")
    rows = conn.execute(
        "SELECT id, raw_data FROM standards WHERE raw_data LIKE '%\"sector\"%'"
    ).fetchall()
    filled = 0
    for r in rows:
        try:
            d = json.loads(r["raw_data"])
            sector = d.get("sector", "").strip()
            if sector:
                conn.execute(
                    "UPDATE standards SET scopes = ?, updated_at = datetime('now','localtime') WHERE id = ?",
                    (sector, r["id"])
                )
                filled += 1
        except (json.JSONDecodeError, TypeError):
            pass
    conn.commit()
    print(f"  → {filled} 条已写入 scopes")

    # ── 第 3 步：补标 24 条缺失 sector ─────────────────
    print("\n[3/4] 补标缺失 sector 的记录...")
    rows = conn.execute(
        "SELECT id, title, raw_data FROM standards WHERE raw_data NOT LIKE '%\"sector\"%'"
    ).fetchall()
    tagged = 0
    for r in rows:
        sector = tag_missing_sector(r["title"])
        # 更新 raw_data
        rd = r["raw_data"]
        try:
            d = json.loads(rd) if rd else {}
        except:
            d = {}
        d["sector"] = sector
        conn.execute(
            "UPDATE standards SET raw_data = ?, scopes = ?, updated_at = datetime('now','localtime') WHERE id = ?",
            (json.dumps(d, ensure_ascii=False), sector, r["id"])
        )
        tagged += 1
        print(f"  {r['title'][:60]:65s} → {sector}")
    conn.commit()
    print(f"  → {tagged} 条已打标")

    # ── 第 4 步：PDF 路径匹配 ──────────────────────────
    print("\n[4/4] 匹配 PDF 文件路径...")
    pdf_paths = sorted(glob.glob(os.path.join(PDF_DIR, "*.pdf")))
    matched = 0
    unmatched = []
    for pdf_path in pdf_paths:
        filename = os.path.basename(pdf_path)
        standard_no = pdf_filename_to_standard_no(filename)
        r = conn.execute(
            "SELECT id FROM standards WHERE standard_no = ?", (standard_no,)
        ).fetchone()
        if r:
            conn.execute(
                "UPDATE standards SET local_path = ?, updated_at = datetime('now','localtime') WHERE id = ?",
                (pdf_path, r["id"])
            )
            matched += 1
        else:
            unmatched.append((filename, standard_no))
    conn.commit()
    print(f"  → {matched}/{len(pdf_paths)} PDF 已匹配")

    if unmatched:
        print(f"\n  无法匹配的 PDF ({len(unmatched)} 个):")
        for fname, sno in unmatched[:10]:
            print(f"    {fname}  → 尝试匹配标准号: {sno}")

    # ── 验证 ──────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  验证结果")
    print("=" * 60)
    stats = conn.execute("SELECT COUNT(*) as total, "
                         "SUM(CASE WHEN scopes != '' THEN 1 ELSE 0 END) as with_scopes, "
                         "SUM(CASE WHEN local_path != '' THEN 1 ELSE 0 END) as with_path "
                         "FROM standards").fetchone()
    print(f"  总标准:              {stats['total']}")
    print(f"  有 scopes:           {stats['with_scopes']}")
    print(f"  有 PDF 路径:         {stats['with_path']}")

    sectors = conn.execute(
        "SELECT scopes, COUNT(*) FROM standards WHERE scopes != '' GROUP BY scopes ORDER BY COUNT(*) DESC"
    ).fetchall()
    for s, c in sectors:
        print(f"    {s}: {c}")

    db.close()
    print("\n✅ 迁移完成")


if __name__ == "__main__":
    migrate()
