"""从 news.md 提取已勾选条目的精简摘要"""
import re

with open(r"D:\NOTES\zzz\BriefNexus\news\news_2026-06-29.md", "r", encoding="utf-8") as f:
    text = f.read()

# 解析每个条目的区块
blocks = re.split(r"\n### (?=\d+\.)", text)
selected = []
current_sector = ""

for block in blocks:
    # 判断所属板块
    m = re.search(r"## (.) ([^\n]+)（\d+条）", block)
    if m:
        current_sector = m.group(2).strip()
    
    # 检查是否被选中
    if not re.search(r"^- \[x\]", block, re.MULTILINE):
        continue
    
    # 提取字段
    title_m = re.search(r"### (\d+)\.\s*(.+?)(?:`\[.*?\]`)?\s*$", block, re.MULTILINE)
    url_m = re.search(r"\*\*来源\*\*：(https?://[^\s]+)", block)
    date_m = re.search(r"\*\*日期\*\*：([^/]+)", block)
    
    title = title_m.group(2).strip() if title_m else ""
    url = url_m.group(1) if url_m else ""
    date = date_m.group(1).strip() if date_m else ""
    
    selected.append({
        "title": title,
        "url": url,
        "date": date,
        "sector": current_sector,
        "source": "阿拉丁照明" if "alighting" in url else "LEDinside" if "ledinside" in url else "其他",
    })

# 输出精简摘要
lines = [
    "# 智能照明与智能建筑资讯简报 — 精选摘要",
    f"> 生成时间：2026-06-29 | 选中 {len(selected)} 条 / 来源：{len(set(it['source'] for it in selected))} 个源",
    "",
    f"**共涉及板块：** {' · '.join(set(it['sector'] for it in selected))}",
    "",
]

for i, it in enumerate(selected, 1):
    domain = "照明/光电子" if it["source"] == "LEDinside" else "照明行业"
    lines.append(f"### {i}. {it['title']}")
    lines.append(f"- **板块**：{it['sector']}")
    lines.append(f"- **来源**：{it['source']} | 日期：{it['date']}")
    lines.append(f"- **摘要**：待 AI 生成")
    lines.append("")

outpath = r"D:\NOTES\zzz\BriefNexus\news\brief_2026-06-29.md"
with open(outpath, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))

for it in selected:
    print(f"  [{it['sector']}] {it['title'][:45]}")
