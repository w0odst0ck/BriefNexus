"""
后台 worker — 任务轮询 / 心跳 / 超时 / 重试 / 每源全局锁

模型:
  - 单线程 worker 主循环(1s 轮询 pending,每次循环刷新模块级心跳)
  - ThreadPoolExecutor(max_workers=2) 实际执行采集,避免打爆反爬目标
  - 每源全局锁: 同一 source 同时只跑一个任务(多项目共享限速配额)
  - 超时 300s 强制 failed(error=network);失败自动重试 3 次,退避 5s/10s/20s
  - 取消: pending/running 可取消;running 通过线程内标志位软中止(不强制 kill)
"""
import contextlib
import hashlib
import inspect
import io
import json
import logging
import random
import sys
import threading
import time
import traceback
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from platform import config, delivery
from platform import memory_store as store

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
            return store.ERROR_RATE_LIMITED
    msg = str(exc).lower()
    for token in ("403", "429", "rate limit", "rate-limited", "too many requests",
                  "captcha", "反爬", "频繁", "风控"):
        if token in msg:
            return store.ERROR_RATE_LIMITED
    if isinstance(exc, requests.RequestException):
        return store.ERROR_NETWORK
    return store.ERROR_INTERNAL


def to_platform_item(item, source_name: str) -> dict:
    """intel NewsItem / dict → 平台落库 dict(NewsItem 泛化透传)

    - 标准字段直取; date_str 取 NewsItem.date(YYYY-MM-DD) 或 dict 的 date_str/date
    - type 默认 'news'; 采集器若自带 type/raw_data 则透传
    - 采集器附加的未声明字段塞入 raw_data(结构化负载双保险)
    - dict 形式(内联 crawl 函数契约: title/url 必填,其余可选)同语义转换
    """
    standard = {"title", "url", "summary", "source", "domain", "sector", "date_obj", "raw_data"}
    if isinstance(item, dict):
        raw = dict(item.get("raw_data") or {})
        for k, v in item.items():
            if k in standard or k in ("type", "date_str", "date"):
                continue
            raw[k] = v
        return {
            "dedup_key": store.make_dedup_key(item.get("title", "") or "",
                                             item.get("url", "") or ""),
            "title": item.get("title", "") or "",
            "url": item.get("url", "") or "",
            "summary": item.get("summary", "") or "",
            "source": item.get("source", "") or "",
            "domain": item.get("domain", "") or "",
            "sector": item.get("sector", "") or "",
            "type": item.get("type", "news") or "news",
            "date_str": item.get("date_str") or item.get("date", "") or "",
            "raw_data": _json_dumps(raw),
        }
    raw = dict(getattr(item, "raw_data", None) or {})
    for k, v in vars(item).items():
        if k in standard or k == "type":
            continue
        raw[k] = v
    return {
        "dedup_key": store.make_dedup_key(item.title, getattr(item, "url", "") or ""),
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


def _item_to_callback(row: dict) -> dict:
    """平台落库条目 → 回调内容(item_to_api 形状,raw_data 展开;与 app.item_to_api 等价)"""
    raw = row.get("raw_data")
    try:
        raw = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        raw = {}
    return {
        "title": row["title"],
        "url": row["url"],
        "summary": row["summary"],
        "source": row["source"],
        "domain": row["domain"],
        "sector": row["sector"],
        "type": row["type"],
        "date_str": row["date_str"],
        "raw_data": raw,
    }


# ---------- 内联代码执行(D16/D19/D20) ----------

class InlineCodeBlocked(Exception):
    """内联代码命中 AST 安全检查禁止项"""


class _InlineFunctionCollector:
    """函数式内联代码的最小采集器包装: 暴露 crawl(sess) 契约

    不继承 BaseCollector(避免 __init__ 签名约束),仅转发 crawl 调用。
    """

    def __init__(self, crawl_fn):
        self._crawl_fn = crawl_fn

    def crawl(self, sess):
        return self._crawl_fn(sess)


class InlineCodeRuntime:
    """内联代码执行沙箱: AST 检查 → 受限命名空间 exec → 缓存复用。

    缓存链(D19): 进程内内存 dict(hash → 命名空间)→ collector_cache 表
    (hash 已通过检查)→ 全新 AST 检查 + 编译。不持久化编译对象(pickle code
    对象复杂),内存缓存进程重启失效可接受。
    """

    # 注入命名空间的常用模块(尽力注入,缺依赖静默跳过)
    INLINE_MODULES = ("requests", "bs4", "json", "re", "time", "datetime", "logging")

    def __init__(self):
        # LRU 缓存: 容量上限 128,防长时间运行内存无限增长(性能 low 修复)
        self._mem_cache: dict = OrderedDict()
        self._mem_cache_max = 128
        self._building: dict = {}   # code_hash → 构建锁(并发同 code 只构建一次)
        self._guard = threading.Lock()

    def _cache_get(self, key: str):
        val = self._mem_cache.get(key)
        if val is not None:
            self._mem_cache.move_to_end(key)
        return val

    def _cache_put(self, key: str, val):
        self._mem_cache[key] = val
        self._mem_cache.move_to_end(key)
        while len(self._mem_cache) > self._mem_cache_max:
            self._mem_cache.popitem(last=False)

    def load(self, code: str) -> dict:
        """编译并执行内联代码,返回命名空间;同 hash 复用缓存。

        Raises:
            InlineCodeBlocked: AST 检查命中禁止项
            ValueError: 语法错误
        """
        code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
        # 锁外快速路径: 内存缓存命中直接返回
        with self._guard:
            ns = self._cache_get(code_hash)
            if ns is not None:
                return ns
        # per-hash 构建锁: 同 code 并发任务只构建一次,第二个等完成后再取缓存
        with self._guard:
            build_lock = self._building.get(code_hash)
            if build_lock is None:
                build_lock = self._building[code_hash] = threading.Lock()
        with build_lock:
            # 持构建锁期间可能已被其他线程构建完成 → 再查一次缓存
            with self._guard:
                ns = self._cache_get(code_hash)
                if ns is not None:
                    return ns
            # 锁外构建(compile/exec/DB 可能耗时,不占全局锁阻塞其他内联采集器)
            if store.get_collector_cache(code_hash) is None:
                bad = config.check_inline_code_ast(code)
                if bad:
                    raise InlineCodeBlocked(bad)
            ns = self._build_namespace()
            try:
                code_obj = compile(code, "<collector>", "exec")
            except SyntaxError as e:
                raise ValueError(f"collector.code 语法错误: {e.msg}") from e
            # 受限命名空间 exec 是任务书 D16 明确的执行方式: 已通过 AST 安全检查
            # (黑名单导入/危险调用拦截)+ ALLOW_INLINE_CODE 开关,见 config.check_inline_code_ast。
            try:
                exec(code_obj, ns)  # noqa: S102
            except Exception as e:
                # 顶层代码异常(如裸 raise)转 ValueError,调用方 422 而非 500
                raise ValueError(f"collector.code 顶层执行异常: {type(e).__name__}: {e}") from e
            # 锁内插入缓存
            with self._guard:
                self._cache_put(code_hash, ns)
            store.upsert_collector_cache(code_hash, code)
        return ns

    @staticmethod
    def _build_namespace() -> dict:
        """受限命名空间: 剥离危险 builtins,注入常用模块 + intel 基类 + stderr 写入

        安全(D20 增强): 不直接暴露 __builtins__ 全量——剥离 eval/exec/compile/open/
        __import__/input 等逃逸向量,防 getattr(__builtins__, '__import__')('os') 绕过。
        """
        # 基于真实 builtins 构造受限副本(保留常用,剥离危险)
        safe_builtins = dict(__builtins__.__dict__ if hasattr(__builtins__, "__dict__")
                             else __builtins__)
        for name in ("eval", "exec", "compile", "open", "input",
                     "breakpoint", "memoryview", "staticmethod", "classmethod"):
            safe_builtins.pop(name, None)
        # 受限 __import__: 只允许白名单模块(与 INLINE_MODULES 对齐),其余拒绝
        _allowed = set(InlineCodeRuntime.INLINE_MODULES) | {"intel"}

        def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
            top = name.split(".", 1)[0]
            if top not in _allowed:
                raise ImportError(f"collector.code 禁止导入模块: {name}")
            return __import__(name, globals, locals, fromlist, level)

        safe_builtins["__import__"] = _safe_import
        ns = {"__builtins__": safe_builtins}

        def stderr_write(msg: str):
            """向 stderr 写日志(运行时动态查 sys.stderr,配合 redirect_stderr 捕获)

            不注入 sys 模块本身(黑名单),仅提供写通道供采集器输出日志。
            """
            sys.stderr.write(str(msg))

        ns["stderr_write"] = stderr_write
        for mod_name in InlineCodeRuntime.INLINE_MODULES:
            try:
                ns[mod_name] = __import__(mod_name)
            except Exception:  # noqa: S110 — 可选依赖缺失/损坏静默跳过,不阻塞采集
                pass  # 缺依赖/依赖损坏不阻塞(如 bs4 未安装)
        try:
            from intel.core.base import BaseCollector, NewsItem
            ns.update({"BaseCollector": BaseCollector, "NewsItem": NewsItem})
        except ImportError:
            pass  # intel 不可用时类式写法将无法定义,由 resolve 阶段报错
        return ns


# 模块级单例: 提交阶段(检测 Collector/crawl)与 worker 执行共用同一缓存
_inline_runtime = InlineCodeRuntime()


def resolve_inline_collector(code: str):
    """解析内联代码中的采集器(D16/D21): 返回 (collector_cls_or_callable, source_name)

    - 契约: 先看 class Collector(BaseCollector),再看 def crawl(sess) -> list[dict]
    - 都没有 → ValueError(调用方转 422)
    - source_name: 类式取类属性(空则 code hash 前 8 位);函数式 code hash 前 8 位
    """
    ns = _inline_runtime.load(code)
    collector_cls = ns.get("Collector")
    crawl_fn = ns.get("crawl")
    code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
    if inspect.isclass(collector_cls):
        name = getattr(collector_cls, "source_name", "") or code_hash[:8]
        return collector_cls, name
    if callable(crawl_fn):
        return crawl_fn, code_hash[:8]
    raise ValueError("collector.code 必须定义 class Collector(BaseCollector) 或 def crawl(sess)")


def parse_collector_spec(raw) -> dict | None:
    """任务 collector_spec 列(JSON 字符串)→ dict;空/损坏返回 None

    公共 API: 供 app.py / 外部消费方解析任务里的源规格。
    """
    if not raw:
        return None
    try:
        spec = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return spec if isinstance(spec, dict) else None


def instantiate_class(cls, params: dict):
    """按构造函数签名过滤参数后实例化(宽松模式防 TypeError)"""
    sig = inspect.signature(cls.__init__)
    accepted = set(sig.parameters) - {"self"}
    filtered = {k: v for k, v in params.items() if k in accepted}
    return cls(**filtered)


class Scheduler:
    """后台任务调度器 — start() 后单线程轮询 + 线程池执行"""

    def __init__(self, max_workers: int = 2, task_timeout_s: int = 300,
                 poll_interval: float = 1.0, retry_delays=None,
                 callback_timeout_s: int = 10, callback_retry_delays=None):
        self.max_workers = max_workers
        self.task_timeout_s = task_timeout_s
        self.poll_interval = poll_interval
        self.retry_delays = tuple(retry_delays or RETRY_DELAYS)
        # 回调投递参数(锁外投递,独立线程池,不占用采集 worker)
        self.callback_timeout_s = callback_timeout_s
        self.callback_retry_delays = tuple(callback_retry_delays or (2, 4, 8))

        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="platform-collect")
        self._callback_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="platform-callback")
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
        # 回调投递: 进程退出时内存数据一并清空,未完成回调无持久化意义,
        # 故 shutdown(wait=False) 直接丢弃,不阻塞 stop(避免被慢回调拖住退出)。
        self._callback_pool.shutdown(wait=False)
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
        task = store.get_task(task_id)
        if not task:
            return False
        if task["status"] not in (store.STATUS_PENDING, store.STATUS_RUNNING):
            return False
        store.update_task(task_id, status=store.STATUS_CANCELLED, finished_at=store._now())
        self.request_cancel(task_id)
        self._drop_retry(task_id)
        return True

    def retry_task(self, task_id: str) -> bool:
        """API retry: 仅 failed 可重跑;重置 pending 并清空旧结果/重试计数/堆栈/回调状态"""
        task = store.get_task(task_id)
        if not task or task["status"] != store.STATUS_FAILED:
            return False
        store.clear_items(task_id)
        # 清回调状态/交付标记: 重采后重新交付(回调语义重置)
        store.update_task(task_id, status=store.STATUS_PENDING, error=None,
                          finished_at=None, items_count=0, retries=0,
                          traceback=None, collector_log=None,
                          callback_status=None, delivered=False)
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
                self._sweep_ttl()
            except Exception as e:
                logger.error("worker 主循环异常: %s", e)
            self._stop.wait(self.poll_interval)

    # ---------- TTL 清扫(替代每日清理 D13) ----------

    def _sweep_ttl(self, now: float | None = None):
        """每轮主动回收过期任务(含其 items/dedup),释放内存。

        now 缺省取 time.time();可注入时间用于测试。返回本次删除的任务数。
        """
        try:
            swept = store.sweep_expired(now if now is not None else time.time())
            if swept:
                logger.info("TTL 清扫: %d 个过期任务", swept)
            return swept
        except Exception as e:
            logger.error("TTL 清扫失败: %s", e)
            return 0

    def _dispatch(self):
        """领取 pending 任务: 每源锁非阻塞获取 → 置 running → 交线程池执行"""
        for task in store.list_pending_tasks():
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
                if not store.claim_task(task_id):
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
                self._fail(task_id, store.ERROR_NETWORK, ignore_cancel=True)

    # ---------- 任务执行 ----------

    def _execute(self, task_id: str, lock: threading.Lock):
        deliver = None
        try:
            deliver = self._run_task(task_id)  # 返回 (callback_url, items) 或 None
        finally:
            lock.release()                     # 先放每源锁,避免回调阻塞同源后续任务
            self.forget_cancel(task_id)
            with self._mem_guard:
                self._running_at.pop(task_id, None)
        if deliver is not None:
            self._callback_pool.submit(self._deliver_callback, task_id, *deliver)

    def _run_task(self, task_id: str):
        task = store.get_task(task_id)
        if not task or task["status"] != store.STATUS_RUNNING:
            return None
        try:
            collector = self._instantiate(task)
        except Exception as e:
            logger.error("任务 %s 采集器实例化失败: %s", task_id, e)
            self._fail(task_id, classify_error(e), traceback_text=traceback.format_exc())
            return None

        items = []
        # D18: 捕获采集器 stderr 输出(collector_log),异常时一并入库
        stderr_buf = io.StringIO()
        log = None
        try:
            with contextlib.redirect_stderr(stderr_buf):
                items = collector.crawl(new_session())
            log = stderr_buf.getvalue().strip() or None
        except Exception as e:
            logger.error("任务 %s 采集失败: %s", task_id, e)
            self._fail(task_id, classify_error(e), traceback_text=traceback.format_exc(),
                       collector_log=stderr_buf.getvalue().strip() or None)
            return None

        if self._is_cancelled(task_id):
            logger.info("任务 %s 已取消,丢弃采集结果", task_id)
            return None

        # 源返回空 → source_empty(可重试)
        if not items:
            self._fail(task_id, store.ERROR_SOURCE_EMPTY)
            return None

        # 逐条落库(带去重);每条前检查取消标志
        saved = 0
        saved_items = []  # 实际落库的条目(供回调投递)
        for item in items:
            if self._is_cancelled(task_id):
                logger.info("任务 %s 执行中被取消,丢弃剩余结果", task_id)
                break
            try:
                platform_item = to_platform_item(item, task["source"])
                if store.add_item(task_id, platform_item):
                    saved += 1
                    saved_items.append(platform_item)
            except Exception as e:
                logger.error("任务 %s 落库异常: %s", task_id, e)

        if self._is_cancelled(task_id):
            return None  # 保持 cancelled 状态,不再更新
        # 成功时清空历史失败残留的 traceback;collector_log 保留本次采集的 stderr 输出
        store.update_task(task_id, status=store.STATUS_DONE, items_count=saved,
                          finished_at=store._now(), traceback=None, collector_log=log)
        logger.info("任务 %s done: %d 条", task_id, saved)

        # 回调投递: 仅当有结果且任务声明了 callback_url 时,返回 (url, items)
        if saved_items and task.get("callback_url"):
            return task["callback_url"], [_item_to_callback(p) for p in saved_items]
        return None

    def _deliver_callback(self, task_id: str, url: str, items: list):
        """锁外回调投递: 2xx 成功 → delivered + free_items;失败 → failed + clear_items"""
        payload = {"task_id": task_id, "status": "done",
                   "items_count": len(items), "items": items}
        ok, _ = delivery.post_json_with_retry(
            url, payload, timeout_s=self.callback_timeout_s,
            retry_delays=self.callback_retry_delays)
        if ok:
            store.mark_delivered(task_id)   # delivered=True, callback_status=delivered
            store.free_items(task_id)       # 交付即清
            logger.info("任务 %s 回调成功,结果已释放", task_id)
        else:
            store.mark_callback_failed(task_id)  # status=failed, error=callback_failed
            store.clear_items(task_id)           # 丢弃结果(平台零持久化)
            logger.error("任务 %s 回调失败,结果已丢弃", task_id)

    def _instantiate(self, task: dict):
        """实例化采集器: 按 collector_spec 分派(module 引用 / code 内联 / 配置内置源)

        - collector_spec 有 "code" → 内联代码(缓存编译,取 Collector 类或 crawl 函数)
        - collector_spec 有 "module" → 复用 config.resolve_collector_class 动态 import
        - 无 collector_spec(旧任务)→ 查配置源,查不到报「源未注册」
        参数: 任务 params(JSON)与配置默认 params 合并,按构造函数签名过滤防 TypeError。
        """
        spec = parse_collector_spec(task.get("collector_spec"))
        task_params = json.loads(task["params"]) if task.get("params") else {}
        if spec is not None and "code" in spec:
            return self._instantiate_inline(spec, task_params)
        if spec is not None and "module" in spec:
            cls = config.resolve_collector_class(spec["module"])
            return instantiate_class(cls, task_params)
        # 旧任务(无 collector_spec): 走配置内置源
        cfg = config.get_sources_config().get(task["source"], {})
        if not cfg or not cfg.get("enabled", True):
            raise ValueError(f"源未注册: {task['source']}，请用 collector 字段传入")
        cls = config.resolve_collector_class(cfg["module"])
        params = dict(cfg.get("params") or {})
        params.update(task_params or {})
        return instantiate_class(cls, params)

    def _instantiate_inline(self, spec: dict, params: dict):
        """内联代码采集器实例化: 类式走 BaseCollector 签名过滤;函数式包装最小采集器"""
        code = spec.get("code") or ""
        ns = _inline_runtime.load(code)
        collector_cls = ns.get("Collector")
        crawl_fn = ns.get("crawl")
        if inspect.isclass(collector_cls):
            # 校验必须是 BaseCollector 子类(或提供 crawl 方法),避免任意类被当采集器实例化
            from intel.core.base import BaseCollector
            if issubclass(collector_cls, BaseCollector) or callable(getattr(collector_cls, "crawl", None)):
                return instantiate_class(collector_cls, params)
            raise ValueError("collector.code 的 class Collector 必须继承 BaseCollector 或实现 crawl()")
        if callable(crawl_fn):
            return _InlineFunctionCollector(crawl_fn)
        raise ValueError("collector.code 必须定义 class Collector(BaseCollector) 或 def crawl(sess)")

    def _fail(self, task_id: str, error: str, ignore_cancel: bool = False,
              traceback_text: str | None = None, collector_log: str | None = None):
        """任务失败: 更新状态(含 D18 traceback/collector_log);未达重试上限则置回 pending 并安排退避

        ignore_cancel=True 用于超时场景(已设软中止标志但仍需走重试)。
        """
        task = store.get_task(task_id)
        if not task:
            return
        if not ignore_cancel and self._is_cancelled(task_id):
            return  # 已取消的任务保持 cancelled
        retries = task["retries"] or 0
        if retries < len(self.retry_delays):
            delay = self.retry_delays[retries]
            store.update_task(task_id, status=store.STATUS_PENDING, error=error,
                              finished_at=store._now(), retries=retries + 1,
                              traceback=traceback_text, collector_log=collector_log)
            with self._mem_guard:
                self._retry_after[task_id] = time.time() + delay
            logger.warning("任务 %s 失败(%s),第 %d 次重试,退避 %ds",
                           task_id, error, retries + 1, delay)
        else:
            store.update_task(task_id, status=store.STATUS_FAILED, error=error,
                              finished_at=store._now(), traceback=traceback_text,
                              collector_log=collector_log)
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
