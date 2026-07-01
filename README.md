# BriefNexus — 智能照明与智能建筑资讯工作流

> 基于 OpenClaw + DeepSeek 的多源情报采集与分析系统。
> 从学术论文、政策动态、政府公告三路数据源采集，产出问题驱动型分析报告。

---

## 项目架构

```
D:\NOTES\zzz\BriefNexus\
├── scripts\                    ← 核心脚本
│   ├── run_pipeline.py         ← 主控 Pipeline（总入口）
│   ├── pipeline.bat            ← Windows 快捷启动
│   └── crawler_config.ini      ← API 配置（Key 在此修改）
│
├── news\
│   ├── news\                   ← Phase 1: 原始采集数据
│   ├── brief\                  ← Phase 2.5: 联网核实后的精选简报
│   └── prompt\                 ← Phase 2: 导出到 DeepSeek 网页端的提示词
│
├── report\                     ← Phase 3: 问题驱动型分析报告
└── topic\                      ← Phase 3: 社群话题帖
```

## 数据源

| 来源 | 类型 | 覆盖内容 | 访问方式 |
|------|------|----------|----------|
| **arXiv** | 学术论文 | MicroLED、LiFi、可见光通信、集成光子学 | API 开放查询 |
| **CSA 联盟** | 政策产业 | 半导体照明政策、产业动态、联盟活动 | 网页抓取 |
| **上海住建委** | 政府公告 | 绿色建筑、节能标准、建筑管理政策 | 网页抓取 |

## 工作流

```
Phase 1: 采集
  pipeline.bat crawl
  → 从 arXiv + CSA + 住建委 采集最新情报
  → 输出 news/news_{日期}.md（含勾选清单）
        ↓
Phase 2: 人工勾选
  打开 news_{日期}.md，在 [ ] 中打 [x] 标记想要分析的条目
        ↓
Phase 2.5: 导出 + 联网核实
  pipeline.bat export
  → 输出 prompt/prompt_{日期}.md
  → 复制到 DeepSeek 网页端（开启联网搜索）
  → 将结果保存到 brief/brief_{日期}.md
        ↓
Phase 3: 生成
  pipeline.bat generate
  → 输出 report/report_{日期}.md  （问题驱动型分析报告）
  → 输出 topic/topic_{日期}.md    （社群话题帖）
```

## 报告风格

不写新闻摘要。每期选定一个核心问题，用三路数据交叉分析：

```
核心问题 → 学术证据 → 政策动态 → 产业现状 → 交叉分析 → 判断建议
```

## 快速开始

```cmd
# 1. 配置 API Key
编辑 scripts\crawler_config.ini

# 2. 采集数据
cd scripts
pipeline.bat crawl

# 3. 勾选 + 导出
#    编辑 news/news_{日期}.md → 打 [x]
pipeline.bat export

# 4. 联网核实
#    复制 prompt/prompt_{日期}.md → DeepSeek 网页端
#    保存到 brief/brief_{日期}.md

# 5. 生成产出
pipeline.bat generate
```

## 自动化

工作日 09:00 自动执行采集（通过 OpenClaw cron）。

## 配置

`scripts/crawler_config.ini`:

```ini
[api]
base_url = https://api.deepseek.com
api_key = sk-your-key-here
model = deepseek-v4-flash
```

## 输出示例

| 文件 | 内容 | 长度 |
|------|------|------|
| `news/news_2026-06-29.md` | 119 条原始情报（39学术 + 60政策 + 20政府） | ~39KB |
| `report/report_2026-06-29.md` | MicroLED微显示驱动照明转型分析 | ~2.5KB |
| `topic/topic_2026-06-29.md` | 3 条社群话题帖 | ~1KB |
