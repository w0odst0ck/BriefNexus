# 行业标准采集模块

> BriefNexus 子模块 — 定向采集照明/智能灯具领域相关国家标准与行业标准

---

## 定位

独立于 BriefNexus 原有情报工作流的**标准数据采集模块**，专注从国家标准化平台获取：

- **国家标准**（GB / GB/T / GB/Z）
- **行业标准**（各行业标准代号）
- **团体标准**（T/*）
- **地方标准**（DB*）

## 架构

```
standards/
├── README.md                     ← 本文件
├── standards_config.ini          ← 领域配置（ICS代码、关键词、平台开关）
├── crawler/
│   ├── main.py                   ← CLI 统一入口
│   ├── utils.py                  ← HTTP工具、标准化、去重键
│   └── platforms/
│       ├── base.py               ← 采集器基类
│       ├── samr.py               ← 全国标准信息公共服务平台
│       ├── spc.py                ← 中国标准在线服务网
│       └── csres.py              ← 工标网
├── engine/
│   ├── collector.py              ← 采集调度引擎（协调多源+多关键词）
│   ├── dedup.py                  ← 去重、合并、分类
│   └── exporter.py               ← JSON/MD/CSV 输出
├── output/                       ← 采集结果
└── openclaw/
    ├── README.md                 ← OpenClaw 集成说明
    └── workflow.sh               ← Shell 包装器
```

## 快速开始

```bash
cd /home/zzz/workspace/projects/BriefNexus

# 安装依赖（首次）
pip install requests beautifulsoup4 lxml

# 全量采集
python -m standards.crawler.main

# 详细模式（空跑）
python -m standards.crawler.main --verbose --dry-run
```

## 输出

| 格式 | 路径 | 用途 |
|------|------|------|
| JSON | `output/standards_YYYYMMDD.json` | 上游数据处理 |
| Markdown | `output/standards_YYYYMMDD.md` | 人工阅读 |
| CSV | `output/standards_YYYYMMDD.csv` | Excel 分析 |
| 最新索引 | `output/_latest.json` | OpenClaw 轮询 |

## 数据字段

| 字段 | 说明 | 示例 |
|------|------|------|
| `title` | 标准名称 | 普通照明用LED模块 安全规范 |
| `standard_no` | 标准号 | GB/T 12345-2023 |
| `publisher` | 发布机构 | 国家市场监督管理总局 |
| `publish_date` | 发布日期 | 2023-12-01 |
| `status` | 标准状态 | 现行 / 废止 |
| `category` | 分类 | 国标 / 行标 / 团标 |
| `ics_code` | ICS分类代码 | 29.140.40 |
| `scopes` | 适用范围 | ... |
| `source` | 数据源 | samr / spc / csres |
| `url` | 来源链接 | https://... |
| `dedup_key` | 去重键 | md5(标准号+标题) |
| `collected_at` | 采集时间戳 | 2026-07-07T11:56:00+08:00 |

## 定制领域

编辑 `standards_config.ini`，修改以下配置即可切换领域：

```ini
[domain]
name = 你的领域名称
keywords = 关键词1,关键词2,关键词3
ics_codes = 29.140,13.020

[crawler]
enable_samr = true
enable_spc = true
enable_csres = false
```

## 标准全文下载（openstd 平台）

本模块支持从 **openstd.samr.gov.cn**（国家标准全文公开系统）下载标准 PDF。

> openstd 收录约 46,592 项现行推荐性国家标准，其中非采标 ~30,803 项可下载。
> 照明领域标准大部分为 采标（adopted from IEC），少量可下载。
> 2026 年新标准通常有 20 工作日延迟后才公开。

### 下载流程（自动）

```
1. 搜索 openstd → 获取 hcno（标准内部 ID）
2. 访问 newGbInfo?hcno=...     → 建立 session
3. 访问 showGb?type=download  → 触发服务端校验
4. 访问 viewGb?hcno=...       → 获取 PDF 文件流
```

### CLI 命令

```bash
# 批量下载全部标准
python -m standards.crawler.main fetch

# 下载前 20 条（从旧到新，更容易成功）
python -m standards.crawler.main fetch --limit 20

# 下载指定标准
python -m standards.crawler.main fetch "GB/T 29294-2012"

# 查看标准详情（含 openstd 收录状态）
python -m standards.crawler.main detail "GB/T 29294"
```

### 输出

| 路径 | 说明 |
|------|------|
| `downloads/*.pdf` | 已下载的 PDF 文件 |
| `downloads/_hcno_results.json` | hcno 查询结果缓存 |

### 采标状态说明

| 页面标识 | 含义 | 是否可下载 |
|----------|------|-----------|
| `采` | 采标（adopted from IEC/ISO） | ❌ 无公开全文 |
| 空白 | 非采标（国内自主制定） | ✅ 通常可下载 |
| 2026 新标准 | 尚未提交至 openstd | ⏳ 等待 20 工作日 |

## 与 BriefNexus 主工作流的关系

```
BriefNexus 主工作流（情报采集 + 分析）
        │
        ├── news/        ← 资讯/动态
        ├── report/      ← 分析报告
        ├── topic/       ← 社群话题
        │
        └── standards/   ← 标准数据（本模块，独立运行）
                      └── downloads/   ← 标准全文 PDF
```

两者**独立运行**，但后期可通过 OpenClaw 编排联动：
- 标准更新 → 触发相关资讯采集
- 资讯中引用新标准 → 补全标准详情
- 标准全文下载 → 本地知识库
