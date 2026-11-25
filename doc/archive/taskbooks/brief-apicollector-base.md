# Brief — APICollector 基类（C1 能力补全）

## 背景
- BriefNexus 能力矩阵 C1 采集层：静态抓取/RSS/CDP 渲染/PDF 解析/变更监控均已实现，
  唯一剩「JSON API 适配」为规划中——现状只有 arxiv_perception 一个特例实现，无通用基类
- 目标范式：能力先行——建好 APICollector 基类后，新增 JSON 源 = 加配置（非写代码），
  与 RSSHubCollector 对 RSS 的定位一致

## 目标
- 实现 `APICollector` 抽象基类（intel/core/ 或 intel/collectors/ 下，与 BaseCollector 兼容）：
  - 配置驱动：endpoint / auth（api_key 头、basic、无）/ 分页（cursor、offset、page）/ 字段映射（JSON path → NewsItem 字段）
  - 默认容错契约：超时/HTTP 错误/JSON 解析失败 → 返回空列表不抛异常（与现有采集器一致）
- 1 个验证实例：把 arxiv_perception 或 cninfo（POST API 分支）改造成 APICollector 子类，证明"JSON 源=配置"
- 单测 ≥10 用例（stub 隔离，不依赖真实网络）+ ruff 干净 + 现有 33+79 测试不破坏

## 约束
- 必须继承 BaseCollector，保持 crawl(sess) 契约（sess 可能是 RenderAwareSession 或 requests.Session）
- 零持久化哲学：平台不落盘，采集器只管返回 NewsItem
- PARAM_SCHEMA 声明全部参数；结构化数据放 raw_data
- 客户端挂载模式：通用能力可入 intel 内置，业务源留在客户端项目

## 红线
- 不碰平台存储/API 层（platform/ 只读不写）
- 不引入新第三方依赖（requests + stdlib 足够）
- 不改变现有采集器行为（验证实例除外，且需测试覆盖）

## 验收标准
1. `./.venv/bin/python -m unittest intel.tests.test_check_cmd intel.tests.test_render intel.tests.test_render_live` 全绿（33 过 3 skip）
2. `cd /tmp && pytest <BriefNexus>/platform/tests -q` 全绿（79 过，platform shadow 需 /tmp 跑）
3. APICollector 单测 ≥10 用例全绿 + ruff 零错误（0.16.2 默认规则集）
4. 验证实例真实跑通（arxiv API 或 cninfo POST API 分支）产出 NewsItem
5. `intel run -d <验证实例的域>` 正常

## 环境
- 仓库根：/home/l/.openclaw/workspace/projects/BriefNexus
- 测试：`./.venv/bin/python -m unittest ...`；pytest 必须 `cd /tmp` 跑（platform 包 shadow 标准库）
- ruff：`./.venv/bin/ruff`（0.16.2，默认规则集 413 条；per-file-ignores 已保护 collectors/__init__.py）
- 不要 git commit（commit/push 由青简完成）
