"""
搜索引擎查找器 — 在 bzxz.net (标准分享站) 上定位标准

功能:
  1. 对给定标准号 + 标题，通过搜索引擎查找 bzxz.net 上的匹配页面
  2. 支持两种查找模式:
     - agent_mode: 通过 agent 的 web_search 工具搜索 (传入 search_results)
     - direct_mode: 直接访问 bzxz.net 尝试发现标准 (使用 HTTP 请求)
  3. 抓取页面 HTML 并提取标准正文文本

URL 模式:
  - https://www.bzxz.net/bzxz/{id}.html  — 中文标准详情页
  标准详情页结构:
    - "标准简介" 区: 标准号、名称、状态、简介文本
    - "标准内容" 区: 完整的标准正文文本 (HTML 渲染, 可能仅部分)
    - 下载链接: /bzxz/dl/{hash}.html
"""

import logging
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Tuple

from bs4 import BeautifulSoup

# ── Make sure project root is on path ──
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from standards.crawler.utils import (
    logger as root_logger,
    new_session,
    safe_get,
    normalize_standard_no,
)

logger = logging.getLogger("standards.search_finder")

BZXZ_BASE = "https://www.bzxz.net"
BZXZ_PATTERN = f"{BZXZ_BASE}/bzxz/{{}}.html"
DOWNLOAD_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "downloads",
)

CST = timezone(timedelta(hours=8))


# ── 标准化处理 ──────────────────────────────────────────

def make_safe_filename(standard_no: str) -> str:
    """将标准号转成安全文件名"""
    safe = re.sub(r'[\\/:*?"<>|]', "_", standard_no)
    safe = re.sub(r"\s+", "_", safe)
    return safe


def clean_standard_no_for_search(raw: str) -> str:
    """清理标准号，生成多个搜索变体"""
    no = raw.strip().upper()
    variants = [no]

    # 去掉空格
    no_nospace = no.replace(" ", "")
    if no_nospace != no:
        variants.append(no_nospace)

    # 去掉分隔符 (GB/T → GBT, GB → GB)
    no_stripped = re.sub(r"[^A-Z0-9]", "", no)
    if no_stripped != no_nospace:
        variants.append(no_stripped)

    # 无年份版本
    no_part = re.sub(r"[-—]\d{4}$", "", no)
    no_part = re.sub(r"[-—]\d{4}.*$", "", no_part)
    if no_part != no:
        variants.append(no_part)
        no_part_nospace = no_part.replace(" ", "")
        if no_part_nospace != no_part:
            variants.append(no_part_nospace)

    # 无前缀版本：取数字部分
    nums = re.findall(r"\d+", no)
    if nums:
        variants.extend(nums[:3])

    return list(dict.fromkeys(variants))  # 去重保持顺序


# ── 搜索结果解析 ──────────────────────────────────────

def parse_search_results(html: str, standard_no: str) -> List[str]:
    """解析搜索引擎结果页面，提取 bzxz.net 链接

    注意: 此函数处理通用搜索引擎 HTML 结果。
    对于 agent 提供的搜索结果 (web_search tool), 推荐使用 find_bzxz_urls_in_search_results()。

    Returns:
        bzxz.net 详情页 URL 列表
    """
    urls = []
    # 尝试解析
    soup = BeautifulSoup(html, "html.parser")

    # 通用搜索引擎结果
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if "bzxz.net/bzxz/" in href and href.endswith(".html"):
            # 标准化 URL
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = BZXZ_BASE + href
            elif "bzxz.net" not in href:
                continue
            urls.append(href)

    # 按相关性排序 — 优先匹配标准号
    target = normalize_standard_no(standard_no)
    scored = []
    for url in urls:
        score = 0
        text = link.get_text(strip=True) if link else ""
        if target.replace(" ", "") in text.replace(" ", ""):
            score += 10
        if target[:8] in text:
            score += 5
        scored.append((score, url))

    scored.sort(key=lambda x: -x[0])
    return [url for _, url in scored]


def find_bzxz_urls_in_search_results(search_results: list,
                                      standard_no: str) -> List[dict]:
    """从 web_search tool 返回的结果中提取 bzxz.net 链接

    Args:
        search_results: web_search 返回的 results 列表
            每个元素包含 title, url, description, siteName 等字段
        standard_no: 要匹配的标准号

    Returns:
        [{"url": "...", "score": int, "title": "..."}] sorted by score desc
    """
    candidates = []
    target = normalize_standard_no(standard_no)
    target_clean = target.replace(" ", "").upper()

    for r in search_results:
        url = r.get("url", "")
        title = r.get("title", "")
        desc = r.get("description", "")

        # 必须是 bzxz.net 且包含 /bzxz/
        if "bzxz.net/bzxz/" not in url:
            continue

        # 忽略英文版 /en/
        if "/en/" in url:
            continue

        # 忽略下载链接 /bzxz/dl/
        if "/bzxz/dl/" in url:
            continue

        combined_text = (title + " " + desc).upper()
        score = 0
        # 标准号精确匹配
        if target_clean in combined_text.replace(" ", ""):
            score += 20
        elif target_clean[:8] in combined_text:
            score += 10

        # 标准号数字部分匹配
        nums_in_no = re.findall(r"\d+", target_clean)
        for n in nums_in_no[:3]:
            if n in combined_text:
                score += 3

        candidates.append({
            "url": url,
            "score": score,
            "title": title[:100],
        })

    candidates.sort(key=lambda x: -x["score"])
    return candidates


# ── 页面抓取与内容提取 ──────────────────────────────────

def fetch_bzxz_page(url: str, session=None) -> Optional[str]:
    """抓取 bzxz.net 详情页的 HTML

    Args:
        url: bzxz.net 标准详情页 URL
        session: 可复用的 requests Session

    Returns:
        HTML 文本，失败返回 None
    """
    if session is None:
        session = new_session()

    # 等待时间
    delay = 1.0 + (hash(url) % 3) * 0.3
    time.sleep(delay)

    headers = {
        "Referer": "https://www.bzxz.net/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    try:
        r = session.get(url, headers=headers, timeout=30, allow_redirects=True)
        r.raise_for_status()
        r.encoding = "utf-8"
        return r.text
    except Exception as e:
        logger.warning("抓取 bzxz 页面失败: %s — %s", url, e)
        return None


def extract_standard_content(html: str) -> dict:
    """从 bzxz.net 标准详情页提取标准正文内容

    页面结构:
      - 标准简介区 (标准号、名称、状态)
      - 标准内容区 (正文文本，在 "标准内容" 标题下)
      - 小提示区 (表示仅展示部分内容)

    Args:
        html: 标准详情页 HTML

    Returns:
        {
            "standard_no": str,
            "title": str,
            "status": str,
            "content_intro": str,   # 标准简介文本
            "content_full": str,    # 标准正文文本
            "is_truncated": bool,   # 是否仅展示部分内容
            "download_url": str,    # 下载链接
        }
    """
    result = {
        "standard_no": "",
        "title": "",
        "status": "",
        "content_intro": "",
        "content_full": "",
        "is_truncated": False,
        "download_url": "",
    }

    soup = BeautifulSoup(html, "html.parser")

    # ── 提取标准号 ──
    # 常见位置: h2 标签, 或基本信息区的 strong 文本
    std_no_el = soup.find("h2", string=re.compile(r"GB"))
    if not std_no_el:
        std_no_el = soup.find("strong", string=re.compile(r"GB"))
    if not std_no_el:
        std_no_el = soup.find("h1", string=re.compile(r"GB"))
    if std_no_el:
        result["standard_no"] = normalize_standard_no(std_no_el.get_text(strip=True))

    # 如果没找到，从基本信息区查找
    if not result["standard_no"]:
        info_section = soup.find("div", class_=re.compile(r"info|basic"))
        if info_section:
            no_match = re.search(
                r"(GB[ZT\s/]*[\d.]+[-—]\d{4})",
                info_section.get_text()
            )
            if no_match:
                result["standard_no"] = normalize_standard_no(no_match.group(1))

    # ── 提取标题 ──
    # 页面中标准名称通常紧跟在标准号之后或作为页面标题
    title_el = soup.find("h1", class_=re.compile(r"title", re.I))
    if not title_el:
        title_el = soup.find("h2")
    if not title_el:
        title_el = soup.find("title")
    if title_el:
        raw = title_el.get_text(strip=True)
        # 清理标题
        raw = re.sub(r"^.*?标准(?:免费)?下载\s*", "", raw)
        raw = raw.replace("标准下载网", "").replace("- www.bzxz.net", "").strip()
        # 去掉标准号部分
        raw = re.sub(r"^(GB[ZT\s/]*[\d.]+[-—]\d{4})\s*", "", raw)
        # 去掉纯英文标题
        if re.match(r"^[A-Z][a-zA-Z\s,;:.]+$", raw[:50]):
            raw = ""
        result["title"] = raw.strip(" -—|")

    # ── 提取状态 ──
    status_match = re.search(r"(现行|废止|作废|即将实施|替代)", html)
    if status_match:
        result["status"] = status_match.group(1)

    # ── 提取标准简介 ──
    intro_section = soup.find("div", string=re.compile(r"标准简介"))
    if not intro_section:
        intro_section = soup.find("h3", string=re.compile(r"标准简介"))
    if intro_section:
        # 获取简介标题之后的兄弟节点内容
        parent = intro_section.parent
        for sibling in intro_section.find_next_siblings():
            text = sibling.get_text(strip=True)
            if text and len(text) > 20:
                result["content_intro"] = text
                break

    # 如果没有通过标签找到，尝试正则
    if not result["content_intro"]:
        intro_match = re.search(
            r"标准简介[：:\s]*(.*?)(?:标准内容|标准图片预览|$)",
            html,
            re.DOTALL
        )
        if intro_match:
            intro_text = intro_match.group(1)
            intro_text = BeautifulSoup(intro_text, "html.parser").get_text(strip=True)
            result["content_intro"] = intro_text[:2000]

    # ── 提取标准正文内容 ──
    # 位置: "标准内容" 标题之后的内容
    content_section = soup.find("div", string=re.compile(r"标准内容"))
    if not content_section:
        content_section = soup.find("h3", string=re.compile(r"标准内容"))
    if content_section:
        content_parts = []
        for sibling in content_section.find_next_siblings():
            text = sibling.get_text(strip=True)
            if not text:
                continue
            # 遇到下一个标题停止
            if sibling.name in ("h2", "h3", "h4") and "标准" in text:
                break
            content_parts.append(text)
        if content_parts:
            result["content_full"] = "\n\n".join(content_parts)

    # 正则备份提取
    if not result["content_full"]:
        content_match = re.search(
            r"标准内容[：:\s]*(.*?)(?:小提示|标准图片预览|其它标准|设为首页|$)",
            html,
            re.DOTALL
        )
        if content_match:
            raw_content = content_match.group(1)
            # 去掉 HTML 标签
            content_text = BeautifulSoup(raw_content, "html.parser").get_text(strip=True)
            result["content_full"] = content_text

    # ── 截断标记 ──
    if "仅展示完整标准里的部分截取内容" in html:
        result["is_truncated"] = True

    # ── 下载链接 ──
    dl_link = soup.find("a", href=re.compile(r"/bzxz/dl/"))
    if dl_link:
        href = dl_link.get("href", "")
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = BZXZ_BASE + href
        result["download_url"] = href

    return result


def save_standard_text(standard_no: str, content: dict, base_dir: str = None) -> Optional[str]:
    """将提取的标准正文保存为文本文件

    Args:
        standard_no: 标准号 (用于文件名)
        content: extract_standard_content() 返回的字典
        base_dir: 下载目录，默认 standards/downloads

    Returns:
        保存的文件路径，失败返回 None
    """
    if base_dir is None:
        base_dir = DOWNLOAD_DIR

    safe_name = make_safe_filename(standard_no)
    local_path = os.path.join(base_dir, f"{safe_name}.txt")

    os.makedirs(base_dir, exist_ok=True)

    # 合并内容
    lines = []
    lines.append(f"标准号: {standard_no}")
    lines.append(f"标题: {content.get('title', '')}")
    lines.append(f"状态: {content.get('status', '')}")
    lines.append(f"来源: {BZXZ_BASE}")
    lines.append(f"下载链接: {content.get('download_url', '')}")
    lines.append(f"截断: {'是（仅展示部分内容）' if content.get('is_truncated') else '否'}")
    lines.append(f"抓取时间: {datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    lines.append("=" * 60)
    lines.append("标准简介")
    lines.append("=" * 60)
    lines.append(content.get("content_intro", ""))
    lines.append("")
    lines.append("=" * 60)
    lines.append("标准正文")
    lines.append("=" * 60)
    lines.append(content.get("content_full", ""))

    text = "\n".join(lines)

    with open(local_path, "w", encoding="utf-8") as f:
        f.write(text)

    logger.info("已保存标准文本: %s → %s (%d 字符)", standard_no, local_path, len(text))
    return local_path


# ── 主查找接口 ──────────────────────────────────────────

# ── bzxz.net 列表页浏览搜索 ──────────────────────────

def fetch_list_page(category: str = "gb", page: int = 1) -> Optional[str]:
    """获取 bzxz.net 分类列表页

    Args:
        category: 分类 (gb=国家标准, hg=化工, qb=轻工, jt=交通, etc.)
        page: 页码

    Returns:
        HTML 文本
    """
    session = new_session()
    if category == "gb":
        url = f"{BZXZ_BASE}/gb/?page={page}"
    else:
        url = f"{BZXZ_BASE}/{category}/?page={page}"

    time.sleep(1.0 + (hash(url) % 3) * 0.3)
    try:
        r = session.get(url, timeout=30, allow_redirects=True)
        r.raise_for_status()
        r.encoding = "utf-8"
        return r.text
    except Exception as e:
        logger.warning("获取列表页失败: %s — %s", url, e)
        return None


def parse_list_page(html: str) -> List[dict]:
    """解析分类列表页，提取标准条目

    Returns:
        [{standard_no, title, url, bzxz_id}, ...]
    """
    results = []
    soup = BeautifulSoup(html, "html.parser")

    # 查找所有标准条目
    for item in soup.select("li, .list-item, .item"):
        link = item.find("a", href=re.compile(r"/bzxz/\d+\.html"))
        if not link:
            continue
        href = link.get("href", "")
        text = link.get_text(strip=True)

        # 提取标准号
        std_match = re.search(r"(GB[ZT/\s]*[\d.]+[-—]\d{4})", text)
        if not std_match:
            # 有些条目链接文本 = 标准号
            continue
        standard_no = normalize_standard_no(std_match.group(1))

        # 提取 bzxz_id
        id_match = re.search(r"/bzxz/(\d+)\.html", href)
        bzxz_id = int(id_match.group(1)) if id_match else 0

        # 提取标题
        title = re.sub(r"(GB[ZT/\s]*[\d.]+[-—]\d{4})\s*", "", text, count=1).strip()

        results.append({
            "standard_no": standard_no,
            "title": title,
            "url": BZXZ_BASE + href if href.startswith("/") else href,
            "bzxz_id": bzxz_id,
        })

    # 如果上面没找到，尝试更宽松的匹配
    if not results:
        for link in soup.find_all("a", href=re.compile(r"/bzxz/\d+\.html")):
            href = link.get("href", "")
            text = link.get_text(strip=True)

            std_match = re.search(r"(GB[ZT/\s]*[\d.]+[-—]\d{4})", text)
            if not std_match:
                continue
            standard_no = normalize_standard_no(std_match.group(1))
            id_match = re.search(r"/bzxz/(\d+)\.html", href)
            bzxz_id = int(id_match.group(1)) if id_match else 0
            title = re.sub(r"(GB[ZT/\s]*[\d.]+[-—]\d{4})\s*", "", text, count=1).strip()

            results.append({
                "standard_no": standard_no,
                "title": title,
                "url": BZXZ_BASE + href if href.startswith("/") else href,
                "bzxz_id": bzxz_id,
            })

    return results


def search_on_bzxz_list(target_standards: List[str],
                         max_pages: int = 20) -> dict:
    """在 bzxz.net 列表页上批量查找标准

    遍历 bzxz.net 的标准分类列表页，寻找匹配的标准。
    适合一批标准集中查找（如全库 85 条非采标）。

    Args:
        target_standards: 目标标准号列表 ["GB/T 39394-2020", ...]
        max_pages: 最多爬取页数

    Returns:
        {
            "found": {standard_no: {url, bzxz_id, ...}, ...},
            "not_found": [standard_no, ...],
            "pages_scanned": int,
        }
    """
    # 标准化目标列表
    targets = {normalize_standard_no(s): s for s in target_standards}
    target_nos = set(targets.keys())

    found = {}
    pages_scanned = 0

    for page in range(1, max_pages + 1):
        html = fetch_list_page("gb", page)
        if not html:
            logger.warning("列表页 %d 获取失败，终止", page)
            break

        entries = parse_list_page(html)
        if not entries:
            logger.info("列表页 %d 无条目，终止", page)
            break

        pages_scanned += 1

        for entry in entries:
            std_no = entry["standard_no"]
            if std_no in target_nos and std_no not in found:
                found[std_no] = entry
                logger.info("找到目标: %s → /bzxz/%d.html", std_no, entry["bzxz_id"])

        # 如果所有目标都已找到，提前终止
        if len(found) == len(target_nos):
            logger.info("所有目标已找到，提前终止")
            break

        # 流控
        if page < max_pages:
            time.sleep(1.5 + (page % 5) * 0.3)

    not_found = [targets[s] for s in target_nos if s not in found]

    logger.info("列表搜索完成: 扫描 %d 页, 找到 %d/%d 条",
                pages_scanned, len(found), len(target_nos))
    if not_found:
        logger.info("未找到: %s", not_found[:10])
        if len(not_found) > 10:
            logger.info("... 共 %d 条", len(not_found))

    return {
        "found": found,
        "not_found": not_found,
        "pages_scanned": pages_scanned,
    }


# ── 主查找接口 ──────────────────────────────────────────

def search_on_bzxz(standard_no: str, title: str = "",
                   search_results: list = None,
                   bzxz_id: int = None) -> Optional[dict]:
    """在 bzxz.net 上查找标准

    Args:
        standard_no: 标准号
        title: 标准标题 (可选)
        search_results: 由 agent 通过 web_search tool 获取的搜索结果
            (列表, 每个元素含 title/url/description/siteName)
        bzxz_id: 已知的 bzxz.net 标准页 ID (跳过搜索步骤)

    Returns:
        {
            "standard_no": 匹配的标准号,
            "url": bzxz 详情页 URL,
            "page_content": extract_standard_content() 结果,
            "local_path": 保存的本地文件路径 (如已保存),
        }
        未找到返回 None
    """
    target_no = normalize_standard_no(standard_no)
    logger.info("在 bzxz.net 搜索: %s", target_no)

    # ── 确定 URL ──
    bzxz_url = None

    # 策略 1: 已知 bzxz_id
    if bzxz_id:
        bzxz_url = f"{BZXZ_BASE}/bzxz/{bzxz_id}.html"
        logger.info("使用已知 ID: /bzxz/%d.html", bzxz_id)

    # 策略 2: 使用 agent 提供的搜索结果
    if not bzxz_url and search_results:
        candidates = find_bzxz_urls_in_search_results(search_results, target_no)
        if candidates and candidates[0]["score"] > 0:
            bzxz_url = candidates[0]["url"]
            logger.info("通过搜索结果找到 URL: %s (score=%d)", bzxz_url, candidates[0]["score"])

    if not bzxz_url:
        logger.info("未找到 bzxz.net 链接: %s", target_no)
        return None

    # ── 抓取页面 ──
    html = fetch_bzxz_page(bzxz_url)
    if not html:
        logger.warning("抓取页面失败: %s", bzxz_url)
        return None

    # ── 提取内容 ──
    content = extract_standard_content(html)
    if not content.get("content_full") and not content.get("content_intro"):
        logger.warning("页面内容提取为空: %s", bzxz_url)
        return None

    return {
        "standard_no": target_no,
        "url": bzxz_url,
        "page_content": content,
        "local_path": None,  # 调用者调用 save_standard_text
    }


def get_download_url_from_page(html: str) -> Optional[str]:
    """从 bzxz 详情页提取 PDF/ZIP 下载链接

    Returns:
        下载页面 URL，失败返回 None
    """
    dl_match = re.search(r'<a\s+href="(/bzxz/dl/[^"]+\.html)"', html)
    if dl_match:
        return BZXZ_BASE + dl_match.group(1)
    return None


# ── 测试入口 ────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    # 简单测试
    test_no = "GB/T 39394-2020"
    print(f"测试搜索: {test_no}")
    # 注意: 此测试需要 agent 提供 search_results
    # 在独立运行模式下仅测试 URL 抓取
    test_url = "https://www.bzxz.net/bzxz/201214.html"
    print(f"测试抓取: {test_url}")
    html = fetch_bzxz_page(test_url)
    if html:
        content = extract_standard_content(html)
        print(f"  标准号: {content['standard_no']}")
        print(f"  标题: {content['title']}")
        print(f"  内容长度: {len(content.get('content_full', ''))}")
        print(f"  截断: {content['is_truncated']}")
        path = save_standard_text(test_no, content)
        print(f"  已保存: {path}")
    else:
        print("  抓取失败")
