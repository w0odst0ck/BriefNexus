"""
BOSS 直聘搜索页 — 浏览器渲染验证源(T6, 最小采集器)

声明 `render: true` 后, cmd_run/cmd_check 会给本源传 RenderAwareSession:
`sess.get(url)` 返回渲染后 HTML(RenderedResponse); 渲染失败自动降级静态。
本采集器只读 r.text 用正则提取职位卡片, 渲染失败/反爬拦截一律返回 [],
不抛异常, 不中断整体巡检。
"""
import logging
import re
from urllib.parse import urlencode

from intel.core.base import BaseCollector, NewsItem
from intel.core.registry import register

logger = logging.getLogger("intel.boss_zhipin")

BOSS_BASE = "https://www.zhipin.com"
BOSS_QUERY = "自动驾驶"
BOSS_CITY = "100010000"  # 北京

# 职位卡片: <a class="job-card-left" href="..."> 内 <span class="job-name">职位</span>
# 选择器写成可调正则, DOM 变动时解析失败静默返回 [] 而非抛异常
_CARD_RE = re.compile(
    r'<div class="job-card-wrapper".*?'
    r'<a[^>]*class="[^"]*job-card-left[^"]*"[^>]*href="([^"]+)"[^>]*>.*?'
    r'<span class="job-name">(.*?)</span>',
    re.DOTALL,
)


@register("boss_zhipin")
class BossZhipinCollector(BaseCollector):
    source_name = "boss_zhipin"
    display_name = "BOSS 直聘"
    domains = ["self_driving"]

    def crawl(self, sess) -> list[NewsItem]:
        items = []
        try:
            url = BOSS_BASE + "/web/geek/job?" + urlencode(
                {"query": BOSS_QUERY, "city": BOSS_CITY}
            )
            r = sess.get(url, timeout=30)
            r.raise_for_status()
            text = r.text or ""
            if "job-card-wrapper" not in text:
                logger.warning("BOSS 直聘页面无职位卡片(反爬/结构变更), 返回空")
                return []
            for href, title in _CARD_RE.findall(text):
                title = re.sub(r"<[^>]+>", "", title).strip()
                if not title:
                    continue
                if not href.startswith("http"):
                    href = BOSS_BASE + href
                items.append(NewsItem(
                    title=title, url=href, source=self.display_name, domain="招聘"
                ))
        except Exception as e:  # 渲染失败/网络失败/解析失败均收敛为 []
            logger.error("BOSS 直聘采集失败: %s", e)
        return items
