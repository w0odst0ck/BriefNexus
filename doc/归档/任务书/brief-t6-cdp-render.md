# T6 CDP 浏览器渲染能力 — 简报（context pack）

> work-item: t6-cdp-render ｜ collab-flow 第三个真实项目
> 2026-08-15 晚间 ｜ 目标：BriefNexus 采集器支持浏览器渲染（JS 渲染/反爬源统一解法）

## 背景

- 现状：BriefNexus 采集器只做静态 HTTP 抓取（BaseCollector.crawl(sess)），JS 渲染/反爬源（BOSS 直聘、东财 WAF、巨潮 403 等）无法采集
- 工具箱调研结论：CDP 渲染是 WAF/JS 源统一解法（chrome-devtools-mcp 路线），能力矩阵 C1 已标记规划中
- 环境：本机无 Chrome/Chromium；Playwright + Chromium 正在安装（/home/l/venvs/playwright/.venv，代理下载中）
- 目标模式：**采集器声明式启用**——源配置 `render: true` 时，采集走浏览器渲染后再解析；不渲染的源零影响

## 目标

1. 新增渲染执行器（Playwright 封装）：给定 URL → headless Chromium 渲染 → 返回渲染后 HTML（或 DOM 提取）
2. BriefNexus 采集流程支持 `render: true`：渲染后的 HTML 传给采集器解析（采集器接口兼容，crawl 收到渲染页）
3. 首次验证源：选 1 个 JS 渲染真实源（如 BOSS 直聘搜索页或任一需渲染的页面）端到端跑通
4. 测试：stub/mock（不依赖真实浏览器可跑单测）+ 1 个真实冒烟（可选，标记 live）

## 约束

- 渲染执行器独立封装（intel/core/render.py 或独立模块），不侵入现有 BaseCollector 接口
- Playwright 走独立 venv（/home/l/venvs/playwright/.venv），不污染 BriefNexus 依赖
- 渲染超时/失败 → 降级静态抓取或返回空（不中断整体）
- 礼貌渲染：并发 1-2，超时 30s，不渲染静态可达源

## 红线

- 不写 token/key；渲染只对声明 render:true 的源启用
- 不改现有采集器行为（默认不渲染，现有 9 内置 + 挂载采集器零影响）
- 不引入重型常驻服务（按需启动浏览器实例，用完即关）

## 验收标准

1. 渲染执行器：URL → 渲染 HTML（单测 stub + 真实冒烟）
2. `render: true` 源走渲染路径（测试验证调用）
3. 默认源不渲染（现有测试不破坏）
4. 渲染失败降级（超时/浏览器不可用 → 警告 + 不中断）
5. ruff 干净；BriefNexus 现有 79 测试 + 新测试全绿

## 备注

- 浏览器安装状态：/tmp/pw-install.status（Playwright + Chromium）
- 若 Chromium 下载失败，备选：系统 apt 装 chromium 或 npx playwright install chromium
