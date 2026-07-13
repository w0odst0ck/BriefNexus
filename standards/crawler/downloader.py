#!/usr/bin/env python3
"""
批量下载标准全文（PDF）

开放平台: openstd.samr.gov.cn (国家标准全文公开系统)
采标标准（adopted）无公开全文，非采标标准可通过 3 步流程下载。

用法:
  python -m standards.crawler.main fetch                      # 批量下载全部
  python -m standards.crawler.main fetch --limit 10           # 先下10条
  python -m standards.crawler.main fetch "GB/T 29639"         # 下载指定标准
  python -m standards.crawler.main fetch --find-hcno          # 先查找 hcno 再下载
"""

import json
import logging
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from .utils import logger, new_session

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DOWNLOAD_DIR = PROJECT_ROOT / "standards" / "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


def _get_local_path(standard_no: str) -> str:
    safe_name = re.sub(r'[\\/:*?"<>|]', "_", standard_no)
    return str(DOWNLOAD_DIR / f"{safe_name}.pdf")


# ── 旧式 URL 猜测下载（保留兼容，但已基本失效） ──────

def _candidates_old(standard_no: str, url: str = "") -> list:
    """旧的 URL 猜测候选（openstd 已改版，基本不可用）"""
    safe_no = standard_no.replace("/", "_").replace(" ", "")
    candidates = []

    for prefix in ["", "common/", "std/"]:
        candidates.append(
            f"https://openstd.samr.gov.cn/bzgk/gb/{prefix}{safe_no}.pdf"
        )

    if url and "gbDetailed" in url:
        view_url = url.replace("gbDetailed", "gbView")
        candidates.append(view_url)

    return candidates


def download_standard_legacy(standard_no: str, url: str = "",
                             session=None) -> Optional[str]:
    """旧式下载（猜测 URL），留作兜底"""
    if session is None:
        session = new_session()

    local_path = _get_local_path(standard_no)
    if os.path.exists(local_path):
        return local_path

    for dl_url in _candidates_old(standard_no, url):
        try:
            r = session.get(dl_url, timeout=15, allow_redirects=True)
            if r.status_code == 200 and "application/pdf" in r.headers.get("Content-Type", ""):
                with open(local_path, "wb") as f:
                    f.write(r.content)
                logger.info("已下载(旧): %s → %s (%d KB)", standard_no, local_path,
                            len(r.content) // 1024)
                return local_path
        except Exception:
            continue

    return None


# ── 新版 openstd 下载 ────────────────────────────────

def find_hcno(standard_no: str, title: str = "") -> str:
    """通过 openstd 平台查找标准 hcno（搜索引擎方式）

    使用独立会话避免与下载流冲突。

    Returns:
        hcno 字符串，未找到返回空字符串
    """
    try:
        from .platforms.openstd import OpenStdCollector
        collector = OpenStdCollector()
        return collector.find_hcno(standard_no, title) or ""
    except Exception as e:
        logger.warning("查找 hcno 失败 %s: %s", standard_no, e)
        return ""


def download_standard(standard_no: str, url: str = "", session=None,
                      hcno: str = None) -> Optional[str]:
    """
    下载标准全文 PDF

    流程:
      1. 检查本地是否已有
      2. 如果有 hcno → 用 openstd viewGb 下载
      3. 无 hcno → 尝试旧式 URL 猜测

    Args:
        standard_no: 标准号 (如 GB/T 39394-2020)
        url: 来源URL（用于旧式猜测）
        session: requests Session
        hcno: openstd 平台 hcno（若已知）

    Returns:
        本地文件路径，失败返回 None
    """
    local_path = _get_local_path(standard_no)
    if os.path.exists(local_path):
        return local_path

    # 优先用 hcno 下载
    hcno = hcno or ""
    if hcno:
        try:
            from .platforms.openstd import download_pdf as _download_openstd
            result = _download_openstd(
                hcno, standard_no,
                session=session or new_session(),
                download_dir=str(DOWNLOAD_DIR)
            )
            if result:
                return result
        except Exception as e:
            logger.warning("openstd 下载失败 %s: %s", standard_no, e)

    # 兜底：旧式猜测
    if session is None:
        session = new_session()
    return download_standard_legacy(standard_no, url, session)


def batch_download_with_hcno(items: list, max_workers: int = 3,
                              limit: int = 0) -> tuple:
    """带 hcno 查询的批量下载（先查 hcno → 再下载）

    先筛选出没有本地文件的标准 → 只查未缓存 hcno → 下载

    Args:
        items: [{standard_no, url, title, _hcno, ...}]
               如果已有 _hcno 字段（来自 raw_data），跳过查找直接下载
        max_workers: 并发数
        limit: 下载上限（0=全部）

    Returns:
        (成功数, 失败数)
    """
    if limit > 0:
        items = items[:limit]

    # 筛选本地没有的
    pending = [it for it in items
               if not os.path.exists(_get_local_path(it.get("standard_no", "")))]
    if not pending:
        logger.info("所有标准已有本地文件，无需下载")
        return (0, 0)

    logger.info("待下载: %d 个(已跳过 %d 个已有文件)",
                len(pending), len(items) - len(pending))

    # 区分已缓存 hcno 和需查询的
    need_search = [it for it in pending if not it.get("_hcno")]
    have_cache = [it for it in pending if it.get("_hcno")]

    # 查 hcno（只查未缓存的）
    from .platforms.openstd import OpenStdCollector
    enriched = []
    if need_search:
        collector = OpenStdCollector()
        enriched = collector.find_hcno_batch(need_search, max_workers=max_workers)

    # 合并
    all_enriched = have_cache + enriched

    # 所有有 hcno 的尝试下载
    downloadable = [it for it in all_enriched if it.get("_hcno")]
    skipped_no_hcno = [it for it in all_enriched if not it.get("_hcno")]

    logger.info("有 hcno(尝试下载): %d / 未找到 hcno: %d (已缓存 %d)",
                len(downloadable), len(skipped_no_hcno), len(have_cache))

    # 下载
    from .platforms.openstd import batch_download as _batch_openstd
    success, failed = _batch_openstd(downloadable, max_workers=max_workers)

    # 保存 hcno 查询结果
    if enriched:
        _save_hcno_results(all_enriched)

    return (success, failed)


def _save_hcno_results(items: list):
    """将 hcno 查询结果保存到 JSON 供后续参考"""
    output_path = DOWNLOAD_DIR / "_hcno_results.json"
    try:
        data = []
        for it in items:
            data.append({
                "standard_no": it.get("standard_no", ""),
                "title": it.get("title", ""),
                "_hcno": it.get("_hcno", ""),
                "_is_adopted": it.get("_is_adopted", False),
                "_has_fulltext": it.get("_has_fulltext", False),
                "local_exists": os.path.exists(
                    _get_local_path(it.get("standard_no", ""))
                ),
            })
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info("hcno 结果已保存: %s", output_path)
    except Exception as e:
        logger.warning("保存 hcno 结果失败: %s", e)


def batch_download(items: list, max_workers: int = 3, limit: int = 0) -> tuple:
    """批量下载（兼容旧接口，自动使用新流程）

    这是主入口函数。
    """
    return batch_download_with_hcno(items, max_workers=max_workers, limit=limit)
