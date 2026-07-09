#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能照明与智能建筑资讯 Pipeline v2
数据源：学术(arXiv) + 政策(CSA联盟) + 政府(住建委)
产出：问题驱动型分析报告

用法:
  pipeline.bat crawl           # Phase 1: 采集
  pipeline.bat export          # Phase 2: 导出 prompt
  pipeline.bat generate        # Phase 3: 生成 report+topic
  pipeline.bat all             # 全流程
"""

import argparse, configparser, hashlib, json, os, random, re, sys, time, xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from typing import Optional
import requests
from bs4 import BeautifulSoup

# encoding for Windows console
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

# -- paths --
BASE = r"D:\NOTES\zzz\BriefNexus"
DIRS = {
    "news_dir":   os.path.join(BASE, "news", "news"),
    "brief_dir":  os.path.join(BASE, "news", "brief"),
    "prompt_dir": os.path.join(BASE, "news", "prompt"),
    "report_dir": os.path.join(BASE, "report"),
    "topic_dir":  os.path.join(BASE, "topic"),
}

# load .env (if exists) + api config
from _dotenv import load_project_env; load_project_env()
_cfg_obj = configparser.ConfigParser()
_cfg_obj.read(os.path.join(os.path.dirname(os.path.abspath(__file__)), "crawler_config.ini"), encoding="utf-8")
LLM_BASE = _cfg_obj.get("api", "base_url")
LLM_KEY = os.environ.get("DEEPSEEK_API_KEY") or _cfg_obj.get("api", "api_key")
LLM_MODEL = _cfg_obj.get("api", "model")

CST = timezone(timedelta(hours=8))
TODAY = datetime.now(CST).strftime("%Y-%m-%d")
TODAY_DT = datetime.now(CST)


# -- helpers --
def ensure_dirs():
    for k in DIRS:
        os.makedirs(DIRS[k], exist_ok=True)


def llm_call(system, user, max_tokens=4096):
    try:
        r = requests.post(
            f"{LLM_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {LLM_KEY}", "Content-Type": "application/json"},
            json={"model": LLM_MODEL, "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ], "temperature": 0.3, "max_tokens": max_tokens},
            timeout=120,
        )
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"  [ERROR] LLM: {e}", file=sys.stderr)
        sys.exit(1)


def p(kind):
    return os.path.join(DIRS[kind + "_dir"], f"{kind}_{TODAY}.md")


# ══════════════════════════════════════════════════════════
#  Phase 1: 采集
# ══════════════════════════════════════════════════════════

@dataclass
class Item:
    title: str
    url: str
    date: Optional[datetime] = None
    summary: str = ""
    source: str = ""
    stype: str = ""

    def dk(self):
        return hashlib.md5(self.title.encode()).hexdigest()


def _fetch(url, sess):
    try:
        r = sess.get(url, timeout=20)
        r.encoding = r.apparent_encoding or "utf-8"
        return r.text
    except Exception as e:
        print(f"  [WARN] {url[:60]} - {e}", file=sys.stderr)
        return None


# arXiv
_ARXIV_Q = [
    ("smart lighting", "all:smart+AND+all:lighting"),
    ("smart building", "all:smart+AND+all:building+AND+(energy+OR+lighting)"),
    ("LiFi", "all:LiFi+OR+(visible+AND+light+AND+communication)"),
    ("MicroLED", "all:MicroLED+OR+(micro+AND+LED+AND+display)"),
]

def _arxiv(sess):
    items = []
    print("[arXiv] 抓取中...", file=sys.stderr)
    for label, q in _ARXIV_Q:
        url = f"http://export.arxiv.org/api/query?search_query={q}&max_results=3&sortBy=submittedDate&sortOrder=descending"
        xml = _fetch(url, sess)
        if not xml:
            continue
        try:
            root = ET.fromstring(xml)
            ns = {"a": "http://www.w3.org/2005/Atom"}
            for e in root.findall("a:entry", ns):
                t = e.find("a:title", ns)
                li = e.find("a:id", ns)
                pub = e.find("a:published", ns)
                s = e.find("a:summary", ns)
                if t is not None and li is not None:
                    ti = t.text.strip().replace("\n", " ")
                    d = None
                    if pub is not None:
                        try:
                            d = datetime.strptime(pub.text.strip()[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                        except:
                            pass
                    ss = s.text.strip().replace("\n", " ") if s is not None else ""
                    items.append(Item(title=ti, url=li.text.strip(), date=d, summary=ss, source="arXiv", stype="academic"))
            time.sleep(3)
        except Exception as e:
            print(f"  [WARN] arXiv {label}: {e}", file=sys.stderr)
    print(f"  -> {len(items)} 条", file=sys.stderr)
    return items


# CSA
def _csa(sess):
    items = []
    print("[CSA联盟] 抓取中...", file=sys.stderr)
    for page in ["https://www.china-led.net/news/", "https://www.china-led.net/policy/"]:
        html = _fetch(page, sess)
        if not html:
            continue
        soup = BeautifulSoup(html, "lxml")
        for a in soup.select("a[href*='china-led.net']"):
            href = a.get("href", "")
            title = a.get_text(strip=True)
            if not title or len(title) < 8 or not href:
                continue
            if not href.startswith("http"):
                href = "https://www.china-led.net" + href
            items.append(Item(title=title, url=href, source="CSA联盟", stype="policy"))
        time.sleep(1)
    print(f"  -> {len(items)} 条", file=sys.stderr)
    return items


# 上海住建委
def _shgov(sess):
    items = []
    print("[上海住建委] 抓取中...", file=sys.stderr)
    for page in ["https://zjw.sh.gov.cn/gsgg/index.html", "https://zjw.sh.gov.cn/zcfg/index.html"]:
        html = _fetch(page, sess)
        if not html:
            continue
        soup = BeautifulSoup(html, "lxml")
        for a in soup.select("a[href*='/gsgg/'], a[href*='/zcfg/']"):
            href = a.get("href", "")
            title = a.get_text(strip=True)
            if not title or len(title) < 8:
                continue
            if href.startswith("/"):
                href = "https://zjw.sh.gov.cn" + href
            items.append(Item(title=title, url=href, source="上海住建委", stype="gov"))
        time.sleep(1)
    print(f"  -> {len(items)} 条", file=sys.stderr)
    return items


def cmd_crawl():
    sess = requests.Session()
    sess.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/128"
    all_items = _arxiv(sess) + _csa(sess) + _shgov(sess)
    sess.close()

    seen = set()
    dedup = []
    for it in all_items:
        k = it.dk()
        if k not in seen:
            seen.add(k)
            dedup.append(it)
    dedup.sort(key=lambda x: x.date or TODAY_DT, reverse=True)

    tmap = {"academic": ("学术论文", "\U0001f4d6"), "policy": ("政策产业", "\U0001f3db"), "gov": ("政府公告", "\U0001f3e0")}
    grp = {}
    for it in dedup:
        grp.setdefault(it.stype, []).append(it)

    seq = 0
    lines = [
        "# 智能照明与智能建筑 - 多源情报简报",
        f"> 采集时间：{TODAY} | 共 {len(dedup)} 条 | 学术/政策/政府",
        "",
    ]
    for st in ["academic", "policy", "gov"]:
        sitems = grp.get(st, [])
        if not sitems:
            continue
        label, icon = tmap[st]
        lines.append(f"---\n## {icon} {label}（{len(sitems)}条）\n")
        for it in sitems:
            seq += 1
            ds = it.date.strftime("%Y-%m-%d") if it.date else ""
            lines.append(f"### {seq}. {it.title}")
            if ds:
                lines.append(f"- **日期**：{ds}")
            lines.append(f"- **来源**：{it.source} | {it.url}")
            if it.summary:
                lines.append(f"- **摘要**：{it.summary}")
            lines.append("")

    lines.append("---\n")
    lines.append("## \u2705 请勾选你想要深入分析的条目\n")
    ss = 0
    for st in ["academic", "policy", "gov"]:
        sitems = grp.get(st, [])
        if not sitems:
            continue
        _, icon = tmap[st]
        lines.append(f"**{icon} {tmap[st][0]}**\n")
        for it in sitems:
            ss += 1
            lines.append(f"- [ ] {ss}. {it.title}")
        lines.append("")

    out = p("news")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    for st in ["academic", "policy", "gov"]:
        c = len(grp.get(st, []))
        if c:
            print(f"  {tmap[st][1]} {tmap[st][0]}: {c}条", file=sys.stderr)
    print(f"  -> {out}", file=sys.stderr)


# ══════════════════════════════════════════════════════════
#  Phase 2: 导出 prompt
# ══════════════════════════════════════════════════════════
def cmd_export():
    np = p("news")
    if not os.path.exists(np):
        print(f"[ERROR] 未找到 {np}", file=sys.stderr)
        sys.exit(1)

    with open(np, "r", encoding="utf-8") as f:
        text = f.read()

    parts = text.split("## \u2705 \u8bf7\u52fe\u9009")
    if len(parts) < 2:
        print("[ERROR] 未找到勾选清单", file=sys.stderr)
        sys.exit(1)

    cp = parts[1]
    nums = []
    for line in cp.split("\n"):
        m = re.match(r"^- \[x\] (\d+)\.", line.strip())
        if m:
            nums.append(int(m.group(1)))
    if not nums:
        print("[ERROR] 未找到勾选标记。请先在 news.md 中用 [x] 勾选", file=sys.stderr)
        sys.exit(1)

    # 解析主内容
    mp = parts[0]
    imap, cur = {}, ""
    for sb in re.split(r"\n---\n", mp):
        sm = re.search(r"## . (.+?)（\d+条）", sb)
        if sm:
            cur = sm.group(1).strip()
        entries = re.split(r"\n### (\d+)\.\s*", sb)
        i = 1
        while i < len(entries):
            n = int(entries[i])
            content = entries[i + 1] if i + 1 < len(entries) else ""
            i += 2
            title = content.split("\n")[0].strip()
            # 支持两种来源格式:
            # v1: {source} | {url}  旧格式
            # v2: {url} + 源站：{source}
            um = re.search(r"\*\*来源\*\*：.+?\| (.+)", content)
            if not um:
                um = re.search(r"\*\*来源\*\*：(.+?)\n", content)
            sm = re.search(r"\*\*源站\*\*：(.+?)\n", content)
            dm = re.search(r"\*\*日期\*\*：(.+?)\n", content)
            imap[n] = dict(title=title,
                           url=um.group(1).strip() if um else "",
                           date=dm.group(1).strip() if dm else "",
                           stype=sm.group(1).strip() if sm else cur)

    selected = [imap[n] for n in nums if n in imap]

    prompt = [
        "# 智能照明与智能建筑 - 联网核实与简报生成",
        "",
        "> \u26a0\ufe0f 请在 DeepSeek 网页端使用此提示词，开启「联网搜索」",
        "> 任务：搜索以下条目的原文，核实事实，输出简报",
        "",
        "---\n## 待核实条目\n",
    ]
    for i, it in enumerate(selected, 1):
        prompt += [f"### {i}. {it['title']}", f"- 链接：{it['url']}", f"- 类别：{it['stype']}\n"]

    prompt += [
        "---\n## 输出格式",
        "每条：",
        "```",
        "### N. 标题",
        "- **核实**：确认/修正",
        "- **摘要**：核心事实（含数据）",
        "- **来源**：{网站}",
        "```\n",
        "---\n## 提示",
        "- 学术论文用中文写摘要",
        "- 包含具体数据",
        "- 输出保存到 brief/brief_{date}.md\n",
    ]

    pp = p("prompt")
    os.makedirs(DIRS["prompt_dir"], exist_ok=True)
    with open(pp, "w", encoding="utf-8") as f:
        f.write("\n".join(prompt))

    print(f"  已勾选: {len(selected)} 条", file=sys.stderr)
    print(f"  prompt: {pp}", file=sys.stderr)
    print(f"\n  下一步: 复制 prompt.md 到 DeepSeek 网页端（开联网搜索）", file=sys.stderr)
    print(f"          -> 将结果保存到 brief/brief_{TODAY}.md", file=sys.stderr)


# ══════════════════════════════════════════════════════════
#  Phase 3: 生成
# ══════════════════════════════════════════════════════════
def cmd_generate():
    bp = p("brief")
    if not os.path.exists(bp):
        print(f"[ERROR] 未找到 {bp}", file=sys.stderr)
        sys.exit(1)

    with open(bp, "r", encoding="utf-8") as f:
        brief = f.read()

    print(f"读取 brief.md ({len(brief)} chars)", file=sys.stderr)

    # report
    print("生成 report.md...", file=sys.stderr)
    rp = f"""你是一位智能照明与建筑智能化领域的行业分析师。
基于以下已联网核实的多源情报（学术论文 + 政策 + 政府），写一份问题驱动型分析简报。

简报内容：
{brief}

## 写作框架
1. 本期核心问题：从情报中提炼 1-2 个值得关注的核心问题
2. 证据：分学术/政策/产业三个维度，列出相关事实
3. 交叉分析：学术研究是否指向同一方向？政策是否在跟进？产业落地了吗？
4. 判断与建议：我的分析判断 + 对行业参与者的启示

## 规则
- 每段第一句必须是观点
- 句子不超过 30 字，总字数 1500-2500
- 直接输出 markdown，不要额外说明"""

    rc = llm_call("你是智能照明行业分析师。输出问题驱动型分析，不是新闻摘要。", rp, 4096)
    rp2 = p("report")
    with open(rp2, "w", encoding="utf-8") as f:
        f.write(rc)
    print(f"  -> {rp2}", file=sys.stderr)

    # topic
    print("生成 topic.md...", file=sys.stderr)
    tp = f"""你是一位智能照明与建筑智能化领域的社群运营专家。
基于以下已联网核实的多源情报，生成 4-5 条社群话题帖。

情报内容：
{brief}

## 格式
```
## 话题N：钩子标题

正文（不超过150字）

**互动问题**：开放式问题
```

## 要求
- 4-5 条，不同角度
- 第一句是疑问句或反常识陈述
- 语气像圈内人聊天
- 直接输出 markdown"""

    tc = llm_call("你是智能照明行业社群运营专家。", tp, 3072)
    tp2 = p("topic")
    with open(tp2, "w", encoding="utf-8") as f:
        f.write(tc)
    print(f"  -> {tp2}", file=sys.stderr)

    print(f"\n完成！{rp2}\n{tp2}", file=sys.stderr)


# ══════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════
if __name__ == "__main__":
    ensure_dirs()
    ap = argparse.ArgumentParser(description="智能照明资讯 Pipeline v2")
    ap.add_argument("cmd", nargs="?", default="help",
                    choices=["crawl", "export", "generate", "all", "help"])
    args = ap.parse_args()

    if args.cmd == "help":
        print(__doc__)
    elif args.cmd == "crawl":
        cmd_crawl()
    elif args.cmd == "export":
        cmd_export()
    elif args.cmd == "generate":
        cmd_generate()
    elif args.cmd == "all":
        print("=" * 50 + "\nPhase 1: 采集\n" + "=" * 50, file=sys.stderr)
        cmd_crawl()
        print("=" * 50 + "\nPhase 2: 导出\n" + "=" * 50, file=sys.stderr)
        cmd_export()
        print("=" * 50 + "\nPhase 2.5: 手动\n" + "=" * 50, file=sys.stderr)
        print(f"  1. 复制 prompt/prompt_{TODAY}.md 到 DeepSeek 网页端（开联网搜索）", file=sys.stderr)
        print(f"  2. 将输出保存到 brief/brief_{TODAY}.md", file=sys.stderr)
        print(f"  3. 运行: {sys.argv[0]} generate", file=sys.stderr)
