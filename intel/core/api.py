"""
通用 JSON API 采集器 — APICollector（能力先行，加源 = 声明配置）

新数据源只需继承 APICollector 并声明 API_SPEC（ClassVar dict），
无需手写 crawl()/sleep/去重/过滤/limit —— 请求构建、auth 注入、
分页状态机、JSON 路径解析、字段映射、容错、去重、recency 过滤、
limit、限速全部由本基类承担（design apicollector-base §1）。

非 JSON 源（如 arxiv Atom XML）仅覆盖 `_extract_records` 一个 hook，
将响应归一化为 list[dict] 后走同一套字段映射管线。

容错契约（与现有采集器一致）：crawl 永不抛异常。任何单点失败
（超时/HTTP/JSON 解析/结构异常/单条映射异常）→ warning + 跳过或
停止该请求；首请求失败返回 []，后续页失败返回已收集部分。
"""
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, ClassVar

import requests

from intel.core.base import CST, BaseCollector, NewsItem

logger = logging.getLogger("intel.api")

DEFAULT_LIMIT = 40
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_PAGES = 50


# ---------- 内置 transform（仅 stdlib，7 个） ----------

def _collapse_ws(value: Any) -> str:
    """折叠连续空白（首尾 strip + 内部空白归一为单空格）"""
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).strip())


def _strip_html(value: Any) -> str:
    """去 HTML 标签"""
    if value is None:
        return ""
    return re.sub(r"<[^>]+>", "", str(value))


def _truncate(value: Any, max: int = 200) -> str:
    """截断到 max 字符"""
    if value is None:
        return ""
    s = str(value)
    return s[:max]


def _parse_date(value: Any, fmts=None, tz: str = "utc") -> datetime | None:
    """日期字符串 → aware datetime

    fmts: str 或 list[str]；对完整字符串逐格式尝试。ISO 时间戳
    （含 'T' 分隔，如 arxiv 的 2026-08-15T18:00:00Z）回退到仅日期部分
    再解析（等价于原 arxiv 实现 date_str[:10] + strptime("%Y-%m-%d")）。
    tz: "utc" 或 "cst"，给 naive datetime 补时区。
    """
    if value is None or not isinstance(value, str):
        return None
    if isinstance(fmts, str):
        fmts = [fmts]
    fmts = list(fmts or ["%Y-%m-%d"])
    s = value.strip()
    candidates = [s]
    if "T" in s:
        candidates.append(s.split("T")[0])
    dt = None
    for fmt in fmts:
        for cand in candidates:
            try:
                dt = datetime.strptime(cand, fmt)  # noqa: DTZ007 — 随后统一补时区
                break
            except ValueError:
                continue
        if dt is not None:
            break
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc if tz != "cst" else CST)
    return dt


def _ms_to_cst(value: Any) -> datetime | None:
    """毫秒时间戳 → datetime(CST)"""
    if value is None:
        return None
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ms / 1000, tz=CST)


def _join_names(value: Any, sep: str = ", ", limit: int | None = None,
                suffix: str = "") -> str:
    """list join；超过 limit 个只取前 limit 个并追加 suffix（et al.）"""
    if not isinstance(value, (list, tuple)):
        return ""
    names = [str(v) for v in value if v is not None]
    if limit is not None and len(names) > limit:
        return sep.join(names[:limit]) + suffix
    return sep.join(names)


# name 匹配 template 占位符：中间变量（_xxx）或原始记录字段路径（a.b）
_TEMPLATE_TOKEN = re.compile(r"\{([\w.]+)\}")


class APICollector(BaseCollector):
    """声明式 JSON API 采集器基类

    子类声明 API_SPEC（见 docstring / design §1.3），构造参数作为运行时
    覆盖（同名字段覆盖 API_SPEC；api_key/user/password 等 auth 注入值
    也经 overrides 传入）。实现 `crawl(sess)` 后不再是抽象类。
    """

    API_SPEC: ClassVar[dict] = {}
    PARAM_SCHEMA: ClassVar[dict] = {"max_age": {"type": "int", "min": 1, "max": 90}}

    _TRANSFORMS: ClassVar[dict] = {
        "collapse_ws": _collapse_ws,
        "strip_html": _strip_html,
        "truncate": _truncate,
        "parse_date": _parse_date,
        "ms_to_cst": _ms_to_cst,
        "join_names": _join_names,
        "template": None,  # 特殊处理：需要 mapped 上下文
    }

    def __init__(self, max_age: int = 7, **overrides):
        super().__init__(max_age=max_age)
        self._overrides = dict(overrides)

    # ---------- 对外契约 ----------

    def crawl(self, sess) -> list[NewsItem]:
        """采集该源最新情报。永不抛异常：失败归一为 [] 或已收集部分。"""
        try:
            spec = self._merged_spec()
        except Exception as e:  # 兜底：spec 合并失败也绝不外抛
            logger.warning("%s spec 合并失败: %s", self.source_name, e)
            return []
        items: list[NewsItem] = []
        seen: set[str] = set()
        for req in self._build_requests(spec):
            page = _PageState(spec)
            while True:
                try:
                    payload = self._merge_params(spec, req, page.params())
                    resp = self._do_request(sess, spec, payload)
                    records = self._extract_records(resp, spec)
                except (requests.RequestException, ValueError) as e:
                    # 覆盖 design §1.4 的 Timeout/HTTPError/ValueError/JSONDecodeError，
                    # 并额外兜住 ConnectionError 等其他请求异常（容错契约：绝不外抛）
                    logger.warning("%s 请求失败: %s", spec.get("endpoint"), e)
                    break
                for rec in records:
                    try:
                        item = self._map_record(rec, spec)
                    except Exception as e:
                        logger.warning("%s 单条映射失败，跳过: %s", spec.get("endpoint"), e)
                        continue
                    if item is None:
                        continue  # title/url 空
                    if not self._is_recent(item.date_obj):
                        continue  # 过旧
                    if item.url and item.url in seen:
                        continue  # 去重
                    if item.url:
                        seen.add(item.url)
                    if spec.get("raw_data", True):
                        item.raw_data = rec
                    items.append(item)
                    if len(items) >= spec.get("limit", DEFAULT_LIMIT):
                        return items
                page.collected += len(records)
                if not self._next_page(resp, records, spec, page):
                    break
            if spec.get("delay"):
                time.sleep(float(spec["delay"]))
        return items

    # ---------- spec 合并与请求构建 ----------

    def _merged_spec(self) -> dict:
        """类 API_SPEC ⊕ 构造 overrides（后者优先）"""
        spec = dict(self.API_SPEC or {})
        spec.update(self._overrides)
        return spec

    def _build_requests(self, spec):
        """生成请求参数集迭代器：queries 逐项，否则单一空覆盖"""
        queries = spec.get("queries")
        if queries:
            yield from queries
        else:
            yield {}

    def _merge_params(self, spec, req, page_params) -> dict:
        payload = dict(spec.get("params") or {})
        payload.update(req or {})
        payload.update(page_params or {})
        return payload

    def _resolve_endpoint(self, spec) -> str:
        """endpoint 的 {param} 占位符从 overrides 注入（可选）"""
        endpoint = spec["endpoint"]
        if "{" in endpoint and self._overrides:
            try:
                return endpoint.format(**self._overrides)
            except (KeyError, ValueError, IndexError):
                return endpoint
        return endpoint

    def _do_request(self, sess, spec, payload):
        """GET/POST + auth 注入。api_key 缺失时抛 ValueError（fail-closed）。"""
        headers = dict(spec.get("headers") or {})
        req_auth = None
        auth = spec.get("auth")
        if isinstance(auth, dict):
            if auth.get("type") == "api_key":
                key = self._overrides.get("api_key") or os.environ.get(auth.get("env") or "")
                if not key:
                    raise ValueError(f"auth env {auth.get('env')} 未配置")
                headers[auth["header"]] = key
            elif auth.get("type") == "basic":
                user = self._overrides.get(auth.get("user") or auth.get("env_user") or "")
                password = self._overrides.get(
                    auth.get("password") or auth.get("env_pass") or ""
                )
                req_auth = (user, password)
        endpoint = self._resolve_endpoint(spec)
        timeout = spec.get("timeout", DEFAULT_TIMEOUT)
        if spec.get("method") == "POST":
            kwargs = {"json": payload} if spec.get("body") == "json" else {"data": payload}
            return sess.post(endpoint, headers=headers, auth=req_auth,
                             timeout=timeout, **kwargs)
        return sess.get(endpoint, params=payload, headers=headers,
                        auth=req_auth, timeout=timeout)

    # ---------- 响应解析 ----------

    def _extract_records(self, resp, spec) -> list:
        """默认：r.json() + items_path 取记录列表（E4/E5 归一为 []）"""
        data = resp.json()
        items_path = spec.get("items_path")
        records = self._get_path(data, items_path) if items_path else data
        if records is None:
            logger.warning("%s items_path '%s' 缺失，视为 0 条",
                           spec.get("endpoint"), items_path)
            return []
        if not isinstance(records, list):
            logger.warning("%s items 结构非 list（%s），视为 0 条",
                           spec.get("endpoint"), type(records).__name__)
            return []
        return records

    # ---------- 字段映射 ----------

    def _map_record(self, rec, spec) -> NewsItem | None:
        """field_map → NewsItem。中间变量（_ 前缀）进 mapped 供 template 引用。"""
        field_map = spec.get("field_map") or {}
        mapped: dict = {}
        for key, mapping in field_map.items():
            if isinstance(mapping, str):
                value = self._get_path(rec, mapping)
            else:
                value = self._resolve_mapping(rec, mapping, mapped)
            mapped[key] = value
        title = mapped.get("title")
        url = mapped.get("url")
        if not title or not url:
            return None  # E6: 缺 title/url → 跳过该记录
        domain = mapped.get("domain") or spec.get("domain") or ""
        sector = mapped.get("sector") or spec.get("sector") or ""
        return NewsItem(
            title=str(title),
            url=str(url),
            summary=str(mapped.get("summary") or ""),
            date_obj=mapped.get("date_obj"),
            source=self.display_name,
            domain=str(domain),
            sector=str(sector),
        )

    def _resolve_mapping(self, rec, mapping, mapped):
        """单条映射解析：path 取值 + transform + default 兜底"""
        transform = mapping.get("transform")
        if transform == "template":
            return self._apply_template(rec, mapping, mapped)
        value = self._get_path(rec, mapping["path"]) if mapping.get("path") else None
        if transform:
            fn = self._TRANSFORMS.get(transform)
            if fn is None:
                logger.warning("未知 transform: %s", transform)
                return None
            kwargs = {k: v for k, v in mapping.items()
                      if k not in ("path", "transform", "default")}
            value = fn(value, **kwargs)
        if value is None and "default" in mapping:
            return mapping["default"]
        return value

    def _apply_template(self, rec, mapping, mapped) -> str:
        """{name} 依次解析：中间变量（mapped）→ 原始记录字段路径"""
        template = mapping.get("template") or ""
        def repl(match: re.Match) -> str:
            name = match.group(1)
            v = mapped.get(name) if name in mapped else self._get_path(rec, name)
            return "" if v is None else str(v)
        return _TEMPLATE_TOKEN.sub(repl, template)

    # ---------- 分页 ----------

    def _read_meta(self, resp, path):
        """从响应 JSON 读分页元数据（total/next_cursor），失败返回 None"""
        if not path:
            return None
        try:
            return self._get_path(resp.json(), path)
        except Exception:
            return None

    def _next_page(self, resp, records, spec, page) -> bool:
        """判定是否继续下一页。任一终止条件命中 → False（停）。"""
        pag = spec.get("pagination")
        if not pag:
            return False
        # ① 本页 0 条
        if not records:
            return False
        page_type = pag.get("type")
        # ② 本页条数 < page_size（末页）
        page_size = pag.get("page_size")
        if page_type in ("offset", "page") and page_size and len(records) < page_size:
            return False
        # ③ total_path 存在 → 刷新 total
        if pag.get("total_path"):
            total = self._read_meta(resp, pag.get("total_path"))
            if total is not None:
                try:
                    page.total = int(total)
                except (TypeError, ValueError):
                    page.total = None
        # ④ cursor 的 next_path 缺失/为空 → 停
        if page_type == "cursor":
            nxt = self._read_meta(resp, pag.get("next_path"))
            if not nxt:
                return False
            page.next_cursor = nxt
        page.page_no += 1
        # ⑥ max_pages 硬上限（默认 50，防死循环）
        if page.page_no >= pag.get("max_pages", DEFAULT_MAX_PAGES):
            return False
        # ③ 已取条数 ≥ total → 停
        return not (page.total is not None and page.collected >= page.total)

    # ---------- JSON 路径 ----------

    @staticmethod
    def _get_path(obj, path):
        """点路径与下标：'a.b' / 'a[0]' / 'data.items[0].title'。

        缺键/类型不符 → None（由 mapping default 兜底）。
        """
        if not path:
            return None
        cur = obj
        for name, idx in re.findall(r"(\w+)|\[(\d+)\]", str(path)):
            try:
                if name:
                    cur = cur[name]
                else:
                    cur = cur[int(idx)]
            except (KeyError, IndexError, TypeError, AttributeError):
                return None
        return cur


class _PageState:
    """分页状态机：offset/page/cursor/None 四种 + max_pages 硬上限"""

    def __init__(self, spec):
        self.pagination = spec.get("pagination")
        self.page_no = 0
        self.total: int | None = None
        self.next_cursor = None
        self.collected = 0

    def params(self) -> dict:
        """本页请求参数（静态 params 之外的分页参数）"""
        pag = self.pagination or {}
        if not pag:
            return {}
        page_type = pag.get("type")
        page_size = pag.get("page_size")
        if page_type == "offset":
            return {
                pag.get("param", "start"): self.page_no * (page_size or 10),
                pag.get("size_param", "max_results"): page_size or 10,
            }
        if page_type == "page":
            return {
                pag.get("param", "pageNum"): self.page_no + 1,
                pag.get("size_param", "pageSize"): page_size or 20,
            }
        if page_type == "cursor":
            if self.next_cursor:
                return {pag.get("param", "cursor"): self.next_cursor}
            return {}
        return {}
