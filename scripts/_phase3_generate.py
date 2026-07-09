#!/usr/bin/env python3
"""Phase 3: 基于联网核实的 brief.md 生成 report.md + topic.md"""
import os, sys, configparser
from _dotenv import load_project_env; load_project_env()
sys.stdout.reconfigure(encoding="utf-8")
import requests

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crawler_config.ini")
_cfg = configparser.ConfigParser()
with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    _cfg.read_file(f)
LLM_BASE = _cfg.get("api", "base_url")
LLM_KEY = os.environ.get("DEEPSEEK_API_KEY") or _cfg.get("api", "api_key")
LLM_MODEL = _cfg.get("api", "model")

BRIEF_FILE = r"D:\NOTES\zzz\BriefNexus\news\brief\brief_2026-06-29.md"
REPORT_DIR = r"D:\NOTES\zzz\BriefNexus\report"
TOPIC_DIR  = r"D:\NOTES\zzz\BriefNexus\topic"
DATE = "2026-06-29"

def call_llm(system, user, max_tokens=4096):
    r = requests.post(f"{LLM_BASE}/chat/completions",
        headers={"Authorization": f"Bearer {LLM_KEY}", "Content-Type": "application/json"},
        json={"model": LLM_MODEL, "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ], "temperature": 0.3, "max_tokens": max_tokens},
        timeout=120)
    return r.json()["choices"][0]["message"]["content"]

def main():
    with open(BRIEF_FILE, "r", encoding="utf-8") as f:
        brief_content = f.read()

    print(f"读取 brief.md ({len(brief_content)} chars)", file=sys.stderr)

    # --- report.md ---
    print("生成 report.md...", file=sys.stderr)
    report_prompt = """你是一位智能照明与建筑智能化领域的资深行业分析师。
基于以下已联网核实过的精选简报，撰写一份专业行业简报（report.md）。

简报内容：
""" + brief_content + """

## 写作要求
- 开篇总览：1段话概括本期核心趋势（不超过200字）
- 正文结构：按简报中的板块分类展开，每个板块一个子标题
- 数据引用：每条新闻引用时，必须带上核实后的具体数据和关键信息
- 趋势信号：文末列出 3 个值得关注的趋势信号
- 附录：信息来源列表

## 语言规则（硬性约束）
- 每段第一句必须是核心观点
- 句子不超过30字，每段不超过3行
- 总字数 1500-2500 字
- 客观、数据驱动、专业
- 直接输出 markdown，不要额外说明"""

    report_content = call_llm("你是智能照明行业资深分析师。", report_prompt, 4096)

    os.makedirs(REPORT_DIR, exist_ok=True)
    rpath = os.path.join(REPORT_DIR, f"report_{DATE}.md")
    with open(rpath, "w", encoding="utf-8") as f:
        f.write(report_content)
    print(f"  -> {rpath}", file=sys.stderr)

    # --- topic.md ---
    print("生成 topic.md...", file=sys.stderr)
    topic_prompt = """你是一位智能照明与建筑智能化领域的社群运营专家。
基于以下已联网核实过的精选简报，生成 3-5 个适合社群传播的话题帖（topic.md）。

简报内容：
""" + brief_content + """

## 每条话题格式
```
## 话题N：一句话钩子标题

正文内容（不超过150字）

**互动问题**：一个开放式问题引导评论
```

## 硬性要求
- 必须生成 **4-5 条**不同角度的话题
- 每条覆盖不同的新闻或组合
- 第一句必须是疑问句或反常识陈述
- 全文不超过5句话
- 结尾必须有开放式互动问题
- 语气像圈内人在聊天，不要官方通告
- 每个话题必须有钩子
- 直接输出 markdown，不要额外说明"""

    topic_content = call_llm("你是智能照明行业社群运营专家。", topic_prompt, 3072)

    os.makedirs(TOPIC_DIR, exist_ok=True)
    tpath = os.path.join(TOPIC_DIR, f"topic_{DATE}.md")
    with open(tpath, "w", encoding="utf-8") as f:
        f.write(topic_content)
    print(f"  -> {tpath}", file=sys.stderr)

    print(f"\n{'='*50}", file=sys.stderr)
    print(f"输入: brief.md（11条，已联网核实）", file=sys.stderr)
    print(f"输出:", file=sys.stderr)
    print(f"  report.md: {rpath}", file=sys.stderr)
    print(f"  topic.md:  {tpath}", file=sys.stderr)

if __name__ == "__main__":
    main()
