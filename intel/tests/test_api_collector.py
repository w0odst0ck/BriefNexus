"""APICollector 基类单测 — T1-T22，全 stub（FakeSession/FakeResponse），无网络

覆盖（design apicollector-base §2）：
  T1-T6   基础映射/容错（超时/HTTP/JSON/结构异常）
  T7-T9   auth（api_key 注入 / basic / fail-closed）
  T10-T13 分页（offset/page/cursor + limit 早停）
  T14-T17 记录级过滤（缺字段/过旧/去重/raw_data）
  T18     transform（truncate + template 中间变量）
  T19-T21 queries 循环 / POST / 后续页失败部分结果
  T22     arxiv _extract_records Atom XML 归一化 + 全链路等价
"""
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import requests
from intel.collectors.arxiv_perception import ArxivPerceptionCollector
from intel.core.api import APICollector
from intel.core.base import CST

# ---------- helpers ----------


class FakeResponse:
    """requests.Response 替身：可预设 JSON payload / 文本 / 状态 / json 异常"""

    def __init__(self, payload=None, text="", status=200, json_error=None):
        self._payload = payload
        self._text = text
        self._status = status
        self._json_error = json_error
        self.url = ""

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        if self._payload is not None:
            return self._payload
        return json.loads(self._text)

    def raise_for_status(self):
        if not (200 <= self._status < 400):
            raise requests.HTTPError(f"{self._status} Server Error", response=self)

    @property
    def text(self):
        return self._text


class FakeSession:
    """记录 get/post 调用的内存替身

    responses: 按调用顺序弹出的响应；元素为异常则抛出。
    factory(idx, method, url, kwargs): 可选工厂函数，按调用序号生成响应。
    calls: [(method, url, kwargs), ...] 完整调用记录。
    """

    def __init__(self, responses=None, factory=None):
        self._responses = list(responses or [])
        self._factory = factory
        self.calls = []

    def _next(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        idx = len(self.calls) - 1
        if self._factory is not None:
            return self._factory(idx, method, url, kwargs)
        if idx < len(self._responses):
            resp = self._responses[idx]
            if isinstance(resp, BaseException):
                raise resp
            return resp
        raise AssertionError(f"FakeSession 响应耗尽: 第 {idx} 次调用 {method} {url}")

    def get(self, url, **kwargs):
        return self._next("GET", url, kwargs)

    def post(self, url, **kwargs):
        return self._next("POST", url, kwargs)


class _Collector(APICollector):
    """测试用最小配置采集器：各用例经构造 overrides 覆盖 API_SPEC 键"""

    source_name = "stub_api"
    display_name = "Stub API"
    domains = ["stub"]
    API_SPEC = {
        "endpoint": "https://api.test.invalid/items",
        "method": "GET",
        "params": {},
        "items_path": "items",
        "field_map": {"title": "title", "url": "url"},
        "domain": "测试",
        "sector": "test",
    }


def _items(n, prefix="u"):
    """生成 n 条唯一 url 的记录"""
    return [{"title": f"{prefix}-{i}", "url": f"http://e/{prefix}/{i}"}
            for i in range(n)]


# ---------- T1-T6: 基础映射与容错 ----------

class ApiCollectorBasicsTest(unittest.TestCase):

    def _c(self, **overrides):
        return _Collector(**overrides)

    def test_single_page_get_maps_fields(self):
        """T1: GET 单页；嵌套 items_path；title/url/summary/date_obj 正确"""
        d = (datetime.now(CST) - timedelta(days=1)).strftime("%Y-%m-%d")
        c = self._c(
            items_path="data.items",
            field_map={
                "title": {"path": "name", "default": ""},
                "url": "link",
                "summary": {"path": "desc", "transform": "truncate", "max": 15},
                "date_obj": {"path": "created", "transform": "parse_date",
                             "fmts": ["%Y-%m-%d"], "tz": "utc"},
            },
        )
        sess = FakeSession(responses=[FakeResponse(payload={"data": {"items": [
            {"name": "A", "link": "http://e/a", "desc": "long description here",
             "created": d},
        ]}})])
        items = c.crawl(sess)

        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it.title, "A")
        self.assertEqual(it.url, "http://e/a")
        self.assertEqual(it.summary, "long descriptio")  # 15 字符截断
        self.assertEqual(it.date_obj, datetime.strptime(d, "%Y-%m-%d")
                         .replace(tzinfo=timezone.utc))
        self.assertEqual(it.domain, "测试")  # spec 静态 domain
        self.assertEqual(it.sector, "test")
        self.assertEqual(it.source, "Stub API")
        self.assertEqual(sess.calls[0][0], "GET")

    def test_timeout_returns_empty(self):
        """T2: sess.get 抛 requests.Timeout → [] 不抛"""
        c = self._c()
        sess = FakeSession(responses=[requests.Timeout("boom")])
        self.assertEqual(c.crawl(sess), [])

    def test_http_error_returns_empty(self):
        """T3: raise_for_status 抛 HTTPError → []"""
        c = self._c()
        sess = FakeSession(responses=[FakeResponse(payload={}, status=500)])
        self.assertEqual(c.crawl(sess), [])

    def test_json_parse_error_returns_empty(self):
        """T4: resp.json() 抛 json.JSONDecodeError → []"""
        c = self._c()
        sess = FakeSession(responses=[FakeResponse(
            json_error=json.JSONDecodeError("bad json", "doc", 0))])
        self.assertEqual(c.crawl(sess), [])

    def test_missing_items_path_returns_empty(self):
        """T5: items_path 缺键 → [] 且 warning"""
        c = self._c(items_path="data.missing")
        sess = FakeSession(responses=[FakeResponse(payload={"data": {}})])
        with self.assertLogs("intel.api", level="WARNING") as cm:
            items = c.crawl(sess)
        self.assertEqual(items, [])
        self.assertTrue(any("缺失" in m for m in cm.output))

    def test_invalid_items_shape_returns_empty(self):
        """T6: items 非 list（dict）→ []"""
        c = self._c(items_path="data")
        sess = FakeSession(responses=[FakeResponse(payload={"data": {"x": 1}})])
        self.assertEqual(c.crawl(sess), [])


# ---------- T7-T9: auth ----------

class ApiCollectorAuthTest(unittest.TestCase):

    def _c(self, **overrides):
        return _Collector(**overrides)

    def test_api_key_header_injected(self):
        """T7: auth api_key → headers 含 X-API-Key"""
        c = self._c(
            auth={"type": "api_key", "header": "X-API-Key", "env": "TEST_API_KEY"},
            api_key="k123",
        )
        sess = FakeSession(responses=[FakeResponse(payload={"items": []})])
        c.crawl(sess)

        self.assertEqual(sess.calls[0][2]["headers"]["X-API-Key"], "k123")

    def test_basic_auth_passed(self):
        """T8: auth basic → auth=(u, p) 传入"""
        c = self._c(
            auth={"type": "basic", "user": "user", "password": "password"},
            user="alice", password="secret",
        )
        sess = FakeSession(responses=[FakeResponse(payload={"items": []})])
        c.crawl(sess)

        self.assertEqual(sess.calls[0][2]["auth"], ("alice", "secret"))

    def test_missing_api_key_fail_closed(self):
        """T9: env/override 均缺 → []，且不发任何请求（不带空头）"""
        c = self._c(
            auth={"type": "api_key", "header": "X-API-Key", "env": "NEVER_SET_TEST_KEY"},
        )
        sess = FakeSession(responses=[FakeResponse(payload={"items": []})])
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NEVER_SET_TEST_KEY", None)
            items = c.crawl(sess)

        self.assertEqual(items, [])
        self.assertEqual(sess.calls, [])  # fail-closed：一次请求都没发


# ---------- T10-T13: 分页 ----------

class ApiCollectorPaginationTest(unittest.TestCase):

    def _c(self, **overrides):
        return _Collector(**overrides)

    def test_offset_pagination(self):
        """T10: start 递增 0/10/20；末页（<page_size）停"""
        c = self._c(pagination={
            "type": "offset", "param": "start",
            "size_param": "max_results", "page_size": 10,
        })

        def factory(idx, method, url, kwargs):
            start = kwargs["params"]["start"]
            size = 10 if start < 20 else 5  # 第 3 页只有 5 条 → 末页
            return FakeResponse(payload={"items": _items(size, f"s{start}")})

        sess = FakeSession(factory=factory)
        items = c.crawl(sess)

        self.assertEqual(len(items), 25)
        starts = [call[2]["params"]["start"] for call in sess.calls]
        self.assertEqual(starts, [0, 10, 20])
        self.assertEqual(sess.calls[0][2]["params"]["max_results"], 10)

    def test_page_pagination_total(self):
        """T11: pageNum 递增；已取条数达 total 停"""
        c = self._c(pagination={
            "type": "page", "param": "pageNum", "size_param": "pageSize",
            "page_size": 10, "total_path": "total",
        })

        def factory(idx, method, url, kwargs):
            page_num = kwargs["params"]["pageNum"]
            return FakeResponse(payload={
                "items": _items(10, f"p{page_num}"), "total": 25,
            })

        sess = FakeSession(factory=factory)
        items = c.crawl(sess)

        # 3 页均满 10 条：第 3 页后 collected=30 >= total=25 → 停
        self.assertEqual(len(items), 30)
        page_nums = [call[2]["params"]["pageNum"] for call in sess.calls]
        self.assertEqual(page_nums, [1, 2, 3])
        self.assertEqual(sess.calls[0][2]["params"]["pageSize"], 10)

    def test_cursor_pagination_stops_on_empty(self):
        """T12: cursor 跟随 next_path；next 为空 → 停"""
        c = self._c(pagination={
            "type": "cursor", "param": "cursor", "next_path": "next_cursor",
        })

        def factory(idx, method, url, kwargs):
            cursor = kwargs["params"].get("cursor")
            if cursor is None:
                return FakeResponse(payload={
                    "items": _items(10, "c0"), "next_cursor": "c1"})
            if cursor == "c1":
                return FakeResponse(payload={
                    "items": _items(10, "c1"), "next_cursor": ""})  # 空 → 停
            raise AssertionError(f"不应出现第 {idx} 次调用 cursor={cursor}")

        sess = FakeSession(factory=factory)
        items = c.crawl(sess)

        self.assertEqual(len(items), 20)
        self.assertEqual(len(sess.calls), 2)
        self.assertEqual(sess.calls[1][2]["params"]["cursor"], "c1")

        # 首页 next 即缺失 → 单页即停
        sess2 = FakeSession(responses=[FakeResponse(payload={
            "items": _items(3, "only"), "next_cursor": ""})])
        items2 = c.crawl(sess2)
        self.assertEqual(len(items2), 3)
        self.assertEqual(len(sess2.calls), 1)

    def test_limit_stops_early(self):
        """T13: limit=5, page_size=10 → 5 条且仅 1 次请求"""
        c = self._c(limit=5, pagination={
            "type": "offset", "param": "start",
            "size_param": "max_results", "page_size": 10,
        })
        sess = FakeSession(factory=lambda idx, m, u, k: FakeResponse(
            payload={"items": _items(10, f"x{idx}")}))
        items = c.crawl(sess)

        self.assertEqual(len(items), 5)
        self.assertEqual(len(sess.calls), 1)


# ---------- T14-T17: 记录级过滤 ----------

class ApiCollectorRecordFilterTest(unittest.TestCase):

    def _c(self, **overrides):
        return _Collector(**overrides)

    def test_skip_record_without_title_or_url(self):
        """T14: 缺 title/url 的记录被跳过，不中断"""
        c = self._c()
        sess = FakeSession(responses=[FakeResponse(payload={"items": [
            {"title": "a", "url": "http://e/a"},
            {"title": "", "url": "http://e/b"},
            {"title": "c", "url": ""},
            {"title": "d", "url": "http://e/d"},
        ]})])
        items = c.crawl(sess)

        self.assertEqual([it.title for it in items], ["a", "d"])

    def test_recency_filter_drops_old(self):
        """T15: 早于 cutoff 的记录被过滤（默认 max_age=7）"""
        today = datetime.now(CST).strftime("%Y-%m-%d")
        old = (datetime.now(CST) - timedelta(days=30)).strftime("%Y-%m-%d")
        c = self._c(field_map={
            "title": "title", "url": "url",
            "date_obj": {"path": "date", "transform": "parse_date",
                         "fmts": ["%Y-%m-%d"], "tz": "utc"},
        })
        sess = FakeSession(responses=[FakeResponse(payload={"items": [
            {"title": "fresh", "url": "http://e/f", "date": today},
            {"title": "stale", "url": "http://e/s", "date": old},
        ]})])
        items = c.crawl(sess)

        self.assertEqual([it.title for it in items], ["fresh"])

    def test_dedup_by_url(self):
        """T16: 同 url 两条 → 去重 1 条"""
        c = self._c()
        sess = FakeSession(responses=[FakeResponse(payload={"items": [
            {"title": "a1", "url": "http://e/same"},
            {"title": "a2", "url": "http://e/same"},
        ]})])
        items = c.crawl(sess)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "a1")

    def test_raw_data_preserved(self):
        """T17: item.raw_data == 原始记录 dict"""
        rec = {"title": "x", "url": "http://e/x", "extra": 42}
        c = self._c()
        sess = FakeSession(responses=[FakeResponse(payload={"items": [rec]})])
        items = c.crawl(sess)

        self.assertEqual(items[0].raw_data, rec)


# ---------- T18-T21: transform / queries / POST / 部分失败 ----------

class ApiCollectorTransformTest(unittest.TestCase):

    def _c(self, **overrides):
        return _Collector(**overrides)

    def test_transform_truncate_and_template(self):
        """T18: 中间变量 + template 组合正确，中间变量不落 NewsItem"""
        d = (datetime.now(CST) - timedelta(days=2)).strftime("%Y-%m-%d")
        c = self._c(field_map={
            "title": {"path": "title", "transform": "collapse_ws"},
            "url": "url",
            "date_obj": {"path": "date", "transform": "parse_date",
                         "fmts": ["%Y-%m-%d"], "tz": "utc"},
            "_authors": {"path": "authors", "transform": "join_names",
                         "sep": ", ", "limit": 2, "suffix": " et al."},
            "_summary": {"path": "summary", "transform": "truncate", "max": 10},
            "summary": {"transform": "template",
                        "template": "[{_authors}] {_summary}"},
        })
        sess = FakeSession(responses=[FakeResponse(payload={"items": [
            {"title": "  A   B ", "url": "http://e/u",
             "authors": ["x", "y", "z"], "summary": "1234567890abcdef",
             "date": d},
        ]})])
        items = c.crawl(sess)

        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it.title, "A B")  # collapse_ws
        self.assertEqual(it.summary, "[x, y et al.] 1234567890")
        self.assertEqual(it.date_obj, datetime.strptime(d, "%Y-%m-%d")
                         .replace(tzinfo=timezone.utc))
        self.assertFalse(hasattr(it, "_authors"))  # 中间变量不落属性
        self.assertFalse(hasattr(it, "_summary"))

    def test_queries_loop_multiple_requests(self):
        """T19: queries 2 项 → 2 次请求，静态 params 与 query 覆盖合并"""
        c = self._c(params={"k": "v"}, queries=[{"q": "one"}, {"q": "two"}])
        sess = FakeSession(responses=[
            FakeResponse(payload={"items": _items(1, "one")}),
            FakeResponse(payload={"items": _items(1, "two")}),
        ])
        items = c.crawl(sess)

        self.assertEqual(len(items), 2)
        self.assertEqual(len(sess.calls), 2)
        for i, q in enumerate(["one", "two"]):
            params = sess.calls[i][2]["params"]
            self.assertEqual(params["k"], "v")  # 静态参数每请求都在
            self.assertEqual(params["q"], q)

    def test_post_json_body(self):
        """T20: POST + body:json → json= 传入；body:form → data= 传入"""
        c = self._c(method="POST", body="json", params={"a": 1})
        sess = FakeSession(responses=[FakeResponse(payload={"items": []})])
        c.crawl(sess)

        self.assertEqual(sess.calls[0][0], "POST")
        self.assertEqual(sess.calls[0][2]["json"], {"a": 1})
        self.assertNotIn("data", sess.calls[0][2])

        c2 = self._c(method="POST", body="form", params={"a": 1})
        sess2 = FakeSession(responses=[FakeResponse(payload={"items": []})])
        c2.crawl(sess2)
        self.assertEqual(sess2.calls[0][2]["data"], {"a": 1})

    def test_partial_result_on_later_page_failure(self):
        """T21: 第 1 页 OK、第 2 页超时 → 返回第 1 页条目不抛"""
        c = self._c(pagination={
            "type": "offset", "param": "start",
            "size_param": "max_results", "page_size": 10,
        })
        sess = FakeSession(responses=[
            FakeResponse(payload={"items": _items(10, "pg0")}),
            requests.Timeout("page2 timeout"),
        ])
        items = c.crawl(sess)

        self.assertEqual(len(items), 10)
        self.assertEqual(len(sess.calls), 2)


# ---------- T22: arxiv XML 归一化 ----------

ARXIV_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2401.12345v1</id>
    <title>  Glare-aware   Autonomous  Driving  Perception </title>
    <published>{pub1}</published>
    <summary>This paper studies the impact of glare
on autonomous driving perception systems.</summary>
    <author><name>Alice</name></author>
    <author><name>Bob</name></author>
    <author><name>Carol</name></author>
    <author><name>Dave</name></author>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2401.54321v2</id>
    <title>Low Light Dataset for Night Driving</title>
    <published>{pub2}</published>
    <summary>A new dataset for low light night driving.</summary>
    <author><name>Eve</name></author>
  </entry>
</feed>"""


class ArxivExtractTest(unittest.TestCase):

    def setUp(self):
        # 用相对日期保证过旧过滤不误伤（沙箱当前为 2026 年）
        self.pub1 = (datetime.now(CST) - timedelta(days=1)).strftime("%Y-%m-%d") + "T18:00:00Z"
        self.pub2 = (datetime.now(CST) - timedelta(days=2)).strftime("%Y-%m-%d") + "T12:00:00Z"
        self.xml = ARXIV_XML.format(pub1=self.pub1, pub2=self.pub2)

    def test_arxiv_spec_extract_records_xml(self):
        """T22a: _extract_records 对 Atom XML 归一化为正确 dict 字段"""
        c = ArxivPerceptionCollector(max_age=90, delay=0)
        spec = c._merged_spec()
        records = c._extract_records(FakeResponse(text=self.xml), spec)

        self.assertEqual(len(records), 2)
        rec0 = records[0]
        self.assertEqual(rec0["title"], "Glare-aware Autonomous Driving Perception")
        self.assertEqual(rec0["id"], "http://arxiv.org/abs/2401.12345v1")
        self.assertEqual(rec0["published"], self.pub1)
        self.assertEqual(rec0["authors"], ["Alice", "Bob", "Carol", "Dave"])
        self.assertIn("impact of glare", rec0["summary"])  # strip 但保留内部换行
        self.assertEqual(records[1]["authors"], ["Eve"])

    def test_arxiv_crawl_full_chain(self):
        """T22b: crawl 全链路行为与改造前等价（title/summary/domain/sector/去重）"""
        c = ArxivPerceptionCollector(max_age=90, delay=0)
        sess = FakeSession(factory=lambda idx, m, u, k: FakeResponse(text=self.xml))
        items = c.crawl(sess)

        self.assertEqual(len(items), 2)  # 7 queries × 2 entry，url 去重后仍 2 条
        it = items[0]
        self.assertEqual(it.title, "Glare-aware Autonomous Driving Perception")
        expect_summary = "[Alice, Bob, Carol et al.] " + (
            "This paper studies the impact of glare\n"
            "on autonomous driving perception systems.")[:200]
        self.assertEqual(it.summary, expect_summary)
        self.assertEqual(it.domain, "学术")
        self.assertEqual(it.sector, "perception_lighting")
        self.assertEqual(it.source, "arXiv (感知+光照)")
        self.assertEqual(it.date_obj.tzinfo, timezone.utc)
        self.assertTrue(it.date_obj >= c.cutoff)  # 近期论文保留
        self.assertEqual(it.raw_data["id"], "http://arxiv.org/abs/2401.12345v1")


if __name__ == "__main__":
    unittest.main()
