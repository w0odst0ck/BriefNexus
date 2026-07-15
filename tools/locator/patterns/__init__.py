"""参数检测规则库 — 规则由 YAML 配置文件驱动。

使用方式：
    from tools.locator.patterns import get_rules, match_page
    signals, is_param, confidence = match_page(text, category="lighting")

迭代规则：
    编辑 yaml/{category}.yaml → 运行 python3 validate.py → 看查全/查准变化
"""

import os
import glob
from typing import TypedDict

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore


class RuleTier(TypedDict):
    keywords: list[str]
    weight: float


class CategoryRules(TypedDict):
    P0: RuleTier
    P1: RuleTier
    P2: RuleTier


_RULES: dict[str, CategoryRules] = {}
_YAML_DIR = os.path.join(os.path.dirname(__file__), "yaml")


def _load_yaml_rules() -> None:
    """Load all YAML rule files from yaml/ directory."""
    if yaml is None:
        raise ImportError("PyYAML is required. Run: pip install pyyaml")

    pattern = os.path.join(_YAML_DIR, "*.yaml")
    for yaml_path in sorted(glob.glob(pattern)):
        category = os.path.splitext(os.path.basename(yaml_path))[0]
        with open(yaml_path, encoding="utf-8") as f:
            rules_data = yaml.safe_load(f)
        _RULES[category] = rules_data  # type: ignore


def register(name: str, rules: CategoryRules) -> None:
    """Fallback: register rules programmatically (for backward compat)."""
    _RULES[name] = rules


def get_rules(category: str) -> CategoryRules | None:
    return _RULES.get(category)


def list_categories() -> list[str]:
    return list(_RULES.keys())


def reload_rules() -> None:
    """重新加载所有 YAML 规则（迭代时无需重启 Python 进程）。"""
    _RULES.clear()
    _load_yaml_rules()


def match_page(text: str, category: str) -> tuple[list[dict], bool, str]:
    """对一页文本进行匹配。

    Args:
        text: 页面文本（已 normalize，换行替换为空格）
        category: 标准类别，对应 yaml/{category}.yaml

    Returns:
        (signals, is_param_page, confidence)
        signals: [{"keyword": ..., "tier": ..., "confidence": ...}, ...]
    """
    rules = get_rules(category)
    if not rules:
        return [], False, "low"

    text_lower = text.lower()
    signals = []
    p0_hits = 0
    p1_hits = 0

    for tier_name in ("P0", "P1", "P2"):
        tier = rules[tier_name]
        weight = tier["weight"]
        for kw in tier["keywords"]:
            if kw.lower() in text_lower:
                signals.append({
                    "keyword": kw,
                    "tier": tier_name,
                    "confidence": weight,
                })
                if tier_name == "P0":
                    p0_hits += 1
                elif tier_name == "P1":
                    p1_hits += 1

    p2_hits = len([s for s in signals if s["tier"] == "P2"])

    # 判定规则
    if p0_hits >= 1:
        is_param = True
        conf = "high"
    elif p1_hits >= 3:
        is_param = True
        conf = "medium"
    elif p1_hits >= 1 and p2_hits >= 2:
        is_param = True
        conf = "medium"
    else:
        is_param = False
        conf = "low"

    return signals, is_param, conf


# Load rules at module import
_load_yaml_rules()
