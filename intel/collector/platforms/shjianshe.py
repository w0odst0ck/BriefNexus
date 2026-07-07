#!/usr/bin/env python3
"""
上海住建委 — 政府公告采集
"""

import logging, re
from typing import List
from bs4 import BeautifulSoup

from .base import BaseCollector, NewsItem, CST

logger = logging.getLogger("intel.collector.shjianshe")


class ShjiansheCollector(BaseCollector):
    source_name = "上海住建委"
    display_name = "上海住建委（政府公告）"

    TARGETS = [
        ("https://zjw.sh.gov.cn", "首页"),
        ("https://zjw.sh.gov.cn/zwgk/", "政务公开"),
    ]

    def crawl(self, sess) -> List[NewsItem]:
        items = []
        seen_url = set()
        for url, label in self.TARGETS:
            html = self._get(sess, url)
            if not html:
                continue
            soup = BeautifulSoup(html, "lxml")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                title = a.get_text(strip=True)
                if not title or len(title) < 10 or href in seen_url:
                    continue
                if not href.startswith("http"):
                    if href.startswith("/"):
                        href = "https://zjw.sh.gov.cn" + href
                    else:
                        continue
                if "zjw.sh.gov.cn" not in href:
                    continue
                seen_url.add(href)
                date_obj = self._date_from_url(href)
                items.append(NewsItem(
                    title=title, url=href, date_obj=date_obj,
                    source="上海住建委", domain="政府公告",
                ))
            self._delay()
        logger.info("上海住建委: %d 条", len(items))
        return items

    def _date_from_url(self, url: str):
        m = re.search(r"/(\d{4})[-/](\d{1,2})[-/](\d{1,2})", url)
        if m:
            try:
                from datetime import datetime
                return datetime(int(m.group(1)), int(m.group(2)),
                                int(m.group(3)), tzinfo=CST)
            except:
                pass
        return None

    def _get(self, sess, url: str):
        try:
            r = sess.get(url, timeout=15)
            r.encoding = "utf-8"
            return r.text
        except:
            return None

    def _delay(self):
        import time, random
        time.sleep(random.uniform(0.5, 1.5))
