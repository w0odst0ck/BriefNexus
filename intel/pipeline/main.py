#!/usr/bin/env python3
"""
情报流水线 — 导出 prompt + 报告生成

LLM 默认禁用，通过 --llm 可选启用。
"""

import argparse
import configparser
import hashlib
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger("intel.pipeline")

# ── 路径 ──
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CST = timezone(timedelta(hours=8))
TODAY = datetime.now(CST).strftime("%Y-%m-%d")

# ── LLM（默认禁用） ──
_LLM_ENABLED = False
_config_cache = None


def _load_config():
    global _config_cache
    if _config_cache is None:
        cfg = configparser.ConfigParser()
        cfg_path = os.path.join(PROJECT_ROOT, "scripts", "crawler_config.ini")
        if os.path.exists(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg.read_file(f)
        _config_cache = cfg
    return _config_cache


def llm_call(system: str, user: str, max_tokens: int = 4096, timeout: int = 120) -> Optional[str]:
    """LLM 调用，异常返回 None 而非崩溃"""
    cfg = _load_config()
    api_key = os.environ.get("DEEPSEEK_API_KEY") or cfg.get("api", "api_key", fallback="")
    if not api_key:
        logger.warning("API Key 未配置，LLM 不可用")
        return None
    base = cfg.get("api", "base_url", fallback="https://api.deepseek.com")
    model = cfg.get("api", "model", fallback="deepseek-v4-flash")
    try:
        import requests
        r = requests.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json={"model": model, "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ], "temperature": 0.3, "max_tokens": max_tokens},
            timeout=timeout,
        )
        if r.status_code != 200:
            logger.warning("LLM HTTP %s: %s", r.status_code, r.text[:80])
            return None
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        logger.warning("LLM 异常: %s", e)
        return None


# ── Phase 2: 导出 prompt ─────────────────────────────

_EXPORT_PROMPT = """你是一个专业情报分析师。以下是采集到的行业资讯，请筛选出最有价值的条目，并附上联网核实的证据链接。

筛选标准：
1. 与技术趋势、政策变化、市场动态直接相关
2. 信息真实可靠（请务必联网核实）
3. 每条需提供核实后的摘要（1-2句话）和来源链接

输出格式：Markdown
- 标题：[日期] 行业情报精选简报
- 每条：标题 + 核实摘要 + 证据链接(s)"""


def cmd_export(use_llm: bool = False):
    """导出 prompt（Phase 2）"""
    news_path = os.path.join(PROJECT_ROOT, "intel", "output", "news", f"news_{TODAY}.md")
    prompt_dir = os.path.join(PROJECT_ROOT, "intel", "output", "prompt")
    os.makedirs(prompt_dir, exist_ok=True)
    prompt_path = os.path.join(prompt_dir, f"prompt_{TODAY}.md")

    if not os.path.exists(news_path):
        # 也尝试原来的路径
        news_path = os.path.join(PROJECT_ROOT, "news", "news", f"news_{TODAY}.md")
        if not os.path.exists(news_path):
            logger.error("采集文件不存在，请先运行 crawl")
            return

    with open(news_path, "r", encoding="utf-8") as f:
        news_content = f.read()

    # 提取已勾选条目
    selected = []
    for line in news_content.split("\n"):
        if line.strip().startswith("- [x]") or line.strip().startswith("* [x]"):
            selected.append(line.strip())

    if not selected:
        # 如果没有勾选，取全部
        logger.info("未发现勾选条目，使用全部数据")
        selected_text = news_content
    else:
        selected_text = "\n".join(selected)

    prompt_lines = [
        _EXPORT_PROMPT,
        "",
        "---",
        f"## 原始数据 ({TODAY})",
        "",
        selected_text,
    ]

    if use_llm:
        logger.info("LLM 增强 prompt...")
        enhanced = llm_call("你是专业情报分析师", selected_text)
        if enhanced:
            prompt_lines.append("\n---\n## LLM 增强建议\n")
            prompt_lines.append(enhanced)

    content = "\n".join(prompt_lines)
    with open(prompt_path, "w", encoding="utf-8") as f:
        f.write(content)
    logger.info("导出 prompt: %s", prompt_path)
    print(f"\n>>> Prompt: {prompt_path}")


# ── Phase 3: 报告生成 ────────────────────────────────

_REPORT_PROMPT = """你是一个行业分析专家。请基于以下情报数据，撰写问题驱动型分析报告。

格式要求：
1. 选定一个核心问题
2. 交叉分析：学术证据 → 政策动态 → 产业现状
3. 给出判断与建议
4. 使用 Markdown 格式

字数：1500-2500 字"""

_TOPIC_PROMPT = """你是一个社群运营专家。请基于以下情报数据，撰写 4-5 条社群话题帖。

要求：
1. 每条话题 120-250 字
2. 使用中文，语气专业但友好
3. 包含互动引导（提问或讨论点）
4. 可附 emoji"""


def cmd_generate(use_llm: bool = False):
    """生成报告和话题帖（Phase 3）"""
    brief_dir = os.path.join(PROJECT_ROOT, "intel", "output", "brief")
    report_dir = os.path.join(PROJECT_ROOT, "intel", "output", "report")
    topic_dir = os.path.join(PROJECT_ROOT, "intel", "output", "topic")
    os.makedirs(brief_dir, exist_ok=True)
    os.makedirs(report_dir, exist_ok=True)
    os.makedirs(topic_dir, exist_ok=True)

    # 查找最新的 brief 或 news
    brief_path = os.path.join(brief_dir, f"brief_{TODAY}.md")
    if not os.path.exists(brief_path):
        news_path = os.path.join(
            PROJECT_ROOT, "intel", "output", "news", f"news_{TODAY}.md")
        if not os.path.exists(news_path):
            news_path = os.path.join(
                PROJECT_ROOT, "news", "news", f"news_{TODAY}.md")
        if not os.path.exists(news_path):
            logger.error("数据文件不存在，请先完成采集")
            return
        source_path = news_path
        source_label = "news"
    else:
        source_path = brief_path
        source_label = "brief"

    with open(source_path, "r", encoding="utf-8") as f:
        source_data = f.read()

    if not use_llm:
        logger.info("LLM 已禁用，跳过报告生成")
        # 生成占位报告
        placeholder = (
            f"# 分析报告 — {TODAY}\n\n"
            f"> LLM 已禁用，请使用 --llm 启用后重新生成。\n\n"
            f"**数据来源:** {source_label}\n"
            f"**条数:** {len([l for l in source_data.split(chr(10)) if l.strip()])}\n"
        )
        report_path = os.path.join(report_dir, f"report_{TODAY}.md")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(placeholder)
        logger.info("生成占位报告: %s", report_path)
        print(f"\n>>> 报告 (占位): {report_path}")
        return

    logger.info("LLM 生成报告...")
    report = llm_call(_REPORT_PROMPT, source_data)
    if report:
        report_path = os.path.join(report_dir, f"report_{TODAY}.md")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report)
        logger.info("生成报告: %s", report_path)

    logger.info("LLM 生成话题帖...")
    topic = llm_call(_TOPIC_PROMPT, source_data)
    if topic:
        topic_path = os.path.join(topic_dir, f"topic_{TODAY}.md")
        with open(topic_path, "w", encoding="utf-8") as f:
            f.write(topic)
        logger.info("生成话题帖: %s", topic_path)


# ── CLI ──

def run_export_cli():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
    cmd_export(use_llm=False)


def run_generate_cli():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
    cmd_generate(use_llm=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="情报流水线")
    parser.add_argument("command", choices=["export", "generate"])
    parser.add_argument("--llm", action="store_true", help="启用 LLM 生成")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")

    if args.command == "export":
        cmd_export(use_llm=args.llm)
    elif args.command == "generate":
        cmd_generate(use_llm=args.llm)
