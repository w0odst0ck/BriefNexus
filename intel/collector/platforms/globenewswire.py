"""
GlobeNewswire — 企业公告直发平台（财报/并购/技术发布/上市公告）
"""

import logging, re, time
from datetime import datetime, timezone, timedelta
from typing import List, Optional
from bs4 import BeautifulSoup
from .base import BaseCollector, NewsItem, CST

logger = logging.getLogger("intel.globenewswire")

GN_RSS = "https://www.globenewswire.com/RssFeed/industry/13-Technology/feed.xml"
GN_TOPIC_RSS = "https://www.globenewswire.com/RssFeed/subjectcode/13-Software/feed.xml"


class GlobeNewswireCollector(BaseCollector):
    source_name = "globenewswire"
    display_name = "GlobeNewswire"

    def crawl(self, sess) -> List[NewsItem]:
        items = []
        urls_seen = set()

        for rss_url in [GN_RSS, GN_TOPIC_RSS]:
            try:
                import feedparser
                r = sess.get(rss_url, timeout=30)
                r.raise_for_status()
                feed = feedparser.parse(r.content)

                for entry in feed.entries[:40]:
                    title = entry.get("title", "").strip()
                    url = entry.get("link", "").strip()
                    if not title or not url or url in urls_seen:
                        continue
                    urls_seen.add(url)

                    date_obj = None
                    pub = entry.get("published_parsed") or entry.get("updated_parsed")
                    if pub:
                        date_obj = datetime(*pub[:6], tzinfo=timezone.utc)

                    summary = entry.get("summary", "")[:300]

                    item = NewsItem(title=title, url=url, summary=summary,
                                    source=self.display_name, domain="资本",
                                    date_obj=date_obj)
                    items.append(item)

            except Exception as e:
                logger.warning("GlobeNewswire[%s] 采集失败: %s", rss_url, e)

        return items
