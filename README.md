# BriefNexus — 多源情报采集与分析工作流

> 基于 OpenClaw + LLM 的多源情报采集与分析系统。
> 支持任意领域的学术论文、政策动态、政府公告三路数据源采集与交叉分析。
> 内置示例：智能照明与智能建筑领域（arXiv/CSA联盟/上海住建委）。

---

## 项目架构

```
D:\NOTES\zzz\BriefNexus\
├── scripts\                    ← 核心脚本
│   ├── news_crawler.py         ← Phase 1: 多源采集 + LLM 分类
│   ├── run_pipeline.py         ← Phase 2/3: 导出 + LLM 生成
│   ├── pipeline.bat            ← Windows 快捷启动
│   └── crawler_config.ini      ← API 配置（Key 在此修改）
│
├── news\
│   ├── news\                   ← Phase 1: 原始采集数据（含勾选清单）
│   ├── brief\                  ← Phase 2.5: 联网核实后的精选简报
│   └── prompt\                 ← Phase 2: 导出到 LLM 网页端的提示词
│
├── report\                     ← Phase 3: 问题驱动型分析报告
└── topic\                      ← Phase 3: 社群话题帖
```

## 工作流（通用，不限领域）

```
Phase 1: 采集
  news_crawler.py → 从数据源抓取最新情报
  → 输出 news/news_{日期}.md（含 LLM 板块分类 + 勾选清单）

Phase 2: 人工勾选
  打开 news_{日期}.md，在 [ ] 中打 [x] 标记想要分析的条目

Phase 2.5: 导出 + 联网核实
  run_pipeline.py export → 输出 prompt.md
  → 复制到 LLM 网页端（开启联网搜索）
  → 将核实结果保存到 brief/brief_{日期}.md

Phase 3: 生成
  run_pipeline.py generate
  → 输出 report/report_{日期}.md  （问题驱动型分析报告）
  → 输出 topic/topic_{日期}.md    （社群话题帖）
```

### 报告风格

不写新闻摘要。每期选定一个核心问题，用多路数据交叉分析：

```
核心问题 → 学术证据 → 政策动态 → 产业现状 → 交叉分析 → 判断建议
```

### 示例输出

| 文件 | 内容 | 长度 |
|------|------|------|
| `news/news_2026-07-01.md` | 47 条（15学术 + 32政府） | ~40KB |
| `news/news_2026-07-02.md` | 40 条（3 个数据源） | ~27KB |
| `report/report_2026-07-02.md` | 问题驱动型分析报告 | ~6.9KB |
| `topic/topic_2026-07-02.md` | 4-5 条社群话题帖 | ~3.8KB |

---

## 内置示例：智能照明与建筑智能化

### 数据源

| 来源 | 类型 | 覆盖内容 | 访问方式 |
|------|------|----------|----------|
| **arXiv** | 学术论文 | MicroLED、LiFi、可见光通信、集成光子学 | API / 网页抓取 |
| **CSA 联盟** | 政策产业 | 半导体照明政策、产业动态、联盟活动 | 网页抓取 |
| **上海住建委** | 政府公告 | 绿色建筑、节能标准、建筑管理政策 | 网页抓取 |

### 板块体系（LLM 自动分类）

| 板块 | 图标 | 覆盖方向 |
|------|------|----------|
| 行业大势 | 🌐 | 展览展会、行业报告、市场趋势、政策法规 |
| 技术突破 | 🔬 | MicroLED/OLED、芯片封装、光学器件、专利 |
| 资本脉搏 | 📈 | IPO/上市、投融资、并购、股市 |
| 供应链深水 | ⛓️ | 产线建设、材料供应、产能布局 |
| 企业交锋 | 🏷️ | 品牌竞争、公司动态、战略合作 |
| 场景新战场 | 🎯 | 智能建筑、植物照明、车载照明、医疗照明 |
| 招标市场 | 📝 | 政府/企业标讯、招标公告、中标公示 |

## 快速开始

```bash
# 1. 配置 API Key（必选）
#    编辑 scripts/crawler_config.ini：
#    api_key = sk-your-key-here

# 2. 采集（Phase 1）
cd scripts
python news_crawler.py --max-age 7

# 3. 勾选 + 导出（Phase 2）
#    编辑 news/news_{日期}.md → 将 [ ] 改为 [x]
python run_pipeline.py export

# 4. 联网核实（Phase 2.5，手动）
#    复制 news/prompt/prompt_{日期}.md → 你的 LLM 网页端（开启联网搜索）
#    将结果保存到 news/brief/brief_{日期}.md

# 5. 生成（Phase 3）
python run_pipeline.py generate
```

---

## ⏱ 每日自动采集

本项目支持两种定时任务方式（二选一或双重保险）：

### 方式一：Windows 任务计划程序（主力）

```powershell
# 创建任务（周一到周五 9:00）
schtasks /CREATE /SC WEEKLY /D MON,TUE,WED,THU,FRI `
  /TN "BriefNexus-DailyCrawl" `
  /TR "<python路径> D:\NOTES\zzz\BriefNexus\scripts\news_crawler.py --max-age 7" `
  /ST 09:00 /RL LIMITED /F
```

- 不依赖 LLM，稳定可靠
- 需要用户登录后运行

### 方式二：OpenClaw cron（兜底）

```
在 OpenClaw 中创建定时任务，用 systemEvent 唤醒主会话执行脚本。
```

> ⚠️ 注意：OpenClaw 隔离会话的 LLM 调用可能超时，建议使用 `sessionTarget: "main"` + `payload.kind: "systemEvent"` 避免此问题。

## 定制你的数据源

**关键文件一览：**

| 文件 | 功能 | 可定制 |
|------|------|--------|
| `scripts/news_crawler.py` | 采集引擎 + LLM 分类 | 更换数据源函数、调整板块体系 |
| `scripts/run_pipeline.py` | 导出 + 报告生成 | 修改 prompt、生成模板 |
| `scripts/crawler_config.ini` | API 配置 | 更换 LLM 模型、API Key |

**更换领域**：只需修改 `news_crawler.py` 中的 `SOURCES` 列表，将 arXiv/CSA/住建委 替换为你的领域数据源（如金融研报、医疗政策、科技博客等），调整 LLM 分类的板块体系即可。

## 配置

`scripts/crawler_config.ini`:

```ini
[api]
base_url = https://api.deepseek.com
api_key = sk-your-key-here
model = deepseek-v4-flash
```

## Python 环境

```cmd
# 所需依赖
pip install requests beautifulsoup4 feedparser lxml
```

## 输出示例

```
news/news_2026-07-02.md       → 40 条（3 个数据源）
report/report_2026-07-02.md    → 问题驱动型分析报告（~6.9KB）
topic/topic_2026-07-02.md      → 4-5 条社群话题帖（~3.8KB）
```
