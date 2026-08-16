"""接口测试 — FastAPI TestClient 覆盖 8 个端点 + PARAM_SCHEMA 校验"""
import hashlib
import time
from platform import scheduler
from platform.tests.conftest import wait_api_task

# ---------- /healthz ----------

def test_healthz_ok(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert body["storage"] == "memory"
    assert body["worker"] == "alive"


def test_healthz_worker_dead_returns_503(client, monkeypatch):
    # 模拟 worker 心跳停滞(>60s)
    monkeypatch.setattr(scheduler, "is_worker_alive", lambda: False)
    r = client.get("/healthz")
    assert r.status_code == 503
    assert r.json()["detail"]["worker"] == "dead"


# ---------- POST /collect ----------

def test_collect_async_ok(client):
    r = client.post("/collect", json={"source": "ok", "params": {"max_age": 7}})
    assert r.status_code == 201
    task_id = r.json()["task_id"]
    assert task_id.startswith("t_")

    task = wait_api_task(client, task_id)
    assert task["status"] == "done"
    assert task["items_count"] == 3
    assert task["error"] is None
    assert task["finished_at"] is not None


def test_collect_unknown_source_422(client):
    r = client.post("/collect", json={"source": "no_such_source"})
    assert r.status_code == 422
    assert "no_such_source" in r.json()["detail"]


def test_collect_disabled_source_422(client):
    r = client.post("/collect", json={"source": "disabled_source"})
    assert r.status_code == 422


def test_collect_param_schema_validation(client):
    # max_age > 90 → 422,错误消息格式固定
    r = client.post("/collect", json={"source": "ok", "params": {"max_age": 100}})
    assert r.status_code == 422
    assert r.json()["detail"] == "param max_age: must be <= 90"
    # max_age < 1 → 422
    r = client.post("/collect", json={"source": "ok", "params": {"max_age": 0}})
    assert r.status_code == 422
    assert r.json()["detail"] == "param max_age: must be >= 1"
    # 类型错误 → 422
    r = client.post("/collect", json={"source": "ok", "params": {"max_age": "7"}})
    assert r.status_code == 422
    assert r.json()["detail"] == "param max_age: must be int"
    # 边界内 → 201
    r = client.post("/collect", json={"source": "ok", "params": {"max_age": 90}})
    assert r.status_code == 201


def test_collect_loose_mode_no_schema(client):
    # 未声明 PARAM_SCHEMA 的源: 宽松模式,任意参数不校验
    r = client.post("/collect", json={"source": "loose", "params": {"whatever": 123}})
    assert r.status_code == 201
    task = wait_api_task(client, r.json()["task_id"])
    assert task["status"] == "done"
    assert task["items_count"] == 1


def test_collect_domain_and_callback(client, callback_server):
    """CB-1 回调成功: 带 callback_url 的任务 done 后锁外回调投递,stub 收到 items"""
    r = client.post("/collect", json={
        "source": "ok", "domain": "self_driving", "callback_url": callback_server["url"]})
    assert r.status_code == 201
    task_id = r.json()["task_id"]
    task = wait_api_task(client, task_id)
    assert task["source"] == "ok"
    assert task["status"] == "done"
    # 回调为锁外异步投递,轮询等 stub 收到请求
    deadline = time.time() + 10
    bodies = []
    while time.time() < deadline:
        bodies = callback_server["state"].json_bodies()
        if bodies:
            break
        time.sleep(0.05)
    assert len(bodies) == 1
    assert bodies[0]["task_id"] == task_id
    assert bodies[0]["status"] == "done"
    assert bodies[0]["items_count"] == 3
    assert len(bodies[0]["items"]) == 3
    assert bodies[0]["items"][0]["title"]
    # 回调成功后 callback_status=delivered,items 交付即清
    deadline = time.time() + 10
    t = None
    while time.time() < deadline:
        t = client.get(f"/tasks/{task_id}").json()
        if t.get("callback_status") == "delivered":
            break
        time.sleep(0.05)
    assert t["callback_status"] == "delivered"
    assert client.get(f"/tasks/{task_id}/items").json()["items"] == []


# ---------- GET /tasks/{id} ----------

def test_task_not_found_404(client):
    assert client.get("/tasks/t_nope").status_code == 404
    assert client.get("/tasks/t_nope/items").status_code == 404
    assert client.post("/tasks/t_nope/cancel").status_code == 404
    assert client.post("/tasks/t_nope/retry").status_code == 404


def test_task_status_shape(client):
    r = client.post("/collect", json={"source": "ok"})
    task_id = r.json()["task_id"]
    task = wait_api_task(client, task_id)
    # 响应字段齐全且不含 items
    assert set(task.keys()) == {"task_id", "status", "source", "created_at",
                                "finished_at", "error", "items_count", "archived"}
    assert task["task_id"] == task_id
    assert task["source"] == "ok"
    assert task["created_at"]
    assert task["archived"] is False


# ---------- GET /tasks/{id}/items(分页 + consume)----------

def test_items_pagination_and_consume(client):
    task_id = client.post("/collect", json={"source": "ok"}).json()["task_id"]
    wait_api_task(client, task_id)

    # 第一次: limit=2 & consume=1 → 2 条并标记
    r = client.get(f"/tasks/{task_id}/items", params={"limit": 2, "consume": 1})
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 2
    assert body["total"] == 3
    assert body["has_more"] is True
    assert all(it["title"] for it in body["items"])  # title 非空
    first_batch = [it["title"] for it in body["items"]]

    # 第二次: 再 consume=1 → 剩 1 条
    r = client.get(f"/tasks/{task_id}/items", params={"consume": 1})
    body = r.json()
    assert len(body["items"]) == 1
    assert body["total"] == 1
    assert body["has_more"] is False
    assert body["items"][0]["title"] not in first_batch

    # 第三次: 已全部消费 → 0 条
    r = client.get(f"/tasks/{task_id}/items")
    assert r.json()["items"] == []
    assert r.json()["total"] == 0


def test_items_without_consume_not_marked(client):
    task_id = client.post("/collect", json={"source": "ok"}).json()["task_id"]
    wait_api_task(client, task_id)
    r1 = client.get(f"/tasks/{task_id}/items")
    assert len(r1.json()["items"]) == 3
    # 不带 consume 拉取不标记,可重复拉取
    r2 = client.get(f"/tasks/{task_id}/items")
    assert len(r2.json()["items"]) == 3


def test_items_item_shape(client):
    task_id = client.post("/collect", json={"source": "ok"}).json()["task_id"]
    wait_api_task(client, task_id)
    items = client.get(f"/tasks/{task_id}/items").json()["items"]
    assert set(items[0].keys()) == {"title", "url", "summary", "source",
                                    "domain", "sector", "type", "date_str", "raw_data"}
    assert items[0]["type"] == "news"
    assert items[0]["raw_data"] == {}


# ---------- POST /tasks/{id}/cancel ----------

def test_cancel_pending_immediately(client, monkeypatch):
    from platform.tests import fake_collectors
    monkeypatch.setattr(fake_collectors.SlowCollector, "duration", 5.0)
    task_id = client.post("/collect", json={"source": "slow"}).json()["task_id"]
    r = client.post(f"/tasks/{task_id}/cancel")
    assert r.status_code == 200
    assert r.json() == {"status": "cancelled"}
    task = wait_api_task(client, task_id)
    assert task["status"] == "cancelled"


def test_cancel_running_task(client, monkeypatch):
    from platform.tests import fake_collectors
    monkeypatch.setattr(fake_collectors.SlowCollector, "duration", 2.0)
    task_id = client.post("/collect", json={"source": "slow"}).json()["task_id"]
    # 等任务进入 running(crawl 已开始)
    for _ in range(200):
        if client.get(f"/tasks/{task_id}").json()["status"] == "running":
            break
        time.sleep(0.05)
    r = client.post(f"/tasks/{task_id}/cancel")
    assert r.status_code == 200
    assert r.json() == {"status": "cancelled"}
    task = wait_api_task(client, task_id)
    assert task["status"] == "cancelled"
    assert task["items_count"] == 0


def test_cancel_done_task_conflict(client):
    task_id = client.post("/collect", json={"source": "ok"}).json()["task_id"]
    wait_api_task(client, task_id)
    r = client.post(f"/tasks/{task_id}/cancel")
    assert r.status_code == 409


# ---------- POST /tasks/{id}/retry ----------

def test_retry_failed_task(client):
    # net_err 自动重试 3 次后终态 failed
    task_id = client.post("/collect", json={"source": "net_err"}).json()["task_id"]
    task = wait_api_task(client, task_id)
    assert task["status"] == "failed"
    assert task["error"] == "network"

    r = client.post(f"/tasks/{task_id}/retry")
    assert r.status_code == 200
    assert r.json() == {"status": "pending"}
    assert client.get(f"/tasks/{task_id}").json()["status"] == "pending"


def test_retry_non_failed_conflict(client):
    task_id = client.post("/collect", json={"source": "ok"}).json()["task_id"]
    wait_api_task(client, task_id)
    r = client.post(f"/tasks/{task_id}/retry")
    assert r.status_code == 409


# ---------- GET /sources ----------

def test_sources_list(client):
    r = client.get("/sources")
    assert r.status_code == 200
    sources = {s["name"]: s for s in r.json()}
    assert "ok" in sources and "loose" in sources
    assert "disabled_source" not in sources  # enabled=false 不透出
    ok = sources["ok"]
    assert ok["display_name"] == "OK Source"
    assert ok["domains"] == ["test", "finance"]
    assert ok["params"] == {"max_age": 7}


# ---------- POST /collect/sync ----------

def test_collect_sync_ok(client):
    r = client.post("/collect/sync", json={"source": "ok"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"
    assert body["items_count"] == 3
    assert len(body["items"]) == 3
    assert body["items"][0]["title"]


def test_collect_sync_failed_source(client):
    r = client.post("/collect/sync", json={"source": "internal_err"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "failed"
    assert body["items"] == []
    assert body["items_count"] == 0


# ---------- /v1 版本化路由(D14) ----------

def test_v1_healthz(client):
    r = client.get("/v1/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_v1_collect_async_ok(client):
    r = client.post("/v1/collect", json={"source": "ok", "params": {"max_age": 7}})
    assert r.status_code == 201
    task_id = r.json()["task_id"]
    task = wait_api_task(client, task_id)
    assert task["status"] == "done"
    assert task["items_count"] == 3


def test_v1_collect_sync_ok(client):
    r = client.post("/v1/collect/sync", json={"source": "ok"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"
    assert body["items_count"] == 3
    assert len(body["items"]) == 3


def test_v1_task_lifecycle_and_items(client):
    task_id = client.post("/v1/collect", json={"source": "ok"}).json()["task_id"]
    task = wait_api_task(client, task_id)
    assert task["status"] == "done"
    # /v1/tasks/{id} 状态
    r = client.get(f"/v1/tasks/{task_id}")
    assert r.status_code == 200
    assert r.json()["status"] == "done"
    # /v1/tasks/{id}/items 分页拉取
    r = client.get(f"/v1/tasks/{task_id}/items")
    assert r.status_code == 200
    assert len(r.json()["items"]) == 3
    # /v1/tasks/{id}/cancel + retry 路由存在且语义正确
    assert client.post(f"/v1/tasks/{task_id}/cancel").status_code == 409  # done 不可取消
    assert client.post(f"/v1/tasks/{task_id}/retry").status_code == 409   # done 不可重试


def test_v1_sources_list(client):
    r = client.get("/v1/sources")
    assert r.status_code == 200
    sources = {s["name"]: s for s in r.json()}
    assert "ok" in sources and "loose" in sources
    assert sources["ok"]["display_name"] == "OK Source"


def test_legacy_paths_compat(client):
    """D14 旧无前缀路径保持可用(同 handler 并存,不跳转)"""
    assert client.get("/healthz").status_code == 200
    assert client.get("/sources").status_code == 200
    r = client.post("/collect", json={"source": "ok"})
    assert r.status_code == 201
    task = wait_api_task(client, r.json()["task_id"])
    assert task["status"] == "done"
    r = client.post("/collect/sync", json={"source": "ok"})
    assert r.status_code == 200
    task_id = r.json()["task_id"]
    assert client.get(f"/tasks/{task_id}").status_code == 200
    assert client.get(f"/tasks/{task_id}/items").status_code == 200


# ---------- D16: 源即参数(source 可选 / collector module / code 内联)----------

def test_collect_no_source_no_collector_422(client):
    """source 与 collector 都空 → 422"""
    r = client.post("/v1/collect", json={})
    assert r.status_code == 422
    assert "source 或 collector" in r.json()["detail"]


def test_collect_unregistered_source_422(client):
    """零源模式: source 查不到 → 422「源未注册,请用 collector 字段传入」"""
    r = client.post("/v1/collect", json={"source": "white_house"})
    assert r.status_code == 422
    assert r.json()["detail"] == "源未注册: white_house，请用 collector 字段传入"


def test_collector_module_and_code_both_422(client):
    """collector.module 与 code 都填 / 都空 → 422 二选一"""
    r = client.post("/v1/collect", json={"collector": {"module": "a:B", "code": "x = 1"}})
    assert r.status_code == 422
    assert "二选一" in r.json()["detail"]
    r = client.post("/v1/collect", json={"collector": {}})
    assert r.status_code == 422
    assert "二选一" in r.json()["detail"]


def test_collector_module_resolve_failed_422(client):
    """collector.module 解析失败 → 422"""
    r = client.post("/v1/collect", json={"collector": {"module": "no.such.mod:Cls"}})
    assert r.status_code == 422
    assert "解析失败" in r.json()["detail"]


def test_collect_module_reference_ok(client):
    """collector.module 引用(复用 config.resolve_collector_class)→ 201 + done"""
    r = client.post("/v1/collect", json={
        "collector": {"module": "platform.tests.fake_collectors:ModuleExtCollector"},
        "params": {"max_age": 7}})
    assert r.status_code == 201
    task = wait_api_task(client, r.json()["task_id"])
    assert task["status"] == "done"
    assert task["items_count"] == 1
    items = client.get(f"/v1/tasks/{task['task_id']}/items").json()["items"]
    assert items[0]["title"] == "模块引用"


def test_inline_code_disabled_without_flag(client, monkeypatch):
    """ALLOW_INLINE_CODE 未开启 → collector.code 直接 422"""
    monkeypatch.delenv("ALLOW_INLINE_CODE", raising=False)
    r = client.post("/v1/collect", json={"collector": {"code": "def crawl(sess):\n    return []\n"}})
    assert r.status_code == 422
    assert r.json()["detail"] == "代码内联未启用（ALLOW_INLINE_CODE=true 开启，仅限本机）"


def test_inline_code_function_ok(client, monkeypatch):
    """函数式内联 crawl(sess) -> list[dict] → 201 + done,title/url 落库"""
    monkeypatch.setenv("ALLOW_INLINE_CODE", "true")
    code = ("def crawl(sess):\n"
            "    return [\n"
            "        {'title': '内联第一条', 'url': 'https://example.com/inline/1', 'summary': '来自内联代码'},\n"
            "        {'title': '内联第二条', 'url': 'https://example.com/inline/2'},\n"
            "    ]\n")
    r = client.post("/v1/collect", json={"collector": {"code": code}})
    assert r.status_code == 201
    task = wait_api_task(client, r.json()["task_id"])
    assert task["status"] == "done"
    assert task["items_count"] == 2
    items = client.get(f"/v1/tasks/{task['task_id']}/items").json()["items"]
    assert items[0]["title"] == "内联第一条"
    assert items[0]["summary"] == "来自内联代码"
    assert items[1]["title"] == "内联第二条"
    assert items[1]["raw_data"] == {}


def test_inline_code_class_ok(client, monkeypatch):
    """类式内联 class Collector(BaseCollector) → 201 + done"""
    monkeypatch.setenv("ALLOW_INLINE_CODE", "true")
    code = ("from intel.core.base import BaseCollector, NewsItem\n"
            "class Collector(BaseCollector):\n"
            "    source_name = 'inline_cls'\n"
            "    def crawl(self, sess):\n"
            "        return [NewsItem(title='类式内联', url='https://example.com/inline/cls',\n"
            "                         source=self.source_name)]\n")
    r = client.post("/v1/collect", json={"collector": {"code": code}})
    assert r.status_code == 201
    task = wait_api_task(client, r.json()["task_id"])
    assert task["status"] == "done"
    assert task["items_count"] == 1
    items = client.get(f"/v1/tasks/{task['task_id']}/items").json()["items"]
    assert items[0]["title"] == "类式内联"
    assert task["source"] == "inline_cls"


def test_inline_class_param_schema(client, monkeypatch):
    """类式内联声明 PARAM_SCHEMA → 提交阶段校验生效"""
    monkeypatch.setenv("ALLOW_INLINE_CODE", "true")
    code = ("from intel.core.base import BaseCollector, NewsItem\n"
            "class Collector(BaseCollector):\n"
            "    source_name = 'inline_schema'\n"
            "    PARAM_SCHEMA = {'max_age': {'type': 'int', 'min': 1, 'max': 90}}\n"
            "    def crawl(self, sess):\n"
            "        return [NewsItem(title='校验', url='https://example.com/s')]\n")
    r = client.post("/v1/collect", json={"collector": {"code": code}, "params": {"max_age": 100}})
    assert r.status_code == 422
    assert r.json()["detail"] == "param max_age: must be <= 90"
    r = client.post("/v1/collect", json={"collector": {"code": code}, "params": {"max_age": 7}})
    assert r.status_code == 201
    task = wait_api_task(client, r.json()["task_id"])
    assert task["status"] == "done"


def test_inline_code_no_collector_422(client, monkeypatch):
    """内联代码既无 Collector 类也无 crawl 函数 → 422"""
    monkeypatch.setenv("ALLOW_INLINE_CODE", "true")
    r = client.post("/v1/collect", json={"collector": {"code": "x = 1\n"}})
    assert r.status_code == 422
    assert "Collector" in r.json()["detail"] and "crawl" in r.json()["detail"]


# ---------- D20: 内联代码安全拦截 ----------

def test_inline_code_blocked_import_os(client, monkeypatch):
    monkeypatch.setenv("ALLOW_INLINE_CODE", "true")
    r = client.post("/v1/collect", json={"collector": {"code": "import os\n\ndef crawl(sess):\n    return []\n"}})
    assert r.status_code == 422
    assert r.json()["detail"] == "collector.code 含禁止操作: os"


def test_inline_code_blocked_eval(client, monkeypatch):
    monkeypatch.setenv("ALLOW_INLINE_CODE", "true")
    code = "def crawl(sess):\n    return [{'title': eval('1'), 'url': 'u'}]\n"
    r = client.post("/v1/collect", json={"collector": {"code": code}})
    assert r.status_code == 422
    assert r.json()["detail"] == "collector.code 含禁止操作: eval"


# ---------- D18: traceback / collector_log 透出 ----------

def test_inline_code_failure_traceback(client, monkeypatch):
    """内联代码抛异常 → 任务 failed + traceback 字段含完整堆栈"""
    monkeypatch.setenv("ALLOW_INLINE_CODE", "true")
    code = "def crawl(sess):\n    raise ValueError('内联爆炸')\n"
    r = client.post("/v1/collect", json={"collector": {"code": code}})
    assert r.status_code == 201
    task = wait_api_task(client, r.json()["task_id"])
    assert task["status"] == "failed"
    assert task["error"] == "internal"
    assert "ValueError" in task["traceback"]
    assert "内联爆炸" in task["traceback"]


def test_inline_stderr_captured(client, monkeypatch):
    """crawl 期间采集器 stderr 输出 → collector_log 字段透出"""
    monkeypatch.setenv("ALLOW_INLINE_CODE", "true")
    code = ("def crawl(sess):\n"
            "    stderr_write('来自采集器的 stderr 输出\\n')\n"
            "    return [{'title': 'log', 'url': 'https://example.com/l'}]\n")
    r = client.post("/v1/collect", json={"collector": {"code": code}})
    assert r.status_code == 201
    task = wait_api_task(client, r.json()["task_id"])
    assert task["status"] == "done"
    assert "来自采集器的 stderr 输出" in task["collector_log"]


# ---------- D21: /v1/template 与 /sources 动态发现 ----------

def test_template_python(client):
    r = client.get("/v1/template")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    assert "class Collector" in r.text
    assert "def crawl" in r.text
    # 旧无前缀路径兼容
    assert client.get("/template").status_code == 200


def test_template_unsupported_lang(client):
    r = client.get("/v1/template", params={"lang": "go"})
    assert r.status_code == 422
    assert "go" in r.json()["detail"]


def test_sources_dynamic_discovery(client, monkeypatch):
    """D21: /sources 聚合静态配置源 + 最近 30 天动态源(module 引用 / code 内联)"""
    monkeypatch.setenv("ALLOW_INLINE_CODE", "true")
    # 静态源提交过任务 → last_used/success_count 补全
    r0 = client.post("/v1/collect", json={"source": "ok"})
    wait_api_task(client, r0.json()["task_id"])
    # module 引用 → 动态源 module_ext
    r1 = client.post("/v1/collect", json={
        "collector": {"module": "platform.tests.fake_collectors:ModuleExtCollector"}})
    wait_api_task(client, r1.json()["task_id"])
    # code 内联 → 动态源 name=hash 前 8 位
    code = "def crawl(sess):\n    return [{'title': '动态', 'url': 'https://example.com/d'}]\n"
    r2 = client.post("/v1/collect", json={"collector": {"code": code}})
    wait_api_task(client, r2.json()["task_id"])

    sources = {s["name"]: s for s in client.get("/v1/sources").json()}
    # 静态源: 保留 display_name/domains/params,新增 module/active/last_used/success_count
    ok = sources["ok"]
    assert ok["display_name"] == "OK Source"
    assert ok["domains"] == ["test", "finance"]
    assert ok["module"] == "platform.tests.fake_collectors:OkCollector"
    assert ok["active"] is True
    assert ok["success_count"] == 1
    assert ok["last_used"] is not None
    # 动态: module 引用源
    ext = sources.get("module_ext")
    assert ext is not None
    assert ext["module"] == "platform.tests.fake_collectors:ModuleExtCollector"
    assert ext["active"] is True
    assert ext["success_count"] == 1
    assert ext["last_used"] is not None
    # 动态: code 内联源
    code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
    inline = sources.get(code_hash[:8])
    assert inline is not None
    assert inline["code_hash"] == code_hash
    assert inline["success_count"] == 1


# ---------- 零持久化新增: callback_url scheme 校验 / 容量上限 / 交付即清 ----------

def test_callback_url_scheme_validation_422(client):
    """CB-5 scheme 校验: ftp/file/空 scheme/无 netloc → 提交 422"""
    for bad in ("ftp://example.com/x", "file:///etc/passwd", "not_a_url", ""):
        r = client.post("/collect", json={"source": "ok", "callback_url": bad})
        assert r.status_code == 422, bad


def test_capacity_task_table_429(tmp_path):
    """CAP-1 任务表上限: max_tasks=1,第二个 POST /collect → 429"""
    from platform.app import create_app
    from platform.tests.conftest import FAST_CALLBACK_OPTS, FAST_OPTS, make_test_config

    from fastapi.testclient import TestClient

    cfg = make_test_config(storage_overrides={"max_tasks": 1})
    app = create_app(cfg, scheduler_opts=dict(FAST_OPTS, **FAST_CALLBACK_OPTS))
    with TestClient(app) as c:
        r1 = c.post("/collect", json={"source": "ok"})
        assert r1.status_code == 201
        r2 = c.post("/collect", json={"source": "loose"})
        assert r2.status_code == 429
        assert "已满" in r2.json()["detail"]


def test_sync_delivery_frees_items(client):
    """LIF-1 交付即清(同步): /collect/sync 返回后 items 已释放,任务元数据保留"""
    r = client.post("/collect/sync", json={"source": "ok"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"
    assert len(body["items"]) == 3
    task_id = body["task_id"]
    # 任务仍可查(状态 200),但 items 已交付即清 → 空列表 + total=0
    r2 = client.get(f"/tasks/{task_id}/items")
    assert r2.status_code == 200
    assert r2.json()["items"] == []
    assert r2.json()["total"] == 0


def _wait_callback_status(client, task_id, wanted, timeout=15.0):
    """轮询任务直到 callback_status 达到目标值,返回任务 dict"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = client.get(f"/tasks/{task_id}").json()
        if t.get("callback_status") == wanted or t["status"] == wanted:
            return t
        time.sleep(0.05)
    raise AssertionError(f"任务 {task_id} callback_status 未到 {wanted}")


def test_callback_retry_then_success(client, callback_server):
    """CB-2 回调重试后成功: stub 先 500×2 再 200 → delivered,stub 计次=3"""
    callback_server["state"].responses = [500, 500]  # 消费式: 前两次 500,后续 200
    r = client.post("/collect", json={"source": "ok",
                                      "callback_url": callback_server["url"]})
    assert r.status_code == 201
    task_id = r.json()["task_id"]
    t = _wait_callback_status(client, task_id, "delivered")
    assert t["callback_status"] == "delivered"
    assert t["status"] == "done"
    assert len(callback_server["state"].requests) == 3  # 初次 + 2 次重试


def test_callback_final_failure(client, callback_server):
    """CB-3 回调最终失败: stub 恒 500 → failed + error=callback_failed + items 清空"""
    callback_server["state"].responses = [500, 500, 500, 500]
    r = client.post("/collect", json={"source": "ok",
                                      "callback_url": callback_server["url"]})
    task_id = r.json()["task_id"]
    t = _wait_callback_status(client, task_id, "failed")
    assert t["status"] == "failed"
    assert t["error"] == "callback_failed"
    assert t["callback_status"] == "failed"
    assert len(callback_server["state"].requests) == 4  # 初次 + 3 次重试
    assert client.get(f"/tasks/{task_id}/items").json()["items"] == []  # 结果丢弃


def test_callback_timeout(client, callback_server):
    """CB-4 回调超时: stub 处理延迟 > timeout_s → 触发重试,最终 failed"""
    callback_server["state"].delay = 2.0  # > callback_timeout_s(1s)
    r = client.post("/collect", json={"source": "ok",
                                      "callback_url": callback_server["url"]})
    task_id = r.json()["task_id"]
    t = _wait_callback_status(client, task_id, "failed")
    assert t["status"] == "failed"
    assert t["error"] == "callback_failed"


def test_callback_no_redirect_follow(client, callback_server):
    """CB-6 不跟随重定向: stub 返回 302 → 判定失败并重试(不落 302 目标)"""
    callback_server["state"].responses = [302, 302, 302, 302]
    r = client.post("/collect", json={"source": "ok",
                                      "callback_url": callback_server["url"]})
    task_id = r.json()["task_id"]
    t = _wait_callback_status(client, task_id, "failed")
    assert t["status"] == "failed"
    assert t["error"] == "callback_failed"
    assert len(callback_server["state"].requests) == 4
