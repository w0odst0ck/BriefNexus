# BriefNexus — 通用信息采集平台

> 多源信息采集的统一平台。对内是采集内核，对外是 HTTP 服务。
> 任何需要数据的应用，通过接口即可复用采集能力——不共享代码，不耦合仓库。

---

## 🏗 平台定位

```
┌─────────────────────────────────────┐
│           消费方（任意应用）           │
│  通过 HTTP 接口调用，零代码耦合        │
└──────┬──────────────────────────────┘
       │ HTTP (JSON)
┌──────▼──────────────────────────────┐
│      BriefNexus 采集平台（服务层）     │
│  FastAPI :9000（API）→ 任务队列       │
│  → 采集内核（intel/）                 │
│  → 内存态存储（零磁盘数据文件）         │
└─────────────────────────────────────┘
```

**平台纯净性原则**：
- 只留存采集器 + 平台自身数据，**不包含任何业务项目的代码或数据**
- 消费方通过 HTTP 接口访问（提交任务 / 查状态 / 拉结果 / 收回调），**不知道也不关心采集器内部**
- 采集内核持续演进（加源/改源/换实现）不影响 API 与消费方
- 扩展采集器可在平台外挂载（见「扩展新采集器」），平台仓零改动

**采集器分流规则（数据源放哪）**：

| 维度 | 服务端内置（intel/collectors/） | 客户端挂载（业务项目 extra_dirs） |
|:--|:--|:--|
| 通用性 | 多项目可复用（公共情报源） | 业务特定（查询词/目标源是业务策略） |
| 机密性 | 无业务机密（本仓 public，内置=公开） | **涉业务机密（必须客户端，铁律）** |
| 归属 | 平台统一维护质量 | 业务自维护，改采集器不碰平台 |
| 发布 | 低频稳定，跟平台版本 | 独立迭代，随业务发布 |

**判断公式**：
```
通用 ? ──是──> 多项目用 ? ──是──> 服务端内置
   │              └──否──> 客户端（将来有第二项目复用再提升）
   └──否──> 涉业务机密 ? ──是──> 客户端（铁律）
                 └──否──> 看维护归属：业务自维护→客户端；平台管→服务端
```

> ⚠️ **平台仓 public**：业务项目的挂载配置片段（含本地路径/业务目录）**不入库**，通过 `.gitignore` 排除；平台只保留通用挂载机制文档（`platform/config.d/README.md`）。

---

## ✨ 核心能力

| 能力 | 说明 |
|------|------|
| **HTTP 采集 API** | `POST /collect` 提交任务 → 异步执行 → 查状态/拉结果/收回调 |
| **内存态存储** | 采集结果仅存进程内存，TTL 到期/交付/重启即清，无磁盘数据文件 |
| **三种交付模式** | 同步返回（`/collect/sync`）+ 回调 webhook（`callback_url`）+ 拉取（`/tasks/{id}/items`） |
| **异步任务队列** | 线程池（不引外部队列），超时/重试/取消/每源锁 |
| **单任务内去重** | `(title, url)` MD5，同一任务内重复条目自动跳过 |
| **即插即用扩展** | 新源 = 一个适配器文件 + 配置条目，平台零改动 |

---

## 🧩 能力矩阵（已实现 + 规划中）

> 原则：**能力先行，数据源是能力的实例**——建好能力层，加源 = 加配置。规划详见 `plan/调研工具箱-全量与TODO.md`。

| 层 | 能力 | 状态 | 说明 |
|:--|:--|:--|:--|
| C1 采集层 | 静态页面抓取 | ✅ 已实现 | BaseCollector（内置 9 源范式） |
| C1 采集层 | 健康巡检 | ✅ 已实现 | `intel check`（2026-08-15） |
| C1 采集层 | RSS/Atom 订阅 | 🚧 规划中 | RSSHub 本地实例已部署（:1200）；RSSHubCollector 接入中 |
| C1 采集层 | JSON API 适配 | 🚧 规划中 | APICollector 基类（arxiv 范式泛化） |
| C1 采集层 | 浏览器渲染（CDP） | 🚧 规划中 | chrome-devtools-mcp 集成（WAF/JS 源统一解法） |
| C1 采集层 | 文档/PDF 解析 | 🚧 规划中 | MinerU 集成（标准/政策全文检索） |
| C1 采集层 | 通用变更监控 | 🚧 规划中 | changedetection.io（部署中）＋快照 diff |
| C2 处理层 | 去重/分类 | ✅ 部分 | 去重 ✅；分类规则待补全 |
| C2 处理层 | LLM 增强 | 🚧 规划中 | 摘要/翻译/实体抽取（DeepSeek） |
| C3 交付层 | 报告（md/json） | ✅ 已实现 | 模板化待增强 |
| C3 交付层 | 变更预警推送 | 🚧 规划中 | diff 触发 → 飞书（feishu_push.py） |
| C3 交付层 | 调度 cron 化 | 🚧 规划中 | command payload 模式 |

---

## 🚀 快速开始

### 1. 启动服务

```bash
cd <项目目录>          # 替换为你的 BriefNexus 路径（下同）
.venv/bin/python platform/run.py        # 或 systemd 服务（已配置开机自启）
# 服务监听 http://127.0.0.1:9000
```

> 平台必须**单 worker 进程**运行：内存态存储只在单进程内可见，`uvicorn --workers>1` 会导致任务分散/丢失。

### 2. 提交采集任务

```bash
# 异步提交（推荐）
curl -X POST http://127.0.0.1:9000/collect \
  -H 'Content-Type: application/json' \
  -d '{"source":"<source>","params":{}}'
# → {"task_id":"t_1786..."}

# 查状态
curl http://127.0.0.1:9000/tasks/t_1786...
# → {"status":"done","items_count":10,"error":null,...}

# 拉结果（分页 + consume 防重复消费）
curl "http://127.0.0.1:9000/tasks/t_1786.../items?limit=50&consume=1"

# 小任务同步执行（<30s 直接返回结果）
curl -X POST http://127.0.0.1:9000/collect/sync \
  -H 'Content-Type: application/json' -d '{"source":"<source>"}'

# 回调模式（任务完成后 POST 结果到你的地址）
curl -X POST http://127.0.0.1:9000/collect \
  -H 'Content-Type: application/json' \
  -d '{"source":"<source>","callback_url":"https://your.service/cb"}'

# 看可用源
curl http://127.0.0.1:9000/sources

# 健康检查（含 worker 心跳）
curl http://127.0.0.1:9000/healthz
# → {"status":"ok","db":"ok","storage":"memory","worker":"alive"}
```

---

## 📡 API 参考（v1）

### 任务管理

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/v1/collect` | 提交异步任务 → 201 `{task_id}` |
| POST | `/v1/collect/sync` | 同步执行小任务（<30s）→ 直接返回结果 |
| GET | `/v1/tasks/{id}` | 任务状态（不含 items） |
| GET | `/v1/tasks/{id}/items` | 分页拉取结果：`?offset=&limit=&consume=` |
| POST | `/v1/tasks/{id}/cancel` | 取消（pending/running） |
| POST | `/v1/tasks/{id}/retry` | 失败任务重跑 |
| GET | `/v1/sources` | 列出可用源 + 参数说明 |
| GET | `/v1/healthz` | 健康检查（含 worker 心跳，超时 503） |

> 交互式文档：`http://127.0.0.1:9000/docs`（FastAPI Swagger UI）

### 任务状态机

```
pending → running → done | failed | cancelled
failed 可 /retry 重跑（自动重试 3 次，退避 5s/10s/20s）
超时 300s 强制 failed
```

错误分类（`error` 字段）：`rate_limited`（反爬/限流）| `network`（超时/断连）| `source_empty`（源无数据）| `internal`（代码异常）| `callback_failed`（回调投递失败）

### 请求示例

```json
POST /v1/collect
{
  "source": "<source>",
  "params": {},
  "domain": "<domain>",
  "callback_url": "https://your.service/cb"
}
```

### 结果协议（NewsItem，泛化）

```json
{
  "title": "...",
  "url": "https://...",
  "summary": "",
  "source": "...",
  "domain": "...",
  "sector": "",
  "type": "news",
  "date_str": "2026-08-12",
  "raw_data": {}
}
```

`type` 泛化：`news`（新闻）| `doc`（文档）| `table`（结构化表格）| `standard`（标准元数据）。
结构化数据放 `raw_data`，消费方按 type 分支处理。

---

## 📦 数据存储与生命周期（内存态）

- **零持久化**：采集结果只存在于进程堆内存（`dict`/`list`/`set`），**不写任何磁盘数据文件**——无数据库文件、无临时文件、无数据目录。进程重启/崩溃即全部清空。
- **TTL**：任务与结果默认保留 3600s（可配 `storage.ttl_seconds`），到期由惰性查询 + worker 主动清扫两层回收。
- **容量上限**：任务表默认上限 1000（超限 `POST /collect` 返回 **429**）；单任务条目默认上限 10000（超限截断并透出 `items_truncated=true`）。
- **交付即清**：结果一旦成功交付（同步响应 / 回调成功 / 拉取消费完）即释放，缩短敏感数据驻留窗口。
- **单任务内去重**：键为 `(title, url)` 的 MD5，仅在同一任务内生效，不跨任务去重。

### 三种交付模式

| 模式 | 入口 | 结果释放时机 |
|------|------|-------------|
| 同步返回 | `POST /v1/collect/sync` | 响应体返回后立即释放 |
| 回调 webhook | `POST /v1/collect` + `callback_url` | 回调成功后立即释放；失败丢弃结果 |
| 拉取 | `GET /v1/tasks/{id}/items?consume=1` | 全部消费完即释放（`free_on_full_consume=true`） |

### 回调契约

- 触发时机：采集完成（`status=done`）后，在**每源锁之外**独立线程池投递，不阻塞同源后续任务。
- 请求体：`{"task_id","status":"done","items_count","items":[...]}`，`Content-Type: application/json`。
- 超时：默认 10s（`callback.timeout_s`）。
- 重试：初次 + 最多 3 次重试，指数退避 `2s/4s/8s`（`callback.retry_delays`），共 4 次尝试。
- 成功判定：HTTP **2xx**。
- 失败处理：4 次尝试仍失败 → 任务置 `failed`、`error=callback_failed`，结果丢弃（平台零持久化）。
- **至少一次**语义：回调超时后重试可能导致重复投递，消费方应保证幂等。
- **`callback_url` 仅允许 `http`/`https`**，提交阶段校验 scheme（非法 422）；投递阶段 `allow_redirects=False`（不跟随重定向），防 SSRF 跳转放大。
  - 平台默认**不阻断私网 IP**（平台绑定 `127.0.0.1` 仅受信调用方 + 本机回环 stub），若未来对外暴露需另加 RFC1918/链路本地拦截（不在当前范围）。

> ⚠️ 同步 `/collect/sync` 超时（30s）后任务可能仍处于 `pending/running`，可用 `GET /v1/tasks/{id}/items` 在 TTL 窗口内拉取结果。

---

## 🧩 扩展新采集器

### 方式一：平台内置（推荐给通用源）

1. **写适配器**（继承 `BaseCollector`）：

```python
# intel/collectors/my_source.py
from intel.core.base import BaseCollector, NewsItem
from intel.core.registry import register

@register("my_source")
class MySourceCollector(BaseCollector):
    source_name = "mysource"
    display_name = "My Source"
    domains = ["my_domain"]
    PARAM_SCHEMA = {"max_age": {"type": "int", "min": 1, "max": 90}}

    def crawl(self, sess) -> list[NewsItem]:
        # 采集逻辑 → 返回 NewsItem 列表
        return [NewsItem(title="...", url="...", type="news")]
```

2. **注册到平台配置** `platform/platform_config.yaml`：

```yaml
sources:
  my_source:
    enabled: true
    module: intel.collectors.my_source:MySourceCollector
    params: {max_age: 7}
```

3. **重启服务** → `GET /v1/sources` 即可看到，`POST /v1/collect` 即可调用。

### 方式二：外部挂载（平台外扩展，平台仓零改动，推荐生产使用）

平台支持从**外部目录**加载采集器（`collectors_extra_dirs` 配置），
业务项目可在自己仓库内维护适配器，无需改动平台：

```yaml
# platform/platform_config.yaml
collectors_extra_dirs:
  - /path/to/your/project/collectors   # 你的项目自己的适配器目录
```

```yaml
# /path/to/your/project/collectors/my_source.yaml（config.d 片段）
sources:
  my_source:
    enabled: true
    module: your_project.collectors.my_source:MySourceCollector
```

**开发红线**：必须继承 BaseCollector 并实现 crawl()；必须声明 PARAM_SCHEMA；结构化数据放 raw_data（不塞 summary）；不得在采集器内直接访问平台存储。

> ⚠️ **代码内联（collector.code）为实验性功能**：仅限受信本机调用方，默认关闭（ALLOW_INLINE_CODE=true 才启用）。
> 生产/对外请使用 **collector.module 引用**（调用方自管代码，部署到 collectors_extra_dirs）——平台不执行任意代码，更安全。

---

## 🔧 运维

| 项 | 说明 |
|----|------|
| systemd | `briefnexus-api.service`（:9000，Restart=always，开机自启） |
| healthcheck cron | 每小时 `脚本/briefnexus_healthcheck.sh`，失败飞书告警 |
| 日志 | `journalctl -u briefnexus-api -f` |
| 测试 | `.venv/bin/pytest platform/tests/ -q` |

> ⚠️ 注意：`platform/` 包名与 Python 标准库 `platform` 同名——启动/测试请走 `platform/run.py` 和项目根下的 pytest，不要裸 `uvicorn platform.app:app`。

---

## 📁 项目结构

```
BriefNexus/
├── platform/                  ← 平台服务层（HTTP API + 任务队列）
│   ├── app.py                FastAPI 入口（/v1 路由）
│   ├── scheduler.py          后台 worker（心跳/超时/重试/每源锁/回调投递/TTL 清扫）
│   ├── memory_store.py       内存态存储（任务/结果/去重/缓存，零落盘）
│   ├── delivery.py           回调交付（webhook POST + 重试/超时/SSRF 护栏）
│   ├── config.py             platform_config.yaml + config.d 片段合并 + 动态 import
│   ├── run.py                启动入口（规避包名冲突）
│   ├── platform_config.yaml  平台配置（源开关/参数/存储/回调/外部目录）
│   └── tests/                测试用例
├── intel/                     ← 采集内核（情报采集）
│   ├── core/                 BaseCollector 基类 / 注册器 / 去重
│   ├── collectors/           数据源适配器
│   ├── pipeline/             分类 / 报告
│   └── cli.py                命令行入口（run/list）
├── tools/                     工具
├── scripts/                   辅助脚本
├── plan/                      技术选型书 / 任务书（本地，不入库）
└── memory/                    项目日志（本地，不入库）
```

---

## 🔗 消费方接入指南

任何应用按以下步骤接入：

1. **看源**：`GET /v1/sources` 了解可用采集器和参数
2. **采集**：`POST /v1/collect` 提交任务（可选带 `callback_url` 收回调），轮询 `GET /v1/tasks/{id}` 至 done
3. **消费**：`GET /v1/tasks/{id}/items?consume=1` 分页拉取，转成自己的数据结构

平台不感知消费方身份——提交什么源、怎么消费结果，完全由调用方决定。

---

## 📋 Roadmap

- [x] **P0**：API 骨架 + 异步任务 + 部署
- [x] **P0.5**：外部采集器目录 / config.d / API 版本化
- [x] **内存态改造**：零持久化 + 三种交付模式（sync/callback/pull）+ TTL/容量
- [ ] **P1**：更多业务源适配器 / 人工审核队列（消费方侧）
- [ ] **P2**：鉴权 / 限流 / 源级健康状态

---

## 📚 参考

- 技术选型书：`plan/通用采集平台-技术选型书.md`（架构决策 D1-D14）
- 情报采集模块：`intel/README.md`
