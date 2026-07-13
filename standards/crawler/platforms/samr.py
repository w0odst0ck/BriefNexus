"""
全国标准信息公共服务平台 (std.samr.gov.cn)

国家标准的官方查询平台，提供 GB（强制性）、GB/T（推荐性）、GB/Z（指导性）
等国家标准的检索与详情查看。

搜索API (Bootstrap Table):
  GET /gb/search/gbQueryPage?searchText=KEYWORD&ics=ICS&state=STATE&ISSUE_DATE=&pageNumber=N&pageSize=S
  返回 JSON: { total, pageNumber, rows: [...] }

详情页:
  GET /gb/search/gbDetailed?id=UUID
"""

import json
import logging
import re
from typing import List, Optional
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup

from ..utils import (
    safe_get, safe_get_json, new_session,
    normalize_standard_no, normalize_date, make_standard_item,
    logger as root_logger
)
from .base import BaseStandardCollector

logger = logging.getLogger("standards.samr")

# ── API 端点（已验证） ─────────────────────────────────────
SEARCH_API = "https://std.samr.gov.cn/gb/search/gbQueryPage"
DETAIL_URL = "https://std.samr.gov.cn/gb/search/gbDetailed?id={}"

# ICS 分类代码 → 名称（常用）
ICS_MAP = {
    "29.140": "照明",
    "29.140.01": "照明综合",
    "29.140.10": "灯头和灯座",
    "29.140.20": "白炽灯",
    "29.140.30": "荧光灯、放电灯",
    "29.140.40": "灯具",
    "29.140.50": "照明安装系统",
    "29.140.99": "照明其他标准",
    "91.140": "建筑物中的安装",
    "91.140.01": "建筑物安装综合",
    "91.140.50": "供电系统",
    "91.140.99": "建筑物安装其他",
    "13.020": "环境保护",
    "13.020.01": "环境和环境保护综合",
    "13.020.10": "环境管理",
    "13.020.20": "环境经济",
    "13.020.99": "环境保护其他",
}


class SamrCollector(BaseStandardCollector):
    source_name = "samr"
    display_name = "全国标准信息公共服务平台"

    def search_by_keyword(self, keyword: str, page: int = 1) -> List[dict]:
        return self._query_api(search_text=keyword, page=page)

    def search_by_ics(self, ics_code: str, page: int = 1) -> List[dict]:
        """按 ICS 代码搜索

        注意：SAMR 的 ics 参数传完整分类号（如 29.140.40）
        """
        return self._query_api(ics=ics_code, page=page)

    def _query_api(self, search_text: str = "", ics: str = "",
                   state: str = "", issue_date: str = "",
                   page: int = 1, page_size: int = 20) -> List[dict]:
        """直接调用 SAMR Bootstrap Table JSON API"""
        params = {
            "searchText": search_text,
            "ics": ics,
            "state": state,
            "ISSUE_DATE": issue_date,
            "pageNumber": page,
            "pageSize": page_size,
        }

        headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://std.samr.gov.cn/gb/gbQuery",
            "X-Requested-With": "XMLHttpRequest",
        }

        # 构造 URL（使用 session 传递 headers）
        url = SEARCH_API + "?" + urlencode(params, encoding="utf-8")

        data = safe_get_json(url, self.session, headers=headers)
        if data is None:
            logger.warning("API 返回空，尝试重试一次...")
            data = safe_get_json(url, self.session, headers=headers)

        if data is None:
            logger.warning("API 持续失败: searchText=%s page=%d", search_text[:20], page)
            return []

        return self._parse_api_response(data)

    def _parse_api_response(self, data: dict) -> List[dict]:
        """解析 SAMR JSON API 返回"""
        results = []

        total = data.get("total", 0)
        rows = data.get("rows", [])

        if not isinstance(rows, list) or total == 0:
            return results

        for raw in rows:
            if not isinstance(raw, dict):
                continue
            item = self._map_row(raw)
            if item:
                results.append(item)

        logger.debug("SAMR API: total=%d, this_page=%d", total, len(results))
        return results

    def _map_row(self, raw: dict) -> Optional[dict]:
        """映射 SAMR 单行数据到统一格式

        原始字段:
          - id: UUID
          - C_C_NAME: 标准名称（含 <sacinfo> 标签）
          - C_STD_CODE: 标准号
          - STD_NATURE: 推荐性/强制性
          - ACT_DATE: 实施日期
          - STATE: 现行/废止/即将实施
          - ISSUE_DATE: 发布日期
          - PROJECT_ID: 项目编号
        """
        # 标题：去除 <sacinfo> 标签
        raw_title = raw.get("C_C_NAME", "") or ""
        title = re.sub(r"</?sacinfo>", "", raw_title).strip()
        if not title:
            return None

        standard_no = raw.get("C_STD_CODE", "") or ""
        std_nature = raw.get("STD_NATURE", "") or ""
        act_date = raw.get("ACT_DATE", "") or ""
        state = raw.get("STATE", "") or ""
        issue_date = raw.get("ISSUE_DATE", "") or ""
        record_id = raw.get("id", "") or ""
        project_id = raw.get("PROJECT_ID", "") or ""

        # 发布机构：SAMR API 返回中不包含，可补充
        publisher = "国家市场监督管理总局"
        if "强制性" in std_nature:
            publisher = "国家市场监督管理总局"
        elif "推荐性" in std_nature:
            publisher = "国家市场监督管理总局"

        # 状态映射
        status_map = {
            "现行": "现行",
            "废止": "废止",
            "即将实施": "即将实施",
            "废止 ": "废止",
        }
        status = status_map.get(state.strip(), state.strip())

        # 推测 ICS 代码（搜索时已知）
        ics_code = ""

        detail_url = DETAIL_URL.format(record_id) if record_id else ""

        item = make_standard_item(
            title=title,
            standard_no=standard_no,
            publisher=publisher,
            publish_date=issue_date,
            status=status,
            source=self.source_name,
            url=detail_url,
            ics_code=ics_code,
            scopes="",
            summary="",
        )

        # 保留原始数据
        item["_raw"] = {
            "std_nature": std_nature,
            "act_date": normalize_date(act_date),
            "project_id": project_id,
            "record_id": record_id,
        }

        return item

    def fetch_ics_from_detail(self, item: dict) -> str:
        """从详情页提取 ICS 代码"""
        url = item.get("url", "")
        if not url:
            return ""
        try:
            html = safe_get(url, self.session)
            if html:
                soup = BeautifulSoup(html, "html.parser")
                # SAMR 详情页格式：<td>29.140.40 照明设备</td>
                # 优先提取最精确的三级 ICS (如 29.140.40)
                best_code = ""
                for td in soup.find_all("td"):
                    text = td.get_text(strip=True)
                    m = re.match(r"^(\d{2}\.\d{1,3}(?:\.\d{1,3})?)\s", text)
                    if m:
                        code = m.group(1)
                        # 三级 > 二级 > 一级
                        if code.count(".") == 2:
                            return code  # 三级最精确，直接返回
                        elif code.count(".") == 1 and not best_code:
                            best_code = code
                        elif code.count(".") == 0 and not best_code:
                            best_code = code
                if best_code:
                    return best_code
        except Exception as e:
            self.logger.debug("提取ICS失败: %s", e)
        return ""

    def enrich_ics_codes(self, items: List[dict], max_workers: int = 3) -> List[dict]:
        """批量从详情页提取 ICS 代码"""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _fetch(item):
            if item.get("ics_code"):
                return item  # 已有 ICS，跳过
            ics = self.fetch_ics_from_detail(item)
            if ics:
                item["ics_code"] = ics
            return item

        enriched = []
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = [ex.submit(_fetch, it) for it in items]
            for i, f in enumerate(as_completed(futures)):
                enriched.append(f.result())
                if (i + 1) % 10 == 0:
                    self.logger.info("ICS enrichment: %d/%d", i + 1, len(items))

        return enriched
