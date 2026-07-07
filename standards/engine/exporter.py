"""
数据导出器 — JSON / Excel
"""

import csv
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import List

from ..crawler.utils import load_config, pretty_json, logger

CST = timezone(timedelta(hours=8))


def export_json(items: List[dict], output_path: str):
    """导出为 JSON 文件"""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(pretty_json(items))
    logger.info("导出 JSON: %s (%d 条)", output_path, len(items))


def export_csv(items: List[dict], output_path: str,
               fieldnames: List[str] = None):
    """导出为 CSV 文件"""
    if not items:
        logger.warning("无数据，跳过 CSV 导出")
        return

    if fieldnames is None:
        # 自动推断字段
        fieldnames = list(items[0].keys())
        # 去掉内部字段
        fieldnames = [f for f in fieldnames if not f.startswith("_")]

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for item in items:
            row = {k: item.get(k, "") for k in fieldnames}
            writer.writerow(row)
    logger.info("导出 CSV: %s (%d 条)", output_path, len(items))


def export_markdown(items: List[dict], output_path: str,
                    domain_name: str = "行业标准"):
    """导出为 Markdown 报告（方便人工浏览）"""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    lines = [
        f"# {domain_name} — 标准采集报告",
        "",
        f"**采集时间:** {datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')}",
        f"**标准总数:** {len(items)}",
        "",
        "---",
        "",
    ]

    # 按分类分组
    by_category = {}
    for item in items:
        cat = item.get("category", "其他")
        by_category.setdefault(cat, []).append(item)

    for cat, cat_items in by_category.items():
        lines.append(f"## {cat} ({len(cat_items)} 条)")
        lines.append("")
        for it in cat_items:
            no = it.get("standard_no", "")
            title = it.get("title", "")
            status = it.get("status", "")
            pub_date = it.get("publish_date", "")
            publisher = it.get("publisher", "")
            url = it.get("url", "")
            source = it.get("source", "")
            status_tag = f" [{status}]" if status else ""
            lines.append(f"- **{no}** {title}{status_tag}")
            lines.append(f"  - {publisher} | {pub_date} | {source}")
            if url:
                lines.append(f"  - {url}")
            lines.append("")

    content = "\n".join(lines)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)
    logger.info("导出 MD: %s (%d 条)", output_path, len(items))


def auto_export(items: List[dict], output_dir: str,
                domain_name: str = "行业标准", formats: List[str] = None):
    """根据配置自动导出"""
    cfg = load_config()
    if formats is None:
        fmt = cfg.get("output", "format", fallback="json")
        formats = [fmt]

    date_str = datetime.now(CST).strftime("%Y%m%d")
    os.makedirs(output_dir, exist_ok=True)

    for fmt in formats:
        fmt = fmt.strip().lower()
        if fmt == "json":
            path = os.path.join(output_dir, f"standards_{date_str}.json")
            export_json(items, path)
        elif fmt == "csv":
            path = os.path.join(output_dir, f"standards_{date_str}.csv")
            export_csv(items, path)
        elif fmt == "md":
            path = os.path.join(output_dir, f"standards_{date_str}.md")
            export_markdown(items, path, domain_name)

    # 合并模式：同时输出一个最新索引
    latest_path = os.path.join(output_dir, "_latest.json")
    export_json(items, latest_path)
