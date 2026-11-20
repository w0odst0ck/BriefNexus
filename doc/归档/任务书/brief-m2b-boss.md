# Brief — M2b：登录态注入（cookie）+ 详情页 JD 全文采集

> 依据：M2-BOSS采集-方案报告.md §5 M2b。前置：M2a 已 accepted（cbf6e00），无登录确认不通（安全验证页）；用户已导出 BOSS cookie（~/.config/boss/cookies.json，chmod 600），**cookie 有效性已实测**：带 cookie 请求搜索页 HTTP 200 无安全验证（2026-08-17 23:19 验证）。

## 背景

M2a 交付了参数化采集器，但无登录被安全验证拦截。用户已提供登录态 cookie（15 条，含 __zp_stoken__/wt2/zp_at 核心 token），实测可绕过验证。M2b 做两件事：①渲染链路支持 cookie 注入（平台 render 基建扩展）；②详情页 JD 全文采集 + 增量去重。

## 目标

```
cookie（~/.config/boss/cookies.json）──► render 注入（RenderAwareSession）
  ──► 列表页渲染出卡片（M2a 解析器复用）
  ──► 卡片链接 ──► 详情页二次渲染 ──► JD 全文提取（职位描述）
  ──► 增量去重（与历史 joblist.json 对比 link hash）
  ──► 产物: intel/data/boss/<date>/joblist.json（含 jd_full 字段）
```

## 约束

- **render.py 扩展（平台核心，最小侵入）**：`render(url, timeout=None, cookies=None)` + RenderAwareSession 支持 cookie 注入（playwright context.add_cookies 或 requests 透传）；**cookies=None 默认行为完全不变**（向后兼容，其他采集器零影响）
- boss_zhipin：`BN_BOSS_COOKIES_PATH` 环境变量（默认 `~/.config/boss/cookies.json`）；cookie 文件缺失 → warning + 无 cookie 跑（回到 M2a 行为）；**不把 cookie 写入任何日志/产物/git**
- 详情页采集：卡片 job_detail 链接 → 渲染 → JD 全文（职位描述容器正则多级降级）；字段：jd_full + 职位描述文本；失败容错（单详情页失败跳过）
- 增量去重：读当日/最近 joblist.json 的 link hash 集合，重复跳过（可配置 force 重采）
- 频率控制延续（delay ≥5s + jitter）；反爬判定延续（安全验证页/无卡片 → warning break）
- 测试：render cookie 注入单测（mock playwright）+ 采集器 cookie 路径/注入/详情页解析单测（≥10）；不真发网络
- 不新增依赖；ruff 干净

## 红线

- **cookie 安全**：不打印、不进 git、不进日志（redact）、产物不含 cookie；文件权限 chmod 600 保持
- 不虚构 JD 全文（解析失败 → jd_full="" 或跳过，不编造）
- 频率克制、不自动投递、不公开数据
- render.py 向后兼容（cookies=None 行为零变化，其他采集器不受影响）

## 验收标准

1. `python3 -m unittest discover -s intel/tests` 全绿（原 ~100 + 新增 ≥10；BriefNexus .venv/bin/python）
2. ruff 零错误（BriefNexus .venv/bin/ruff check intel/）
3. **真实渲染验证**（宿主，BN_RENDER_LIVE=1）：带 cookie 跑采集 → ≥3 卡片（含职位/公司/薪资）+ ≥1 详情页 JD 全文
4. 产物 joblist.json 含 jd_full 字段；重跑增量去重生效（同岗位不重复）
5. 日志/产物 grep 无 cookie 值泄漏；cookie 文件权限仍 600
6. `git status --short` 只有预期文件（render.py / boss_zhipin.py / 测试 / 简报）

## 环境

- 仓库：/home/l/.openclaw/workspace/projects/BriefNexus（git）
- cookie：/home/l/.config/boss/cookies.json（不进 git，环境外）
- 测试：.venv/bin/python；ruff：.venv/bin/ruff
- 真实验证：宿主跑（渲染需真实 chromium）
- 不要 git commit（由青简完成）
