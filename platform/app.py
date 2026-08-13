"""
FastAPI 入口 — BriefNexus 通用采集平台 HTTP API

启动:
  uvicorn platform.app:app          # 默认 host/port 来自 platform_config.yaml
  python -m platform.app

接口(严格按选型书 3.1;D14 起路由加 /v1 前缀,旧无前缀路径保留兼容,同 handler 并存):
  POST   /v1/collect            提交异步任务 → 201 {task_id}(旧路径 /collect 兼容)
  POST   /v1/collect/sync       同步执行小任务(<30s)
  GET    /v1/tasks/{id}         任务状态(不含 items)
  GET    /v1/tasks/{id}/items   分页拉取结果(?offset=&limit=&consume=)
  POST   /v1/tasks/{id}/cancel  取消(pending/running)
  POST   /v1/tasks/{id}/retry   失败任务重跑 → pending
  GET    /v1/sources            列出启用源
  GET    /v1/healthz            健康检查(db + worker 心跳)
"""
import hashlib
import inspect
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

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

logger = logging.getLogger("platform.api")

SYNC_TIMEOUT_S = 30  # /collect/sync 最大阻塞时长
ITEM_PAGE_MAX = 500  # items 分页 limit 上限


class CollectorSpec(BaseModel):
    """随请求传入的源本体(D16): module 引用 或 code 内联,二选一"""
    module: str | None = None   # "module.path:ClassName" 模块引用
    code: str | None = Field(None, max_length=65536)  # 内联源码上限 64KB(防 DB/内存滥用)


class CollectRequest(BaseModel):
    """提交采集任务请求体"""
    source: str | None = None       # 可选别名: 查配置,查不到 422
    collector: CollectorSpec | None = None  # 源本体(module 或 code 二选一)
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
    """tasks 行 → API 响应(不含 items;archived=1 表示已归档,结果已被清理)

    D18: traceback/collector_log 非空时才透出(避免污染无失败任务的响应形状)。
    """
    body = {
        "task_id": task["id"],
        "status": task["status"],
        "source": task["source"],
        "created_at": task["created_at"],
        "finished_at": task["finished_at"],
        "error": task["error"],
        "items_count": task["items_count"],
        "archived": bool(task.get("archived", 0)),
    }
    if task.get("traceback"):
        body["traceback"] = task["traceback"]
    if task.get("collector_log"):
        body["collector_log"] = task["collector_log"]
    return body


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
    storage_cfg = config.get_storage_config()

    opts = dict(scheduler_opts or {})
    opts.setdefault("max_workers", server["max_workers"])
    opts.setdefault("task_timeout_s", server["task_timeout_s"])
    # 每日清理参数来自 storage 配置(D13)
    opts.setdefault("cleanup_consumed_ttl_days", storage_cfg["consumed_ttl_days"])
    opts.setdefault("cleanup_task_archive_days", storage_cfg["task_archive_days"])
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

    # 路由统一挂到 router: 每个端点同时注册 /v1/* 与旧无前缀路径(D14 兼容,同 handler 并存)
    router = APIRouter()

    def _submit_task(req: CollectRequest) -> str:
        """校验 source/collector + params → 创建 pending 任务,返回 task_id

        D16 校验链:
          - collector 有值 → module/code 二选一(都空/都填 422);
            code 需 ALLOW_INLINE_CODE=true 且通过 AST 安全检查(exec 前)
          - source 有值 → 查配置(命中则用,查不到 422「源未注册」)
          - 都空 → 422
        任务存储: collector_spec JSON(module/code/version)写入 tasks 列。
        """
        collector_spec = None
        if req.collector is not None:
            spec = req.collector
            if (spec.module is None) == (spec.code is None):
                raise HTTPException(status_code=422,
                                    detail="collector.module 与 collector.code 必须二选一")
            if spec.code is not None:
                if not config.is_inline_code_enabled():
                    raise HTTPException(
                        status_code=422,
                        detail="代码内联未启用（ALLOW_INLINE_CODE=true 开启，仅限本机）")
                try:
                    cls_or_fn, source_name = scheduler.resolve_inline_collector(spec.code)
                except scheduler.InlineCodeBlocked as e:
                    raise HTTPException(status_code=422,
                                        detail=f"collector.code 含禁止操作: {e}") from e
                except ValueError as e:
                    raise HTTPException(status_code=422, detail=str(e)) from e
                code_hash = hashlib.sha256(spec.code.encode("utf-8")).hexdigest()
                collector_spec = json.dumps(
                    {"code": spec.code, "version": code_hash}, ensure_ascii=False)
                # 类式内联同样支持 PARAM_SCHEMA 校验
                if inspect.isclass(cls_or_fn):
                    err = validate_params(cls_or_fn, req.params or {})
                    if err:
                        raise HTTPException(status_code=422, detail=err)
            else:
                # collector.module 引用: 调用方代码已部署到平台可访问路径
                # (collectors_extra_dirs / sys.path)——受信部署前提,import 时执行模块
                # 顶层代码。安全模型: 平台绑定 127.0.0.1 + 受信调用方,与内联同风险等级。
                try:
                    cls = config.resolve_collector_class(spec.module)
                except Exception as e:
                    logger.error("collector.module 解析失败 %s: %s", spec.module, e)
                    raise HTTPException(status_code=422,
                                        detail=f"collector.module 解析失败: {spec.module}") from e
                source_name = getattr(cls, "source_name", "") or spec.module
                collector_spec = json.dumps({"module": spec.module}, ensure_ascii=False)
                err = validate_params(cls, req.params or {})
                if err:
                    raise HTTPException(status_code=422, detail=err)
        elif req.source is not None:
            info = config.get_source_info(req.source)
            if info is None:
                raise HTTPException(status_code=422,
                                    detail=f"源未注册: {req.source}，请用 collector 字段传入")
            try:
                cls = config.resolve_collector_class(info["module"])
            except Exception as e:
                logger.error("源 %s 类解析失败: %s", req.source, e)
                raise HTTPException(status_code=500, detail=f"source module error: {req.source}") from e
            err = validate_params(cls, req.params or {})
            if err:
                raise HTTPException(status_code=422, detail=err)
            source_name = req.source
        else:
            raise HTTPException(status_code=422, detail="必须提供 source 或 collector")
        return db.create_task(source_name, req.params or {}, req.domain, req.callback_url,
                              collector_spec=collector_spec)

    @router.post("/collect", status_code=201)
    @router.post("/v1/collect", status_code=201)
    def collect(req: CollectRequest):
        """提交异步采集任务"""
        task_id = _submit_task(req)
        return {"task_id": task_id}

    @router.post("/collect/sync")
    @router.post("/v1/collect/sync")
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

    @router.get("/tasks/{task_id}")
    @router.get("/v1/tasks/{task_id}")
    def get_task(task_id: str):
        """任务状态(不含 items,结果走 /items 分页)"""
        task = db.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
        return task_to_api(task)

    @router.get("/tasks/{task_id}/items")
    @router.get("/v1/tasks/{task_id}/items")
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

    @router.post("/tasks/{task_id}/cancel")
    @router.post("/v1/tasks/{task_id}/cancel")
    def cancel_task(task_id: str):
        """取消任务(pending/running 可取消)"""
        task = db.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
        if not sched.cancel_task(task_id):
            raise HTTPException(status_code=409,
                                detail=f"task not cancellable in status: {task['status']}")
        return {"status": db.STATUS_CANCELLED}

    @router.post("/tasks/{task_id}/retry")
    @router.post("/v1/tasks/{task_id}/retry")
    def retry_task(task_id: str):
        """失败任务重跑 → 重置 pending"""
        task = db.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
        if not sched.retry_task(task_id):
            raise HTTPException(status_code=409,
                                detail=f"only failed task can be retried, current: {task['status']}")
        return {"status": db.STATUS_PENDING}

    @router.get("/sources")
    @router.get("/v1/sources")
    def list_sources():
        """列出启用源(D21 动态发现): 配置挂载源(静态)+ 最近 30 天活跃的动态源

        动态源从 tasks 聚合 last_used/success_count(collector_spec 非空的任务);
        平台内置 8 源已退役(sources: {}),本端点不再返回内置源。
        """
        activity = {r["source"]: r for r in db.get_source_activity(days=30)}
        static_names = set()
        result = []
        # 静态: config.d 挂载 / 配置启用的源
        for name, cfg_item in config.get_enabled_sources().items():
            static_names.add(name)
            cls = None
            try:
                cls = config.resolve_collector_class(cfg_item["module"])
            except Exception as e:
                logger.warning("源 %s 类解析失败: %s", name, e)
            act = activity.get(name, {})
            result.append({
                "name": name,
                "display_name": getattr(cls, "display_name", None)
                                or cfg_item.get("display_name") or name,
                "domains": list(getattr(cls, "domains", []) or cfg_item.get("domains", [])),
                "params": cfg_item.get("params") or {},
                "module": cfg_item.get("module"),
                "active": True,
                "last_used": act.get("last_used"),
                "success_count": act.get("success_count") or 0,
            })
        # 动态: 最近 30 天 tasks 里有 collector_spec 的活跃源(不在静态名单)
        for source, act in activity.items():
            if source in static_names:
                continue
            spec = scheduler.parse_collector_spec(act.get("collector_spec"))
            if not spec:
                continue  # 纯配置源任务(无 collector_spec)已由静态分支覆盖
            entry = {
                "name": source,
                "active": (act.get("success_count") or 0) > 0,
                "last_used": act.get("last_used"),
                "success_count": act.get("success_count") or 0,
            }
            if "code" in spec:
                entry["code_hash"] = spec.get("version")
            else:
                entry["module"] = spec.get("module")
            result.append(entry)
        return result

    @router.get("/template")
    @router.get("/v1/template")
    def get_template(lang: str = "python"):
        """采集器骨架模板(D21): 纯静态文本返回,无逻辑"""
        if lang != "python":
            raise HTTPException(status_code=422, detail=f"不支持的模板语言: {lang}")
        tpl_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "templates", "collector.py")
        try:
            with open(tpl_path, encoding="utf-8") as f:
                content = f.read()
        except OSError as e:
            logger.error("模板文件缺失: %s", tpl_path)
            raise HTTPException(status_code=500, detail="模板文件缺失") from e
        return Response(content=content, media_type="text/plain; charset=utf-8")

    @router.get("/healthz")
    @router.get("/v1/healthz")
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

    app.include_router(router)  # 含 /v1/* 与旧无前缀路径
    return app


# 模块级应用实例(uvicorn platform.app:app)
app = create_app()


if __name__ == "__main__":
    import uvicorn
    server = config.get_server_config()
    uvicorn.run(app, host=server["host"], port=server["port"])
