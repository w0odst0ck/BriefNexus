"""
网络请求工具、标准化函数、随机 UA
"""

import configparser
import hashlib
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import urlparse

import requests

# ── 日志 ──────────────────────────────────────────────────
logger = logging.getLogger("standards")

CST = timezone(timedelta(hours=8))

# ── 配置加载 ──────────────────────────────────────────────
_config_cache = None


def load_config(path: str = None) -> configparser.ConfigParser:
    global _config_cache
    if _config_cache is not None and path is None:
        return _config_cache

    if path is None:
        # 从本文件位置推算
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(base, "standards_config.ini")

    cfg = configparser.ConfigParser()
    with open(path, "r", encoding="utf-8") as f:
        cfg.read_file(f)
    _config_cache = cfg
    return cfg


def reload_config():
    """强制重载配置（用于调试/热更新）"""
    global _config_cache
    _config_cache = None
    return load_config()


# ── User-Agent ────────────────────────────────────────────
_ua_list = None


def _load_ua() -> list:
    global _ua_list
    if _ua_list is not None:
        return _ua_list
    cfg = load_config()
    raw = cfg.get("network", "user_agents", fallback="")
    _ua_list = [ua.strip() for ua in raw.split(",") if ua.strip()]
    if not _ua_list:
        _ua_list = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        ]
    return _ua_list


def random_ua() -> str:
    return random.choice(_load_ua())


# ── Session ────────────────────────────────────────────────
def new_session() -> requests.Session:
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": random_ua(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    # 可选代理配置
    cfg = load_config()
    proxy = cfg.get("network", "proxy", fallback="")
    if proxy:
        sess.proxies = {"http": proxy, "https": proxy}
    return sess


# ── HTTP 请求 ──────────────────────────────────────────────
def safe_get(url: str, sess: requests.Session = None, *,
             timeout: int = None, encoding: str = "utf-8",
             params: dict = None) -> Optional[str]:
    """带随机延迟的安全 GET 请求"""
    cfg = load_config()
    delay_min = float(cfg.get("crawler", "request_delay_min", fallback="0.5"))
    delay_max = float(cfg.get("crawler", "request_delay_max", fallback="2.0"))
    time.sleep(random.uniform(delay_min, delay_max))

    if sess is None:
        sess = new_session()
    if timeout is None:
        timeout = int(cfg.get("crawler", "timeout", fallback="30"))

    try:
        r = sess.get(url, timeout=timeout, params=params)
        r.encoding = encoding or r.apparent_encoding
        r.raise_for_status()
        return r.text
    except requests.exceptions.Timeout:
        logger.warning("TIMEOUT: %s", url[:80])
    except requests.exceptions.HTTPError as e:
        logger.warning("HTTP %s: %s", e.response.status_code, url[:80])
    except requests.exceptions.ConnectionError:
        logger.warning("CONN_ERR: %s", url[:80])
    except Exception as e:
        logger.warning("REQ_FAIL: %s - %s", url[:80], e)
    return None


def safe_get_json(url: str, sess: requests.Session = None, *,
                  timeout: int = None, method: str = "GET",
                  headers: dict = None, data: dict = None) -> Optional[dict]:
    """JSON API 安全请求"""
    cfg = load_config()
    delay_min = float(cfg.get("crawler", "request_delay_min", fallback="0.5"))
    delay_max = float(cfg.get("crawler", "request_delay_max", fallback="2.0"))
    time.sleep(random.uniform(delay_min, delay_max))

    if sess is None:
        sess = new_session()
    if timeout is None:
        timeout = int(cfg.get("crawler", "timeout", fallback="30"))

    try:
        if method.upper() == "POST" and data:
            r = sess.post(url, json=data, headers=headers, timeout=timeout)
        else:
            r = sess.get(url, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.Timeout:
        logger.warning("JSON TIMEOUT: %s", url[:80])
    except requests.exceptions.HTTPError as e:
        logger.warning("JSON HTTP %s: %s", e.response.status_code, url[:80])
    except json.JSONDecodeError:
        logger.warning("JSON_DECODE: %s", url[:80])
    except Exception as e:
        logger.warning("JSON_FAIL: %s - %s", url[:80], e)
    return None


# ── 数据标准化 ──────────────────────────────────────────────
def normalize_standard_no(raw: str) -> str:
    """标准化标准号：GB/T 12345-2023"""
    raw = raw.strip().upper()
    # 去全角空格
    raw = raw.replace("\u3000", " ")
    # 合并连续空格
    raw = re.sub(r"\s+", " ", raw)
    return raw


def normalize_date(raw: str) -> str:
    """标准化日期到 YYYY-MM-DD"""
    if not raw:
        return ""
    raw = raw.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日", "%Y%m%d"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    # 只有年份
    m = re.match(r"(\d{4})", raw)
    if m:
        return m.group(1) + "-01-01"
    return raw


def classify_standard_no(standard_no: str) -> str:
    """判断标准类别：国标/行标/地标/团标/其他"""
    no = standard_no.upper()
    if no.startswith("GB/T") or no.startswith("GB "):
        return "国标"
    if no.startswith("GB/Z"):
        return "国标(指导)"
    if re.match(r"[A-Z]+/T", no):
        return "行标"
    if re.match(r"DB\d+/T", no):
        return "地标"
    if re.match(r"T/[A-Z]", no):
        return "团标"
    if re.match(r"DB\d+ ", no):
        return "地标"
    if re.match(r"T/[\u4e00-\u9fff]", no):
        return "团标"
    return "其他"


def gen_dedup_key(item: dict) -> str:
    """生成去重键：标准号 + 标题前30字"""
    no = item.get("standard_no", "")
    title = item.get("title", "")[:30]
    raw = f"{no}-{title}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def make_standard_item(*, title: str, standard_no: str = "",
                       publisher: str = "", publish_date: str = "",
                       status: str = "", category: str = "",
                       url: str = "", source: str = "",
                       ics_code: str = "", scopes: str = "",
                       summary: str = "", **extra) -> dict:
    """构造标准化标准条目字典"""
    item = {
        "title": title.strip(),
        "standard_no": normalize_standard_no(standard_no),
        "publisher": publisher.strip(),
        "publish_date": normalize_date(publish_date),
        "status": status.strip(),
        "category": category or classify_standard_no(standard_no),
        "url": url.strip(),
        "source": source.strip(),
        "ics_code": ics_code.strip(),
        "scopes": scopes.strip(),
        "summary": summary.strip(),
        "dedup_key": "",
        "collected_at": datetime.now(CST).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
    }
    item.update(extra)
    # 去重键
    item["dedup_key"] = gen_dedup_key(item)
    return item


def pretty_json(items: list, ensure_ascii: bool = True) -> str:
    return json.dumps(items, ensure_ascii=ensure_ascii, indent=2, default=str)


def save_json(items: list, filepath: str):
    """保存为 JSON 文件"""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(pretty_json(items))
    logger.info("SAVED %d items → %s", len(items), filepath)


def load_json(filepath: str) -> list:
    """从 JSON 文件加载"""
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


# ── 提取 ICS 代码 ──────────────────────────────────────────
def extract_ics_code(html_or_text: str) -> str:
    m = re.search(r"ICS[\s　]*[（(]?(\d{2}\.\d+(?:\.\d+)?)[）)]?", html_or_text)
    return m.group(1) if m else ""
