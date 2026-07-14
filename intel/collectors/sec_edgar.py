"""
SEC EDGAR 8-K — 美国证监会备案（科技巨头重大事件披露）
"""

import logging, re, time, xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from typing import List, Optional
from bs4 import BeautifulSoup
from intel.core.registry import register
from intel.core.base import BaseCollector, NewsItem, CST

logger = logging.getLogger("intel.sec")

SEC_ATOM = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&CIK=&type=8-K&company=&dateb=&owner=include&start=0&count=40&output=atom"
SEC_HEADERS = {
    "User-Agent": "BriefNexus/2.0 (your.email@example.com)",
    "Accept": "application/xml,text/xml",
    "Host": "www.sec.gov",
}

# 感兴趣的科技公司 CIK 列表（SEC 8-K 重点监控）
TECH_CIKS = {
    "0000320193": "Apple",
    "0001652044": "Alphabet (Google)",
    "0001045810": "NVIDIA",
    "0000796343": "Microsoft",
    "0001018724": "Amazon",
    "0001318605": "Tesla",
    "0000936395": "Meta (Facebook)",
    "0000724121": "Intel",
    "0001040741": "AMD",
    "0000931515": "Baidu",
    "0001408863": "Micron",
    "00012927": "HP",
    "0000315066": "Qualcomm",
    "0000736712": "Texas Instruments",
    "0000768364": "Applied Materials",
    "0001001838": "ASML",
    "0000858877": "Taiwan Semi (TSMC)",
}

# 相关性关键词
RELEVANT_ITEMS = [
    "Results of Operations", "Financial", "AI", "半导体", "Chip", "Tariff",
    "Export", "Trade", "Sanction", "Regulation", "Acquisition", "Merger",
    "Restructuring", "Dividend", "Shareholder",
]


@register("sec_edgar")
class SECEdgarCollector(BaseCollector):
    source_name = "sec_edgar"
    domains = ["finance", "semiconductor"]
    display_name = "SEC EDGAR"

    def crawl(self, sess) -> List[NewsItem]:
        items = []
        try:
            # 设置 SEC 所需的 User-Agent
            sess.headers.update(SEC_HEADERS)
            r = sess.get(SEC_ATOM, timeout=30)
            r.raise_for_status()

            root = ET.fromstring(r.content)
            ns = {"atom": "http://www.w3.org/2005/Atom",
                  "sec": "http://www.sec.gov/edgar"}

            for entry in root.findall("atom:entry", ns):
                title = entry.findtext("atom:title", "", ns).strip()
                url = entry.findtext("atom:link", "", ns).strip()
                if not url:
                    link_el = entry.find("atom:link", ns)
                    if link_el is not None:
                        url = link_el.get("href", "")

                summary = entry.findtext("atom:summary", "", ns).strip()

                date_obj = None
                pub = entry.findtext("atom:published", "", ns)
                if pub:
                    try:
                        date_obj = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                    except:
                        pass

                # 检查是否相关（科技公司 or 关键词匹配）
                cik_match = re.search(r"CIK=(\d+)", url)
                is_tech = cik_match and cik_match.group(1) in TECH_CIKS

                has_keyword = any(kw.lower() in (title + summary).lower()
                                  for kw in RELEVANT_ITEMS)

                if not is_tech and not has_keyword:
                    continue

                if not title:
                    # 尝试从 summary 提取有意义的标题
                    for item_s in ["Item 2.02", "Item 8.01", "Item 7.01", "Item 1.01"]:
                        if item_s in summary:
                            summary_start = summary.find(item_s)
                            title = summary[summary_start:summary_start + 120].strip()
                            title = title.replace("</b>", "").replace("<b>", "")
                            break

                if not title:
                    title = "8-K Filing"

                domain = "科技" if is_tech else "合规"
                item = NewsItem(title=title, url=url, summary=summary[:300],
                                source=self.display_name, domain=domain,
                                date_obj=date_obj)
                items.append(item)

        except Exception as e:
            logger.error("SEC EDGAR 采集失败: %s", e)

        return items
