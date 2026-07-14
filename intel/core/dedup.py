"""
持久化去重存储 — 跨天跨运行的标题 MD5 跟踪

存储: intel/output/.dedup_store.json
格式: { "md5_hex": "first_seen_date", ... }

清理: 自动清理超过 max_days 的旧记录
"""

import hashlib
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Set, List

logger = logging.getLogger("intel.dedup")

CST = timezone(timedelta(hours=8))
MAX_DAYS = 30  # 保留 30 天去重历史


class DedupStore:
    """跨天去重存储"""

    def __init__(self, store_path: str = None, max_days: int = MAX_DAYS):
        if store_path is None:
            # 默认放在 output 目录下
            base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            store_path = os.path.join(base, "output", ".dedup_store.json")
        self.store_path = store_path
        self.max_days = max_days
        self._data: dict = {}
        self._load()

    def _load(self):
        """从文件加载去重记录"""
        if os.path.exists(self.store_path):
            try:
                with open(self.store_path, "r") as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logger.warning("去重存储加载失败，重置: %s", e)
                self._data = {}

    def save(self):
        """持久化到文件"""
        os.makedirs(os.path.dirname(self.store_path), exist_ok=True)
        with open(self.store_path, "w") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    def is_seen(self, title: str) -> bool:
        """检查标题是否已被采集过"""
        key = self._md5(title)
        return key in self._data

    def mark_seen(self, title: str, date: str = None):
        """标记标题为已采集"""
        key = self._md5(title)
        if key not in self._data:
            self._data[key] = date or datetime.now(CST).strftime("%Y-%m-%d")

    def mark_seen_batch(self, titles: List[str], date: str = None):
        """批量标记"""
        today = date or datetime.now(CST).strftime("%Y-%m-%d")
        for t in titles:
            self.mark_seen(t, today)

    def filter_new(self, titles: List[str]) -> List[str]:
        """返回不在去重存储中的新标题"""
        return [t for t in titles if not self.is_seen(t)]

    def cleanup(self):
        """清理超过 max_days 的旧记录"""
        cutoff = datetime.now(CST) - timedelta(days=self.max_days)
        cutoff_str = cutoff.strftime("%Y-%m-%d")
        before = len(self._data)
        self._data = {
            k: v for k, v in self._data.items()
            if v >= cutoff_str
        }
        after = len(self._data)
        if before != after:
            logger.info("去重存储清理: %d → %d 条 (保留 %d 天)", before, after, self.max_days)

    @staticmethod
    def _md5(text: str) -> str:
        return hashlib.md5(text.encode("utf-8")).hexdigest()
