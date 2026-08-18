"""
BOSS 直聘搜索页 — 正式采集器(M2a)

升级自 T6 验证源: 参数化(query/city/pages/max_items) + 关键词池驱动 + 字段扩展
(职位名/公司/薪资/经验/学历/城市) + 频率控制(delay 硬下限 5s + jitter) +
link-hash 跨关键词去重 + 产物落盘(intel/data/boss/<date>/joblist.json, 原子写幂等)。

声明 `render: true` 后, cmd_run/cmd_check 会给本源传 RenderAwareSession:
`sess.get(url)` 返回渲染后 HTML(RenderedResponse); 渲染失败自动降级静态。
本采集器只读 r.text + r.raise_for_status(), 渲染失败/反爬拦截返回已收集部分,
顶层兜底绝不抛异常, 不中断整体巡检。

参数面(design §1.2): 构造 kwarg > 环境变量 BN_BOSS_* > 代码默认。
关键词池(design §1.3): job-search/job_keywords.json 单点真源, 失败降级内置代表词快照。
"""
import glob
import hashlib
import json
import logging
import os
import random
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlencode

from intel.core.base import CST, BaseCollector, NewsItem
from intel.core.registry import register

logger = logging.getLogger("intel.boss_zhipin")

BOSS_BASE = "https://www.zhipin.com"

# ---------- API 直连常量(design §2.3, 主路线: requests + cookie 直连 JSON API) ----------
API_LIST_URL = "https://www.zhipin.com/wapi/zpgeek/search/joblist.json"
API_DETAIL_URL = "https://www.zhipin.com/wapi/zpgeek/job/detail.json"
API_PAGE_SIZE = 30
API_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")
LIST_REFERER = "https://www.zhipin.com/web/geek/job"

# ---------- 默认参数(design §1.2) ----------
DEFAULT_CITY = "101020100"      # 上海
DEFAULT_PAGES = 1
DEFAULT_MAX_ITEMS = 30
DEFAULT_DELAY = 5.0             # 硬下限 5.0(小于则 clamp)
DEFAULT_JITTER = 2.0            # 下限 0

# cookie 默认路径: 用户已导出的登录态(EditThisCookie 格式, chmod 600, repo 外)
DEFAULT_COOKIES_PATH = os.path.expanduser("~/.config/boss/cookies.json")
# sameSite 归一化: EditThisCookie 值 → playwright add_cookies 值(其余省略)
_SAMESITE_MAP = {"no_restriction": "None", "lax": "Lax", "strict": "Strict"}

# 城市 code → 名称映射(仅已知城市, 未知返回 "", 不虚构)
_CITY_NAMES = {"101020100": "上海"}

# 关键词池「代表词」精选快照(design §1.3, 由池 87 词蒸馏; 池缺失/非法时的降级真源)
DEFAULT_REPRESENTATIVE_QUERIES = {
    "ai-app-llm":             ["大模型", "RAG"],
    "iot-embedded":           ["物联网", "嵌入式"],
    "ai-product-manager":     ["AI产品经理"],
    "python-data-collection": ["爬虫"],
    "cross-border-ecommerce": ["跨境电商", "独立站"],
}

# ---------- 路径推导 ----------

def _repo_root() -> str:
    """<repo>/intel/collectors/boss_zhipin.py → <repo>"""
    here = os.path.dirname(os.path.abspath(__file__))   # .../BriefNexus/intel/collectors
    return os.path.dirname(os.path.dirname(here))       # .../BriefNexus


def _default_keywords_path() -> str:
    """单点真源: 上溯到 projects/ 后同级 job-search/job_keywords.json"""
    here = os.path.dirname(os.path.abspath(__file__))
    projects = os.path.dirname(os.path.dirname(os.path.dirname(here)))
    return os.path.join(projects, "job-search", "job_keywords.json")


def _default_output_dir() -> str:
    """产物根目录: <repo>/intel/data/boss"""
    return os.path.join(_repo_root(), "intel", "data", "boss")


def _default_cookies_path() -> str:
    """默认 cookie 路径: ~/.config/boss/cookies.json(用户导出, chmod 600)"""
    return DEFAULT_COOKIES_PATH


# ---------- 卡片解析正则(design §1.4, 多级降级) ----------

# 分块 marker: 先 <li> 形态, 无命中再 <div> 形态(regex 不能平衡标签, 按 marker 切分同层兄弟卡片)
_CARD_SPLIT_LI = re.compile(r'<li[^>]*class="[^"]*job-card-wrapper[^"]*"[^>]*>', re.IGNORECASE)
_CARD_SPLIT_DIV = re.compile(r'<div[^>]*class="[^"]*job-card-wrapper[^"]*"[^>]*>', re.IGNORECASE)

_URL_RE = re.compile(r'href="(/job_detail/[^"]+)"')
_URL_ANY_RE = re.compile(r'href="([^"]*job_detail[^"]*)"')
_JOB_NAME_RE = re.compile(
    r'<span[^>]*class="[^"]*job-name[^"]*"[^>]*>(.*?)</span>', re.DOTALL)
_COMPANY_RE = re.compile(
    r'class="[^"]*company-info[^"]*".*?'
    r'<h3[^>]*class="[^"]*name[^"]*"[^>]*>(.*?)</h3>', re.DOTALL)
_COMPANY_RE2 = re.compile(r'class="[^"]*company-name[^"]*"[^>]*>(.*?)</', re.DOTALL)
_COMPANY_RE3 = re.compile(r'<h3[^>]*class="[^"]*name[^"]*"[^>]*>(.*?)</h3>', re.DOTALL)
_SALARY_RE = re.compile(r'<span[^>]*class="[^"]*salary[^"]*"[^>]*>(.*?)</span>', re.DOTALL)
_AREA_RE = re.compile(r'<span[^>]*class="[^"]*job-area[^"]*"[^>]*>(.*?)</span>', re.DOTALL)
_JOB_INFO_RE = re.compile(r'<ul[^>]*class="[^"]*job-info[^"]*"[^>]*>.*?</ul>', re.DOTALL)
_EXP_RE = re.compile(r'(?:经验[^<]{0,8}|[0-9]+-[0-9]+年|应届|在校)')
_EDU_RE = re.compile(r'(?:本科|硕士|博士|大专|高中|中专|学历不限)')

# 旧版正则(L2 兜底): wrapper → job-card-left → href → job-name
_CARD_RE = re.compile(
    r'<div[^>]*class="[^"]*job-card-wrapper[^"]*"[^>]*>.*?'
    r'<a[^>]*class="[^"]*job-card-left[^"]*"[^>]*href="([^"]+)"[^>]*>.*?'
    r'<span[^>]*class="[^"]*job-name[^"]*"[^>]*>(.*?)</span>',
    re.DOTALL,
)

# 薪资 sanity(design §1.4): 含数字 或 命中 面议/薪 才保留, 乱码/字体反爬置 ""
_SALARY_SANE_RE = re.compile(r'\d|面议|薪')

# 详情页 JD 容器多级正则(design §1.5.2, L0→L2 降级, 不虚构)
_JD_RE_L0 = re.compile(
    r'<div[^>]*class="[^"]*job-sec-text[^"]*"[^>]*>(.*?)</div>', re.DOTALL)
_JD_RE_L1 = re.compile(
    r'<div[^>]*class="[^"]*(?:job-detail|job-detail-section|job-description)[^"]*"[^>]*>'
    r'(.*?)</div>', re.DOTALL)
_JD_RE_L2 = re.compile(
    r'职位描述\s*</?[^>]*>\s*(.*?)(?=<h[1-6]|<section|$)', re.DOTALL)


def _strip(s: str) -> str:
    """去标签 + 折叠空白"""
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _split_cards(text: str) -> list[str]:
    """按 job-card-wrapper marker 切分同层兄弟卡片, 丢弃首段(卡片前的页面头)"""
    if _CARD_SPLIT_LI.search(text):
        parts = _CARD_SPLIT_LI.split(text)
    elif _CARD_SPLIT_DIV.search(text):
        parts = _CARD_SPLIT_DIV.split(text)
    else:
        return []
    return parts[1:]


def _extract(text: str, *regexes) -> str:
    """按顺序尝试各正则, 首个命中取 group(1) 清洗; 全部未命中返回 ''"""
    for rx in regexes:
        m = rx.search(text)
        if m:
            return _strip(m.group(1))
    return ""


def _exp_edu(chunk: str) -> tuple[str, str]:
    """经验/学历: 优先 job-info 块内匹配, 块缺失则全 chunk 匹配"""
    info = _JOB_INFO_RE.search(chunk)
    scope = info.group(0) if info else chunk
    exp = _strip(m.group(0)) if (m := _EXP_RE.search(scope)) else ""
    edu = _strip(m.group(0)) if (m := _EDU_RE.search(scope)) else ""
    return exp, edu


def _salary_sane(s: str) -> bool:
    """薪资防伪: 含数字 或 命中 面议/薪 才保留"""
    return bool(_SALARY_SANE_RE.search(s))


def _extract_jd_full(text: str) -> tuple[str, str]:
    """详情页 JD 全文多级降级提取 → (jd_full, jd_status)

    L0 job-sec-text → L1 job-detail/job-description 容器 → L2 职位描述标题捕获。
    全部未命中/提取为空 → ("", "empty"), 绝不虚构。
    """
    for rx in (_JD_RE_L0, _JD_RE_L1, _JD_RE_L2):
        m = rx.search(text)
        if m:
            jd = _strip(m.group(1))
            if jd:
                return jd, "ok"
    return "", "empty"


def _link_hash(url: str) -> str:
    """归一化 URL → sha256: 去 tracking query 与尾斜杠"""
    path = url.split("?", 1)[0].rstrip("/")
    return hashlib.sha256(path.encode("utf-8")).hexdigest()


def _build_url(term: str, city: str, page: int = 1, api: bool = False) -> str:
    """构造列表页 URL: api=True → joblist.json 直连(主路线); 否则 HTML 模式(M2a/M2b 不变)"""
    if api:
        p = {"query": term, "city": city, "page": page, "pageSize": API_PAGE_SIZE}
        return API_LIST_URL + "?" + urlencode(p)
    params = {"query": term, "city": city}
    if page > 1:
        params["page"] = page
    return BOSS_BASE + "/web/geek/job?" + urlencode(params)


def _build_api_detail_url(encrypt_job_id: str, lid: str, security_id: str) -> str:
    """详情 API URL: jobId/lid/securityId 为会话作用域参数, 落盘前 strip(design §2.5)"""
    return (API_DETAIL_URL + "?" +
            urlencode({"jobId": encrypt_job_id, "lid": lid, "securityId": security_id}))


def _api_headers(referer: str) -> dict:
    """API 直连请求头: Chrome 138 UA + Referer + Accept JSON(design §2.3)"""
    return {"User-Agent": API_UA, "Referer": referer,
            "Accept": "application/json, text/plain, */*"}


def _clean_text(s: str) -> str:
    """仅折叠空白、不去标签: postDescription 是明文, 可能含 < > 等字面字符(design §2.5)"""
    return re.sub(r"\s+", " ", s).strip()


def _area_from_job(j: dict) -> str:
    """area: cityName + areaDistrict + businessDistrict 以 · 拼接, 部分缺失拼现有, 全缺 ''"""
    parts = [j.get("cityName"), j.get("areaDistrict"), j.get("businessDistrict")]
    return "·".join(p for p in parts if isinstance(p, str) and p.strip())


def _parse_joblist_json(payload, term: str, q: dict) -> list[dict] | None:
    """列表 API 载荷 → 记录列表; 失败(非 dict/code≠0/jobList 非 list) → None(降级 HTML)

    code==0 且 jobList 空 → [] (合法空页, 非降级)。缺 encryptJobId/jobName → 跳过该条。
    salary 恒置 ""(字体反爬无明文, 不猜测, design §2.4)。
    """
    if not isinstance(payload, dict) or payload.get("code") != 0:
        return None                        # code≠0 / 非 dict → 视为 API 失败(降级)
    jobs = (payload.get("zpData") or {}).get("jobList")
    if not isinstance(jobs, list):
        return None                        # 结构异常 → 降级
    out = []
    for j in jobs:
        if not isinstance(j, dict):
            continue
        eid = j.get("encryptJobId")
        name = j.get("jobName") or ""
        if not eid or not name:
            continue                       # 缺关键字段跳过(不虚构)
        url = f"https://www.zhipin.com/job_detail/{eid}.html"
        out.append({
            "job_title": name,
            "company": j.get("brandName") or "",
            "industry": j.get("brandIndustry") or "",
            "scale": j.get("brandScaleName") or "",
            "experience": j.get("jobExperience") or "",
            "education": j.get("jobDegree") or "",
            "area": _area_from_job(j),
            "salary": "",                  # API 无明文(字体反爬), 不猜测
            "url": url,
            "link_hash": _link_hash(url),
            "query": term,
            "direction_id": q.get("direction_id", ""),
            "direction_name": q.get("direction_name", ""),
            "encryptJobId": eid,
            "lid": j.get("lid") or "",
            "securityId": j.get("securityId") or "",
        })
    return out                             # code==0 且 jobList 空 → [](合法空页)


def _parse_detail_json(payload) -> tuple[str, str]:
    """详情 API 载荷 → (jd_full, jd_status); 失败→failed, 空/缺失→empty, 绝不虚构"""
    if not isinstance(payload, dict) or payload.get("code") != 0:
        return "", "failed"
    jd = ((payload.get("zpData") or {}).get("jobInfo") or {}).get("postDescription")
    if isinstance(jd, str) and _clean_text(jd):
        return _clean_text(jd), "ok"
    return "", "empty"


def _to_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_bool(value, default: bool = False) -> bool:
    """布尔解析, fail-closed: None → default; 非 1/true/yes 一律 False"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes")


# ---------- 关键词池(design §1.3) ----------

def _load_keyword_pool(path: str) -> list[dict] | None:
    """读 job_keywords.json → 按 order 升序的 directions 列表; 失败返回 None(降级)"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        directions = data.get("directions") if isinstance(data, dict) else None
        if not isinstance(directions, list) or not directions:
            raise ValueError("directions 缺失或为空")
        directions = [d for d in directions if isinstance(d, dict) and d.get("id")]
        if not directions:
            raise ValueError("directions 无合法方向")
        return sorted(directions, key=lambda d: d.get("order", 0))
    except Exception as e:
        logger.warning("读取关键词池失败(%s): %s, 降级内置代表词快照", path, e)
        return None


def _derive_from_direction(d: dict) -> list[str]:
    """池派生: target_roles[0] → 缺失则首个 clusters 子簇首词; 两者皆空返回 []"""
    roles = d.get("target_roles") or []
    if isinstance(roles, list) and roles and isinstance(roles[0], str) and roles[0].strip():
        return [roles[0].strip()]
    clusters = d.get("clusters") or {}
    if isinstance(clusters, dict):
        for words in clusters.values():
            if isinstance(words, list) and words and isinstance(words[0], str) and words[0].strip():
                return [words[0].strip()]
    return []


def _resolve_queries(pool, query: str | None, queries: list[str] | None,
                     curated: dict[str, list[str]]) -> list[dict]:
    """查询生成三级: 显式覆盖 → 池驱动(代表词映射 → 池派生 → 派不出跳过) → 池缺失降级快照

    返回: [{direction_id, direction_name, terms: [...]}, ...]
    """
    explicit = []
    if queries:
        explicit = list(queries)
    elif query:
        explicit = [query]
    if explicit:
        return [{"direction_id": "", "direction_name": "", "terms": explicit}]

    if pool is None:  # 池加载失败 → 内置代表词快照(无 direction 元数据)
        return [{"direction_id": "", "direction_name": "", "terms": list(terms)}
                for terms in curated.values()]

    result = []
    seen_terms = set()
    for d in pool:
        did = d.get("id", "")
        if did in curated:
            terms = list(curated[did])
        else:
            terms = _derive_from_direction(d)
            if not terms:
                continue  # 派不出 → 跳过该方向
        fresh = []
        for t in terms:
            if t not in seen_terms:
                seen_terms.add(t)
                fresh.append(t)
        if fresh:
            result.append({
                "direction_id": did,
                "direction_name": d.get("name", ""),
                "terms": fresh,
            })
    return result


# ---------- 产物落盘(design §1.8) ----------

def _atomic_write_json(path: str, data: dict) -> None:
    """mkstemp 临时文件 + os.replace 原子替换; 失败清理临时文件后重抛"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------- cookie 加载与归一化(design §1.5.1 + §2.2 双格式) ----------

@dataclass
class Cookies:
    """cookie 双格式: requests 用 dict{name:value}, render/playwright 用 list[dict]"""
    requests: dict[str, str]   # {name: value} —— requests.Session.get(cookies=) 用
    playwright: list[dict]     # [{name,value,domain,path,...}] —— render/playwright 用


def _is_render_aware(sess) -> bool:
    """duck-typing: 渲染包装有 _session 属性; 裸 requests.Session / 测试 FakeSession 无"""
    return getattr(sess, "_session", None) is not None


def _raw_session(sess):
    """API 直连: 渲染包装取内层 requests.Session; 裸 Session 原样返回(design §2.2)"""
    return getattr(sess, "_session", None) or sess


def _cookies_for_html(sess, cookies: Cookies | None):
    """HTML 降级/详情路径的 cookie 格式: render 感知→playwright list; 裸 requests→dict; 无→None"""
    if cookies is None:
        return None
    return cookies.playwright if _is_render_aware(sess) else cookies.requests


def _normalize_cookie(c) -> dict | None:
    """单条 EditThisCookie → playwright add_cookies 格式; 非法/非白名单域 → None

    映射: expirationDate(unix 秒) → expires; session/no expirationDate → 不写 expires;
    sameSite no_restriction/lax/strict → None/Lax/Strict(其余省略); domain 去前导点;
    丢弃缺 name/value 或非 zhipin.com 域的条目(fail-closed)。
    """
    if not isinstance(c, dict):
        return None
    name = c.get("name")
    value = c.get("value")
    if not name or value is None:
        return None
    domain = (c.get("domain") or "").lstrip(".")
    if domain and not domain.endswith("zhipin.com"):  # 域白名单
        return None
    out = {"name": name, "value": value}
    if domain:
        out["domain"] = domain
    out["path"] = c.get("path") or "/"
    if c.get("secure"):
        out["secure"] = True
    if c.get("httpOnly"):
        out["httpOnly"] = True
    ss = c.get("sameSite")
    if ss:
        mapped = _SAMESITE_MAP.get(str(ss).lower())
        if mapped:  # 非法值 → 省略该字段, 不抛
            out["sameSite"] = mapped
    exp = c.get("expirationDate")
    if isinstance(exp, (int, float)) and not isinstance(exp, bool) and not c.get("session"):
        out["expires"] = float(exp)
    return out


def _load_cookies(path: str) -> Cookies | None:
    """读 cookie 文件 → 双格式 Cookies; 缺失/坏/空/全丢弃 → warning + None(无 cookie 跑)

    对每条 _normalize_cookie(c) 命中后同时构建 requests dict 与 playwright list;
    _normalize_cookie 不改; 若 requests 为空 → warning + None(M2a 无 cookie 行为不变)。
    日志仅报 path + 原因 + 条数, 绝不输出 cookie 名/值(红线)。
    """
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        logger.warning("BOSS cookie 文件缺失(%s), 无 cookie 采集(可能被安全验证拦截)", path)
        return None
    except Exception as e:
        logger.warning("BOSS cookie 文件读取失败(%s): %s, 无 cookie 采集", path, e)
        return None
    if isinstance(raw, dict):  # 兼容 {"cookies": [...]} 顶层
        raw = raw.get("cookies")
    if not isinstance(raw, list) or not raw:
        logger.warning("BOSS cookie 文件为空或非法(%s), 无 cookie 采集", path)
        return None
    reqs: dict[str, str] = {}
    plist: list[dict] = []
    for c in raw:
        n = _normalize_cookie(c)
        if n:
            reqs[n["name"]] = n["value"]
            plist.append(n)
    if not reqs:
        logger.warning("BOSS cookie 全部被归一化丢弃(%s), 无 cookie 采集", path)
        return None
    logger.info("加载 BOSS cookie %d 条", len(reqs))
    return Cookies(requests=reqs, playwright=plist)


def _merge_dedup(prior: list[dict], new_jobs: list[dict]) -> list[dict]:
    """merge 历史 ∪ 本次新采集, 按 link_hash 去重, prior 保留首次溯源"""
    merged: list[dict] = []
    seen: set[str] = set()
    for rec in list(prior) + list(new_jobs):
        if not isinstance(rec, dict):
            continue
        h = rec.get("link_hash")
        if not h or h in seen:
            continue
        seen.add(h)
        merged.append(rec)
    return merged


@register("boss_zhipin")
class BossZhipinCollector(BaseCollector):
    source_name = "boss_zhipin"
    display_name = "BOSS 直聘"
    domains = ["self_driving"]

    def __init__(self, max_age: int = 7, **ov):
        super().__init__(max_age=max_age)
        # 参数面: 构造 kwarg > 环境变量 BN_BOSS_* > 代码默认(design §1.2)
        self.query = ov.get("query") or os.environ.get("BN_BOSS_QUERY") or None
        raw_queries = ov.get("queries") or os.environ.get("BN_BOSS_QUERIES") or None
        if isinstance(raw_queries, str):
            raw_queries = [q.strip() for q in raw_queries.split(",") if q.strip()]
        self.queries = list(raw_queries) if raw_queries else None
        self.city = ov.get("city") or os.environ.get("BN_BOSS_CITY") or DEFAULT_CITY
        self.pages = _to_int(ov.get("pages") or os.environ.get("BN_BOSS_PAGES"),
                             DEFAULT_PAGES)
        self.max_items = _to_int(ov.get("max_items") or os.environ.get("BN_BOSS_MAX_ITEMS"),
                                 DEFAULT_MAX_ITEMS)
        self.keywords_path = (ov.get("keywords_path")
                              or os.environ.get("BN_BOSS_KEYWORDS_PATH")
                              or _default_keywords_path())
        # 归一化: delay 硬下限 5.0, jitter 下限 0(design §1.6)
        self.delay = max(_to_float(ov.get("delay") or os.environ.get("BN_BOSS_DELAY"),
                                   DEFAULT_DELAY), 5.0)
        self.jitter = max(_to_float(ov.get("jitter") or os.environ.get("BN_BOSS_JITTER"),
                                    DEFAULT_JITTER), 0.0)
        self.output_dir = (ov.get("output_dir")
                           or os.environ.get("BN_BOSS_OUTPUT_DIR")
                           or _default_output_dir())
        # M2b: cookie 路径 / 详情开关 / force 重采(构造 kwarg > 环境变量 > 默认)
        self.cookies_path = (ov.get("cookies_path")
                             or os.environ.get("BN_BOSS_COOKIES_PATH")
                             or _default_cookies_path())
        details = ov.get("details")
        if details is None:
            details = os.environ.get("BN_BOSS_DETAILS")
        self.details = _to_bool(details, True)      # 生产默认开详情采集
        force = ov.get("force")
        if force is None:
            force = os.environ.get("BN_BOSS_FORCE")
        self.force = _to_bool(force, False)         # 默认增量, force 才重采

    # ---------- 频率控制 ----------

    def _sleep_between(self) -> None:
        time.sleep(max(self.delay, 5.0) + random.uniform(0.0, max(self.jitter, 0.0)))

    # ---------- 卡片解析 ----------

    def _parse_card(self, chunk: str, term: str, q: dict) -> dict | None:
        """分块内逐字段解析, L0-L3 多级降级; 解析失败返回 None(跳过该卡片)"""
        url = _extract(chunk, _URL_RE, _URL_ANY_RE)
        title = _extract(chunk, _JOB_NAME_RE)
        if url and not title:  # L2 职位名兜底: 旧版正则复提 job-name
            m = _CARD_RE.search(chunk)
            if m:
                title = _strip(m.group(2))
                if not url:
                    url = m.group(1)
        if not url and not title:  # L3 跳过: url 与职位名均空
            return None
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("/"):
            url = BOSS_BASE + url

        company = _extract(chunk, _COMPANY_RE, _COMPANY_RE2, _COMPANY_RE3)
        salary = _extract(chunk, _SALARY_RE)
        if salary and not _salary_sane(salary):  # E8 薪资乱码/字体反爬 → 置 "", 不猜测
            logger.warning("BOSS 薪资疑似字体反爬乱码(%s), 置空: %s", salary, url)
            salary = ""
        exp, edu = _exp_edu(chunk)
        area = _extract(chunk, _AREA_RE)

        return {
            "job_title": title,
            "company": company,
            "salary": salary,
            "experience": exp,
            "education": edu,
            "area": area,
            "url": url,
            "link_hash": _link_hash(url),
            "query": term,
            "direction_id": q.get("direction_id", ""),
            "direction_name": q.get("direction_name", ""),
        }

    # ---------- 采集 ----------

    def crawl(self, sess) -> list[NewsItem]:
        jobs: list[dict] = []
        seen: set[str] = set()
        stats = {"requests": 0, "cards_seen": 0, "unique_jobs": 0,
                 "blocked_queries": 0, "failed_requests": 0,
                 "new_jobs": 0, "skipped_existing": 0,
                 "detail_fetched": 0, "detail_failed": 0, "detail_empty": 0}
        cookies = _load_cookies(self.cookies_path)   # None → 无 cookie(M2a 行为)
        # 双格式: API 直连用 dict; HTML 降级按会话类型选格式(render 感知→list)
        req_cookies = cookies.requests if cookies else None
        html_cookies = _cookies_for_html(sess, cookies)
        pool = _load_keyword_pool(self.keywords_path)
        queries = _resolve_queries(pool, self.query, self.queries,
                                   DEFAULT_REPRESENTATIVE_QUERIES)
        today = datetime.now(CST).strftime("%Y-%m-%d")
        # 增量去重: force → 忽略历史全量重采; 否则读当日/最近历史
        history_hashes, prior_jobs = (set(), []) if self.force \
            else self._load_history(today)
        try:
            first_request = [True]

            def _gate() -> None:
                """列表+详情所有请求共用的频率门控: 首个请求前不睡"""
                if not first_request[0]:
                    self._sleep_between()
                first_request[0] = False

            for q in queries:
                for term in q["terms"]:
                    for page in range(1, self.pages + 1):
                        if len(jobs) >= self.max_items:  # E10 早停
                            return self._finish(jobs, stats, queries, prior_jobs)
                        _gate()                         # 频率控制(列表请求)
                        recs = self._fetch_list_api(sess, term, page, q, req_cookies)
                        if recs is None:                # API 失败 → 降级 HTML
                            stats["failed_requests"] += 1
                            logger.warning("BOSS API 列表失败, 降级 HTML: term=%s page=%s",
                                           term, page)
                            recs, status = self._fetch_list_html(sess, term, page, q,
                                                                 html_cookies)
                            if status == "blocked":     # 反爬/无卡片 → 放弃该 query
                                stats["blocked_queries"] += 1
                                logger.warning("BOSS 页面无职位卡片(反爬/结构变更), "
                                               "放弃该查询: term=%s page=%s", term, page)
                                break
                            if status == "error":       # 降级请求异常 → 下一 page
                                stats["failed_requests"] += 1
                                continue
                        for rec in recs:
                            stats["cards_seen"] += 1
                            h = rec["link_hash"]
                            if h in seen:                # E9 本次运行跨词去重(保留首溯源)
                                continue
                            if not self.force and h in history_hashes:  # 增量去重
                                stats["skipped_existing"] += 1
                                continue
                            seen.add(h)
                            if self.details:             # 详情(新卡片)
                                _gate()                  # 频率控制(详情请求, 共用门控)
                                if rec.get("encryptJobId"):   # API 卡片 → 详情 API
                                    jd_full, jd_status = self._fetch_jd_api(sess, rec,
                                                                            req_cookies)
                                else:                        # HTML 降级卡片 → HTML 详情
                                    jd_full, jd_status = self._fetch_jd(sess, rec["url"],
                                                                        html_cookies)
                                rec["jd_full"], rec["jd_status"] = jd_full, jd_status
                                if jd_status == "ok":
                                    stats["detail_fetched"] += 1
                                elif jd_status == "failed":
                                    stats["detail_failed"] += 1
                                else:
                                    stats["detail_empty"] += 1
                            else:                        # 详情关闭 → 跳过
                                rec["jd_full"], rec["jd_status"] = "", "skipped"
                            jobs.append(rec)
                        stats["requests"] += 1
                        if not recs:                     # 合法空页 → 停(与 M2b 空页语义一致)
                            break
        except Exception:                                 # E12 顶层兜底: 返回已收集部分
            logger.exception("boss crawl 未预期异常")
        return self._finish(jobs, stats, queries, prior_jobs)

    # ---------- API 直连(design §2.3-2.5, 主路线) ----------

    def _fetch_list_api(self, sess, term: str, page: int, q: dict,
                        req_cookies) -> list[dict] | None:
        """列表 API 直连: 裸 requests 路径, cookie 用 dict; 任何失败 → None(上层降级 HTML)"""
        url = _build_url(term, self.city, page, api=True)
        kwargs = {"headers": _api_headers(LIST_REFERER), "timeout": 30}
        if req_cookies:                       # 无 cookie 不传 cookies kwarg(M2a 语义)
            kwargs["cookies"] = req_cookies
        try:
            r = _raw_session(sess).get(url, **kwargs)
            r.raise_for_status()
            payload = r.json()
        except Exception:                     # 非200/非JSON/网络异常 → 上层降级
            return None
        return _parse_joblist_json(payload, term, q)

    def _fetch_jd_api(self, sess, rec: dict, req_cookies) -> tuple[str, str]:
        """详情 API 直连: detail.json → postDescription; 失败 → ("", "failed"), 不中断"""
        url = _build_api_detail_url(rec["encryptJobId"], rec["lid"], rec["securityId"])
        kwargs = {"headers": _api_headers(rec["url"]), "timeout": 30}  # Referer=详情页
        if req_cookies:
            kwargs["cookies"] = req_cookies
        try:
            r = _raw_session(sess).get(url, **kwargs)
            r.raise_for_status()
            payload = r.json()
        except Exception:
            return "", "failed"
        return _parse_detail_json(payload)

    # ---------- HTML 降级路径(design §2.6, 保留 M2b 能力) ----------

    def _fetch_list_html(self, sess, term: str, page: int, q: dict,
                         html_cookies) -> tuple[list[dict], str]:
        """HTML 列表降级: 卡片解析失败 → ("ok", recs)/("blocked")/("error")"""
        url = _build_url(term, self.city, page)          # HTML 模式
        try:
            r = sess.get(url, timeout=30,
                         **({"cookies": html_cookies} if html_cookies else {}))
            r.raise_for_status()
        except Exception:
            return [], "error"                            # 上层 failed_requests++ + continue
        text = r.text or ""
        if "job-card-wrapper" not in text:
            return [], "blocked"                          # 上层 blocked_queries++ + break
        recs = []
        for chunk in _split_cards(text):
            rec = self._parse_card(chunk, term, q)        # 原 M2a/M2b HTML 解析器, 不改
            if rec:
                recs.append(rec)
        return recs, "ok"

    # ---------- 详情页 JD 采集(design §1.5.2) ----------

    def _fetch_jd(self, sess, url: str, cookies) -> tuple[str, str]:
        """单详情页渲染 → (jd_full, jd_status)。任何失败 → ("", "failed"), 不中断整体"""
        try:
            r = sess.get(url, timeout=30,
                         **({"cookies": cookies} if cookies else {}))
            r.raise_for_status()
        except Exception:
            return "", "failed"
        return _extract_jd_full(r.text or "")

    # ---------- 增量历史(design §1.5.3) ----------

    def _load_history(self, today: str) -> tuple[set[str], list[dict]]:
        """读当日 joblist.json(缺则取日期最大者) → (link_hash 集合, prior 记录)

        缺失/坏 JSON/结构异常 → warning + 空历史(全量当新)。
        """
        path = os.path.join(self.output_dir, today, "joblist.json")
        if not os.path.exists(path):
            candidates = sorted(glob.glob(
                os.path.join(self.output_dir, "*", "joblist.json")))
            if not candidates:
                return set(), []
            path = candidates[-1]   # YYYY-MM-DD 字典序最大 = 日期最新
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            jobs = data.get("jobs") if isinstance(data, dict) else None
            if not isinstance(jobs, list):
                raise TypeError("jobs 缺失或非法")
            hashes: set[str] = set()
            prior: list[dict] = []
            for j in jobs:
                if not isinstance(j, dict) or not j.get("link_hash"):
                    continue       # 结构异常条目跳过(E14: 全无 → 视为空历史)
                h = j["link_hash"]
                if h in hashes:
                    continue
                hashes.add(h)
                prior.append(j)
            return hashes, prior
        except Exception as e:
            logger.warning("读取历史 joblist.json 失败(%s): %s, 全量当新", path, e)
            return set(), []

    # ---------- 收尾: 产物 + NewsItem ----------

    def _finish(self, jobs: list[dict], stats: dict, queries: list[dict],
                prior_jobs: list[dict]) -> list[NewsItem]:
        # merge 历史 ∪ 本次新采集(link_hash 去重, prior 保留首次溯源)
        final_jobs = _merge_dedup(prior_jobs, jobs)
        for rec in final_jobs:  # strip 详情临时字段(仅详情请求用, 不入产物) + 向后兼容补齐
            rec.pop("encryptJobId", None)
            rec.pop("lid", None)
            rec.pop("securityId", None)
            rec.setdefault("jd_full", "")
            rec.setdefault("jd_status", "skipped")
            rec.setdefault("industry", "")  # API 新字段, 历史记录补齐
            rec.setdefault("scale", "")
        stats["unique_jobs"] = len(final_jobs)
        stats["new_jobs"] = len(jobs)
        self._write_joblist(final_jobs, stats, queries)
        return [self._to_newsitem(j) for j in final_jobs]

    def _to_newsitem(self, rec: dict) -> NewsItem:
        # .get 兜底: 历史 merge 进来的 prior 记录可能字段不全, 不崩
        item = NewsItem(
            title=rec.get("job_title") or rec.get("url") or "",  # title 兜底用真实 url
            url=rec.get("url", ""),
            summary=" · ".join(filter(None, [rec.get("company", ""),
                                             rec.get("salary", ""),
                                             rec.get("experience", "")])),
            source=self.display_name,
            domain="招聘",
        )
        item.raw_data = {"job": rec}  # 扩展字段挂 raw_data(cmd_run/cmd_check 管道可见)
        return item

    def _write_joblist(self, jobs: list[dict], stats: dict, queries: list[dict]) -> None:
        today = datetime.now(CST).strftime("%Y-%m-%d")
        path = os.path.join(self.output_dir, today, "joblist.json")
        data = {
            "schema_version": "1.0",
            "source": "boss_zhipin",
            "collected_at": datetime.now(CST).isoformat(timespec="seconds"),
            "city": self.city,
            "city_name": _CITY_NAMES.get(self.city, ""),
            "params": {"pages": self.pages, "max_items": self.max_items,
                       "delay": self.delay, "jitter": self.jitter,
                       "details": self.details, "force": self.force},
            "queries": queries,
            "stats": stats,
            "jobs": jobs,
        }
        try:  # E11 写盘失败 warning, 不致命
            _atomic_write_json(path, data)
        except Exception as e:
            logger.warning("BOSS 产物落盘失败 %s: %s", path, e)
