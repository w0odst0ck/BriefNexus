# BriefNexus

> 采集能力即服务，数据永不出门。

---

## 一、定位

可挂载的 HTTP 采集服务：内置采集器 + 路由表，任何应用即连即用，**代码不耦合、数据不出调用方项目**。

```
消费方（任意应用） ── HTTP ──▶ BriefNexus :9000
                              任务队列 → 采集内核 → 内存态（零持久化）
```

**三条原则**

1. **基建产品**：HTTP 接口，加源不改内核（`collectors_extra_dirs` 外挂）
2. **完全解耦**：平台仓 public，不含业务代码/数据；业务源走客户端挂载（铁律）
3. **轻量**：纯 Python + FastAPI，无数据库引擎、无临时文件，单进程可跑

---

## 二、状态

| 版本 | 范围 | 状态 |
|---|---|---|
| **V1** | HTTP API + 任务队列 + 内存态 + 采集器外挂 | ✅ 已上线（commit 3274410） |
| **V2** | 采集能力升级（渲染/声明式抽取/网络层/正文抽取） | 待启动，见 `方案/V2-采集能力升级.md` |
| **V3** | 平台层升级（认证/多实例/SDK/路由管理） | 未规划 |

**能力缺口**：无 `HTML_SPEC`、渲染每请求 2-17s（无池化）、代理未接、正文抽取缺库。详见 `方案/采集能力.md`。

**近期进展（2026-11-17~20）**

- 源头重分类：`research-collectors/sources.yaml` 26 源分 `scheduled`/`manual`/`archive`，支持 `daily`/`weekly`/`monthly` 节奏（commit `c11e106`）
- `pull.py` 支持 `--mode`/`--source`/`--dry-run`；`cron_daily_snapshot.sh` 白名单空时 no-op 不告警
- healthcheck 修假绿灯：白名单空期不误报，零产出源可见化（commit `446519b`）
- 已确认缺口：X / Instagram 无现成通道（MediaCrawler 不支持，RSSHub 需 token）→ 方案见 `research-collectors/plan/X图集采集方案.md`

---

## 三、API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/collect` | 异步提交 → `{task_id}` |
| POST | `/v1/collect/sync` | 同步执行（<30s） |
| GET | `/v1/tasks/{id}` | 任务状态 |
| GET | `/v1/tasks/{id}/items` | 拉结果（`?offset&limit&consume`） |
| POST | `/v1/tasks/{id}/cancel` / `retry` | 取消 / 重跑 |
| GET | `/v1/sources` | 可用源 |
| GET | `/v1/healthz` | 健康检查 |

提交：`{"source": "name", "params": {}, "callback_url": "..."}` 或 `{"collector": {"module": "pkg.mod:Class"}}`

**状态机**：`pending → running → done | failed | cancelled`
**错误分类**：`rate_limited` / `network` / `source_empty` / `internal` / `callback_failed`

结果字段：`title/url/summary/source/domain/sector/type/date_str/raw_data`
`type`：`news` / `doc` / `table` / `standard` / `image`

---

## 四、存储语义

- **零持久化**：只在进程堆内存，重启即清
- **TTL** 3600s，任务上限 1000（超限 429），单任务条目上限 10000
- **交付即清**：同步响应/回调成功/消费完 → 释放
- **单任务内去重**：`(title, url)` MD5，不跨任务

---

## 五、采集器分流

| 维度 | 内置（`intel/collectors/`） | 外挂（业务项目 `collectors_extra_dirs`） |
|---|---|---|
| 通用性 | 多项目复用 | 业务特定 |
| 机密性 | 公开 | **涉业务机密必须外挂（铁律）** |
| 维护 | 平台管 | 业务自维护 |

现有：内置 9 个（8 注册 + 1 死代码）、外挂 4 个（在 `research-collectors`）。

---

## 六、运行

```bash
.venv/bin/python platform/run.py          # 必须单 worker（内存态）
curl -X POST :9000/v1/collect -d '{"source":"x"}'
```

- 服务：`systemctl --user status briefnexus-api`（:9000）
- 测试：`cd /tmp && pytest <repo>/platform/tests <repo>/intel/tests`（**必须 cd /tmp**，否则 `platform` 包遮蔽标准库）
- 巡检：`脚本/briefnexus_healthcheck.sh`（每小时 cron）

---

## 七、文档索引

| 文档 | 内容 |
|---|---|
| `README.md` | 本文档 — 定位/状态/API/存储/分流/运行 |
| `产品/产品核心目标.md` | 对内北极星（唯一真相源） |
| `产品/产品书.md` | 对外口径 |
| `方案/V2-采集能力升级.md` | V2 五个能力包 + 验收标准 |
| `方案/采集能力.md` | 当前能力全景（2026-11-18 实测） |
| `架构/技术选型书.md` | V1 架构决策记录 |
| `归档/` | 历史任务书 + 早期规划（已完成，留档查证） |

> 文档约定：所有文档放 `doc/` 并入库；`plan/` 仅暂存未定稿草稿（gitignore）。新文档先问是否必要，避免膨胀。