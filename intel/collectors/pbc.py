"""
中国人民银行 — 货币政策/公开市场操作公告
"""

import logging, re
from datetime import datetime, timezone, timedelta
from typing import List, Optional
from urllib.parse import urljoin

from intel.core.registry import register
from intel.core.base import BaseCollector, NewsItem, CST

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

logger = logging.getLogger("intel.pbc")

# 公开市场操作公告列表
PBC_OPEN_MARKET = "http://www.pbc.gov.cn/zhengcehuobisi/125207/125213/125431/index.html"
# 首页要闻
PBC_HOME = "http://www.pbc.gov.cn/"
PBC_GOV = "https://www.pbc.gov.cn"


@register("pbc")
class PbcCollector(BaseCollector):
    source_name = "pbc"
    display_name = "中国人民银行"

    def crawl(self, sess) -> List[NewsItem]:
        items = []
        try:
            # 主源：央行首页要闻
            r = sess.get(PBC_HOME, timeout=30)
            r.raise_for_status()
            # PBC 使用 UTF-8 编码，但 requests 可能误判
            r.encoding = "utf-8"
            soup = BeautifulSoup(r.text, "html.parser")
            items = self._parse_homepage(soup)

            if not items:
                logger.info("首页未解析到内容，尝试公开市场操作页")
                items = self._crawl_open_market(sess)

        except ImportError:
            logger.warning("BeautifulSoup 未安装，无法解析 PBC 页面")
        except Exception as e:
            logger.error("PBC 首页采集失败: %s，尝试公开市场页", e)
            items = self._crawl_open_market(sess)

        return items

    # noinspection PyMethodMayBeStatic
    def _parse_homepage(self, soup) -> List[NewsItem]:
        """解析央行首页要闻"""
        items = []
        seen = set()

        # 首页新闻列表的多个容器
        containers = [
            # 滚动要闻区
            soup.find(id="ssnews"),
            soup.find(id="scroll_div"),
            # 主内容区域
            soup.find("div", class_="mainw950"),
        ]

        for container in containers:
            if not container:
                continue
            for a in container.find_all("a", href=True):
                title = a.get_text(strip=True)
                href = a["href"].strip()
                if not title or len(title) < 8 or href in seen:
                    continue
                if href.startswith("javascript") or href == "#":
                    continue
                seen.add(href)

                if not href.startswith("http"):
                    href = urljoin(PBC_GOV, href)

                # 从 URL 提取可能的日期（PBC 页面 URL 常包含日期）
                date_obj = self._extract_date_from_url(href)

                item = NewsItem(
                    title=title,
                    url=href,
                    source=self.display_name,
                    domain="宏观",
                    sector="货币政策",
                    date_obj=date_obj,
                )
                items.append(item)

        return items[:30]

    def _crawl_open_market(self, sess) -> List[NewsItem]:
        """备用：解析公开市场操作公告页"""
        items = []
        try:
            r = sess.get(PBC_OPEN_MARKET, timeout=30)
            r.raise_for_status()
            r.encoding = "utf-8"
            soup = BeautifulSoup(r.text, "html.parser")
            seen = set()

            # 公开市场公告页通常是表格结构
            for a in soup.find_all("a", href=True):
                title = a.get_text(strip=True)
                href = a["href"].strip()
                if not title or len(title) < 8 or href in seen:
                    continue
                if href.startswith("javascript") or href == "#":
                    continue
                seen.add(href)

                if not href.startswith("http"):
                    href = urljoin(PBC_GOV, href)

                # 过滤导航链接
                if "index.html" in href and "125431" in href:
                    continue

                date_obj = self._extract_date_from_url(href)

                # 尝试从附近文本提取日期
                if not date_obj:
                    parent = a.find_parent(["td", "li", "div"])
                    if parent:
                        m = re.search(r"(\d{4}[-/]\d{1,2}[-/]\d{1,2})", parent.get_text())
                        if m:
                            try:
                                date_obj = datetime.strptime(m.group(1).replace("/", "-"), "%Y-%m-%d").replace(tzinfo=CST)
                            except ValueError:
                                pass

                item = NewsItem(
                    title=title,
                    url=href,
                    source=self.display_name,
                    domain="宏观",
                    sector="货币政策",
                    date_obj=date_obj,
                )
                items.append(item)

        except Exception as e:
            logger.error("公开市场操作页采集失败: %s", e)

        return items[:30]

    @staticmethod
    def _extract_date_from_url(url: str) -> Optional[datetime]:
        """从 PBC 的 URL 中提取日期（如 .../20260708182733...）"""
        # PBC 详情页 URL 通常包含 14 位时间戳: YYYYMMDDHHMMSS
        m = re.search(r"/(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(\d{6})", url)
        if m:
            year, month, day = m.group(1), m.group(2), m.group(3)
            try:
                return datetime(int(year), int(month), int(day), tzinfo=CST)
            except ValueError:
                pass

        # 尝试其他日期格式
        m = re.search(r"/(20\d{6})/", url)  # YYYYMMDD
        if m:
            s = m.group(1)
            try:
                return datetime.strptime(s, "%Y%m%d").replace(tzinfo=CST)
            except ValueError:
                pass

        return None
