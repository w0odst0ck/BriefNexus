"""
NVIDIA Blog — 企业官方博客（AI/GPGPU/机器人/HPC）
"""

import logging, re, time
from datetime import datetime, timezone, timedelta
from typing import List, Optional
from intel.core.registry import register
from intel.core.base import BaseCollector, NewsItem, CST

logger = logging.getLogger("intel.nvidia")

NVIDIA_RSS = "https://blogs.nvidia.com/feed/"


@register("nvidia_blog")
class NvidiaBlogCollector(BaseCollector):
    source_name = "nvidia_blog"
    domains = ["semiconductor"]
    display_name = "NVIDIA Blog"

    def crawl(self, sess) -> List[NewsItem]:
        items = []
        try:
            import feedparser
            r = sess.get(NVIDIA_RSS, timeout=30)
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

                # 取摘要
                summary = ""
                for f in ["summary", "description"]:
                    v = entry.get(f, "")
                    if v:
                        summary = v[:300]
                        break

                item = NewsItem(title=title, url=url, summary=summary,
                                source=self.display_name, domain="企业", date_obj=date_obj)
                items.append(item)

        except Exception as e:
            logger.error("NVIDIA Blog 采集失败: %s", e)

        return items
