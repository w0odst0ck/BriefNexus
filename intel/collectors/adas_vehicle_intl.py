"""
ADAS & Autonomous Vehicle International — 行业出版物

追踪最新摘录中与大灯眩光、感知、照明法规相关的内容。

源: https://adas.mydigitalpublication.com/

注意: 该网站有反爬保护（403），备用方案：
  - 通过 Google 搜索/索引获取最新文章
  - web_fetch 读取公开文章页面
"""

import logging, re, json, urllib.parse
from datetime import datetime, timezone, timedelta
from typing import List, Optional
from intel.core.registry import register
from intel.core.base import BaseCollector, NewsItem, CST

logger = logging.getLogger("intel.adas")

# 通过 Google 搜索获取该杂志的最新文章
SEARCH_URL = "https://www.google.com/search"
SEARCH_QUERY = 'site:adas.mydigitalpublication.com "headlight" OR "glare" OR "lighting" OR "perception" OR "LED"'

INTERESTING_TOPICS = [
    "glare", "lighting", "headlight", "night", "perception",
    "LED", "ADAS", "autonomous", "camera", "sensor",
    "regulation", "safety", "low light", "illumination",
]


@register("adas_vehicle_intl")
class AdasVehicleCollector(BaseCollector):
    source_name = "adas_vehicle_intl"
    display_name = "ADAS & Autonomous Vehicle Intl"
    domains = ["self_driving"]

    def crawl(self, sess) -> List[NewsItem]:
        items = []
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        }

        # 尝试方法1: 直接访问，但加好头
        try:
            r = sess.get("https://adas.mydigitalpublication.com/",
                         timeout=15, headers=headers)
            if r.ok:
                html = r.text
                # 提取文章标题 + 链接（通用模式）
                articles = re.findall(
                    r'<a[^>]*href="(/?[a-z][^"]*page\d+[^"]*|/?[a-z][^"]*\d{4}/?[^"]*)"[^>]*>([^<]{15,})</a>',
                    html
                )
                seen = set()
                for url, title in articles:
                    title = re.sub(r'\s+', ' ', title).strip()
                    if not title:
                        continue
                    dedup = title.lower()[:40]
                    if dedup in seen:
                        continue
                    seen.add(dedup)

                    if url.startswith("/"):
                        url = "https://adas.mydigitalpublication.com" + url
                    elif not url.startswith("http"):
                        url = "https://adas.mydigitalpublication.com/" + url

                    tl = title.lower()
                    relevant = any(k in tl for k in INTERESTING_TOPICS)
                    items.append(NewsItem(
                        title=title, url=url, summary=f"[{'相关' if relevant else '其他'}] {title}",
                        source=self.display_name, domain="行业",
                        date_obj=datetime.now(CST),
                        sector="lighting_perception" if relevant else "",
                    ))

                if items:
                    logger.info("ADAS 直接访问成功: %d 条", len(items))
        except Exception as e:
            logger.debug("ADAS 直接访问失败: %s", e)

        # 方法2: 如果直接访问没出结果，用搜索引擎线索
        if not items:
            try:
                r = sess.get(SEARCH_URL, params={"q": SEARCH_QUERY, "num": 10},
                             timeout=15, headers=headers)
                if r.ok:
                    # 提取搜索结果链接
                    links = re.findall(r'<a[^>]*href="(https://adas\.mydigitalpublication\.com[^"]*)"[^>]*>(.*?)</a>',
                                       r.text)
                    seen = set()
                    for url, title in links:
                        title = re.sub(r'<[^>]+>', '', title).strip()
                        if not title:
                            continue
                        dedup = title.lower()[:40]
                        if dedup in seen:
                            continue
                        seen.add(dedup)
                        tl = title.lower()
                        relevant = any(k in tl for k in INTERESTING_TOPICS)
                        items.append(NewsItem(
                            title=title, url=url, summary=f"[{'相关' if relevant else '其他'}] {title}",
                            source=self.display_name, domain="行业",
                            date_obj=datetime.now(CST),
                            sector="lighting_perception" if relevant else "",
                        ))
                    if items:
                        logger.info("ADAS 搜索引擎: %d 条", len(items))
            except Exception as e:
                logger.debug("ADAS 搜索引擎失败: %s", e)

        # 兜底：保留杂志主页URL供手动查看
        if not items:
            items.append(NewsItem(
                title="ADAS & Autonomous Vehicle International - 最新期",
                url="https://adas.mydigitalpublication.com/",
                summary="季刊，关注ADAS/自动驾驶/照明",
                source=self.display_name, domain="行业",
                date_obj=datetime.now(CST),
            ))

        logger.info("ADAS 最终采集: %d 条", len(items))
        return items
