"""
arXiv — 自动驾驶感知 + 光照/天气 学术论文追踪

通过 arXiv API 按关键词搜索最新论文，追踪:
  - adverse weather autonomous driving perception
  - low light perception headlight glare
  - lighting autonomous driving benchmark

用法: 自动进入 intel 采集管道，按 config 设置关键词运行

实现: APICollector 声明式实例 —— 抓取循环/去重/过滤/limit/限速全部
由基类承担，本模块只保留关键词常量 + Atom XML 归一化 hook。
"""

import logging
import re
from typing import ClassVar

from intel.core.api import APICollector
from intel.core.registry import register

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
class ArxivPerceptionCollector(APICollector):
    source_name = "arxiv_perception"
    display_name = "arXiv (感知+光照)"
    domains: ClassVar[list] = ["self_driving"]
    PARAM_SCHEMA: ClassVar[dict] = {"max_age": {"type": "int", "min": 1, "max": 90}}

    API_SPEC: ClassVar[dict] = {
        "endpoint": ARXIV_API,
        "method": "GET",
        "params": {"sortBy": "submittedDate", "sortOrder": "descending",
                   "start": 0, "max_results": 10},
        "queries": [{"search_query": q} for q in SEARCH_QUERIES],
        "pagination": None,
        "limit": 40, "timeout": 30, "delay": 1.5,
        "domain": "学术", "sector": "perception_lighting",
        "field_map": {
            "title":    {"path": "title", "transform": "collapse_ws"},
            "url":      {"path": "id"},
            "date_obj": {"path": "published", "transform": "parse_date",
                         "fmts": ["%Y-%m-%d"], "tz": "utc"},
            "_authors": {"path": "authors", "transform": "join_names",
                         "sep": ", ", "limit": 3, "suffix": " et al."},
            "_summary": {"path": "summary", "transform": "truncate", "max": 200},
            "summary":  {"transform": "template", "template": "[{_authors}] {_summary}"},
        },
    }

    def _extract_records(self, resp, spec) -> list[dict]:
        """唯一非 JSON 覆盖：Atom XML → list[dict]。

        复用现有 re 提取逻辑，行为与改造前逐字节等价：title 折叠空白、
        published 保留原始字符串（由 field_map 的 parse_date 取前 10 字符）、
        summary 仅 strip 不折叠、authors 为 name 元素原样列表。
        """
        content = resp.text
        records = []
        for entry in re.findall(r'<entry>(.*?)</entry>', content, re.DOTALL):
            id_match = re.search(r'<id>(.*?)</id>', entry)
            url = id_match.group(1).strip() if id_match else ""

            title_match = re.search(r'<title>(.*?)</title>', entry, re.DOTALL)
            title = title_match.group(1).strip() if title_match else ""
            title = re.sub(r'\s+', ' ', title)

            date_match = re.search(r'<published>(.*?)</published>', entry)
            published = date_match.group(1).strip() if date_match else ""

            summary_match = re.search(r'<summary>(.*?)</summary>', entry, re.DOTALL)
            summary = summary_match.group(1).strip() if summary_match else ""

            authors = re.findall(r'<name>(.*?)</name>', entry)

            records.append({
                "title": title,
                "id": url,
                "published": published,
                "summary": summary,
                "authors": authors,
            })
        return records
