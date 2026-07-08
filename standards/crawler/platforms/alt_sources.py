"""
替代来源下载模块 — 从标准分享站抓取标准全文

主要功能:
  - 遍历标准列表，在 bzxz.net (标准分享站) 上查找并下载标准正文
  - 使用搜索引擎定位标准页面 (需要 agent 提供 web_search 结果)
  - 提取 HTML 中的标准正文并保存为文本文件
  - 更新数据库中的 raw_data 字段，记录 alt_source 信息

使用方式:
  from standards.crawler.platforms.alt_sources import fetch_from_sharing_sites

  results = fetch_from_sharing_sites(standards_list)

与 search_finder.py 的关系:
  - search_finder: 提供查找 + 抓取 + 提取的底层能力
  - alt_sources: 提供协调、数据库更新、批量处理的顶层编排
"""

import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Tuple

# ── Make sure project root is on path ──
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from standards.crawler.utils import (
    logger as root_logger,
    normalize_standard_no,
)

logger = logging.getLogger("standards.alt_sources")

CST = timezone(timedelta(hours=8))

# ── 延迟导入 (避免循环依赖) ──
BZXZ_SEARCH_FINDER = None


def _get_search_finder():
    """延迟加载 search_finder 模块"""
    global BZXZ_SEARCH_FINDER
    if BZXZ_SEARCH_FINDER is None:
        from standards.crawler.platforms.search_finder import (
            search_on_bzxz as _search_on_bzxz,
            save_standard_text as _save_text,
            extract_standard_content as _extract,
            BZXZ_BASE,
        )
        BZXZ_SEARCH_FINDER = {
            "search_on_bzxz": _search_on_bzxz,
            "save_standard_text": _save_text,
            "extract_standard_content": _extract,
            "BZXZ_BASE": BZXZ_BASE,
        }
    return BZXZ_SEARCH_FINDER


# ── 数据库操作 ──────────────────────────────────────────

def _get_db_path() -> str:
    """获取数据库路径"""
    base = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    return os.path.join(base, "standards.db")


def _fetch_standards_from_db(limit: int = 0) -> List[dict]:
    """从数据库读取标准列表

    Args:
        limit: 返回数量上限 (0=全部)

    Returns:
        [{"id": int, "standard_no": str, "title": str, "raw_data": dict}]
    """
    import sqlite3

    db_path = _get_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    if limit > 0:
        c.execute(
            "SELECT id, standard_no, title, raw_data FROM standards ORDER BY id LIMIT ?",
            (limit,)
        )
    else:
        c.execute(
            "SELECT id, standard_no, title, raw_data FROM standards ORDER BY id"
        )

    rows = []
    for r in c.fetchall():
        item = {
            "id": r["id"],
            "standard_no": r["standard_no"],
            "title": r["title"],
            "raw_data": json.loads(r["raw_data"]) if r["raw_data"] else {},
        }
        rows.append(item)

    conn.close()
    return rows


def _update_db_alt_source(std_id: int, alt_info: dict):
    """更新数据库中的 alt_source 信息

    将 alt_info 合并到 raw_data 字段的 alt_source 键中。

    Args:
        std_id: 标准在数据库中的 id
        alt_info: {
            "url": str,            # bzxz 页面 URL
            "local_path": str,     # 保存的本地文件路径
            "status": str,         # "ok" | "not_found" | "fetch_failed" | "empty"
            "is_truncated": bool,  # 是否仅部分内容
            "content_length": int, # 正文长度
            "error": str,          # 错误信息 (仅失败时)
            "timestamp": str,      # 抓取时间
        }
    """
    import sqlite3

    db_path = _get_db_path()
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    # 读取当前 raw_data
    c.execute("SELECT raw_data FROM standards WHERE id = ?", (std_id,))
    row = c.fetchone()
    if row is None:
        logger.warning("数据库中无此记录: id=%d", std_id)
        conn.close()
        return

    raw_data = json.loads(row[0]) if row[0] else {}
    if not isinstance(raw_data, dict):
        raw_data = {"_raw": str(raw_data)}

    # 写入 alt_source 信息
    raw_data["alt_source"] = alt_info

    # 如果保存了本地路径，同步到 local_path
    if alt_info.get("local_path"):
        raw_data["local_path"] = alt_info["local_path"]

    c.execute(
        "UPDATE standards SET raw_data = ?, updated_at = ? WHERE id = ?",
        (
            json.dumps(raw_data, ensure_ascii=False),
            datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
            std_id,
        ),
    )
    conn.commit()
    conn.close()
    logger.debug("已更新 alt_source: id=%d, status=%s", std_id, alt_info.get("status"))


# ── 单条标准处理 ────────────────────────────────────────

def process_single_standard(std_item: dict,
                            search_results: dict = None,
                            bzxz_id_map: dict = None) -> dict:
    """处理单条标准: 查找 → 抓取 → 提取 → 保存 → 更新数据库

    Args:
        std_item: {"id": int, "standard_no": str, "title": str, "raw_data": dict}
        search_results: 可选的预搜索结构化结果，格式:
            {"standard_no": [web_search 结果条目]}
        bzxz_id_map: 已知的 bzxz_id 映射 {standard_no: bzxz_id}

    Returns:
        {
            "id": int,
            "standard_no": str,
            "success": bool,
            "alt_source": {...}  # 数据库更新的 alt_info
        }
    """
    std_id = std_item["id"]
    std_no = normalize_standard_no(std_item["standard_no"])
    title = std_item.get("title", "")
    raw_data = std_item.get("raw_data", {})

    # ── 检查是否已有 alt_source ──
    if raw_data and raw_data.get("alt_source") and raw_data["alt_source"].get("status") == "ok":
        logger.info("跳过 (已有 alt_source): %s", std_no)
        return {
            "id": std_id,
            "standard_no": std_no,
            "success": True,
            "alt_source": raw_data["alt_source"],
            "skipped": True,
        }

    sf = _get_search_finder()
    search_on_bzxz = sf["search_on_bzxz"]
    save_standard_text = sf["save_standard_text"]
    extract_standard_content = sf["extract_standard_content"]

    # ── 获取此标准的已知 bzxz_id ──
    known_id = None
    if bzxz_id_map and std_no in bzxz_id_map:
        known_id = bzxz_id_map[std_no]
        logger.info("已知 bzxz_id: %s → %d", std_no, known_id)

    # ── 获取此标准的搜索结果 ──
    std_search_results = None
    if search_results and std_no in search_results:
        std_search_results = search_results[std_no]

    # ── 备用：如果 search_results 是扁平列表而非字典，尝试通用匹配 ──
    if std_search_results is None and isinstance(search_results, list):
        std_search_results = [
            r for r in search_results
            if std_no.replace(" ", "") in (r.get("title", "") + r.get("description", ""))
        ]

    # ── 在 bzxz.net 上搜索（优先使用已知 ID）──
    result = search_on_bzxz(std_no, title,
                            search_results=std_search_results,
                            bzxz_id=known_id)

    if result is None:
        alt_info = {
            "url": "",
            "local_path": "",
            "status": "not_found",
            "is_truncated": False,
            "content_length": 0,
            "error": "在 bzxz.net 上未找到该标准",
            "timestamp": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S+08:00"),
        }
        _update_db_alt_source(std_id, alt_info)
        logger.info("未找到: %s", std_no)
        return {
            "id": std_id,
            "standard_no": std_no,
            "success": False,
            "alt_source": alt_info,
        }

    # ── 保存文本 ──
    content = result["page_content"]
    local_path = save_standard_text(std_no, content)
    result["local_path"] = local_path

    # ── 构建 alt_info ──
    alt_info = {
        "url": result["url"],
        "local_path": local_path,
        "status": "ok",
        "is_truncated": content.get("is_truncated", False),
        "content_length": len(content.get("content_full", "")),
        "content_intro_length": len(content.get("content_intro", "")),
        "error": "",
        "timestamp": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S+08:00"),
    }

    # 更新数据库
    _update_db_alt_source(std_id, alt_info)

    success = bool(local_path and os.path.exists(local_path))
    logger.info(
        "%s: %s (内容 %d 字, 截断=%s)",
        "✅" if success else "⚠️",
        std_no,
        alt_info["content_length"],
        alt_info["is_truncated"],
    )

    return {
        "id": std_id,
        "standard_no": std_no,
        "success": success,
        "alt_source": alt_info,
    }


# ── 批量处理 ──────────────────────────────────────────────

def fetch_from_sharing_sites(standards_list: List[dict] = None,
                              max_search_workers: int = 3,
                              max_fetch_workers: int = 3,
                              limit: int = 0,
                              search_results: dict = None,
                              bzxz_id_map: dict = None) -> dict:
    """主入口：从分享站抓取标准全文

    处理流程:
      1. 读取标准列表 (从数据库或传入)
      2. 对每条标准，通过搜索引擎查找 bzxz.net 页面
      3. 抓取页面 HTML
      4. 提取标准正文
      5. 保存为文本文件
      6. 更新数据库

    Args:
        standards_list: 可选的标准列表 [{standard_no, title, ...}]
            如果为 None，从数据库读取
        max_search_workers: 搜索并发数 (当前保留参数，串行执行避免限流)
        max_fetch_workers: 抓取并发数 (当前保留参数，串行执行避免限流)
        limit: 处理标准数上限 (0=全部)
        search_results: 预搜索的结构化结果，格式:
            {"GB/T XXXX-XXXX": [web_search results], ...}
            由 agent 调用 web_search tool 后提供
        bzxz_id_map: 已知的 bzxz_id 映射 {standard_no: bzxz_id}
            可通过 search_on_bzxz_list() 预先批量扫描获得

    Returns:
        {
            "total": int,
            "success": int,
            "failed": int,
            "skipped": int,
            "not_found": int,
            "results": [...],       # 每条标准的处理结果
            "timestamp": str,
        }
    """
    start_time = time.time()

    # ── 读取标准列表 ──
    if standards_list is None:
        standards_list = _fetch_standards_from_db(limit=limit)
    elif limit > 0:
        standards_list = standards_list[:limit]

    total = len(standards_list)
    logger.info("=" * 60)
    logger.info("开始从分享站抓取标准全文 (共 %d 条)", total)
    logger.info("+" * 60)

    if total == 0:
        logger.warning("标准列表为空，跳过")
        return {
            "total": 0,
            "success": 0,
            "failed": 0,
            "skipped": 0,
            "not_found": 0,
            "results": [],
            "duration_s": 0,
            "timestamp": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S+08:00"),
        }

    # ── 串行处理 (避免搜索引擎限流) ──
    # 每次只搜索 1 个标准，间隔 2 秒
    results = []
    success_count = 0
    failed_count = 0
    skipped_count = 0
    not_found_count = 0

    for idx, std in enumerate(standards_list, 1):
        std_id = std.get("id", 0)
        std_no = std.get("standard_no", "")
        title = std.get("title", "")

        logger.info("[%d/%d] 处理: %s — %s", idx, total, std_no, title[:40])

        # 查找此标准的预搜索结果
        std_search = None
        if search_results:
            std_search = search_results.get(normalize_standard_no(std_no))

        # 处理
        r = process_single_standard(
            std_item={"id": std_id, "standard_no": std_no, "title": title,
                       "raw_data": std.get("raw_data", {})},
            search_results=search_results,  # 传递整个字典用于内部匹配
            bzxz_id_map=bzxz_id_map,
        )

        results.append(r)

        if r.get("skipped"):
            skipped_count += 1
        elif r["success"]:
            success_count += 1
        elif r["alt_source"]["status"] == "not_found":
            not_found_count += 1
            failed_count += 1
        else:
            failed_count += 1

        # 避免过快搜索 (流控)
        if idx < total:
            delay = 2.0 + (idx % 3)
            time.sleep(delay)

    # ── 汇总 ──
    duration = time.time() - start_time
    summary = {
        "total": total,
        "success": success_count,
        "failed": failed_count,
        "skipped": skipped_count,
        "not_found": not_found_count,
        "results": results,
        "duration_s": round(duration, 1),
        "timestamp": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S+08:00"),
    }

    logger.info("=" * 60)
    logger.info("抓取完成!")
    logger.info("  总数: %d", total)
    logger.info("  成功: %d", success_count)
    logger.info("  失败: %d", failed_count)
    logger.info("  跳过: %d", skipped_count)
    logger.info("  未找到: %d", not_found_count)
    logger.info("  耗时: %.1f 秒", duration)
    logger.info("=" * 60)

    return summary


# ── 搜索辅助: agent 调用 web_search 的包装 ──────────────

def build_search_queries(standards_list: List[dict]) -> List[dict]:
    """为每条标准构建搜索查询

    Returns:
        [{"standard_no": str, "query": str, "title": str}, ...]
    """
    queries = []
    for std in standards_list:
        no = normalize_standard_no(std.get("standard_no", ""))
        title = std.get("title", "")
        query = f"site:bzxz.net {no}"
        queries.append({
            "standard_no": no,
            "query": query,
            "title": title[:60],
        })
    return queries


def parse_web_search_results(standard_no: str,
                               web_search_return: dict) -> List[dict]:
    """将 web_search tool 的返回转换为 search_finder 需要的格式

    web_search 返回格式:
        {
            "results": [
                {"title": "...", "url": "...", "description": "...", "siteName": "..."},
                ...
            ],
            ...
        }

    Returns:
        [{"title": str, "url": str, "description": str, "siteName": str}]
    """
    if not web_search_return:
        return []

    results = web_search_return.get("results", [])
    if not results:
        return []

    parsed = []
    for r in results:
        parsed.append({
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "description": r.get("description", ""),
            "siteName": r.get("siteName", ""),
        })

    return parsed


# ── 测试入口 ────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    import argparse

    parser = argparse.ArgumentParser(description="从分享站抓取标准全文")
    parser.add_argument("--limit", type=int, default=5,
                        help="处理标准数量上限")
    parser.add_argument("--db-only", action="store_true",
                        help="仅从数据库读取 (不需要传入 standards_list)")
    args = parser.parse_args()

    if args.db_only:
        stds = _fetch_standards_from_db(limit=args.limit)
        result = fetch_from_sharing_sites(
            standards_list=stds,
            limit=args.limit,
        )
    else:
        # 使用手工传入的标准
        test_stds = [
            {"standard_no": "GB/T 39394-2020", "title": "LED灯、LED灯具和LED模块的测试方法"},
            {"standard_no": "GB/T 44473-2024", "title": "植物照明用LED灯、LED灯具和LED模块"},
        ]
        result = fetch_from_sharing_sites(
            standards_list=test_stds,
            limit=args.limit,
        )

    print(f"\n结果摘要: 成功={result['success']}, 失败={result['failed']}, "
          f"跳过={result['skipped']}, 耗时={result['duration_s']}s")
