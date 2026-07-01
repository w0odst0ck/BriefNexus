#!/usr/bin/env python3
"""导出已勾选标题为 head.md + prompt.md"""
import os, re, sys

NEWS_FILE = r"D:\NOTES\zzz\BriefNexus\news\news\news_2026-06-29.md"
HEAD_OUT = r"D:\NOTES\zzz\BriefNexus\news\prompt\head_2026-06-29.md"
PROMPT_OUT = r"D:\NOTES\zzz\BriefNexus\news\prompt\prompt_2026-06-29.md"

with open(NEWS_FILE, "r", encoding="utf-8") as f:
    text = f.read()

parts = text.split("## \u2705 请勾选")
main_part, check_part = parts[0], parts[1]

checked = []
for line in check_part.split("\n"):
    m = re.match(r"^- \[x\] (\d+)\.", line.strip())
    if m:
        checked.append(int(m.group(1)))

items = {}
current_sector = ""
for sb in re.split(r"\n---\n", main_part):
    sm = re.search(r"## \S (.+?)（\d+条）", sb)
    if sm:
        current_sector = sm.group(1).strip()
    entries = re.split(r"\n### (\d+)\.\s*", sb)
    i = 1
    while i < len(entries):
        num = int(entries[i])
        content = entries[i+1] if i+1 < len(entries) else ""
        i += 2
        title = re.sub(r"\s*`\[.*?\]`\s*$", "", content.split("\n")[0].strip())
        url_m = re.search(r"\*\*来源\*\*：(https?://[^\s]+)", content)
        src_m = re.search(r"\*\*源站\*\*：(.+)", content)
        date_m = re.search(r"\*\*日期\*\*：([^\n]+)", content)
        items[num] = {
            "title": title,
            "url": url_m.group(1) if url_m else "",
            "source": src_m.group(1).strip() if src_m else "",
            "date": date_m.group(1).strip() if date_m else "",
            "sector": current_sector,
        }

selected = [items[n] for n in checked if n in items]

# ── head.md ──────────────────────────────────────────
head = [
    "# 智能照明与智能建筑 — 待核实标题清单",
    "> 请逐条核对，确认标题准确、事实无误。如有问题直接在下面备注。",
    f"> 共 {len(selected)} 条 | 生成时间：2026-06-29",
    "",
    "---",
    "",
]
for i, it in enumerate(selected, 1):
    head += [
        f"## {i}. {it['title']}",
        "",
        f"- **板块**：{it['sector']}",
        f"- **来源**：{it['source']}  |  {it['date']}",
        f"- **链接**：{it['url']}",
        f"- **核对备注**：（如无疑问可留空）",
        "",
    ]
head += [
    "---",
    "",
    "### 整体确认",
    "- [ ] 以上标题均无误，可以用于报告/话题生成",
    "- [ ] 有修正（已在备注中注明）",
]

with open(HEAD_OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(head))

# ── prompt.md ────────────────────────────────────────
prompt = [
    "# 智能照明与智能建筑资讯 — 联网核实与简报生成",
    "",
    "> ⚠️ **请在 DeepSeek 网页端使用此提示词，并打开「联网搜索」功能。**",
    ">",
    "> 任务：逐条搜索下方链接，核实新闻事实，输出结构化简报 brief.md。",
    "",
    "---",
    "",
    "## 任务说明",
    "",
    "你是行业研究助理。以下 11 条新闻标题来自智能照明与智能建筑领域的行业媒体。请执行：",
    "",
    "1. **逐条联网搜索**每条链接，确认标题所述事件是否真实存在",
    "2. **提取核心事实**：谁、做了什么、时间、关键数据",
    "3. **如有偏差**，以搜索到的实际内容为准修正标题表述",
    "4. **输出 structured brief.md**（格式见下方模板）",
    "",
    "---",
    "",
    "## 待核实条目",
    "",
]
for i, it in enumerate(selected, 1):
    prompt += [
        f"### {i}. {it['title']}",
        f"- 链接：{it['url']}",
        f"- 板块：{it['sector']}",
        "",
    ]

prompt += [
    "---",
    "",
    "## 输出模板：brief.md",
    "",
    "按以下格式输出，保存为 brief_2026-06-29.md：",
    "",
    "```markdown",
    "# 智能照明与智能建筑 — 精选简报（联网核实版）",
    "> 生成时间：2026-06-29 | 共 11 条 | ✅ 已联网核实",
    "",
    "---",
    "",
    "## 行业大势",
    "",
    "### 1. {标题}",
    "- **核实结果**：✅ 确认 | {如标题有偏差，写修正说明}",
    "- **摘要**：{一句话核心事实，含数据}",
    "- **来源**：{来源网站名称}",
    "- **链接**：{URL}",
    "",
    "### 2. ...",
    "...",
    "",
    "## 技术突破",
    "",
    "### 3. ...",
    "...",
    "",
    "---",
    "",
    "## 附录：核实说明",
    "- 所有条目已通过来源链接页面核实",
    "- 渠道：LEDinside 中文站",
    "- 日期范围：2026-06-23 ~ 2026-06-29",
    "```",
    "",
    "---",
    "",
    "## 注意事项",
    "",
    "- 如果链接打不开或内容不符，注明\"⚠️ 未确认\"并说明原因",
    "- 摘要尽量包含具体数据（金额、增长率、时间节点等）",
    "- 每条不超过 100 字",
    "- 输出完成后，将 brief.md 内容发送给我",
]

with open(PROMPT_OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(prompt))

print(f"已生成：", file=sys.stderr)
print(f"  {HEAD_OUT}  ← 人工核对用", file=sys.stderr)
print(f"  {PROMPT_OUT} ← DeepSeek 网页端用", file=sys.stderr)
