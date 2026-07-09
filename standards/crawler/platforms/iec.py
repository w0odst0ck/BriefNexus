"""
IEC Webstore 元数据采集器 — 从 webstore.iec.ch 抓取 IEC 标准元数据

注意: IEC Webstore 已全面迁移到 Magento + Hyvä Themes (JS 渲染)，
      简单 HTTP 请求无法抓取元数据（返回 404）。
      autocomplete API 可查询标准号是否存在。

接口: IECCollector
"""

import logging
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from standards.crawler.utils import logger as root_logger, new_session

logger = logging.getLogger("standards.iec")

IEC_BASE = "https://webstore.iec.ch"
AUTOCOMPLETE_API = "https://webstore-search-api.iec.ch/api/publications/autocomplete"

CST = timezone(timedelta(hours=8))
TIME_FMT = "%Y-%m-%d %H:%M:%S"


class IECCollector:
    """IEC Webstore 元数据采集器

    由于 Webstore 使用 Magento + JS 渲染，本模块仅能通过
    autocomplete API 验证标准号是否存在，元数据需手动补充。
    """

    def __init__(self, delay: float = 2.0):
        self.session = new_session()
        self.session.headers.update({
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
        })
        self.delay = delay

    def search_by_iec_no(self, iec_no: str) -> Optional[dict]:
        """通过 autocomplete API 验证 IEC 标准号存在性"""
        query = self._normalize_iec_no(iec_no)
        logger.info("搜索 IEC: %s", query)

        confirmed = self._check_autocomplete(query)
        if confirmed:
            return {
                "iec_no": query,
                "confirmed": True,
                "source": "iec-autocomplete",
            }

        # 重试纯数字部分
        nums = re.findall(r"\d+", query)
        for num in nums[:2]:
            if len(num) >= 4:
                logger.info("重试数字部分: %s", num)
                confirmed = self._check_autocomplete(num)
                if confirmed:
                    return {
                        "iec_no": query,
                        "confirmed": True,
                        "source": "iec-autocomplete",
                    }
        return None

    def batch_search(self, iec_numbers: List[str],
                     max_workers: int = 2) -> Dict[str, Optional[dict]]:
        """批量验证 IEC 标准号"""
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

    def _check_autocomplete(self, query: str) -> bool:
        """通过 autocomplete API 验证标准号"""
        try:
            time.sleep(1.0)
            r = self.session.get(AUTOCOMPLETE_API,
                                  params={"query": query},
                                  timeout=15)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list) and len(data) > 0:
                    logger.info("autocomplete 确认存在: %s → %s",
                                query, data[0].get("value", ""))
                    return True
            return False
        except Exception as e:
            logger.warning("autocomplete 请求失败: %s — %s", query, e)
            return False

    @staticmethod
    def _normalize_iec_no(raw: str) -> str:
        no = raw.strip().upper()
        if not no.startswith("IEC"):
            no = "IEC " + no
        return no


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    collector = IECCollector(delay=1.0)
    for iec_no in ["IEC 62722-2-1", "IEC 60598-1", "IEC 62612"]:
        meta = collector.search_by_iec_no(iec_no)
        print(f"{iec_no}: {'✅' if meta else '❌'} {meta}")
