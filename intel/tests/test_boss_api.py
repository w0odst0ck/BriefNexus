"""boss_zhipin API 化改造单测(A1-A20)— cookie 双格式 / API 主路线 / 降级 / 产物

覆盖(design §3.2 表格):
  A1  cookie 双格式(Cookies.requests dict + .playwright list, name 集合一致)
  A2  cookie 缺失 → API 列表请求无 cookies kwarg(M2a 语义)
  A3  _build_url API 模式(joblist.json + query/city/page/pageSize=30); HTML 模式不变
  A4  列表字段映射全量(job_title/company/industry/scale/experience/education/area/url/
      link_hash/encryptJobId/lid/securityId; area 拼接与缺失兜底)
  A5  salary 恒置 ""(不读取/猜测任何 salary 字段)
  A6  缺 encryptJobId/jobName → 跳过该条
  A7  详情 postDescription 提取(含 < > 字面字符不被破坏)→ jd_status=="ok"
  A8  postDescription 空/缺失 → jd_status=="empty"
  A9  详情 code≠0 → jd_status=="failed"
  A10 详情网络异常 → jd_status=="failed"、岗位仍返回、detail_failed==1
  A11 API 非 200 → 降级 HTML(failed_requests≥1、HTML 成功、requests 计入)
  A12 API 非 JSON → 降级 HTML
  A13 API code≠0 / jobList 非 list → 降级 HTML
  A14 API+HTML 均无卡片 → blocked_queries==1、jobs==[]、结构完整
  A15 增量去重(历史 hash → skipped_existing>0、new_jobs==0、merge 无重复)
  A16 force 重采(忽略历史、new_jobs>0、skipped_existing==0)
  A17 产物结构(schema_version 1.0、jobs 含 industry/scale、不含 encryptJobId/lid/securityId)
  A18 cookie 零泄漏(产物 + 日志 grep 无 cookie 值/名)
  A19 HTML 降级卡片走 HTML 详情(job_detail/<pid>.html 而非 detail.json)
  A20 详情请求 URL 参数(jobId/lid/securityId); 有 cookie 时带 cookies=dict
"""
import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock

from intel.collectors.boss_zhipin import (
    API_PAGE_SIZE,
    BossZhipinCollector,
    Cookies,
    _build_api_detail_url,
    _build_url,
    _cookies_for_html,
    _is_render_aware,
    _link_hash,
    _load_cookies,
    _parse_detail_json,
    _parse_joblist_json,
    _raw_session,
)
from intel.core.base import CST

# ---------- stub helpers(design §3.1) ----------


class FakeResponse:
    """requests.Response 替身: text/status/json_body; json_body=None → json() 抛 ValueError"""

    def __init__(self, text="", status=200, json_body=None):
        self._text, self._status, self._json = text, status, json_body

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


# ---------- HTML builders / helpers ----------

def _card(pid, title="岗位", company="某某科技", salary="30-50K",
          exp="3-5年", edu="本科", area="上海·浦东新区"):
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


# 实测 EditThisCookie 导出格式(子集, 含核心 token, 与 M2b 共用)
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


def _api_fail():
    """API 失败响应(非 JSON)"""
    return FakeResponse(json_body=None)


def _job(**fields):
    """jobList 元素 dict(默认含全部 API 字段)"""
    base = {
        "encryptJobId": "abc123",
        "jobName": "大模型工程师",
        "brandName": "某某科技",
        "brandIndustry": "人工智能",
        "brandScaleName": "1000-9999人",
        "jobExperience": "3-5年",
        "jobDegree": "本科",
        "cityName": "上海",
        "areaDistrict": "浦东新区",
        "businessDistrict": "张江",
        "lid": "lid-1",
        "securityId": "sec-1",
    }
    base.update(fields)
    return base


def _payload(code=0, jobs=None):
    return {"code": code, "zpData": {"jobList": jobs if jobs is not None else []}}


def _detail_payload(code=0, post_description=None):
    return {"code": code, "zpData": {"jobInfo": {"postDescription": post_description}}}


def _html_session(*cards, detail=None):
    """HTML 降级路径专用: 先 API 失败、再 HTML 列表、可选 HTML 详情"""
    responses = [_api_fail(), FakeResponse(_page(*cards))]
    if detail is not None:
        responses.append(FakeResponse(detail))
    return FakeSession(responses)


# ---------- A1-A3: cookie 双格式 / URL 构造 ----------

class CookieDualFormatTest(unittest.TestCase):

    def test_cookie_dual_format(self):
        """A1: _load_cookies 返回 Cookies; .requests 为 {name:value} dict;
        .playwright 为归一化 list; 两格式 name 集合一致"""
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_cookies(tmp, EDIT_THIS_COOKIES)
            ck = _load_cookies(path)
        self.assertIsInstance(ck, Cookies)
        self.assertIsInstance(ck.requests, dict)
        self.assertEqual(ck.requests["__zp_stoken__"], "SECRET_TOKEN_VALUE")
        self.assertEqual(ck.requests["wt2"], "SECRET_WT2_VALUE")
        self.assertEqual(ck.requests["zp_session"], "SECRET_SESSION_VALUE")
        self.assertIsInstance(ck.playwright, list)
        by_name = {n["name"]: n for n in ck.playwright}
        # playwright 格式保留 B3 归一化字段
        self.assertEqual(by_name["__zp_stoken__"]["domain"], "zhipin.com")
        self.assertEqual(by_name["__zp_stoken__"]["sameSite"], "None")
        self.assertEqual(by_name["wt2"]["sameSite"], "Lax")
        self.assertEqual(by_name["zp_session"]["sameSite"], "Strict")
        self.assertNotIn("expires", by_name["zp_session"])   # session=True → 无 expires
        # 两格式 name 集合一致(R7)
        self.assertEqual(set(ck.requests), {n["name"] for n in ck.playwright})

    def test_cookie_missing_api_no_cookies_kwarg(self):
        """A2: cookie 文件缺失 → warning; API 与 HTML 请求均无 cookies kwarg(M2a 语义)"""
        with tempfile.TemporaryDirectory() as tmp:
            c = BossZhipinCollector(query="x", output_dir=tmp,
                                    cookies_path="/nonexistent/cookies.json",
                                    details=False)
            sess = _html_session(_card("1"))
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"), \
                 self.assertLogs("intel.boss_zhipin", level="WARNING"):
                items = c.crawl(sess)
        self.assertEqual(len(items), 1)
        self.assertEqual(len(sess.calls), 2)  # API 失败 + HTML 降级
        for _url, kwargs in sess.calls:
            self.assertNotIn("cookies", kwargs)

    def test_build_url_api_mode(self):
        """A3: api=True → joblist.json + query/city/page/pageSize=30; HTML 模式不变"""
        url = _build_url("RAG", "101020100", 2, api=True)
        self.assertIn("joblist.json", url)
        self.assertIn("query=RAG", url)
        self.assertIn("city=101020100", url)
        self.assertIn("page=2", url)
        self.assertIn(f"pageSize={API_PAGE_SIZE}", url)
        html = _build_url("RAG", "101020100", 1)
        self.assertIn("web/geek/job", html)
        self.assertNotIn("joblist.json", html)
        self.assertNotIn("pageSize", html)
        self.assertIn("query=RAG", html)


# ---------- A4-A6: 列表字段映射 ----------

class JoblistParseTest(unittest.TestCase):

    def test_field_mapping_full(self):
        """A4: API 字段 → 产物字段全量映射 + area 拼接"""
        q = {"direction_id": "ai-app-llm", "direction_name": "AI应用/大模型"}
        recs = _parse_joblist_json(_payload(jobs=[_job()]), "大模型", q)
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec["job_title"], "大模型工程师")
        self.assertEqual(rec["company"], "某某科技")
        self.assertEqual(rec["industry"], "人工智能")
        self.assertEqual(rec["scale"], "1000-9999人")
        self.assertEqual(rec["experience"], "3-5年")
        self.assertEqual(rec["education"], "本科")
        self.assertEqual(rec["area"], "上海·浦东新区·张江")
        self.assertEqual(rec["url"], "https://www.zhipin.com/job_detail/abc123.html")
        self.assertEqual(rec["link_hash"], _link_hash(rec["url"]))
        self.assertEqual(rec["query"], "大模型")
        self.assertEqual(rec["direction_id"], "ai-app-llm")
        self.assertEqual(rec["direction_name"], "AI应用/大模型")
        self.assertEqual(rec["encryptJobId"], "abc123")
        self.assertEqual(rec["lid"], "lid-1")
        self.assertEqual(rec["securityId"], "sec-1")

    def test_area_partial_and_missing(self):
        """A4b: area 部分缺失拼现有, 全缺 ''"""
        recs = _parse_joblist_json(_payload(jobs=[_job(cityName="上海",
                                                       areaDistrict="",
                                                       businessDistrict="张江")]),
                                   "x", {})
        self.assertEqual(recs[0]["area"], "上海·张江")
        recs2 = _parse_joblist_json(_payload(jobs=[_job(cityName=None,
                                                        areaDistrict="",
                                                        businessDistrict=None)]),
                                    "x", {})
        self.assertEqual(recs2[0]["area"], "")

    def test_salary_always_empty(self):
        """A5: salary 恒置 '' — 即使载荷含 salaryDesc 也不读取/猜测(字体反爬)"""
        jobs = [_job(salaryDesc="30-50K·13薪", salary="50K以上")]
        rec = _parse_joblist_json(_payload(jobs=jobs), "x", {})[0]
        self.assertEqual(rec["salary"], "")

    def test_missing_key_fields_skipped(self):
        """A6: 缺 encryptJobId / jobName → 跳过该条, 不虚构; 其余条正常"""
        jobs = [
            _job(encryptJobId=""),                       # 缺 eid → 跳过
            _job(jobName=""),                            # 缺 name → 跳过
            _job(encryptJobId="e2", jobName="正常岗位"),  # 正常
        ]
        recs = _parse_joblist_json(_payload(jobs=jobs), "x", {})
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["job_title"], "正常岗位")
        self.assertEqual(recs[0]["encryptJobId"], "e2")


# ---------- A7-A10: 详情 API ----------

class DetailApiTest(unittest.TestCase):

    def test_detail_extract_ok(self):
        """A7: postDescription → jd_full(含 < > 字面字符不被 _strip 破坏), jd_status=="ok" """
        jd = "岗位职责：<p>开发大模型推理引擎</p> 与 调优 > 阈值"
        jd_full, status = _parse_detail_json(_detail_payload(post_description=jd))
        self.assertEqual(jd_full, "岗位职责：<p>开发大模型推理引擎</p> 与 调优 > 阈值")
        self.assertEqual(status, "ok")

    def test_detail_empty(self):
        """A8: postDescription 空/缺失 → jd_status=="empty"、jd_full=="" """
        self.assertEqual(_parse_detail_json(_detail_payload(post_description="")),
                         ("", "empty"))
        self.assertEqual(_parse_detail_json(_detail_payload(post_description=None)),
                         ("", "empty"))
        self.assertEqual(_parse_detail_json({"code": 0, "zpData": {}}), ("", "empty"))

    def test_detail_code_nonzero(self):
        """A9: 详情 code≠0 → jd_status=="failed" """
        self.assertEqual(_parse_detail_json(_detail_payload(code=1001,
                                                            post_description="x")),
                         ("", "failed"))

    def test_detail_network_error_soft(self):
        """A10: 详情 API 网络异常 → jd_status=="failed"、岗位仍返回、detail_failed==1"""
        with tempfile.TemporaryDirectory() as tmp:
            c = BossZhipinCollector(query="x", output_dir=tmp,
                                    cookies_path="/nonexistent/cookies.json",
                                    details=True)
            sess = FakeSession([FakeResponse(json_body=_payload(jobs=[_job()])),
                                RuntimeError("detail boom")])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"):
                items = c.crawl(sess)
            data = _read_joblist(tmp)
        self.assertEqual(len(items), 1)
        rec = items[0].raw_data["job"]
        self.assertEqual(rec["jd_full"], "")
        self.assertEqual(rec["jd_status"], "failed")
        self.assertEqual(data["stats"]["detail_failed"], 1)


# ---------- A11-A14: 降级链 ----------

class FallbackTest(unittest.TestCase):

    def _crawl_fallback(self, api_resp, html_resp, details=False, queries=None):
        with tempfile.TemporaryDirectory() as tmp:
            c = BossZhipinCollector(queries=queries or ["x"], output_dir=tmp,
                                    cookies_path="/nonexistent/cookies.json",
                                    details=details)
            sess = FakeSession([api_resp, html_resp])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"):
                items = c.crawl(sess)
            data = _read_joblist(tmp)
        return items, data, sess

    def test_api_http_error_falls_back_html(self):
        """A11: API 非 200 → failed_requests≥1、HTML 卡片解析成功、requests 计入"""
        items, data, sess = self._crawl_fallback(
            FakeResponse(json_body=None, status=500), FakeResponse(_page(_card("1"))))
        self.assertEqual(len(items), 1)
        self.assertEqual(len(sess.calls), 2)
        self.assertGreaterEqual(data["stats"]["failed_requests"], 1)
        self.assertEqual(data["stats"]["requests"], 1)   # HTML 成功页计入
        self.assertEqual(items[0].title, "岗位")

    def test_api_non_json_falls_back_html(self):
        """A12: API 非 JSON → 降级 HTML, HTML 卡片成功"""
        items, data, sess = self._crawl_fallback(
            _api_fail(), FakeResponse(_page(_card("1"))))
        self.assertEqual(len(items), 1)
        self.assertEqual(len(sess.calls), 2)
        self.assertGreaterEqual(data["stats"]["failed_requests"], 1)
        self.assertEqual(items[0].raw_data["job"]["url"],
                         "https://www.zhipin.com/job_detail/1.html")

    def test_api_code_nonzero_falls_back_html(self):
        """A13a: API code≠0 → 降级 HTML"""
        items, data, _ = self._crawl_fallback(
            FakeResponse(json_body=_payload(code=1001, jobs=[])),
            FakeResponse(_page(_card("1"))))
        self.assertEqual(len(items), 1)
        self.assertEqual(data["stats"]["failed_requests"], 1)

    def test_api_joblist_not_list_falls_back_html(self):
        """A13b: jobList 非 list(结构异常)→ 降级 HTML(E5)"""
        items, data, _ = self._crawl_fallback(
            FakeResponse(json_body={"code": 0, "zpData": {"jobList": "nope"}}),
            FakeResponse(_page(_card("1"))))
        self.assertEqual(len(items), 1)
        self.assertEqual(data["stats"]["failed_requests"], 1)

    def test_api_and_html_both_empty_blocked(self):
        """A14: API 失败 + HTML 无卡片 → blocked_queries==1、jobs==[]、结构完整"""
        with tempfile.TemporaryDirectory() as tmp:
            c = BossZhipinCollector(query="x", output_dir=tmp,
                                    cookies_path="/nonexistent/cookies.json")
            sess = FakeSession([FakeResponse(json_body=_payload(code=1001)),
                                FakeResponse("<html>验证码, 请完成安全验证</html>")])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"), \
                 self.assertLogs("intel.boss_zhipin", level="WARNING"):
                items = c.crawl(sess)
            data = _read_joblist(tmp)
        self.assertEqual(items, [])
        self.assertEqual(data["jobs"], [])
        self.assertEqual(data["stats"]["blocked_queries"], 1)
        self.assertEqual(data["stats"]["requests"], 0)
        for key in ("schema_version", "source", "stats", "jobs"):
            self.assertIn(key, data)

    def test_api_empty_joblist_legal_empty_page(self):
        """A6b: code==0 且 jobList 空 = 合法空页(非降级, 不计 failed, 停分页)"""
        with tempfile.TemporaryDirectory() as tmp:
            c = BossZhipinCollector(query="x", pages=3, output_dir=tmp,
                                    cookies_path="/nonexistent/cookies.json",
                                    details=False)
            sess = FakeSession([FakeResponse(json_body=_payload(code=0, jobs=[]))])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"):
                items = c.crawl(sess)
            data = _read_joblist(tmp)
        self.assertEqual(items, [])
        self.assertEqual(len(sess.calls), 1)               # 空页 → break, 不再请求
        self.assertEqual(data["stats"]["failed_requests"], 0)
        self.assertEqual(data["stats"]["blocked_queries"], 0)


# ---------- A15-A16: 增量去重 / force ----------

class IncrementalDedupTest(unittest.TestCase):

    def _history_rec(self, pid, jd_full="历史 JD 文本"):
        url = f"https://www.zhipin.com/job_detail/{pid}.html"
        return {"job_title": "同岗位", "url": url, "link_hash": _link_hash(url),
                "jd_full": jd_full, "jd_status": "ok"}

    def test_incremental_dedup_skip_history(self):
        """A15: 历史含同 hash(API 卡片)→ skipped_existing>0、new_jobs==0、merge 无重复"""
        with tempfile.TemporaryDirectory() as tmp:
            _write_history(tmp, [self._history_rec("9")])
            c = BossZhipinCollector(query="x", output_dir=tmp,
                                    cookies_path="/nonexistent/cookies.json",
                                    details=False)
            sess = FakeSession([FakeResponse(json_body=_payload(
                jobs=[_job(encryptJobId="9", jobName="同岗位")]))])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"):
                items = c.crawl(sess)
            data = _read_joblist(tmp)
        self.assertEqual(len(items), 1)              # 累积快照: 含 prior 岗位
        self.assertEqual(data["stats"]["skipped_existing"], 1)
        self.assertEqual(data["stats"]["new_jobs"], 0)
        self.assertEqual(data["stats"]["unique_jobs"], 1)
        self.assertEqual(len(data["jobs"]), 1)
        self.assertEqual(data["jobs"][0]["jd_full"], "历史 JD 文本")  # prior 保留
        hashes = [j["link_hash"] for j in data["jobs"]]
        self.assertEqual(len(hashes), len(set(hashes)))

    def test_force_refetch(self):
        """A16: force=True → 忽略历史全量重采(new_jobs>0、skipped_existing==0)"""
        with tempfile.TemporaryDirectory() as tmp:
            _write_history(tmp, [self._history_rec("9")])
            c = BossZhipinCollector(query="x", output_dir=tmp,
                                    cookies_path="/nonexistent/cookies.json",
                                    details=False, force=True)
            sess = FakeSession([FakeResponse(json_body=_payload(
                jobs=[_job(encryptJobId="9", jobName="同岗位")]))])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"):
                items = c.crawl(sess)
            data = _read_joblist(tmp)
        self.assertEqual(len(items), 1)
        self.assertEqual(data["stats"]["new_jobs"], 1)
        self.assertEqual(data["stats"]["skipped_existing"], 0)
        self.assertEqual(data["jobs"][0]["jd_status"], "skipped")  # details=False


# ---------- A17-A18: 产物 / 安全 ----------

class ArtifactAndSafetyTest(unittest.TestCase):

    def test_artifact_structure(self):
        """A17: schema_version 1.0、jobs 含 industry/scale、不含 encryptJobId/lid/securityId"""
        with tempfile.TemporaryDirectory() as tmp:
            c = BossZhipinCollector(query="x", output_dir=tmp,
                                    cookies_path="/nonexistent/cookies.json",
                                    details=True)
            sess = FakeSession([
                FakeResponse(json_body=_payload(jobs=[_job()])),
                FakeResponse(json_body=_detail_payload(
                    post_description="岗位职责：开发推理引擎")),
            ])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"):
                items = c.crawl(sess)
            data = _read_joblist(tmp)
        self.assertEqual(data["schema_version"], "1.0")
        job = data["jobs"][0]
        self.assertEqual(job["industry"], "人工智能")
        self.assertEqual(job["scale"], "1000-9999人")
        for key in ("encryptJobId", "lid", "securityId"):
            self.assertNotIn(key, job)               # 临时字段不入产物(R5)
        self.assertEqual(job["jd_full"], "岗位职责：开发推理引擎")
        self.assertEqual(job["jd_status"], "ok")
        self.assertEqual(len(items), 1)

    def test_cookie_no_leak(self):
        """A18: 产物 JSON + 日志 grep 无 cookie 值/名(E27)"""
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_cookies(tmp, EDIT_THIS_COOKIES)
            c = BossZhipinCollector(query="x", output_dir=tmp, cookies_path=path,
                                    details=True)
            sess = FakeSession([
                FakeResponse(json_body=_payload(jobs=[_job()])),
                FakeResponse(json_body=_detail_payload(post_description="JD")),
            ])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"), \
                 self.assertLogs("intel.boss_zhipin", level="INFO") as cm:
                c.crawl(sess)
            data = _read_joblist(tmp)
        secrets = ("SECRET_TOKEN_VALUE", "SECRET_WT2_VALUE", "SECRET_SESSION_VALUE",
                   "__zp_stoken__", "zp_at")
        blob = json.dumps(data, ensure_ascii=False)
        for secret in secrets:
            self.assertNotIn(secret, blob)
        log_blob = "\n".join(cm.output)
        self.assertIn("加载 BOSS cookie", log_blob)   # 仅报条数
        for secret in secrets:
            self.assertNotIn(secret, log_blob)


# ---------- A19-A20: HTML 降级详情 / 详情 URL 参数 ----------

class DetailRoutingTest(unittest.TestCase):

    def test_html_card_uses_html_detail(self):
        """A19: HTML 降级卡片(无 encryptJobId)→ 详情打 job_detail/<pid>.html 而非 detail.json"""
        with tempfile.TemporaryDirectory() as tmp:
            c = BossZhipinCollector(query="x", output_dir=tmp,
                                    cookies_path="/nonexistent/cookies.json",
                                    details=True)
            sess = _html_session(_card("42", title="降级岗位"),
                                 detail=_detail(jd='<div class="job-sec-text">HTML详情JD</div>'))
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"):
                items = c.crawl(sess)
            data = _read_joblist(tmp)
        self.assertEqual(len(items), 1)
        detail_url = sess.calls[2][0]
        self.assertEqual(detail_url, "https://www.zhipin.com/job_detail/42.html")
        self.assertNotIn("detail.json", detail_url)
        rec = items[0].raw_data["job"]
        self.assertEqual(rec["jd_full"], "HTML详情JD")
        self.assertEqual(rec["jd_status"], "ok")
        self.assertEqual(data["stats"]["detail_fetched"], 1)

    def test_detail_api_url_params(self):
        """A20: API 卡片详情 → URL 含 jobId/lid/securityId; 无 cookie 时无 cookies kwarg"""
        with tempfile.TemporaryDirectory() as tmp:
            c = BossZhipinCollector(query="x", output_dir=tmp,
                                    cookies_path="/nonexistent/cookies.json",
                                    details=True)
            sess = FakeSession([
                FakeResponse(json_body=_payload(jobs=[_job()])),
                FakeResponse(json_body=_detail_payload(post_description="JD")),
            ])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"):
                c.crawl(sess)
        url = sess.calls[1][0]
        self.assertIn("detail.json", url)
        self.assertIn("jobId=abc123", url)
        self.assertIn("lid=lid-1", url)
        self.assertIn("securityId=sec-1", url)
        self.assertNotIn("cookies", sess.calls[1][1])

    def test_detail_api_with_cookies_dict(self):
        """A20b: 有 cookie 时详情 API 带 cookies=dict(requests 格式)"""
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_cookies(tmp, EDIT_THIS_COOKIES)
            c = BossZhipinCollector(query="x", output_dir=tmp, cookies_path=path,
                                    details=True)
            sess = FakeSession([
                FakeResponse(json_body=_payload(jobs=[_job()])),
                FakeResponse(json_body=_detail_payload(post_description="JD")),
            ])
            with mock.patch("intel.collectors.boss_zhipin.time.sleep"):
                c.crawl(sess)
        kwargs = sess.calls[1][1]
        self.assertIn("cookies", kwargs)
        self.assertIsInstance(kwargs["cookies"], dict)   # 裸 requests 路径 → dict
        self.assertEqual(kwargs["cookies"]["__zp_stoken__"], "SECRET_TOKEN_VALUE")


# ---------- 会话类型 duck-typing ----------

class SessionTypeTest(unittest.TestCase):

    class _RenderAware:
        """模拟 RenderAwareSession: 仅 _session 属性可见(不 import render.py)"""
        def __init__(self, inner=None):
            self._session = inner

    def test_render_aware_detection(self):
        self.assertTrue(_is_render_aware(self._RenderAware(inner=object())))
        self.assertFalse(_is_render_aware(FakeSession()))
        self.assertFalse(_is_render_aware(object()))

    def test_raw_session_unwrap(self):
        inner = FakeSession()
        self.assertIs(_raw_session(self._RenderAware(inner)), inner)  # 取内层
        bare = FakeSession()
        self.assertIs(_raw_session(bare), bare)                       # 裸原样

    def test_cookies_for_html_selects_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_cookies(tmp, EDIT_THIS_COOKIES)
            ck = _load_cookies(path)
        aware = self._RenderAware(inner=object())
        self.assertIs(_cookies_for_html(FakeSession(), ck), ck.requests)      # 裸 → dict
        self.assertIs(_cookies_for_html(aware, ck), ck.playwright)            # render → list
        self.assertIsNone(_cookies_for_html(FakeSession(), None))

    def test_build_api_detail_url(self):
        url = _build_api_detail_url("abc", "lid9", "sec7")
        self.assertIn("jobId=abc", url)
        self.assertIn("lid=lid9", url)
        self.assertIn("securityId=sec7", url)


if __name__ == "__main__":
    unittest.main()
