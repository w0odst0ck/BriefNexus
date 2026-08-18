"""boss_zhipin 自适应频率控制单测(B 系列)— 指数退避 / 风控熔断 / 429 Retry-After

覆盖(boss-backoff design §2.1 表格):
  B0.1-B0.8 纯函数: 结果分类(_classify_api)、Retry-After 解析(_read_retry_after)、
            退避数学(_backoff_base/_risk_backoff_base)
  B2/B3/B4a/B4b 状态机: _record_* + _backoff_sleep(patch uniform→1.0 消 jitter,
            patch time.sleep 记录参数)
  B1/B5/B5b/B5c/B5d/B5e/B6/B6b/B7/B7b/B8/B9 集成: crawl + FakeSession(风控熔断/详情停抓/429/
            stats 恒含 4 新字段/cookie 零泄漏)

本文件自包含: FakeSession/FakeResponse 惯例同 test_boss_api.py, 但 FakeResponse
新增 status_code/headers 属性以覆盖 429/Retry-After 路径(既有测试零改动靠
_api_get 的 raise_for_status 回退分支兼容)。不发真网络。
"""
import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock

from intel.collectors.boss_zhipin import (
    KIND_FAIL,
    KIND_OK,
    KIND_RATE_LIMITED,
    KIND_RISK,
    BossZhipinCollector,
    _BackoffState,
    _classify_api,
    _read_retry_after,
)
from intel.core.base import CST

# ---------- stub helpers(同 test_boss_api.py 惯例, 增强 status_code/headers) ----------


class FakeResponse:
    """requests.Response 替身: 带 status_code/headers(真实 Response 语义)"""

    def __init__(self, text="", status=200, json_body=None, headers=None):
        self._text, self._status, self._json = text, status, json_body
        self.status_code = status
        self.headers = dict(headers or {})

    @property
    def text(self):
        return self._text

    def json(self, **kw):
        if self._json is None:
            raise ValueError("no JSON body")   # 模拟「非 JSON」
        return self._json

    def raise_for_status(self):
        if not (200 <= self._status < 400):
            raise RuntimeError(f"HTTP {self._status}")


class FakeSession:
    """记录 get 调用的内存替身; 响应耗尽/异常按序弹出"""

    def __init__(self, responses=None):
        self._responses = list(responses or [])
        self.calls = []  # [(url, kwargs), ...]

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        idx = len(self.calls) - 1
        if idx < len(self._responses):
            resp = self._responses[idx]
            if isinstance(resp, BaseException):
                raise resp
            return resp
        raise AssertionError(f"FakeSession 响应耗尽: 第 {idx} 次 get {url}")


# ---------- payload builders ----------


def _job(eid="abc123", name="大模型工程师"):
    return {"encryptJobId": eid, "jobName": name, "brandName": "某某科技",
            "brandIndustry": "人工智能", "brandScaleName": "1000-9999人",
            "jobExperience": "3-5年", "jobDegree": "本科",
            "cityName": "上海", "areaDistrict": "浦东新区", "businessDistrict": "张江",
            "lid": f"lid-{eid}", "securityId": f"sec-{eid}"}


def _payload(code=0, jobs=None):
    return {"code": code, "zpData": {"jobList": jobs if jobs is not None else []}}


def _detail_payload(code=0, post_description=None):
    return {"code": code, "zpData": {"jobInfo": {"postDescription": post_description}}}


def _api_ok(jobs=None):
    """列表 API 成功(code==0)"""
    return FakeResponse(json_body=_payload(code=0, jobs=jobs or []))


def _api_risk():
    """列表风控(code==37 + message 含「环境异常」)"""
    return FakeResponse(json_body={"code": 37, "message": "环境异常, 请稍后再试",
                                   "zpData": {}})


def _detail_ok(jd="岗位职责：开发引擎"):
    return FakeResponse(json_body=_detail_payload(post_description=jd))


def _detail_empty():
    return FakeResponse(json_body=_detail_payload(post_description=""))


def _detail_risk():
    return FakeResponse(json_body={"code": 37, "message": "环境异常", "zpData": {}})


def _net_error():
    return RuntimeError("connection reset")


def _api_429(retry_after=None):
    """HTTP 429; 可选 Retry-After 头"""
    headers = {}
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return FakeResponse(status=429, json_body=None, headers=headers)


def _today():
    return datetime.now(CST).strftime("%Y-%m-%d")


def _read_joblist(tmp):
    with open(os.path.join(tmp, _today(), "joblist.json"), encoding="utf-8") as f:
        return json.load(f)


# 与 test_boss_api.py A18 共用的 secrets 清单(cookie 零泄漏红线)
EDIT_THIS_COOKIES = [
    {"domain": ".zhipin.com", "expirationDate": 1893456000, "hostOnly": False,
     "httpOnly": True, "name": "__zp_stoken__", "path": "/",
     "sameSite": "no_restriction", "secure": True, "session": False,
     "storeId": "0", "value": "SECRET_TOKEN_VALUE"},
    {"domain": "www.zhipin.com", "expirationDate": 1893456000, "hostOnly": True,
     "httpOnly": False, "name": "wt2", "path": "/", "sameSite": "lax",
     "secure": False, "session": False, "storeId": "0", "value": "SECRET_WT2_VALUE"},
    {"domain": "www.zhipin.com", "hostOnly": True, "httpOnly": False,
     "name": "zp_session", "path": "/", "sameSite": "strict", "secure": False,
     "session": True, "storeId": "0", "value": "SECRET_SESSION_VALUE"},
]
SECRETS = ("SECRET_TOKEN_VALUE", "SECRET_WT2_VALUE", "SECRET_SESSION_VALUE",
           "__zp_stoken__", "zp_at")


def _write_cookies(tmp, cookies, name="cookies.json"):
    path = os.path.join(tmp, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cookies, f)
    return path


def _make(tmp, **kw):
    """默认: 无 cookie、query="x"、单页; kw 覆盖"""
    base = {"query": "x", "output_dir": tmp,
            "cookies_path": "/nonexistent/cookies.json"}
    base.update(kw)
    return BossZhipinCollector(**base)


# ---------- B0.x: 纯函数(分类 / Retry-After / 退避数学) ----------


class ClassifyApiTest(unittest.TestCase):

    def test_code37_is_risk(self):
        """B0.1: 200 + code==37 → KIND_RISK"""
        self.assertEqual(_classify_api(200, {"code": 37}), KIND_RISK)

    def test_env_message_is_risk(self):
        """B0.2: message 含「环境异常」→ KIND_RISK(无 code37 也命中)"""
        self.assertEqual(_classify_api(200, {"code": 1001,
                                             "message": "您的环境异常, 请稍后再试"}),
                         KIND_RISK)

    def test_ordinary_code_nonzero_is_fail(self):
        """B0.3: 200 + code≠0(非37)→ KIND_FAIL"""
        self.assertEqual(_classify_api(200, {"code": 1001}), KIND_FAIL)

    def test_code0_is_ok(self):
        """B0.4: 200 + code==0 → KIND_OK"""
        self.assertEqual(_classify_api(200, {"code": 0}), KIND_OK)

    def test_429_500_nondict(self):
        """B0.5: 429→rate_limited; 500→fail; 200+非dict→fail"""
        self.assertEqual(_classify_api(429, {"code": 0}), KIND_RATE_LIMITED)
        self.assertEqual(_classify_api(500, {"code": 0}), KIND_FAIL)
        self.assertEqual(_classify_api(200, "x"), KIND_FAIL)

    def test_string_code37(self):
        """B0.1b: code 为字符串 "37" 的脏数据也判风控"""
        self.assertEqual(_classify_api(200, {"code": "37"}), KIND_RISK)


class RetryAfterTest(unittest.TestCase):

    def test_clamp_and_parse(self):
        """B0.6: 整数秒解析 + 钳制 [0,300] + 大小写变体 + 非法/缺失 → None"""
        self.assertEqual(_read_retry_after({"Retry-After": "90"}), 90)
        self.assertEqual(_read_retry_after({"Retry-After": "999"}), 300)
        self.assertEqual(_read_retry_after({"Retry-After": "-5"}), 0)
        self.assertIsNone(_read_retry_after({"Retry-After": "abc"}))
        self.assertIsNone(_read_retry_after({}))
        self.assertIsNone(_read_retry_after(None))
        self.assertEqual(_read_retry_after({"retry-after": "12"}), 12)
        self.assertEqual(_read_retry_after({"RETRY-AFTER": "7"}), 7)


class BackoffMathTest(unittest.TestCase):

    def test_exponential_cap(self):
        """B0.7: _backoff_base 2^n 封顶 60: 1/2/3/4/5 → 10/20/40/60/60"""
        with tempfile.TemporaryDirectory() as tmp:
            c = _make(tmp)
            self.assertEqual([c._backoff_base(n) for n in (1, 2, 3, 4, 5)],
                             [10.0, 20.0, 40.0, 60.0, 60.0])

    def test_risk_high_start(self):
        """B0.8: _risk_backoff_base 高位起算: 1/2/4 → 40/40/60"""
        with tempfile.TemporaryDirectory() as tmp:
            c = _make(tmp)
            self.assertEqual([c._risk_backoff_base(n) for n in (1, 2, 4)],
                             [40.0, 40.0, 60.0])


# ---------- B2/B3/B4: 状态机(_record_* + _backoff_sleep) ----------


class BackoffStateTest(unittest.TestCase):

    def test_failure_backoff_escalates(self):
        """B2: 连续 3 次 fail → sleep 10/20/40, backoffs==3"""
        with tempfile.TemporaryDirectory() as tmp:
            c = _make(tmp)
            st = _BackoffState()
            with mock.patch("intel.collectors.boss_zhipin.time.sleep") as m_sleep, \
                 mock.patch("intel.collectors.boss_zhipin.random.uniform",
                            return_value=1.0):
                for _ in range(3):
                    c._record_failure(st, KIND_FAIL)
                    c._backoff_sleep(st)
        self.assertEqual([a.args[0] for a in m_sleep.call_args_list],
                         [10.0, 20.0, 40.0])
        self.assertEqual(st.backoffs, 3)
        self.assertEqual(st.n, 3)

    def test_success_resets_n(self):
        """B3: fail→success→fail→sleep → 末次 sleep==10.0(n 已归零)"""
        with tempfile.TemporaryDirectory() as tmp:
            c = _make(tmp)
            st = _BackoffState()
            with mock.patch("intel.collectors.boss_zhipin.time.sleep") as m_sleep, \
                 mock.patch("intel.collectors.boss_zhipin.random.uniform",
                            return_value=1.0):
                c._record_failure(st, KIND_FAIL)
                c._backoff_sleep(st)
                c._record_success(st)
                c._record_failure(st, KIND_FAIL)
                c._backoff_sleep(st)
        self.assertEqual(m_sleep.call_args_list[-1].args[0], 10.0)
        self.assertEqual(st.n, 1)

    def test_429_with_retry_after(self):
        """B4a: 429 + Retry-After=90 → sleep==90.0, rate_limited==1"""
        with tempfile.TemporaryDirectory() as tmp:
            c = _make(tmp)
            st = _BackoffState()
            with mock.patch("intel.collectors.boss_zhipin.time.sleep") as m_sleep, \
                 mock.patch("intel.collectors.boss_zhipin.random.uniform",
                            return_value=1.0):
                c._record_failure(st, KIND_RATE_LIMITED, 90)
                c._backoff_sleep(st)
        self.assertEqual(m_sleep.call_args.args[0], 90.0)
        self.assertEqual(st.rate_limited, 1)

    def test_429_without_retry_after_falls_back_exp(self):
        """B4b: 429 无头 → 回落指数退避(非 Retry-After 值)"""
        with tempfile.TemporaryDirectory() as tmp:
            c = _make(tmp)
            st = _BackoffState()
            with mock.patch("intel.collectors.boss_zhipin.time.sleep") as m_sleep, \
                 mock.patch("intel.collectors.boss_zhipin.random.uniform",
                            return_value=1.0):
                c._record_failure(st, KIND_RATE_LIMITED, None)
                c._backoff_sleep(st)
        self.assertEqual(m_sleep.call_args.args[0], 10.0)   # n=1 指数退避


# ---------- B1/B5-B9: 集成(crawl + FakeSession) ----------


class CrawlIntegrationTest(unittest.TestCase):

    def test_success_no_backoff(self):
        """B1: 全成功无退避: backoffs==0, 所有 sleep==5.0(基频)"""
        with tempfile.TemporaryDirectory() as tmp:
            c = _make(tmp, details=True, jitter=0.0)
            sess = FakeSession([
                _api_ok([_job("1"), _job("2")]),
                _detail_ok("JD1"),
                _detail_ok("JD2"),
            ])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep") as m_sleep, \
                 mock.patch("intel.collectors.boss_zhipin.random.uniform",
                            return_value=0.0):
                items = c.crawl(sess)
            data = _read_joblist(tmp)
        self.assertEqual(len(items), 2)
        self.assertEqual(m_sleep.call_count, 2)          # 2 卡详情, 列表首请求不睡
        for call in m_sleep.call_args_list:
            self.assertEqual(call.args[0], 5.0)          # base + jitter(0)
        self.assertEqual(data["stats"]["backoffs"], 0)

    def test_list_risk_circuit_breaks(self):
        """B5: 列表风控 → 长退避 → 重试仍风控 → 熔断, 无后续 query 请求"""
        with tempfile.TemporaryDirectory() as tmp:
            c = _make(tmp, queries=["x", "y"], details=False)
            sess = FakeSession([_api_risk(), _api_risk()])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep") as m_sleep, \
                 mock.patch("intel.collectors.boss_zhipin.random.uniform",
                            return_value=1.0):
                items = c.crawl(sess)
            data = _read_joblist(tmp)
        self.assertEqual(items, [])                      # 返回已收集部分(空)
        self.assertEqual(len(sess.calls), 2)             # 仅该 query 的 2 次重试
        self.assertIs(data["stats"]["circuit_open"], True)
        self.assertIs(data["stats"]["risk_blocked"], True)
        self.assertEqual(data["stats"]["backoffs"], 1)   # 重试前长退避 1 次
        self.assertEqual(m_sleep.call_args_list[0].args[0], 40.0)  # 长退避 ≥40s
        for key in ("backoffs", "rate_limited", "circuit_open", "risk_blocked"):
            self.assertIn(key, data["stats"])

    def test_list_risk_retry_success(self):
        """B5b: 列表风控重试成功 → 不熔断, 正常采集"""
        with tempfile.TemporaryDirectory() as tmp:
            c = _make(tmp, details=True)
            sess = FakeSession([
                _api_risk(),            # 列表 → 风控
                _api_ok([_job("1")]),   # 重试 → 成功
                _detail_ok("JD"),
            ])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"), \
                 mock.patch("intel.collectors.boss_zhipin.random.uniform",
                            return_value=1.0):
                items = c.crawl(sess)
            data = _read_joblist(tmp)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].raw_data["job"]["jd_status"], "ok")
        self.assertIs(data["stats"]["circuit_open"], False)
        self.assertIs(data["stats"]["risk_blocked"], False)

    def test_detail_risk_twice_halts(self):
        """B6: 详情风控连 2 → detail_halt, 剩余卡 skipped, 列表保留"""
        with tempfile.TemporaryDirectory() as tmp:
            c = _make(tmp, details=True)
            sess = FakeSession([
                _api_ok([_job("1"), _job("2"), _job("3")]),
                _detail_risk(),         # 卡1 → 风控(1)
                _detail_risk(),         # 卡2 → 风控(2) → halt
            ])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"), \
                 mock.patch("intel.collectors.boss_zhipin.random.uniform",
                            return_value=1.0):
                items = c.crawl(sess)
            data = _read_joblist(tmp)
        self.assertEqual(len(items), 3)                  # 列表 3 张都保留
        statuses = [i.raw_data["job"]["jd_status"] for i in items]
        self.assertEqual(statuses, ["failed", "failed", "skipped"])
        self.assertEqual(len(sess.calls), 3)             # 卡3 详情未发
        self.assertIs(data["stats"]["circuit_open"], False)
        self.assertIs(data["stats"]["risk_blocked"], False)
        self.assertEqual(data["stats"]["detail_failed"], 2)

    def test_detail_risk_once_no_halt(self):
        """B6b: 详情风控仅 1 次 → 不停详情, 卡2 正常 ok"""
        with tempfile.TemporaryDirectory() as tmp:
            c = _make(tmp, details=True)
            sess = FakeSession([
                _api_ok([_job("1"), _job("2")]),
                _detail_risk(),
                _detail_ok("JD2"),
            ])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"), \
                 mock.patch("intel.collectors.boss_zhipin.random.uniform",
                            return_value=1.0):
                items = c.crawl(sess)
        statuses = [i.raw_data["job"]["jd_status"] for i in items]
        self.assertEqual(statuses, ["failed", "ok"])
        self.assertEqual(len(sess.calls), 3)

    def test_detail_fail_three_halts_list_continues(self):
        """B7: 详情失败 3 次 → 停详情; 下一 query 仍发列表请求"""
        with tempfile.TemporaryDirectory() as tmp:
            c = _make(tmp, queries=["x", "y"], details=True)
            sess = FakeSession([
                _api_ok([_job("1"), _job("2"), _job("3"), _job("4")]),
                _net_error(),           # 卡1 详情网络异常
                _net_error(),           # 卡2
                _net_error(),           # 卡3 → halt
                _api_ok([]),            # query y 列表(空页, 合法)
            ])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"), \
                 mock.patch("intel.collectors.boss_zhipin.random.uniform",
                            return_value=1.0):
                items = c.crawl(sess)
            data = _read_joblist(tmp)
        self.assertEqual(len(items), 4)
        statuses = [i.raw_data["job"]["jd_status"] for i in items]
        self.assertEqual(statuses, ["failed", "failed", "failed", "skipped"])
        self.assertEqual(len(sess.calls), 5)             # 列表+3详情+query y 列表
        self.assertIn("query=y", sess.calls[4][0])       # 列表继续
        self.assertIs(data["stats"]["circuit_open"], False)
        self.assertEqual(data["stats"]["detail_failed"], 3)

    def test_detail_fail_then_empty_no_halt(self):
        """B7b: 详情失败 2 次后 empty(成功) → 重置 streak, 不停详情"""
        with tempfile.TemporaryDirectory() as tmp:
            c = _make(tmp, details=True)
            sess = FakeSession([
                _api_ok([_job("1"), _job("2"), _job("3")]),
                _net_error(),
                _net_error(),
                _detail_empty(),        # 卡3 → empty(重置连续计数)
            ])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"), \
                 mock.patch("intel.collectors.boss_zhipin.random.uniform",
                            return_value=1.0):
                items = c.crawl(sess)
            data = _read_joblist(tmp)
        statuses = [i.raw_data["job"]["jd_status"] for i in items]
        self.assertEqual(statuses, ["failed", "failed", "empty"])
        self.assertEqual(len(sess.calls), 4)             # 卡3 详情仍发
        self.assertEqual(data["stats"]["detail_failed"], 2)
        self.assertEqual(data["stats"]["detail_empty"], 1)

    def test_429_retry_after_in_crawl(self):
        """B5c: 详情 429 + Retry-After=90 → 下一详情退避 90s; rate_limited 计数"""
        with tempfile.TemporaryDirectory() as tmp:
            c = _make(tmp, details=True)
            sess = FakeSession([
                _api_ok([_job("1"), _job("2")]),
                _api_429(90),           # 卡1 详情 429
                _detail_ok("JD2"),      # 卡2 详情 OK
            ])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep") as m_sleep, \
                 mock.patch("intel.collectors.boss_zhipin.random.uniform",
                            return_value=1.0):
                items = c.crawl(sess)
            data = _read_joblist(tmp)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].raw_data["job"]["jd_status"], "failed")
        self.assertEqual(items[1].raw_data["job"]["jd_status"], "ok")
        self.assertEqual(data["stats"]["rate_limited"], 1)
        self.assertEqual(data["stats"]["detail_failed"], 1)
        # 卡2 详情前经 _backoff_sleep → Retry-After 90s(详情路径无 last_kind 覆盖)
        self.assertIn(90.0, [a.args[0] for a in m_sleep.call_args_list])

    def test_list_429_falls_back_html(self):
        """B5d: 列表 429(无头)→ 计入 rate_limited + 降级 HTML 成功(E9/E10)"""
        html = '<html><body><div class="job-card-wrapper"><a class="job-card-left" ' \
               'href="/job_detail/9.html"><span class="job-name">降级岗</span></a>' \
               '</div></body></html>'
        with tempfile.TemporaryDirectory() as tmp:
            c = _make(tmp, details=False, jitter=0.0)
            sess = FakeSession([_api_429(), FakeResponse(html)])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"):
                items = c.crawl(sess)
            data = _read_joblist(tmp)
        self.assertEqual(len(items), 1)
        self.assertEqual(data["stats"]["rate_limited"], 1)
        self.assertGreaterEqual(data["stats"]["failed_requests"], 1)
        self.assertEqual(data["stats"]["requests"], 1)   # HTML 成功页计入

    def test_detail_429_streak_halts(self):
        """B5e: 详情连续 429(无头)计入 detail_fail_streak, 3 次后停详情(E13)"""
        with tempfile.TemporaryDirectory() as tmp:
            c = _make(tmp, details=True)
            sess = FakeSession([
                _api_ok([_job("1"), _job("2"), _job("3"), _job("4")]),
                _api_429(),             # 卡1 429
                _api_429(),             # 卡2 429
                _api_429(),             # 卡3 429 → halt
            ])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"), \
                 mock.patch("intel.collectors.boss_zhipin.random.uniform",
                            return_value=1.0):
                items = c.crawl(sess)
            data = _read_joblist(tmp)
        statuses = [i.raw_data["job"]["jd_status"] for i in items]
        self.assertEqual(statuses, ["failed", "failed", "failed", "skipped"])
        self.assertEqual(data["stats"]["rate_limited"], 3)
        self.assertEqual(len(sess.calls), 4)             # 卡4 详情未发

    def test_stats_defaults_and_triggered(self):
        """B8: 正常 run 默认值; 风控 run 触发值; 4 键恒存在"""
        # 正常 run
        with tempfile.TemporaryDirectory() as tmp:
            c = _make(tmp, details=False)
            sess = FakeSession([_api_ok([_job("1")])])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"):
                c.crawl(sess)
            data = _read_joblist(tmp)
            stats = data["stats"]
            self.assertEqual(stats["backoffs"], 0)
            self.assertEqual(stats["rate_limited"], 0)
            self.assertIs(stats["circuit_open"], False)
            self.assertIs(stats["risk_blocked"], False)
        # 风控 run
        with tempfile.TemporaryDirectory() as tmp:
            c = _make(tmp, queries=["x", "y"], details=False)
            sess = FakeSession([_api_risk(), _api_risk()])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"), \
                 mock.patch("intel.collectors.boss_zhipin.random.uniform",
                            return_value=1.0):
                c.crawl(sess)
            data = _read_joblist(tmp)
            stats = data["stats"]
            self.assertEqual(stats["backoffs"], 1)
            self.assertEqual(stats["rate_limited"], 0)
            self.assertIs(stats["circuit_open"], True)
            self.assertIs(stats["risk_blocked"], True)
        for key in ("backoffs", "rate_limited", "circuit_open", "risk_blocked"):
            self.assertIn(key, stats)

    def test_cookie_zero_leak(self):
        """B9: 带 cookie 跑出退避+熔断+风控日志 → 产物与日志零泄漏(复用 A18 secrets)"""
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_cookies(tmp, EDIT_THIS_COOKIES)
            c = _make(tmp, queries=["x", "y"], details=False, cookies_path=path)
            sess = FakeSession([_api_risk(), _api_risk()])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"), \
                 mock.patch("intel.collectors.boss_zhipin.random.uniform",
                            return_value=1.0), \
                 self.assertLogs("intel.boss_zhipin", level="WARNING") as cm:
                c.crawl(sess)
            data = _read_joblist(tmp)
        blob = json.dumps(data, ensure_ascii=False)
        log_blob = "\n".join(cm.output)
        self.assertIn("BOSS 退避", log_blob)             # 退避日志确实产生
        self.assertIn("熔断", log_blob)                  # 熔断日志确实产生
        for secret in SECRETS:
            self.assertNotIn(secret, blob)
            self.assertNotIn(secret, log_blob)


if __name__ == "__main__":
    unittest.main()
