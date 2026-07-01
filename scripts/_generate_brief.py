#!/usr/bin/env python3
"""
Phase 2.5: 精选摘要生成器（修正版）
解析文件末尾的勾选清单 → 匹配主内容 → LLM 摘要 → brief.md
"""
import json, os, re, sys
sys.stdout.reconfigure(encoding="utf-8")

import requests
import configparser

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crawler_config.ini")
_cfg = configparser.ConfigParser()
with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    _cfg.read_file(f)
LLM_BASE = _cfg.get("api", "base_url")
LLM_KEY = _cfg.get("api", "api_key")
LLM_MODEL = _cfg.get("api", "model")

NEWS_FILE = r"D:\NOTES\zzz\BriefNexus\news\news_2026-06-29.md"
OUTPUT = r"D:\NOTES\zzz\BriefNexus\news\brief_2026-06-29.md"


def parse_news(path):
    """返回 (main_items, checked_numbers)"""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    # 分隔: 主内容 和 勾选清单
    parts = text.split("## \u2705 请勾选")
    if len(parts) < 2:
        print("[ERROR] 未找到勾选清单分区", file=sys.stderr)
        return [], []

    main_part = parts[0]
    check_part = parts[1]

    # --- 解析勾选清单：提取被选中的序号 ---
    checked = []
    for line in check_part.split("\n"):
        m = re.match(r"^- \[x\] (\d+)\.", line.strip())
        if m:
            checked.append(int(m.group(1)))
    checked.sort()

    # --- 解析主内容：提取条目详情 ---
    items = {}
    current_sector = ""

    # 按板块分组
    sector_blocks = re.split(r"\n---\n", main_part)
    for sb in sector_blocks:
        # 板块标题
        sm = re.search(r"## \S (.+?)（\d+条）", sb)
        if sm:
            current_sector = sm.group(1).strip()

        # 条目
        entry_blocks = re.split(r"\n### (\d+)\.\s*", sb)
        # entry_blocks 格式: ["前文", "1", "标题 + 详情", "2", "标题 + 详情", ...]
        i = 1
        while i < len(entry_blocks):
            num = int(entry_blocks[i].strip())
            content = entry_blocks[i + 1] if i + 1 < len(entry_blocks) else ""
            i += 2

            # 提取标题（去掉 `[照明]` 后缀）
            title = re.sub(r"\s*`\[.*?\]`\s*$", "", content.split("\n")[0].strip())

            # 提取 URL
            url_m = re.search(r"\*\*来源\*\*：(https?://[^\s]+)", content)
            url = url_m.group(1) if url_m else ""

            # 提取日期
            date_m = re.search(r"\*\*日期\*\*：([^\n]+)", content)
            date = date_m.group(1).strip() if date_m else ""

            # 提取源站
            src_m = re.search(r"\*\*源站\*\*：(.+)", content)
            src = src_m.group(1).strip() if src_m else ""

            items[num] = {
                "title": title,
                "url": url,
                "date": date,
                "source": src,
                "sector": current_sector,
            }

    # 按 checked 序号收集
    selected = [items[n] for n in checked if n in items]
    return selected, checked


def summarize_batch(items):
    titles = [it["title"] for it in items]
    prompt = f"""为以下每条新闻写一句中文摘要，不超过30字。
直接返回 JSON 数组，不要多余文字。

{json.dumps(titles, ensure_ascii=False, indent=2)}"""
    try:
        r = requests.post(
            f"{LLM_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {LLM_KEY}", "Content-Type": "application/json"},
            json={
                "model": LLM_MODEL,
                "messages": [
                    {"role": "system", "content": "你是行业分析师，负责为新闻标题生成简洁摘要。"},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.1,
                "max_tokens": 1024,
            },
            timeout=30,
        )
        if r.status_code != 200:
            print(f"  API error: {r.status_code}", file=sys.stderr)
            return [""] * len(items)
        content = r.json()["choices"][0]["message"]["content"].strip()
        print(f"  LLM raw: {content[:100]}...", file=sys.stderr)
        # 尝试解析多种格式
        for parser in [
            lambda c: json.loads(c) if c.startswith("[") else None,
            lambda c: json.loads(c)["summaries"] if c.startswith("{") else None,
            lambda c: json.loads(re.search(r"\[(.*?)\]", c, re.DOTALL).group(0)),
        ]:
            try:
                result = parser(content)
                if result and len(result) == len(titles):
                    return result
            except:
                continue
        print(f"  [WARN] 无法解析: {content[:100]}", file=sys.stderr)
    except Exception as e:
        print(f"  摘要出错: {e}", file=sys.stderr)
    return [""] * len(items)


def main():
    print("解析勾选条目...", file=sys.stderr)
    items, nums = parse_news(NEWS_FILE)
    print(f"  选中: {nums}", file=sys.stderr)
    print(f"  共 {len(items)} 条，覆盖 {len(set(it['sector'] for it in items))} 个板块", file=sys.stderr)

    print("批量生成摘要...", file=sys.stderr)
    summaries = summarize_batch(items)

    # 按板块分组合并
    sector_order = ["行业大势", "技术突破", "资本脉搏", "供应链深水", "企业交锋", "场景新战场", "招标市场"]
    grouped = {s: [] for s in sector_order}
    for it, sm in zip(items, summaries):
        sc = it["sector"]
        if sc in grouped:
            grouped[sc].append({**it, "summary": sm})

    lines = [
        "# 智能照明与智能建筑 — 精选简报",
        f"> 生成时间：2026-06-29 | 精选 {len(items)} 条 | 节省约 80% tokens",
        "",
    ]
    seq = 0
    for sk in sector_order:
        sector_items = grouped[sk]
        if not sector_items:
            continue
        lines.append(f"---\n## {sk}\n")
        for it in sector_items:
            seq += 1
            lines.append(f"### {seq}. {it['title']}")
            lines.append(f"- **板块**：{sk}")
            lines.append(f"- **摘要**：{it['summary']}")
            lines.append(f"- **来源**：{it['source']} | {it['date']}")
            lines.append("")

    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"\n输出: {OUTPUT}", file=sys.stderr)
    print(f"{len(items)} 条精选 -> brief.md 约 500 tokens", file=sys.stderr)

    # 显示结果
    for it, sm in zip(items, summaries):
        print(f"  [{it['sector'][:4]}] {it['title'][:35]:35s} | {sm[:30] if sm else '（待补充）'}")


if __name__ == "__main__":
    main()
