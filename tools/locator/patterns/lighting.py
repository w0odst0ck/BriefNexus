"""照明类标准（ICS 29.140）参数检测规则。

场景：配光曲线、光度参数、色温、显色指数。
"""

from . import CategoryRules, RuleTier, register

LIGHTING: CategoryRules = {
    "P0": RuleTier(
        keywords=[
            "配光性能要求",
            "配光曲线",
            "光度参数",
            "测试点或测试区域",
            "位置(°)",
            "位置/(°)",
            "位置/(",
            "位置（°）",
        ],
        weight=1.0,
    ),
    "P1": RuleTier(
        keywords=[
            "规定照度",
            "照度/lx",
            "照度值",
            "发光强度",
            "色品坐标",
            "显色指数",
            "配光分布",
            "配光要求",
            "光分布",
            "限值/cd",
            "cd/lm",
            "光束",
        ],
        weight=0.5,
    ),
    "P2": RuleTier(
        keywords=[
            "最小值",
            "最大值",
            "公差",
            "限值",
        ],
        weight=0.2,
    ),
}

register("lighting", LIGHTING)
