"""
夸克分享链接自动保存 — 一键保存分享文件到你的夸克网盘

用法:
  python -m standards.downloader.quark_save <quark_url>
  python -m standards.downloader.quark_save --batch path/to/urls.json
"""

import asyncio
import json
import logging
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from playwright.async_api import async_playwright
from standards.downloader.auth import load_cookies, check_login

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("quark_save")


async def save_to_quark(context, share_url: str, std_no: str = "") -> bool:
    """打开夸克分享链接，点击保存到网盘

    Args:
        context: Playwright BrowserContext (已登录夸克)
        share_url: 夸克分享链接 https://pan.quark.cn/s/xxxxx
        std_no: 标准号（仅日志用）

    Returns:
        True=保存成功
    """
    page = await context.new_page()
    try:
        logger.info("[%s] 访问夸克分享页: %s", std_no or share_url[:40], share_url)
        await page.goto(share_url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(5)  # 等页面渲染

        # 打印当前信息
        current_url = page.url
        logger.info("  当前 URL: %s", current_url)

        # 检查是否被重定向到登录页
        if "passport.quark.cn" in current_url or "login" in current_url:
            logger.warning("  ❌ 未登录，跳转到登录页")
            return False

        # 检查是否需要提取密码
        page_text = await page.text_content("body") or ""
        if "提取码" in page_text or "密码" in page_text or "提取密码" in page_text:
            logger.warning("  ⚠️ 需要提取码，暂不支持自动填写")
            logger.info("  页面文本: %s", page_text[:200])
            return False

        # 查找"保存到网盘"按钮
        save_selectors = [
            "button:has-text('保存到网盘')",
            "button:has-text('保存')",
            "div:has-text('保存到网盘')",
            "[class*=save]",
            "button:has-text('转存')",
            "a:has-text('保存')",
            "[class*=Save]",
        ]

        saved = False
        for sel in save_selectors:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=3000):
                    btn_text = await btn.text_content()
                    logger.info("  找到按钮: '%s' (selector=%s)", btn_text.strip() if btn_text else "", sel)
                    await btn.click()
                    await asyncio.sleep(3)
                    saved = True
                    logger.info("  ✅ 已点击保存!")
                    break
            except Exception:
                continue

        if not saved:
            # 打印页面详情
            html = await page.content()
            # 搜索关键文本
            for kw in ["保存到网盘", "保存", "转存", "下载"]:
                if kw in html:
                    idx = html.index(kw)
                    snippet = html[max(0, idx-100):idx+100]
                    logger.info("  发现文本 '%s': ...%s...", kw, snippet.strip()[:120])

            # 检查是否已经保存过
            if "已保存" in page_text or "已转存" in page_text:
                logger.info("  📦 文件已存在网盘中（之前已保存）")
                saved = True
            elif "页面不存在" in page_text or "分享已失效" in page_text or "不存在" in page_text:
                logger.warning("  ❌ 分享链接已失效")
            else:
                logger.warning("  ❌ 未找到保存按钮")
                logger.info("  页面标题: %s", await page.title())
                # 截图保存（如果有显示）
                try:
                    await page.screenshot(path="/tmp/quark_debug.png")
                    logger.info("  截图已保存: /tmp/quark_debug.png")
                except:
                    pass

        return saved

    except Exception as e:
        logger.error("[%s] 异常: %s", std_no or share_url[:40], e)
        return False
    finally:
        await page.close()


async def batch_save(urls: list):
    """批量处理夸克分享链接

    Args:
        urls: [{"std_no": str, "quark_url": str}, ...]
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        )

        if not await load_cookies(context):
            logger.error("请先运行 auth.py --save 登录夸克网盘")
            await browser.close()
            return

        page = await context.new_page()
        if not await check_login(page):
            logger.error("夸克 cookie 已过期，请重新运行 auth.py --save")
            await browser.close()
            return
        await page.close()

        results = {"success": 0, "failed": 0, "already_saved": 0}
        for i, item in enumerate(urls, 1):
            std_no = item.get("std_no", "")
            quark_url = item["quark_url"]
            logger.info("[%d/%d] %s", i, len(urls), std_no or quark_url[:40])

            success = await save_to_quark(context, quark_url, std_no)
            if success:
                results["success"] += 1
            else:
                results["failed"] += 1

            if i < len(urls):
                delay = 3
                await asyncio.sleep(delay)

        await browser.close()

        logger.info("=" * 50)
        logger.info("保存完成: 成功 %d / 失败 %d",
                    results["success"], results["failed"])
        return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="夸克分享链接自动保存")
    parser.add_argument("url", nargs="?", help="单条夸克分享链接")
    parser.add_argument("--batch", help="批量处理JSON文件 (格式: [{std_no, quark_url}])")
    parser.add_argument("--std", help="标准号（单条时）")
    args = parser.parse_args()

    if args.batch:
        with open(args.batch) as f:
            items = json.load(f)
        asyncio.run(batch_save(items))
    elif args.url:
        asyncio.run(batch_save([{"std_no": args.std or "", "quark_url": args.url}]))
    else:
        # 默认从 manifest 读取
        manifest_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                      "downloads", "_download_manifest.json")
        if os.path.exists(manifest_path):
            with open(manifest_path) as f:
                data = json.load(f)
            items = [{"std_no": k, "quark_url": v["quark_url"]}
                     for k, v in data.items() if v.get("quark_url")]
            # 过滤掉已下载的
            already_dl = {"GB/T 32481-2016", "GB/T 39021-2020",
                         "GB/T 3027-2012", "GB/T 42824-2023", "GB/T 7000.1-2023",
                         "GB 7258-2017", "GB/T 18661-2020", "GB/T 3836.1-2021"}
            items = [i for i in items if i["std_no"] not in already_dl]
            print(f"从 manifest 加载 {len(items)} 条未处理链接")
            asyncio.run(batch_save(items))
        else:
            parser.print_help()
