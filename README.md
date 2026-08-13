# BriefNexus — 通用信息采集平台

> 多源信息采集 + 行业标准检索的统一平台。对内是采集内核，对外是 HTTP 服务。
> 一套采集能力，多项目复用（Mojin RAG / trade-pulse / SceneCraft / 任何需要数据的项目）。

---

## 🏗 平台定位

```
┌────────────────────────────────────────────────────┐
│                   消费方（多项目）                    │
│  Mojin RAG  │  trade-pulse  │  SceneCraft  │ 未来   │
└──────┬─────────────────────────────────────────────┘
       │ HTTP (JSON)
┌──────▼─────────────────────────────────────────────┐
│            BriefNexus 采集平台（服务层）              │
│  FastAPI :9000（API）→ 任务队列 → 采集内核 → SQLite   │
└────────────────────────────────────────────────────┘
```

**三层隔离设计**：采集内核持续演进（加源/改源/换实现）不影响 API 与消费方。
稳定契约 = HTTP 路径 + 任务状态机 + NewsItem 结构；可变实现 = 单个采集器内部逻辑、队列实现、存储引擎。

---

## ✨ 核心能力

| 能力 | 说明 |
|------|------|
| **HTTP 采集 API** | `POST /collect` 提交任务 → 异步执行 → `GET /tasks/{id}` 查状态/结果 |
| **8 个即用数据源** | 白宫/NVIDIA/SEC/欧盟/arXiv/ADAS/东方财富/巨潮资讯 |
| **异步任务队列** | SQLite + 线程池（不引 Redis），超时/重试/取消/每源锁 |
| **全局去重** | `(source, title)` MD5，重复采集自动跳过，消费幂等（consume） |
| **行业标准库** | 430 条照明/汽车照明国标 + SQLite FTS5 全文检索（standards 模块） |
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

### 3. 查标准库（standards CLI）

```bash
.venv/bin/python -m standards.crawler.main stats     # 数据库统计
.venv/bin/python -m standards.crawler.main search "LED 筒灯"
.venv/bin/python -m standards.crawler.main list --category 国标 --date-from 2024
.venv/bin/python -m standards.crawler.main tree 29.140.40
```

---

## 📡 API 参考（v1）

### 任务管理

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/collect` | 提交异步任务 → 201 `{task_id}` |
| POST | `/collect/sync` | 同步执行小任务（<30s）→ 直接返回结果 |
| GET | `/tasks/{id}` | 任务状态（不含 items） |
| GET | `/tasks/{id}/items` | 分页拉取结果：`?offset=&limit=&consume=` |
| POST | `/tasks/{id}/cancel` | 取消（pending/running） |
| POST | `/tasks/{id}/retry` | 失败任务重跑 |
| GET | `/sources` | 列出可用源 + 参数说明 |
| GET | `/healthz` | 健康检查（含 worker 心跳，超时 503） |

### 任务状态机

```
pending → running → done | failed | cancelled
failed 可 /retry 重跑（自动重试 3 次，退避 5s/10s/20s）
超时 300s 强制 failed
```

错误分类（`error` 字段）：`rate_limited`（反爬/限流）| `network`（超时/断连）| `source_empty`（源无数据）| `internal`（代码异常）

### 请求示例

```json
POST /collect
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

## 🧩 扩展新采集器（10 分钟）

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

3. **重启服务** → `GET /sources` 即可看到，`POST /collect` 即可调用。

**开发红线**：必须继承 BaseCollector 并实现 crawl()；必须声明 PARAM_SCHEMA；结构化数据放 raw_data（不塞 summary）；不得在采集器内直接访问平台 DB。

---

## 🗄 数据存储

- **平台库** `data/briefnexus.db`（SQLite，WAL 模式）：
  - `tasks` 表 — 任务状态/错误/重试计数
  - `items` 表 — 采集结果（dedup_key 全局去重 + consumed 消费标记）
- **标准库** `standards/standards.db`：430 条国标（FTS5 全文索引 + ICS 分类树 + 采标 IEC 映射）

---

## 🔧 运维

| 项 | 说明 |
|----|------|
| systemd | `briefnexus-api.service`（:9000，Restart=always，开机自启） |
| healthcheck cron | 每小时 `脚本/briefnexus_healthcheck.sh`，失败飞书告警 |
| 日志 | `journalctl --user -u briefnexus-api -f` |
| 测试 | `.venv/bin/pytest platform/tests/ -q`（37 用例） |

> ⚠️ 注意：`platform/` 包名与 Python 标准库 `platform` 同名——启动/测试请走 `platform/run.py` 和项目根下的 pytest，不要裸 `uvicorn platform.app:app`。

---

## 📁 项目结构

```
BriefNexus/
├── platform/                  ← 采集平台服务层（HTTP API + 任务队列）
│   ├── app.py                 FastAPI 入口（8 端点）
│   ├── scheduler.py           后台 worker（心跳/超时/重试/每源锁）
│   ├── db.py                  SQLite WAL 存储（tasks/items）
│   ├── config.py              platform_config.yaml 加载 + 动态 import
│   ├── run.py                 启动入口（规避包名冲突）
│   ├── platform_config.yaml   平台配置（源开关/参数/存储）
│   └── tests/                 37 用例
├── intel/                     ← 采集内核（情报采集）
│   ├── core/                  BaseCollector 基类 / 注册器 / 去重
│   ├── collectors/            数据源适配器（8 个）
│   ├── pipeline/              分类 / 报告
│   └── cli.py                 命令行入口（run/list）
├── standards/                 ← 采集内核（行业标准）
│   ├── crawler/               SAMR/openstd/IEC 平台适配器 + 下载器
│   ├── engine/                SQLite FTS5 存储 / 去重 / 导出 / ICS 树
│   └── standards.db           430 条国标
├── tools/                     工具（locator 等）
├── scripts/                   辅助脚本
├── plan/                      技术选型书 / 任务书（本地，不入库）
└── memory/                    项目日志
```

---

## 🔗 与业务项目衔接

| 消费方 | 用法 |
|--------|------|
| **Mojin RAG** | 采集竞品/合规知识 → 转换脚本 → 客服知识库（P1 规划中） |
| **trade-pulse** | 采集行业数据 → 因子/信号分析（未来） |
| **SceneCraft** | 采集短剧行业动态 → 选题调研（未来） |
| **BriefNexus CLI** | 标准检索/情报 CLI 保留，平台是超集 |

---

## 📋 Roadmap

- [x] **P0**（2026-08-13）：API 骨架 + 异步任务 + 8 源透出 + 部署
- [ ] **P1**：Mojin 新源（SASO 合规/竞品/FAQ 挖掘）+ 知识转换管道 + 人工审核队列
- [ ] **P2**：鉴权 / 限流 / 任务清理 / Webhook 回调 / OpenAPI 文档完善

---

## 📚 参考

- 技术选型书：`plan/通用采集平台-技术选型书.md`（架构决策 D1-D8）
- 标准采集模块：`standards/README.md`
- 情报采集模块：`intel/README.md`
