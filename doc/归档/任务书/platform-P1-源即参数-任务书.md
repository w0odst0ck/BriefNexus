# BriefNexus 平台任务书：源即参数（方案 C）+ 6 细节

> 2026-08-13 ｜ 依据：`plan/通用采集平台-技术选型书.md` D16-D21
> 你是执行者，禁止嵌套调用任何 AI coding 工具，直接完成任务。

## 背景

平台升级为「纯执行框架」：源不再内置在平台配置，而是**随请求传入**。
`POST /v1/collect` 支持 `collector` 字段（模块引用或代码内联），平台只负责执行调度。
现有 8 个源退役为测试样例，不再注册为内置源。

## 交付物（修改 platform/，intel/ 零改动）

```
platform/
├── app.py                # 改：collect 请求支持 collector 字段；/v1/template 新端点；/sources 动态发现
├── scheduler.py          # 改：_instantiate 支持 module/code 两种规格；traceback 捕获
├── db.py                 # 改：tasks 加 traceback/collector_spec 列；collector_cache 表；活跃源统计
├── config.py             # 改：内联代码 AST 安全检查（security.py 或内置函数）
├── platform_config.yaml  # 改：sources 清空为 {}（8 源退役）；加 ALLOW_INLINE_CODE 说明
├── templates/            # 新增：collector.py 采集器骨架模板
└── tests/
    ├── test_api.py       # 改：无源 422 + collector module/code 用例 + /template
    └── test_scheduler.py # 改：traceback 捕获 + 代码缓存
```

**约束：intel/ 零改动**（8 源适配器文件保留但不再内置注册）；不破坏现有测试（除 sources 相关断言需适配）。

## 详细需求

### 1. 请求协议改造（D16）

`CollectRequest` 模型扩展：

```python
class CollectorSpec(BaseModel):
    module: str | None = None   # "module.path:ClassName"
    code: str | None = None     # 内联 Python 源码

class CollectRequest(BaseModel):
    source: str | None = None       # 可选别名（查配置，查不到 422）
    collector: CollectorSpec | None = None  # 源本体（module 或 code 二选一）
    params: dict | None = None
    domain: str | None = None
    callback_url: str | None = None
```

校验逻辑：
- `source` 有值 → 查 config（兼容旧行为，命中则用；**查不到 → 422** `{"detail": "源未注册: xxx，请用 collector 字段传入"}`）
- `collector` 有值 → 用之（module 或 code 二选一，都空或都填 → 422）
- 都空 → 422
- 任务存储：tasks 加 `collector_spec TEXT` 列（JSON 序列化 module/code/version）

### 2. 模块引用执行（D16 复用现有）

- `collector.module` 格式 `"path.to.module:ClassName"` → 复用 `config.resolve_collector_class()`
- 模块路径解析顺序：已加载模块 → sys.path → collectors_extra_dirs（P0.5 已支持挂载）

### 3. 代码内联执行（D16 + D20 安全）

- `collector.code` 为 Python 源码字符串
- **执行方式**：`compile(code, "<collector>", "exec")` → 在受限命名空间 exec（`__builtins__` 保留，提供 `requests`/`bs4`/`json` 等常用模块注入）
- **AST 安全检查**（exec 前）：
  - 遍历 AST，禁止 `Import`/`ImportFrom` 导入黑名单模块：`os`/`subprocess`/`socket`/`shutil`/`sys`/`pathlib`/`ctypes`/`pickle`/`multiprocessing`/`threading`
  - 禁止 `Call` 调用 `eval`/`exec`/`compile`/`open`/`__import__`
  - 命中 → 422 `{"detail": "collector.code 含禁止操作: <名称>"}`
- **开关**：`ALLOW_INLINE_CODE` 环境变量（默认 false）；false 时 `collector.code` 直接 422 `{"detail": "代码内联未启用（ALLOW_INLINE_CODE=true 开启，仅限本机）"}`
- **代码缓存**（D19）：code 取 SHA256 → `collector_cache` 表（hash, code, compiled_pickle?, created_at）缓存编译结果；同 hash 复用
  - 简单实现：不持久化编译对象（pickle code 对象复杂），只缓存 hash→是否已通过检查 + 复用命名空间导入
  - 或：内存 dict 缓存（hash → 编译 module），进程重启失效可接受

### 4. 内联代码的采集器包装

内联代码的契约（与 BaseCollector 对齐）：
- 代码内必须定义 `class Collector(BaseCollector)`（继承 intel 基类）**或** `def crawl(sess) -> list[dict]` 顶层函数
- 平台检测：先看 `Collector` 类，再看 `crawl` 函数；都没有 → 422
- 函数式时自动包装：`lambda` 包装为最小采集器（source_name=code_hash 前 8 位）
- NewsItem dict 化：函数返回 `list[dict]`（title/url 必填，其余可选）→ `to_platform_item` 统一转换（已有）

### 5. traceback 捕获（D18）

- tasks 加 `traceback TEXT` 列
- scheduler `_run_task`：采集器实例化/crawl 异常时 `traceback.format_exc()` 存库
- `GET /v1/tasks/{id}` 返回 `traceback` 字段（非 null 时）
- 采集器 stderr 捕获：`contextlib.redirect_stderr(io.StringIO())` 包住 crawl 调用，输出存 `collector_log` 列（可选，与 traceback 同思路）

### 6. /sources 动态发现（D21）

`GET /v1/sources` 返回：
```json
[
  {"name": "my_ext_source", "module": "...", "active": true, "last_used": "...", "success_count": 5},
  {"name": "abc1234", "code_hash": "abc...", "active": true, "last_used": "...", "success_count": 2}
]
```
- config.d 挂载的源（静态）
- 最近 30 天 tasks 里有 collector_spec 的活跃源（动态，从 tasks 聚合 last_used/success_count）
- 平台内置源返回空（已退役）

### 7. /v1/template 脚手架（D21）

- `GET /v1/template?lang=python` 返回采集器骨架（templates/collector.py 内容，带注释）
- 纯静态文本返回，无逻辑

### 8. 8 源退役（D17）

- platform_config.yaml：`sources: {}`（或注释掉全部 8 源，留注释说明「内置源已退役，用 collector 传入」）
- intel/collectors/*.py **不动**（文件保留作测试/示例）
- 测试用例改用 fake_collectors（已有）+ 内联代码/模块引用方式；真实源测试（white_house 等）改为可选（环境变量 RUN_LIVE_TESTS=1 才跑）

### 9. 无源模式测试（D21）

- `sources: {}` 时 `POST /collect {"source":"white_house"}` → 422「源未注册」
- `POST /collect {"collector":{"module":"platform.tests.fake_collectors:OkCollector"}}` → 201 + done
- `POST /collect {"collector":{"code":"...def crawl..."}}`（ALLOW_INLINE_CODE=true 时）→ 201 + done
- `POST /collect {"collector":{"code":"import os..."}}` → 422 安全拦截

## 验收标准

1. `.venv/bin/pytest platform/tests/ -q` 全过（原 52 适配 + 新增）
2. `.venv/bin/ruff check platform/` 干净
3. 实测：零源模式启动 → source-only 422 / module 引用成功 / code 内联成功（本机开 ALLOW_INLINE_CODE）
4. 实测：内联代码抛异常 → 任务 failed + traceback 字段有内容
5. 实测：`import os` 内联 → 422 安全拦截
6. 实测：/v1/template 返回骨架；/v1/sources 动态显示活跃源
7. git status 只改上述文件（intel/ 零改动）

## 环境

- venv：项目 .venv
- 现有代码必读：`platform/app.py`（CollectRequest/路由）、`platform/scheduler.py`（_instantiate/_run_task）、`platform/db.py`（tasks 表/migration）、`platform/config.py`（resolve_collector_class）、`platform/tests/fake_collectors.py`（测试采集器）
- 中文注释，风格对齐
