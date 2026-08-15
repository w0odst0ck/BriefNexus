"""intel render 能力单测 — 全 stub/mock, 不起真实浏览器、不发真实网络请求

覆盖(design §3.1):
  T1-T7   RenderExecutor 各路径归一化(ok/超时/缺浏览器/worker error JSON/坏 JSON/空 HTML/超时覆盖)
  T8-T10  RenderAwareSession: 渲染成功 / 降级静态 / post 透传
  T11     RenderedResponse duck typing(text/content/encoding/url/ok/headers/json/raise_for_status)
  T12-T13 cli._sess(render=False/True) 返回类型
  T14-T19 cmd_run/cmd_check 逐源 render 标志解析 + 渲染失败不中断整体
  T20     boss_zhipin 采集器接口(正常提取 / 反爬空壳 / 网络失败 → [])
"""
import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from unittest import mock

import requests
from intel import cli
from intel.core.base import BaseCollector, NewsItem
from intel.core.render import (
    RenderAwareSession,
    RenderedResponse,
    RenderExecutor,
    RenderResult,
)

# ---------- helpers ----------

def _ok_stdout(html="<html>rendered</html>", final_url="https://example.com/",
               status=200):
    return json.dumps({"ok": True, "html": html, "final_url": final_url,
                       "status_code": status, "error": ""})


class _Proc:
    """mock subprocess.run 的返回对象"""

    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class _FakeExecutor:
    """假渲染执行器: 记录调用, 返回预设 RenderResult"""

    def __init__(self, result=None):
        self.result = result or RenderResult(ok=True, html="<html>r</html>",
                                             final_url="https://example.com/",
                                             status_code=200)
        self.calls = []

    def render(self, url, timeout=None):
        self.calls.append((url, timeout))
        return self.result


class _FakeSess:
    """cli._sess 的替身: 按 render 标志返回可辨识对象"""

    def __init__(self, render=False, render_timeout=30.0):
        self.render = render
        self.render_timeout = render_timeout

    def __repr__(self):  # 便于断言失败时定位
        return f"_FakeSess(render={self.render})"


class _RecCollector(BaseCollector):
    """记录收到的 sess 的 stub 采集器"""

    def __init__(self, source_name, n_items=0):
        super().__init__(max_age=7)
        self.source_name = source_name
        self.display_name = source_name
        self.n_items = n_items
        self.seen = None

    def crawl(self, sess):
        self.seen = sess
        return [NewsItem(title=f"{self.source_name} {i}",
                         url=f"http://stub/{self.source_name}/{i}")
                for i in range(self.n_items)]


class _BoomCollector(BaseCollector):
    """crawl 抛任意异常的 stub(模拟渲染源彻底失败)"""

    source_name = "stub_boom"
    display_name = "Stub Boom"

    def crawl(self, sess):
        raise RuntimeError("render boom")


class _FakeDedup:
    """cmd_run 的去重替身: filter_new 全放行, 其余 no-op"""

    def filter_new(self, titles):
        return titles

    def mark_seen_batch(self, titles, day):
        pass

    def save(self):
        pass

    def cleanup(self):
        pass


def _run_check(*args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.cmd_check(*args, **kwargs)
    return code, buf.getvalue()


class _FakeResp:
    def __init__(self, text, status=200):
        self.text = text
        self._status = status

    def raise_for_status(self):
        if not (200 <= self._status < 400):
            raise RuntimeError(f"HTTP {self._status}")


class _FakeRespSession:
    """boss_zhipin 正常路径: get 返回预设文本"""

    def __init__(self, text, status=200):
        self._resp = _FakeResp(text, status)

    def get(self, url, **kwargs):
        return self._resp


class _BoomSession:
    """boss_zhipin 网络失败路径: get 抛异常"""

    def get(self, url, **kwargs):
        raise RuntimeError("network down")


# ---------- T1-T7: RenderExecutor ----------

class RenderExecutorTest(unittest.TestCase):

    def test_render_executor_returns_html_on_worker_ok(self):
        """T1: worker ok → RenderResult.ok/html, cmd 参数正确"""
        proc = _Proc(stdout=_ok_stdout(html="<html>hi</html>"))
        ex = RenderExecutor(python="/v/bin/python", worker="/w/render_worker.py")
        with mock.patch("intel.core.render.subprocess.run", return_value=proc) as m_run:
            result = ex.render("https://example.com")

        self.assertTrue(result.ok)
        self.assertEqual(result.html, "<html>hi</html>")
        self.assertEqual(result.final_url, "https://example.com/")
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.failed)
        cmd = m_run.call_args.args[0]
        self.assertEqual(cmd, ["/v/bin/python", "/w/render_worker.py",
                               "https://example.com", "--timeout", "30"])

    def test_render_executor_timeout_degrades(self):
        """T2: subprocess 超时 → ok=False, error 含"超时", 不抛异常"""
        ex = RenderExecutor()
        with mock.patch("intel.core.render.subprocess.run",
                        side_effect=subprocess.TimeoutExpired(cmd=["x"], timeout=1)):
            result = ex.render("https://example.com")
        self.assertFalse(result.ok)
        self.assertIn("超时", result.error)
        self.assertTrue(result.failed)

    def test_render_executor_browser_missing(self):
        """T3: venv python/worker 不存在(FileNotFoundError) → ok=False"""
        ex = RenderExecutor()
        with mock.patch("intel.core.render.subprocess.run",
                        side_effect=FileNotFoundError()):
            result = ex.render("https://example.com")
        self.assertFalse(result.ok)
        self.assertIn("不存在", result.error)

    def test_render_executor_worker_error_json(self):
        """T4: worker 输出 {ok:false, error} → error 透传"""
        proc = _Proc(stdout=json.dumps({"ok": False, "html": "", "final_url": "",
                                        "status_code": 0, "error": "chromium 未安装"}))
        ex = RenderExecutor()
        with mock.patch("intel.core.render.subprocess.run", return_value=proc):
            result = ex.render("https://example.com")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "chromium 未安装")

    def test_render_executor_bad_json(self):
        """T5: worker stdout 非法 JSON → ok=False, 不抛异常"""
        proc = _Proc(stdout="not-json{{{")
        ex = RenderExecutor()
        with mock.patch("intel.core.render.subprocess.run", return_value=proc):
            result = ex.render("https://example.com")
        self.assertFalse(result.ok)
        self.assertIn("非法 JSON", result.error)

    def test_render_executor_empty_html(self):
        """T6: ok=true 但 html 为空 → failed=True"""
        proc = _Proc(stdout=_ok_stdout(html=""))
        ex = RenderExecutor()
        with mock.patch("intel.core.render.subprocess.run", return_value=proc):
            result = ex.render("https://example.com")
        self.assertTrue(result.ok)  # worker 声称成功
        self.assertTrue(result.failed)  # 但 html 为空视为失败

    def test_render_executor_timeout_override(self):
        """T7: render(url, timeout=10) → cmd 含 --timeout 10, 外层超时 10+GRACE"""
        proc = _Proc(stdout=_ok_stdout())
        ex = RenderExecutor()
        with mock.patch("intel.core.render.subprocess.run", return_value=proc) as m_run:
            ex.render("https://example.com", timeout=10)
        cmd = m_run.call_args.args[0]
        self.assertEqual(cmd[-1], "10")
        self.assertEqual(m_run.call_args.kwargs["timeout"], 10 + ex.grace)

    def test_render_executor_returncode_nonzero_parses_error(self):
        """T4b: returncode!=0 且 stdout 是 JSON → error 取 stdout"""
        proc = _Proc(stdout=json.dumps({"error": "launch failed"}),
                     stderr="raw stderr", returncode=1)
        ex = RenderExecutor()
        with mock.patch("intel.core.render.subprocess.run", return_value=proc):
            result = ex.render("https://example.com")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "launch failed")


# ---------- T8-T10: RenderAwareSession ----------

class RenderAwareSessionTest(unittest.TestCase):

    def test_render_aware_session_get_renders(self):
        """T8: 渲染成功 → RenderedResponse, .text==html, raise_for_status 不抛"""
        executor = _FakeExecutor(RenderResult(ok=True, html="<html>js</html>",
                                              final_url="https://example.com/x",
                                              status_code=200))
        inner = mock.Mock(spec=requests.Session)
        sess = RenderAwareSession(inner, executor)
        r = sess.get("https://example.com")

        self.assertIsInstance(r, RenderedResponse)
        self.assertEqual(r.text, "<html>js</html>")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.url, "https://example.com/x")
        r.raise_for_status()  # 不抛
        self.assertEqual(executor.calls, [("https://example.com", 30.0)])
        inner.get.assert_not_called()

    def test_render_aware_session_get_falls_back_to_static(self):
        """T9: 渲染失败 → 降级 inner session.get, 返回其响应"""
        executor = _FakeExecutor(RenderResult(ok=False, error="超时"))
        inner = mock.Mock(spec=requests.Session)
        inner.get.return_value = "static-response"
        sess = RenderAwareSession(inner, executor)
        r = sess.get("https://example.com", timeout=12)

        self.assertEqual(r, "static-response")
        inner.get.assert_called_once_with("https://example.com", timeout=12)

    def test_render_aware_session_post_delegates(self):
        """T10: post 永不渲染, 直接透传内层 session"""
        executor = _FakeExecutor()
        inner = mock.Mock(spec=requests.Session)
        inner.post.return_value = "post-response"
        sess = RenderAwareSession(inner, executor)
        r = sess.post("https://example.com/api", data={"x": 1})

        self.assertEqual(r, "post-response")
        inner.post.assert_called_once_with("https://example.com/api", data={"x": 1})
        self.assertEqual(executor.calls, [])  # executor 从未被调用

    def test_render_aware_session_attr_passthrough(self):
        """T8b: headers/cookies/close 等经 __getattr__ 透传"""
        inner = mock.Mock(spec=requests.Session)
        sess = RenderAwareSession(inner, _FakeExecutor())
        sess.close()
        inner.close.assert_called_once_with()


# ---------- T11: RenderedResponse duck typing ----------

class RenderedResponseTest(unittest.TestCase):

    def test_rendered_response_duck_type(self):
        """T11: text/content/encoding(可赋值)/url/ok/headers/json() 契约"""
        r = RenderedResponse(RenderResult(ok=True, html="<html>é</html>",
                                          final_url="https://example.com/",
                                          status_code=200))
        self.assertEqual(r.text, "<html>é</html>")
        self.assertEqual(r.content, "<html>é</html>".encode())
        self.assertEqual(r.encoding, "utf-8")
        r.encoding = "gbk"  # eastmoney 风格: 可赋值
        self.assertEqual(r.content, "<html>é</html>".encode("gbk"))
        self.assertEqual(r.url, "https://example.com/")
        self.assertTrue(r.ok)
        self.assertEqual(r.headers, {})

        rj = RenderedResponse(RenderResult(ok=True, html='[1, 2, 3]'))
        self.assertEqual(rj.json(), [1, 2, 3])

        r404 = RenderedResponse(RenderResult(ok=True, html="x",
                                             final_url="https://e/",
                                             status_code=404))
        self.assertFalse(r404.ok)
        with self.assertRaises(requests.HTTPError):
            r404.raise_for_status()


# ---------- T12-T19: cli 集成 ----------

class CliRenderFlagTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name

    def test_plain_session_when_render_false(self):
        """T12: _sess(render=False) → 裸 requests.Session(非包装)"""
        sess = cli._sess()
        self.assertIsInstance(sess, requests.Session)
        self.assertNotIsInstance(sess, RenderAwareSession)

    def test_render_session_when_render_true(self):
        """T13: _sess(render=True) → RenderAwareSession"""
        sess = cli._sess(render=True)
        self.assertIsInstance(sess, RenderAwareSession)

    def _patch_run_env(self, instances, config):
        """cmd_run 公共 patch: 注册表 + _sess + 去重/分类/报告 + sleep"""
        return (
            mock.patch("intel.cli.instantiate_collectors", return_value=instances),
            mock.patch("intel.cli._sess", side_effect=_FakeSess),
            mock.patch("intel.cli.DedupStore", return_value=_FakeDedup()),
            mock.patch("intel.cli.classify"),
            mock.patch("intel.cli.build_report", return_value="{}"),
            mock.patch("intel.cli.time.sleep"),
        )

    def test_cmd_run_render_source_gets_render_session(self):
        """T14: render 源收到 render=True 的 sess, 非 render 源收到 plain sess"""
        render_c = _RecCollector("stub_render")
        plain_c = _RecCollector("stub_plain")
        config = {"sources": {"stub_render": {"render": True}, "stub_plain": {}}}
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch("intel.cli._load_config", return_value=config))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            for p in self._patch_run_env([render_c, plain_c], config):
                stack.enter_context(p)
            cli.cmd_run(output_dir=self.tmp)

        self.assertEqual(render_c.seen.render, True)
        self.assertEqual(plain_c.seen.render, False)

    def test_cmd_check_render_flag_lookup(self):
        """T15: cmd_check 按 source_name 解析 render 标志"""
        render_c = _RecCollector("stub_render")
        classes = {"stub_render": type(render_c)}
        config = {"sources": {"stub_render": {"render": True}}}
        with mock.patch("intel.cli._load_config", return_value=config), \
             mock.patch("intel.cli.get_collector_classes", return_value=classes), \
             mock.patch("intel.cli.instantiate_collectors", return_value=[render_c]), \
             mock.patch("intel.cli._sess", side_effect=_FakeSess):
            code, _ = _run_check(timeout=5, interval=0, output_dir=self.tmp)

        self.assertEqual(code, 0)
        self.assertEqual(render_c.seen.render, True)

    def test_default_no_render_flag(self):
        """T16: 源无 render 键 → 不渲染(plain sess)"""
        c = _RecCollector("stub_ok")
        classes = {"stub_ok": type(c)}
        config = {"sources": {"stub_ok": {"enabled": True}}}
        with mock.patch("intel.cli._load_config", return_value=config), \
             mock.patch("intel.cli.get_collector_classes", return_value=classes), \
             mock.patch("intel.cli.instantiate_collectors", return_value=[c]), \
             mock.patch("intel.cli._sess", side_effect=_FakeSess):
            code, _ = _run_check(timeout=5, interval=0, output_dir=self.tmp)

        self.assertEqual(code, 0)
        self.assertEqual(c.seen.render, False)

    def test_config_render_false_explicit(self):
        """T17: render:false → plain sess"""
        c = _RecCollector("stub_ok")
        classes = {"stub_ok": type(c)}
        config = {"sources": {"stub_ok": {"render": False}}}
        with mock.patch("intel.cli._load_config", return_value=config), \
             mock.patch("intel.cli.get_collector_classes", return_value=classes), \
             mock.patch("intel.cli.instantiate_collectors", return_value=[c]), \
             mock.patch("intel.cli._sess", side_effect=_FakeSess):
            code, _ = _run_check(timeout=5, interval=0, output_dir=self.tmp)

        self.assertEqual(code, 0)
        self.assertEqual(c.seen.render, False)

    def test_config_render_flag_nonbool(self):
        """T18: render:"true"/1/"false" → 一律 fail-closed 不渲染"""
        for bad in ("true", 1, "false"):
            c = _RecCollector("stub_ok")
            classes = {"stub_ok": type(c)}
            config = {"sources": {"stub_ok": {"render": bad}}}
            with mock.patch("intel.cli._load_config", return_value=config), \
                 mock.patch("intel.cli.get_collector_classes", return_value=classes), \
                 mock.patch("intel.cli.instantiate_collectors", return_value=[c]), \
                 mock.patch("intel.cli._sess", side_effect=_FakeSess):
                code, _ = _run_check(timeout=5, interval=0, output_dir=self.tmp)

            self.assertEqual(code, 0, f"render={bad!r} 不应导致失败")
            self.assertEqual(c.seen.render, False, f"render={bad!r} 应 fail-closed")

    def test_cmd_run_single_source_render_failure_does_not_abort(self):
        """T19: 渲染失败源 + 正常源并存, 正常源仍产出, 整体不中断"""
        boom = _BoomCollector()
        ok_c = _RecCollector("stub_ok", n_items=2)
        config = {"sources": {"stub_boom": {"render": True}, "stub_ok": {}}}
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch("intel.cli._load_config", return_value=config))
            stack.enter_context(mock.patch("intel.cli.instantiate_collectors",
                                           return_value=[boom, ok_c]))
            stack.enter_context(mock.patch("intel.cli._sess", side_effect=_FakeSess))
            stack.enter_context(mock.patch("intel.cli.DedupStore",
                                           return_value=_FakeDedup()))
            stack.enter_context(mock.patch("intel.cli.classify"))
            m_br = stack.enter_context(
                mock.patch("intel.cli.build_report", return_value="{}")
            )
            stack.enter_context(mock.patch("intel.cli.time.sleep"))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            cli.cmd_run(output_dir=self.tmp)

        # 正常源 2 条仍进入最终报告; 渲染失败源被 try/except 兜住
        items = m_br.call_args.args[0]
        self.assertEqual([it.title for it in items], ["stub_ok 0", "stub_ok 1"])


# ---------- T20: boss_zhipin 采集器接口 ----------

class BossZhipinCollectorTest(unittest.TestCase):

    def test_boss_zhipin_parses_cards(self):
        """T20a: 渲染 HTML 含职位卡片 → NewsItem 列表"""
        from intel.collectors.boss_zhipin import BossZhipinCollector
        html = ('<div class="job-card-wrapper">'
                '<a class="job-card-left" href="/job_detail/123.html">'
                '<span class="job-name">自动驾驶算法工程师</span></a></div>'
                '<div class="job-card-wrapper">'
                '<a class="job-card-left" href="/job_detail/456.html">'
                '<span class="job-name">感知融合工程师</span></a></div>')
        c = BossZhipinCollector(max_age=7)
        items = c.crawl(_FakeRespSession(html))

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].title, "自动驾驶算法工程师")
        self.assertTrue(items[0].url.startswith("https://www.zhipin.com"))
        self.assertEqual(items[1].title, "感知融合工程师")

    def test_boss_zhipin_anti_bot_returns_empty(self):
        """T20b: 反爬空壳/无卡片 → []"""
        from intel.collectors.boss_zhipin import BossZhipinCollector
        c = BossZhipinCollector(max_age=7)
        self.assertEqual(c.crawl(_FakeRespSession("<html>验证码</html>")), [])

    def test_boss_zhipin_network_failure_returns_empty(self):
        """T20c: sess.get 抛异常 → [] 不抛"""
        from intel.collectors.boss_zhipin import BossZhipinCollector
        c = BossZhipinCollector(max_age=7)
        self.assertEqual(c.crawl(_BoomSession()), [])


if __name__ == "__main__":
    unittest.main()
