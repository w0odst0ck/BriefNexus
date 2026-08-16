"""
平台内存存储 — 纯内存态任务/结果/去重/内联代码缓存(零持久化)

设计(零持久化改造):
  - 全部状态放在进程堆内存 dict/list/set,由单把 threading.RLock 保护
  - 无数据库引擎、无文件句柄、无临时文件;进程重启/崩溃即全清
  - TTL: 任务创建时写 expires_at = now + ttl_seconds,过期由「惰性(get_task/get_items)
    + 主动(worker 每轮 sweep_expired)」两层回收
  - 容量: 任务表超限抛 CapacityError(上层转 429);单任务条目超限截断并置 items_truncated
  - 去重: 仅单任务内,键 (title,url) 的 md5;跨任务去重/引用已取消
  - 交付即清: free_items / mark_consumed 全消费释放 / mark_delivered / mark_callback_failed

公共函数名/常量名与原 db.py 保持一致,app.py/scheduler.py 仅改 import 与少量语义分支。
"""
import hashlib
import json
import logging
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("platform.memory_store")

# ---------- 状态与错误分类常量 ----------
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

TERMINAL_STATUSES = (STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED)

ERROR_RATE_LIMITED = "rate_limited"   # 限流 / 反爬
ERROR_NETWORK = "network"             # 超时 / 断连
ERROR_SOURCE_EMPTY = "source_empty"   # 源无数据
ERROR_INTERNAL = "internal"           # 代码异常
ERROR_CALLBACK_FAILED = "callback_failed"  # 回调投递失败(结果丢弃,零持久化)

# add_item 返回码: True 本次新增入库 / False 同任务内重复或容量截断(跳过)
ADDED = True
DUPLICATED = False

# update_task 哨兵: 区分「不更新该字段」与「显式置 NULL/0/False」
_UNSET = object()

CST = timezone(timedelta(hours=8))  # 与 intel.core.base 同用东八区


class CapacityError(Exception):
    """内存任务表已满(上层转 HTTP 429)"""


def _now() -> str:
    """本地时间字符串(与原 db.py 一致)"""
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def make_task_id() -> str:
    """生成任务 ID: t_<毫秒时间戳>_<随机hex>"""
    return f"t_{int(time.time() * 1000)}_{secrets.token_hex(4)}"


def make_dedup_key(title: str, url: str) -> str:
    """单任务内去重键: (title, url) 的 MD5(键语义已从 (source,title) 变更)"""
    raw = f"{title}|{url}".encode()
    return hashlib.md5(raw).hexdigest()


class MemoryStore:
    """纯内存存储单例: 任务/结果/去重/内联缓存,单把 RLock 保证线程安全"""

    def __init__(self, ttl_seconds=3600, max_tasks=1000, max_items_per_task=10000,
                 free_on_full_consume=True):
        self._lock = threading.RLock()
        self.tasks: dict[str, dict] = {}          # task_id -> 任务记录
        self.items: dict[str, list] = {}          # task_id -> 结果条目列表(插入序稳定分页)
        self.dedup: dict[str, set] = {}           # task_id -> {dedup_key} 仅单任务内
        self.collector_cache: dict[str, dict] = {}  # code_hash -> {"hash","code","created_at"}
        self._next_item_id = 0                    # 全局单调递增 item 主键
        self.ttl_seconds = ttl_seconds
        self.max_tasks = max_tasks
        self.max_items_per_task = max_items_per_task
        self.free_on_full_consume = free_on_full_consume

    # ---------- tasks CRUD ----------

    def create_task(self, source: str, params: dict | None = None, domain: str | None = None,
                    callback_url: str | None = None, task_id: str | None = None,
                    collector_spec: str | None = None) -> str:
        """创建任务(status=pending),返回 task_id;任务表满抛 CapacityError"""
        task_id = task_id or make_task_id()
        with self._lock:
            if len(self.tasks) >= self.max_tasks:
                raise CapacityError(
                    f"内存任务表已满(max_tasks={self.max_tasks}),请稍后重试")
            task = {
                "id": task_id,
                "source": source,
                "params": json.dumps(params or {}, ensure_ascii=False),
                "domain": domain,
                "status": STATUS_PENDING,
                "created_at": _now(),
                "finished_at": None,
                "error": None,
                "items_count": 0,
                "retries": 0,
                "callback_url": callback_url,
                "collector_spec": collector_spec,
                "traceback": None,
                "collector_log": None,
                "archived": False,            # 保留字段,恒 False(无归档语义)
                "expires_at": time.time() + self.ttl_seconds,  # 绝对过期时间戳
                "delivered": False,           # 是否已成功交付(同步/回调)
                "callback_status": None,      # None|"pending"|"delivered"|"failed"
                "items_truncated": False,     # 单任务条目是否被截断
            }
            self.tasks[task_id] = task
            self.items[task_id] = []
            self.dedup[task_id] = set()
        return task_id

    def get_task(self, task_id: str) -> dict | None:
        """按 ID 查任务,不存在或已过期(惰性清扫)返回 None"""
        with self._lock:
            if self._purge_if_expired_locked(task_id):
                return None
            task = self.tasks.get(task_id)
            return dict(task) if task else None

    def update_task(self, task_id: str, status: str | None = None, error: object = _UNSET,
                    finished_at: object = _UNSET, items_count: object = _UNSET,
                    retries: object = _UNSET, traceback: object = _UNSET,
                    collector_log: object = _UNSET, callback_status: object = _UNSET,
                    delivered: object = _UNSET):
        """按需更新任务字段;None/False 表示显式清空,_UNSET 表示不更新"""
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                return
            if status is not None:
                task["status"] = status
            if error is not _UNSET:
                task["error"] = error
            if finished_at is not _UNSET:
                task["finished_at"] = finished_at
            if items_count is not _UNSET:
                task["items_count"] = items_count
            if retries is not _UNSET:
                task["retries"] = retries
            if traceback is not _UNSET:
                task["traceback"] = traceback
            if collector_log is not _UNSET:
                task["collector_log"] = collector_log
            if callback_status is not _UNSET:
                task["callback_status"] = callback_status
            if delivered is not _UNSET:
                task["delivered"] = delivered

    def claim_task(self, task_id: str) -> bool:
        """原子领取: 仅当仍为 pending 时置 running(防多 worker 双领)"""
        with self._lock:
            task = self.tasks.get(task_id)
            if task and task["status"] == STATUS_PENDING:
                task["status"] = STATUS_RUNNING
                return True
            return False

    def list_pending_tasks(self) -> list:
        """所有 pending 任务,按 created_at 升序(worker 轮询用)"""
        with self._lock:
            pending = [dict(t) for t in self.tasks.values()
                       if t["status"] == STATUS_PENDING]
            pending.sort(key=lambda t: t["created_at"])
            return pending

    # ---------- items CRUD ----------

    def add_item(self, task_id: str, item: dict):
        """插入一条采集结果(单任务内去重 + 容量截断)。

        返回 ADDED(True) / DUPLICATED(False)。无 dedup_key 不参与去重。
        该任务条目已达 max_items_per_task → 返回 False 并置 items_truncated(不再追加)。
        """
        with self._lock:
            if task_id not in self.tasks:
                return DUPLICATED
            dedup_key = item.get("dedup_key")
            if not dedup_key:
                if len(self.items[task_id]) >= self.max_items_per_task:
                    self.tasks[task_id]["items_truncated"] = True
                    return DUPLICATED
                self._append_item(task_id, item)
                return ADDED
            dset = self.dedup[task_id]
            if dedup_key in dset:
                return DUPLICATED
            if len(self.items[task_id]) >= self.max_items_per_task:
                self.tasks[task_id]["items_truncated"] = True
                return DUPLICATED
            dset.add(dedup_key)
            self._append_item(task_id, item)
            return ADDED

    def _append_item(self, task_id: str, item: dict):
        """追加单条结果(已持有锁);id 全局单调递增,插入序稳定分页"""
        self._next_item_id += 1
        self.items[task_id].append({
            "id": self._next_item_id,
            "task_id": task_id,
            "dedup_key": item.get("dedup_key"),
            "title": item.get("title"),
            "url": item.get("url"),
            "summary": item.get("summary") or "",
            "source": item.get("source") or "",
            "domain": item.get("domain") or "",
            "sector": item.get("sector") or "",
            "type": item.get("type") or "news",
            "date_str": item.get("date_str") or "",
            "raw_data": item.get("raw_data") or "{}",
            "consumed": False,
            "created_at": _now(),
        })

    def get_items(self, task_id: str, offset: int = 0, limit: int = 50,
                  include_consumed: bool = False) -> list:
        """分页拉取任务结果(默认只拉未消费,插入序稳定分页);过期任务返回空列表"""
        with self._lock:
            if self._purge_if_expired_locked(task_id):
                return []
            rows = self.items.get(task_id, [])
            if not include_consumed:
                rows = [r for r in rows if not r["consumed"]]
            return list(rows[offset:offset + limit])

    def count_items(self, task_id: str, unconsumed_only: bool = True) -> int:
        """统计任务结果数(unconsumed_only=True 时只统计未消费)"""
        with self._lock:
            if self._purge_if_expired_locked(task_id):
                return 0
            rows = self.items.get(task_id, [])
            if unconsumed_only:
                return sum(1 for r in rows if not r["consumed"])
            return len(rows)

    def mark_consumed(self, task_id: str, item_ids: list):
        """标记条目已消费;全消费且 free_on_full_consume 时立即释放 items"""
        if not item_ids:
            return
        with self._lock:
            rows = self.items.get(task_id)
            if not rows:
                return
            id_set = {int(i) for i in item_ids}
            for row in rows:
                if row["id"] in id_set:
                    row["consumed"] = True
            if self.free_on_full_consume and rows and all(r["consumed"] for r in rows):
                self._clear_items_locked(task_id)

    def clear_items(self, task_id: str):
        """清空任务结果与去重集(手动 retry 重跑时旧结果作废)"""
        with self._lock:
            self._clear_items_locked(task_id)

    def free_items(self, task_id: str):
        """交付即清: 释放任务结果与去重集(任务元数据保留至 TTL)"""
        with self._lock:
            self._clear_items_locked(task_id)

    def _clear_items_locked(self, task_id: str):
        self.items.pop(task_id, None)
        self.dedup.pop(task_id, None)

    # ---------- 交付状态 ----------

    def mark_delivered(self, task_id: str):
        """回调成功: delivered=True, callback_status=delivered(随后 free_items)"""
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                return
            task["delivered"] = True
            task["callback_status"] = "delivered"

    def mark_callback_failed(self, task_id: str):
        """回调重试耗尽: status=failed, error=callback_failed, callback_status=failed(结果丢弃)"""
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                return
            task["status"] = STATUS_FAILED
            task["error"] = ERROR_CALLBACK_FAILED
            task["callback_status"] = "failed"
            task["delivered"] = False
            task["finished_at"] = _now()

    # ---------- TTL 回收 ----------

    def sweep_expired(self, now: float) -> int:
        """主动回收: 删除 expires_at <= now 的任务(含其 items/dedup),返回删除数"""
        with self._lock:
            expired = [tid for tid, t in self.tasks.items() if now >= t["expires_at"]]
            for tid in expired:
                self._purge_task_locked(tid)
            return len(expired)

    def _purge_if_expired_locked(self, task_id: str) -> bool:
        """惰性回收: 命中任务且已过期 → purge 并返回 True"""
        task = self.tasks.get(task_id)
        if task and time.time() >= task["expires_at"]:
            self._purge_task_locked(task_id)
            return True
        return False

    def _purge_task_locked(self, task_id: str):
        self.tasks.pop(task_id, None)
        self.items.pop(task_id, None)
        self.dedup.pop(task_id, None)

    # ---------- 内联代码缓存 ----------

    def upsert_collector_cache(self, code_hash: str, code: str):
        """记录某 code 已通过安全检查(同 hash 覆盖刷新)"""
        with self._lock:
            self.collector_cache[code_hash] = {
                "hash": code_hash, "code": code, "created_at": _now()}

    def get_collector_cache(self, code_hash: str) -> dict | None:
        """按 hash 查缓存记录(存在说明该 code 已通过安全检查)"""
        with self._lock:
            rec = self.collector_cache.get(code_hash)
            return dict(rec) if rec else None

    # ---------- 活跃源统计 ----------

    def get_source_activity(self, days: int = 30) -> list:
        """最近 days 天有任务记录的源聚合统计(活跃源发现)

        Returns:
            [{source, last_used, success_count, collector_spec(样例)}]
        """
        cutoff = (datetime.now(CST) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            agg: dict[str, dict] = {}
            for t in self.tasks.values():
                if t["created_at"] < cutoff:
                    continue
                a = agg.get(t["source"])
                if a is None:
                    a = agg[t["source"]] = {
                        "source": t["source"], "last_used": t["created_at"],
                        "success_count": 0, "collector_spec": None}
                a["last_used"] = max(a["last_used"], t["created_at"])
                if t["status"] == STATUS_DONE:
                    a["success_count"] += 1
                spec = t.get("collector_spec")
                if spec and (a["collector_spec"] is None or spec > a["collector_spec"]):
                    a["collector_spec"] = spec
            return list(agg.values())

    # ---------- 健康 / 观测 ----------

    def ping(self) -> bool:
        """健康检查: 内存态恒健康"""
        return True

    def stats(self) -> dict:
        """运维观测: 任务/条目计数与内存占用提示"""
        with self._lock:
            task_count = len(self.tasks)
            item_count = sum(len(v) for v in self.items.values())
            return {
                "task_count": task_count,
                "item_count": item_count,
                "memory_hint": f"{task_count} tasks / {item_count} items in memory",
            }


# ---------- 模块级单例与委托函数(同名同签名兼容原 db.py) ----------

_store: MemoryStore | None = None


def _require() -> MemoryStore:
    if _store is None:
        raise RuntimeError("内存存储未初始化,请先调用 init_store()")
    return _store


def init_store(ttl_seconds: int = 3600, max_tasks: int = 1000,
               max_items_per_task: int = 10000, free_on_full_consume: bool = True):
    """初始化内存存储(重置单例为空;启动清零 + 测试隔离)"""
    global _store
    _store = MemoryStore(ttl_seconds=ttl_seconds, max_tasks=max_tasks,
                         max_items_per_task=max_items_per_task,
                         free_on_full_consume=free_on_full_consume)


def reset_store():
    """清空全局单例(测试隔离用)"""
    global _store
    _store = None


def create_task(source, params=None, domain=None, callback_url=None,
                task_id=None, collector_spec=None):
    return _require().create_task(source, params, domain, callback_url, task_id,
                                  collector_spec)


def get_task(task_id):
    return _require().get_task(task_id)


def update_task(task_id, status=None, error=_UNSET, finished_at=_UNSET,
                items_count=_UNSET, retries=_UNSET, traceback=_UNSET,
                collector_log=_UNSET, callback_status=_UNSET, delivered=_UNSET):
    return _require().update_task(task_id, status, error, finished_at, items_count,
                                  retries, traceback, collector_log, callback_status,
                                  delivered)


def claim_task(task_id):
    return _require().claim_task(task_id)


def list_pending_tasks():
    return _require().list_pending_tasks()


def add_item(task_id, item):
    return _require().add_item(task_id, item)


def get_items(task_id, offset=0, limit=50, include_consumed=False):
    return _require().get_items(task_id, offset, limit, include_consumed)


def count_items(task_id, unconsumed_only=True):
    return _require().count_items(task_id, unconsumed_only)


def mark_consumed(task_id, item_ids):
    return _require().mark_consumed(task_id, item_ids)


def clear_items(task_id):
    return _require().clear_items(task_id)


def free_items(task_id):
    return _require().free_items(task_id)


def mark_delivered(task_id):
    return _require().mark_delivered(task_id)


def mark_callback_failed(task_id):
    return _require().mark_callback_failed(task_id)


def sweep_expired(now):
    return _require().sweep_expired(now)


def upsert_collector_cache(code_hash, code):
    return _require().upsert_collector_cache(code_hash, code)


def get_collector_cache(code_hash):
    return _require().get_collector_cache(code_hash)


def get_source_activity(days=30):
    return _require().get_source_activity(days)


def ping():
    return _require().ping()


def stats():
    return _require().stats()
