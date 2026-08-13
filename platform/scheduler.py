"""
后台 worker — 任务轮询 / 心跳 / 超时 / 重试 / 每源全局锁

模型:
  - 单线程 worker 主循环(1s 轮询 pending,每次循环刷新模块级心跳)
  - ThreadPoolExecutor(max_workers=2) 实际执行采集,避免打爆反爬目标
  - 每源全局锁: 同一 source 同时只跑一个任务(多项目共享限速配额)
  - 超时 300s 强制 failed(error=network);失败自动重试 3 次,退避 5s/10s/20s
  - 取消: pending/running 可取消;running 通过线程内标志位软中止(不强制 kill)
"""
import inspect
import json
import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from platform import config, db

import requests

logger = logging.getLogger("platform.scheduler")

# 模块级心跳 — /healthz 读取,worker 每次主循环刷新
heartbeat = 0.0

# 失败自动重试退避(秒): 第 1/2/3 次失败后等待时长
RETRY_DELAYS = (5, 10, 20)
MAX_RETRIES = len(RETRY_DELAYS)  # 3 次自动重试

# 采集会话 UA 轮换(与 intel/cli.py 同风格)
UA = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/128.0.0.0 Safari/537.36",
]


def get_heartbeat() -> float:
    """读取 worker 心跳时间戳"""
    return heartbeat


def is_worker_alive(max_age_s: int = 60) -> bool:
    """心跳超时判定: now - heartbeat > max_age_s → worker 疑似死亡"""
    return heartbeat > 0 and (time.time() - heartbeat) <= max_age_s


def new_session() -> requests.Session:
    """构造采集用 Session(UA 轮换)"""
    sess = requests.Session()
    sess.headers["User-Agent"] = random.choice(UA)
    sess.headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    return sess


def classify_error(exc: Exception) -> str:
    """异常 → 错误分类: rate_limited | network | internal"""
    if isinstance(exc, requests.HTTPError):
        code = exc.response.status_code if exc.response is not None else None
        if code in (403, 429):
            return db.ERROR_RATE_LIMITED
    msg = str(exc).lower()
    for token in ("403", "429", "rate limit", "rate-limited", "too many requests",
                  "captcha", "反爬", "频繁", "风控"):
        if token in msg:
            return db.ERROR_RATE_LIMITED
    if isinstance(exc, requests.RequestException):
        return db.ERROR_NETWORK
    return db.ERROR_INTERNAL


def to_platform_item(item, source_name: str) -> dict:
    """intel NewsItem → 平台落库 dict(NewsItem 泛化透传)

    - 标准字段直取; date_str 取 NewsItem.date(YYYY-MM-DD)
    - type 默认 'news'; 采集器若自带 type/raw_data 则透传
    - 采集器附加的未声明字段塞入 raw_data(结构化负载双保险)
    """
    standard = {"title", "url", "summary", "source", "domain", "sector", "date_obj", "raw_data"}
    raw = dict(getattr(item, "raw_data", None) or {})
    for k, v in vars(item).items():
        if k in standard or k == "type":
            continue
        raw[k] = v
    return {
        "dedup_key": db.make_dedup_key(source_name, item.title),
        "title": item.title,
        "url": getattr(item, "url", "") or "",
        "summary": getattr(item, "summary", "") or "",
        "source": getattr(item, "source", "") or "",
        "domain": getattr(item, "domain", "") or "",
        "sector": getattr(item, "sector", "") or "",
        "type": getattr(item, "type", "news") or "news",
        "date_str": getattr(item, "date", "") or "",
        "raw_data": _json_dumps(raw),
    }


def _json_dumps(obj) -> str:
    import json
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        return "{}"


class Scheduler:
    """后台任务调度器 — start() 后单线程轮询 + 线程池执行"""

    def __init__(self, max_workers: int = 2, task_timeout_s: int = 300,
                 poll_interval: float = 1.0, retry_delays=None,
                 cleanup_consumed_ttl_days: int = 90, cleanup_task_archive_days: int = 30):
        self.max_workers = max_workers
        self.task_timeout_s = task_timeout_s
        self.poll_interval = poll_interval
        self.retry_delays = tuple(retry_delays or RETRY_DELAYS)
        # 每日清理(D13)参数与上次清理日期(重启后当天不再重复执行)
        self.cleanup_consumed_ttl_days = cleanup_consumed_ttl_days
        self.cleanup_task_archive_days = cleanup_task_archive_days
        self._last_cleanup_date = None

        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="platform-collect")
        self._stop = threading.Event()
        self._thread = None

        self._source_locks: dict = {}       # source → threading.Lock
        self._locks_guard = threading.Lock()
        self._cancel_flags: dict = {}       # task_id → True(软中止信号)
        self._cancel_guard = threading.Lock()
        self._running_at: dict = {}         # task_id → 开始时间戳(超时判定)
        self._retry_after: dict = {}        # task_id → 退避截止时间戳
        self._mem_guard = threading.Lock()

    # ---------- 生命周期 ----------

    def start(self):
        """启动 worker 线程(幂等)"""
        global heartbeat
        if self._thread and self._thread.is_alive():
            return
        heartbeat = time.time()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="platform-worker", daemon=True)
        self._thread.start()
        logger.info("调度器已启动: max_workers=%d task_timeout=%ds", self.max_workers, self.task_timeout_s)

    def stop(self, timeout: float = 3.0):
        """停止 worker 主循环并等待线程池排空"""
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._pool.shutdown(wait=False)
        logger.info("调度器已停止")

    # ---------- 取消 / 重试 对外接口(由 API 层调用) ----------

    def request_cancel(self, task_id: str):
        """设置取消标志(软中止 running 任务)"""
        with self._cancel_guard:
            self._cancel_flags[task_id] = True

    def forget_cancel(self, task_id: str):
        with self._cancel_guard:
            self._cancel_flags.pop(task_id, None)

    def cancel_task(self, task_id: str) -> bool:
        """API cancel: pending/running → cancelled;返回是否取消成功"""
        task = db.get_task(task_id)
        if not task:
            return False
        if task["status"] not in (db.STATUS_PENDING, db.STATUS_RUNNING):
            return False
        db.update_task(task_id, status=db.STATUS_CANCELLED, finished_at=db._now())
        self.request_cancel(task_id)
        self._drop_retry(task_id)
        return True

    def retry_task(self, task_id: str) -> bool:
        """API retry: 仅 failed 可重跑;重置 pending 并清空旧结果/重试计数"""
        task = db.get_task(task_id)
        if not task or task["status"] != db.STATUS_FAILED:
            return False
        db.clear_items(task_id)
        db.update_task(task_id, status=db.STATUS_PENDING, error=None,
                       finished_at=None, items_count=0, retries=0)
        self._drop_retry(task_id)
        return True

    # ---------- 主循环 ----------

    def _run(self):
        global heartbeat
        while not self._stop.is_set():
            heartbeat = time.time()
            try:
                self._enforce_timeouts()
                self._dispatch()
                self._maybe_cleanup()
            except Exception as e:
                logger.error("worker 主循环异常: %s", e)
            self._stop.wait(self.poll_interval)

    # ---------- 每日清理(D13) ----------

    def _maybe_cleanup(self, today: datetime | None = None) -> dict | None:
        """每日执行一次数据清理: 已消费过期 items 删除 + 终态超期任务归档。

        last_cleanup_date 记录执行日期,重启后当天不再重复执行(任务书要求)。
        无论成败都记录日期,失败次日再试(避免主循环每秒重试刷错误日志)。
        Returns:
            本次实际执行返回 {"items_deleted", "tasks_archived"};当天已执行返回 None。
        """
        today = today or datetime.now(db.CST).date()
        if today == self._last_cleanup_date:
            return None
        self._last_cleanup_date = today  # 先记录,防主循环高频重试
        try:
            stats = db.cleanup(self.cleanup_consumed_ttl_days, self.cleanup_task_archive_days)
            if stats["items_deleted"] or stats["tasks_archived"]:
                logger.info("每日清理完成: %s", stats)
            return stats
        except Exception as e:
            logger.error("每日清理失败: %s", e)
            return None

    def _dispatch(self):
        """领取 pending 任务: 每源锁非阻塞获取 → 置 running → 交线程池执行"""
        for task in db.list_pending_tasks():
            if self._stop.is_set():
                return
            task_id = task["id"]
            # 退避中(自动重试等待期)的任务跳过
            if self._in_retry_wait(task_id):
                continue
            lock = self._get_source_lock(task["source"])
            if not lock.acquire(blocking=False):
                continue  # 同源任务在跑 → 跳过,下轮再试
            try:
                # 原子领取: 仅当仍为 pending 才置 running(防多 worker 双领)
                if not db.claim_task(task_id):
                    lock.release()
                    continue
                with self._mem_guard:
                    self._running_at[task_id] = time.time()
                self._pool.submit(self._execute, task_id, lock)
            except Exception:
                lock.release()

    def _enforce_timeouts(self):
        """扫描 running 任务,超时(task_timeout_s)→ 按 network 失败处理并软中止执行线程"""
        with self._mem_guard:
            running = list(self._running_at.items())
        now = time.time()
        for task_id, started in running:
            if now - started > self.task_timeout_s:
                with self._mem_guard:
                    self._running_at.pop(task_id, None)  # 防止每轮重复触发超时
                logger.warning("任务 %s 超时(>%ds),强制 failed(error=network)",
                               task_id, self.task_timeout_s)
                self.request_cancel(task_id)  # 通知执行线程尽快退出(软中止)
                self._fail(task_id, db.ERROR_NETWORK, ignore_cancel=True)

    # ---------- 任务执行 ----------

    def _execute(self, task_id: str, lock: threading.Lock):
        try:
            self._run_task(task_id)
        finally:
            lock.release()
            self.forget_cancel(task_id)
            with self._mem_guard:
                self._running_at.pop(task_id, None)

    def _run_task(self, task_id: str):
        task = db.get_task(task_id)
        if not task or task["status"] != db.STATUS_RUNNING:
            return
        try:
            collector = self._instantiate(task)
        except Exception as e:
            logger.error("任务 %s 采集器实例化失败: %s", task_id, e)
            self._fail(task_id, classify_error(e))
            return

        items = []
        try:
            items = collector.crawl(new_session())
        except Exception as e:
            logger.error("任务 %s 采集失败: %s", task_id, e)
            self._fail(task_id, classify_error(e))
            return

        if self._is_cancelled(task_id):
            logger.info("任务 %s 已取消,丢弃采集结果", task_id)
            return

        # 源返回空 → source_empty(可重试)
        if not items:
            self._fail(task_id, db.ERROR_SOURCE_EMPTY)
            return

        # 逐条落库(带去重);每条前检查取消标志
        saved = 0
        for item in items:
            if self._is_cancelled(task_id):
                logger.info("任务 %s 执行中被取消,丢弃剩余结果", task_id)
                break
            try:
                if db.add_item(task_id, to_platform_item(item, task["source"])):
                    saved += 1
            except Exception as e:
                logger.error("任务 %s 落库异常: %s", task_id, e)

        if self._is_cancelled(task_id):
            return  # 保持 cancelled 状态,不再更新
        db.update_task(task_id, status=db.STATUS_DONE, items_count=saved, finished_at=db._now())
        logger.info("任务 %s done: %d 条", task_id, saved)

    def _instantiate(self, task: dict):
        """实例化采集器: 配置默认 params 与任务 params 合并后传给构造函数

        宽松模式可能传入构造函数不认识的参数,按签名过滤避免 TypeError。
        """
        cfg = config.get_sources_config().get(task["source"], {})
        cls = config.resolve_collector_class(cfg["module"])
        params = dict(cfg.get("params") or {})
        task_params = json.loads(task["params"]) if task["params"] else {}
        params.update(task_params or {})
        sig = inspect.signature(cls.__init__)
        accepted = set(sig.parameters) - {"self"}
        filtered = {k: v for k, v in params.items() if k in accepted}
        return cls(**filtered)

    def _fail(self, task_id: str, error: str, ignore_cancel: bool = False):
        """任务失败: 更新状态;未达重试上限则置回 pending 并安排退避

        ignore_cancel=True 用于超时场景(已设软中止标志但仍需走重试)。
        """
        task = db.get_task(task_id)
        if not task:
            return
        if not ignore_cancel and self._is_cancelled(task_id):
            return  # 已取消的任务保持 cancelled
        retries = task["retries"] or 0
        if retries < len(self.retry_delays):
            delay = self.retry_delays[retries]
            db.update_task(task_id, status=db.STATUS_PENDING, error=error,
                           finished_at=db._now(), retries=retries + 1)
            with self._mem_guard:
                self._retry_after[task_id] = time.time() + delay
            logger.warning("任务 %s 失败(%s),第 %d 次重试,退避 %ds",
                           task_id, error, retries + 1, delay)
        else:
            db.update_task(task_id, status=db.STATUS_FAILED, error=error, finished_at=db._now())
            logger.error("任务 %s 失败(%s),重试耗尽,终态 failed", task_id, error)

    # ---------- 内部辅助 ----------

    def _get_source_lock(self, source: str) -> threading.Lock:
        with self._locks_guard:
            if source not in self._source_locks:
                self._source_locks[source] = threading.Lock()
            return self._source_locks[source]

    def _is_cancelled(self, task_id: str) -> bool:
        with self._cancel_guard:
            return self._cancel_flags.get(task_id, False)

    def _in_retry_wait(self, task_id: str) -> bool:
        with self._mem_guard:
            deadline = self._retry_after.get(task_id)
        return deadline is not None and time.time() < deadline

    def _drop_retry(self, task_id: str):
        """取消/手动重试时清除退避记录"""
        with self._mem_guard:
            self._retry_after.pop(task_id, None)
