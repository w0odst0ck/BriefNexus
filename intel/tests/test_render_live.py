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

    def test_render_live_boss_m2a(self):
        """L4: boss_zhipin M2a 真实验证 — 上海+关键词池代表词 → ≥3 卡片
        (含 职位/公司/薪资 至少一项) 或如实反爬拦截 0 条; 产物 joblist.json 落盘"""
        import os
        import tempfile
        from datetime import datetime

        import requests
        from intel.collectors.boss_zhipin import BossZhipinCollector
        from intel.core.base import CST

        out = tempfile.mkdtemp(prefix="boss_live_")
        c = BossZhipinCollector(max_age=7, output_dir=out)
        sess = RenderAwareSession(requests.Session(), RenderExecutor())
        items = c.crawl(sess)
        # 诚实语义: ≥3 卡片, 或反爬拦截如实 0 条(不硬刚)
        self.assertTrue(len(items) >= 3 or len(items) == 0,
                        f"期望 ≥3 卡片或如实 0 条(反爬), 实际 {len(items)}")
        for it in items:
            self.assertTrue(it.url.startswith("http"))
        if items:
            # 至少一项扩展字段非空(职位/公司/薪资)
            self.assertTrue(any(
                (it.raw_data or {}).get("job", {}).get(f)
                for it in items for f in ("job_title", "company", "salary")
            ))
        # 产物落盘
        today = datetime.now(CST).strftime("%Y-%m-%d")
        self.assertTrue(os.path.exists(
            os.path.join(out, today, "joblist.json")), "joblist.json 未落盘")


if __name__ == "__main__":
    unittest.main()
