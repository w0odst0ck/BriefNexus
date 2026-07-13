"""
中国标准在线服务网 (www.spc.org.cn)

国家标准全文公开平台，可检索 GB/GB/T 标准全文。
提供搜索接口和标准详情页。
"""

import json
import logging
import re
from typing import List, Optional
from urllib.parse import urlencode, urljoin

from ..utils import (
    safe_get, safe_get_json, new_session,
    normalize_standard_no, normalize_date, gen_dedup_key,
    make_standard_item, extract_ics_code
)
from .base import BaseStandardCollector

logger = logging.getLogger("standards.spc")

# 搜索 URL
SEARCH_URL = "https://www.spc.org.cn/search/"
# 搜索结果 API
SEARCH_API = "https://www.spc.org.cn/api/search/standard"


class SpcCollector(BaseStandardCollector):
    source_name = "spc"
    display_name = "中国标准在线服务网"

    def _build_search_url(self, keyword: str = "", ics: str = "",
                          page: int = 1, size: int = 20) -> str:
        """构造搜索 URL"""
        params = {
            "keyword": keyword,
            "ics": ics,
            "page": page,
            "pageSize": size,
        }
        return SEARCH_API + "?" + urlencode(params, encoding="utf-8")

    def search_by_keyword(self, keyword: str, page: int = 1) -> List[dict]:
        return self._search(keyword=keyword, page=page)

    def search_by_ics(self, ics_code: str, page: int = 1) -> List[dict]:
        return self._search(ics=ics_code, page=page)

    def _search(self, keyword: str = "", ics: str = "",
                page: int = 1, size: int = 20) -> List[dict]:
        """执行搜索"""
        url = self._build_search_url(keyword=keyword, ics=ics, page=page, size=size)

        # 先尝试 JSON API
        headers = {
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://www.spc.org.cn/",
        }
        for hk, hv in headers.items():
            self.session.headers[hk] = hv

        data = safe_get_json(url, self.session)
        if data:
            items = self._parse_api_response(data)
            if items:
                return items

        # 回退到 HTML 解析
        html = safe_get(SEARCH_URL + "?" + urlencode(
            {"keyword": keyword, "page": page},
            encoding="utf-8"
        ), self.session)
        if html:
            return self.parse_page(html)

        return []

    def _parse_api_response(self, data: dict) -> List[dict]:
        """解析 SPC JSON API 返回"""
        results = []

        records = data
        # 尝试常见包裹结构
        if isinstance(data, dict):
            records = data.get("data", data)
        if isinstance(records, dict):
            records = records.get("list", records.get("records", records.get("items", [])))
        if isinstance(records, dict):
            records = records.get("result", records.get("data", []))

        if not isinstance(records, list):
            return results

        for raw in records:
            if not isinstance(raw, dict):
                continue
            item = self._map_raw_item(raw)
            if item:
                results.append(item)

        return results

    def _map_raw_item(self, raw: dict) -> Optional[dict]:
        """映射 SPC 原始字段"""
        title = (raw.get("standardName") or raw.get("name") or
                 raw.get("title") or raw.get("stdName") or "")
        if not title:
            return None

        standard_no = (raw.get("standardNo") or raw.get("stdNo") or
                       raw.get("code") or "")
        publisher = (raw.get("publishDept") or raw.get("department") or
                     raw.get("publisher") or "")
        publish_date = (raw.get("publishDate") or raw.get("pubDate") or
                        raw.get("issueDate") or "")
        status = (raw.get("status") or raw.get("standardStatus") or "")
        ics_code = raw.get("icsCode") or raw.get("ics") or ""
        scopes = raw.get("scope") or raw.get("scopes") or ""
        item_id = raw.get("id") or raw.get("standardId") or ""

        detail_url = f"https://www.spc.org.cn/standard/{item_id}" if item_id else ""

        item = make_standard_item(
            title=title,
            standard_no=standard_no,
            publisher=publisher,
            publish_date=publish_date,
            status=status,
            ics_code=ics_code,
            scopes=scopes,
            source=self.source_name,
            url=detail_url,
        )
        item["_raw_id"] = item_id
        return item

    def parse_page(self, html: str) -> List[dict]:
        """从 HTML 页面解析标准列表"""
        results = []
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")

        # 常见列表结构
        items = soup.select(".standard-item, .search-result-item, .std-item, table tr")
        for el in items:
            tds = el.find_all("td")
            if not tds:
                # 尝试 div 结构
                title_el = el.select_one("a.standard-name, a.title, h3 a")
                if not title_el:
                    continue
            else:
                title_el = tds[0].select_one("a")

            if not title_el:
                continue

            title = title_el.get_text(strip=True)
            href = title_el.get("href", "")
            detail_url = href if href.startswith("http") else urljoin(SEARCH_URL, href)

            # 尝试解析标准号
            no_el = el.select_one(".standard-no, .std-no, .code")
            standard_no = no_el.get_text(strip=True) if no_el else ""

            publisher = (tds[1].get_text(strip=True) if len(tds) > 1 else
                         el.select_one(".publisher").get_text(strip=True) if el.select_one(".publisher") else "")
            pub_date = (tds[2].get_text(strip=True) if len(tds) > 2 else
                        el.select_one(".pub-date").get_text(strip=True) if el.select_one(".pub-date") else "")

            item = make_standard_item(
                title=title,
                standard_no=standard_no,
                publisher=publisher,
                publish_date=pub_date,
                source=self.source_name,
                url=detail_url,
            )
            results.append(item)

        return results
