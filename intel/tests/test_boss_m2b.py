"""boss_zhipin M2b 单测(B1-B16)— cookie 加载/详情 JD 采集/增量去重, 全 stub/mock

覆盖(design §2.2 / 错误处理 E6-E14, E20):
  B1  cookie 文件缺失 → warning + 无 cookie(sess 无 cookies kwarg)
  B2  cookie 文件坏 JSON → warning + 无 cookie
  B3  EditThisCookie 归一化: expirationDate→expires / sameSite 映射 / domain 去点
      / session 无 expires / secure+httpOnly 透传
  B4  非 zhipin.com 域 / 缺 name-value → 丢弃
  B5  crawl 注入: sess.get 调用含 cookies= 归一化列表
  B6  详情页二次渲染 → jd_full 取 job-sec-text 文本
  B7  _extract_jd_full 多级降级 L0→L1→L2(职位描述标题捕获)
  B8  详情请求失败 → jd_full="", jd_status="failed", 岗位仍返回
  B9  详情页无 JD 容器 → jd_full="", jd_status="empty"(不虚构)
  B10 增量去重: 预置历史 hash → 重跑 skipped_existing>0 / new_jobs=0, merge 无重复
  B11 force=True → 忽略历史全量重采
  B12 历史缺失/坏 JSON → 全量当新, 不崩
  B13 产物含 jd_full, 全文无 cookie 值/名泄漏
  B14 cookies_path: 环境变量覆盖默认, 构造 kwarg 覆盖环境
  B15 日志无 cookie 值(redaction)
  B16 产物 params.details/force 落盘
"""
import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock

from intel.collectors.boss_zhipin import (
    BossZhipinCollector,
    _extract_jd_full,
    _link_hash,
    _normalize_cookie,
)
from intel.core.base import CST

# ---------- stub helpers(与 M2a 同一惯例) ----------


class FakeResponse:
    def __init__(self, text="", status=200, json_body=None):
        self._text = text
        self._status = status
        self._json = json_body

    @property
    def text(self):
        return self._text

    def json(self, **kw):
        if self._json is None:
            raise ValueError("no JSON body")   # 模拟「非 JSON」→ API 降级
        return self._json

    def raise_for_status(self):
        if not (200 <= self._status < 400):
            raise RuntimeError(f"HTTP {self._status}")


class FakeSession:
    """记录 get 调用(含 kwargs); 响应耗尽/异常按序弹出"""

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


# ---------- HTML builders / helpers ----------

def _card(pid, title="岗位", company="某某科技", salary="30-50K"):
    parts = [
        (f'<div class="job-card-wrapper"><a class="job-card-left" '
         f'href="/job_detail/{pid}.html"><span class="job-name">{title}</span></a>'),
    ]
    if any([company, salary]):
        parts.append('<ul class="job-info">')
        if salary:
            parts.append(f'<span class="salary">{salary}</span>')
        parts.append("</ul>")
    if company:
        parts.append(f'<div class="company-info"><h3 class="name">{company}</h3></div>')
    parts.append("</div>")
    return "".join(parts)


def _page(*cards):
    return "<html><body>" + "".join(cards) + "</body></html>"


def _api_fail():
    """API 失败响应(非 JSON)→ crawl 自动降级 HTML(design §2.3)"""
    return FakeResponse(json_body=None)


def _html_session(*cards, detail=None):
    """HTML 降级路径专用: 先 API 失败、再 HTML 列表、可选 HTML 详情"""
    responses = [_api_fail(), FakeResponse(_page(*cards))]
    if detail is not None:
        responses.append(FakeResponse(detail))
    return FakeSession(responses)


def _detail(pid=1, jd=None):
    jd = jd or f'<div class="job-sec-text">岗位职责：开发大模型推理引擎 {pid}</div>'
    return f"<html><body>{jd}</body></html>"


def _today():
    return datetime.now(CST).strftime("%Y-%m-%d")


def _read_joblist(tmp):
    with open(os.path.join(tmp, _today(), "joblist.json"), encoding="utf-8") as f:
        return json.load(f)


def _write_cookies(tmp, cookies, name="cookies.json"):
    path = os.path.join(tmp, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cookies, f)
    return path


def _write_history(tmp, jobs):
    path = os.path.join(tmp, _today(), "joblist.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"schema_version": "1.0", "jobs": jobs}, f, ensure_ascii=False)


# 实测 EditThisCookie 导出格式(15 条的子集, 含核心 token)
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


# ---------- B1-B4: cookie 加载与归一化 ----------

class CookieLoadTest(unittest.TestCase):

    def test_cookie_file_missing(self):
        """B1: 文件缺失 → warning + 无 cookie(sess 无 cookies kwarg)"""
        with tempfile.TemporaryDirectory() as tmp:
            c = BossZhipinCollector(query="x", output_dir=tmp,
                                    cookies_path="/nonexistent/cookies.json",
                                    details=True)
            sess = _html_session(_card("1"), detail=_detail())
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"), \
                 self.assertLogs("intel.boss_zhipin", level="WARNING"):
                items = c.crawl(sess)
        self.assertEqual(len(items), 1)
        self.assertEqual(len(sess.calls), 3)  # API 失败 + HTML 列表 + HTML 详情
        for _url, kwargs in sess.calls:
            self.assertNotIn("cookies", kwargs)

    def test_cookie_file_bad_json(self):
        """B2: 坏 JSON → warning + 无 cookie"""
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_cookies(tmp, None)
            with open(path, "w", encoding="utf-8") as f:
                f.write("{bad json{{{")
            c = BossZhipinCollector(query="x", output_dir=tmp, cookies_path=path,
                                    details=False)
            sess = FakeSession([FakeResponse(json_body=None),
                                FakeResponse(_page(_card("1")))])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"), \
                 self.assertLogs("intel.boss_zhipin", level="WARNING"):
                c.crawl(sess)
        self.assertNotIn("cookies", sess.calls[1][1])  # HTML 降级请求同样无 cookie

    def test_cookie_normalize_edittocookie(self):
        """B3: expirationDate→expires / no_restriction→None / domain 去点 /
        session 无 expires / secure+httpOnly 透传"""
        out = _normalize_cookie(EDIT_THIS_COOKIES[0])
        self.assertEqual(out["name"], "__zp_stoken__")
        self.assertEqual(out["value"], "SECRET_TOKEN_VALUE")
        self.assertEqual(out["domain"], "zhipin.com")      # 前导点去除
        self.assertEqual(out["expires"], 1893456000.0)     # unix 秒
        self.assertEqual(out["sameSite"], "None")          # no_restriction
        self.assertTrue(out["secure"])
        self.assertTrue(out["httpOnly"])
        self.assertEqual(out["path"], "/")

        out2 = _normalize_cookie(EDIT_THIS_COOKIES[1])
        self.assertEqual(out2["domain"], "www.zhipin.com")
        self.assertEqual(out2["sameSite"], "Lax")
        self.assertNotIn("secure", out2)                   # false → 省略

        out3 = _normalize_cookie(EDIT_THIS_COOKIES[2])
        self.assertEqual(out3["sameSite"], "Strict")
        self.assertNotIn("expires", out3)                  # session=True → 无 expires

    def test_cookie_normalize_drops_non_zhipin_and_missing_fields(self):
        """B4: 非 zhipin.com 域 / 缺 name-value → None(丢弃)"""
        self.assertIsNone(_normalize_cookie(
            {"name": "x", "value": "1", "domain": "evil.example.com"}))
        self.assertIsNone(_normalize_cookie({"name": "", "value": "1"}))
        self.assertIsNone(_normalize_cookie({"name": "x"}))   # value 缺失
        self.assertIsNone(_normalize_cookie("not a dict"))

    def test_cookie_all_dropped_means_none(self):
        """B4b: 全被丢弃 → 加载结果为 None(无 cookie 跑)"""
        from intel.collectors.boss_zhipin import _load_cookies
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_cookies(tmp, [{"name": "x", "value": "1",
                                         "domain": "evil.com"}])
            with self.assertLogs("intel.boss_zhipin", level="WARNING"):
                self.assertIsNone(_load_cookies(path))


# ---------- B5-B9: 注入 / 详情 JD 采集 ----------

class DetailCrawlTest(unittest.TestCase):

    def test_crawl_injects_cookies(self):
        """B5: crawl 注入 cookie — 裸 requests 会话(HTML 降级)收到 dict 格式(design §3.3)"""
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_cookies(tmp, EDIT_THIS_COOKIES)
            c = BossZhipinCollector(query="x", output_dir=tmp, cookies_path=path,
                                    details=False)
            sess = FakeSession([FakeResponse(json_body=None),
                                FakeResponse(_page(_card("1")))])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"):
                c.crawl(sess)
        _url, kwargs = sess.calls[1]          # HTML 降级请求(裸 requests → dict 格式)
        sent = kwargs["cookies"]
        self.assertIsInstance(sent, dict)     # 不再传 playwright list(原 TypeError 根因)
        self.assertEqual(sent["__zp_stoken__"], "SECRET_TOKEN_VALUE")
        self.assertEqual(sent["wt2"], "SECRET_WT2_VALUE")
        self.assertEqual(sent["zp_session"], "SECRET_SESSION_VALUE")

    def test_detail_fetch_extracts_jd_full(self):
        """B6: 列表页卡片 → 第 2 次 get 到详情 URL → jd_full 取 job-sec-text"""
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_cookies(tmp, EDIT_THIS_COOKIES)
            c = BossZhipinCollector(query="x", output_dir=tmp, cookies_path=path,
                                    details=True)
            sess = _html_session(
                _card("1", title="大模型工程师"),
                detail=_detail(jd='<div class="job-sec-text">'
                                  '岗位职责：开发大模型推理引擎</div>'))
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"):
                items = c.crawl(sess)
            data = _read_joblist(tmp)
        rec = items[0].raw_data["job"]
        self.assertEqual(rec["jd_full"], "岗位职责：开发大模型推理引擎")
        self.assertEqual(rec["jd_status"], "ok")
        self.assertEqual(sess.calls[2][0], "https://www.zhipin.com/job_detail/1.html")
        self.assertIn("cookies", sess.calls[2][1])  # HTML 详情请求同样注入 cookie(dict)
        self.assertEqual(data["stats"]["detail_fetched"], 1)

    def test_jd_extract_multilevel_fallback(self):
        """B7: L0 缺失→L1; L1 缺失→L2 职位描述标题捕获; 全缺→empty"""
        self.assertEqual(_extract_jd_full('<div class="job-sec-text">L0 文本</div>'),
                         ("L0 文本", "ok"))
        self.assertEqual(_extract_jd_full('<div class="job-detail-section">'
                                          '<p>L1 岗位职责</p></div>'),
                         ("L1 岗位职责", "ok"))
        self.assertEqual(_extract_jd_full('<div class="job-description">'
                                          '<p>L1b 内容</p></div>'),
                         ("L1b 内容", "ok"))
        self.assertEqual(_extract_jd_full('<h3>职位描述</h3><div>L2 全文内容</div>'
                                          '<h3>任职要求</h3>'),
                         ("L2 全文内容", "ok"))
        self.assertEqual(_extract_jd_full("<html>无任何容器</html>"), ("", "empty"))
        self.assertEqual(_extract_jd_full('<div class="job-sec-text">  </div>'),
                         ("", "empty"))  # 空内容不虚构

    def test_detail_failure_soft(self):
        """B8: 详情 get 抛异常 → jd_full="", jd_status="failed", 岗位仍返回"""
        with tempfile.TemporaryDirectory() as tmp:
            c = BossZhipinCollector(query="x", output_dir=tmp,
                                    cookies_path="/nonexistent/cookies.json",
                                    details=True)
            sess = FakeSession([FakeResponse(json_body=None),
                                FakeResponse(_page(_card("2", title="幸存岗位"))),
                                RuntimeError("detail network down")])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"):
                items = c.crawl(sess)
            data = _read_joblist(tmp)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "幸存岗位")
        rec = items[0].raw_data["job"]
        self.assertEqual(rec["jd_full"], "")
        self.assertEqual(rec["jd_status"], "failed")
        self.assertEqual(data["stats"]["detail_failed"], 1)

    def test_detail_empty_no_fabrication(self):
        """B9: 详情页无 JD 容器(含反爬验证页) → jd_full="", jd_status="empty" """
        with tempfile.TemporaryDirectory() as tmp:
            c = BossZhipinCollector(query="x", output_dir=tmp,
                                    cookies_path="/nonexistent/cookies.json",
                                    details=True)
            sess = FakeSession([FakeResponse(json_body=None),
                                FakeResponse(_page(_card("3"))),
                                FakeResponse("<html>请完成安全验证</html>")])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"):
                items = c.crawl(sess)
            data = _read_joblist(tmp)
        self.assertEqual(len(items), 1)
        rec = items[0].raw_data["job"]
        self.assertEqual(rec["jd_full"], "")
        self.assertEqual(rec["jd_status"], "empty")
        self.assertEqual(data["stats"]["detail_empty"], 1)


# ---------- B10-B12: 增量去重 ----------

class IncrementalDedupTest(unittest.TestCase):

    def _history_rec(self, pid, jd_full="历史 JD 文本"):
        url = f"https://www.zhipin.com/job_detail/{pid}.html"
        return {"job_title": "同岗位", "url": url, "link_hash": _link_hash(url),
                "jd_full": jd_full, "jd_status": "ok"}

    def test_incremental_dedup_skip_history(self):
        """B10: 历史含同 hash → 重跑 skipped_existing>0, new_jobs=0, merge 无重复"""
        html = _page(_card("9", title="同岗位"))
        with tempfile.TemporaryDirectory() as tmp:
            _write_history(tmp, [self._history_rec("9")])
            c = BossZhipinCollector(query="x", output_dir=tmp,
                                    cookies_path="/nonexistent/cookies.json",
                                    details=False)
            sess = FakeSession([_api_fail(), FakeResponse(html)])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"):
                items = c.crawl(sess)
            data = _read_joblist(tmp)
        self.assertEqual(len(items), 1)             # 累积快照: 含 prior 岗位
        self.assertEqual(items[0].title, "同岗位")
        self.assertEqual(data["stats"]["skipped_existing"], 1)
        self.assertEqual(data["stats"]["new_jobs"], 0)
        self.assertEqual(data["stats"]["unique_jobs"], 1)
        self.assertEqual(len(data["jobs"]), 1)
        self.assertEqual(data["jobs"][0]["jd_full"], "历史 JD 文本")  # prior 保留
        hashes = [j["link_hash"] for j in data["jobs"]]
        self.assertEqual(len(hashes), len(set(hashes)))  # link_hash 无重复

    def test_force_refetch(self):
        """B11: force=True → 忽略历史全量重采(new_jobs>0, skipped_existing=0)"""
        html = _page(_card("9", title="同岗位"))
        with tempfile.TemporaryDirectory() as tmp:
            _write_history(tmp, [self._history_rec("9")])
            c = BossZhipinCollector(query="x", output_dir=tmp,
                                    cookies_path="/nonexistent/cookies.json",
                                    details=False, force=True)
            sess = FakeSession([_api_fail(), FakeResponse(html)])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"):
                items = c.crawl(sess)
            data = _read_joblist(tmp)
        self.assertEqual(len(items), 1)
        self.assertEqual(data["stats"]["new_jobs"], 1)
        self.assertEqual(data["stats"]["skipped_existing"], 0)
        self.assertEqual(data["jobs"][0]["jd_status"], "skipped")  # details=False 跳过

    def test_history_missing_or_corrupt(self):
        """B12: 历史缺失/坏 JSON → 全量当新, 不崩"""
        # 缺失(空目录)
        with tempfile.TemporaryDirectory() as tmp:
            c = BossZhipinCollector(query="x", output_dir=tmp,
                                    cookies_path="/nonexistent/cookies.json",
                                    details=False)
            sess = FakeSession([_api_fail(), FakeResponse(_page(_card("1")))])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"):
                items = c.crawl(sess)
            data = _read_joblist(tmp)
            self.assertEqual(len(items), 1)
            self.assertEqual(data["stats"]["new_jobs"], 1)
        # 坏 JSON
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, _today(), "joblist.json")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write("{broken{{{")
            c = BossZhipinCollector(query="x", output_dir=tmp,
                                    cookies_path="/nonexistent/cookies.json",
                                    details=False)
            sess = FakeSession([_api_fail(), FakeResponse(_page(_card("1")))])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"), \
                 self.assertLogs("intel.boss_zhipin", level="WARNING"):
                items = c.crawl(sess)
            data = _read_joblist(tmp)
            self.assertEqual(len(items), 1)
            self.assertEqual(data["stats"]["new_jobs"], 1)


# ---------- B13-B16: 安全 / 参数 / 产物 ----------

class ArtifactAndSafetyTest(unittest.TestCase):

    def test_artifact_jd_full_and_no_cookie_leak(self):
        """B13: 产物含 jd_full; 序列化全文无 cookie 值/名泄漏"""
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_cookies(tmp, EDIT_THIS_COOKIES)
            c = BossZhipinCollector(query="x", output_dir=tmp, cookies_path=path,
                                    details=True)
            sess = FakeSession([_api_fail(), FakeResponse(_page(_card("1"))),
                                FakeResponse(_detail())])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"):
                c.crawl(sess)
            data = _read_joblist(tmp)
        blob = json.dumps(data, ensure_ascii=False)
        self.assertTrue(data["jobs"][0]["jd_full"])
        self.assertEqual(data["jobs"][0]["jd_status"], "ok")
        for secret in ("SECRET_TOKEN_VALUE", "SECRET_WT2_VALUE",
                       "SECRET_SESSION_VALUE", "__zp_stoken__"):
            self.assertNotIn(secret, blob)      # 产物无 cookie 痕迹

    def test_cookies_path_env_and_kwarg_precedence(self):
        """B14: 构造 kwarg > 环境变量 > 代码默认"""
        with mock.patch.dict(os.environ, {"BN_BOSS_COOKIES_PATH": "/env/cookies.json"},
                             clear=True):
            c = BossZhipinCollector()
            self.assertEqual(c.cookies_path, "/env/cookies.json")
            c2 = BossZhipinCollector(cookies_path="/kw/cookies.json")
            self.assertEqual(c2.cookies_path, "/kw/cookies.json")

    def test_no_cookie_value_in_logs(self):
        """B15: crawl 日志(含 info/warning)不含 cookie 值/名"""
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_cookies(tmp, EDIT_THIS_COOKIES)
            c = BossZhipinCollector(query="x", output_dir=tmp, cookies_path=path,
                                    details=True)
            sess = FakeSession([_api_fail(), FakeResponse(_page(_card("1"))),
                                FakeResponse(_detail())])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"), \
                 self.assertLogs("intel.boss_zhipin", level="INFO") as cm:
                c.crawl(sess)
        blob = "\n".join(cm.output)
        self.assertIn("加载 BOSS cookie", blob)   # 仅报条数
        for secret in ("SECRET_TOKEN_VALUE", "SECRET_WT2_VALUE",
                       "SECRET_SESSION_VALUE", "__zp_stoken__"):
            self.assertNotIn(secret, blob)

    def test_params_details_force_in_artifact(self):
        """B16: 产物 params.details/force 落盘"""
        with tempfile.TemporaryDirectory() as tmp:
            c = BossZhipinCollector(query="x", output_dir=tmp,
                                    cookies_path="/nonexistent/cookies.json",
                                    details=True, force=False)
            sess = FakeSession([_api_fail(), FakeResponse(_page(_card("1")))])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"):
                c.crawl(sess)
            data = _read_joblist(tmp)
        self.assertIs(data["params"]["details"], True)
        self.assertIs(data["params"]["force"], False)


if __name__ == "__main__":
    unittest.main()
