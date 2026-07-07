"""
标准平台适配器基类
"""

import logging
from abc import ABC, abstractmethod
from typing import List, Optional

from ..utils import logger, new_session


class BaseStandardCollector(ABC):
    """标准数据采集器基类"""

    # 平台名称（唯一标识）
    source_name: str = ""
    # 平台显示名
    display_name: str = ""

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.session = new_session()
        self.logger = logging.getLogger(f"standards.{self.source_name}")

    @abstractmethod
    def search_by_keyword(self, keyword: str, page: int = 1) -> List[dict]:
        """按关键词搜索标准"""
        ...

    @abstractmethod
    def search_by_ics(self, ics_code: str, page: int = 1) -> List[dict]:
        """按 ICS 分类代码搜索"""
        ...

    def collect(self, keywords: list, ics_codes: list,
                max_pages: int = 5) -> List[dict]:
        """
        统一采集入口：遍历关键词和ICS代码，去重后返回
        """
        all_items = []
        seen_keys = set()

        for kw in keywords:
            self.logger.info("搜索关键词: %s", kw)
            for p in range(1, max_pages + 1):
                items = self.search_by_keyword(kw, page=p)
                if not items:
                    break
                for item in items:
                    dk = item.get("dedup_key", "")
                    if dk and dk not in seen_keys:
                        seen_keys.add(dk)
                        all_items.append(item)
                self.logger.info("  第%d页 → %d条", p, len(items))

        for ics in ics_codes:
            self.logger.info("搜索ICS: %s", ics)
            for p in range(1, max_pages + 1):
                items = self.search_by_ics(ics, page=p)
                if not items:
                    break
                for item in items:
                    dk = item.get("dedup_key", "")
                    if dk and dk not in seen_keys:
                        seen_keys.add(dk)
                        all_items.append(item)
                self.logger.info("  第%d页 → %d条", p, len(items))

        self.logger.info("采集完成，共 %d 条(去重后)", len(all_items))
        return all_items

    def parse_page(self, html: str) -> List[dict]:
        """解析列表页 HTML 为标准条目（子类可选覆盖）"""
        raise NotImplementedError

    def parse_detail(self, url: str) -> Optional[dict]:
        """解析详情页（子类可选覆盖）"""
        return None
