#!/usr/bin/env python3
"""
情报采集 — 平台适配器基类

所有数据源（arXiv、CSA、上海住建委等）继承此基类，
实现统一的采集接口。
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import List, Optional

CST = timezone(timedelta(hours=8))

logger = logging.getLogger("intel.collector")


@dataclass
class NewsItem:
    """统一的情报条目"""
    title: str
    url: str
    summary: str = ""
    date_obj: Optional[datetime] = None
    source: str = ""
    domain: str = "综合"
    sector: str = ""

    @property
    def date(self) -> str:
        return self.date_obj.strftime("%Y-%m-%d") if self.date_obj else ""

    def __hash__(self):
        import hashlib
        return hash(hashlib.md5(self.title.encode()).hexdigest())


class BaseCollector(ABC):
    """情报源采集器基类"""

    source_name: str = ""
    display_name: str = ""

    def __init__(self, max_age: int = 7):
        self.max_age = max_age
        self.cutoff = datetime.now(CST) - timedelta(days=max_age)

    @abstractmethod
    def crawl(self, sess) -> List[NewsItem]:
        """采集该源的最新情报"""
        ...

    def _is_recent(self, date_obj: Optional[datetime]) -> bool:
        """检查是否在时效范围内"""
        if date_obj is None:
            return True
        return date_obj >= self.cutoff
