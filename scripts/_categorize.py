"""按板块分类新闻条目"""
import re

with open(r"D:\NOTES\zzz\BriefNexus\news\news_2026-06-29.md", "r", encoding="utf-8") as f:
    text = f.read()

items = re.findall(r"### \d+\. (.+)", text)
for i, t in enumerate(items, 1):
    print(f"{i:2d}. {t[:60]}")
