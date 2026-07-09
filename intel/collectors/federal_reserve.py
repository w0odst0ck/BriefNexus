"""
Federal Reserve — 美联储新闻稿（利率决策/金融监管/经济政策）
"""

import logging, re, time
from datetime import datetime, timezone, timedelta
from typing import List, Optional
from bs4 import BeautifulSoup
from intel.core.registry import register
from intel.core.base import BaseCollector, NewsItem, CST

logger = logging.getLogger("intel.fed")

FED_RSS = "https://www.federalreserve.gov/feeds/press_all.xml"


@register("federal_reserve")
class FederalReserveCollector(BaseCollector):
    source_name = "federal_reserve"
    display_name = "Federal Reserve"

    def crawl(self, sess) -> List[NewsItem]:
        items = []
        try:
            import feedparser
            r = sess.get(FED_RSS, timeout=30)
            r.raise_for_status()
            feed = feedparser.parse(r.content)

            for entry in feed.entries[:30]:
                title = entry.get("title", "").strip()
                url = entry.get("link", "").strip()
                if not title or not url:
                    continue

                date_obj = None
                pub = entry.get("published_parsed") or entry.get("updated_parsed")
                if pub:
                    date_obj = datetime(*pub[:6], tzinfo=timezone.utc)

                summary = entry.get("summary", "")[:300]

                item = NewsItem(title=title, url=url, summary=summary,
                                source=self.display_name, domain="宏观",
                                date_obj=date_obj)
                items.append(item)

        except Exception as e:
            logger.error("Federal Reserve 采集失败: %s", e)

        return items
