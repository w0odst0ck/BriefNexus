"""
White House Briefing Room — 美国政府官方声明（科技/AI/贸易/经济）
"""

import logging, re, time
from datetime import datetime, timezone, timedelta
from typing import List, Optional
from bs4 import BeautifulSoup
from intel.core.registry import register
from intel.core.base import BaseCollector, NewsItem, CST

logger = logging.getLogger("intel.whitehouse")

WH_BASE = "https://www.whitehouse.gov"
WH_URL = f"{WH_BASE}/briefing-room/"


@register("white_house")
class WhiteHouseCollector(BaseCollector):
    source_name = "whitehouse"
    display_name = "White House"

    def crawl(self, sess) -> List[NewsItem]:
        items = []
        try:
            r = sess.get(WH_URL, timeout=30)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")

            for post in soup.select("li.wp-block-post"):
                time_el = post.find("time")
                link_el = post.find("a", href=True)

                if not link_el:
                    continue

                title = link_el.get_text(strip=True)
                url = link_el["href"]
                if not url.startswith("http"):
                    url = WH_BASE + url

                date_obj = None
                if time_el and time_el.get("datetime"):
                    try:
                        date_obj = datetime.fromisoformat(time_el["datetime"].replace("Z", "+00:00"))
                    except:
                        pass

                if not title:
                    continue

                item = NewsItem(title=title, url=url, source=self.display_name,
                                domain="政策", date_obj=date_obj)
                items.append(item)

        except Exception as e:
            logger.error("White House 采集失败: %s", e)

        return items
