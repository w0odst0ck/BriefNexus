"""报告生成器"""
import json
import os
import logging
from datetime import datetime
from typing import List
from intel.core.base import NewsItem, CST

logger = logging.getLogger("intel.reporter")

SECTORS = [
    ("行业大势", "expo", "\U0001f310"),
    ("技术突破", "tech", "\U0001f52c"),
    ("资本脉搏", "finance", "\U0001f4c8"),
    ("供应链深水", "supply", "\u26d3"),
    ("企业交锋", "corp", "\U0001f3f7"),
    ("政策风向", "policy", "\U0001f4cb"),
    ("宏观数据", "macro", "\U0001f4ca"),
]

TODAY = datetime.now(CST).strftime("%Y-%m-%d")
NOW = datetime.now(CST).strftime("%Y-%m-%dT%H:%M:%S+08:00")

def build_report(items: List[NewsItem], fmt: str = "json", output_dir: str = None) -> str:
    if fmt == "json":
        content = _build_json(items)
    else:
        content = _build_md(items)

    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
        fname = f"report_{TODAY}.{fmt}"
        fpath = os.path.join(output_dir, fname)
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info("报告输出: %s", fpath)

    return content

def _build_json(items: List[NewsItem]) -> str:
    report = {
        "meta": {
            "generated_at": NOW,
            "date": TODAY,
            "total_items": len(items),
            "sources": sorted(set(it.source for it in items if it.source)),
            "sectors": sorted(set(it.sector for it in items if it.sector)),
        },
        "items": [{
            "title": it.title,
            "url": it.url,
            "summary": it.summary,
            "date": it.date,
            "source": it.source,
            "domain": it.domain,
            "sector": it.sector,
        } for it in items],
    }
    return json.dumps(report, ensure_ascii=False, indent=2)

def _build_md(items: List[NewsItem]) -> str:
    grouped = {sk: [] for _, sk, _ in SECTORS}
    for it in items:
        grouped.setdefault(it.sector, []).append(it)

    lines = [
        f"# BriefNexus \u8d44\u8baf\u7b80\u62a5 \u2014 {TODAY}",
        "",
        f"**\u751f\u6210\u65f6\u95f4:** {NOW}",
        f"**\u603b\u6761\u6570:** {len(items)}",
        "---",
        "",
    ]
    for name, sk, icon in SECTORS:
        group = grouped.get(sk, [])
        if not group:
            continue
        lines.append(f"## {icon} {name} ({len(group)} \u6761)\n")
        for it in group:
            s = (it.summary[:300]+"...") if len(it.summary) > 300 else it.summary
            lines.append(f"- **{it.title}**")
            if it.date: lines.append(f"  - \U0001f4c5 {it.date}")
            lines.append(f"  - \U0001f517 {it.url}")
            if s: lines.append(f"  - {s}")
            lines.append("")
        lines.append("---\n")
    return "\n".join(lines)
