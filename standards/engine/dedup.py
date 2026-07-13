"""
去重、合并、标准化引擎
"""

import hashlib
import logging
from typing import List, Dict

from ..crawler.utils import gen_dedup_key, logger


def deduplicate(items: List[dict], key_field: str = "dedup_key") -> List[dict]:
    """
    基础去重：相同 dedup_key 只保留第一条

    Returns:
        去重后的列表
    """
    seen = set()
    result = []
    for item in items:
        key = item.get(key_field, "")
        if not key:
            key = gen_dedup_key(item)
            item[key_field] = key
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def merge_sources(source_results: Dict[str, List[dict]]) -> List[dict]:
    """
    合并多源采集结果，去重后返回

    Args:
        source_results: { source_name: [items] }

    Returns:
        合并去重后的列表
    """
    all_items = []
    for source, items in source_results.items():
        for item in items:
            item["source"] = source
        all_items.extend(items)

    merged = deduplicate(all_items)
    logger.info("多源合并: %d 源 → %d 条(原始) → %d 条(去重后)",
                len(source_results),
                sum(len(v) for v in source_results.values()),
                len(merged))
    return merged


def filter_by_keywords(items: List[dict], keywords: List[str],
                       fields: List[str] = None) -> List[dict]:
    """
    关键词过滤：只保留标题/标准号包含指定关键词的条目

    Args:
        items: 标准条目列表
        keywords: 关键词列表
        fields: 检索字段（默认 title + standard_no）

    Returns:
        过滤后的列表
    """
    if not keywords:
        return items

    if fields is None:
        fields = ["title", "standard_no", "scopes"]

    keywords_lower = [kw.lower() for kw in keywords]

    result = []
    for item in items:
        matched = False
        for field in fields:
            val = item.get(field, "").lower()
            for kw in keywords_lower:
                if kw in val:
                    matched = True
                    break
            if matched:
                break
        if matched:
            result.append(item)

    if len(result) < len(items):
        logger.info("关键词过滤: %d → %d", len(items), len(result))
    return result


def classify_items(items: List[dict]) -> Dict[str, List[dict]]:
    """
    按标准类别分组

    Returns:
        { "国标": [...], "行标": [...], "团标": [...], "地标": [...], "其他": [...] }
    """
    groups = {}
    for item in items:
        cat = item.get("category", "其他")
        groups.setdefault(cat, []).append(item)

    # 按类别统计排序
    order = ["国标", "国标(指导)", "行标", "团标", "地标", "其他"]
    ordered = {}
    for cat in order:
        if cat in groups:
            ordered[cat] = groups[cat]
    for cat, val in groups.items():
        if cat not in ordered:
            ordered[cat] = val

    return ordered


def add_standard_meta(items: List[dict]) -> List[dict]:
    """
    补充元数据：分类、标准化字段等
    """
    from ..crawler.utils import classify_standard_no

    for item in items:
        if not item.get("category"):
            item["category"] = classify_standard_no(item.get("standard_no", ""))
        if not item.get("dedup_key"):
            item["dedup_key"] = gen_dedup_key(item)
    return items
