# brief: company-site-collector — 公司官网采集器（求职公司画像数据源）

> 2026-08-24 ｜ BriefNexus 仓 ｜ collab-flow ｜ 与 job-search company-profile 双仓协作
> 调度：12:00 午间窗口（off-peak）自动执行

## 1. 背景与目标

求职管线新增「公司画像」环节（投递第二核心输入）：采集目标公司**官网**公开信息，
产出结构化素材，供 job-search 侧 company_profile.py 生成画像（定位/规模/客户/产品线/技术/招聘信号）。

用户已拍板：
- **采集范围：官网 6 类页全采**（P1 首页 / P2 关于我们 / P3 产品与方案 / P4 新闻中心 / P5 招聘页 / P6 技术博客+联系我们）
- **P5 招聘页尽力采**（常跳第三方系统北森/Moka/Lever 等，采不到标注 `unavailable`，不硬刚）
- **不使用 BOSS**，不采集任何招聘平台私有 API

## 2. 任务范围（做什么 / 不做什么）

做：
1. 新采集器 `intel/collectors/company_site.py`，继承 `BaseCollector`，注册进 registry
2. 官网 6 类页发现与采集：
   - **sitemap.xml / robots.txt 优先**（自动发现页面清单，替代硬编码路径）；无 sitemap 则用默认候选路径（/ /about /products /solutions /news /careers /join /blog /contact）
   - **JSON-LD 优先**（Organization schema：名称/logo/成立/地址/社交链接——零 LLM 成本且最准，命中即入结构化字段）
3. 降级链：`requests 直连 → bs4 静态解析 → 反爬/动态渲染才 CDP`（复用 `intel/core/render.py` RenderAwareSession，声明 `render: true`；**CDP 只默认开给 P5 招聘页**——JS 渲染第三方系统；其余页直连优先）
4. 正文去噪（剥离 nav/footer/script/style，主内容提取），控制落盘体积
5. 幂等落盘 `intel/data/company/<domain>/<date>/`：
   - `raw_pages/<page-id>.html`（去噪后正文或原文，含页面元数据头）
   - `pages.json`（页面清单：URL/类型/状态码/是否 CDP/抓取时间/正文长度）
   - `meta.json`（域名/采集时间/采集器版本/失败记录/每页耗时）
6. 频率控制（delay 硬下限 + jitter，复用 boss_zhipin 模式）；失败静默记录，不中断整体
7. 单测 + ruff 绿

不做：
- **不跑真实 CDP 渲染验证**（reasonix 沙箱不能跑真实浏览器）——真实验证由宿主青简自理
- 不做 LLM 结构化（那是 job-search company_profile.py 的活）
- 不采 BOSS/招聘平台；不采需登录的付费墙内容
- 不改动现有采集器/平台核心代码（只新增文件 + registry 注册）

## 3. 接口契约（与 job-search 侧对齐）

产物 JSON 契约（company_profile.py 消费端依赖）：
- `pages.json` 每页条目：`{page_id, page_type: home|about|products|news|careers|blog|contact|other, url, title, status_code, rendered: bool, fetched_at, text_len, unavailable?: bool}`
- `meta.json`：`{domain, company_name?, fetched_at, collector_version, failures: [{url, error}], page_count}`
- 原始正文在 `raw_pages/<page_id>.html`（UTF-8，含 `<!-- source: <url> -->` 头注释）

## 4. 验收标准

1. 单测覆盖：页面发现（sitemap/默认路径/404 降级）、降级链选择、JSON-LD 提取、正文去噪、幂等落盘、失败静默
2. `python3 -m pytest intel/tests/ -x` 全绿（在 BriefNexus 仓）；ruff 干净
3. 产物结构符合 §3 契约（dry-run 以本地 fixture 网站验证，不依赖外网）
4. 报告列出：文件清单、测试数、契约字段表

## 5. 默认约束

- 不改无关代码；不碰 git（commit 由青简执行）
- 产物目录 `intel/data/` 已在 gitignore（确认即可，不加新敏感文件）
- 不打印/不落盘任何 cookie/token
- 完成后独立报告：改动文件、测试结果、验收对照
