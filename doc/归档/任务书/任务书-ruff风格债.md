# 任务书：ruff 风格债清扫（BriefNexus）

## 背景
- ruff 0.16.2 视角下 `./.venv/bin/ruff check intel/ platform/` 报 85 处错误（规则集默认 E4/E7/E9/F）
- 分类：50 F401（unused import）+ 19 E402（module import not at top）+ 8 E401（一行多 import）+ 3 E722（bare except）+ 2 E701（单行多语句）+ 1 F841 + 1 F541 + 1 F403
- 根因：存量风格债 + ruff 版本升级规则转正；**不是 bug**，不得改变任何运行时行为

## 目标
`./.venv/bin/ruff check intel/ platform/` 零错误；unittest 全绿；零行为变更

## 约束（红线）
1. **`intel/collectors/__init__.py` 的 import 是注册触发副作用（模块级 @register 机制），绝不能删**。处理：在 `intel/ruff.toml` 加 `lint.per-file-ignores` 排除该文件的 F401/E402，或在 import 行加 `# noqa: F401`——选更优者并说明理由
2. **不得无脑 --fix 全跑**（会把 __init__.py 注册 import 删掉）。正确顺序：① 先加 per-file-ignores 保护 → ② 对非 __init__.py 跑 `ruff check --fix`（安全修复 F401/E401/E701/F841/F541）→ ③ 手动修 E722（bare `except:` → `except Exception:`，语义不变）和 F403（star import 显式化）→ ④ 复验
3. 不许改动两个 ruff.toml 现有的 `lint.ignore`（BLE001/RUF012 是设计使然）；不许新增 ignore 掩盖问题（per-file-ignores 仅限注册模式 __init__ 文件）
4. E722：采集器兜底哲学允许盲捕，但语法上升级为 `except Exception:`，不得改为吞掉具体异常的 try 重构
5. 只清理 lint 债：不重构、不改业务逻辑、不碰与本任务无关的代码
6. 若某 F401 删除后运行期报错（延迟 import/副作用），恢复该 import 并改用 `# noqa` 注释

## 验收
1. `./.venv/bin/ruff check intel/ platform/` → 0 errors
2. `./.venv/bin/python -m unittest intel.tests.test_check_cmd intel.tests.test_render intel.tests.test_render_live` → 全绿（33 过、3 skip）
3. `timeout 150 ./.venv/bin/python -m intel run --domain finance` 正常跑完（东财/巨潮渲染源可用）
4. 报告：修改文件清单 + per-file-ignores 方案与理由

## 环境
- 仓库根：/home/l/.openclaw/workspace/projects/BriefNexus（先 cd 到这里再执行）
- ruff：`./.venv/bin/ruff`（0.16.2）；测试：`./.venv/bin/python -m unittest`
- **不要 git commit**（commit/push 由我完成）
