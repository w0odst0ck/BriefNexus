"""
SAMR 标准采集适配器 — 将 standards/ 引擎的 SAMR 采集包装为平台采集器(D10)

source=samr 走平台统一 API:POST /v1/collect {"source": "samr", "params": {...}}
复用 standards/crawler/platforms/samr.py 的 SamrCollector(只 import,不改动),
默认搜索参数读取 standards/standards_config.ini 的 keywords/ics_codes,
任务 params(keyword/ics/max_pages)可覆盖。

采集结果转换为 NewsItem:type="standard",raw_data 携带标准元数据
(standard_no/title/category/status/publish_date/ics_code)。
"""
import configparser
import logging
from datetime import datetime
from typing import ClassVar

from intel.core.base import CST, BaseCollector, NewsItem

logger = logging.getLogger("intel.samr_standard")

# 标准字段名(与 standards/crawler/utils.make_standard_item 输出一致)
_STANDARD_KEYS = (
    "standard_no", "title", "category", "status",
    "publish_date", "ics_code", "publisher",
)


def _parse_date(s: str):
    """'YYYY-MM-DD' → datetime(东八区 aware);解析失败返回 None"""
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=CST)
        except ValueError:
            continue
    return None


class SamrStandardCollector(BaseCollector):
    """全国标准信息公共服务平台采集适配器"""

    source_name = "samr"
    display_name = "全国标准信息公共服务平台"
    domains: ClassVar[list] = ["standards"]
    PARAM_SCHEMA: ClassVar[dict] = {
        "keyword": {"type": "str"},
        "ics": {"type": "str"},
        "max_pages": {"type": "int", "min": 1, "max": 20},
    }

    def __init__(self, keyword: str | None = None, ics: str | None = None,
                 max_pages: int | None = None, **kwargs):
        super().__init__(**kwargs)  # 透传 max_age 等 BaseCollector 参数
        self.keyword = keyword
        self.ics = ics
        self.max_pages = max_pages

    # ---------- 默认参数(standards_config.ini) ----------

    def _default_keywords(self) -> list:
        """standards_config.ini [domain] keywords,逗号分隔"""
        try:
            from standards.crawler.utils import load_config
            raw = load_config().get("domain", "keywords", fallback="")
            return [k.strip() for k in raw.split(",") if k.strip()]
        except (OSError, configparser.Error, ValueError) as e:
            logger.warning("读取 standards_config.ini keywords 失败: %s", e)
            return []

    def _default_ics_codes(self) -> list:
        """standards_config.ini [domain] ics_codes,逗号分隔"""
        try:
            from standards.crawler.utils import load_config
            raw = load_config().get("domain", "ics_codes", fallback="")
            return [k.strip() for k in raw.split(",") if k.strip()]
        except (OSError, configparser.Error, ValueError) as e:
            logger.warning("读取 standards_config.ini ics_codes 失败: %s", e)
            return []

    def _default_max_pages(self) -> int:
        """standards_config.ini [crawler] max_pages,缺省 5"""
        try:
            from standards.crawler.utils import load_config
            return int(load_config().get("crawler", "max_pages", fallback="5"))
        except (OSError, configparser.Error, ValueError):
            return 5

    # ---------- 采集 ----------

    def crawl(self, sess) -> list:
        """复用 standards 的 SamrCollector 搜索标准,转换为 NewsItem 列表"""
        from standards.crawler.platforms.samr import SamrCollector

        # 默认参数: config 片段(standards_config.ini);任务 params 可覆盖
        keywords = [self.keyword] if self.keyword else self._default_keywords()
        ics_codes = [self.ics] if self.ics else self._default_ics_codes()
        max_pages = self.max_pages if self.max_pages else self._default_max_pages()
        if not keywords and not ics_codes:
            logger.warning("samr 源无可搜索关键词/ICS(standards_config.ini 未配置且未传 params)")
            return []

        # SamrCollector 内部自行 new_session(standards 配置的 UA/延迟/代理)
        sampler = SamrCollector()
        raw_items = sampler.collect(keywords=keywords, ics_codes=ics_codes,
                                    max_pages=max_pages)
        return [self._to_news_item(it) for it in raw_items if it]

    def _to_news_item(self, raw: dict) -> NewsItem:
        """标准条目 dict → NewsItem(type=standard, raw_data=标准元数据)"""
        item = NewsItem(
            title=raw.get("title", ""),
            url=raw.get("url", ""),
            summary=raw.get("summary", ""),
            source=self.display_name,
            domain="标准",
            sector="标准",
            date_obj=_parse_date(raw.get("publish_date", "")),
        )
        item.type = "standard"
        item.raw_data = {k: raw.get(k, "") for k in _STANDARD_KEYS}
        return item
