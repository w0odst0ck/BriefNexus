"""
IEC Webstore 元数据采集器 — 从 webstore.iec.ch 抓取 IEC 标准元数据

功能:
  - 根据 IEC 标准号查询元数据
  - 提取标题、范围、发布日期、版本、ICS 代码、TC 信息
  - 检查标准状态

接口: IECCollector (继承 BaseStandardCollector)

URL 模式:
  - 详情页: https://webstore.iec.ch/en/publication/{publication_id}
  - 搜索:    https://webstore.iec.ch/en/search?q={query}

注意:
  - IEC 标准全文需付费购买，本模块仅采集公开元数据
  - 部分页面可能被 Cloudflare 保护，需 requests 模拟浏览器
  - 存在请求频率限制，建议 delay >= 3s
"""

import logging
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

# ── 项目路径 ──
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from standards.crawler.utils import (
    logger as root_logger,
    new_session,
    safe_get,
    normalize_standard_no,
)

logger = logging.getLogger("standards.iec")

IEC_BASE = "https://webstore.iec.ch"
SEARCH_URL = f"{IEC_BASE}/en/search"
PUBLICATION_URL = f"{IEC_BASE}/en/publication"

CST = timezone(timedelta(hours=8))
TIME_FMT = "%Y-%m-%d %H:%M:%S"


class IECCollector:
    """IEC Webstore 元数据采集器"""

    def __init__(self, delay: float = 3.0):
        self.session = new_session()
        self.session.headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
        })
        self.delay = delay

    # ── 公开接口 ────────────────────────────────────

    def search_by_iec_no(self, iec_no: str) -> Optional[dict]:
        """按 IEC 标准号搜索元数据

        Args:
            iec_no: IEC 标准号，如 "IEC 62722-2-1"

        Returns:
            {title, scope, publication_date, edition, ics_codes, tc, status, url}
            未找到返回 None
        """
        query = self._normalize_iec_no(iec_no)
        logger.info("搜索 IEC: %s", query)

        # 策略 1: 尝试直接访问 publication 页面
        pub_id = self._search_publication_id(query)
        if pub_id:
            return self._fetch_publication_metadata(pub_id)

        # 策略 2: 搜索页面
        return self._search_via_webstore(query)

    def batch_search(self, iec_numbers: List[str],
                     max_workers: int = 2) -> Dict[str, Optional[dict]]:
        """批量搜索 IEC 标准元数据

        Args:
            iec_numbers: IEC 标准号列表
            max_workers: 并发数

        Returns:
            {iec_no: metadata_dict_or_None, ...}
        """
        results = {}
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _search(iec_no):
            return iec_no, self.search_by_iec_no(iec_no)

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_search, no): no for no in iec_numbers}
            for future in as_completed(futures):
                iec_no, meta = future.result()
                results[iec_no] = meta
                time.sleep(self.delay)

        return results

    # ── 内部方法 ─────────────────────────────────────

    def _search_publication_id(self, query: str) -> Optional[str]:
        """尝试用 /en/publication/{query} 路径直接访问

        Returns:
            publication_id (数字)，失败返回 None
        """
        # 去掉前缀和版本号
        clean = re.sub(r"^(IEC|ISO)\s*", "", query, flags=re.IGNORECASE).strip()
        # IEC 62722-2-1 → 尝试多个变体
        variants = [clean]
        # 去掉子部分: 62722-2-1 → 62722
        main_part = re.match(r"(\d+)-", clean)
        if main_part:
            variants.append(main_part.group(1))

        for v in variants:
            url = f"{PUBLICATION_URL}/{v}"
            try:
                time.sleep(1.0)
                r = self.session.get(url, timeout=20, allow_redirects=True)
                if r.status_code == 200:
                    # 检查页面是否确实包含对应的 IEC 标准
                    soup = BeautifulSoup(r.text, "html.parser")
                    title_el = soup.find("h1") or soup.find("title")
                    if title_el:
                        title_text = title_el.get_text(strip=True)
                        if self._is_relevant_page(title_text, clean):
                            # 从 URL 提取 publication_id
                            pub_match = re.search(r"/publication/(\d+)", r.url)
                            if pub_match:
                                logger.info("直接访问成功: %s → publication/%s",
                                            url, pub_match.group(1))
                                return pub_match.group(1)
            except Exception as e:
                logger.debug("直接访问 %s 失败: %s", url, e)

        return None

    def _is_relevant_page(self, title_text: str, query: str) -> bool:
        """判断页面标题是否与查询相关"""
        # IEC 页面标题格式: "IEC XXXXXX:YYYY | IEC"
        iec_match = re.search(r"(IEC\s+\d+)", title_text, re.IGNORECASE)
        if not iec_match:
            return False
        # 检查数字部分是否匹配
        query_nums = re.findall(r"\d+", query)
        title_nums = re.findall(r"\d+", iec_match.group(1))
        return any(qn in title_nums for qn in query_nums if len(qn) >= 3)

    def _search_via_webstore(self, query: str) -> Optional[dict]:
        """通过 Webstore 搜索页面查找"""
        params = {"q": query}
        try:
            time.sleep(1.0)
            r = self.session.get(SEARCH_URL, params=params, timeout=20)
            if r.status_code != 200:
                logger.warning("搜索页面返回 %d", r.status_code)
                return None

            soup = BeautifulSoup(r.text, "html.parser")

            # 查找搜索结果链接
            for link in soup.find_all("a", href=re.compile(r"/en/publication/\d+")):
                href = link.get("href", "")
                pub_match = re.search(r"/publication/(\d+)", href)
                if pub_match:
                    logger.info("搜索结果找到 publication/%s", pub_match.group(1))
                    return self._fetch_publication_metadata(pub_match.group(1))

            logger.info("搜索页面未找到匹配: %s", query)
            return None

        except Exception as e:
            logger.warning("搜索页面请求失败: %s — %s", query, e)
            return None

    def _fetch_publication_metadata(self, pub_id: str) -> Optional[dict]:
        """抓取公开页面的标准元数据

        Returns:
            {
                "iec_no": str,          # IEC 标准号
                "title": str,           # 英文标题
                "scope": str,           # 范围描述
                "publication_date": str,# 发布日期 YYYY-MM-DD
                "edition": str,         # 版本号 (如 "2.0")
                "ics_codes": [str],     # ICS 代码列表
                "tc": str,             # 技术委员会
                "status": str,          # 状态
                "pages": int,
                "url": str,
                "source": str,          # "iec"
            }
        """
        url = f"{PUBLICATION_URL}/{pub_id}"
        logger.info("获取元数据: %s", url)

        try:
            time.sleep(self.delay)
            r = self.session.get(url, timeout=20, allow_redirects=True)
            if r.status_code != 200:
                logger.warning("页面返回 %d: %s", r.status_code, url)
                return None

            return self._parse_publication_page(r.text, url)

        except Exception as e:
            logger.warning("获取元数据失败: %s — %s", url, e)
            return None

    def _parse_publication_page(self, html: str, url: str) -> dict:
        """解析 IEC 标准详情页"""
        soup = BeautifulSoup(html, "html.parser")
        page_text = soup.get_text("\n", strip=True)

        # ── IEC 标准号 ──
        iec_no = ""
        title_el = soup.find("h1")
        if title_el:
            text = title_el.get_text(strip=True)
            iec_match = re.match(r"(IEC\s+[\d\-.A-Z]+)", text, re.IGNORECASE)
            if iec_match:
                iec_no = iec_match.group(1).strip()

        # ── 标题 (去除 "IEC XXXXXX:YYYY" 前缀) ──
        title = ""
        if title_el:
            full = title_el.get_text(strip=True)
            title = re.sub(r"^IEC\s+[\d\-.A-Z]+:\d{4}\s*", "", full, flags=re.IGNORECASE).strip()

        # ── 范围/摘要 ──
        scope = ""
        # 查找 <meta name="description">
        meta_desc = soup.find("meta", attrs={"name": "description"})
        if meta_desc and meta_desc.get("content"):
            scope = meta_desc["content"].strip()

        # ── 发布日期 ──
        pub_date = ""
        date_patterns = [
            r"Publication\s*date\s*(\d{4}-\d{2}-\d{2})",
            r"Publication\s*date\s*(\d{4})",
        ]
        for pat in date_patterns:
            m = re.search(pat, page_text, re.IGNORECASE)
            if m:
                pub_date = m.group(1)
                break

        # ── 版本号 ──
        edition = ""
        ed_match = re.search(r"Edition\s*([\d.]+)", page_text, re.IGNORECASE)
        if ed_match:
            edition = ed_match.group(1)

        # ── ICS 代码 ──
        ics_codes = []
        ics_match = re.search(r"ICS\s*([\d.]+)", page_text)
        if ics_match:
            ics_codes.append(ics_match.group(1))

        # ── 技术委员会 ──
        tc = ""
        tc_match = re.search(r"Technical\s*committee\s*(TC\s+\d+)", page_text, re.IGNORECASE)
        if tc_match:
            tc = tc_match.group(1)

        # ── 状态 ──
        status = ""
        # 检查是否 Base publication / Amendment / etc
        status_match = re.search(r"Publication\s*type\s*(.+)", page_text, re.IGNORECASE)
        if status_match:
            status = status_match.group(1).strip()

        # ── 页数 ──
        pages = 0
        pages_match = re.search(r"Pages\s*(\d+)", page_text)
        if pages_match:
            pages = int(pages_match.group(1))

        metadata = {
            "iec_no": iec_no,
            "title": title,
            "scope": scope,
            "publication_date": pub_date,
            "edition": edition,
            "ics_codes": ics_codes,
            "tc": tc,
            "status": status,
            "pages": pages,
            "url": url,
            "source": "iec",
        }

        logger.info("解析完成: %s | %s", iec_no or "(unknown)", title[:60] if title else "")
        return metadata

    @staticmethod
    def _normalize_iec_no(raw: str) -> str:
        """标准化 IEC 标准号"""
        no = raw.strip().upper()
        if not no.startswith("IEC"):
            no = "IEC " + no
        return no


# ── 测试入口 ────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

    collector = IECCollector(delay=2.0)

    # 测试几个已知的照明 IEC 标准
    test_cases = [
        "IEC 62722-2-1",
        "IEC 60598-1",
        "IEC 62612",
    ]

    for iec_no in test_cases:
        print(f"\n{'='*60}")
        print(f"搜索: {iec_no}")
        meta = collector.search_by_iec_no(iec_no)
        if meta:
            print(f"  标题: {meta.get('title', 'N/A')}")
            print(f"  范围: {meta.get('scope', 'N/A')[:120]}")
            print(f"  发布日期: {meta.get('publication_date', 'N/A')}")
            print(f"  版本: {meta.get('edition', 'N/A')}")
            print(f"  ICS: {meta.get('ics_codes', [])}")
            print(f"  TC: {meta.get('tc', 'N/A')}")
            print(f"  URL: {meta.get('url', 'N/A')}")
        else:
            print(f"  ❌ 未找到")
