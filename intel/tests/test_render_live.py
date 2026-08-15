"""intel render live 冒烟 — 真实浏览器渲染, 默认跳过(design §3.2)

门控: 设置环境变量 BN_RENDER_LIVE=1 才执行(unittest skipUnless)。
  L1 真实渲染 https://example.com → ok=True, html 非空
  L2 渲染含 JS 填充 DOM 的页面 → html 含 JS 生成的标记(证明是 JS 渲染而非静态抓取)
  L3 boss_zhipin 端到端 → 走渲染路径, 不崩溃, 返回 List[NewsItem](允许 0 条,
     反爬墙时合法, 诚实语义: 交付"渲染后 HTML"而非破解反爬)
"""
import os
import unittest
from urllib.parse import quote

from intel.core.render import RenderAwareSession, RenderExecutor

LIVE = bool(os.environ.get("BN_RENDER_LIVE"))


@unittest.skipUnless(LIVE, "需要 BN_RENDER_LIVE=1(真实浏览器/网络)")
class RenderLiveTest(unittest.TestCase):

    def test_render_live_real_page(self):
        """L1: 渲染执行器核心能力 — 浏览器能 launch + 返回 HTML"""
        ex = RenderExecutor()
        r = ex.render("https://example.com")
        self.assertTrue(r.ok, r.error)
        self.assertTrue(r.html)
        self.assertIn("<html", r.html.lower())

    def test_render_live_js_page(self):
        """L2: JS 填充 DOM → 渲染后 HTML 含 JS 生成的标记"""
        js = ("<script>document.body.innerHTML="
              "'<p id=js>JS_RENDERED_42</p>'</script>")
        url = "data:text/html," + quote(f"<html><body>{js}</body></html>")
        ex = RenderExecutor()
        r = ex.render(url)
        self.assertTrue(r.ok, r.error)
        self.assertIn("JS_RENDERED_42", r.html)

    def test_render_live_boss_end_to_end(self):
        """L3: boss_zhipin 渲染路径端到端 — 不崩溃, 返回列表(允许 0 条)"""
        import requests
        from intel.collectors.boss_zhipin import BossZhipinCollector
        sess = RenderAwareSession(requests.Session(), RenderExecutor())
        collector = BossZhipinCollector(max_age=7)
        items = collector.crawl(sess)
        self.assertIsInstance(items, list)
        for it in items:
            self.assertTrue(it.title)
            self.assertTrue(it.url.startswith("http"))


if __name__ == "__main__":
    unittest.main()
