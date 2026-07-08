"""
夸克网盘登录态管理

首次使用：
  python -m standards.downloader.auth --save

  这会打开一个 Chromium 浏览器窗口，你手动登录夸克网盘后按回车，
  脚本会自动保存 cookie 到 downloads/_quark_cookies/

后续使用 cookie 自动登录：
  python -m standards.downloader.auth --check
"""

import asyncio
import json
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("downloader.auth")

# ── 配置 ──
COOKIE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "downloads", "_quark_cookies")
COOKIE_FILE = os.path.join(COOKIE_DIR, "quark_cookies.json")
QUARK_URL = "https://pan.quark.cn/"


def get_cookie_path() -> str:
    os.makedirs(COOKIE_DIR, exist_ok=True)
    return COOKIE_FILE


async def save_cookies(browser_context=None):
    """
    打开浏览器让用户手动登录夸克，然后保存 cookie。
    如果传入 browser_context，直接保存其 cookie。
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser_context or await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36",
        )
        page = await context.new_page()

        logger.info("正在打开夸克网盘...")
        logger.info("请在弹出的浏览器中手动登录夸克网盘。")
        logger.info("登录完成后，回到终端按 Enter 继续...")
        await page.goto(QUARK_URL, wait_until="networkidle")

        # 等待用户按回车
        await asyncio.get_event_loop().run_in_executor(None, input)

        # 保存 cookies
        cookies = await context.cookies()
        cookie_path = get_cookie_path()
        with open(cookie_path, "w") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)
        logger.info("Cookie 已保存: %s (%d 条)", cookie_path, len(cookies))

        await browser.close()
        return cookies


async def load_cookies(browser_context) -> bool:
    """向 browser_context 注入已保存的夸克 cookie

    Returns:
        True=成功加载 cookie
    """
    cookie_path = get_cookie_path()
    if not os.path.exists(cookie_path):
        logger.warning("Cookie 文件不存在: %s", cookie_path)
        logger.warning("请先运行: python -m standards.downloader.auth --save")
        return False

    with open(cookie_path) as f:
        cookies = json.load(f)

    await browser_context.add_cookies(cookies)
    logger.info("已加载 %d 条 cookie", len(cookies))
    return True


async def check_login(page) -> bool:
    """检查当前登录状态

    通过访问夸克首页看是否重定向到登录页
    """
    await page.goto("https://pan.quark.cn/", wait_until="networkidle",
                    timeout=30000)
    current_url = page.url
    if "passport.quark.cn" in current_url or "login" in current_url:
        logger.warning("未登录状态，需要重新登录")
        return False
    logger.info("夸克网盘已登录 ✅")
    return True


# ── CLI 入口 ──

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="夸克网盘登录态管理")
    parser.add_argument("--save", action="store_true", help="打开浏览器登录并保存 cookie")
    parser.add_argument("--check", action="store_true", help="检查 cookie 有效性")
    args = parser.parse_args()

    if args.save:
        asyncio.run(save_cookies())
    elif args.check:
        async def _check():
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context()
                ok = await load_cookies(context)
                if ok:
                    page = await context.new_page()
                    logged_in = await check_login(page)
                    print("登录状态:", "✅ 有效" if logged_in else "❌ 已过期")
                await browser.close()
        asyncio.run(_check())
    else:
        parser.print_help()
