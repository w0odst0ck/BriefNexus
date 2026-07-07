# intel/ — 情报采集与分析模块

> BriefNexus 核心模块，封装情报采集、分类、导出、生成的全流程。

## 架构

```
intel/
├── main.py                  ← 统一 CLI 入口
├── collector/
│   ├── main.py              ← 采集引擎 + 规则分类
│   └── platforms/
│       ├── base.py          ← 采集器基类
│       ├── arxiv.py         ← arXiv 学术论文
│       ├── csa.py           ← CSA 联盟（半导体照明网）
│       └── shjianshe.py     ← 上海住建委
├── pipeline/
│   └── main.py              ← 导出 prompt + 报告生成
└── output/
    ├── news/                ← 采集结果
    ├── prompt/              ← 导出的 prompt
    ├── brief/               ← 联网核实后的简报
    ├── report/              ← 分析报告
    └── topic/               ← 社群话题帖
```

## 设计原则

- **LLM 默认禁用** — 避免因 LLM 不稳定导致流程崩溃
- **仅规则分类可用** — 无 API Key 也能正常工作
- **按需启用 LLM** — 通过 `--llm` 标志开启

## 用法

```bash
# 采集（仅规则分类）
python -m intel.main crawl
python -m intel.main crawl --max-age 14

# 采集（启用 LLM 增强分类）
python -m intel.main crawl --llm

# 导出 prompt
python -m intel.main export

# 生成报告（带 LLM）
python -m intel.main generate --llm

# 全流程
python -m intel.main all --llm
```

## 对比原脚本

| 方面 | 原 scripts/ | 新 intel/ 模块 |
|------|-------------|---------------|
| 架构 | 单文件+辅助脚本 | 模块化：平台适配器+引擎 |
| LLM | 必选，失败即崩溃 | 默认禁用，`--llm` 可选 |
| 输出 | `news/`, `report/`, `topic/` | `intel/output/` |
| 扩展性 | 需修改主文件 | 加平台适配器即可 |
