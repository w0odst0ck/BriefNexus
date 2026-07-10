"""
东方财富 — A股公告/要闻（上证指数要闻）
"""

import logging, re, json
from datetime import datetime, timezone, timedelta
from typing import List, Optional
from intel.core.registry import register
from intel.core.base import BaseCollector, NewsItem, CST

logger = logging.getLogger("intel.eastmoney")

# 东方财富要闻列表（服务端渲染，无需 API）
EASTMONEY_FINANCE = "https://finance.eastmoney.com/a/czqyw.html"
# 备用：东方财富首页
EASTMONEY_HOME = "https://www.eastmoney.com/"


@register("eastmoney")
class EastMoneyCollector(BaseCollector):
    source_name = "eastmoney"
    display_name = "东方财富"

    def crawl(self, sess) -> List[NewsItem]:
        items = []
        try:
            # 主源：东方财富要闻列表（服务端渲染）
            r = sess.get(EASTMONEY_FINANCE, timeout=30)
            r.raise_for_status()
            r.encoding = "utf-8"
            items = self._parse_article_list(r.text, EASTMONEY_FINANCE)

            if not items:
                logger.info("要闻列表未解析到内容，尝试首页")
                items = self._crawl_fallback(sess)

        except Exception as e:
            logger.error("东方财富要闻列表采集失败: %s，尝试备用", e)
            items = self._crawl_fallback(sess)

        return items

    def _parse_article_list(self, html: str, base_url: str) -> List[NewsItem]:
        """解析 finance.eastmoney.com 的要闻文章列表"""
        items = []
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            seen = set()

            # 方案1: div.title a → 主要文章
            for a in soup.select("div.title a"):
                title = a.get_text(strip=True)
                href = a.get("href", "")
                if not title or len(title) < 8 or href in seen:
                    continue
                seen.add(href)
                if not href.startswith("http"):
                    href = "https:" + href if href.startswith("//") else base_url.rstrip("/") + "/" + href.lstrip("/")
                item = NewsItem(
                    title=title,
                    url=href,
                    source=self.display_name,
                    domain="金融",
                    sector="A股",
                )
                items.append(item)

            # 方案2: li a → 补充列表中的文章
            if len(items) < 10:
                for a in soup.select("li a"):
                    title = a.get_text(strip=True)
                    href = a.get("href", "")
                    if not title or len(title) < 8 or href in seen:
                        continue
                    seen.add(href)
                    if not href.startswith("http"):
                        href = "https:" + href if href.startswith("//") else base_url.rstrip("/") + "/" + href.lstrip("/")
                    item = NewsItem(
                        title=title,
                        url=href,
                        source=self.display_name,
                        domain="金融",
                        sector="A股",
                    )
                    items.append(item)
                    if len(items) >= 30:
                        break

        except Exception as e:
            logger.error("要闻列表解析失败: %s", e)

        return items[:30]

    def _crawl_fallback(self, sess) -> List[NewsItem]:
        """备用方案：从东方财富首页解析要闻"""
        items = []
        try:
            from bs4 import BeautifulSoup
            r = sess.get("https://www.eastmoney.com/", timeout=30)
            r.raise_for_status()
            # 东方财富首页使用 UTF-8 with BOM
            r.encoding = "utf-8-sig"
            soup = BeautifulSoup(r.text, "html.parser")

            seen = set()

            # 优先用已知的新闻容器选择器
            containers = [
                "div.head-news.bw",       # 东方视点 / 焦点专题
                "div.newsboxb",            # 新闻盒子
                "div.hsgs_news",           # 股市焦点
                "div.news_kuaixun",        # 7x24h 快讯
                "div.news_l2",             # 二级新闻
            ]
            for container_sel in containers:
                container = soup.select_one(container_sel)
                if not container:
                    continue
                for a in container.find_all("a", href=True):
                    title = a.get_text(strip=True)
                    href = a["href"]
                    if not title or len(title) < 6:
                        continue
                    if href in seen:
                        continue
                    seen.add(href)
                    if not href.startswith("http"):
                        href = "https:" + href if href.startswith("//") else "https://www.eastmoney.com" + href
                    # 跳过导航/功能链接
                    if any(skip in href for skip in ["guba", "quote", "zixuan"]):
                        continue
                    item = NewsItem(
                        title=title,
                        url=href,
                        source=self.display_name,
                        domain="金融",
                        sector="A股",
                    )
                    items.append(item)
                    if len(items) >= 20:
                        break
                if len(items) >= 20:
                    break

            # 兜底：从 div.title a 中补充
            if len(items) < 5:
                for a in soup.select("div.title a"):
                    title = a.get_text(strip=True)
                    href = a.get("href", "")
                    if not title or len(title) < 6 or href in seen:
                        continue
                    seen.add(href)
                    if not href.startswith("http"):
                        href = "https:" + href if href.startswith("//") else "https://www.eastmoney.com" + href
                    if any(skip in href for skip in ["guba", "quote", "zixuan"]):
                        continue
                    item = NewsItem(
                        title=title,
                        url=href,
                        source=self.display_name,
                        domain="金融",
                        sector="A股",
                    )
                    items.append(item)
                    if len(items) >= 20:
                        break

        except Exception as e:
            logger.error("东方财富首页解析失败: %s", e)

        return items

    @staticmethod
    def _parse_date(raw) -> Optional[datetime]:
        """尝试多种时间格式"""
        if isinstance(raw, (int, float)):
            # 毫秒时间戳
            ts = raw if raw > 1e11 else raw * 1000
            return datetime.fromtimestamp(ts / 1000, tz=CST)
        s = str(raw).strip()
        for fmt in [
            "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
            "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d",
        ]:
            try:
                return datetime.strptime(s, fmt).replace(tzinfo=CST)
            except ValueError:
                continue
        return None
