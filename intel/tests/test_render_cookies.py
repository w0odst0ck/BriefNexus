"""render cookie 注入单测(R1-R11)— 全 stub/mock, 不真发网络、不真启浏览器

覆盖(design §2.1):
  R1  render(url, cookies=None): cmd 无 --cookies-stdin, input 为 None(等价未传)
  R2  cookies 非空: cmd 追加 --cookies-stdin, 值仅经 stdin JSON, argv 无 cookie 值
  R3  cookies=[]: 视同无 cookie(无标志、无 stdin 载荷)
  R4  get(url, cookies=X) → executor.render(url, timeout, cookies=X)
  R5  构造默认 cookies → get(url) 用默认
  R6  get(url, cookies=Y) 覆盖构造默认 X
  R7  渲染失败降级静态: inner.get 收到 requests cookie dict(list 转 dict)
  R8  worker _render: browser.new_context() + context.add_cookies(cookies)
  R9  worker cookies=None → add_cookies 不被调
  R10 worker add_cookies 抛异常 → 继续渲染, error 为固定文案(不含 cookie 值)
  R11 worker stdin 坏 JSON → 不崩溃, 无 cookie 渲染
"""
import contextlib
import io
import json
import sys
import types
import unittest
from unittest import mock

import requests
from intel.core import render_worker
from intel.core.render import (
    RenderAwareSession,
    RenderedResponse,
    RenderExecutor,
    RenderResult,
)

# ---------- fake playwright(主 venv 可能未装, 注入可 import 的假包) ----------

def _install_fake_playwright() -> None:
    if "playwright" not in sys.modules:
        pkg = types.ModuleType("playwright")
        sync = types.ModuleType("playwright.sync_api")
        sync.sync_playwright = None  # 占位, 由各用例 patch
        pkg.sync_api = sync
        sys.modules["playwright"] = pkg
        sys.modules["playwright.sync_api"] = sync


_install_fake_playwright()


class _FakeResp:
    def __init__(self, status=200):
        self.status = status


class _FakePage:
    def __init__(self, html="<html>detail</html>",
                 url="https://www.zhipin.com/job_detail/1.html"):
        self._html = html
        self.url = url
        self._resp = _FakeResp()

    def goto(self, url, timeout=None, wait_until=None):
        return self._resp

    def wait_for_load_state(self, *args, **kwargs):
        return None

    def content(self):
        return self._html


class _FakeContext:
    def __init__(self, page):
        self.page = page
        self.added = []

    def add_cookies(self, cookies):
        self.added.append(list(cookies))

    def new_page(self):
        return self.page


class _FakeBrowser:
    def __init__(self, context):
        self.context = context
        self.closed = False

    def new_context(self):
        return self.context

    def close(self):
        self.closed = True


class _FakePlaywright:
    def __init__(self, browser):
        self._browser = browser

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    @property
    def chromium(self):
        return self

    def launch(self, headless=True):
        return self._browser


# ---------- render.py 层 helpers ----------

class _Proc:
    """mock subprocess.run 的返回对象"""

    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class _FakeExecutor:
    """记录 render 调用(支持 cookies 3 参)的假执行器"""

    def __init__(self, result=None):
        self.result = result or RenderResult(ok=True, html="<html>r</html>",
                                             final_url="https://example.com/",
                                             status_code=200)
        self.calls = []

    def render(self, url, timeout=None, cookies=None):
        self.calls.append((url, timeout, cookies))
        return self.result


def _ok_stdout(html="<html>rendered</html>", final_url="https://example.com/",
               status=200):
    return json.dumps({"ok": True, "html": html, "final_url": final_url,
                       "status_code": status, "error": ""})


# ---------- R1-R3: RenderExecutor cookies 传输 ----------

class RenderExecutorCookiesTest(unittest.TestCase):

    def test_render_cookies_none_backward_compat(self):
        """R1: cookies=None → cmd 无 --cookies-stdin, input 为 None(等价未传)"""
        proc = _Proc(stdout=_ok_stdout())
        ex = RenderExecutor(python="/v/bin/python", worker="/w/render_worker.py")
        with mock.patch("intel.core.render.subprocess.run", return_value=proc) as m_run:
            result = ex.render("https://example.com")

        self.assertTrue(result.ok)
        cmd = m_run.call_args.args[0]
        self.assertNotIn("--cookies-stdin", cmd)
        self.assertIsNone(m_run.call_args.kwargs.get("input"))  # 等价不传 stdin
        self.assertEqual(cmd, ["/v/bin/python", "/w/render_worker.py",
                               "https://example.com", "--timeout", "30"])

    def test_render_cookies_passed_via_stdin_not_argv(self):
        """R2: cookies 经 stdin JSON 传 worker, argv 中无任何 cookie 值"""
        cookies = [{"name": "__zp_stoken__", "value": "TOP-SECRET-VALUE",
                    "domain": "www.zhipin.com", "path": "/"}]
        proc = _Proc(stdout=_ok_stdout())
        ex = RenderExecutor()
        with mock.patch("intel.core.render.subprocess.run", return_value=proc) as m_run:
            ex.render("https://example.com", cookies=cookies)

        cmd = m_run.call_args.args[0]
        self.assertIn("--cookies-stdin", cmd)
        argv_blob = json.dumps(cmd)
        self.assertNotIn("TOP-SECRET-VALUE", argv_blob)   # argv 无 cookie 值
        self.assertNotIn("__zp_stoken__", argv_blob)
        sent = json.loads(m_run.call_args.kwargs["input"])  # stdin 载荷
        self.assertEqual(sent, {"cookies": cookies})
        self.assertIsInstance(m_run.call_args.kwargs["input"], str)

    def test_render_cookies_empty_list(self):
        """R3: cookies=[] → 视同无 cookie(无标志、input 为 None)"""
        proc = _Proc(stdout=_ok_stdout())
        ex = RenderExecutor()
        with mock.patch("intel.core.render.subprocess.run", return_value=proc) as m_run:
            ex.render("https://example.com", cookies=[])

        cmd = m_run.call_args.args[0]
        self.assertNotIn("--cookies-stdin", cmd)
        self.assertIsNone(m_run.call_args.kwargs.get("input"))


# ---------- R4-R7: RenderAwareSession cookies 语义 ----------

class RenderAwareSessionCookiesTest(unittest.TestCase):

    def test_session_get_forwards_cookies(self):
        """R4: get(url, cookies=X) → executor.render(url, timeout, cookies=X)"""
        executor = _FakeExecutor(RenderResult(ok=True, html="<html>js</html>",
                                              final_url="https://example.com/",
                                              status_code=200))
        inner = mock.Mock(spec=requests.Session)
        sess = RenderAwareSession(inner, executor)
        cookies = [{"name": "a", "value": "1"}]
        r = sess.get("https://example.com", cookies=cookies)

        self.assertIsInstance(r, RenderedResponse)
        self.assertEqual(executor.calls,
                         [("https://example.com", 30.0, cookies)])
        inner.get.assert_not_called()

    def test_session_get_default_cookies(self):
        """R5: 构造默认 cookies → get(url) 自动用默认"""
        cookies = [{"name": "a", "value": "1"}]
        executor = _FakeExecutor()
        inner = mock.Mock(spec=requests.Session)
        sess = RenderAwareSession(inner, executor, cookies=cookies)
        sess.get("https://example.com")

        self.assertEqual(executor.calls[0][2], cookies)

    def test_session_get_cookies_override(self):
        """R6: get(url, cookies=Y) 覆盖构造默认 X"""
        cookies_x = [{"name": "x", "value": "1"}]
        cookies_y = [{"name": "y", "value": "2"}]
        executor = _FakeExecutor()
        inner = mock.Mock(spec=requests.Session)
        sess = RenderAwareSession(inner, executor, cookies=cookies_x)
        sess.get("https://example.com", cookies=cookies_y)

        self.assertEqual(executor.calls[0][2], cookies_y)

    def test_session_fallback_static_injects_requests_cookies(self):
        """R7: 渲染失败降级静态 → inner.get(url, cookies={name:value})"""
        executor = _FakeExecutor(RenderResult(ok=False, error="超时"))
        inner = mock.Mock(spec=requests.Session)
        inner.get.return_value = "static-response"
        sess = RenderAwareSession(inner, executor)
        cookies = [{"name": "a", "value": "1"}, {"name": "b", "value": "2"}]
        r = sess.get("https://example.com", cookies=cookies)

        self.assertEqual(r, "static-response")
        inner.get.assert_called_once_with("https://example.com", timeout=30.0,
                                          cookies={"a": "1", "b": "2"})


# ---------- R8-R11: render_worker playwright 注入 ----------

class RenderWorkerCookiesTest(unittest.TestCase):

    def _browser(self, context):
        return _FakeBrowser(context)

    def test_worker_add_cookies_called(self):
        """R8: _render 走 new_context + add_cookies(cookies), 返回 html"""
        browser = self._browser(_FakeContext(_FakePage()))
        cookies = [{"name": "a", "value": "1", "domain": "www.zhipin.com"}]
        with mock.patch("playwright.sync_api.sync_playwright",
                        return_value=_FakePlaywright(browser)):
            result = render_worker._render("https://x", 30.0, cookies)

        self.assertEqual(browser.context.added, [cookies])
        self.assertTrue(result["ok"])
        self.assertEqual(result["html"], "<html>detail</html>")
        self.assertEqual(result["error"], "")

    def test_worker_no_cookies_no_add(self):
        """R9: cookies=None → add_cookies 不被调, 照常渲染"""
        context = _FakeContext(_FakePage())
        browser = self._browser(context)
        with mock.patch("playwright.sync_api.sync_playwright",
                        return_value=_FakePlaywright(browser)):
            result = render_worker._render("https://x", 30.0, None)

        self.assertEqual(context.added, [])
        self.assertTrue(result["ok"])

    def test_worker_add_cookies_error_generic(self):
        """R10: add_cookies 抛异常 → 继续渲染, error 固定文案(不含 cookie 值)"""

        class _BadContext(_FakeContext):
            def add_cookies(self, cookies):
                raise ValueError("bad domain SECRET-COOKIE-VALUE")

        browser = self._browser(_BadContext(_FakePage()))
        cookies = [{"name": "a", "value": "SECRET-COOKIE-VALUE",
                    "domain": "bad.example.com"}]
        with mock.patch("playwright.sync_api.sync_playwright",
                        return_value=_FakePlaywright(browser)):
            result = render_worker._render("https://x", 30.0, cookies)

        self.assertTrue(result["ok"])  # 注入失败仍继续渲染
        self.assertEqual(result["error"], "cookie 注入失败(域/格式不匹配)")
        self.assertNotIn("SECRET-COOKIE-VALUE", result["error"])  # 不 echo 值

    def test_worker_bad_stdin_ignores_cookies(self):
        """R11: stdin 坏 JSON → 不崩溃, 无 cookie 渲染"""
        browser = self._browser(_FakeContext(_FakePage()))
        with mock.patch("playwright.sync_api.sync_playwright",
                        return_value=_FakePlaywright(browser)), \
             mock.patch("sys.stdin", io.StringIO("{bad json{{{")), \
             contextlib.redirect_stdout(io.StringIO()):
            rc = render_worker.main(["/job_detail/1.html", "--timeout", "10",
                                     "--cookies-stdin"])

        self.assertEqual(rc, 0)
        self.assertEqual(browser.context.added, [])  # 坏 stdin → 无 cookie

    def test_worker_no_flag_never_reads_stdin(self):
        """R11b: 无 --cookies-stdin → 不读 stdin(不阻塞), 无 cookie 渲染"""
        browser = self._browser(_FakeContext(_FakePage()))
        with mock.patch("playwright.sync_api.sync_playwright",
                        return_value=_FakePlaywright(browser)), \
             mock.patch("sys.stdin", io.StringIO('{"cookies": [{"name": "x"}]}')), \
             contextlib.redirect_stdout(io.StringIO()):
            rc = render_worker.main(["/job_detail/1.html"])

        self.assertEqual(rc, 0)
        self.assertEqual(browser.context.added, [])


if __name__ == "__main__":
    unittest.main()
