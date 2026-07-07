# OpenClaw 集成 — 行业标准采集模块

## 架构概述

OpenClaw 作为调度层，通过 shell 调用 Python 采集引擎：

```
OpenClaw (编排层)
  │
  ├── cron 定时调度 (cron job / heartbeat)
  │     │
  │     └── shell 调用 Python 采集
  │           │
  │           └── Python standards.crawler.main
  │                 │
  │                 ├── samr.py  (全国标准信息平台)
  │                 ├── spc.py   (标准在线服务网)
  │                 └── csres.py (工标网)
  │
  ├── collector.py   ← 统一采集入口（CLI）
  │
  ├── dedup.py       ← 去重+分类
  │
  └── exporter.py    ← JSON/MD/CSV 输出
```

## 数据流

```
Python 采集器 → JSON (output/standards_YYYYMMDD.json)
              → MD  (output/standards_YYYYMMDD.md)   ← 人工浏览
              → CSV (output/standards_YYYYMMDD.csv)   ← Excel 分析
              → _latest.json                          ← 最新版本索引
```

## 定时采集（Cron）

### 方式一：OpenClaw cron job

在 OpenClaw 控制台中添加：

```yaml
name: standards-daily-collect
schedule:
  kind: cron
  expr: "0 9 * * 1-5"          # 工作日 09:00
  tz: Asia/Shanghai
sessionTarget: isolated
payload:
  kind: agentTurn
  message: |
    运行 BriefNexus 行业标准采集模块：
    cd /home/zzz/workspace/projects/BriefNexus && python -m standards.crawler.main
  timeoutSeconds: 300
```

### 方式二：Shell 包装器

```bash
# 直接运行
python /home/zzz/workspace/projects/BriefNexus/standards/crawler/main.py

# 指定配置
python /home/zzz/workspace/projects/BriefNexus/standards/crawler/main.py --config /path/to/custom_config.ini
```

### 方式三：crontab

```bash
# 编辑 crontab
crontab -e

# 添加行（工作日 09:00 Asia/Shanghai）
0 9 * * 1-5 cd /home/zzz/workspace/projects/BriefNexus && \
  /usr/bin/python3 -m standards.crawler.main >> \
  /home/zzz/workspace/projects/BriefNexus/standards/output/crawl.log 2>&1
```

## OpenClaw 交互钩子

采集完成后，OpenClaw 可以：

1. **读取 `_latest.json`** 检查是否有新标准
2. **通过 cron 定期比对** 上次采集的 `dedup_key` 集合，识别新增标准
3. **触发后续动作**：如通知用户、更新报告

## 单次运行

```bash
cd /home/zzz/workspace/projects/BriefNexus

# 全量采集（JSON + Markdown）
python -m standards.crawler.main

# 仅 JSON + CSV
python -m standards.crawler.main --format json csv

# 模拟运行
python -m standards.crawler.main --dry-run --verbose
```
