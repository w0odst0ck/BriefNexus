"""
FastAPI 入口 — BriefNexus 通用采集平台 HTTP API

启动:
  uvicorn platform.app:app          # 默认 host/port 来自 platform_config.yaml
  python -m platform.app

接口(严格按选型书 3.1):
  POST   /collect               提交异步任务 → 201 {task_id}
  POST   /collect/sync          同步执行小任务(<30s)
  GET    /tasks/{id}            任务状态(不含 items)
  GET    /tasks/{id}/items      分页拉取结果(?offset=&limit=&consume=)
  POST   /tasks/{id}/cancel     取消(pending/running)
  POST   /tasks/{id}/retry      失败任务重跑 → pending
  GET    /sources               列出启用源
  GET    /healthz               健康检查(db + worker 心跳)
"""
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager

# 兼容 `python platform/app.py` 直接运行: 将项目根加入 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 与 intel CLI 保持环境变量兼容(采集器 import 时可能需要 .env)
from scripts._dotenv import load_project_env

load_project_env()

from platform import config, db, scheduler
from platform.scheduler import Scheduler

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logger = logging.getLogger("platform.api")

SYNC_TIMEOUT_S = 30  # /collect/sync 最大阻塞时长
ITEM_PAGE_MAX = 500  # items 分页 limit 上限


class CollectRequest(BaseModel):
    """提交采集任务请求体"""
    source: str
    params: dict | None = None
    domain: str | None = None
    callback_url: str | None = None


# ---------- PARAM_SCHEMA 校验 ----------

def validate_params(cls, params: dict) -> str | None:
    """按采集器类声明的 PARAM_SCHEMA 校验参数;未声明 → 宽松模式(返回 None)。

    PARAM_SCHEMA 形如: {"max_age": {"type": "int", "min": 1, "max": 90}}
    不通过返回错误消息(HTTP 422 detail),通过返回 None。
    """
    schema = getattr(cls, "PARAM_SCHEMA", None)
    if not schema:
        return None  # 宽松模式: 现有 8 源未声明 schema,不校验
    for key, rule in schema.items():
        if key not in params:
            continue  # 可选参数,缺省不校验
        value = params[key]
        typ = rule.get("type", "int")
        if typ == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                return f"param {key}: must be int"
            if "min" in rule and value < rule["min"]:
                return f"param {key}: must be >= {rule['min']}"
            if "max" in rule and value > rule["max"]:
                return f"param {key}: must be <= {rule['max']}"
        elif typ == "float":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return f"param {key}: must be float"
            if "min" in rule and value < rule["min"]:
                return f"param {key}: must be >= {rule['min']}"
            if "max" in rule and value > rule["max"]:
                return f"param {key}: must be <= {rule['max']}"
        elif typ == "str":
            if not isinstance(value, str):
                return f"param {key}: must be str"
        elif typ == "bool":
            if not isinstance(value, bool):
                return f"param {key}: must be bool"
    return None


# ---------- 响应转换 ----------

def item_to_api(row: dict) -> dict:
    """DB items 行 → API 响应(NewsItem 协议泛化版,透传 raw_data)"""
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


def task_to_api(task: dict) -> dict:
    """tasks 行 → API 响应(不含 items)"""
    return {
        "task_id": task["id"],
        "status": task["status"],
        "source": task["source"],
        "created_at": task["created_at"],
        "finished_at": task["finished_at"],
        "error": task["error"],
        "items_count": task["items_count"],
    }


# ---------- 应用工厂 ----------

def create_app(cfg: dict | None = None, scheduler_opts: dict | None = None) -> FastAPI:
    """创建应用。

    Args:
        cfg: 配置 dict;None 时从 platform_config.yaml 加载(生产)
        scheduler_opts: Scheduler 构造覆盖(测试用,如 retry_delays/poll_interval)
    """
    if cfg is not None:
        config.set_config(cfg)
    else:
        config.load_config()
    server = config.get_server_config()

    opts = dict(scheduler_opts or {})
    opts.setdefault("max_workers", server["max_workers"])
    opts.setdefault("task_timeout_s", server["task_timeout_s"])
    sched = Scheduler(**opts)

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        storage = config.get_storage_config()
        db.init_db(storage["db_path"])
        sched.start()
        try:
            yield
        finally:
            sched.stop()

    app = FastAPI(title="BriefNexus 通用采集平台", version="0.1.0", lifespan=_lifespan)
    app.state.scheduler = sched

    def _submit_task(req: CollectRequest) -> str:
        """校验 source + params → 创建 pending 任务,返回 task_id"""
        info = config.get_source_info(req.source)
        if info is None:
            raise HTTPException(status_code=422, detail=f"unknown source: {req.source}")
        try:
            cls = config.resolve_collector_class(info["module"])
        except Exception as e:
            logger.error("源 %s 类解析失败: %s", req.source, e)
            raise HTTPException(status_code=500, detail=f"source module error: {req.source}") from e
        err = validate_params(cls, req.params or {})
        if err:
            raise HTTPException(status_code=422, detail=err)
        return db.create_task(req.source, req.params or {}, req.domain, req.callback_url)

    @app.post("/collect", status_code=201)
    def collect(req: CollectRequest):
        """提交异步采集任务"""
        task_id = _submit_task(req)
        return {"task_id": task_id}

    @app.post("/collect/sync")
    def collect_sync(req: CollectRequest):
        """同步执行小任务(<30s): 阻塞等待终态后直接返回结果"""
        task_id = _submit_task(req)
        deadline = time.time() + SYNC_TIMEOUT_S
        while True:
            task = db.get_task(task_id)
            items = []
            if task["status"] == db.STATUS_DONE:
                rows = db.get_items(task_id, 0, ITEM_PAGE_MAX)
                items = [item_to_api(r) for r in rows]
            if task["status"] in db.TERMINAL_STATUSES or time.time() >= deadline:
                return {"task_id": task_id, "status": task["status"],
                        "items": items, "items_count": task["items_count"]}
            time.sleep(0.2)

    @app.get("/tasks/{task_id}")
    def get_task(task_id: str):
        """任务状态(不含 items,结果走 /items 分页)"""
        task = db.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
        return task_to_api(task)

    @app.get("/tasks/{task_id}/items")
    def get_task_items(task_id: str, offset: int = 0, limit: int = 50, consume: int = 0):
        """分页拉取结果;consume=1 时返回后标记 consumed,下次拉取跳过"""
        if not db.get_task(task_id):
            raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
        offset = max(0, offset)
        limit = max(1, min(limit, ITEM_PAGE_MAX))
        rows = db.get_items(task_id, offset, limit)
        total = db.count_items(task_id, unconsumed_only=True)
        if consume:
            db.mark_consumed(task_id, [r["id"] for r in rows])
        return {
            "items": [item_to_api(r) for r in rows],
            "total": total,
            "has_more": offset + len(rows) < total,
        }

    @app.post("/tasks/{task_id}/cancel")
    def cancel_task(task_id: str):
        """取消任务(pending/running 可取消)"""
        task = db.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
        if not sched.cancel_task(task_id):
            raise HTTPException(status_code=409,
                                detail=f"task not cancellable in status: {task['status']}")
        return {"status": db.STATUS_CANCELLED}

    @app.post("/tasks/{task_id}/retry")
    def retry_task(task_id: str):
        """失败任务重跑 → 重置 pending"""
        task = db.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
        if not sched.retry_task(task_id):
            raise HTTPException(status_code=409,
                                detail=f"only failed task can be retried, current: {task['status']}")
        return {"status": db.STATUS_PENDING}

    @app.get("/sources")
    def list_sources():
        """列出启用源(name/display_name/domains/params)"""
        result = []
        for name, cfg_item in config.get_enabled_sources().items():
            cls = None
            try:
                cls = config.resolve_collector_class(cfg_item["module"])
            except Exception as e:
                logger.warning("源 %s 类解析失败: %s", name, e)
            result.append({
                "name": name,
                "display_name": getattr(cls, "display_name", None)
                                or cfg_item.get("display_name") or name,
                "domains": list(getattr(cls, "domains", []) or cfg_item.get("domains", [])),
                "params": cfg_item.get("params") or {},
            })
        return result

    @app.get("/healthz")
    def healthz():
        """健康检查: db 可读 + worker 心跳(<60s)"""
        db_ok = db.ping()
        worker_alive = scheduler.is_worker_alive()
        body = {
            "status": "ok" if (db_ok and worker_alive) else "error",
            "db": "ok" if db_ok else "error",
            "worker": "alive" if worker_alive else "dead",
        }
        if not (db_ok and worker_alive):
            raise HTTPException(status_code=503, detail=body)
        return body

    return app


# 模块级应用实例(uvicorn platform.app:app)
app = create_app()


if __name__ == "__main__":
    import uvicorn
    server = config.get_server_config()
    uvicorn.run(app, host=server["host"], port=server["port"])
