"""通用 URL 采集器 — 输入任意网址，自动选路抓取并抽取正文。

三级降级（实测依据 2026-11-25）：
  ① 静态 requests 抓取（0.1-2s）—— 覆盖约 70% 站点（cninfo/eastmoney/sspai）
  ② 内容不足 → CDP 渲染（15-20s）—— 覆盖约 20%（依赖 JS 的站）
  ③ 仍不足 → 明确失败并说明原因（SPA/反爬/需登录，如 36kr）

正文抽取三级：
  ① trafilatura（主）—— 学术级抽取，保留段落/标题结构
  ② readabilipy（备）—— Mozilla readability 移植，覆盖 trafilatura 失败场景
  ③ BeautifulSoup 去噪（兜底）—— 零依赖，质量较低但绝不返回空

设计约束：
  - crawl 永不抛异常（采集器协议）
  - 失败必须有明确 error，不静默返回空（nag 假绿灯教训）
  - 单 URL 采集，不做批量（批量由调用方循环）
"""
from __future__ import annotations

import logging
import re
import time
from typing import ClassVar
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from intel.core.base import BaseCollector, NewsItem

logger = logging.getLogger("intel.generic_url")

# 正文足够的判定阈值（字符数）——低于此值触发降级
MIN_STATIC_CHARS = 500
# 单次抓取上限（字符数），防超大页面撑爆内存
MAX_CHARS = 50_000
# 提取链接上限
MAX_LINKS = 200

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class GenericUrlCollector(BaseCollector):
    """任意 URL → NewsItem（type=page，正文/链接/图片放 raw_data）"""

    source_name = "generic_url"
    display_name = "通用网址采集"
    domains = ["generic"]

    PARAM_SCHEMA: ClassVar[dict] = {
        "url": {"type": "str", "required": True, "desc": "目标网址（http/https）"},
        "force_render": {"type": "bool", "default": False,
                         "desc": "强制用 CDP 渲染（慢但能过 JS 站）"},
        "max_chars": {"type": "int", "default": MAX_CHARS,
                      "min": 1000, "max": 200000, "desc": "正文上限字符数"},
        "include_links": {"type": "bool", "default": True, "desc": "是否提取页面链接"},
        "include_images": {"type": "bool", "default": True, "desc": "是否提取图片地址"},
        "timeout": {"type": "float", "default": 20.0, "min": 5, "max": 120},
        "max_age": {"type": "int", "default": 7, "min": 1, "max": 90},
    }

    def __init__(self, url: str = "", force_render: bool = False,
                 max_chars: int = MAX_CHARS, include_links: bool = True,
                 include_images: bool = True, timeout: float = 20.0,
                 max_age: int = 7, **kwargs):
        super().__init__(max_age=max_age)
        self.url = (url or "").strip()
        self.force_render = bool(force_render)
        self.max_chars = int(max_chars)
        self.include_links = bool(include_links)
        self.include_images = bool(include_images)
        self.timeout = float(timeout)

    # ---------- 主流程 ----------

    def crawl(self, sess) -> list[NewsItem]:
        """永不抛异常。失败返回 [] 并记 error 日志。"""
        if not self._valid_url(self.url):
            logger.error("URL 非法或非 http(s)：%r", self.url[:120])
            return []

        started = time.time()
        html = ""
        method = ""

        # ① 静态抓取（除非强制渲染）
        if not self.force_render:
            html, status = self._fetch_static(sess)
            method = "static"

        # ② 静态不足 → 渲染降级
        text_probe = self._extract(html)[0] if html else ""
        need_render = self.force_render or len(text_probe) < MIN_STATIC_CHARS

        if need_render:
            rendered = self._fetch_render()
            if rendered:
                render_text = self._extract(rendered)[0]
                # 渲染结果更好才替换（避免渲染反而不如静态）
                if len(render_text) > len(text_probe):
                    html, method = rendered, "render"
                else:
                    logger.info("渲染内容未优于静态（%d vs %d 字），保留静态",
                                len(render_text), len(text_probe))
            elif not html:
                logger.error("静态与渲染均失败：%s", self.url[:120])
                return []

        if not html:
            logger.error("抓取失败（无内容）：%s", self.url[:120])
            return []

        # ③ 正文抽取（三级）
        text, extractor = self._extract(html)
        text = text[: self.max_chars]
        soup = BeautifulSoup(html, "html.parser")
        title = self._title(soup)

        elapsed = round(time.time() - started, 2)
        if not text:
            logger.error("正文抽取为空（%s）：%s", extractor, self.url[:120])
            # 仍返回一条含 title 的条目，让调用方知道抓到页面但无正文
            return [self._make_item(title, "", method, extractor, elapsed, soup)]

        # 内容过少时给出明确提示（而非让调用方以为抓到的是全部）
        # 典型场景：JS 渲染站 + 环境无 playwright → 静态只能拿到壳
        quality = "good" if len(text) >= MIN_STATIC_CHARS else "thin"
        if quality == "thin":
            logger.warning("正文过少（%d 字）：%s —— 可能是 JS 站，且本环境渲染不可用",
                           len(text), self.url[:60])

        logger.info("抓取成功 %s | %s/%s | %d 字 | %.2fs",
                    self.url[:60], method, extractor, len(text), elapsed)
        return [self._make_item(title, text, method, extractor, elapsed, soup, quality)]

    # ---------- 抓取 ----------

    def _fetch_static(self, sess) -> tuple[str, int]:
        try:
            headers = {"User-Agent": DEFAULT_UA,
                       "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                       "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
            r = sess.get(self.url, timeout=self.timeout, headers=headers)
            r.raise_for_status()
            # 部分中文站声明错误编码，用 apparent_encoding 兜底
            if not r.encoding or r.encoding.lower() in ("iso-8859-1", "ascii"):
                r.encoding = r.apparent_encoding or "utf-8"
            return r.text, r.status_code
        except requests.HTTPError as e:
            logger.warning("静态抓取 HTTP 错误：%s", e)
            return "", 0
        except requests.RequestException as e:
            logger.warning("静态抓取失败（%s）：%s", type(e).__name__, e)
            return "", 0

    def _fetch_render(self) -> str:
        """CDP 渲染。失败返回空串（绝不抛异常）。"""
        try:
            from intel.core.render import RenderExecutor
        except ImportError as e:
            logger.warning("渲染模块不可用：%s", e)
            return ""
        try:
            ex = RenderExecutor(default_timeout=max(30.0, self.timeout))
            result = ex.render(self.url)
            if result.ok and result.html:
                return result.html
            logger.warning("渲染失败：%s", (result.error or "")[:120])
            return ""
        except Exception as e:      # 渲染模块任何异常都不得中断采集
            logger.warning("渲染异常：%s", e)
            return ""

    # ---------- 正文抽取（三级） ----------

    def _extract(self, html: str) -> tuple[str, str]:
        """返回 (正文, 抽取器名)。三级降级，绝不返回 None。"""
        if not html:
            return "", "none"

        # ① trafilatura
        try:
            import trafilatura
            # 不用 favor_precision：实测会把 eastmoney 正文从 1319 字压到 138 字
            # （列表页/新闻列表结构被当成噪声剔除）。默认参数最均衡。
            text = trafilatura.extract(
                html, include_comments=False, include_tables=True,
                include_images=False)
            if text and text.strip():
                return self._clean(text), "trafilatura"
        except Exception as e:
            logger.debug("trafilatura 失败：%s", e)

        # ② readabilipy（Mozilla readability 移植）
        try:
            from readabilipy import simple_json_from_html_string
            article = simple_json_from_html_string(html, use_readability=False)
            plain = article.get("plain_text") or []
            text = "\n\n".join(p.get("text", "") for p in plain if p.get("text"))
            if text.strip():
                return self._clean(text), "readabilipy"
        except Exception as e:
            logger.debug("readabilipy 失败：%s", e)

        # ③ BeautifulSoup 去噪兜底
        try:
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header",
                             "aside", "form", "noscript", "iframe"]):
                tag.decompose()
            body = soup.body or soup
            text = body.get_text("\n", strip=True)
            return self._clean(text), "bs4"
        except Exception as e:
            logger.warning("bs4 兜底也失败：%s", e)
            return "", "none"

    @staticmethod
    def _clean(text: str) -> str:
        """压缩空白，去掉过短的行（导航/按钮碎片）"""
        lines = []
        for raw in text.splitlines():
            line = re.sub(r"[ \t\u3000]+", " ", raw).strip()
            # 保留 >=6 字的行（过滤单字导航）
            if len(line) >= 6:
                lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def _title(soup: BeautifulSoup) -> str:
        if soup.title and soup.title.string:
            return soup.title.string.strip()[:300]
        h1 = soup.find("h1")
        if h1:
            return h1.get_text(strip=True)[:300]
        return ""

    # ---------- 辅助 ----------

    def _make_item(self, title: str, text: str, method: str,
                   extractor: str, elapsed: float,
                   soup: BeautifulSoup, quality: str = "good") -> NewsItem:
        base = self.url
        raw = {
            "url": self.url,
            "method": method,           # static | render
            "extractor": extractor,     # trafilatura | readabilipy | bs4
            "elapsed_s": elapsed,
            "text": text,
            "text_chars": len(text),
            # good=内容充实；thin=可能只抓到页面壳（JS 站且渲染不可用）
            "quality": quality,
        }
        if self.include_links:
            raw["links"] = self._links(soup, base)
        if self.include_images:
            raw["images"] = self._images(soup, base)

        return NewsItem(
            title=title or self.url,
            url=self.url,
            summary=text[:300],
            source=self.display_name,
            domain="generic",
            type="page",
            raw_data=raw,
        )

    @staticmethod
    def _links(soup: BeautifulSoup, base: str) -> list[dict]:
        seen, out = set(), []
        for a in soup.find_all("a", href=True):
            href = urljoin(base, a["href"].strip())
            if not href.startswith(("http://", "https://")) or href in seen:
                continue
            text = a.get_text(strip=True)[:120]
            if not text:
                continue
            seen.add(href)
            out.append({"text": text, "href": href})
            if len(out) >= MAX_LINKS:
                break
        return out

    @staticmethod
    def _images(soup: BeautifulSoup, base: str) -> list[dict]:
        seen, out = set(), []
        for img in soup.find_all("img", src=True):
            src = urljoin(base, img["src"].strip())
            if not src.startswith(("http://", "https://")) or src in seen:
                continue
            seen.add(src)
            out.append({"src": src, "alt": (img.get("alt") or "").strip()[:200]})
            if len(out) >= MAX_LINKS:
                break
        return out

    @staticmethod
    def _valid_url(url: str) -> bool:
        if not url:
            return False
        try:
            p = urlparse(url)
            return p.scheme in ("http", "https") and bool(p.netloc)
        except ValueError:
            return False