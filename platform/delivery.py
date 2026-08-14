"""
回调交付层 — webhook POST + 重试/超时/SSRF 护栏(零新依赖,复用 requests)

职责单一、可独立单测:
  - validate_callback_url: 提交阶段校验 callback_url 仅 http/https 且 netloc 非空
  - post_json_with_retry: 初次 + 最多 3 次重试(指数退避 2/4/8s),timeout 10s,
    allow_redirects=False(防 SSRF 跳转),2xx 判定成功,返回 (ok, http_code)
"""
import logging
import time
from urllib.parse import urlparse

import requests

logger = logging.getLogger("platform.delivery")

# 默认重试退避(秒): 初次后 3 次重试
DEFAULT_RETRY_DELAYS = (2, 4, 8)
DEFAULT_TIMEOUT_S = 10


class WebhookDeliveryError(Exception):
    """回调投递异常(预留,当前实现用 (ok, http_code) 返回值而非抛异常)"""


def validate_callback_url(url: str) -> str | None:
    """校验回调 URL 仅 http/https 且含主机名;返回错误消息或 None(通过)。

    ftp/file/空 scheme/netloc 为空一律拒绝。不阻断私网 IP(平台绑定 127.0.0.1
    仅受信调用方 + 测试 stub 在本机回环,阻断私网会误伤;README 已注明该取舍)。
    """
    if not url or not isinstance(url, str):
        return "callback_url 必须为 http/https URL"
    try:
        parsed = urlparse(url)
    except ValueError:
        return "callback_url 非法: 无法解析"
    if parsed.scheme not in ("http", "https"):
        return f"callback_url 仅支持 http/https,实际为: {parsed.scheme or '(空)'}"
    if not parsed.netloc:
        return "callback_url 缺少主机名"
    return None


def is_safe_callback_url(url: str) -> bool:
    """scheme 校验包装(提交阶段用): 合法返回 True"""
    return validate_callback_url(url) is None


def post_json_with_retry(url: str, payload: dict, *, timeout_s: int = DEFAULT_TIMEOUT_S,
                         retry_delays=(2, 4, 8)) -> tuple[bool, int]:
    """POST JSON 到回调地址,初次 + 重试;2xx 成功。

    Returns:
        (ok, http_code): ok 是否成功;http_code 为最终响应码(网络错误为 0)。
    """
    attempts = [0.0, *tuple(retry_delays)]  # 初次不 sleep,后续退避
    last_code = 0
    for i, delay in enumerate(attempts):
        if delay:
            time.sleep(delay)
        try:
            resp = requests.post(url, json=payload, timeout=timeout_s,
                                 allow_redirects=False)
            last_code = resp.status_code
            if 200 <= resp.status_code < 300:
                return True, resp.status_code
            logger.warning("回调 %s 第 %d 次尝试返回非 2xx: %d",
                           url, i + 1, resp.status_code)
        except requests.RequestException as e:
            last_code = 0
            logger.warning("回调 %s 第 %d 次尝试网络错误: %s", url, i + 1, e)
    return False, last_code
