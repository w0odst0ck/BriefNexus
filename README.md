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
│  → 采集内核（intel/ + standards/）    │
│  → SQLite 存储                       │
└─────────────────────────────────────┘
```

**平台纯净性原则**：
- 只留存采集器 + 平台自身数据，**不包含任何业务项目的代码或数据**
- 消费方通过 HTTP 接口访问（提交任务 / 查状态 / 拉结果），**不知道也不关心采集器内部**
- 采集内核持续演进（加源/改源/换实现）不影响 API 与消费方
- 扩展采集器可在平台外挂载（见「扩展新采集器」），平台仓零改动

---

## ✨ 核心能力

| 能力 | 说明 |
|------|------|
| **HTTP 采集 API** | `POST /collect` 提交任务 → 异步执行 → `GET /tasks/{id}` 查状态/结果 |
| **8 个即用数据源** | 白宫/NVIDIA/SEC/欧盟/arXiv/ADAS/东方财富/巨潮资讯 |
| **异步任务队列** | SQLite + 线程池（不引 Redis），超时/重试/取消/每源锁 |
| **全局去重** | `(source, title)` MD5 + 跨任务引用，多消费方同时使用互不饿死 |
| **行业标准采集** | 国标元数据采集 + 全文检索（standards 模块，可选服务化） |
| **即插即用扩展** | 新源 = 一个适配器文件 + 配置条目，平台零改动 |

---

## 🚀 快速开始

### 1. 启动服务

```bash
cd ~/.openclaw/workspace/projects/BriefNexus
.venv/bin/python platform/run.py        # 或 systemd 服务（已配置开机自启）
# 服务监听 http://127.0.0.1:9000
```

### 2. 提交采集任务

```bash
# 异步提交（推荐）
curl -X POST http://127.0.0.1:9000/collect \
  -H 'Content-Type: application/json' \
  -d '{"source":"white_house","params":{"max_age":7}}'
# → {"task_id":"t_1786..."}

# 查状态
curl http://127.0.0.1:9000/tasks/t_1786...
# → {"status":"done","items_count":10,"error":null,...}

# 拉结果（分页 + consume 防重复消费）
curl "http://127.0.0.1:9000/tasks/t_1786.../items?limit=50&consume=1"

# 小任务同步执行（<30s 直接返回结果）
curl -X POST http://127.0.0.1:9000/collect/sync \
  -H 'Content-Type: application/json' -d '{"source":"cninfo"}'

# 看可用源
curl http://127.0.0.1:9000/sources

# 健康检查（含 worker 心跳）
curl http://127.0.0.1:9000/healthz
# → {"status":"ok","db":"ok","worker":"alive"}
```

### 3. 标准采集（standards CLI，可选）

```bash
.venv/bin/python -m standards.crawler.main stats     # 数据库统计
.venv/bin/python -m standards.crawler.main search "LED 筒灯"
.venv/bin/python -m standards.crawler.main list --category 国标 --date-from 2024
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

错误分类（`error` 字段）：`rate_limited`（反爬/限流）| `network`（超时/断连）| `source_empty`（源无数据）| `internal`（代码异常）

### 请求示例

```json
POST /v1/collect
{
  "source": "white_house",
  "params": {"max_age": 7},
  "domain": "self_driving"
}
```

### 结果协议（NewsItem，泛化）

```json
{
  "title": "Fact Sheet: ...",
  "url": "https://www.whitehouse.gov/...",
  "summary": "",
  "source": "White House",
  "domain": "finance",
  "sector": "",
  "type": "news",
  "date_str": "2026-08-12",
  "raw_data": {}
}
```

`type` 泛化：`news`（新闻）| `doc`（文档）| `table`（结构化表格）| `standard`（标准元数据）。
结构化数据放 `raw_data`，消费方按 type 分支处理。

---

## 🗂 数据源（8 个即用）

| 源 | 内容 | domain | 状态 |
|----|------|--------|------|
| `white_house` | 白宫简报室（科技/AI/贸易） | finance/self_driving/semiconductor | ✅ |
| `nvidia` | NVIDIA 官方博客 | semiconductor | ✅ |
| `sec_edgar` | 美国 SEC 文件 | finance | ✅ |
| `eu_commission` | 欧盟委员会动态 | finance/self_driving | ✅ |
| `arxiv_perception` | arXiv 感知/自动驾驶论文 | self_driving | ✅ |
| `adas_vehicle_intl` | ADAS/智能车辆国际动态 | self_driving | ✅ |
| `eastmoney` | 东方财富要闻（国内） | finance | ⚠️ 反爬（418） |
| `cninfo` | 巨潮资讯公告（国内） | finance | ✅ 已实采 20 条 |

> 新增源：见下方「扩展新采集器」。

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

### 方式二：外部挂载（平台外扩展，平台仓零改动）

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

**开发红线**：必须继承 BaseCollector 并实现 crawl()；必须声明 PARAM_SCHEMA；结构化数据放 raw_data（不塞 summary）；不得在采集器内直接访问平台 DB。

---

## 🗄 数据存储

- **平台库** `data/briefnexus.db`（SQLite，WAL 模式）：
  - `tasks` 表 — 任务状态/错误/重试计数
  - `items` 表 — 采集结果（dedup_key 全局去重 + consumed 消费标记 + 跨任务引用）
- **标准库** `standards/standards.db`：国标元数据（FTS5 全文索引 + ICS 分类树 + 采标 IEC 映射）
- **数据清理**：worker 每日清理过期数据（>90 天已消费 items + >30 天完成任务归档）

---

## 🔧 运维

| 项 | 说明 |
|----|------|
| systemd | `briefnexus-api.service`（:9000，Restart=always，开机自启） |
| healthcheck cron | 每小时 `脚本/briefnexus_healthcheck.sh`，失败飞书告警 |
| 日志 | `journalctl --user -u briefnexus-api -f` |
| 测试 | `.venv/bin/pytest platform/tests/ -q` |

> ⚠️ 注意：`platform/` 包名与 Python 标准库 `platform` 同名——启动/测试请走 `platform/run.py` 和项目根下的 pytest，不要裸 `uvicorn platform.app:app`。

---

## 📁 项目结构

```
BriefNexus/
├── platform/                  ← 平台服务层（HTTP API + 任务队列）
│   ├── app.py                FastAPI 入口（/v1 路由）
│   ├── scheduler.py          后台 worker（心跳/超时/重试/每源锁/清理）
│   ├── db.py                 SQLite WAL 存储（tasks/items + 跨任务引用）
│   ├── config.py             platform_config.yaml + config.d 片段合并 + 动态 import
│   ├── run.py                启动入口（规避包名冲突）
│   ├── platform_config.yaml  平台配置（源开关/参数/存储/外部目录）
│   └── tests/                测试用例
├── intel/                     ← 采集内核（情报采集）
│   ├── core/                 BaseCollector 基类 / 注册器 / 去重
│   ├── collectors/           数据源适配器（8 个）
│   ├── pipeline/             分类 / 报告
│   └── cli.py                命令行入口（run/list）
├── standards/                 ← 采集内核（行业标准）
│   ├── crawler/              SAMR/openstd/IEC 平台适配器 + 下载器
│   ├── engine/               SQLite FTS5 存储 / 去重 / 导出 / ICS 树
│   ├── configs/              领域配置示例（照明等）
│   └── standards.db          国标元数据
├── tools/                     工具
├── scripts/                   辅助脚本
├── plan/                      技术选型书 / 任务书（本地，不入库）
└── memory/                    项目日志（本地，不入库）
```

---

## 🔗 消费方接入指南

任何应用按以下三步接入：

1. **看源**：`GET /v1/sources` 了解可用采集器和参数
2. **采集**：`POST /v1/collect` 提交任务，轮询 `GET /v1/tasks/{id}` 至 done
3. **消费**：`GET /v1/tasks/{id}/items?consume=1` 分页拉取，转成自己的数据结构

平台不感知消费方身份——提交什么源、怎么消费结果，完全由调用方决定。

---

## 📋 Roadmap

- [x] **P0**（2026-08-13）：API 骨架 + 异步任务 + 8 源透出 + 部署
- [x] **P0.5**（2026-08-13）：跨任务去重引用 / 外部采集器目录 / config.d / 数据清理 / API 版本化 / SAMR 服务化
- [ ] **P1**：更多业务源适配器 / 人工审核队列（消费方侧）
- [ ] **P2**：鉴权 / 限流 / Webhook 回调 / 源级健康状态

---

## 📚 参考

- 技术选型书：`plan/通用采集平台-技术选型书.md`（架构决策 D1-D14）
- 标准采集模块：`standards/README.md`
- 情报采集模块：`intel/README.md`
