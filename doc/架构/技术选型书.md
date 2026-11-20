# BriefNexus 通用采集平台 — 技术选型书 v0.1

> 2026-08-13 起草 ｜ 状态：待评审
> 目标：把 BriefNexus 从「本地 CLI 采集器」升级为「通用信息采集平台」，对外暴露 HTTP 接口，
> 供 Mojin 独立站 RAG / trade-pulse / SceneCraft 等多项目复用。

---

## 1. 背景与动机

### 1.1 现状

BriefNexus 已有两套采集引擎，均为**本地 CLI / 库**形态：

| 模块 | 能力 | 形态 |
|------|------|------|
| `intel/` | 情报采集（白宫/EU/NVIDIA/SEC/arXiv 等 8 源） | `python -m intel.cli run` |
| `standards/` | 国标采集（SAMR）+ SQLite FTS5 检索 | `python -m standards.crawler.main` |

核心资产（**选型书前提，零改造复用**）：
- 采集器注册表：`intel/core/registry.py` 的 `@register` 装饰器自动发现
- 采集器基类：`intel/core/base.py` 的 `BaseCollector.crawl() → List[NewsItem]`
- 去重：`intel/core/dedup.py` 的 `DedupStore`（MD5 键）
- HTTP 工具：`standards/crawler/utils.py`（UA 轮换 / 限速 / 重试 / safe_get_json）
- 配置驱动：intel 用 YAML（`intel/config/sources.yaml`），standards 用 ini

### 1.2 痛点

1. **多项目无法复用**：Mojin RAG 要竞品/合规知识，trade-pulse 要另类数据，SceneCraft 要行业动态——各自重写采集器，重复造轮子
2. **无统一接口**：都是 CLI，程序化调用要 subprocess，无任务状态、无结果查询
3. **无调度层**：大批量采集是同步阻塞，无法异步提交/轮询
4. **配置分裂**：intel 用 YAML、standards 用 ini，新源接入要读两套文档

### 1.3 目标

```
多项目 ──HTTP──▶ 通用采集平台 ──▶ 复用采集内核 ──▶ 结构化结果
                 FastAPI :9000       intel/ + standards/
```

- **P0**：HTTP API 骨架 + 异步任务队列 + 透出现有 intel 源（今天可验收）
- **P1**：Mojin 新源适配器（SASO 合规 / 竞品 / FAQ 挖掘）
- **P2**：鉴权 / 限流 / 任务清理 / OpenAPI 文档完善

---

## 2. 总体架构

```
┌────────────────────────────────────────────────────┐
│                   消费方（多项目）                    │
│  Mojin RAG  │  trade-pulse  │  SceneCraft  │ 未来   │
└──────┬─────────────────────────────────────────────┘
       │ HTTP (JSON)
┌──────▼─────────────────────────────────────────────┐
│            BriefNexus 采集平台（服务层）              │
│  ┌─────────────────────────────────────────────┐  │
│  │  FastAPI :9000                              │  │
│  │  POST /collect     提交采集任务（异步）        │  │
│  │  GET  /tasks/{id}  查询任务状态 + 结果         │  │
│  │  GET  /sources     列出可用源 + 参数说明       │  │
│  │  POST /collect/sync 同步小任务（<30s）         │  │
│  │  GET  /healthz     健康检查                   │  │
│  └─────────────────────────────────────────────┘  │
│  ┌─────────────────────────────────────────────┐  │
│  │  调度层：任务队列（SQLite 表 + 后台 worker）    │  │
│  │  状态机: pending → running → done/failed     │  │
│  │  重试: 失败 3 次退避 ｜ 超时: 300s 强制失败     │  │
│  └─────────────────────────────────────────────┘  │
│  ┌─────────────────────────────────────────────┐  │
│  │  采集内核（复用，零改造）                      │  │
│  │  intel/ 注册表 + BaseCollector 适配器         │  │
│  │  standards/ SAMR 引擎 + 下载器               │  │
│  │  utils.py（UA/限速/重试）                    │  │
│  └─────────────────────────────────────────────┘  │
│  ┌─────────────────────────────────────────────┐  │
│  │  存储：SQLite（briefnexus.db）               │  │
│  │  sources 表 ｜ tasks 表 ｜ items 表          │  │
│  └─────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────┘
```

---

## 3. 接口设计（v1）

### 3.1 请求/响应模型

```
POST /collect
{
  "source": "white_house",        // 必填，注册的采集器名
  "params": {"max_age": 7},       // 可选，采集器参数
  "domain": "self_driving",       // 可选，领域过滤
  "callback_url": null            // 可选，Webhook 回调（P1）
}
→ 201 {"task_id": "t_8f3a..."}

GET /tasks/{task_id}
→ 200 {
  "task_id": "t_8f3a...",
  "status": "done",               // pending|running|done|failed|cancelled
  "source": "white_house",
  "created_at": "...",
  "finished_at": "...",
  "error": null,                  // failed 时非空（rate_limited|network|source_empty|internal）
  "items_count": 12               // 结果走 /items 分页拉取
}

GET /tasks/{task_id}/items?offset=0&limit=50&consume=1
→ 200 {"items": [NewsItem...], "total": 12, "has_more": false}
  // consume=1 返回后标记 consumed，下次拉取跳过（防重复消费）

POST /tasks/{task_id}/cancel → 200 {"status": "cancelled"}   // pending/running 可取消
POST /tasks/{task_id}/retry  → 200 {"status": "pending"}     // failed 重跑

GET /sources
→ 200 [{"name": "white_house", "display_name": "...", "domains": [...], "params": {...}}]

GET /healthz → 200 {"status": "ok", "db": "ok", "worker": "alive"}  // worker 心跳检查
```

### 3.2 NewsItem 协议（复用现有 + 泛化）

```python
@dataclass
class NewsItem:
    title: str
    url: str
    summary: str = ""
    date_obj: datetime | None = None
    source: str = ""
    domain: str = ""
    sector: str = ""
    type: str = "news"      # 泛化：news|doc|table|standard
    raw_data: dict = None   # 泛化：任意结构化负载（表格/标准元数据等）
```

**决策 D1：结果统一为 NewsItem 协议（泛化版）**。
- 老的新闻源：`type=news`，行为完全不变
- 新的结构源：`type=table`，`raw_data={"sku":..., "price":...}`，消费方按 type 分支处理
- API 层返回 items 时原样透传 raw_data——消费方拿到「协议 + 原始数据」双保险
- 优势：跨项目消费方契约一致；未来加 100 个采集器，只要约定「继承 BaseCollector + 返回带 type 的 Item」，平台零改动

---

## 4. 调度层设计

### 4.1 任务存储（SQLite 表，WAL 模式）

```sql
CREATE TABLE tasks (
    id          TEXT PRIMARY KEY,          -- t_ 前缀 + 时间戳随机
    source      TEXT NOT NULL,
    params      TEXT,                      -- JSON
    domain      TEXT,
    status      TEXT DEFAULT 'pending',    -- pending|running|done|failed|cancelled
    created_at  TEXT DEFAULT (datetime('now','localtime')),
    finished_at TEXT,
    error       TEXT,                      -- 错误分类：rate_limited|network|source_empty|internal
    items_count INTEGER DEFAULT 0,
    retries     INTEGER DEFAULT 0,
    callback_url TEXT
);
CREATE TABLE items (                        -- 采集结果，按任务归属
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    dedup_key   TEXT UNIQUE,               -- 复用 DedupStore MD5
    title       TEXT NOT NULL,
    url         TEXT,
    summary     TEXT,
    source      TEXT,
    domain      TEXT,
    sector      TEXT,
    type        TEXT DEFAULT 'news',       -- 泛化类型
    date_str    TEXT,
    raw_data    TEXT,                      -- 原始回包 JSON（调试 + 结构化负载）
    consumed    INTEGER DEFAULT 0,         -- consume=1 拉取后标记，防重复消费
    created_at  TEXT DEFAULT (datetime('now','localtime'))
);
```

**关键：连接串必须 `?journal_mode=WAL&busy_timeout=5000`**——API 线程与 worker 线程并发写，默认 SQLite 模式会 database is locked。

### 4.2 Worker 模型（轻量，不引入 Redis/Celery）

- **单进程后台线程池**（ThreadPoolExecutor，max_workers=2，避免打爆反爬目标）
- 启动时起 worker 线程，轮询 `status='pending'` 任务（1s 间隔）
- 任务级：`timeout=300s`（超时强制 failed）；`retries=3`（指数退避 5s/10s/20s）
- **worker 心跳**：每次循环更新 heartbeat 时间戳；`/healthz` 检查 `now - heartbeat < 60s`，超时返回 503（防 worker 线程静默死亡）
- **决策 D2**：v1 用 SQLite + 线程池。理由：单机部署、任务量小（几十/天）、零运维；
  未来并发 >10 任务/分钟或需分布式时再评估 Redis + RQ/Celery（接口不变，只换实现）

### 4.3 任务生命周期（补 cancel / retry）

```
POST /collect          → pending
worker 接管            → running
完成/超时/重试耗尽      → done | failed | cancelled
POST /tasks/{id}/cancel → cancelled（pending/running 可取消）
POST /tasks/{id}/retry  → 重置为 pending 重跑（failed 可重试）
```

错误分类（error 字段）：`rate_limited`（反爬/限流）| `network`（超时/断连）| `source_empty`（源无数据）| `internal`（代码异常）——消费方和运维一看便知处理方向。

---

### 4.4 采集器参数 schema 声明（防炸）

采集器类声明 `PARAM_SCHEMA`，API 层提交时校验：

```python
class WhiteHouseCollector(BaseCollector):
    PARAM_SCHEMA = {
        "max_age": {"type": "int", "min": 1, "max": 90},
    }
```

- `POST /collect` 校验 params 不符合 schema → 422 拒绝（消费方调错立刻知道）
- 未声明 schema 的采集器：宽松模式（不校验），兼容现有 8 源

### 4.5 限速与反爬（复用 utils.py）

- 每源独立限速：`request_delay_min/max` 从源配置读取（继承 standards 的 ini 模式）
- UA 轮换 + 重试（已实现，直接 import）
- **每源全局锁**：同一时间只跑一个该源的任务（多项目共享限速配额）

### 4.6 结果消费（分页 + 防重复）

- `GET /tasks/{id}` 只回状态 + items_count（不回全量结果）
- `GET /tasks/{id}/items?offset=0&limit=50&consume=1` 分页拉取；`consume=1` 返回后标记 `consumed`，下次拉取跳过
- 价值：大任务（500 条）分页拉取不爆内存；消费幂等，防止 cron 重跑导致知识库重复块

---

## 5. 配置统一（决策 D3）

**现状**：intel 用 `sources.yaml`，standards 用 `standards_config.ini`——分裂。

**方案**：平台层新增 `platform_config.yaml`（统一入口）：

```yaml
server:
  host: 127.0.0.1
  port: 9000
  max_workers: 2
  task_timeout_s: 300

sources:
  white_house:
    enabled: true
    module: intel.collectors.white_house:WhiteHouseCollector
    params: {max_age: 7}
  nvidia:
    enabled: true
    module: intel.collectors.nvidia_blog:NvidiaBlogCollector
    params: {max_age: 7}
  saso:                    # P1 新源（Mojin）
    enabled: false
    module: intel.collectors.saso:SasoCollector
    params: {delay_min: 1.0, delay_max: 3.0}

storage:
  db_path: data/briefnexus.db
  dedup_ttl_days: 90
```

- **转换策略**：API 层启动时读 platform_config.yaml，动态 import 采集器模块（`importlib`）
- 现有 intel/sources.yaml 保留向后兼容（CLI 继续可用），新平台只认 platform_config.yaml
- standards 的 ini 不动（standards 是检索库场景，非通用采集）

---

## 6. 部署形态

- **systemd user service**（与 mojin-mcp 同模式）：
  `briefnexus-api.service`（uvicorn :9000）+ worker 线程内嵌同进程（简单，不拆两个服务）
- 依赖：venv（`~/venvs/briefnexus/` 或项目内 .venv）、requests、fastapi、uvicorn、pyyaml
- healthcheck cron 每小时（复用 mojin 模式：失败飞书告警）
- 开机自启 + Restart=always

---

## 7. 项目落地清单（P0-P2）

### P0：API 骨架 + 透出现有源（今天，reasonix 执行）

- [ ] `platform/` 目录：`app.py`（FastAPI）+ `scheduler.py`（worker）+ `db.py`（表结构，WAL）+ `platform_config.yaml`
- [ ] 接口 6 个：/collect（异步）+ /collect/sync + /tasks/{id} + /tasks/{id}/items（分页+consume）+ /tasks/{id}/cancel + /tasks/{id}/retry + /sources + /healthz（含 worker 心跳）
- [ ] NewsItem 泛化（+type +raw_data）透传
- [ ] 采集器 PARAM_SCHEMA 校验（未声明宽松）
- [ ] worker：心跳 / 超时 300s / 重试 3 次退避 / 错误分类（rate_limited|network|source_empty|internal）/ 每源全局锁
- [ ] 透出 intel 现有 8 源（white_house/nvidia/sec_edgar/eu_commission/arxiv_perception/adas_vehicle_intl/eastmoney/cninfo）
- [ ] 测试：提交任务 → 轮询 → 结果 JSON 正确；取消/重跑；分页 consume；失败源重试；WAL 并发写
- [ ] systemd 部署 + healthcheck cron

### P1：Mojin 新源适配器（半天）

- [ ] `saso.py` — 沙特 SASO/SABER 合规要求采集（官方源）
- [ ] `competitor.py` — 竞品站产品/价格/配送承诺
- [ ] `faq_miner.py` — 竞品 FAQ + 平台问答 → 客户关心问题
- [ ] 知识转换脚本：采集结果 → 知识块 JSON → `mojin_update_kb.sh` 入库 → RAG

### P2：生产化（半天）

- [ ] 鉴权（Bearer token，本机可无）
- [ ] 限流（每 token / IP）
- [ ] 任务清理（>30 天 done 任务归档）
- [ ] OpenAPI 文档完善 + 示例 curl
- [ ] Webhook 回调（callback_url，任务完成 POST 通知）

---

## 8. 风险与对策

| 风险 | 影响 | 对策 |
|------|------|------|
| 反爬（418/403/封 IP） | 采集失败 | 每源限速 + UA 轮换（已有）；失败重试；必要时代理池 |
| 采集器阻塞 worker | 任务堆积 | 任务超时 300s + 每源独立线程池隔离 |
| 结果质量问题（错信息进 RAG） | 客服幻觉 | P1 知识转换层加**人工审核队列**（草稿区 → 确认 → 入库） |
| workers.dev 被墙（本机验证） | 本地测试受阻 | 平台访问外部走代理（环境变量），与 mojin 同模式 |
| 多项目抢占单源限速 | 互相拖累 | 每源全局锁（同一时间只跑一个任务） |

---

## 9. 演进性设计（采集内核持续更新不影响平台）

### 9.1 三层隔离（核心保证）

```
消费方（Mojin/trade-pulse/SceneCraft）
   ↑ HTTP 契约（永远不变）
API 层 + 调度层（FastAPI + 任务队列）
   ↑ 采集器契约（BaseCollector → NewsItem）
采集内核（intel/ + standards/）← 持续演进区
```

### 9.2 稳定契约 vs 可变实现

| 层 | 稳定（契约，不可破坏） | 可变（实现，可随意演进） |
|---|---|---|
| API | 路径 / 请求格式 / 任务状态机 / NewsItem 结构 | 内部实现 / 框架 / 协议升级（保持兼容） |
| 调度 | 任务生命周期 / 错误分类 / 分页 consume 语义 | 队列实现（SQLite→Redis）、线程数、超时参数 |
| 采集 | BaseCollector.crawl() 签名 / NewsItem 返回 | 单个采集器内部逻辑、反爬策略、新增源 |
| 存储 | items/tasks 表语义 | 具体 DB（SQLite→PG）、索引优化 |

### 9.3 演进场景验证

| 场景 | 影响 | 操作 |
|---|---|---|
| 加新采集器（saso.py） | 零 | 新文件 + @register + 配置条目 |
| 修现有采集器（eastmoney 反爬） | 仅该源 | 改单文件，重启服务 |
| 换采集内核（requests→Playwright） | 零 | 保持 crawl() 契约，内部重写 |
| 加新内容类型（标准元数据） | 零 | NewsItem type=standard + raw_data |
| 换队列（SQLite→Redis） | 零 | 调度层实现替换，API/消费方不变 |

### 9.4 约束（开发新采集器时的红线）

1. 必须继承 BaseCollector 并实现 crawl() → List[NewsItem]
2. 必须声明 PARAM_SCHEMA（新增采集器强制）
3. 结构化数据放 raw_data（不塞 summary）
4. 不得在采集器内直接访问平台 DB（结果由调度层落库）

---

## 10. 与现有项目的衔接

| 消费方 | 用法 |
|--------|------|
| Mojin RAG | `POST /collect` 采竞品/合规 → 转换脚本 → 知识块 → 客服知识库 |
| trade-pulse | `POST /collect` 采行业数据 → 因子/信号分析（未来） |
| SceneCraft | `POST /collect` 采短剧行业动态 → 选题调研（未来） |
| BriefNexus 自身 | CLI 保留（standards/intel），平台是超集 |

---

## 11. 待决策清单（用户拍板）

- [x] **D1**：结果统一 NewsItem 协议（泛化版 +type +raw_data）——已定（2026-08-13）
- [x] **D2**：SQLite + 线程池（不引 Redis/Celery）——已定
- [x] **D3**：新平台用 platform_config.yaml 统一配置，现有 CLI 配置保留——已定
- [x] **D4**：P0 只透出 intel 现有源，standards/SAMR 暂不暴露——已定
- [x] **D5**：部署单进程（API+worker 同进程）——已定
- [x] **D6**：P0 无鉴权（本机）——已定
- [x] **D7**：NewsItem 泛化（type/raw_data）+ 演进性设计三层隔离——已定
- [x] **D8**：P0 工程细节（WAL 并发写 / cancel-retry / 分页 consume / PARAM_SCHEMA / worker 心跳）——已定
- [ ] **待评审**：本选型书整体确认后写 P0 任务书交 reasonix

---

## 12. 参考

- 现有代码：`intel/core/`（base/registry/dedup）、`standards/crawler/utils.py`
- 同模式先例：mojin-mcp（FastAPI + systemd + healthcheck cron）
- 任务书：本文件评审通过后，按 P0 清单写 `platform-P0-任务书.md` 交 reasonix

---

## 13. 平台净化 + 解耦决策（2026-08-13 追加）

**背景**：平台定位升级为「公共信息采集平台」——只留存采集器 + 平台数据，不包含任何业务项目代码/数据；消费方只走 HTTP 接口，零代码耦合。

### 决策 D9：平台纯净性原则
- 仓库只含采集内核 + 平台服务层 + 平台自身数据
- README/代码注释不引用任何业务项目名
- plan/（选型书/任务书）与 memory/ 本地不入库（gitignore），允许业务上下文
- 消费方通过 HTTP 接口访问，平台不感知消费方身份

### 决策 D10：standards 服务化（决策 1 选 C）
- standards 保留在平台内，SAMR 标准采集包装为平台采集器（`source=samr`）
- 领域配置中性化：`standards_config.ini` 改通用默认；原照明配置备份到 `standards/configs/照明与智能灯具.ini`
- 任何消费方可通过 `POST /collect {"source":"samr"}` 采标准，CLI 保留为内部工具

### 决策 D11：跨任务去重引用（P0，真 bug 修复）
- 原全局 dedup 导致：B 项目采 A 已采过的源 → 全部跳过 → B 拉不到数据
- 修复：items 加跨任务引用——dedup_key 已存在时，新任务复制引用该条（新 task 的 items 可见），全局去重只防重复采集不阻断多任务消费

### 决策 D12：外部采集器挂载（P0，解耦核心）
- `platform_config.yaml` 加 `collectors_extra_dirs`：扫描外部目录加载采集器（业务项目自维护适配器）
- 支持 `config.d/*.yaml` 配置片段合并（平台内置配置 + 外部片段）
- 平台仓对外部项目零改动

### 决策 D13：数据清理（P1）
- worker 每日清理：>90 天已消费 items 删除 + >30 天完成任务归档
- 防止公共平台数据无限膨胀

### 决策 D14：API 版本化（P1）
- 路由前缀 `/v1`（/v1/collect、/v1/tasks/...），v2 可共存
- FastAPI /docs 交互文档开启

### 决策 D15：源级健康状态（P2）
- /sources 返回每源最近任务统计（成功/失败/耗时/最后采集时间），source_stats 表由 worker 更新

---

## 14. 源即参数（方案 C，2026-08-13 拍板）

**背景**：平台从「内置源」升级为「纯执行框架」——源随请求传入，平台零内置源。

### 决策 D16：源作为请求参数（方案 C）
- `POST /v1/collect` body 增加 `collector` 字段：
  - `collector.module` — 模块引用（`module:ClassName`，调用方部署代码到 collectors_extra_dirs）
  - `collector.code` — 代码内联（Python 源码，本机可用，公网需 ALLOW_INLINE_CODE）
- `source` 变可选别名；查不到时 422 提示「源未注册，请用 collector 字段传入」
- platform_config.yaml 清空内置 sources（8 源退役为测试样例）

### 决策 D17：8 源退役为测试样例
- 现有 8 适配器文件保留在 intel/collectors/（作测试/示例），平台不再内置注册
- 测试用例改为用这些源验证（fake 或真实）
- DB 已有数据保留（白宫 10 条/巨潮 20 条作示例数据）

### 决策 D18：调试支持
- tasks 表加 `traceback` 列：采集器异常时存完整堆栈，GET /tasks/{id} 返回
- 采集器 stderr/logging 捕获随任务返回

### 决策 D19：代码 hash 缓存
- collector.code 取 SHA256，collector_cache 表缓存编译结果，同 hash 复用
- 统计源活跃度（last_used/success_count）

### 决策 D20：内联代码安全护栏
- exec 前 AST 静态检查：禁 os/subprocess/socket/shutil import + eval/exec/open 调用
- 命中 → 422 拒绝；本机 ALLOW_INLINE_CODE=true 才开内联
- 纯采集逻辑（requests/bs4/json）不受影响

### 决策 D21：/sources 动态发现 + 模板脚手架 + 无源测试
- /sources 返回 config.d 挂载 + 最近 30 天活跃源（含 last_used/success_count）
- POST /v1/template 返回采集器骨架（降低接入成本）
- 测试：零源时 source-only 请求 422 + collector 传入正常执行

---

## 15. 内联代码安全边界（2026-08-13 终审决策 D22）

**背景**：ocr 终审发现纯 Python exec 沙箱存在理论逃逸路径
（`().__class__.__base__.__subclasses__()` 反射、链式别名、注入模块 `__dict__` 拿真实 builtins）。
这是所有纯 Python 沙箱的通病，AST 静态检查无法根治。

**决策 D22：方案 1+3 组合（用户拍板）**
- **对外正式路径 = collector.module 引用**（调用方自管代码，部署到 collectors_extra_dirs，平台不执行任意代码）——安全
- **collector.code 内联保留但标注实验性**：仅限受信本机调用方，ALLOW_INLINE_CODE 默认关闭
- 不砍内联（保留本机调试工具），不做子进程隔离（复杂度不划算，风险已知且有开关保护）
- 文档明确标注（README/模板/配置注释）；未来要公开代码执行能力再评估子进程隔离（multiprocessing 隔离）

**风险接受**：内联执行仅在本机开启时可用，且调用方为本机受信进程；对外 API 默认拒绝内联（422）。
