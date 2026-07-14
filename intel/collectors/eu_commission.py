"""
EU Commission Press Corner — 欧盟委员会官方新闻稿（AI法案/数字政策/网络安全/贸易）
"""

import logging, re, time
from datetime import datetime, timezone, timedelta
from typing import List, Optional
from intel.core.registry import register
from intel.core.base import BaseCollector, NewsItem, CST

logger = logging.getLogger("intel.eu")

EU_RSS = "https://ec.europa.eu/commission/presscorner/api/rss?type=IP"


@register("eu_commission")
class EUCommissionCollector(BaseCollector):
    source_name = "eu_commission"
    domains = ["finance", "self_driving", "semiconductor"]
    display_name = "EU Commission"

    def crawl(self, sess) -> List[NewsItem]:
        items = []
        try:
            import feedparser
            r = sess.get(EU_RSS, timeout=30)
            r.raise_for_status()

            # feedparser 可能无法解析某些编码，手动传给 feedparser
            feed = feedparser.parse(r.content)

            for entry in feed.entries[:50]:
                title = entry.get("title", "").strip()
                url = entry.get("link", "").strip()
                if not title or not url:
                    continue

                date_obj = None
                pub = entry.get("published_parsed") or entry.get("updated_parsed")
                if pub:
                    date_obj = datetime(*pub[:6], tzinfo=timezone.utc)

                item = NewsItem(title=title, url=url, source=self.display_name,
                                domain="政策", date_obj=date_obj)
                items.append(item)

        except Exception as e:
            logger.error("EU Commission 采集失败: %s", e)

        return items
