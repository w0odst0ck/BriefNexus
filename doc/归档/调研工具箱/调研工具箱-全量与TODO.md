# 调研工具箱 — 全量调研 + TODO

> 2026-08-15 ｜ 对 StarLink 采集相关 15 仓全量调研（gh API 实查元数据）+ 落地 TODO
> 原则：能力先行——优先部署「现成采集器级」仓库，自建只补缺口

---

## 一、15 仓全量调研（元数据 2026-08-15 实查）

### S 档：现成采集器（部署即用）

| 仓库 | ⭐ | 语言/许可 | 更新 | 部署方式 | 落地后能力 |
|:--|:--|:--|:--|:--|:--|
| opendatalab/MinerU | 77.7k | Python | 08-14 | pip/库 + 服务 | PDF/文档 → 文本（标准/政策全文检索） |
| NanmiCoder/MediaCrawler | 62.5k | Python | 08-14 | 源码运行（含登录态/反爬） | 微博/抖音/小红书/B 站采集 |
| mvanhorn/last30days-skill | 58.2k | Python/MIT | 08-14 | skill（agent 场景） | 海外趋势采集（近 30 天） |
| huginn/huginn | 49.8k | Ruby/MIT | 08-15 | Docker | 监控自动化 agent 工作流 |
| DIYgod/RSSHub | 45.7k | TS/AGPL | 08-14 | **Docker 一条命令** | **数千源 RSS 路由**（能力放大器） |
| dgtlmoon/changedetection.io | 33.1k | Python/Apache | 08-07 | Docker | 通用网页变更监控（价格/公告/合规） |
| iv-org/invidious | 22.7k | Crystal/AGPL | 08-15 | Docker | YouTube 数据/评论（免官方配额） |
| Evil0ctal/Douyin_TikTok_Download_API | 19.4k | Python/Apache | 2025-10 | Docker/源码 | 短视频平台采集 |

### A 档：能力集成/借鉴

| 仓库 | ⭐ | 语言/许可 | 更新 | 用途 |
|:--|:--|:--|:--|:--|
| Panniantong/Agent-Reach | 71.9k | Python/MIT | 08-12 | Agent 式采集主线（采集→总结→存档闭环） |
| ChromeDevTools/chrome-devtools-mcp | 49.2k | TS/Apache | 08-14 | **CDP 渲染能力**（WAF/JS 反爬统一解法，MCP 生态可复用） |
| alibaba/page-agent | 28.6k | TS/MIT | 08-12 | 网页自动化新范式（截图/操作驱动） |
| MadsLorentzen/ai-job-search | 31.8k | TS/MIT | 08-14 | 求职自动化（已入求职工具箱） |

### B 档：参考/工具

| 仓库 | ⭐ | 语言/许可 | 更新 | 用途 |
|:--|:--|:--|:--|:--|
| AstrBotDevs/AstrBot | 39.2k | Python/AGPL | 08-15 | 后处理/机器人分发参考 |
| KnockOutEZ/wigolo | 4.6k | TS | 08-15 | MCP 本地搜索爬取参考 |
| wistbean/learn_python3_spider | 22k | Python/MIT | 07-25 | 爬虫学习字典 |

---

## 二、调研关键发现

1. **RSSHub 无 BOSS 直聘职位路由**（实查：仅 bossdesign 设计站路由）——BOSS 反爬太强，RSSHub 未收录；国内岗位采集需 **CDP 自建**（P2）
2. **RSSHub 有 LinkedIn 路由**——海外求职岗位可**立即用**（RSSHubCollector 配一个路由）
3. **能力放大器 = RSSHub**：一个本地实例 + 一个 RSSHubCollector = 数千源，其余自建采集器只在"RSSHub 没有路由"时才需要
4. 15 仓全部活跃维护（最新更新 07-25 起），无废弃

---

## 三、TODO（分批落地）

### P0：能力地基（✅ 2026-08-15 完成）

- [x] **T1 部署 RSSHub 本地实例**：Node 源码部署（docker 不可用），:1200，systemd 常驻（rsshub.service），PROXY_URI 代理；hf 路由实测 200
- [x] **T2 写 RSSHubCollector**：research-collectors/ 项目（reasonix 交付），stdlib xml.etree 解析，参数化 route/base_url/max_items；10 单测 + ruff 全绿
- [x] **T3 验证接入**：5 源端到端全通（hf_blog 14 / hf_papers 30 / aibase 10 / ai_news 30 / baijing 10 = 94 条真实数据）；路由名踩坑（需子路径如 /aibase/daily，且 arxiv/jiqizhixin/github-trending 已在新版移除）
- [x] **T4 部署 changedetection.io**：venv 部署（:5000，datastore 需 -d 指定），systemd 常驻（changedetection.service）
- [x] **T5 能力矩阵写 README**：bddfdd6（C1/C2/C3 12 项状态）

### P1：渲染 + 解析 + 预警（✅ T6/T8 完成，T7 待做）

- [x] **T6 CDP 渲染能力**（2026-08-15，collab-flow 全流程）：render.py（RenderExecutor 子进程隔离 + RenderAwareSession 包装，crawl 接口不变）+ render_worker.py + boss_zhipin 验证源；24 单测 + ruff + platform 79 不破坏 + ocr 0 finding；commit 3ea7b1d。**✅ 真实渲染已解锁（2026-08-16）**：sudo 装 libasound2/libnspr4/libnss3 后 BN_RENDER_LIVE=1 三连测全过（L1/L2/L3）；东财/巨潮 WAF 接入（df38973）
- [x] **T8 变更预警推送**（2026-08-15，collab-flow 全流程）：compare_snapshots --push（惰性导入 feishu_push + 摘要构建 + 6 用例）；accepted。框架坑 5 复发（glob * 受限 YAML）
- [x] **T7 MinerU 文档解析器**（2026-08-16，collab-flow 第 4 项目 accepted）：research-collectors/ pdf_parse.py + worker + pdf_source.py + pdf_pull.py；32 测试；真实解析冒烟通过（arxiv 2401.00001 → 36.8KB 全文 md，公式/图片完整）。依赖链坑：mineru 不自动带 torch 系（torch/torchvision/transformers/timm/safetensors/six 逐个补，CPU 版）

### P2：攻坚（按需）

- [ ] **T10 BOSS 直聘采集器**：CDP 渲染 + 登录态（RSSHub 无路由，自建）；配合求职计划排期
- [ ] **T11 MediaCrawler 社媒源**：微博/抖音热点（登录态），AI 短剧/舆情场景
- [ ] **T12 invidious 实例**：YouTube 数据（如需）
- [ ] **T13 huginn 工作流**：复杂监控编排（如需要时）

---

## 四、技术依赖备忘

- Docker：RSSHub / changedetection / invidious / douyin API 均 Docker 化（本机 WSL2 已具备？需确认 docker 可用）
- RSSHubCollector 走平台挂载模式（客户端项目 `research-collectors/`，与 mojin-intel 同构）
- CDP-MCP 走 :9222 调试端口或 MCP 服务，采集器声明式启用

## 五、待拍板

1. P0 五件套确认？（T1-T5，尤其 T5 写 README 是否同意）
2. Docker 环境确认：本机 docker 是否可用（WSL2），不可用则 RSSHub 用 Node 源码跑
3. 第一批验证路由选哪 3 个？（建议 HuggingFace + 机器之心 + LinkedIn jobs）
