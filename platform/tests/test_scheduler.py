"""调度层测试 — 任务生命周期 / 心跳 / 重试 / 超时 / 每源锁 / WAL 并发写 / 错误分类"""
import threading
import time
from platform import db
from platform.scheduler import Scheduler, classify_error
from platform.tests import fake_collectors
from platform.tests.conftest import wait_task_status

import requests

# ---------- 错误分类 ----------

def test_error_classification():
    # 429 → rate_limited
    resp = requests.Response()
    resp.status_code = 429
    assert classify_error(requests.HTTPError("429", response=resp)) == db.ERROR_RATE_LIMITED
    # 403 → rate_limited(反爬)
    resp403 = requests.Response()
    resp403.status_code = 403
    assert classify_error(requests.HTTPError("403", response=resp403)) == db.ERROR_RATE_LIMITED
    # 超时/断连 → network
    assert classify_error(requests.Timeout("timeout")) == db.ERROR_NETWORK
    assert classify_error(requests.ConnectionError("conn")) == db.ERROR_NETWORK
    # 其他 HTTP 错误(5xx)→ network
    resp500 = requests.Response()
    resp500.status_code = 500
    assert classify_error(requests.HTTPError("500", response=resp500)) == db.ERROR_NETWORK
    # 代码异常 → internal
    assert classify_error(ValueError("boom")) == db.ERROR_INTERNAL


# ---------- 去重键 ----------

def test_make_dedup_key_source_title():
    k1 = db.make_dedup_key("ok", "标题A")
    assert k1 == db.make_dedup_key("ok", "标题A")      # 同源同标题 → 同键
    assert k1 != db.make_dedup_key("ok", "标题B")      # 同源不同标题 → 不同键
    assert k1 != db.make_dedup_key("other", "标题A")   # 不同源同标题 → 不同键
    assert len(k1) == 32  # MD5 hex


# ---------- WAL 并发写 ----------

def test_wal_concurrent_inserts(tmp_path):
    db.init_db(str(tmp_path / "wal.db"))
    try:
        task_id = db.create_task("ok", {"max_age": 7})
        errors = []

        def writer(n):
            try:
                for i in range(50):
                    db.add_item(task_id, {
                        "dedup_key": f"k{n}_{i}", "title": f"标题{n}_{i}",
                        "url": f"https://example.com/{n}/{i}", "raw_data": "{}",
                    })
            except Exception as e:  # 并发写不应出现 database is locked
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert db.count_items(task_id, unconsumed_only=False) == 8 * 50
    finally:
        db.reset_db()


def test_dedup_skip_duplicate_across_tasks(tmp_path):
    """跨任务全局去重: 同 (source,title) 第二条任务跳过"""
    db.init_db(str(tmp_path / "dedup.db"))
    try:
        t1 = db.create_task("ok")
        t2 = db.create_task("ok")
        item = {"dedup_key": db.make_dedup_key("ok", "同标题"),
                "title": "同标题", "url": "https://example.com/x", "raw_data": "{}"}
        assert db.add_item(t1, item) is True
        assert db.add_item(t2, item) is False  # 全局去重命中
    finally:
        db.reset_db()


# ---------- 任务生命周期 ----------

def test_task_lifecycle_pending_running_done(sched_env):
    task_id = db.create_task("ok", {"max_age": 7})
    # 提交后立刻可见 pending(或已被 worker 领取为 running)
    task = db.get_task(task_id)
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
    task_id = db.create_task("empty")
    final = wait_task_status(task_id)
    assert final["status"] == "failed"
    assert final["error"] == db.ERROR_SOURCE_EMPTY
    assert final["retries"] == 3  # 自动重试 3 次后终态


def test_network_error_classified_and_retried(sched_env):
    task_id = db.create_task("net_err")
    final = wait_task_status(task_id)
    assert final["status"] == "failed"
    assert final["error"] == db.ERROR_NETWORK
    assert final["retries"] == 3


def test_rate_limited_classified(sched_env):
    task_id = db.create_task("rate_limited")
    final = wait_task_status(task_id)
    assert final["status"] == "failed"
    assert final["error"] == db.ERROR_RATE_LIMITED


def test_internal_error_classified(sched_env):
    task_id = db.create_task("internal_err")
    final = wait_task_status(task_id)
    assert final["status"] == "failed"
    assert final["error"] == db.ERROR_INTERNAL
    assert final["retries"] == 3


def test_automatic_retry_succeeds(sched_env):
    fake_collectors.FailOnceCollector.calls = 0
    task_id = db.create_task("fail_once")
    final = wait_task_status(task_id)
    assert final["status"] == "done"
    assert final["retries"] == 1        # 第一次失败重试后成功
    assert final["items_count"] == 1
    assert db.count_items(task_id, unconsumed_only=False) == 1


def test_retry_backoff_respects_delay(tmp_path):
    """退避期间任务不被领取(慢退避验证)"""
    db.init_db(str(tmp_path / "backoff.db"))
    sched = Scheduler(max_workers=2, task_timeout_s=300,
                      poll_interval=0.05, retry_delays=(5.0, 10.0, 20.0))
    sched.start()
    try:
        task_id = db.create_task("internal_err")
        deadline = time.time() + 5
        while time.time() < deadline:
            task = db.get_task(task_id)
            # 第一次失败后应进入 pending(等待 5s 退避)
            if task["status"] == "pending" and task["retries"] == 1:
                break
            time.sleep(0.05)
        assert task["status"] == "pending"
        assert task["retries"] == 1
        # 退避未到期前不允许再次 running
        time.sleep(0.2)
        assert db.get_task(task_id)["status"] == "pending"
    finally:
        sched.stop()
        db.reset_db()


def test_timeout_force_failed(tmp_path, monkeypatch):
    """超时强制 failed(error=network),并计入自动重试"""
    db.init_db(str(tmp_path / "timeout.db"))
    monkeypatch.setattr(fake_collectors.SlowCollector, "duration", 1.0)
    sched = Scheduler(max_workers=2, task_timeout_s=0.2,
                      poll_interval=0.05, retry_delays=(0.1, 0.1, 0.1))
    sched.start()
    try:
        task_id = db.create_task("slow")
        final = wait_task_status(task_id, timeout=20)
        assert final["status"] == "failed"
        assert final["error"] == db.ERROR_NETWORK  # 超时强制 failed(error=network)
        assert final["retries"] == 3
    finally:
        sched.stop()
        db.reset_db()


def test_cancel_scheduler_level(sched_env, monkeypatch):
    _, sched = sched_env
    monkeypatch.setattr(fake_collectors.SlowCollector, "duration", 2.0)
    task_id = db.create_task("slow")
    # 等 running
    deadline = time.time() + 5
    while time.time() < deadline:
        if db.get_task(task_id)["status"] == "running":
            break
        time.sleep(0.05)
    assert sched.cancel_task(task_id) is True
    final = wait_task_status(task_id)
    assert final["status"] == "cancelled"
    # 重复 cancel → False(终态不可取消)
    assert sched.cancel_task(task_id) is False


def test_retry_task_scheduler_level(sched_env):
    _, sched = sched_env
    task_id = db.create_task("internal_err")
    final = wait_task_status(task_id)
    assert final["status"] == "failed"
    assert sched.retry_task(task_id) is True
    task = db.get_task(task_id)
    assert task["status"] == "pending"
    assert task["retries"] == 0      # 手动重试重置计数
    assert task["error"] is None
    # 非 failed 任务不可重试
    task_id2 = db.create_task("ok")
    wait_task_status(task_id2)
    assert sched.retry_task(task_id2) is False


# ---------- 每源全局锁 ----------

def test_per_source_lock_serializes(sched_env):
    """同源两个任务不并行执行: 并发计数始终 ≤ 1"""
    fake_collectors.LockProbeCollector._active = 0
    fake_collectors.LockProbeCollector._max_active = 0
    t1 = db.create_task("lock_probe")
    t2 = db.create_task("lock_probe")
    wait_task_status(t1)
    wait_task_status(t2)
    assert fake_collectors.LockProbeCollector._max_active == 1
