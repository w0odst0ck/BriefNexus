# BriefNexus 平台净化编码任务书：去重引用 + 外部挂载 + config.d + 清理 + 版本化 + SAMR 服务化

> 2026-08-13 ｜ 依据：`plan/通用采集平台-技术选型书.md` D9-D15
> 你是执行者，禁止嵌套调用任何 AI coding 工具，直接完成任务。

## 背景

BriefNexus 定位升级为「公共信息采集平台」。P0 已上线（platform/ 13 文件，37 测试）。
本次编码任务解决 6 个问题（对应选型书 D10-D14，D15 留 P2）：

1. **D11 跨任务去重引用（真 bug）**：现在 items 的 dedup_key 全局唯一，B 项目采 A 已采过的源 → 全部命中 dedup 跳过 → B 的任务 items_count=0 且拉不到数据
2. **D12 外部采集器挂载**：支持 `collectors_extra_dirs` 配置，从外部目录加载采集器（业务项目自维护适配器，平台仓零改动）
3. **D12b config.d 配置片段合并**：支持 `platform/config.d/*.yaml` 片段，启动时与 platform_config.yaml 合并
4. **D13 数据清理**：worker 每日清理 >90 天已消费 items + >30 天完成任务归档
5. **D14 API 版本化**：路由前缀 /v1，旧路径保持兼容（重定向或并存）
6. **D10 SAMR 服务化**：standards 的 SAMR 采集包装为平台采集器（source=samr），走统一 API

## 交付物（修改 platform/，intel/ 只加不改）

```
platform/
├── app.py               # 改：/v1 路由前缀 + 旧路径兼容
├── db.py                # 改：跨任务引用（add_item 复制引用逻辑）+ 清理函数 + source_stats 表预留
├── scheduler.py         # 改：每日清理任务 + 外部采集器实例化支持
├── config.py            # 改：config.d 片段合并 + collectors_extra_dirs 扫描
├── platform_config.yaml # 改：加 collectors_extra_dirs + config_d 说明
├── config.d/            # 新增：示例片段（README 说明）
└── tests/
    ├── test_api.py      # 改：/v1 路径
    └── test_scheduler.py# 改：去重引用/清理用例
intel/collectors/samr_standard.py  # 新增：SAMR 标准采集适配器（包装 standards 引擎）
```

**约束：不动 standards/ 现有代码**（SAMR 适配器 import 复用，不修改）；不破坏现有 37 测试（兼容性优先）。

## 详细需求

### 1. D11 跨任务去重引用（核心）

当前逻辑：`db.add_item(task_id, item)` 若 dedup_key 已存在 → 返回 False（跳过）。

**新逻辑**：dedup_key 已存在时，**复制引用**——在新任务的 items 表插入一条「引用记录」（同一 dedup_key，task_id=新任务，consumed=0），保证每个任务都能拉到自己的结果。

实现方式：
- `add_item(task_id, item)` 返回 True（本次新增）或 `'ref'`（跨任务引用）或 False（同任务内重复）
- 引用记录：完整复制 item 内容（title/url/summary/...）但标记 `is_ref=1`？还是只存 dedup_key 关联？
  - **选完整复制**（简单可靠）：items 表加 `is_ref INTEGER DEFAULT 0` 列，引用记录复制全部字段 + is_ref=1
  - 理由：消费方拉取逻辑不变（不用 join），数据冗余可接受（同一源内容不会巨大）
- 任务 items_count 统计：新增 + 引用都计入

### 2. D12 外部采集器挂载

`platform_config.yaml` 新增：
```yaml
collectors_extra_dirs:
  # - /path/to/external/collectors   # 外部适配器目录（可选，可多个）
```

- `config.py`：启动时扫描每个 extra_dir 下的 `*.py`，`importlib` 导入（模块名冲突加前缀避免污染）
- 外部目录的采集器通过 config.d 片段声明（见下）
- 无外部目录时行为不变

### 3. D12b config.d 片段合并

- 新增 `platform/config.d/` 目录（不存在则创建）
- 启动时：读 `platform_config.yaml` + `config.d/*.yaml` 全部片段，**深合并**（sources 字典合并，server/storage 以主配置为准）
- 片段格式与主配置 sources 节一致
- 示例片段 `platform/config.d/README.md`（说明用法，不启用的示例）

### 4. D13 数据清理

- `db.py` 新增 `cleanup(consumed_ttl_days=90, task_archive_days=30)`：
  - 删除 `consumed=1 且 created_at < now-90天` 的 items
  - 归档（标记 archived 或删除）`status in (done,failed,cancelled) 且 finished_at < now-30天` 的任务及其 items
  - 返回清理统计 {items_deleted, tasks_archived}
- `scheduler.py`：worker 启动后每日执行一次（记录 last_cleanup 时间戳，重启后当天不再跑）

### 5. D14 API 版本化

- 所有路由加 `/v1` 前缀：`/v1/collect`、`/v1/collect/sync`、`/v1/tasks/{id}`、`/v1/tasks/{id}/items`、`/v1/tasks/{id}/cancel`、`/v1/tasks/{id}/retry`、`/v1/sources`、`/v1/healthz`
- **旧路径（无 /v1）保留**：返回 301 重定向到 /v1（或直接同 handler 并存，选简单：同 handler 并存，`include_router(router, prefix="/v1")` + 无前缀版）
- 测试更新为 /v1

### 6. D10 SAMR 服务化

新增 `intel/collectors/samr_standard.py`：
- 继承 BaseCollector，`source_name="samr"`，`display_name="全国标准信息公共服务平台"`，domains=["standards"]
- `PARAM_SCHEMA = {"keyword": {"type":"str"}, "ics": {"type":"str"}, "max_pages": {"type":"int","min":1,"max":20}}`
- crawl() 实现：复用 `standards/crawler/platforms/samr.py` 的 `SamrCollector`（import 实例化，传入 config）
  - 读取 `standards/standards_config.ini` 的 keywords/ics 作为默认参数，任务 params 可覆盖
  - 采集结果 → NewsItem（type="standard"，raw_data 放标准元数据：standard_no/title/category/status/publish_date/ics_code）
  - 注意：standards 的 SamrCollector.search_by_keyword 返回 dict 列表，需要转 NewsItem
- `platform_config.yaml` 注册 samr 源（enabled=true，默认参数 keyword=空 用 config 默认）

### 验收标准

1. `.venv/bin/pytest platform/tests/ -q` 全过（原 37 + 新用例）
2. `.venv/bin/ruff check platform/ intel/collectors/samr_standard.py` 干净
3. 跨任务引用实测：源 A 采集 → 源 B 再采同源 → B 任务 done 且 items_count>0（能拉到数据）
4. config.d 实测：放一个片段 → 重启 → /v1/sources 出现新源（或主配置源参数被片段覆盖）
5. 清理实测：插入一条 100 天前 consumed item → 跑 cleanup → 被删除
6. /v1 路径实测：/v1/collect 正常 + 旧 /collect 也可用
7. samr 实测：POST /v1/collect {"source":"samr"} → done + items 为标准条目（type=standard，raw_data 含 standard_no）
8. git status 只新增/修改上述文件（intel/ 只有 samr_standard.py 新增）

## 环境

- venv：项目 .venv（fastapi/uvicorn/pyyaml/requests/pytest/httpx 已装）
- 现有代码必读：`platform/app.py`（路由）、`platform/db.py`（add_item/去重）、`platform/config.py`（配置加载）、`platform/scheduler.py`（worker）、`standards/crawler/platforms/samr.py`（SAMR 采集器）、`standards/engine/storage.py`（标准入库，可选复用）
- 中文注释，风格对齐现有代码
