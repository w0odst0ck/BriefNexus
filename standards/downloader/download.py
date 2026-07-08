"""
夸克网盘 PDF 自动化下载器 (Playwright)

用法:
  # 下载单条标准 (需要已知 bzxz_id)
  python -m standards.downloader.download --std GB/T 39394-2020

  # 批量下载 (先从 DB 读取非采标，逐条尝试)
  python -m standards.downloader.download --batch --domestic-only

  # 用已知 bzxz_id_map 批量下载
  python -m standards.downloader.download --batch --bzxz-map path/to/map.json

前置条件:
  先运行 auth.py --save 保存夸克 cookie
"""

import asyncio
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from playwright.async_api import async_playwright, Page, BrowserContext

from standards.downloader.auth import load_cookies, check_login
from standards.crawler.utils import normalize_standard_no

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("downloader.download")

# ── 配置 ──
BZXZ_BASE = "https://www.bzxz.net"
DOWNLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "downloads")
CST = timezone(timedelta(hours=8))


# ── 工具函数 ──

def _make_safe_filename(std_no: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", std_no).replace(" ", "_")


def _find_bzxz_download_page_url(html: str) -> Optional[str]:
    """从标准详情页 HTML 中提取下载页 URL (/bzxz/dl/{hash}.html)"""
    # 匹配完整 URL 或相对路径
    m = re.search(r'href="(https?://www\.bzxz\.net/bzxz/dl/[^"]+\.html)"', html)
    if m:
        return m.group(1)
    m = re.search(r'href="(/bzxz/dl/[^"]+\.html)"', html)
    if m:
        return BZXZ_BASE + m.group(1)
    return None



def _find_quark_url(html: str) -> Optional[str]:
    """从 bzxz 下载页 HTML 中提取夸克网盘分享链接"""
    m = re.search(r'pan\.quark\.cn/s/([^"\' \\]+)', html)
    if m:
        return "https://pan.quark.cn/s/" + m.group(1)
    return None


def _find_real_download_url(html: str) -> Optional[str]:
    """从下载页 HTML 中提取真实下载 URL (/bzxz/dl/down?id=xxx&exp=xxx&sign=xxx)"""
    # 先解析 HTML 实体
    html = html.replace("&amp;", "&")
    m = re.search(r'href="(https?://www\\.bzxz\\.net/bzxz/dl/down[^"]+)"', html)
    if m:
        return m.group(1)
    # 如果 JS 重定向到夸克
    m = re.search(r'window\\.location\\.href\s*=\s*["\']([^"\']+)["\']', html)
    if m:
        return m.group(1)
    return None


# ── 核心下载逻辑 ──

async def download_via_quark(context: BrowserContext,
                              std_no: str,
                              bzxz_id: int,
                              quark_url: str = None) -> Optional[str]:
    """从 bzxz.net 标准页开始，自动化下载 PDF

    Args:
        context: Playwright BrowserContext (已加载夸克 cookie)
        std_no: 标准号
        bzxz_id: bzxz.net 标准页 ID
        quark_url: 可选的直接夸克链接（跳过 bzxz 步骤）

    Returns:
        本地 PDF 文件路径，失败返回 None
    """
    page = await context.new_page()

    try:
        # ── 步骤 1: 直接给夸克链接 ──
        if quark_url:
            logger.info("[%s] 直接访问夸克: %s", std_no, quark_url[:60])
            pdf_path = await _download_from_quark(page, std_no, quark_url)
            if pdf_path:
                return pdf_path
            logger.info("[%s] 直接夸克访问失败，尝试 bzxz 路线", std_no)

        # ── 步骤 2: 通过 bzxz.net 路线 ──
        std_page_url = f"{BZXZ_BASE}/bzxz/{bzxz_id}.html"
        logger.info("[%s] 访问标准页: %s", std_no, std_page_url)

        await page.goto(std_page_url, wait_until="networkidle", timeout=30000)
        await asyncio.sleep(2)

        html = await page.content()

        # 提取下载页 URL
        dl_page_url = _find_bzxz_download_page_url(html)
        if not dl_page_url:
            logger.warning("[%s] 未找到下载页链接", std_no)
            return None

        logger.info("[%s] 访问下载页: %s", std_no, dl_page_url.split("/")[-1])

        # ── 步骤 3: 访问下载页（提取夸克链接或直接文件链接）──
        response = await page.goto(dl_page_url, wait_until="networkidle",
                                   timeout=30000)
        await asyncio.sleep(2)

        current_url = page.url
        final_html = await page.content()

        # 检查是否直接重定向到夸克
        if "pan.quark.cn" in current_url:
            logger.info("[%s] 已跳转到夸克: %s", std_no, current_url[:60])
            return await _download_from_quark_ui(page, std_no)

        # 从页面提取夸克分享链接（二维码中的链接）
        quark_url = _find_quark_url(final_html)
        if quark_url:
            logger.info("[%s] 发现夸克分享链接: %s", std_no, quark_url[:60])
            await page.goto(quark_url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(3)
            return await _download_from_quark_ui(page, std_no)

        # 尝试直接下载（down?id= -> JS redirect -> d/file/xxx.rar）
        redirect_url = _find_real_download_url(final_html)
        if redirect_url and "pan.quark.cn" not in redirect_url:
            logger.info("[%s] 发现直链: %s", std_no, redirect_url[:60])
            # 用 requests 测试（Playwright 可能无法处理 JS 重定向）
            try:
                dl_session = requests.Session()
                dl_session.headers.update({"User-Agent": "Mozilla/5.0"})
                dl_session.cookies.update(
                    {c["name"]: c["value"] for c in await context.cookies()}
                )
                r = dl_session.get(redirect_url, timeout=30, allow_redirects=True,
                                   headers={"Referer": "https://www.bzxz.net/"})
                if r.status_code == 200 and len(r.content) > 10000:
                    safe_name = _make_safe_filename(std_no)
                    local_path = os.path.join(DOWNLOAD_DIR, f"{safe_name}.rar")
                    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
                    with open(local_path, "wb") as f:
                        f.write(r.content)
                    logger.info("[%s] ✅ 直链下载: %s (%.1f KB)",
                                std_no, local_path, len(r.content) / 1024)
                    return local_path
                logger.info("[%s] 直链不可用 (404/太小), 可能已失效", std_no)
            except Exception as e:
                logger.info("[%s] 直链下载异常: %s", std_no, e)

        logger.warning("[%s] 未找到可用下载链接", std_no)
        return None

    except Exception as e:
        logger.error("[%s] 下载异常: %s", std_no, e)
        return None
    finally:
        await page.close()


async def _download_from_quark(page: Page,
                                std_no: str,
                                quark_url: str) -> Optional[str]:
    """从夸克链接直接下载（尝试获取文件 URL）"""
    try:
        await page.goto(quark_url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(2)
        return await _download_from_quark_ui(page, std_no)
    except Exception as e:
        logger.warning("[%s] 直接从夸克下载失败: %s", std_no, e)
        return None


async def _download_from_quark_ui(page: Page, std_no: str) -> Optional[str]:
    """从夸克页面 UI 出发下载 PDF

    夸克页面结构:
      - 文件列表页: 有复选框和"下载"按钮
      - 可能需要点击文件 → 进入预览页 → 下载
    """
    await asyncio.sleep(2)

    safe_name = _make_safe_filename(std_no)
    local_path = os.path.join(DOWNLOAD_DIR, f"{safe_name}.pdf")
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    try:
        # ── 策略 1: 找页面上的下载按钮 ──
        download_selectors = [
            "button:has-text('下载')",
            "a:has-text('下载')",
            "[class*=download]",
            "[class*=Download]",
            "button:has-text('保存')",
        ]

        for sel in download_selectors:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=2000):
                    # 用 Playwright 的 download 事件捕获
                    async with page.expect_download(timeout=15000) as dl_info:
                        await btn.click()
                    download = await dl_info.value
                    await download.save_as(local_path)
                    if os.path.exists(local_path) and os.path.getsize(local_path) > 1000:
                        logger.info("[%s] ✅ 下载成功: %s (%.1f KB)",
                                    std_no, local_path,
                                    os.path.getsize(local_path) / 1024)
                        return local_path
            except Exception:
                continue

        # ── 策略 2: 查找文件直链 ──
        html = await page.content()
        # 夸克通常在 JS data 中有 file_url
        file_urls = re.findall(r'(https?://[^"\']+\.pdf[^"\']*)', html)
        if file_urls:
            import requests
            r = requests.get(file_urls[0], timeout=30,
                             headers={"User-Agent": page.context.browser.user_agent})
            if r.status_code == 200 and r.content[:4] == b"%PDF":
                with open(local_path, "wb") as f:
                    f.write(r.content)
                logger.info("[%s] ✅ 直接 PDF 下载: %s", std_no, local_path)
                return local_path

        logger.warning("[%s] 夸克页面上未找到下载入口", std_no)
        return None

    except Exception as e:
        logger.warning("[%s] 夸克 UI 下载失败: %s", std_no, e)
        return None


# ── 批量调度 ──────────────────────────────────────────────

async def batch_download(bzxz_map: Dict[str, int],
                          quark_map: Dict[str, str] = None,
                          max_concurrent: int = 2):
    """批量下载标准 PDF

    Args:
        bzxz_map: {standard_no: bzxz_id}
        quark_map: {standard_no: 夸克网盘URL} 可选
        max_concurrent: 并发数
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,  # 夸克可能需要人机验证，第一次可以开窗口看
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36",
        )

        # 加载夸克 cookie
        if not await load_cookies(context):
            logger.error("请先运行 auth.py --save 登录夸克网盘")
            await browser.close()
            return

        # 验证登录
        page = await context.new_page()
        if not await check_login(page):
            logger.error("夸克 cookie 已过期，请重新运行 auth.py --save")
            await browser.close()
            return
        await page.close()

        # ── 依次下载 ──
        results = {"success": 0, "failed": 0, "skipped": 0}
        for i, (std_no, bzxz_id) in enumerate(bzxz_map.items(), 1):
            quark_url = (quark_map or {}).get(std_no)
            logger.info("[%d/%d] %s (bzxz_id=%s)", i, len(bzxz_map), std_no, bzxz_id)

            path = await download_via_quark(context, std_no, bzxz_id, quark_url)

            if path:
                results["success"] += 1
                # 注册到数据库
                _register_to_db(std_no, path)
            else:
                results["failed"] += 1

            # 间隔
            if i < len(bzxz_map):
                delay = 5 + (i % 5) * 2
                logger.info("等待 %ds...", delay)
                await asyncio.sleep(delay)

        await browser.close()

        logger.info("=" * 50)
        logger.info("批量下载完成")
        logger.info("  成功: %d / 失败: %d / 跳过: %d",
                    results["success"], results["failed"], results["skipped"])
        return results


def _register_to_db(std_no: str, local_path: str):
    """注册文件路径到 SQLite 数据库"""
    try:
        from standards.engine.storage import StandardDB
        db = StandardDB()
        row = db.get_by_standard_no(std_no)
        if row:
            db.update_local_path(row["id"], local_path)
            logger.info("✅ 路径已注册: %s → %s", std_no, local_path)
        db.close()
    except Exception as e:
        logger.warning("数据库注册失败: %s", e)


# ── BzxzID 发现（从 DB 标准和 web_search 结果） ──────────

def find_bzxz_ids_from_db(domestic_only: bool = True,
                           limit: int = 0) -> Dict[str, int]:
    """从数据库读取标准，尝试用 bzxz.net 列表页扫描找 ID

    注意: 扫描 bzxz.net 列表页只看到最新上传的标准，
    老标准可能不在前几百页。这里作为辅助手段。
    """
    from standards.crawler.platforms.search_finder import search_on_bzxz_list

    try:
        from standards.engine.storage import StandardDB
        db = StandardDB()
        if domestic_only:
            cur = db._conn.execute(
                "SELECT standard_no FROM standards "
                "WHERE is_adopted = 0 OR is_adopted IS NULL "
                "ORDER BY standard_no"
            )
        else:
            cur = db._conn.execute(
                "SELECT standard_no FROM standards ORDER BY standard_no"
            )
        nos = [r[0] for r in cur.fetchall()]
        db.close()

        if limit > 0:
            nos = nos[:limit]

        logger.info("从 bzxz.net 列表页扫描 %d 条标准...", len(nos))
        result = search_on_bzxz_list(nos, max_pages=50)
        logger.info("找到 %d/%d 条", len(result["found"]), len(nos))

        return {std_no: info["bzxz_id"]
                for std_no, info in result["found"].items()}
    except Exception as e:
        logger.error("扫描失败: %s", e)
        return {}


# ── CLI ──────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="夸克网盘 PDF 自动化下载")
    parser.add_argument("--std", help="单条标准号")
    parser.add_argument("--bzxz-id", type=int, help="对应的 bzxz_id")
    parser.add_argument("--batch", action="store_true", help="批量下载")
    parser.add_argument("--domestic-only", action="store_true",
                        help="仅下载非采标")
    parser.add_argument("--bzxz-map", help="bzxz_id 映射 JSON 文件")
    parser.add_argument("--quark-map", help="夸克链接映射 JSON 文件")
    parser.add_argument("--limit", type=int, default=0, help="上限")
    parser.add_argument("--headless", action="store_true", help="无头模式")
    args = parser.parse_args()

    if args.std:
        # 单条下载
        if not args.bzxz_id:
            print("错误: 单条下载需要指定 --bzxz-id")
            sys.exit(1)
        loop = asyncio.new_event_loop()
        loop.run_until_complete(
            batch_download({args.std: args.bzxz_id})
        )
    elif args.batch:
        # 批量下载
        bzxz_map = {}

        # 优先使用外部映射文件
        if args.bzxz_map:
            with open(args.bzxz_map) as f:
                bzxz_map = json.load(f)
            logger.info("加载 %d 条 bzxz 映射", len(bzxz_map))

        # 从 DB 扫描补充
        if args.domestic_only or not bzxz_map:
            found = find_bzxz_ids_from_db(
                domestic_only=args.domestic_only or True,
                limit=args.limit,
            )
            # 合并，不覆盖已存在的
            for k, v in found.items():
                if k not in bzxz_map:
                    bzxz_map[k] = v
            logger.info("合并后共 %d 条", len(bzxz_map))

        # 加载夸克映射
        quark_map = {}
        if args.quark_map:
            with open(args.quark_map) as f:
                quark_map = json.load(f)

        if not bzxz_map:
            print("错误: 没有找到可下载的标准")
            sys.exit(1)

        loop = asyncio.new_event_loop()
        loop.run_until_complete(batch_download(bzxz_map, quark_map))
    else:
        parser.print_help()
