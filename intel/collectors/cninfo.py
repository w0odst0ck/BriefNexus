"""
巨潮资讯 — A股上市公司公告
"""

import logging, re, json
from datetime import datetime, timezone, timedelta
from typing import List, Optional
from urllib.parse import urljoin
from intel.core.registry import register
from intel.core.base import BaseCollector, NewsItem, CST

logger = logging.getLogger("intel.cninfo")

CNINFO_QUERY = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_HOME = "http://www.cninfo.com.cn/new/disclosure"
CNINFO_DETAIL = "http://www.cninfo.com.cn/new/disclosure/detail"

# 列名映射（公告分类）
COLUMNS = {
    "szse_main": "深交所主板",
    "szse_gem": "深交所创业板",
    "sse_main": "上交所主板",
    "sse_kcb": "上交所科创板",
    "bjse": "北交所",
}


@register("cninfo")
class CninfoCollector(BaseCollector):
    source_name = "cninfo"
    display_name = "巨潮资讯"

    def crawl(self, sess) -> List[NewsItem]:
        items = []
        try:
            # 尝试 POST API
            payload = {
                "pageNum": 1,
                "pageSize": 20,
                "column": "szse_main",
                "tabName": "fulltext",
                "plate": "sz",
                "stock": "",
                "searchkey": "",
                "secid": "",
                "category": "",
                "trade": "",
                "seDate": ["", ""],
                "sortName": "",
                "sortType": "",
                "isHLtitle": True,
            }
            headers = {
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Referer": "http://www.cninfo.com.cn/new/disclosure",
                "Origin": "http://www.cninfo.com.cn",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
            }

            r = sess.post(
                CNINFO_QUERY,
                data=payload,
                headers=headers,
                timeout=30,
            )
            r.raise_for_status()
            result = r.json()

            announcements = result.get("announcements")
            if not announcements or not isinstance(announcements, list):
                logger.warning("巨潮 API 返回异常: %s", str(result)[:200])
                return self._crawl_fallback(sess)

            for ann in announcements:
                try:
                    title = (ann.get("announcementTitle") or "").strip()
                    ann_id = (ann.get("announcementId") or "").strip()
                    if not title or not ann_id:
                        continue

                    # 去除 HTML 标签
                    title = re.sub(r"<[^>]+>", "", title).strip()

                    # 构造详情 URL
                    org_id = (ann.get("orgId") or "").strip()
                    url = f"{CNINFO_DETAIL}?announcementId={ann_id}"
                    if org_id:
                        url += f"&orgId={org_id}"

                    # 解析时间
                    date_obj = None
                    raw_time = ann.get("announcementTime")
                    if raw_time:
                        try:
                            ts = int(raw_time)
                            date_obj = datetime.fromtimestamp(ts / 1000, tz=CST)
                        except (ValueError, OSError):
                            pass

                    # 公告分类
                    col = ann.get("column", "")
                    sector = COLUMNS.get(col, "A股公告")

                    # 股票代码
                    stock_code = (ann.get("secCode") or "").strip()
                    stock_name = (ann.get("secName") or "").strip()
                    summary_parts = []
                    if stock_code and stock_name:
                        summary_parts.append(f"[{stock_code} {stock_name}]")
                    adjuct_url = ann.get("adjunctUrl", "")
                    if adjuct_url:
                        summary_parts.append("含附件")

                    item = NewsItem(
                        title=title,
                        url=url,
                        summary=" ".join(summary_parts),
                        source=self.display_name,
                        domain="金融",
                        sector=sector,
                        date_obj=date_obj,
                    )
                    items.append(item)
                except Exception:
                    continue

        except Exception as e:
            logger.error("巨潮 API 采集失败: %s，尝试备用方案", e)
            items = self._crawl_fallback(sess)

        return items

    def _crawl_fallback(self, sess) -> List[NewsItem]:
        """备用方案：从巨潮资讯首页抓取最新公告列表"""
        items = []
        try:
            from bs4 import BeautifulSoup
            r = sess.get(CNINFO_HOME, timeout=30)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")

            # 尝试多种选择器
            for selector in [
                "div.announcement-list a", "ul.list li a",
                "div.news-list a", "div.info-list a",
                "div.right-content a",
            ]:
                links = soup.select(selector)
                for a in links[:25]:
                    title = a.get_text(strip=True)
                    href = a.get("href", "")
                    if not title or not href or len(title) < 6:
                        continue
                    if not href.startswith("http"):
                        href = urljoin(CNINFO_HOME, href)

                    item = NewsItem(
                        title=re.sub(r"<[^>]+>", "", title).strip(),
                        url=href,
                        source=self.display_name,
                        domain="金融",
                        sector="A股公告",
                    )
                    items.append(item)
                    if len(items) >= 20:
                        break
                if items:
                    break
        except Exception as e:
            logger.error("巨潮首页解析失败: %s", e)

        return items
