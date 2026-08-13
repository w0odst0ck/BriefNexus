"""共享测试夹具 — 全假源配置,不碰真实 platform_config.yaml 与磁盘 DB

注意: 项目根下的 platform/ 包与 Python 标准库 platform 同名,pytest 启动时标准库
platform 已被缓存进 sys.modules(uuid.py 依赖),必须先弹出缓存再插入项目根,
后续 `from platform import ...` 才能解析到本项目包。
"""
import os
import sys
import time

# 项目根 = 本文件(platform/tests/)上两级
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# 标准库 platform 已缓存: 弹出,让 import platform 解析到本项目包(而非标准库)
import platform as _stdlib_platform

if (sys.modules.get("platform") is _stdlib_platform
        and os.path.isdir(os.path.join(_PROJECT_ROOT, "platform"))):
    sys.modules.pop("platform", None)

from platform import db
from platform.app import create_app
from platform.scheduler import Scheduler

import pytest
from fastapi.testclient import TestClient

# 快速轮询 + 快速退避,让生命周期/重试测试不等待真实 5s/10s/20s
FAST_OPTS = {"poll_interval": 0.05, "retry_delays": (0.1, 0.1, 0.1)}


def make_test_config(db_path, **server_overrides) -> dict:
    """构造测试配置: server 可覆盖(max_workers/task_timeout_s 等)"""
    server = {"host": "127.0.0.1", "port": 9000, "max_workers": 2, "task_timeout_s": 300}
    server.update(server_overrides)
    return {
        "server": server,
        "sources": {
            "ok": {"enabled": True, "module": "platform.tests.fake_collectors:OkCollector", "params": {"max_age": 7}},
            "loose": {"enabled": True, "module": "platform.tests.fake_collectors:LooseCollector", "params": {}},
            "empty": {"enabled": True, "module": "platform.tests.fake_collectors:EmptyCollector", "params": {}},
            "net_err": {"enabled": True, "module": "platform.tests.fake_collectors:NetworkErrorCollector", "params": {}},
            "rate_limited": {"enabled": True, "module": "platform.tests.fake_collectors:RateLimitedCollector", "params": {}},
            "internal_err": {"enabled": True, "module": "platform.tests.fake_collectors:InternalErrorCollector", "params": {}},
            "slow": {"enabled": True, "module": "platform.tests.fake_collectors:SlowCollector", "params": {}},
            "fail_once": {"enabled": True, "module": "platform.tests.fake_collectors:FailOnceCollector", "params": {}},
            "lock_probe": {"enabled": True, "module": "platform.tests.fake_collectors:LockProbeCollector", "params": {}},
            "disabled_source": {"enabled": False, "module": "platform.tests.fake_collectors:OkCollector", "params": {}},
        },
        "storage": {"db_path": str(db_path), "dedup_ttl_days": 90},
    }


@pytest.fixture
def client(tmp_path):
    """快速轮询的 API 客户端(自动重试/生命周期测试用)"""
    cfg = make_test_config(tmp_path / "test.db")
    app = create_app(cfg, scheduler_opts=dict(FAST_OPTS))
    with TestClient(app) as c:
        yield c


@pytest.fixture
def sched_env(tmp_path):
    """独立 scheduler 环境: 临时 DB + 已启动的 Scheduler(不经 FastAPI)"""
    db_path = tmp_path / "sched.db"
    db.init_db(str(db_path))
    sched = Scheduler(max_workers=2, task_timeout_s=300,
                      poll_interval=0.05, retry_delays=(0.1, 0.1, 0.1))
    sched.start()
    yield db_path, sched
    sched.stop()
    db.reset_db()


def wait_task_status(task_id: str, timeout: float = 15.0) -> dict:
    """轮询 DB 直到任务到达终态,返回任务 dict"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = db.get_task(task_id)
        if task and task["status"] in db.TERMINAL_STATUSES:
            return task
        time.sleep(0.05)
    raise AssertionError(f"任务 {task_id} 未在 {timeout}s 内到达终态: {db.get_task(task_id)}")


def wait_api_task(client, task_id: str, timeout: float = 15.0) -> dict:
    """通过 API 轮询任务直到终态"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/tasks/{task_id}")
        assert r.status_code == 200
        task = r.json()
        if task["status"] in ("done", "failed", "cancelled"):
            return task
        time.sleep(0.05)
    raise AssertionError(f"任务 {task_id} 未在 {timeout}s 内到达终态")
