"""共享测试夹具 — 全假源配置,不碰真实 platform_config.yaml 与磁盘 DB

注意: 项目根下的 platform/ 包与 Python 标准库 platform 同名,pytest 启动时标准库
platform 已被缓存进 sys.modules(uuid.py 依赖),必须先弹出缓存再插入项目根,
后续 `from platform import ...` 才能解析到本项目包。
"""
import http.server
import json
import os
import sys
import threading
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

from platform import memory_store as store
from platform.app import create_app
from platform.scheduler import Scheduler

import pytest
from fastapi.testclient import TestClient

# 快速轮询 + 快速退避,让生命周期/重试测试不等待真实 5s/10s/20s
FAST_OPTS = {"poll_interval": 0.05, "retry_delays": (0.1, 0.1, 0.1)}
# 回调投递短退避(回调测试不等待真实 2/4/8s)
FAST_CALLBACK_OPTS = {"callback_timeout_s": 1, "callback_retry_delays": (0.1, 0.1, 0.1)}


def make_test_config(server_overrides=None, storage_overrides=None) -> dict:
    """构造测试配置: server/storage 可覆盖(零持久化: 内存态 storage + callback 段)"""
    server = {"host": "127.0.0.1", "port": 9000, "max_workers": 2, "task_timeout_s": 300}
    server.update(server_overrides or {})
    storage = {"ttl_seconds": 3600, "max_tasks": 1000, "max_items_per_task": 10000,
               "free_on_full_consume": True}
    storage.update(storage_overrides or {})
    callback = {"timeout_s": 10, "retry_delays": [2, 4, 8]}
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
        "storage": storage,
        "callback": callback,
    }


@pytest.fixture(autouse=True)
def reset_store():
    """用例间隔离: 每个用例开始重置内存存储为空单例(替代原 db.reset_db)"""
    store.reset_store()
    store.init_store()
    yield
    store.reset_store()


@pytest.fixture
def client(tmp_path):
    """快速轮询的 API 客户端(自动重试/生命周期测试用)"""
    cfg = make_test_config()
    app = create_app(cfg, scheduler_opts=dict(FAST_OPTS, **FAST_CALLBACK_OPTS))
    with TestClient(app) as c:
        yield c


@pytest.fixture
def sched_env():
    """独立 scheduler 环境: 内存存储 + 已启动的 Scheduler(不经 FastAPI)"""
    store.init_store()
    sched = Scheduler(max_workers=2, task_timeout_s=300,
                      poll_interval=0.05, retry_delays=(0.1, 0.1, 0.1))
    sched.start()
    yield sched
    sched.stop()
    store.reset_store()


class _CallbackState:
    """stub 回调服务器共享状态: 记录请求、可编程响应码/延迟"""

    def __init__(self):
        self.lock = threading.Lock()
        self.requests = []      # 收到的请求体(bytes)
        self.responses = []     # 待返回的响应码队列(消费式)
        self.delay = 0.0        # 每个请求的处理延迟(秒)

    def json_bodies(self):
        return [json.loads(b.decode("utf-8")) for b in self.requests]


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    state: _CallbackState = _CallbackState()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        with self.state.lock:
            self.state.requests.append(body)
            if self.state.responses:
                code = self.state.responses.pop(0)
            else:
                code = 200
        if self.state.delay:
            time.sleep(self.state.delay)
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass  # 静默,避免污染测试输出


@pytest.fixture
def callback_server():
    """stub 回调服务器: stdlib http.server 起线程 + 随机空闲端口(127.0.0.1:0)"""
    state = _CallbackState()
    _CallbackHandler.state = state
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _host, port = server.server_address
    yield {"url": f"http://127.0.0.1:{port}/cb", "state": state}
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def wait_task_status(task_id: str, timeout: float = 15.0) -> dict:
    """轮询内存存储直到任务到达终态,返回任务 dict"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = store.get_task(task_id)
        if task and task["status"] in store.TERMINAL_STATUSES:
            return task
        time.sleep(0.05)
    raise AssertionError(f"任务 {task_id} 未在 {timeout}s 内到达终态: {store.get_task(task_id)}")


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
