"""
渲染 worker — 独立子进程入口(在 playwright venv 内运行)

用法: <venv-python> render_worker.py <url> [--timeout 30]
stdout: 单行 JSON {"ok","html","final_url","status_code","error"}

设计约束:
- playwright 延迟 import(函数内), venv 未装 playwright 时归一为 error JSON
- headless chromium, 每请求 launch → goto → content → browser.close(用完即关)
- goto 超时/networkidle 永不触发(长轮询页)均不阻塞: 捕获后继续取 page.content()
- 任何异常只进 error 字段, 不吐堆栈到 stdout, 进程绝不崩溃
"""
import argparse
import json


def _goto(page, url: str, timeout: float):
    """goto(domcontentloaded)。失败/超时返回 None, 不阻塞(仍尝试取部分 DOM)"""
    try:
        return page.goto(url, timeout=int(timeout * 1000), wait_until="domcontentloaded")
    except Exception:
        return None


def _wait_networkidle(page, timeout: float) -> None:
    """networkidle 等待, 钳制 min(timeout, 10)s; 长轮询页永不触发时静默返回"""
    try:
        page.wait_for_load_state("networkidle", timeout=int(min(timeout, 10.0) * 1000))
    except Exception:
        return


def _render(url: str, timeout: float) -> dict:
    """渲染 URL → 结果 dict。launch 失败等异常向上传播给 main 统一捕获。"""
    # 延迟 import: 仅在本 venv 具备 playwright 时可用
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            # domcontentloaded 即返回; load 级等待由下方 networkidle 钳制
            resp = _goto(page, url, timeout)
            _wait_networkidle(page, timeout)
            html = page.content()
            return {
                "ok": bool(html and html.strip()),
                "html": html,
                "final_url": page.url,
                "status_code": resp.status if resp is not None else 0,
                "error": "",
            }
        finally:
            browser.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="playwright 渲染 worker")
    parser.add_argument("url", help="要渲染的 URL")
    parser.add_argument("--timeout", type=float, default=30.0, help="渲染超时秒数")
    args = parser.parse_args(argv)

    result = {"ok": False, "html": "", "final_url": "", "status_code": 0, "error": ""}
    try:
        result = _render(args.url, args.timeout)
    except Exception as e:  # 任何异常归一为 error JSON, 不崩溃
        result = {"ok": False, "html": "", "final_url": "",
                  "status_code": 0, "error": str(e)[:500]}
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
