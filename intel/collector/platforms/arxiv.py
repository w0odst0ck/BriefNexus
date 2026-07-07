#!/usr/bin/env python3
"""
arXiv 学术论文采集
"""

import logging, re, time, feedparser
from datetime import datetime, timezone, timedelta
from typing import List
from bs4 import BeautifulSoup

from .base import BaseCollector, NewsItem, CST

logger = logging.getLogger("intel.collector.arxiv")


class ArxivCollector(BaseCollector):
    source_name = "arXiv"
    display_name = "arXiv 学术论文"

    QUERY = ("all:microLED+OR+all:visible+light+communication+OR+"
             "all:LiFi+OR+(all:LED+AND+all:lighting)+OR+"
             "(all:optical+AND+all:interconnect+AND+all:silicon)")

    def crawl(self, sess) -> List[NewsItem]:
        items = self._crawl_api(sess)
        if items:
            return items
        return self._crawl_web(sess)

    def _crawl_api(self, sess) -> List[NewsItem]:
        """方式1：arXiv API"""
        items = []
        try:
            r = sess.get(
                "http://export.arxiv.org/api/query",
                params={"search_query": self.QUERY,
                        "sortBy": "submittedDate",
                        "sortOrder": "descending",
                        "max_results": 15},
                timeout=30,
            )
            if r.status_code == 200:
                feed = feedparser.parse(r.text)
                for entry in feed.entries:
                    title = entry.title.replace("\n", " ").strip()
                    if not title or len(title) < 10:
                        continue
                    link = entry.link if hasattr(entry, "link") else ""
                    pub_date = None
                    if hasattr(entry, "published"):
                        try:
                            pub_date = datetime.strptime(entry.published[:10], "%Y-%m-%d")
                            pub_date = pub_date.replace(tzinfo=CST)
                        except:
                            pass
                    summary = ""
                    if hasattr(entry, "summary"):
                        summary = re.sub(r"<[^>]+>", "", entry.summary)
                        summary = summary.replace("\n", " ").strip()
                    authors = []
                    if hasattr(entry, "authors"):
                        authors = [a.name for a in entry.authors[:3]]
                    author_str = ", ".join(authors) if authors else ""
                    summary_text = f"作者: {author_str}\n{summary}" if author_str else summary
                    items.append(NewsItem(
                        title=title, url=link, summary=summary_text,
                        date_obj=pub_date, source="arXiv", domain="学术论文",
                    ))
                if items:
                    logger.info("arXiv API: %d 条", len(items))
                    return items
        except Exception as e:
            logger.warning("arXiv API 不可用: %s", e)
        return items

    def _crawl_web(self, sess) -> List[NewsItem]:
        """方式2：网页抓取"""
        items = []
        logger.info("arXiv 尝试网页抓取...")
        try:
            html = self._fetch(sess, "https://arxiv.org/list/physics.optics/recent")
            if not html:
                return items
            soup = BeautifulSoup(html, "lxml")
            current_date = None
            paper_links = []
            for tag in soup.find_all(["h3", "dt"]):
                if tag.name == "h3":
                    m = re.search(r"(\w+,\s+\d+\s+\w+\s+\d{4})", tag.get_text())
                    if m:
                        try:
                            current_date = datetime.strptime(m.group(1), "%a, %d %b %Y")
                            current_date = current_date.replace(tzinfo=CST)
                        except:
                            current_date = None
                    continue
                a = tag.find("a", title=True)
                if not a:
                    continue
                link = "https://arxiv.org" + a.get("href", "")
                dd = tag.find_next_sibling("dd")
                title = ""
                if dd:
                    title_el = dd.select_one(".list-title")
                    if title_el:
                        title = title_el.get_text(strip=True).replace("Title:", "").strip()
                if not title or len(title) < 10:
                    continue
                paper_links.append((title, link, current_date))
                if len(paper_links) >= 15:
                    break
            for title, link, date_obj in paper_links:
                abs_html = self._fetch(sess, link)
                summary = ""
                if abs_html:
                    abs_soup = BeautifulSoup(abs_html, "lxml")
                    abs_el = abs_soup.select_one("blockquote.abstract")
                    if abs_el:
                        summary = abs_el.get_text(strip=True).replace("Abstract:", "").strip()
                items.append(NewsItem(
                    title=title, url=link, summary=summary,
                    date_obj=date_obj, source="arXiv", domain="学术论文",
                ))
                time.sleep(0.5)
        except Exception as e:
            logger.warning("arXiv 网页抓取失败: %s", e)
        logger.info("arXiv 网页: %d 条", len(items))
        return items

    def _fetch(self, sess, url: str) -> str:
        try:
            r = sess.get(url, timeout=15)
            r.encoding = "utf-8"
            return r.text
        except:
            return ""
