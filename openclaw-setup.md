# BriefNexus — OpenClaw 一键配置清单

> 将此文件发送给 OpenClaw，它会自动执行以下所有配置。
> 保存路径：`D:\NOTES\zzz\BriefNexus\openclaw-setup.md`

---

## 1. 配置参数

```yaml
# 请修改以下参数后再使用
project_path: D:\NOTES\zzz\BriefNexus
api_key: sk-your-api-key-here
api_model: deepseek-v4-flash
cron_schedule: "0 9 * * 1-5"
cron_timezone: Asia/Shanghai
python_path: C:\Users\shzhangzhongze\AppData\Local\Programs\Python\Python313\python.exe
```

---

## 2. 必配：API Key

写入 `scripts/crawler_config.ini`：

```ini
[api]
base_url = https://api.deepseek.com
api_key = sk-your-api-key-here
model = deepseek-v4-flash
```

```bash
cat > scripts/crawler_config.ini << 'EOF'
[api]
base_url = https://api.deepseek.com
api_key = sk-your-api-key-here
model = deepseek-v4-flash
EOF
```

---

## 3. 必配：Python 依赖

```bash
pip install requests beautifulsoup4 feedparser lxml
```

---

## 4. 可选：安装 Skill

注册 `briefnexus-pipeline` skill（提案名称），后续可通过 skill 名称直接运行：

```yaml
# OpenClaw 会自动将此文件内容注册为 skill
# 包含完整工作流描述、可执行步骤、输出路径
```

确认 skill 状态：

```bash
openclaw skill list | grep briefnexus
```

---

## 5. 可选：设置定时任务

工作日 09:00 自动采集（Asia/Shanghai）：

```yaml
name: briefnexus-daily-crawl
schedule: "0 9 * * 1-5"
timezone: Asia/Shanghai
command: python news_crawler.py --max-age 7
workdir: D:\NOTES\zzz\BriefNexus\scripts
python: C:\Users\shzhangzhongze\AppData\Local\Programs\Python\Python313\python.exe
```

验证定时任务：

```bash
openclaw cron list | grep briefnexus
```

---

## 6. 验证配置

```bash
# 测试采集（Phase 1）
cd D:\NOTES\zzz\BriefNexus\scripts
python news_crawler.py --max-age 7
# 预期输出：news/news_{日期}.md

# 测试导出（Phase 2）
python run_pipeline.py export
# 预期输出：news/prompt/prompt_{日期}.md
```

---

## 目录结构

```
D:\NOTES\zzz\BriefNexus\
├── scripts\                    ← 核心脚本
│   ├── news_crawler.py         ← Phase 1: 多源采集 + LLM 分类
│   ├── run_pipeline.py         ← Phase 2/3: 导出 + LLM 生成
│   ├── pipeline.bat            ← Windows 快捷启动
│   └── crawler_config.ini      ← API 配置
├── news\
│   ├── news\                   ← 原始采集数据
│   ├── brief\                  ← 联网核实后的精选简报
│   └── prompt\                 ← 导出提示词
├── report\                     ← 分析报告
└── topic\                      ← 社群话题帖
```

---

## 工作流速览

```
Phase 1: 采集（自动  →  news/news_{日期}.md）
Phase 2: 勾选（手动  →  [x] 标记条目）
Phase 2.5: 联网核实（手动 → 复制 prompt 到 LLM 网页端）
Phase 3: 生成（自动  →  report + topic）
```
