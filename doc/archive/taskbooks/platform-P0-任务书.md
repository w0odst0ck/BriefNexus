# BriefNexus 通用采集平台 P0 任务书：API 骨架 + 异步任务 + 透出现有源

> 2026-08-13 ｜ 依据：`plan/通用采集平台-技术选型书.md`（已评审定稿）
> 你是执行者，禁止嵌套调用任何 AI coding 工具，直接完成任务。

## 背景

BriefNexus 已有 intel/（情报采集，8 源）+ standards/（国标采集）两套 CLI 采集引擎。
现需在其上加一层 **HTTP API 服务**，对外暴露采集能力，供 Mojin RAG / trade-pulse / SceneCraft 多项目复用。

## 交付物（全部在 BriefNexus 项目内新增 `platform/` 目录）

```
platform/
├── __init__.py
├── app.py               # FastAPI 入口（uvicorn 启动）
├── db.py                # SQLite 连接（WAL 模式）+ 表结构 + CRUD
├── scheduler.py         # 后台 worker 线程（任务轮询/心跳/超时/重试/每源锁）
├── config.py            # platform_config.yaml 加载 + 采集器动态 import
├── platform_config.yaml # 平台配置（server/sources/storage）
└── tests/
    ├── test_api.py      # 接口测试（pytest + fastapi TestClient）
    └── test_scheduler.py# 任务生命周期/心跳/重试测试
```

**约束：不改动 intel/、standards/ 任何现有代码**（只 import 复用），不引入 Redis/Celery。

## 接口规格（严格按选型书 3.1）

| 方法/路径 | 说明 |
|---|---|
| POST /collect | 提交异步任务：`{source, params?, domain?, callback_url?}` → 201 `{task_id}`；params 按采集器 PARAM_SCHEMA 校验，非法 422 |
| POST /collect/sync | 同步执行小任务（<30s）：同参数，直接返回 `{task_id, status, items, items_count}`（阻塞等完成） |
| GET /tasks/{id} | 任务状态：`{task_id, status, source, created_at, finished_at, error, items_count}`（**不含 items**） |
| GET /tasks/{id}/items | 分页拉取：`?offset=&limit=&consume=` → `{items, total, has_more}`；consume=1 标记 consumed，下次跳过 |
| POST /tasks/{id}/cancel | 取消（pending/running 可取消）→ `{status: cancelled}` |
| POST /tasks/{id}/retry | 失败任务重跑 → 重置 pending |
| GET /sources | 列出可用源：`[{name, display_name, domains, params}]`（来自 platform_config.yaml enabled=true） |
| GET /healthz | `{status, db, worker}`——worker 心跳 >60s 未更新返回 503 |

## 任务状态机

```
pending → running → done | failed | cancelled
failed 可通过 /retry 回到 pending（重试 3 次自动退避 5s/10s/20s）
超时 300s 强制 failed（error=network）
```

error 分类：`rate_limited`（限流/反爬）| `network`（超时/断连）| `source_empty`（源无数据）| `internal`（代码异常）

## 关键实现要求

### db.py
- 连接串：`sqlite:///...data/briefnexus.db?journal_mode=WAL&busy_timeout=5000`（**必须**，API 线程与 worker 并发写）
- 表：`tasks`（id TEXT PK, source, params TEXT, domain, status, created_at, finished_at, error, items_count, retries, callback_url）
- 表：`items`（id INTEGER PK AUTOINCREMENT, task_id FK CASCADE, dedup_key UNIQUE, title, url, summary, source, domain, sector, type DEFAULT 'news', date_str, raw_data TEXT, consumed INTEGER DEFAULT 0, created_at）
- dedup：item 入库前按 `(source, title)` MD5 生成 dedup_key，重复跳过（跨任务全局去重）

### scheduler.py
- 单线程后台 worker：ThreadPoolExecutor(max_workers=2) 执行采集，worker 主循环 1s 轮询 pending
- **心跳**：每次循环更新模块级 `heartbeat = time.time()`（/healthz 读它）
- **每源全局锁**：同一 source 同时只跑一个任务（dict of threading.Lock）
- 采集执行：实例化采集器（config.py 动态 import）→ `collector.crawl(sess)` → 结果 NewsItem 列表 → 逐条落库（带去重）→ 更新 task done
- 异常处理：requests 异常 → error=network；源返回空 → source_empty；其他 → internal；重试 3 次
- 取消检查：任务 running 时每 5s 检查 cancel 标志（线程内标志位，不强制 kill）

### config.py
- 读 `platform/platform_config.yaml`：server（host/port/max_workers/task_timeout_s）+ sources（enabled/module/params）+ storage（db_path/dedup_ttl_days）
- 动态 import：`module` 字段 `intel.collectors.xxx:ClassName` → `importlib.import_module` + `getattr`
- 透出源（enabled=true）：white_house / nvidia / sec_edgar / eu_commission / arxiv_perception / adas_vehicle_intl / eastmoney / cninfo
  - 注意：现有 intel 采集器用 `@register` + `sources.yaml` 配置；P0 用 platform_config.yaml 的 module 字段直接指向类路径（不依赖注册表，但保留注册表兼容）
  - 采集器实例化方式参考 `intel/cli.py` 的 `instantiate_collectors()`（阅读现有代码后对齐参数）

### NewsItem 泛化（透传）
- intel 现有 NewsItem 只有 title/url/summary/date/source/domain/sector——**不修改 intel 代码**
- platform 层定义自己的响应模型（dataclass/dict），采集器返回的 NewsItem 转为 `{title, url, summary, source, domain, sector, type:"news", date_str, raw_data:{}}` 落库
- raw_data：采集器若返回额外字段，塞入 raw_data（若无则空 dict）

### PARAM_SCHEMA 校验
- platform 层支持采集器类属性 `PARAM_SCHEMA = {"param": {"type":"int","min":1,"max":90}}`（可选）
- 校验：type 检查 + min/max 边界；不通过 → 422 `{"detail": "param max_age: must be <= 90"}`
- 现有 8 源无 PARAM_SCHEMA → 宽松模式（不校验）

## 验收标准（reasonix 自测 + 我会独立复验）

1. `uv run pytest platform/tests/ -q` 全过（预计 20+ 用例）
2. ruff check 干净
3. 手动链路：起服务 → POST /collect 提交 white_house → 轮询到 done → /items 拉到结果（title 非空）
4. 取消：提交任务 → 立即 cancel → 状态 cancelled
5. 分页 consume：第二次拉取 consume=1 后，再次拉取不含已消费条目
6. 健康检查：/healthz 返回 ok + worker alive
7. `git status` 只有 platform/ 新增 + platform_config.yaml（intel/ standards/ 零改动）
8. 单测覆盖：WAL 并发写（多线程同时 insert）、每源锁（同源两任务不并行）、错误分类、超时强制 failed

## 环境

- Python venv：项目内 `.venv`（已有？`ls .venv` 确认；无则 `uv venv` + `uv pip install fastapi uvicorn pyyaml requests pytest httpx`）
- 现有代码参考：`intel/cli.py`（采集器实例化）、`intel/core/base.py`（NewsItem）、`intel/core/dedup.py`（去重）、`standards/crawler/utils.py`（HTTP 工具，可选复用）
- 注意：intel 采集器 import 时可能加载 .env（scripts/_dotenv.py），保持环境变量兼容

## 中文注释，代码风格对齐现有项目
