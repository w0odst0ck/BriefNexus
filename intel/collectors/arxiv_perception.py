"""
arXiv — 自动驾驶感知 + 光照/天气 学术论文追踪

通过 arXiv API 按关键词搜索最新论文，追踪:
  - adverse weather autonomous driving perception
  - low light perception headlight glare
  - lighting autonomous driving benchmark

用法: 自动进入 intel 采集管道，按 config 设置关键词运行
"""

import logging, re, time, json
from datetime import datetime, timezone, timedelta
from typing import List, Optional
from intel.core.registry import register
from intel.core.base import BaseCollector, NewsItem, CST

logger = logging.getLogger("intel.arxiv_perception")

ARXIV_API = "https://export.arxiv.org/api/query"

# 追踪的关键词组合（按优先级排序）
SEARCH_QUERIES = [
    # 核心：眩光/光照影响感知
    'all:"autonomous driving" AND all:"glare"',
    'all:"autonomous driving" AND all:"headlight"',
    'all:"autonomous driving" AND all:"low light" AND all:"perception"',
    # 恶劣天气感知
    'all:"autonomous driving" AND all:"adverse weather" AND all:"perception"',
    'all:"autonomous driving" AND all:"fog" AND all:"detection"',
    'all:"autonomous driving" AND all:"night" AND all:"dataset"',
    # 传感器退化
    'all:"camera" AND all:"LiDAR" AND all:"adverse weather" AND all:"autonomous"',
]


@register("arxiv_perception")
class ArxivPerceptionCollector(BaseCollector):
    source_name = "arxiv_perception"
    domains = ["self_driving"]
    display_name = "arXiv (感知+光照)"

    def crawl(self, sess) -> List[NewsItem]:
        items = []
        seen_ids = set()

        for query in SEARCH_QUERIES:
            if len(items) >= 40:
                break  # 限制单次采集量

            params = {
                "search_query": query,
                "start": 0,
                "max_results": 10,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            }

            try:
                r = sess.get(ARXIV_API, params=params, timeout=30)
                r.raise_for_status()

                # 解析 Atom XML
                content = r.text
                entries = re.findall(r'<entry>(.*?)</entry>', content, re.DOTALL)

                for entry in entries:
                    # 提取 arXiv ID
                    id_match = re.search(r'<id>(.*?)</id>', entry)
                    url = id_match.group(1).strip() if id_match else ""

                    # 提取 title
                    title_match = re.search(r'<title>(.*?)</title>', entry, re.DOTALL)
                    title = title_match.group(1).strip() if title_match else ""
                    title = re.sub(r'\s+', ' ', title)

                    # 提取 published date
                    date_match = re.search(r'<published>(.*?)</published>', entry)
                    date_obj = None
                    if date_match:
                        try:
                            date_str = date_match.group(1).strip()[:10]
                            date_obj = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                        except:
                            pass

                    # 提取摘要
                    summary_match = re.search(r'<summary>(.*?)</summary>', entry, re.DOTALL)
                    summary = summary_match.group(1).strip()[:400] if summary_match else ""

                    # 提取作者/机构
                    authors = re.findall(r'<name>(.*?)</name>', entry)
                    author_str = ", ".join(authors[:3])
                    if len(authors) > 3:
                        author_str += f" et al."

                    dedup_key = url or title[:30]
                    if dedup_key in seen_ids:
                        continue
                    seen_ids.add(dedup_key)

                    if not title or not url:
                        continue

                    # 过时论文跳过
                    if date_obj and not self._is_recent(date_obj):
                        continue

                    item = NewsItem(
                        title=title,
                        url=url,
                        summary=f"[{author_str}] {summary[:200]}",
                        source=self.display_name,
                        domain="学术",
                        date_obj=date_obj,
                        sector="perception_lighting",
                    )
                    items.append(item)

            except Exception as e:
                logger.warning("arXiv 查询 '%s...' 失败: %s", query[:30], e)
                continue

            time.sleep(3)  # arXiv API 限速: 几秒一次

        logger.info("arXiv 新论文: %d 条", len(items))
        return items
