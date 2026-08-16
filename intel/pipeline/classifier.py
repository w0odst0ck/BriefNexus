"""规则分类器"""
import re

from intel.core.base import NewsItem

SECTORS = [
    ("行业大势", "expo"),
    ("技术突破", "tech"),
    ("资本脉搏", "finance"),
    ("供应链深水", "supply"),
    ("企业交锋", "corp"),
    ("政策风向", "policy"),
    ("宏观数据", "macro"),
]
SECTOR_KEYS = {s[1] for s in SECTORS}

def classify(items: list[NewsItem]):
    for it in items:
        it.sector = _classify(it.title)

def _classify(title: str) -> str:
    t = title
    # 监管/合规/标准强信号优先（机构名/公告类词是强信号，需先于 corp 的 launch/发布 判定）
    if re.search(r"(Regulatory|regulation|compliance|合规|SFDA|SASO|SABER|GSO|FDA|EMA|customs|海关|standard|标准|公告|notice|notification|certification|认证|approval|approved|recall|召回|AI Act|tariff|export control|sanction|法案|出口管制|贸易|政策|policy)", t, re.IGNORECASE):
        return "policy"
    if re.search(r"(AI|人工智能|Machine Learning|LLM|大模型|GPU|机器人|robot|cyber|security|自动驾驶|quantum)", t, re.IGNORECASE):
        return "tech"
    if re.search(r"(财报|earnings|revenue|Q[1-4]|IPO|融资|投资|acquisition|merger|并购|dividend)", t, re.IGNORECASE):
        return "finance"
    if re.search(r"(fab|产能|chip|semiconductor|wafer|制造|晶圆|supply chain|短缺|TSMC|台积电)", t, re.IGNORECASE):
        return "supply"
    if re.search(r"(partnership|合作|launch|发布|product|strategic|alliance)", t, re.IGNORECASE):
        return "corp"
    if re.search(r"(interest rate|GDP|inflation|CPI|就业|unemployment|国债|treasury|联邦基金|美联储)", t, re.IGNORECASE):
        return "macro"
    return "expo"
