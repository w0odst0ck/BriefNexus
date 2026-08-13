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


def test_cross_task_dedup_reference(tmp_path):
    """D11 跨任务去重引用: 同 (source,title) 被 B 任务再采 → 复制引用入库,不阻断消费"""
    db.init_db(str(tmp_path / "dedup.db"))
    try:
        t1 = db.create_task("ok")
        t2 = db.create_task("ok")
        item = {"dedup_key": db.make_dedup_key("ok", "同标题"),
                "title": "同标题", "url": "https://example.com/x", "raw_data": "{}"}
        assert db.add_item(t1, item) is True
        # 跨任务 → 返回 'ref',引用记录完整复制(is_ref=1),新任务可拉到结果
        assert db.add_item(t2, item) == "ref"
        rows = db.get_items(t2)
        assert len(rows) == 1
        assert rows[0]["is_ref"] == 1
        assert rows[0]["title"] == "同标题"
        assert db.count_items(t2, unconsumed_only=False) == 1
        # 同任务内重复 → False,不计数
        assert db.add_item(t1, item) is False
        assert db.count_items(t1, unconsumed_only=False) == 1
        # 无 dedup_key 的条目不参与去重,直接入库
        assert db.add_item(t1, {"title": "无键", "url": "https://e/n"}) is True
    finally:
        db.reset_db()


# ---------- 数据清理(D13) ----------

def test_cleanup_deletes_expired_consumed_items(tmp_path):
    """过期(>90 天)已消费 items 删除;新鲜已消费与未消费保留"""
    db.init_db(str(tmp_path / "cleanup.db"))
    try:
        t1 = db.create_task("ok")
        db.add_item(t1, {"dedup_key": "old", "title": "旧条目", "url": "u", "raw_data": "{}"})
        db.add_item(t1, {"dedup_key": "new_consumed", "title": "新已消费", "url": "u", "raw_data": "{}"})
        db.add_item(t1, {"dedup_key": "fresh", "title": "新未消费", "url": "u", "raw_data": "{}"})
        conn = db._connect()
        conn.execute("UPDATE items SET consumed = 1, created_at = ? WHERE dedup_key = 'old'",
                     ("2020-01-01 00:00:00",))
        conn.execute("UPDATE items SET consumed = 1 WHERE dedup_key = 'new_consumed'")
        conn.commit()
        conn.close()
        stats = db.cleanup(consumed_ttl_days=90, task_archive_days=30)
        assert stats == {"items_deleted": 1, "tasks_archived": 0}
        remaining = {r["dedup_key"] for r in db.get_items(t1, include_consumed=True)}
        assert remaining == {"new_consumed", "fresh"}
    finally:
        db.reset_db()


def test_cleanup_archives_old_tasks(tmp_path):
    """终态任务超期(>30 天)归档: archived=1 且其 items 删除;新鲜任务不动"""
    db.init_db(str(tmp_path / "cleanup_task.db"))
    try:
        old = db.create_task("ok")
        fresh = db.create_task("ok")
        db.add_item(old, {"dedup_key": "o", "title": "旧任务条目", "url": "u", "raw_data": "{}"})
        db.add_item(fresh, {"dedup_key": "f", "title": "新任务条目", "url": "u", "raw_data": "{}"})
        conn = db._connect()
        conn.execute("UPDATE tasks SET status='done', finished_at=? WHERE id=?", ("2020-01-01 00:00:00", old))
        conn.execute("UPDATE tasks SET status='done', finished_at=? WHERE id=?", (db._now(), fresh))
        conn.commit()
        conn.close()
        stats = db.cleanup(consumed_ttl_days=90, task_archive_days=30)
        assert stats["tasks_archived"] == 1
        assert db.get_task(old)["archived"] == 1
        assert db.get_task(fresh)["archived"] == 0
        assert db.count_items(old, unconsumed_only=False) == 0   # 归档任务 items 已删
        assert db.count_items(fresh, unconsumed_only=False) == 1  # 新鲜任务保留
        # 幂等: 已归档任务不再重复归档
        assert db.cleanup()["tasks_archived"] == 0
    finally:
        db.reset_db()


def test_scheduler_daily_cleanup_once_per_day(tmp_path):
    """worker 每日清理: 当天只执行一次,重启后当天不再重复"""
    from datetime import datetime
    db.init_db(str(tmp_path / "cleanup_sched.db"))
    sched = Scheduler(max_workers=1, task_timeout_s=300, poll_interval=0.05,
                      retry_delays=(0.1, 0.1, 0.1))
    try:
        t1 = db.create_task("ok")
        db.add_item(t1, {"dedup_key": "k1", "title": "过期1", "url": "u", "raw_data": "{}"})
        conn = db._connect()
        conn.execute("UPDATE items SET consumed = 1, created_at = ? WHERE task_id = ?",
                     ("2020-01-01 00:00:00", t1))
        conn.commit()
        conn.close()
        today = datetime.now(db.CST).date()
        stats = sched._maybe_cleanup(today)
        assert stats is not None and stats["items_deleted"] == 1
        # 同一天再调 → 不重复执行(记录 last_cleanup_date)
        t2 = db.create_task("ok")
        db.add_item(t2, {"dedup_key": "k2", "title": "过期2", "url": "u", "raw_data": "{}"})
        conn = db._connect()
        conn.execute("UPDATE items SET consumed = 1, created_at = ? WHERE task_id = ?",
                     ("2020-01-01 00:00:00", t2))
        conn.commit()
        conn.close()
        assert sched._maybe_cleanup(today) is None
        assert db.count_items(t2, unconsumed_only=False) == 1  # 当天不再跑,数据保留
        # 同日期再次调用仍不执行(每日一次,last_cleanup_date 防重复)
        stats2 = sched._maybe_cleanup(today)
        assert stats2 is None  # 同日期仍不跑
    finally:
        sched.stop()
        db.reset_db()


def test_migrate_legacy_dedup_unique_table(tmp_path):
    """旧库(dedup_key 列级 UNIQUE、无 is_ref/archived)迁移:
    补列、重建去重约束、旧数据保留、跨任务引用可插入、幂等"""
    import sqlite3
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE tasks (id TEXT PRIMARY KEY, source TEXT NOT NULL,
        params TEXT, domain TEXT, status TEXT DEFAULT 'pending', created_at TEXT,
        finished_at TEXT, error TEXT, items_count INTEGER DEFAULT 0,
        retries INTEGER DEFAULT 0, callback_url TEXT)""")
    conn.execute("""CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        dedup_key TEXT UNIQUE, title TEXT NOT NULL, url TEXT, summary TEXT, source TEXT,
        domain TEXT, sector TEXT, type TEXT DEFAULT 'news', date_str TEXT, raw_data TEXT,
        consumed INTEGER DEFAULT 0, created_at TEXT)""")
    conn.execute("INSERT INTO tasks (id, source) VALUES ('t_old1', 'ok')")
    conn.execute("INSERT INTO tasks (id, source) VALUES ('t_old2', 'ok')")
    conn.execute("INSERT INTO items (task_id, dedup_key, title) VALUES ('t_old1', 'k1', '老数据1')")
    conn.execute("INSERT INTO items (task_id, dedup_key, title) VALUES ('t_old2', 'k2', '老数据2')")
    conn.commit()
    conn.close()

    db.init_db(str(db_path))  # 触发迁移
    try:
        conn = db._connect()
        icols = {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
        tcols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "is_ref" in icols and "archived" in tcols
        # 旧数据保留且 is_ref=0(原记录)
        assert db.count_items("t_old1", unconsumed_only=False) == 1
        # 部分唯一索引已建,同 dedup_key 可共存(引用)
        assert db.add_item("t_old2", {"dedup_key": "k1", "title": "老数据1",
                                      "url": "u", "raw_data": "{}"}) == "ref"
        conn.close()
        # 幂等: 再次 init_db 不报错、不破坏数据
        db.init_db(str(db_path))
        assert db.count_items("t_old1", unconsumed_only=False) == 1
        assert db.count_items("t_old2", unconsumed_only=False) == 2
    finally:
        db.reset_db()


def test_migrate_dedupe_dirty_duplicate_keys(tmp_path):
    """脏库防御: is_ref=0 的同 dedup_key 多行(异常数据)→ 保留最小 id 其余转引用,启动不崩"""
    import sqlite3
    db_path = tmp_path / "dirty.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE tasks (id TEXT PRIMARY KEY, source TEXT NOT NULL,
        params TEXT, domain TEXT, status TEXT DEFAULT 'pending', created_at TEXT,
        finished_at TEXT, error TEXT, items_count INTEGER DEFAULT 0,
        retries INTEGER DEFAULT 0, callback_url TEXT)""")
    conn.execute("""CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        dedup_key TEXT, title TEXT NOT NULL, url TEXT, summary TEXT, source TEXT,
        domain TEXT, sector TEXT, type TEXT DEFAULT 'news', date_str TEXT, raw_data TEXT,
        consumed INTEGER DEFAULT 0, is_ref INTEGER DEFAULT 0, created_at TEXT)""")
    conn.execute("INSERT INTO tasks (id, source) VALUES ('d1', 'ok')")
    conn.execute("INSERT INTO tasks (id, source) VALUES ('d2', 'ok')")
    conn.execute("INSERT INTO items (task_id, dedup_key, title) VALUES ('d1', 'dup', '重复1')")
    conn.execute("INSERT INTO items (task_id, dedup_key, title) VALUES ('d2', 'dup', '重复2')")
    conn.commit()
    conn.close()

    db.init_db(str(db_path))  # 触发 _dedupe_legacy_keys: dup 第二行转 is_ref=1
    try:
        conn = db._connect()
        rows = conn.execute(
            "SELECT id, is_ref FROM items WHERE dedup_key = 'dup' ORDER BY id").fetchall()
        assert [r["is_ref"] for r in rows] == [0, 1], rows
        conn.close()
        # 索引就绪,新任务可正常采集/引用
        # d2 迁移时已持有引用行(is_ref=1)→ 再次 add_item 同 key 应跳过,不产生重复引用行
        assert db.add_item("d2", {"dedup_key": "dup", "title": "重复2",
                                  "url": "u", "raw_data": "{}"}) is False
        # d2 仍能拉到自己的结果(迁移时已有的引用行)
        assert db.count_items("d2", unconsumed_only=False) == 1
    finally:
        db.reset_db()


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
  db_path: data/x.db
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
        assert cfg["storage"]["db_path"] == "data/x.db"
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
