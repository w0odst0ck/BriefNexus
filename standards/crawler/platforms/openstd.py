"""
国家标准全文公开系统 (openstd.samr.gov.cn)

"公开系统" — 收录现行有效推荐性国家标准 ~46,592 项。
其中非采标 ~30,803 项可在线阅读和下载，采标 ~15,789 项仅提供题录。

与 std.samr.gov.cn (SAMR 搜索平台) 不同，openstd 的 record ID (hcno)
与 SAMR 的 id/UUID 是两个不同的编号体系。

本适配器用途：
  1. 搜索标准号 / 标题 → 获取 hcno
  2. 判断是否 采标 → 决定能否下载全文
  3. 获取 PDF 下载 URL（viewGb?hcno=... 前置需要 showGb 预热 session）

下载流程：
  1. newGbInfo?hcno=...   → 详情页（建立 session）
  2. showGb?type=download → 触发服务端校验
  3. viewGb?hcno=...      → 返回 PDF 文件流
"""

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional, Tuple

from bs4 import BeautifulSoup
from requests import Session

from ..utils import (
    safe_get, safe_get_json, new_session,
    normalize_standard_no, make_standard_item,
    logger as root_logger
)
from .base import BaseStandardCollector

logger = logging.getLogger("standards.openstd")

BASE_URL = "https://openstd.samr.gov.cn"
SEARCH_URL = f"{BASE_URL}/bzgk/std/std_list"
DETAIL_URL = f"{BASE_URL}/bzgk/std/newGbInfo"
DOWNLOAD_TRIGGER = f"{BASE_URL}/bzgk/std/showGb"
PDF_URL = f"{BASE_URL}/bzgk/std/viewGb"

# ── ICS code → description map (subset relevant to lighting) ──
ICS_MAP = {
    "29.140": "照明",
    "29.140.01": "照明综合",
    "29.140.10": "灯头和灯座",
    "29.140.20": "白炽灯",
    "29.140.30": "荧光灯、放电灯",
    "29.140.40": "灯具",
    "29.140.50": "照明安装系统",
    "29.140.99": "照明其他标准",
    "91.140": "建筑物中的安装",
    "91.140.01": "建筑物安装综合",
    "91.140.50": "供电系统",
    "91.140.99": "建筑物安装其他",
}


class OpenStdCollector(BaseStandardCollector):
    """openstd 平台适配器 — 搜索 + 获取 hcno + 可下载性判断"""

    source_name = "openstd"
    display_name = "国家标准全文公开系统"

    def search_by_keyword(self, keyword: str, page: int = 1) -> List[dict]:
        return self._search(p2=keyword, page=page)

    def search_by_ics(self, ics_code: str, page: int = 1) -> List[dict]:
        return self._search(ics=ics_code, page=page)

    def _search(self, p2: str = "", ics: str = "", page: int = 1,
                page_size: int = 20) -> List[dict]:
        """搜索 openstd 标准列表

        URL 参数说明:
          p.p1 — 标准类别 (0=全部, 1=强制性, 2=推荐性, 3=指导性, 6=外文版)
          p.p2 — 关键词
          p.p90 / p.p91 — 排序
        """
        params = {
            "p.p1": "0",
            "p.p90": "circulation_date",
            "p.p91": "desc",
        }
        if p2:
            params["p.p2"] = p2
        # ICS 过滤在页面侧栏，需要额外参数；暂通过 p.p2 模拟

        try:
            r = self.session.get(SEARCH_URL, params=params, timeout=30,
                                 allow_redirects=True)
            r.raise_for_status()
        except Exception as e:
            logger.warning("搜索失败: %s — %s", p2[:30], e)
            return []

        return self._parse_search_page(r.text)

    def _parse_search_page(self, html: str) -> List[dict]:
        """解析搜索页 HTML → 标准条目列表（含 hcno + 采标状态）"""
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table", class_="result_list")
        if not table:
            # 无结果或页面异常
            return []

        results = []
        for row in table.find_all("tr")[1:]:  # skip header
            cells = row.find_all("td")
            if len(cells) < 5:
                continue

            item = self._parse_row(row, cells)
            if item:
                results.append(item)

        return results

    def _parse_row(self, row, cells) -> Optional[dict]:
        """解析一行 → 标准条目（包含 hcno / 采标标记）"""
        std_no = cells[1].get_text(strip=True) if len(cells) > 1 else ""
        title = cells[4].get_text(strip=True) if len(cells) > 4 else ""

        # 采标状态 (col 3 = 语种, col 4 = 是否采标, depending on layout)
        # Actually column mapping: 序号(0) 标准号(1) 语种(2) 是否采标(3) 标准名称(4) 类别(5) 状态(6) 发布日期(7) 实施日期(8) 操作(9)
        adopted_text = cells[3].get_text(strip=True) if len(cells) > 3 else ""
        is_adopted = "采" in adopted_text and "非采" not in adopted_text

        # 状态
        status = cells[6].get_text(strip=True) if len(cells) > 6 else ""

        # 日期
        pub_date = cells[7].get_text(strip=True) if len(cells) > 7 else ""
        impl_date = cells[8].get_text(strip=True) if len(cells) > 8 else ""

        # hcno（从 showInfo 调用中提取）
        onclick_el = row.find("a", onclick=re.compile(r"showInfo"))
        if not onclick_el:
            onclick_el = row.find(onclick=re.compile(r"showInfo"))
        hcno = ""
        if onclick_el:
            m = re.search(r"showInfo\('([^']+)'\)", onclick_el.get("onclick", ""))
            if m:
                hcno = m.group(1)

        if not std_no and not title:
            return None

        # 构建条目（复用标准化工具）
        item = make_standard_item(
            title=title,
            standard_no=normalize_standard_no(std_no),
            publisher="",
            publish_date=pub_date,
            status=status,
            category="国标" if std_no.upper().startswith("GB") else "",
            url=f"{DETAIL_URL}?hcno={hcno}" if hcno else "",
            source=self.source_name,
        )

        # 是否采标
        item["_is_adopted"] = is_adopted
        item["_hcno"] = hcno
        item["_impl_date"] = impl_date

        # 标准类型 (推标/强标)
        type_text = cells[5].get_text(strip=True) if len(cells) > 5 else ""
        item["_std_type"] = type_text

        return item

    # ── 可下载性检查 ──────────────────────────────────────

    def check_availability(self, hcno: str) -> dict:
        """检查标准是否有全文可下载

        注意: "暂无全文" 文本存在于 JS i18n 字典中，不代表当前标准无全文。
        真实判断方式：尝试访问 viewGb 端点检查是否返回 PDF。

        Returns:
            {
                "available": bool,    # 是否有全文（经过实际试探）
                "is_adopted": bool,   # 是否采标
                "title": str,
                "standard_no": str,
            }
        """
        result = {"available": False, "is_adopted": False,
                  "title": "", "standard_no": ""}
        if not hcno:
            return result

        try:
            # 先访问详情页（建立 session）
            r = self.session.get(f"{DETAIL_URL}?hcno={hcno}", timeout=15,
                                 allow_redirects=True)
            html = r.text

            # 提取标题
            title_m = re.search(r"<title>(.*?)</title>", html)
            if title_m:
                result["title"] = title_m.group(1).replace("国家标准|", "")
                result["standard_no"] = result["title"]

            # 尝试 viewGb 试探是否可下载
            # 如果不需要验证码，会返回 PDF；否则返回 0 字节或错误页
            trigger_url = f"{DOWNLOAD_TRIGGER}?type=download&hcno={hcno}&request_locale=zh"
            self.session.get(trigger_url, timeout=15, allow_redirects=True,
                              headers={"Referer": DETAIL_URL})

            probe = self.session.get(f"{PDF_URL}?hcno={hcno}", timeout=30)
            if probe.status_code == 200 and probe.content and probe.content[:4] == b"%PDF":
                result["available"] = True
                result["is_adopted"] = False
            else:
                result["available"] = False
                # 采标标记
                result["is_adopted"] = bool(re.search(r'采[^非]', html))

            return result
        except Exception as e:
            logger.warning("检查可下载性失败 hcno=%s: %s", hcno[:16], e)
            return result

    # ── 查找 hcno ────────────────────────────────────────

    def find_hcno(self, standard_no: str, title: str = "") -> Optional[str]:
        """通过标准号搜索 openstd，返回 hcno

        搜索策略（逐级退火）：
          1. 完整标准号搜索（精确匹配，含年份）
          2. 去掉点号后缀搜索（GB/T 30104.103 → GB/T 30104）
          3. 数字前缀搜索（取 GB/T 后的数字段）
          4. 标题关键词搜索
        """
        target_no = standard_no.replace(" ", "").strip()
        target_prefix = target_no.split("-")[0] if "-" in target_no else target_no

        def _match(items):
            """在搜索结果中精确匹配标准号（含年份）"""
            for it in items:
                db_no = it.get("standard_no", "").replace(" ", "")
                if db_no == target_no:
                    return it.get("_hcno")
            return None

        import re as _re

        # 方法1：完整标准号搜索
        result = _match(self._search(p2=standard_no))
        if result:
            return result

        # 方法2：去掉点号后缀重新搜索
        if "." in target_prefix:
            parts = target_prefix.split(".")
            for i in range(len(parts) - 1, 0, -1):
                shortened = ".".join(parts[:i])
                result = _match(self._search(p2=shortened))
                if result:
                    return result

        # 方法3：数字前缀搜索
        nums = _re.findall(r"\d+", target_prefix)
        for n in nums[:3]:  # 从短到长尝试
            result = _match(self._search(p2=n))
            if result:
                return result

        # 方法4：标题关键词搜索
        if title:
            kw = title[:20]
            result = _match(self._search(p2=kw))
            if result:
                return result

        return None

    def find_hcno_batch(self, standards: list, max_workers: int = 3,
                          check_downloadable: bool = False) -> list:
        """批量查找 hcno

        Args:
            standards: [{standard_no, title, ...}]
            check_downloadable: 是否同时检查可下载性（会额外请求）

        Returns:
            补充了 _hcno 等字段的条目
        """
        def _find(s):
            no = s.get("standard_no", "")
            title = s.get("title", "")
            hcno = self.find_hcno(no, title)
            s["_hcno"] = hcno or ""
            s["_is_adopted"] = False  # 未知，交给下载时判断
            s["_has_fulltext"] = bool(hcno)  # 有 hcno 就有机会
            return s

        enriched = []
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = [ex.submit(_find, s) for s in standards]
            for i, f in enumerate(as_completed(futures)):
                enriched.append(f.result())
                if (i + 1) % 10 == 0:
                    logger.info("查找 hcno: %d/%d", i + 1, len(standards))

        return enriched


# ── PDF 下载函数（独立于 Collector 类） ──────────────────

def download_pdf(hcno: str, standard_no: str, session: Session = None,
                 download_dir: str = None) -> Optional[str]:
    """通过 openstd 平台下载标准 PDF

    3 步流程:
      1. newGbInfo — 详情页（建立 session 状态）
      2. showGb — 触发服务端校验
      3. viewGb — 获取 PDF 文件流

    Args:
        hcno: openstd 平台的标准 ID
        standard_no: 标准号 (用于文件名)
        session: requests Session（可复用）
        download_dir: 下载目录

    Returns:
        本地文件路径，失败返回 None
    """
    if not hcno:
        return None

    if session is None:
        session = new_session()

    if download_dir is None:
        from pathlib import Path
        download_dir = str(Path(__file__).resolve().parent.parent.parent / "downloads")

    import os
    os.makedirs(download_dir, exist_ok=True)

    safe_name = re.sub(r'[\\/:*?"<>|]', "_", standard_no)
    local_path = os.path.join(download_dir, f"{safe_name}.pdf")

    if os.path.exists(local_path):
        return local_path

    try:
        # Step 1: 详情页 (建立 session)
        detail_url = f"{DETAIL_URL}?hcno={hcno}"
        session.get(detail_url, timeout=15, allow_redirects=True)

        # Step 2: showGb?type=download（触发服务端校验，必须 follow redirect）
        trigger_url = f"{DOWNLOAD_TRIGGER}?type=download&hcno={hcno}&request_locale=zh"
        r2 = session.get(trigger_url, timeout=15, allow_redirects=True,
                          headers={"Referer": detail_url})

        # Step 2 返回 404 说明该标准无公开全文
        if r2.status_code in (404, 403) or r2.url.endswith("404.jsp"):
            logger.warning("无公开全文: %s (showGb返回 %d)", standard_no, r2.status_code)
            return None

        # Step 3: viewGb（获取 PDF）
        pdf_url = f"{PDF_URL}?hcno={hcno}"
        r3 = session.get(pdf_url, timeout=30, allow_redirects=True)

        if r3.status_code == 200 and r3.content and r3.content[:4] == b"%PDF":
            with open(local_path, "wb") as f:
                f.write(r3.content)
            logger.info("✅ 已下载: %s → %s (%d KB)",
                        standard_no, local_path, len(r3.content) // 1024)
            return local_path
        elif r3.content and len(r3.content) > 0:
            logger.warning("响应不是 PDF: %s (Content-Type=%s, len=%d)",
                           standard_no, r3.headers.get("Content-Type", ""), len(r3.content))
        else:
            logger.warning("空响应: %s (可能需验证码或已被采标)", standard_no)

        return None

    except Exception as e:
        logger.error("下载失败 %s: %s", standard_no, e)
        return None


def batch_download(items: list, max_workers: int = 3, limit: int = 0) -> tuple:
    """批量下载标准 PDF

    Args:
        items: [{standard_no, _hcno, ...}]  需要先通过 OpenStdCollector 获取 hcno
        max_workers: 并发下载数
        limit: 下载上限（0=全部）

    Returns:
        (成功数, 失败数)
    """
    if limit > 0:
        items = items[:limit]

    # 只处理有 hcno 的（有 hcno 就有机会下载）
    candidates = [it for it in items if it.get("_hcno")]

    if not candidates:
        logger.info("没有可下载的标准（所有标准均未在 openstd 找到 hcno）")
        return (0, 0)

    logger.info("准备下载: %d 个(共 %d 个候选; %d 无 hcno)",
                len(candidates), len(items), len(items) - len(candidates))

    success = 0
    failed = 0
    total = len(candidates)

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {}
        for it in candidates:
            no = it.get("standard_no", "")
            hcno = it.get("_hcno", "")
            f = ex.submit(download_pdf, hcno, no)
            futures[f] = no

        for i, f in enumerate(as_completed(futures), 1):
            no = futures[f]
            try:
                result = f.result()
                if result:
                    success += 1
                else:
                    failed += 1
            except Exception as e:
                logger.error("下载异常 %s: %s", no, e)
                failed += 1

            if i % 10 == 0 or i == total:
                logger.info("进度: %d/%d (成功 %d / 失败 %d)",
                            i, total, success, failed)

    return (success, failed)
