"""
工标网 (www1.csres.com)

可按 ICS 分类代码遍历标准，适合按 29.140（照明）批量拉取。
搜索接口较为传统，主要靠表单提交。
"""

import json
import logging
import re
from typing import List, Optional
from urllib.parse import urlencode, urljoin, parse_qs

from ..utils import (
    safe_get, new_session, make_standard_item
)
from .base import BaseStandardCollector

logger = logging.getLogger("standards.csres")

BASE_URL = "https://www1.csres.com"
SEARCH_URL = "https://www1.csres.com/search/standard/"
LIST_BY_ICS_URL = "https://www1.csres.com/ics/{}/"


class CsresCollector(BaseStandardCollector):
    source_name = "csres"
    display_name = "工标网"

    def search_by_keyword(self, keyword: str, page: int = 1) -> List[dict]:
        return self._search(keyword=keyword, page=page)

    def search_by_ics(self, ics_code: str, page: int = 1) -> List[dict]:
        return self._browse_ics(ics_code, page=page)

    def _search(self, keyword: str = "", page: int = 1) -> List[dict]:
        """搜索关键词"""
        params = {"keyword": keyword, "page": page}
        url = SEARCH_URL + "?" + urlencode(params, encoding="utf-8")
        html = safe_get(url, self.session)
        if html:
            return self.parse_page(html)
        return []

    def _browse_ics(self, ics_code: str, page: int = 1) -> List[dict]:
        """按 ICS 分类浏览"""
        url = LIST_BY_ICS_URL.format(ics_code)
        if page > 1:
            url = url + f"?page={page}"

        html = safe_get(url, self.session)
        if html:
            return self.parse_page(html)
        return []

    def parse_page(self, html: str) -> List[dict]:
        """解析工标网列表页"""
        results = []
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")

        # 工标网常见表格结构
        rows = soup.select("table.list-table tr, .result-list tr, .standard-grid tr")
        if not rows:
            rows = soup.select("tr[class*='standard'], .item-row")

        for row in rows:
            cols = row.find_all("td")
            if len(cols) < 3:
                continue

            # 标准号通常在第一列
            a_tag = cols[0].find("a") if cols else None
            if not a_tag:
                continue

            standard_no = a_tag.get_text(strip=True)
            href = a_tag.get("href", "")
            detail_url = href if href.startswith("http") else urljoin(BASE_URL, href)

            # 标题通常在第二列
            title = ""
            if len(cols) > 1:
                title_el = cols[1].select_one("a")
                if title_el:
                    title = title_el.get_text(strip=True)
                else:
                    title = cols[1].get_text(strip=True)

            # 发布机构
            publisher = cols[2].get_text(strip=True) if len(cols) > 2 else ""

            # 日期和状态
            pub_date = cols[3].get_text(strip=True) if len(cols) > 3 else ""
            status = cols[4].get_text(strip=True) if len(cols) > 4 else ""

            if not title and standard_no:
                title = standard_no

            item = make_standard_item(
                title=title,
                standard_no=standard_no,
                publisher=publisher,
                publish_date=pub_date,
                status=status,
                source=self.source_name,
                url=detail_url,
            )
            results.append(item)

        return results
