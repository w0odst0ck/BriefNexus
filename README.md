# BriefNexus — 多源情报采集 + 标准全文检索系统

> 双引擎：**情报采集分析**（intel）+ **行业标准检索与全文采集**（standards）  
> 内置示例领域：智能照明与智能建筑  
> v1 状态：355 条照明标准元数据 + 108 条 PDF 全文（484MB）

---

## 项目结构

```
BriefNexus/
├── intel/                       ← 情报采集与分析模块
│   ├── collector/platforms/    ← 数据源适配器（白宫/EU/NVIDIA/GNW/SEC/Fed）
│   ├── intel_config.ini       ← 数据源配置
│   └── output/                 ← 采集结果
│
├── standards/                   ← 行业标准检索与全文采集模块
│   ├── crawler/                ← 采集引擎
│   │   ├── main.py            ← CLI 入口
│   │   ├── downloader.py      ← PDF 下载器（openstd / bzxz.net）
│   │   ├── utils.py           ← HTTP 工具
│   │   └── platforms/         ← 平台适配器
│   │       ├── openstd.py     ← 全国标准信息公共服务平台
│   │       ├── search_finder.py ← bzxz.net 搜索引擎
│   │       └── alt_sources.py ← 替代来源编排器
│   ├── engine/                 ← 数据引擎
│   │   ├── collector.py       ← 采集调度 CLI（collect/search/list/stats/tree）
│   │   ├── storage.py         ← SQLite + FTS5 存储
│   │   ├── exporter.py        ← 导出器
│   │   ├── dedup.py           ← 去重与合并
│   │   ├── intl_mapper.py     ← 采标 ↔ IEC/CIE 映射
│   │   └── ics_tree.py        ← ICS 分类树
│   ├── downloads/              ← PDF 全文存储（108 条，484MB）
│   └── README.md              ← 模块说明
│
├── scripts/                     ← 旧版采集脚本（向后兼容）
├── news/                        ← 旧版采集结果
├── memory/                      ← 项目日志
│
└── standards\                  ← 行业标准采集模块（2026-07-07 新增）
    ├── crawler\/platforms\     ← 标准平台适配器（SAMR / SPC / CSRES）
    ├── engine\                 ← 采集引擎 + 去重 + 导出 + SQLite存储
    └── output\                 ← 采集结果（JSON / MD / CSV）
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

## 🏛️ 行业标准采集模块

> 通用行业标准采集工具。自动从国家标准化平台抓取标准数据，构建可检索、可通过 ICS 分类树浏览的本地知识库。
> 数据源、关键词、ICS 代码均可配置，适用于任意行业领域。

### 数据源

| 来源 | 类型 | 覆盖内容 | 状态 |
|------|------|----------|------|
| **全国标准信息公共服务平台 (SAMR)** | 国标 | GB/GB/T/GB/Z 标准检索与详情 | ✅ 稳定 |
| **中国标准在线服务网 (SPC)** | 国标/行标 | 标准全文检索 | ❌ 待修复 |
| **工标网 (CSRES)** | 综合 | 按 ICS 分类遍历 | ⏸ 默认关闭 |

### 工作流

```
配置领域 → 采集 → 入库 → ICS 补充 → 检索/查询/统计
```

### 存储架构

```
standards.db    ← SQLite 数据库（自动创建）
├── standards          ← 主表（UPSERT 去重）
├── standards_fts      ← FTS5 全文索引
├── ics_tree           ← ICS 分类树
└── standard_ics       ← 标准 ↔ ICS 关联（多对多）
```

### CLI 命令

```bash
# 采集标准数据
python -m standards.crawler.main collect

# 从详情页补充 ICS 代码（只需跑一次）
python -m standards.crawler.main enrich

# 全文检索
python -m standards.crawler.main search "关键词"
python -m standards.crawler.main search "GB/T 12345-2023"

# 精确过滤
python -m standards.crawler.main list --status 现行 --category 国标
python -m standards.crawler.main list --date-from 2024-01-01

# ICS 分类树浏览
python -m standards.crawler.main tree                      # 根节点
python -m standards.crawler.main tree 29                   # 电气工程
python -m standards.crawler.main tree 29.140 --standards   # 节点下标准

# 数据库统计
python -m standards.crawler.main stats

# JSON 输出（程序消费）
python -m standards.crawler.main search "关键词" --json > data.json
```

### 快速开始

```bash
# 1. 安装依赖
pip install requests beautifulsoup4 lxml

# 2. 配置领域（必选）
#    编辑 standards/standards_config.ini，修改为你关注的领域
#    name = 你关注的行业名称
#    keywords = 关键词1,关键词2
#    ics_codes = 29.140,91.140

# 3. 采集 + 入库
python -m standards.crawler.main collect

# 4. 补充 ICS 代码
python -m standards.crawler.main enrich

# 5. 检索
python -m standards.crawler.main search "关键词"
```

### 定制领域

编辑 `standards/standards_config.ini`：

```ini
[domain]
name = 你的行业名称
keywords = 关键词1,关键词2,关键词3
ics_codes = 29.140,13.020,91.140

[crawler]
enable_samr = true
enable_spc = false
enable_csres = false
max_pages = 20
```

> **示例配置：** 仓库预置了「照明与智能灯具」领域的配置作为参考样例。`standards/standards_config.ini` 中的参数可直接修改用于你的领域。

# 3. 采集 + 入库
python -m standards.crawler.main collect

# 4. 补充 ICS 代码
python -m standards.crawler.main enrich

# 5. 检索
python -m standards.crawler.main search "LED"
```

### 定制领域

编辑 `standards/standards_config.ini`，修改以下配置即可切换到其他行业：

```ini
[domain]
name = 你的领域名称
keywords = 关键词1,关键词2,关键词3
ics_codes = 29.140,13.020,91.140
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
