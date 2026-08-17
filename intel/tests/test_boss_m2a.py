"""boss_zhipin M2a 升级单测 — 全 stub/mock, 无网络、无真实浏览器

覆盖(design §2.1 / 错误处理表 E1-E14):
  T1  参数化默认值(上海/pages/max_items/delay 硬下限/output_dir 推导)
  T2  单查询覆盖(忽略池, URL 参数正确)
  T3  关键词池派生(代表词映射 / target_roles / 首簇首词 / 派不出跳过 / order 升序)
  T4  池文件不存在 → 降级内置快照, 不抛
  T5  池坏 JSON → 降级内置快照 + warning
  T6  完整卡片字段全解析
  T7  部分字段缺失 → 置 "", 保留 title+url
  T8  旧版卡片(job-card-left + job-name)→ title+url, 其余 ""
  T9  薪资乱码 → 置 "", 不虚构
  T10 跨关键词去重(link hash), 保留首 query 溯源
  T11 频率控制(sleep ≥5.0, delay=1 钳到 5.0)
  T12 单页失败不中断, 返回部分结果
  T13 反爬空壳 → [] 不抛, 产物 jobs:[]
  T14 max_items 早停(后续无 get)
  T15 分页(pages=2 → 第 2 页 URL 含 page=2; 空页停)
  T16 产物结构完整 + link_hash 无重复 + 重跑幂等覆盖
  T17 写盘失败 → 仍返回 items, 不抛
  T18 crawl 顶层兜底(get 全抛 → [] 不抛)
"""
import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock

from intel.collectors.boss_zhipin import (
    DEFAULT_REPRESENTATIVE_QUERIES,
    BossZhipinCollector,
    _resolve_queries,
)
from intel.core.base import CST

# ---------- stub helpers(复用既有 FakeSession/FakeResponse 惯例) ----------


class FakeResponse:
    """requests.Response 替身: 只暴露 crawl 依赖的 text / raise_for_status"""

    def __init__(self, text="", status=200):
        self._text = text
        self._status = status

    @property
    def text(self):
        return self._text

    def raise_for_status(self):
        if not (200 <= self._status < 400):
            raise RuntimeError(f"HTTP {self._status}")


class FakeSession:
    """记录 get 调用的内存替身; 响应耗尽/异常按序弹出"""

    def __init__(self, responses=None, factory=None):
        self._responses = list(responses or [])
        self._factory = factory
        self.calls = []  # [(url, kwargs), ...]

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        idx = len(self.calls) - 1
        if self._factory is not None:
            return self._factory(idx, url, kwargs)
        if idx < len(self._responses):
            resp = self._responses[idx]
            if isinstance(resp, BaseException):
                raise resp
            return resp
        raise AssertionError(f"FakeSession 响应耗尽: 第 {idx} 次 get {url}")


# ---------- HTML builders ----------

def _card(pid, title="岗位", company="某某科技", salary="30-50K",
          exp="3-5年", edu="本科", area="上海·浦东新区"):
    """单张完整职位卡片 HTML(含全部新字段)"""
    parts = [
        (f'<div class="job-card-wrapper"><a class="job-card-left" '
         f'href="/job_detail/{pid}.html"><span class="job-name">{title}</span></a>'),
    ]
    if any([company, salary, exp, edu]):
        parts.append('<ul class="job-info">')
        if salary:
            parts.append(f'<span class="salary">{salary}</span>')
        if exp:
            parts.append(f"<span>{exp}</span>")
        if edu:
            parts.append(f"<span>{edu}</span>")
        parts.append("</ul>")
    if area:
        parts.append(f'<span class="job-area">{area}</span>')
    if company:
        parts.append(f'<div class="company-info"><h3 class="name">{company}</h3></div>')
    parts.append("</div>")
    return "".join(parts)


def _page(*cards):
    return "<html><body>" + "".join(cards) + "</body></html>"


def _today():
    return datetime.now(CST).strftime("%Y-%m-%d")


def _read_joblist(tmp):
    with open(os.path.join(tmp, _today(), "joblist.json"), encoding="utf-8") as f:
        return json.load(f)


# ---------- 参数化 ----------

class ParametrizeTest(unittest.TestCase):

    def test_parametrize_defaults(self):
        """T1: 默认 city=上海/pages=1/max_items=30/delay 硬下限 5.0/output_dir 推导"""
        with mock.patch.dict(os.environ, {}, clear=True):
            c = BossZhipinCollector(max_age=7)
        self.assertEqual(c.city, "101020100")
        self.assertEqual(c.pages, 1)
        self.assertEqual(c.max_items, 30)
        self.assertEqual(c.delay, 5.0)
        self.assertEqual(c.jitter, 2.0)
        self.assertTrue(c.output_dir.endswith(
            os.path.join("intel", "data", "boss")))

    def test_single_query_override(self):
        """T2: query="RAG" → 仅 1 请求、URL 含 query=RAG&city=上海, 忽略池"""
        with tempfile.TemporaryDirectory() as tmp:
            c = BossZhipinCollector(query="RAG", output_dir=tmp)
            sess = FakeSession([FakeResponse(_page(_card("1")))])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"):
                items = c.crawl(sess)
        self.assertEqual(len(items), 1)
        self.assertEqual(len(sess.calls), 1)
        url = sess.calls[0][0]
        self.assertIn("query=RAG", url)
        self.assertIn("city=101020100", url)

    def test_env_override_precedence(self):
        """T2b: 环境变量 > 代码默认, 构造 kwarg > 环境变量"""
        with mock.patch.dict(os.environ, {"BN_BOSS_CITY": "100010000",
                                          "BN_BOSS_PAGES": "2",
                                          "BN_BOSS_MAX_ITEMS": "5"}):
            c = BossZhipinCollector()
            self.assertEqual(c.city, "100010000")
            self.assertEqual(c.pages, 2)
            self.assertEqual(c.max_items, 5)
            # kwarg 优先于环境变量
            c2 = BossZhipinCollector(city="999999")
            self.assertEqual(c2.city, "999999")


# ---------- 关键词池 ----------

class KeywordPoolTest(unittest.TestCase):

    def test_keyword_pool_derives_queries(self):
        """T3: 代表词映射 → target_roles[0] 派生 → 首簇首词派生 → 派不出跳过, order 升序"""
        pool = [
            {"id": "ai-app-llm", "name": "已有方向", "order": 3,
             "target_roles": ["角色甲"], "clusters": {}},
            {"id": "dir-a", "name": "新增A", "order": 1,
             "target_roles": ["角色A"], "clusters": {}},
            {"id": "dir-b", "name": "新增B", "order": 2,
             "target_roles": [], "clusters": {"子簇": ["首词B"]}},
            {"id": "dir-empty", "name": "空方向", "order": 4,
             "target_roles": [], "clusters": {}},
        ]
        pool = sorted(pool, key=lambda d: d.get("order", 0))  # _load_keyword_pool 已按 order 排序
        qs = _resolve_queries(pool, None, None, DEFAULT_REPRESENTATIVE_QUERIES)
        # ai-app-llm 命中代表词映射 → 用快照词
        by_id = {q["direction_id"]: q for q in qs}
        self.assertEqual(by_id["ai-app-llm"]["terms"],
                         DEFAULT_REPRESENTATIVE_QUERIES["ai-app-llm"])
        # 新增方向走派生
        self.assertEqual(by_id["dir-a"]["terms"], ["角色A"])
        self.assertEqual(by_id["dir-b"]["terms"], ["首词B"])
        # 派不出 → 跳过
        self.assertNotIn("dir-empty", by_id)
        # order 升序
        self.assertEqual([q["direction_id"] for q in qs],
                         ["dir-a", "dir-b", "ai-app-llm"])
        # 元数据
        self.assertEqual(qs[0]["direction_name"], "新增A")

    def test_keyword_pool_missing_falls_back(self):
        """T4: 池文件不存在 → 降级内置快照, 仍产出(不抛)"""
        with tempfile.TemporaryDirectory() as tmp:
            c = BossZhipinCollector(keywords_path="/nonexistent/job_keywords.json",
                                    output_dir=tmp)
            sess = FakeSession([FakeResponse(_page(_card("1")))])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"), \
                 self.assertLogs("intel.boss_zhipin", level="WARNING"):
                items = c.crawl(sess)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "岗位")

    def test_keyword_pool_invalid_json_falls_back(self):
        """T5: 坏 JSON → 降级快照 + warning, 采集照常"""
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{bad json{{{")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                c = BossZhipinCollector(keywords_path=path, output_dir=tmp)
                sess = FakeSession([FakeResponse(_page(_card("1")))])
                with mock.patch("intel.collectors.boss_zhipin.time.sleep"), \
                     self.assertLogs("intel.boss_zhipin", level="WARNING"):
                    items = c.crawl(sess)
        finally:
            os.unlink(path)
        self.assertEqual(len(items), 1)

    def test_keyword_pool_queries_flat_order(self):
        """T3b: 真实 5 方向池(代表词) → 8 个词, 保序去重, 附 direction 元数据"""
        pool = [
            {"id": k, "name": f"方向{i}", "order": i,
             "target_roles": ["角色"], "clusters": {}}
            for i, k in enumerate(DEFAULT_REPRESENTATIVE_QUERIES, start=1)
        ]
        qs = _resolve_queries(pool, None, None, DEFAULT_REPRESENTATIVE_QUERIES)
        total = sum(len(q["terms"]) for q in qs)
        self.assertEqual(total, 8)  # 2+2+1+1+2
        for q in qs:
            self.assertIn(q["direction_id"], DEFAULT_REPRESENTATIVE_QUERIES)
            self.assertTrue(q["direction_name"])


# ---------- 字段解析 ----------

class FieldParseTest(unittest.TestCase):

    def test_field_parse_full(self):
        """T6: 完整卡片 → title/company/salary/experience/education/area/url 全解析"""
        html = _page(_card("123", title="大模型算法工程师", company="某某科技",
                           salary="30-50K", exp="3-5年", edu="本科",
                           area="上海·浦东新区"))
        with tempfile.TemporaryDirectory() as tmp:
            c = BossZhipinCollector(query="大模型", output_dir=tmp)
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"):
                items = c.crawl(FakeSession([FakeResponse(html)]))
        rec = items[0].raw_data["job"]
        self.assertEqual(rec["job_title"], "大模型算法工程师")
        self.assertEqual(rec["company"], "某某科技")
        self.assertEqual(rec["salary"], "30-50K")
        self.assertEqual(rec["experience"], "3-5年")
        self.assertEqual(rec["education"], "本科")
        self.assertEqual(rec["area"], "上海·浦东新区")
        self.assertEqual(rec["url"], "https://www.zhipin.com/job_detail/123.html")
        # NewsItem 管道字段
        self.assertEqual(items[0].title, "大模型算法工程师")
        self.assertIn("某某科技", items[0].summary)
        self.assertIn("30-50K", items[0].summary)

    def test_field_parse_partial_fallback(self):
        """T7: 缺 company/salary 的卡片 → 该字段 '', title+url 保留"""
        html = _page('<div class="job-card-wrapper"><a class="job-card-left" '
                     'href="/job_detail/1.html"><span class="job-name">岗位X</span>'
                     '</a></div>')
        with tempfile.TemporaryDirectory() as tmp:
            c = BossZhipinCollector(query="x", output_dir=tmp)
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"):
                items = c.crawl(FakeSession([FakeResponse(html)]))
        rec = items[0].raw_data["job"]
        self.assertEqual(rec["job_title"], "岗位X")
        self.assertEqual(rec["url"], "https://www.zhipin.com/job_detail/1.html")
        self.assertEqual(rec["company"], "")
        self.assertEqual(rec["salary"], "")
        self.assertEqual(rec["experience"], "")
        self.assertEqual(rec["education"], "")

    def test_field_parse_title_only_legacy(self):
        """T8: 旧版卡片(job-card-wrapper→job-card-left→job-name)→ title+url, 其余 ''"""
        html = _page('<div class="job-card-wrapper">'
                     '<a class="job-card-left" href="/job_detail/456.html">'
                     '<span class="job-name">感知融合工程师</span></a></div>')
        with tempfile.TemporaryDirectory() as tmp:
            c = BossZhipinCollector(query="x", output_dir=tmp)
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"):
                items = c.crawl(FakeSession([FakeResponse(html)]))
        self.assertEqual(len(items), 1)
        rec = items[0].raw_data["job"]
        self.assertEqual(rec["job_title"], "感知融合工程师")
        self.assertTrue(rec["url"].startswith("https://www.zhipin.com"))
        self.assertEqual(rec["company"], "")
        self.assertEqual(rec["salary"], "")

    def test_field_parse_card_without_title_skipped(self):
        """T8b: 职位名与链接均缺失的碎片 → 跳过该卡片(L3)"""
        html = _page('<div class="job-card-wrapper"><span>无链接无标题碎片</span></div>')
        with tempfile.TemporaryDirectory() as tmp:
            c = BossZhipinCollector(query="x", output_dir=tmp)
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"):
                items = c.crawl(FakeSession([FakeResponse(html)]))
        self.assertEqual(items, [])

    def test_field_parse_no_fabrication(self):
        """T9: 薪资乱码(字体反爬)→ '' 置空, 绝不回填; 正常薪资保留"""
        html = _page(
            _card("1", title="岗位A", salary="呔嘁䶮氼"),
            _card("2", title="岗位B", salary="面议"),
            _card("3", title="岗位C", salary="20K"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            c = BossZhipinCollector(query="x", output_dir=tmp)
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"), \
                 self.assertLogs("intel.boss_zhipin", level="WARNING"):
                items = c.crawl(FakeSession([FakeResponse(html)]))
        recs = {it.title: it.raw_data["job"] for it in items}
        self.assertEqual(recs["岗位A"]["salary"], "")   # 乱码 → 置空
        self.assertEqual(recs["岗位B"]["salary"], "面议")  # 面议 → 保留
        self.assertEqual(recs["岗位C"]["salary"], "20K")   # 数字 → 保留


# ---------- 去重 / 频率 / 分页 ----------

class CrawlBehaviorTest(unittest.TestCase):

    def test_dedup_cross_keyword(self):
        """T10: 同 job_detail 链接跨 query 命中 → 仅 1 条, 保留首 query 溯源"""
        html = _page(_card("9", title="同岗位"))
        with tempfile.TemporaryDirectory() as tmp:
            c = BossZhipinCollector(queries=["第一词", "第二词"], output_dir=tmp)
            sess = FakeSession([FakeResponse(html), FakeResponse(html)])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"):
                items = c.crawl(sess)
        self.assertEqual(len(items), 1)
        rec = items[0].raw_data["job"]
        self.assertEqual(rec["query"], "第一词")  # 保留首次命中溯源
        self.assertEqual(len(sess.calls), 2)       # 两词都请求过(去重在解析后)

    def test_frequency_control(self):
        """T11: 2 请求 → sleep 恰好 1 次且参数 ≥5.0; delay=1 钳到 5.0"""
        html = _page(_card("1"))
        with tempfile.TemporaryDirectory() as tmp:
            c = BossZhipinCollector(queries=["a", "b"], delay=1.0, jitter=0.0,
                                    output_dir=tmp)
            sess = FakeSession([FakeResponse(html), FakeResponse(html)])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep") as m_sleep, \
                 mock.patch("intel.collectors.boss_zhipin.random.uniform",
                            return_value=0.0):
                c.crawl(sess)
        self.assertEqual(m_sleep.call_count, 1)
        self.assertGreaterEqual(m_sleep.call_args.args[0], 5.0)
        # delay 硬下限也写入产物 params
        with tempfile.TemporaryDirectory() as tmp2:
            c2 = BossZhipinCollector(query="x", delay=0.5, output_dir=tmp2)
            self.assertEqual(c2.delay, 5.0)

    def test_single_page_failure_not_abort(self):
        """T12: 第 1 查询 get 抛异常 → 第 2 查询仍采集, 返回部分结果"""
        html = _page(_card("2", title="幸存岗位"))
        with tempfile.TemporaryDirectory() as tmp:
            c = BossZhipinCollector(queries=["坏词", "好词"], output_dir=tmp)
            sess = FakeSession([RuntimeError("network down"), FakeResponse(html)])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"):
                items = c.crawl(sess)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "幸存岗位")

    def test_anti_bot_returns_empty(self):
        """T13: 无 job-card-wrapper 页面 → [] 不抛, 产物 jobs:[] 结构完整"""
        with tempfile.TemporaryDirectory() as tmp:
            c = BossZhipinCollector(query="x", output_dir=tmp)
            sess = FakeSession([FakeResponse("<html>验证码, 请完成安全验证</html>")])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"), \
                 self.assertLogs("intel.boss_zhipin", level="WARNING"):
                items = c.crawl(sess)
            self.assertEqual(items, [])
            data = _read_joblist(tmp)
            self.assertEqual(data["jobs"], [])
            self.assertEqual(data["stats"]["blocked_queries"], 1)
            self.assertEqual(data["stats"]["requests"], 0)

    def test_max_items_early_stop(self):
        """T14: max_items=2 + 每页 2 卡 × 2 词 → 恰 2 条, 第 2 词不再发请求"""
        html = _page(_card("1"), _card("2"))
        with tempfile.TemporaryDirectory() as tmp:
            c = BossZhipinCollector(queries=["a", "b"], max_items=2, output_dir=tmp)
            sess = FakeSession([FakeResponse(html), FakeResponse(html)])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"):
                items = c.crawl(sess)
        self.assertEqual(len(items), 2)
        self.assertEqual(len(sess.calls), 1)  # 首个请求已满 2 条 → 早停

    def test_pagination(self):
        """T15: pages=2 → 2 次 get, 第 2 页 URL 含 page=2; 第 2 页空 → 停"""
        html = _page(_card("1"))
        with tempfile.TemporaryDirectory() as tmp:
            c = BossZhipinCollector(query="x", pages=2, output_dir=tmp)
            sess = FakeSession([FakeResponse(html),
                                FakeResponse("<html>empty</html>")])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"):
                items = c.crawl(sess)
        self.assertEqual(len(sess.calls), 2)
        self.assertIn("page=2", sess.calls[1][0])
        self.assertEqual(len(items), 1)  # 第 2 页空 → 停, 不丢第 1 页结果


# ---------- 产物 / 容错 ----------

class ArtifactTest(unittest.TestCase):

    def test_artifact_structure_and_idempotent(self):
        """T16: joblist.json 结构齐全、link_hash 无重复; 重跑覆盖同路径"""
        html = _page(_card("1", title="A"), _card("2", title="B"))
        with tempfile.TemporaryDirectory() as tmp:
            c = BossZhipinCollector(query="x", output_dir=tmp)
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"):
                c.crawl(FakeSession([FakeResponse(html)]))
            path = os.path.join(tmp, _today(), "joblist.json")
            self.assertTrue(os.path.exists(path))
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            for key in ("schema_version", "source", "collected_at", "city",
                        "city_name", "params", "queries", "stats", "jobs"):
                self.assertIn(key, data)
            self.assertEqual(data["schema_version"], "1.0")
            self.assertEqual(data["city"], "101020100")
            self.assertEqual(data["city_name"], "上海")
            self.assertEqual(data["source"], "boss_zhipin")
            hashes = [j["link_hash"] for j in data["jobs"]]
            self.assertEqual(len(hashes), len(set(hashes)))
            self.assertEqual(len(data["jobs"]), 2)
            # 幂等: 同日重跑 → 同路径覆盖, jobs 不翻倍
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"):
                c.crawl(FakeSession([FakeResponse(html)]))
            with open(path, encoding="utf-8") as f:
                data2 = json.load(f)
            self.assertEqual(len(data2["jobs"]), 2)
            self.assertEqual(data2["stats"]["unique_jobs"], 2)

    def test_artifact_write_failure_soft(self):
        """T17: os.replace 抛 OSError → 仍返回 items、不抛"""
        html = _page(_card("1"))
        with tempfile.TemporaryDirectory() as tmp:
            c = BossZhipinCollector(query="x", output_dir=tmp)
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"), \
                 mock.patch("intel.collectors.boss_zhipin.os.replace",
                            side_effect=OSError("readonly fs")), \
                 self.assertLogs("intel.boss_zhipin", level="WARNING"):
                items = c.crawl(FakeSession([FakeResponse(html)]))
        self.assertEqual(len(items), 1)

    def test_crawl_boom_returns_partial(self):
        """T18: get 全抛 → 返回 [] 不抛, 产物 jobs:[] 合法"""
        with tempfile.TemporaryDirectory() as tmp:
            c = BossZhipinCollector(query="x", output_dir=tmp)
            sess = FakeSession([RuntimeError("boom")])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"):
                items = c.crawl(sess)
            self.assertEqual(items, [])
            data = _read_joblist(tmp)
            self.assertEqual(data["jobs"], [])
            self.assertEqual(data["stats"]["failed_requests"], 1)


if __name__ == "__main__":
    unittest.main()
