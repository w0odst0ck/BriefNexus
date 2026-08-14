"""调度层测试 — 任务生命周期 / 心跳 / 重试 / 超时 / 每源锁 / 并发写 / 错误分类 / TTL / 容量"""
import threading
import time
from platform import memory_store as store
from platform.scheduler import Scheduler, classify_error, to_platform_item
from platform.tests import fake_collectors
from platform.tests.conftest import wait_task_status

import requests

# ---------- 错误分类 ----------

def test_error_classification():
    # 429 → rate_limited
    resp = requests.Response()
    resp.status_code = 429
    assert classify_error(requests.HTTPError("429", response=resp)) == store.ERROR_RATE_LIMITED
    # 403 → rate_limited(反爬)
    resp403 = requests.Response()
    resp403.status_code = 403
    assert classify_error(requests.HTTPError("403", response=resp403)) == store.ERROR_RATE_LIMITED
    # 超时/断连 → network
    assert classify_error(requests.Timeout("timeout")) == store.ERROR_NETWORK
    assert classify_error(requests.ConnectionError("conn")) == store.ERROR_NETWORK
    # 其他 HTTP 错误(5xx)→ network
    resp500 = requests.Response()
    resp500.status_code = 500
    assert classify_error(requests.HTTPError("500", response=resp500)) == store.ERROR_NETWORK
    # 代码异常 → internal
    assert classify_error(ValueError("boom")) == store.ERROR_INTERNAL


# ---------- 去重键 ----------

def test_make_dedup_key_title_url():
    """去重键 (title,url): 同标题同 url → 同键;任一不同 → 不同键"""
    k1 = store.make_dedup_key("标题A", "https://e/1")
    assert k1 == store.make_dedup_key("标题A", "https://e/1")      # 同标题同 url → 同键
    assert k1 != store.make_dedup_key("标题A", "https://e/2")      # 同标题不同 url → 不同键
    assert k1 != store.make_dedup_key("标题B", "https://e/1")      # 不同标题同 url → 不同键
    assert len(k1) == 32  # MD5 hex


# ---------- 内存态并发写 ----------

def test_memory_concurrent_inserts():
    """多线程并发 add_item 无竞争异常,计数正确(RLock 保护)"""
    store.init_store()
    task_id = store.create_task("ok", {"max_age": 7})
    errors = []

    def writer(n):
        try:
            for i in range(50):
                store.add_item(task_id, {
                    "dedup_key": f"k{n}_{i}", "title": f"标题{n}_{i}",
                    "url": f"https://example.com/{n}/{i}", "raw_data": "{}",
                })
        except Exception as e:  # 并发写不应抛异常
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert store.count_items(task_id, unconsumed_only=False) == 8 * 50


def test_single_task_dedup():
    """单任务内去重: 同 (title,url) 重复 → False;不同键 → True;无 dedup_key 不参与去重"""
    store.init_store()
    t1 = store.create_task("ok")
    item = {"dedup_key": store.make_dedup_key("同标题", "https://e/x"),
            "title": "同标题", "url": "https://e/x", "raw_data": "{}"}
    assert store.add_item(t1, item) is True
    # 同任务内重复 → False,不计数
    assert store.add_item(t1, item) is False
    assert store.count_items(t1, unconsumed_only=False) == 1
    # 无 dedup_key 的条目不参与去重,直接入库
    assert store.add_item(t1, {"title": "无键", "url": "https://e/n"}) is True
    assert store.count_items(t1, unconsumed_only=False) == 2


# ---------- TTL 回收 ----------

def test_ttl_sweep_expired():
    """TTL-1 主动清扫: sweep_expired 删除过期任务 → get_task None"""
    store.init_store(ttl_seconds=1)
    tid = store.create_task("ok")
    store.add_item(tid, {"dedup_key": "k", "title": "t", "url": "u", "raw_data": "{}"})
    assert store.get_task(tid) is not None
    swept = store.sweep_expired(time.time() + 10)  # 注入未来时间触发清扫
    assert swept == 1
    assert store.get_task(tid) is None
    assert store.get_items(tid) == []


def test_ttl_lazy_expiry():
    """TTL-2 惰性过期: 不主动清扫,get_task(过期任务)内部 purge 后 None"""
    store.init_store(ttl_seconds=1)
    tid = store.create_task("ok")
    assert store.get_task(tid) is not None
    store._store.tasks[tid]["expires_at"] = time.time() - 1  # 模拟过期
    assert store.get_task(tid) is None  # 惰性 purge
    assert store.get_task(tid) is None  # 已删除


# ---------- 容量上限 ----------

def test_item_truncation():
    """CAP-2 单任务条目截断: max_items_per_task=2,第 3 条截断 + items_truncated"""
    store.init_store(max_items_per_task=2)
    tid = store.create_task("ok")
    assert store.add_item(tid, {"dedup_key": "a", "title": "1", "url": "u1", "raw_data": "{}"}) is True
    assert store.add_item(tid, {"dedup_key": "b", "title": "2", "url": "u2", "raw_data": "{}"}) is True
    assert store.add_item(tid, {"dedup_key": "c", "title": "3", "url": "u3", "raw_data": "{}"}) is False
    assert store.count_items(tid, unconsumed_only=False) == 2
    assert store.get_task(tid)["items_truncated"] is True


# ---------- 并发安全 ----------

def test_memory_concurrent_operations():
    """CONC-1 并发安全: 多线程 create_task/add_item/get_items 混合 → 无异常,计数与去重一致"""
    store.init_store()
    errors = []

    def worker(n):
        try:
            for i in range(20):
                tid = store.create_task("ok")
                item = {"dedup_key": f"k{n}_{i}", "title": f"t{n}_{i}",
                        "url": f"https://e/{n}/{i}", "raw_data": "{}"}
                store.add_item(tid, item)
                store.add_item(tid, item)  # 同任务重复 → 去重跳过
                store.get_items(tid)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    s = store.stats()
    assert s["task_count"] == 8 * 20        # 每线程 20 任务
    assert s["item_count"] == 8 * 20        # 每任务 1 条(重复被去重)


# ---------- 零落盘 ----------

def test_no_disk_persistence(tmp_path, monkeypatch):
    """PRIV-1 零落盘: 全程运行后工作目录无 *.db/-wal/-shm 新文件"""
    monkeypatch.chdir(tmp_path)
    store.init_store()
    tid = store.create_task("ok")
    store.add_item(tid, {"dedup_key": "k", "title": "t", "url": "u", "raw_data": "{}"})
    store.mark_consumed(tid, [1])
    store.free_items(tid)
    store.sweep_expired(time.time() + 999999)
    leftovers = (list(tmp_path.rglob("*.db")) + list(tmp_path.rglob("*-wal"))
                 + list(tmp_path.rglob("*-shm")))
    assert leftovers == []


# ---------- 配置合并(config.d / 外部采集器,D12/D12b) ----------

def test_config_d_deep_merge(tmp_path):
    """config.d 片段深合并: sources 合并/覆盖;server/storage 以主配置为准"""
    import platform.config as pconfig
    main = tmp_path / "platform_config.yaml"
    main.write_text("""
server:
  port: 9000
sources:
  alpha:
    enabled: true
    module: mod.alpha:ACollector
    params: {max_age: 7}
storage:
  ttl_seconds: 60
callback:
  timeout_s: 10
  retry_delays: [2, 4, 8]
""", encoding="utf-8")
    (tmp_path / "config.d").mkdir()
    (tmp_path / "config.d" / "10_extra.yaml").write_text("""
sources:
  beta:
    enabled: true
    module: mod.beta:BCollector
    params: {max_age: 30}
  alpha:
    enabled: false
server:
  port: 9999
""", encoding="utf-8")
    saved = pconfig._config
    try:
        cfg = pconfig.load_config(str(main))
        assert cfg["sources"]["beta"]["module"] == "mod.beta:BCollector"  # 片段新增源
        assert cfg["sources"]["alpha"]["enabled"] is False                # 片段覆盖已有源
        assert cfg["server"]["port"] == 9000                              # server 以主配置为准
        assert cfg["storage"]["ttl_seconds"] == 60                        # storage 以主配置为准
    finally:
        pconfig._config = saved


def test_config_d_missing_dir_noop(tmp_path):
    """无 config.d 目录时行为不变"""
    import platform.config as pconfig
    main = tmp_path / "platform_config.yaml"
    main.write_text("server:\n  port: 9000\nsources: {}\n", encoding="utf-8")
    saved = pconfig._config
    try:
        cfg = pconfig.load_config(str(main))
        assert cfg["server"]["port"] == 9000
    finally:
        pconfig._config = saved


def test_extra_collectors_dirs_load(tmp_path):
    """外部采集器目录扫描: 以 _platform_ext_ 前缀模块名挂载,不污染命名空间"""
    import platform.config as pconfig
    import sys
    ext_dir = tmp_path / "ext"
    ext_dir.mkdir()
    (ext_dir / "my_collector.py").write_text(
        "class MyCollector:\n    source_name = 'ext_test'\n", encoding="utf-8")
    saved = pconfig._config
    saved_path = list(sys.path)
    try:
        cfg = {"collectors_extra_dirs": [str(ext_dir)], "sources": {}}
        pconfig.load_extra_collectors(cfg)
        mod = sys.modules.get("_platform_ext_my_collector")
        assert mod is not None
        assert mod.MyCollector.source_name == "ext_test"
    finally:
        pconfig._config = saved
        sys.modules.pop("_platform_ext_my_collector", None)
        sys.path[:] = saved_path  # 恢复 sys.path,避免污染后续用例


# ---------- 任务生命周期 ----------

def test_task_lifecycle_pending_running_done(sched_env):
    task_id = store.create_task("ok", {"max_age": 7})
    # 提交后立刻可见 pending(或已被 worker 领取为 running)
    task = store.get_task(task_id)
    assert task["status"] in ("pending", "running", "done")
    final = wait_task_status(task_id)
    assert final["status"] == "done"
    assert final["items_count"] == 3
    assert final["error"] is None
    assert final["finished_at"] is not None


def test_worker_heartbeat_updates(sched_env):
    from platform import scheduler as sched_mod
    assert sched_mod.is_worker_alive() is True  # 主循环在刷新心跳
    h = sched_mod.get_heartbeat()
    assert time.time() - h < 2


def test_empty_source_classified_source_empty(sched_env):
    task_id = store.create_task("empty")
    final = wait_task_status(task_id)
    assert final["status"] == "failed"
    assert final["error"] == store.ERROR_SOURCE_EMPTY
    assert final["retries"] == 3  # 自动重试 3 次后终态


def test_network_error_classified_and_retried(sched_env):
    task_id = store.create_task("net_err")
    final = wait_task_status(task_id)
    assert final["status"] == "failed"
    assert final["error"] == store.ERROR_NETWORK
    assert final["retries"] == 3


def test_rate_limited_classified(sched_env):
    task_id = store.create_task("rate_limited")
    final = wait_task_status(task_id)
    assert final["status"] == "failed"
    assert final["error"] == store.ERROR_RATE_LIMITED


def test_internal_error_classified(sched_env):
    task_id = store.create_task("internal_err")
    final = wait_task_status(task_id)
    assert final["status"] == "failed"
    assert final["error"] == store.ERROR_INTERNAL
    assert final["retries"] == 3


def test_automatic_retry_succeeds(sched_env):
    fake_collectors.FailOnceCollector.calls = 0
    task_id = store.create_task("fail_once")
    final = wait_task_status(task_id)
    assert final["status"] == "done"
    assert final["retries"] == 1        # 第一次失败重试后成功
    assert final["items_count"] == 1
    assert store.count_items(task_id, unconsumed_only=False) == 1


def test_retry_backoff_respects_delay():
    """退避期间任务不被领取(慢退避验证)"""
    store.init_store()
    sched = Scheduler(max_workers=2, task_timeout_s=300,
                      poll_interval=0.05, retry_delays=(5.0, 10.0, 20.0))
    sched.start()
    try:
        task_id = store.create_task("internal_err")
        deadline = time.time() + 5
        task = None
        while time.time() < deadline:
            task = store.get_task(task_id)
            # 第一次失败后应进入 pending(等待 5s 退避)
            if task["status"] == "pending" and task["retries"] == 1:
                break
            time.sleep(0.05)
        assert task["status"] == "pending"
        assert task["retries"] == 1
        # 退避未到期前不允许再次 running
        time.sleep(0.2)
        assert store.get_task(task_id)["status"] == "pending"
    finally:
        sched.stop()
        store.reset_store()


def test_timeout_force_failed(monkeypatch):
    """超时强制 failed(error=network),并计入自动重试"""
    store.init_store()
    monkeypatch.setattr(fake_collectors.SlowCollector, "duration", 1.0)
    sched = Scheduler(max_workers=2, task_timeout_s=0.2,
                      poll_interval=0.05, retry_delays=(0.1, 0.1, 0.1))
    sched.start()
    try:
        task_id = store.create_task("slow")
        final = wait_task_status(task_id, timeout=20)
        assert final["status"] == "failed"
        assert final["error"] == store.ERROR_NETWORK  # 超时强制 failed(error=network)
        assert final["retries"] == 3
    finally:
        sched.stop()
        store.reset_store()


def test_cancel_scheduler_level(sched_env, monkeypatch):
    sched = sched_env
    monkeypatch.setattr(fake_collectors.SlowCollector, "duration", 2.0)
    task_id = store.create_task("slow")
    # 等 running
    deadline = time.time() + 5
    while time.time() < deadline:
        if store.get_task(task_id)["status"] == "running":
            break
        time.sleep(0.05)
    assert sched.cancel_task(task_id) is True
    final = wait_task_status(task_id)
    assert final["status"] == "cancelled"
    # 重复 cancel → False(终态不可取消)
    assert sched.cancel_task(task_id) is False


def test_retry_task_scheduler_level(sched_env):
    sched = sched_env
    task_id = store.create_task("internal_err")
    final = wait_task_status(task_id)
    assert final["status"] == "failed"
    assert sched.retry_task(task_id) is True
    task = store.get_task(task_id)
    assert task["status"] == "pending"
    assert task["retries"] == 0      # 手动重试重置计数
    assert task["error"] is None
    assert task["callback_status"] is None  # retry 清回调状态
    assert task["delivered"] is False
    # 非 failed 任务不可重试
    task_id2 = store.create_task("ok")
    wait_task_status(task_id2)
    assert sched.retry_task(task_id2) is False


# ---------- 每源全局锁 ----------

def test_per_source_lock_serializes(sched_env):
    """同源两个任务不并行执行: 并发计数始终 ≤ 1"""
    fake_collectors.LockProbeCollector._active = 0
    fake_collectors.LockProbeCollector._max_active = 0
    t1 = store.create_task("lock_probe")
    t2 = store.create_task("lock_probe")
    wait_task_status(t1)
    wait_task_status(t2)
    assert fake_collectors.LockProbeCollector._max_active == 1


# ---------- D16/D18/D19: 源即参数(collector_spec)----------

def test_inline_code_traceback_captured(sched_env):
    """D18: 内联代码 crawl 抛异常 → failed + traceback 列含完整堆栈"""
    import json as _json
    code = "def crawl(sess):\n    raise ValueError('内联爆炸')\n"
    task_id = store.create_task("inline_bad",
                                collector_spec=_json.dumps({"code": code, "version": "v1"}))
    final = wait_task_status(task_id)
    assert final["status"] == "failed"
    assert final["error"] == store.ERROR_INTERNAL
    assert final["traceback"] and "ValueError" in final["traceback"]
    assert "内联爆炸" in final["traceback"]


def test_inline_function_collector_via_scheduler(sched_env):
    """函数式内联: 包装为最小采集器,crawl 返回 dict 列表 → 落库"""
    import json as _json
    code = ("def crawl(sess):\n"
            "    return [{'title': '函数式', 'url': 'https://example.com/f', 'custom': 42}]\n")
    task_id = store.create_task("inline_fn",
                                collector_spec=_json.dumps({"code": code, "version": "v2"}))
    final = wait_task_status(task_id)
    assert final["status"] == "done"
    assert final["items_count"] == 1
    row = store.get_items(task_id)[0]
    assert row["title"] == "函数式"
    assert row["raw_data"] != "{}"


def test_inline_code_cache_reused(monkeypatch):
    """D19: 同 code 两次任务 → AST 检查仅一次;collector_cache 记录 hash;内存缓存复用"""
    import hashlib
    import json as _json
    from platform import config as pconfig
    store.init_store()
    code = ("def crawl(sess):\n"
            "    return [{'title': '缓存复用', 'url': 'https://example.com/c'}]\n")
    calls = {"n": 0}
    orig = pconfig.check_inline_code_ast

    def counting(c):
        calls["n"] += 1
        return orig(c)

    monkeypatch.setattr(pconfig, "check_inline_code_ast", counting)
    sched = Scheduler(max_workers=2, task_timeout_s=300,
                      poll_interval=0.05, retry_delays=(0.1, 0.1, 0.1))
    sched.start()
    try:
        spec = _json.dumps({"code": code, "version": "v3"})
        t1 = store.create_task("inline_cache_1", collector_spec=spec)
        wait_task_status(t1)
        t2 = store.create_task("inline_cache_2", collector_spec=spec)
        wait_task_status(t2)
        assert store.get_task(t1)["status"] == "done"
        assert store.get_task(t2)["status"] == "done"
        # 第一次: 内存 miss + cache miss → 检查 1 次;第二次: 内存缓存命中 → 不再检查
        assert calls["n"] == 1
        code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
        cached = store.get_collector_cache(code_hash)
        assert cached is not None and cached["code"] == code
    finally:
        sched.stop()
        store.reset_store()


def test_to_platform_item_dict_and_newitem():
    """dict 形式(内联 crawl 契约)与 NewsItem 形式统一转换"""
    import json as _json

    from intel.core.base import NewsItem
    # dict: 标准字段直取,自定义字段进 raw_data,date_str 可来自 date
    row = to_platform_item(
        {"title": "T", "url": "https://e/1", "summary": "S", "custom": 123, "date": "2026-08-01"},
        "src")
    assert row["title"] == "T"
    assert row["url"] == "https://e/1"
    assert row["summary"] == "S"
    assert row["type"] == "news"
    assert row["date_str"] == "2026-08-01"
    assert _json.loads(row["raw_data"]) == {"custom": 123}
    # 缺 url 不崩溃(契约要求 url,但宽松兜底)
    row2 = to_platform_item({"title": "仅标题"}, "src")
    assert row2["url"] == ""
    # NewsItem 路径不变
    ni = NewsItem(title="N", url="https://e/2")
    row3 = to_platform_item(ni, "src")
    assert row3["title"] == "N"
    assert row3["url"] == "https://e/2"
