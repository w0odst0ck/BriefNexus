"""接口测试 — FastAPI TestClient 覆盖 8 个端点 + PARAM_SCHEMA 校验"""
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


def test_collect_domain_and_callback_persisted(client):
    r = client.post("/collect", json={
        "source": "ok", "domain": "self_driving", "callback_url": "https://cb.example.com/x"})
    assert r.status_code == 201
    task = wait_api_task(client, r.json()["task_id"])
    assert task["source"] == "ok"


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
                                "finished_at", "error", "items_count"}
    assert task["task_id"] == task_id
    assert task["source"] == "ok"
    assert task["created_at"]


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
