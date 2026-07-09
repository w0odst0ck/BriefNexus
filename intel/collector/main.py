#!/usr/bin/env python3
"""
情报采集 — 统一入口

默认仅使用规则分类（无需 LLM），大幅提升稳定性。
如需 LLM 增强分类，传入 --llm。

用法:
  python -m intel.main crawl                    # 仅规则分类
  python -m intel.main crawl --llm              # 启用 LLM 增强分类
  python -m intel.main crawl --max-age 14       # 采集近14天
"""

import argparse
import configparser
import hashlib
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import List, Optional

from .platforms.base import BaseCollector, NewsItem, CST
from .platforms.white_house import WhiteHouseCollector
from .platforms.eu_commission import EUCommissionCollector
from .platforms.nvidia_blog import NvidiaBlogCollector
from .platforms.globenewswire import GlobeNewswireCollector
from .platforms.sec_edgar import SECEdgarCollector
from .platforms.federal_reserve import FederalReserveCollector

logger = logging.getLogger("intel.collector")

# ── 全局 ──
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TODAY = datetime.now(CST).strftime("%Y-%m-%d")

# ── 板块体系 ──
SECTORS = [
    ("行业大势", "expo", "\U0001f310"),
    ("技术突破", "tech", "\U0001f52c"),
    ("资本脉搏", "finance", "\U0001f4c8"),
    ("供应链深水", "supply", "\u26d3"),
    ("企业交锋", "corp", "\U0001f3f7"),
    ("政策风向", "policy", "\U0001f4cb"),
    ("宏观数据", "macro", "\U0001f4ca"),
]
SECTOR_NAMES = {s[1]: s[0] for s in SECTORS}
SECTOR_KEYS = {s[1] for s in SECTORS}
SECTOR_ICONS = {s[1]: s[2] for s in SECTORS}

UA = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/128.0.0.0 Safari/537.36",
]

# ── LLM 配置（默认禁用） ──
_config_cache = None
_LLM_ENABLED = False


def _load_config():
    global _config_cache
    if _config_cache is None:
        cfg = configparser.ConfigParser()
        cfg_path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))),
            "scripts", "crawler_config.ini")
        if os.path.exists(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg.read_file(f)
        _config_cache = cfg
    return _config_cache


def _llm_call(system: str, user: str, max_tokens: int = 4096, timeout: int = 120):
    """LLM 调用，异常时不崩溃"""
    cfg = _load_config()
    api_key = os.environ.get("DEEPSEEK_API_KEY") or cfg.get("api", "api_key", fallback="")
    if not api_key:
        logger.warning("LLM 不可用: API Key 未配置")
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
            ], "temperature": 0.1, "max_tokens": max_tokens},
            timeout=timeout,
        )
        if r.status_code != 200:
            logger.warning("LLM HTTP %s: %s", r.status_code, r.text[:80])
            return None
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        logger.warning("LLM 调用异常: %s", e)
        return None


# ── 分类 ──

_CLASSIFY_SYSTEM_PROMPT = """你是一个全球科技与宏观资讯的分类器。
根据资讯标题，将以下列表中的每条分类到以下板块之一（只返回JSON数组）：

expo=行业大势(市场趋势/行业报告/展览)
tech=技术突破(AI/ML/GPGPU/机器人/网络安全/芯片)
finance=资本脉搏(财报/IPO/投融资/并购)
supply=供应链深水(半导体制造/芯片产能/晶圆代工)
corp=企业交锋(企业合作/产品发布/品牌)
policy=政策风向(AI法案/出口管制/贸易政策/制裁)
macro=宏观数据(利率/GDP/通胀/就业/国债)

输入是一个字符串列表，输出是一个板块key列表。"""


def _classify_rules(title: str) -> str:
    """规则分类（无需 LLM）"""
    t = title
    if re.search(r"(AI|人工智能|Machine Learning|LLM|大模型|GPU|机器人|robot|cyber|security|自动驾驶|quantum)", t, re.IGNORECASE):
        return "tech"
    if re.search(r"(财报|earnings|revenue|Q[1-4]|IPO|融资|投资|acquisition|merger|并购|dividend)", t, re.IGNORECASE):
        return "finance"
    if re.search(r"(fab|产能|chip|semiconductor|wafer|制造|晶圆|supply chain|短缺|TSMC|台积电)", t, re.IGNORECASE):
        return "supply"
    if re.search(r"(partnership|合作|launch|发布|product|strategic|alliance)", t, re.IGNORECASE):
        return "corp"
    if re.search(r"(AI Act|tariff|export control|sanction|regulation|法案|出口管制|贸易|政策|policy)", t, re.IGNORECASE):
        return "policy"
    if re.search(r"(interest rate|GDP|inflation|CPI|就业|unemployment|国债|treasury|联邦基金|美联储)", t, re.IGNORECASE):
        return "macro"
    if re.search(r"(report|趋势|market|行业|展)", t):
        return "expo"
    return "expo"


def classify_batch(items: List[NewsItem], use_llm: bool = False):
    """批量分类，优先规则，可选 LLM 增强"""
    titles = [it.title for it in items]

    # LLM 分类（仅在启用时尝试）
    llm_result = None
    if use_llm:
        from ..utils import llm_call_graceful
        result = _llm_call(_CLASSIFY_SYSTEM_PROMPT, json.dumps(titles, ensure_ascii=False),
                           max_tokens=512, timeout=30)
        if result:
            try:
                if result.startswith("["):
                    llm_result = json.loads(result)
                else:
                    import re as _re
                    m = _re.search(r"```(?:json)?\s*([\s\S]*?)```", result)
                    if m:
                        llm_result = json.loads(m.group(1))
            except:
                logger.warning("LLM 分类解析失败，回退到规则")

    # 逐条分类
    for i, it in enumerate(items):
        if llm_result and i < len(llm_result) and llm_result[i] in SECTOR_KEYS:
            it.sector = llm_result[i]
        else:
            it.sector = _classify_rules(it.title)


# ── 工具 ──

def _sess():
    import requests
    s = requests.Session()
    s.headers["User-Agent"] = random.choice(UA)
    s.headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    return s


def _delay():
    time.sleep(random.uniform(0.8, 2.0))


# ── 输出 ──

def build_markdown(items: List[NewsItem]) -> str:
    """构建按板块分组的 Markdown"""
    grouped = {sk: [] for sk in SECTOR_KEYS}
    for it in items:
        grouped.setdefault(it.sector, []).append(it)

    lines = [
        f"# BriefNexus 全球科技与宏观资讯简报 — {TODAY}",
        "",
        f"**采集时间:** {datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')}",
        f"**总条数:** {len(items)}",
        f"**数据源:** {', '.join(sorted(set(it.source for it in items)))}",
        "",
        "---",
        "",
    ]

    for name, sk, icon in SECTORS:
        group = grouped.get(sk, [])
        if not group:
            continue
        lines.append(f"## {icon} {name} ({len(group)} 条)")
        lines.append("")
        for it in group:
            domain_tag = f"[{it.domain}]" if it.domain else ""
            source_tag = f"[{it.source}]"
            checkbox = "- [ ]"
            # 摘要截断至300字
            summary = (it.summary[:300] + "..." ) if len(it.summary) > 300 else it.summary
            lines.append(f"{checkbox} {domain_tag}{source_tag} **{it.title}**")
            if it.date:
                lines.append(f"  - 📅 {it.date}")
            lines.append(f"  - 🔗 {it.url}")
            if summary:
                lines.append(f"  - {summary}")
            lines.append("")
        lines.append("---")
        lines.append("")

    # 底部来源概况表
    lines.append("## 📊 来源概况")
    lines.append("")
    lines.append("| 数据源 | 类型 | 条数 |")
    lines.append("|--------|------|------|")
    from collections import Counter
    source_counter = Counter((it.source, it.domain) for it in items)
    for (src, dom), cnt in sorted(source_counter.items()):
        lines.append(f"| {src} | {dom} | {cnt} |")
    lines.append("")

    return "\n".join(lines)


def save_output(items: List[NewsItem], output_dir: str):
    """保存采集结果"""
    os.makedirs(output_dir, exist_ok=True)
    content = build_markdown(items)
    outpath = os.path.join(output_dir, f"news_{TODAY}.md")
    with open(outpath, "w", encoding="utf-8") as f:
        f.write(content)
    logger.info("输出: %s (%d 条)", outpath, len(items))
    return outpath


# ── 主采集 ──

def load_platforms(config_path: str = None) -> list:
    """从配置加载平台适配器

    config.ini 格式:
        [sources]
        enabled = arxiv:intel.collector.platforms.arxiv:ArxivCollector,
                  csa:intel.collector.platforms.csa:CsaCollector

    扩展方式：编写平台适配器类继承 BaseCollector，在 enabled 中注册即可。
    """
    cfg = configparser.ConfigParser()
    if config_path and os.path.exists(config_path):
        cfg.read(config_path, encoding="utf-8")
    else:
        # 自动查找配置文件
        for candidate in [
            os.path.join(PROJECT_ROOT, "intel", "intel_config.ini"),
        ]:
            if os.path.exists(candidate):
                cfg.read(candidate, encoding="utf-8")
                break

    sources_cfg = cfg.get("sources", "enabled", fallback="")
    if not sources_cfg:
        logger.warning("未配置数据源，使用默认内置源(v2)")
        return [
            WhiteHouseCollector(),
            EUCommissionCollector(),
            NvidiaBlogCollector(),
            GlobeNewswireCollector(),
            SECEdgarCollector(),
            FederalReserveCollector(),
        ]

    platforms = []
    for entry in sources_cfg.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) != 3:
            logger.warning("配置格式错误(需 name:module:Class): %s", entry)
            continue
        name, module_path, class_name = parts
        try:
            import importlib
            mod = importlib.import_module(module_path)
            cls = getattr(mod, class_name)
            instance = cls()
            platforms.append(instance)
            logger.info("加载平台: %s → %s", name, class_name)
        except Exception as e:
            logger.warning("加载平台失败 [%s]: %s", name, e)

    if not platforms:
        logger.warning("无可用平台，使用默认内置源(v2)")
        return [
            WhiteHouseCollector(),
            EUCommissionCollector(),
            NvidiaBlogCollector(),
            GlobeNewswireCollector(),
            SECEdgarCollector(),
            FederalReserveCollector(),
        ]

    return platforms


SOURCES = load_platforms()


def run_crawl(max_age: int = 7, use_llm: bool = False) -> List[NewsItem]:
    """执行全量采集"""
    logger.info("=" * 50)
    logger.info("情报采集 — 近 %d 天", max_age)
    logger.info("LLM 分类: %s", "启用" if use_llm else "禁用（仅规则）")
    logger.info("数据源: %s", ", ".join(s.display_name for s in SOURCES))
    logger.info("=" * 50)

    sess = _sess()
    all_items = []
    seen = set()

    for source in SOURCES:
        logger.info("[%s] 采集...", source.display_name)
        try:
            items = source.crawl(sess)
            for it in items:
                dk = hashlib.md5(it.title.encode()).hexdigest()
                if dk not in seen:
                    seen.add(dk)
                    all_items.append(it)
            logger.info("  → %d 条", len(items))
        except Exception as e:
            logger.error("  [FAIL] %s: %s", source.display_name, e)

    classify_batch(all_items, use_llm=use_llm)

    logger.info("=" * 50)
    logger.info("采集完成: %d 条", len(all_items))
    logger.info("=" * 50)
    return all_items


# ── CLI ──

def run_crawl_cli(args):
    """CLI 入口"""
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    max_age = getattr(args, "max_age", 7)
    use_llm = getattr(args, "llm", False)

    items = run_crawl(max_age=max_age, use_llm=use_llm)

    # 输出到 intel/output/
    output_dir = os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))),
        "intel", "output", "news")
    outpath = save_output(items, output_dir)
    print(f"\n>>> 完成: {len(items)} 条 | 输出: {outpath}")
