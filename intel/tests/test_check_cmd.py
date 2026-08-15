"""intel check 子命令单测 — stub 采集器,不发起任何真实网络请求

覆盖: 全 ok 退出码/行数、failed 退出码、--domain 过滤透传、
      --timeout 硬超时生效(不挂死)、health-history.jsonl 追加格式(临时目录)。
"""
import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from datetime import datetime
from unittest import mock

from intel import cli
from intel.core.base import CST, BaseCollector, NewsItem


class OkStub(BaseCollector):
    """正常采集器: 返回 2 条"""
    source_name = "stub_ok"
    display_name = "Stub OK"
    domains = ["stub"]

    def crawl(self, sess):
        return [NewsItem(title=f"stub item {i}", url=f"http://stub/{i}") for i in range(2)]


class FailStub(BaseCollector):
    """异常采集器: crawl 抛任意异常"""
    source_name = "stub_fail"
    display_name = "Stub Fail"
    domains = ["stub"]

    def crawl(self, sess):
        raise RuntimeError("stub boom")


class TimeoutStub(BaseCollector):
    """超时采集器: 挂起远超过时值,由外层硬超时兜底"""
    source_name = "stub_timeout"
    display_name = "Stub Timeout"
    domains = ["stub"]

    def crawl(self, sess):
        time.sleep(30)


def _stub_env(instances):
    """patch 注册表两个入口,让 cmd_check 只看到 stub 采集器(不碰真实注册表/网络)"""
    classes = {inst.source_name: type(inst) for inst in instances}
    return (
        mock.patch("intel.cli.get_collector_classes", return_value=classes),
        mock.patch("intel.cli.instantiate_collectors", return_value=instances),
    )


def _run_check(*args, **kwargs):
    """执行 cmd_check,捕获 stdout,返回 (退出码, 输出文本)"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.cmd_check(*args, **kwargs)
    return code, buf.getvalue()


class CheckCmdTest(unittest.TestCase):

    def setUp(self):
        """每个用例独立的临时输出目录,绝不污染 intel/output/"""
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name

    def test_all_ok_exit_zero_with_three_rows(self):
        instances = [OkStub(), OkStub(), OkStub()]
        p_classes, p_inst = _stub_env(instances)
        with p_classes, p_inst:
            code, out = _run_check(timeout=5, interval=0, output_dir=self.tmp)

        self.assertEqual(code, 0)
        self.assertEqual(out.count("stub_ok"), 3)  # 三个源各一行
        self.assertNotIn("failed", out)

    def test_any_failed_exit_one(self):
        instances = [OkStub(), FailStub(), TimeoutStub()]
        p_classes, p_inst = _stub_env(instances)
        with p_classes, p_inst:
            code, out = _run_check(timeout=1, interval=0, output_dir=self.tmp)

        self.assertEqual(code, 1)
        self.assertIn("failed", out)
        self.assertIn("RuntimeError: stub boom", out)

    def test_domain_filter_passed_through(self):
        instances = [OkStub()]
        p_classes, p_inst = _stub_env(instances)
        with p_classes as m_classes, p_inst:
            code, out = _run_check(domain="finance", timeout=5, interval=0, output_dir=self.tmp)

        self.assertEqual(code, 0)
        # 过滤逻辑本身在 registry.get_collector_classes 内部;此处验证透传
        _, kwargs = m_classes.call_args
        self.assertEqual(kwargs.get("domains"), "finance")
        self.assertIn("stub_ok", out)
        self.assertNotIn("stub_fail", out)

    def test_timeout_marks_failed_without_hang(self):
        instances = [TimeoutStub()]
        p_classes, p_inst = _stub_env(instances)
        with p_classes, p_inst:
            t0 = time.monotonic()
            code, out = _run_check(timeout=1, interval=0, output_dir=self.tmp)
            elapsed = time.monotonic() - t0

        self.assertEqual(code, 1)
        # 1s 硬超时兜底: 总耗时远小于 stub 的 30s 挂起,未挂死
        self.assertLess(elapsed, 5)
        self.assertIn("TimeoutError", out)

    def test_history_jsonl_appends_correct_format(self):
        instances = [OkStub(), FailStub()]
        p_classes, p_inst = _stub_env(instances)

        with p_classes, p_inst:
            code1, _ = _run_check(timeout=5, interval=0, output_dir=self.tmp)
            code2, _ = _run_check(timeout=5, interval=0, output_dir=self.tmp)

            self.assertEqual(code1, 1)
            self.assertEqual(code2, 1)

            hist = os.path.join(self.tmp, "health-history.jsonl")
            daily = os.path.join(self.tmp, f"health-{datetime.now(CST).strftime('%Y-%m-%d')}.json")

            with open(hist, encoding="utf-8") as f:
                lines = f.read().strip().splitlines()
            self.assertEqual(len(lines), 4)  # 两次运行 × 2 源,追加而非覆盖
            row = json.loads(lines[0])
            self.assertEqual(
                set(row), {"timestamp", "source", "status", "items", "duration", "error"}
            )
            self.assertEqual(row["source"], "stub_ok")
            self.assertEqual(row["status"], "ok")
            self.assertEqual(row["items"], 2)
            self.assertIsNone(row["error"])
            # ISO 时间戳(带时区偏移)
            self.assertRegex(
                row["timestamp"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:00$"
            )

            with open(daily, encoding="utf-8") as f:
                summary = json.load(f)
            self.assertEqual(summary["total"], 2)
            self.assertEqual(summary["ok"], 1)
            self.assertEqual(summary["failed"], 1)

    def test_interval_sleeps_between_sources(self):
        instances = [OkStub(), OkStub(), OkStub()]
        p_classes, p_inst = _stub_env(instances)
        with p_classes, p_inst, mock.patch("intel.cli.time.sleep") as m_sleep:
            code, _ = _run_check(timeout=5, interval=0.4, output_dir=self.tmp)

        self.assertEqual(code, 0)
        # 3 个源 → 2 次间隔(最后一个源之后不 sleep)
        self.assertEqual(m_sleep.call_count, 2)


if __name__ == "__main__":
    unittest.main()
