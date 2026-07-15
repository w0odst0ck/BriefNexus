"""汽车类标准（ICS 43.xxx）参数检测规则。

场景：信号灯光度、汽车前照灯、制动灯、转向灯。
"""

from . import CategoryRules, RuleTier, register

AUTOMOTIVE: CategoryRules = {
    "P0": RuleTier(
        keywords=[
            "配光性能要求",
            "光度参数",
            "测试点或测试区域",
            "测试点",
            "光分布",
        ],
        weight=1.0,
    ),
    "P1": RuleTier(
        keywords=[
            "位置(°)",
            "位置（°）",
            "规定照度",
            "照度/lx",
            "发光强度",
            "制动灯",
            "转向灯",
            "前照灯",
            "雾灯",
            "信号灯",
            "色度",
            "光通量",
        ],
        weight=0.5,
    ),
    "P2": RuleTier(
        keywords=[
            "最小值",
            "最大值",
            "公差",
            "限值",
            "照度",
            "亮度",
        ],
        weight=0.2,
    ),
}

register("automotive", AUTOMOTIVE)
