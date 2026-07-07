#!/usr/bin/env python3
"""
CSA联盟（半导体照明网）— 政策产业动态采集
"""

import logging, re
from datetime import datetime
from typing import List
from bs4 import BeautifulSoup

from .base import BaseCollector, NewsItem, CST

logger = logging.getLogger("intel.collector.csa")


class CsaCollector(BaseCollector):
    source_name = "CSA联盟"
    display_name = "CSA联盟（半导体照明网）"

    def crawl(self, sess) -> List[NewsItem]:
        items = []
        html = self._get(sess, "https://www.china-led.net/news/")
        if not html:
            return items
        soup = BeautifulSoup(html, "lxml")
        seen_url = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            title = a.get_text(strip=True)
            if not title or len(title) < 10 or not href or href in seen_url:
                continue
            if "/news/" not in href and "/special/" not in href:
                continue
            seen_url.add(href)
            if not href.startswith("http"):
                href = "https://www.china-led.net" + href
            date_obj = self._extract_date(a)
            if not date_obj:
                date_obj = self._date_from_url(href)
            items.append(NewsItem(
                title=title, url=href, date_obj=date_obj,
                source="CSA联盟", domain="政策产业",
            ))
        logger.info("CSA: %d 条", len(items))
        return items

    def _extract_date(self, a_tag) -> datetime | None:
        parent = a_tag.parent
        for sibling in parent.find_all(["span", "em", "small", "time"]):
            txt = sibling.get_text(strip=True)
            m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", txt)
            if m:
                try:
                    return datetime(int(m.group(1)), int(m.group(2)),
                                    int(m.group(3)), tzinfo=CST)
                except:
                    pass
        return None

    def _date_from_url(self, url: str) -> datetime | None:
        m = re.search(r"/(\d{4})[-/](\d{1,2})[-/](\d{1,2})", url)
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)),
                                int(m.group(3)), tzinfo=CST)
            except:
                pass
        return None

    def _get(self, sess, url: str) -> str | None:
        try:
            r = sess.get(url, timeout=15)
            r.encoding = "utf-8"
            return r.text
        except:
            return None
