#!/usr/bin/env python3
"""
智能照明与智能建筑资讯爬虫 — 三主源(arXiv/CSA/住建委) + LLM 板块分类
输出 news.md 按板块分组，每篇带板块标签
"""

import argparse, configparser, hashlib, json, os, random, re, sys, time, feedparser, arxiv
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional
import requests
from bs4 import BeautifulSoup

# ── 全局配置 ──────────────────────────────────────────────────
CST = timezone(timedelta(hours=8))
TODAY = datetime.now(CST).strftime("%Y-%m-%d")
TODAY_DT = datetime.now(CST)
DEFAULT_OUTPUT = r"D:\NOTES\zzz\BriefNexus\news\news"
REQUEST_TIMEOUT = 15
REQUEST_DELAY = (0.8, 2.0)
UA = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/127.0.0.0 Edg/127.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
]

# 加载 API 配置
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crawler_config.ini")
_api_cfg = configparser.ConfigParser()
with open(CONFIG_FILE, "r", encoding="utf-8") as _f:
    _api_cfg.read_file(_f)
LLM_BASE = _api_cfg.get("api", "base_url", fallback="https://api.deepseek.com")
LLM_KEY  = _api_cfg.get("api", "api_key", fallback="")
LLM_MODEL = _api_cfg.get("api", "model", fallback="deepseek-v4-flash")

# ── 板块体系 ──────────────────────────────────────────────────
SECTORS = [
    ("行业大势", "expo", "\U0001f310"),
    ("技术突破", "tech", "\U0001f52c"),
    ("资本脉搏", "finance", "\U0001f4c8"),
    ("供应链深水", "supply", "\u26d3"),
    ("企业交锋", "corp", "\U0001f3f7"),
    ("场景新战场", "scene", "\U0001f3af"),
    ("招标市场", "bid", "\U0001f4dd"),
]
SECTOR_KEYS_SET = {s[1] for s in SECTORS}

# ── 数据结构 ──────────────────────────────────────────────────
@dataclass
class NewsItem:
    title: str
    url: str
    summary: str = ""
    date_obj: Optional[datetime] = None
    source: str = ""
    domain: str = "综合"
    sector: str = ""

    @property
    def dedup_key(self) -> str:
        return hashlib.md5(self.title.encode()).hexdigest()

    @property
    def date(self) -> str:
        return self.date_obj.strftime("%Y-%m-%d") if self.date_obj else ""

# ── 工具函数 ──────────────────────────────────────────────────
def _sess() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = random.choice(UA)
    s.headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    return s

def _get(url: str, sess: requests.Session) -> Optional[str]:
    try:
        r = sess.get(url, timeout=REQUEST_TIMEOUT)
        r.encoding = r.apparent_encoding or "utf-8"
        return r.text
    except Exception as e:
        print(f"  [WARN] {url[:60]} - {e}", file=sys.stderr)
        return None

def _delay():
    time.sleep(random.uniform(*REQUEST_DELAY))

def _date_from_url(url: str) -> Optional[datetime]:
    for pat in [
        r"/(\d{4})(\d{2})(\d{2})/",
        r"/(\d{4})-(\d{2})-(\d{2})/",
        r"(\d{4})(\d{2})(\d{2})[_-]",
        r"/(\d{4})-(\d{2})/",
    ]:
        m = re.search(pat, url)
        if m:
            try:
                g = m.groups()
                if len(g) == 3:
                    y, mo, d = int(g[0]), int(g[1]), int(g[2])
                    if 2000 <= y <= 2099 and 1 <= mo <= 12 and 1 <= d <= 31:
                        return datetime(y, mo, d, tzinfo=CST)
                elif len(g) == 2:
                    y, mo = int(g[0]), int(g[1])
                    if 2000 <= y <= 2099 and 1 <= mo <= 12:
                        return datetime(y, mo, 1, tzinfo=CST)
            except:
                pass
    return None

def _infer_date(it: NewsItem):
    if not it.date_obj:
        d = _date_from_url(it.url)
        if d:
            it.date_obj = d

def _time_label(dt: datetime) -> str:
    d = (TODAY_DT - dt).days
    if d < 1:    return "\U0001f7e2 本日"
    elif d <= 3: return "\U0001f7e1 近3日"
    elif d <= 7: return "\U0001f7e0 本周"
    elif d <= 31:return "\U0001f534 本月"
    else:        return "\u26aa 更早"

# ── LLM 分类 + 规则兜底 ──────────────────────────────────────
_CLASSIFY_SYSTEM_PROMPT = f"""你将收到一批智能照明与智能建筑行业的新闻标题。
请将每条标题分类到以下板块之一（只返回板块 key，不返回其他文字）：

- {SECTORS[0][2]} {SECTORS[0][0]}（{SECTORS[0][1]}）
- {SECTORS[1][2]} {SECTORS[1][0]}（{SECTORS[1][1]}）
- {SECTORS[2][2]} {SECTORS[2][0]}（{SECTORS[2][1]}）
- {SECTORS[3][2]} {SECTORS[3][0]}（{SECTORS[3][1]}）
- {SECTORS[4][2]} {SECTORS[4][0]}（{SECTORS[4][1]}）
- {SECTORS[5][2]} {SECTORS[5][0]}（{SECTORS[5][1]}）
- {SECTORS[6][2]} {SECTORS[6][0]}（{SECTORS[6][1]}）

返回 JSON 数组，不要多余文字。示例：
["expo","tech","finance"]"""


def _classify_rules(title: str) -> str:
    t = title
    if re.search(r"(标讯|招标|投标|控制价)", t):
        return "bid"
    if re.search(r"(MicroLED|Micro LED|OLED|MiniLED|全彩|芯片|光学|专利|封装|半导体)", t, re.IGNORECASE):
        return "tech"
    if re.search(r"(上市|IPO|融资|募资|暴涨|涨停|北交所)", t):
        return "finance"
    if re.search(r"(展览|展会|光亚展|趋势|战略|报告|市场)", t):
        return "expo"
    if re.search(r"(产线|量产|供货|供应|面板|项目封顶|扩建|产能|制造|签约)", t):
        return "supply"
    if re.search(r"(植物|车载|选购|抗菌|护眼|酒店|场景)", t):
        return "scene"
    if re.search(r"(品牌|排名|冠名|仲裁)", t):
        return "corp"
    return "expo"


def _classify_llm_batch(titles):
    if not LLM_KEY:
        return []
    try:
        payload = {
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": _CLASSIFY_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(titles, ensure_ascii=False)},
            ],
            "temperature": 0.1,
            "max_tokens": 512,
        }
        r = requests.post(
            f"{LLM_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {LLM_KEY}", "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        if r.status_code != 200:
            print(f"  [WARN] LLM API {r.status_code}: {r.text[:80]}", file=sys.stderr)
            return []
        content = r.json()["choices"][0]["message"]["content"].strip()
        if content.startswith("["):
            sectors = json.loads(content)
        else:
            m = re.search(r"```(?:json)?\s*([\s\S]*?)```", content)
            sectors = json.loads(m.group(1)) if m else []
        if len(sectors) != len(titles):
            return []
        for s in sectors:
            if s not in SECTOR_KEYS_SET:
                return []
        return sectors
    except Exception as e:
        print(f"  [WARN] LLM 分类出错: {e}", file=sys.stderr)
        return []


def classify_batch(items):
    titles = [it.title for it in items]
    batch_size = 30
    for start in range(0, len(items), batch_size):
        batch = items[start:start + batch_size]
        batch_titles = [it.title for it in batch]
        llm_result = _classify_llm_batch(batch_titles)
        if llm_result:
            for it, sk in zip(batch, llm_result):
                it.sector = sk
        else:
            for it in batch:
                it.sector = _classify_rules(it.title)

# ── 数据源 ────────────────────────────────────────────────────

def crawl_arxiv(sess):
    """arXiv — 学术论文，先尝试 API，失败则网页抓取"""
    items = []
    print("[arXiv 学术] 正在抓取...", file=sys.stderr)

    # 方式1：尝试 API（单次查询）
    query = ("all:microLED+OR+all:visible+light+communication+OR+"
             "all:LiFi+OR+(all:LED+AND+all:lighting)+OR+"
             "(all:optical+AND+all:interconnect+AND+all:silicon)")
    try:
        r = sess.get(
            "http://export.arxiv.org/api/query",
            params={"search_query": query, "sortBy": "submittedDate",
                    "sortOrder": "descending", "max_results": 15},
            timeout=30,
        )
        if r.status_code == 200:
            feed = feedparser.parse(r.text)
            for entry in feed.entries:
                title = entry.title.replace("\n", " ").strip()
                if not title or len(title) < 10:
                    continue
                link = entry.link if hasattr(entry, "link") else ""
                pub_date = None
                if hasattr(entry, "published"):
                    try:
                        pub_date = datetime.strptime(entry.published[:10], "%Y-%m-%d")
                        pub_date = pub_date.replace(tzinfo=CST)
                    except:
                        pass
                summary = ""
                if hasattr(entry, "summary"):
                    summary = re.sub(r"<[^>]+>", "", entry.summary)
                    summary = summary.replace("\n", " ").strip()
                authors = []
                if hasattr(entry, "authors"):
                    authors = [a.name for a in entry.authors[:3]]
                author_str = ", ".join(authors) if authors else ""
                summary_text = f"作者: {author_str}\n{summary}" if author_str else summary
                it = NewsItem(title=title, url=link, summary=summary_text,
                              date_obj=pub_date, source="arXiv", domain="学术论文")
                items.append(it)
            if items:
                print(f"  -> {len(items)} 条 (API)", file=sys.stderr)
                return items
    except Exception as e:
        print(f"  [WARN] arXiv API 不可用: {e}", file=sys.stderr)

    # 方式2：网页抓取 arXiv 最新光学论文列表
    print("  -> API 不可用，尝试网页抓取 arXiv...", file=sys.stderr)
    try:
        html = _get("https://arxiv.org/list/physics.optics/recent", sess)
        if not html:
            return items
        soup = BeautifulSoup(html, "lxml")
        current_date = None
        paper_links = []  # 先收集论文链接，再分批取摘要
        for tag in soup.find_all(["h3", "dt"]):
            if tag.name == "h3":
                m = re.search(r"(\w+,\s+\d+\s+\w+\s+\d{4})", tag.get_text())
                if m:
                    try:
                        current_date = datetime.strptime(m.group(1), "%a, %d %b %Y")
                        current_date = current_date.replace(tzinfo=CST)
                    except:
                        current_date = None
                continue
            a = tag.find("a", title=True)
            if not a:
                continue
            link = "https://arxiv.org" + a.get("href", "")
            dd = tag.find_next_sibling("dd")
            title = ""
            if dd:
                title_el = dd.select_one(".list-title")
                if title_el:
                    title = title_el.get_text(strip=True).replace("Title:", "").strip()
            if not title or len(title) < 10:
                continue
            paper_links.append((title, link, current_date))
            if len(paper_links) >= 15:
                break

        # 分批抓取每篇论文的摘要页
        print(f"  -> 正在抓取 {len(paper_links)} 篇论文的摘要...", file=sys.stderr)
        for title, link, date_obj in paper_links:
            try:
                abs_html = _get(link, sess)
                summary = ""
                if abs_html:
                    abs_soup = BeautifulSoup(abs_html, "lxml")
                    abs_el = abs_soup.select_one("blockquote.abstract")
                    if abs_el:
                        summary = abs_el.get_text(strip=True).replace("Abstract:", "").strip()
                it = NewsItem(title=title, url=link, summary=summary,
                              date_obj=date_obj, source="arXiv", domain="学术论文")
                items.append(it)
                time.sleep(0.5)
            except Exception as e:
                print(f"    [WARN] 摘要抓取失败: {title[:30]} - {e}", file=sys.stderr)
                # 没有摘要也保留条目
                it = NewsItem(title=title, url=link, summary="",
                              date_obj=date_obj, source="arXiv", domain="学术论文")
                items.append(it)
    except Exception as e:
        print(f"  [WARN] arXiv 网页抓取也失败: {e}", file=sys.stderr)

    print(f"  -> {len(items)} 条 (网页)", file=sys.stderr)
    return items


def crawl_csa(sess):
    """CSA 联盟（半导体照明网）— 政策产业动态"""
    items = []
    print("[CSA 联盟] 正在抓取...", file=sys.stderr)
    html = _get("https://www.china-led.net/news/", sess)
    if not html:
        return items
    soup = BeautifulSoup(html, "lxml")
    seen_url = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        title = a.get_text(strip=True)
        if not title or len(title) < 10 or not href or href in seen_url:
            continue
        if "/news/" not in href and "/special/" not in href:
            continue
        seen_url.add(href)
        if not href.startswith("http"):
            href = "https://www.china-led.net" + href
        date_obj = None
        parent = a.parent
        for sibling in parent.find_all(["span", "em", "small", "time"]):
            txt = sibling.get_text(strip=True)
            m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", txt)
            if m:
                try:
                    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                    date_obj = datetime(y, mo, d, tzinfo=CST)
                except:
                    pass
                break
        if not date_obj:
            d = _date_from_url(href)
            if d:
                date_obj = d
        it = NewsItem(
            title=title,
            url=href,
            date_obj=date_obj,
            source="CSA联盟",
            domain="政策产业",
        )
        items.append(it)
    print(f"  -> {len(items)} 条", file=sys.stderr)
    return items


def crawl_shanghai(sess):
    """上海住建委 — 政府公告"""
    items = []
    print("[上海住建委] 正在抓取...", file=sys.stderr)
    seen_url = set()
    targets = [
        ("https://zjw.sh.gov.cn", "首页"),
        ("https://zjw.sh.gov.cn/zwgk/", "政务公开"),
    ]
    for url, label in targets:
        html = _get(url, sess)
        if not html:
            continue
        soup = BeautifulSoup(html, "lxml")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            title = a.get_text(strip=True)
            if not title or len(title) < 10 or href in seen_url:
                continue
            if not href.startswith("http"):
                if href.startswith("/"):
                    href = "https://zjw.sh.gov.cn" + href
                else:
                    continue
            if "zjw.sh.gov.cn" not in href:
                continue
            seen_url.add(href)
            date_obj = _date_from_url(href)
            it = NewsItem(
                title=title,
                url=href,
                date_obj=date_obj,
                source="上海住建委",
                domain="政府公告",
            )
            items.append(it)
        _delay()
    print(f"  -> {len(items)} 条", file=sys.stderr)
    return items


# ── 主流程 ────────────────────────────────────────────────────
SOURCES = [
    ("arXiv 学术论文",  crawl_arxiv),
    ("CSA 联盟",        crawl_csa),
    ("上海住建委",      crawl_shanghai),
]

def build_markdown(items):
    grouped = {sk: [] for sk in SECTOR_KEYS_SET}
    for it in items:
        sk = it.sector if it.sector in grouped else "expo"
        grouped[sk].append(it)
    for sk in grouped:
        grouped[sk].sort(key=lambda x: x.date_obj or TODAY_DT, reverse=True)

    sector_map = {s[1]: (s[0], s[2]) for s in SECTORS}

    lines = [
        "# 智能照明与智能建筑资讯简报",
        f"> 检索时间：{TODAY} | 共 {len(items)} 条 / {len(set(it.source for it in items))} 个源",
        "",
    ]

    seq = 0
    for sk in [s[1] for s in SECTORS]:
        sector_items = grouped[sk]
        if not sector_items:
            continue
        sname, semoji = sector_map[sk]
        lines.append(f"---\n## {semoji} {sname}（{len(sector_items)}条）\n")
        for it in sector_items:
            seq += 1
            tl = _time_label(it.date_obj) if it.date_obj else ""
            tag = f" `[{it.domain}]`" if it.domain else ""
            lines.append(f"### {seq}. {it.title}{tag}")
            lines.append(f"- **摘要**：{it.summary or '—'}")
            lines.append(f"- **来源**：{it.url}")
            lines.append(f"- **日期**：{it.date} / {tl}")
            lines.append(f"- **源站**：{it.source}")
            lines.append("")

    lines.append("---\n")
    lines.append("## \u2705 请勾选你要保留的条目（用于 Phase 3 报告/话题生成）\n")
    seq2 = 0
    for sk in [s[1] for s in SECTORS]:
        sector_items = grouped[sk]
        if not sector_items:
            continue
        sname, semoji = sector_map[sk]
        lines.append(f"**{semoji} {sname}**\n")
        for it in sector_items:
            seq2 += 1
            lines.append(f"- [ ] {seq2}. {it.title}")
        lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="智能照明/建筑爬虫 — arXiv/CSA/住建委")
    ap.add_argument("--output", default=DEFAULT_OUTPUT)
    ap.add_argument("--max-age", type=int, default=7)
    args = ap.parse_args()

    sess = _sess()
    all_items = []
    for name, func in SOURCES:
        try:
            items = func(sess)
            all_items.extend(items)
        except Exception as e:
            print(f"  X {name}: {e}", file=sys.stderr)
    sess.close()

    # 去重
    seen = set()
    deduped = []
    for it in all_items:
        k = it.dedup_key
        if k not in seen:
            seen.add(k)
            deduped.append(it)
    all_items = deduped

    # LLM 批量分类
    print("  \u2139 正在 LLM 分类...", file=sys.stderr)
    classify_batch(all_items)
    sector_counts = {}
    for it in all_items:
        sector_counts[it.sector] = sector_counts.get(it.sector, 0) + 1
    for sk, cn, emo in SECTORS:
        c = sector_counts.get(sk, 0)
        if c:
            print(f"    {emo} {cn}: {c}条", file=sys.stderr)

    # 时效过滤
    if args.max_age > 0:
        cutoff = TODAY_DT - timedelta(days=args.max_age)
        before = len(all_items)
        all_items = [it for it in all_items if it.date_obj and it.date_obj >= cutoff]
        if before != len(all_items):
            print(f"  \u2139 时效过滤: {before} -> {len(all_items)} 条 ({args.max_age}天内)", file=sys.stderr)

    # 输出
    outdir = args.output
    os.makedirs(outdir, exist_ok=True)
    outfile = os.path.join(outdir, f"news_{TODAY}.md")
    with open(outfile, "w", encoding="utf-8") as f:
        f.write(build_markdown(all_items))
    print(f"\u2705 {outfile} ({len(all_items)}条)", file=sys.stderr)

if __name__ == "__main__":
    main()
