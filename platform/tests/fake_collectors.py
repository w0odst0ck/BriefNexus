"""测试用假采集器 — 替代真实网络源,覆盖成功/空/网络错误/限流/内部错误/慢任务/自动重试/并发探针。"""
import threading
import time

import requests
from intel.core.base import BaseCollector, NewsItem


class OkCollector(BaseCollector):
    """正常返回 3 条结果,并声明 PARAM_SCHEMA(校验场景用)"""
    source_name = "ok"
    display_name = "OK Source"
    domains = ["test", "finance"]
    PARAM_SCHEMA = {"max_age": {"type": "int", "min": 1, "max": 90}}

    def crawl(self, sess):
        items = []
        for i in range(3):
            items.append(NewsItem(
                title=f"测试标题{i + 1}",
                url=f"https://example.com/{i + 1}",
                summary=f"摘要{i + 1}",
                source=self.display_name,
                domain="测试",
            ))
        return items


class LooseCollector(BaseCollector):
    """无 PARAM_SCHEMA → 宽松模式(不校验),返回 1 条"""
    source_name = "loose"
    display_name = "Loose Source"

    def crawl(self, sess):
        return [NewsItem(title="宽松模式", url="https://example.com/loose", source=self.display_name)]


class EmptyCollector(BaseCollector):
    """返回空列表 → source_empty"""
    source_name = "empty"
    display_name = "Empty Source"

    def crawl(self, sess):
        return []


class NetworkErrorCollector(BaseCollector):
    """抛 requests 超时 → network"""
    source_name = "net_err"
    display_name = "Network Error"

    def crawl(self, sess):
        raise requests.Timeout("connect timeout")


class RateLimitedCollector(BaseCollector):
    """抛 HTTP 429 → rate_limited"""
    source_name = "rate_limited"
    display_name = "Rate Limited"

    def crawl(self, sess):
        resp = requests.Response()
        resp.status_code = 429
        raise requests.HTTPError("429 Too Many Requests", response=resp)


class InternalErrorCollector(BaseCollector):
    """抛普通异常 → internal"""
    source_name = "internal_err"
    display_name = "Internal Error"

    def crawl(self, sess):
        raise ValueError("boom")


class SlowCollector(BaseCollector):
    """可配置时长的慢任务(取消 / 超时测试用)"""
    duration = 2.0

    def crawl(self, sess):
        time.sleep(type(self).duration)
        return [NewsItem(title="慢结果", url="https://example.com/slow", source=self.display_name)]


class FailOnceCollector(BaseCollector):
    """第一次失败、第二次成功(自动重试验证)"""
    calls = 0

    def crawl(self, sess):
        type(self).calls += 1
        if type(self).calls == 1:
            raise ValueError("第一次失败")
        return [NewsItem(title="重试成功", url="https://example.com/ok", source=self.display_name)]


class LockProbeCollector(BaseCollector):
    """记录最大并发执行数(每源锁验证)"""
    _active = 0
    _max_active = 0
    _guard = threading.Lock()

    def crawl(self, sess):
        with type(self)._guard:
            type(self)._active += 1
            type(self)._max_active = max(type(self)._max_active, type(self)._active)
        try:
            time.sleep(0.8)
        finally:
            with type(self)._guard:
                type(self)._active -= 1
        return [NewsItem(title="并发探针", url="https://example.com/probe", source=self.display_name)]


class ModuleExtCollector(BaseCollector):
    """随请求 collector.module 引用提交的采集器(D16):

    不入配置(非内置源),source_name 唯一 → /sources 动态发现可见。
    """
    source_name = "module_ext"
    display_name = "Module Ext Source"

    def crawl(self, sess):
        return [NewsItem(title="模块引用", url="https://example.com/module_ext",
                         source=self.source_name)]
