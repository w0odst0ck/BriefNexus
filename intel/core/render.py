"""
浏览器渲染能力 — 渲染执行器 + 渲染感知 Session（T6）

声明式启用: sources.yaml 中 `sources.<name>.render: true` 的源由 cli 侧
换用 `RenderAwareSession`, `sess.get(url)` 返回渲染后 HTML(或降级静态)。

进程隔离: 本模块仅依赖 stdlib + requests, **不 import playwright**。
真实渲染在独立 venv 的 render_worker.py 子进程中完成(每请求 launch/close)。

失败语义: `RenderExecutor.render` 绝不抛异常 —— 所有失败(超时/缺浏览器/
坏 JSON/空 HTML)归一为 `RenderResult(ok=False, error=...)`, 由
`RenderAwareSession.get` 降级为静态 requests.get, 采集器自身 try/except
再兜底为 [], 绝不中断 cmd_run/cmd_check 整体。
"""
import json
import logging
import os
import subprocess
from dataclasses import dataclass

import requests

logger = logging.getLogger("intel.render")

# 默认渲染子进程解释器: playwright 独立 venv(可用 BN_RENDER_PYTHON 覆盖)
DEFAULT_RENDER_PYTHON = os.environ.get(
    "BN_RENDER_PYTHON", "/home/l/venvs/playwright/.venv/bin/python"
)
# worker 与 render.py 同目录
DEFAULT_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "render_worker.py")


@dataclass
class RenderResult:
    """渲染结果(归一化)。ok=false 或 html 为空均视为失败(failed)。"""

    ok: bool = False
    html: str = ""
    final_url: str = ""
    status_code: int = 0
    error: str = ""

    @property
    def failed(self) -> bool:
        """渲染失败判定: 未成功或 HTML 为空(白屏/反爬空壳)"""
        return (not self.ok) or (not self.html)


class RenderExecutor:
    """渲染执行器: 子进程调用 render_worker.py, 归一化为 RenderResult。

    绝不抛异常 —— 所有错误路径均返回 RenderResult(ok=False, error=...)。
    """

    def __init__(self, python: str | None = None, worker: str | None = None,
                 default_timeout: float = 30.0, grace: float = 10.0):
        self.python = python or DEFAULT_RENDER_PYTHON
        self.worker = worker or DEFAULT_WORKER
        self.default_timeout = default_timeout
        self.grace = grace  # subprocess 外层硬超时宽限(钳制 worker 清理时间)

    def render(self, url: str, timeout: float | None = None) -> RenderResult:
        """渲染 URL → RenderResult。任何异常都不外抛。"""
        t = float(timeout) if timeout is not None else float(self.default_timeout)
        cmd = [self.python, self.worker, url, "--timeout", f"{t:g}"]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8",
                timeout=t + self.grace, check=False,
            )
        except FileNotFoundError:
            return RenderResult(ok=False, error="playwright venv python/worker 不存在")
        except subprocess.TimeoutExpired:
            return RenderResult(ok=False, error=f"渲染超时 {t}s")
        except OSError as e:
            return RenderResult(ok=False, error=f"子进程启动失败: {e}")
        except Exception as e:  # 绝不抛异常的最后兜底
            return RenderResult(ok=False, error=f"渲染子进程异常: {e}")

        if proc.returncode == 0:
            try:
                data = json.loads(proc.stdout)
                return RenderResult(
                    ok=bool(data.get("ok")),
                    html=data.get("html", "") or "",
                    final_url=data.get("final_url", "") or "",
                    status_code=_to_int(data.get("status_code")),
                    error=(data.get("error") or "")[:500],
                )
            except (json.JSONDecodeError, ValueError):
                return RenderResult(ok=False, error="worker stdout 非法 JSON")

        # returncode != 0: worker 失败时也会输出 JSON, 尝试解析透传 error
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            data = {}
        error = (data.get("error") or proc.stderr or "未知错误")[:500]
        return RenderResult(ok=False, error=error)


def _to_int(value) -> int:
    """status_code 归一化: 数值 0 不丢(None/非法值 → 0)"""
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class RenderedResponse:
    """渲染后响应 —— 满足采集器对 requests.Response 的读取面。

    契约(design §2.3.3): text/content/status_code/url/ok/headers 只读;
    encoding 可赋值(eastmoney 风格写 r.encoding); json() 兼容 cninfo 风格;
    raise_for_status() 非 2xx/3xx 抛 requests.HTTPError。
    """

    def __init__(self, result: RenderResult):
        self._text = result.html
        self._url = result.final_url or ""
        self._status = result.status_code or 200
        self.encoding = "utf-8"  # 可赋值
        self.headers = {}

    @property
    def text(self) -> str:
        return self._text

    @property
    def content(self) -> bytes:
        return self._text.encode(self.encoding or "utf-8")

    @property
    def status_code(self) -> int:
        return self._status

    @property
    def url(self) -> str:
        return self._url

    @property
    def ok(self) -> bool:
        return 200 <= self._status < 400

    def json(self, **kwargs):
        """JSON 解析渲染后文本(兼容 API 型源的 r.json() 用法)"""
        return json.loads(self._text, **kwargs)

    def raise_for_status(self):
        """非 2xx/3xx 抛 requests.HTTPError, 供采集器 r.raise_for_status()"""
        if not self.ok:
            raise requests.HTTPError(
                f"{self._status} Client/Server Error for url: {self._url}",
                response=self,
            )


class RenderAwareSession:
    """渲染感知的 Session 包装 —— 接口兼容 requests.Session。

    get(url): 渲染成功且 HTML 非空 → RenderedResponse; 否则降级静态
              `self._session.get`(warning 日志), 绝不外抛渲染异常。
    post(url): 渲染不适用 POST/API 源, 透明透传静态。
    其余属性(headers/cookies/close 等)经 __getattr__ 透传内层 session。
    """

    def __init__(self, session: requests.Session, executor: RenderExecutor,
                 timeout: float = 30.0):
        self._session = session
        self._executor = executor
        self._timeout = timeout

    def get(self, url: str, **kwargs):
        timeout = kwargs.pop("timeout", self._timeout)
        result = self._executor.render(url, timeout=timeout)
        if result.ok and result.html:  # 渲染成功且非空
            return RenderedResponse(result)
        logger.warning("渲染失败(%s) → 降级静态抓取: %s", result.error, url)
        return self._session.get(url, timeout=timeout, **kwargs)

    def post(self, url: str, **kwargs):
        return self._session.post(url, **kwargs)

    def __getattr__(self, name):
        return getattr(self._session, name)
