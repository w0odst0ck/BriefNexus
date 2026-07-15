"""未来模型接入接口 — PageDetector 抽象基类。

使用方式（当前阶段）：
    from tools.locator.model_hook import PageDetector
    # 暂不使用，detector.py 直接内置规则匹配

使用方式（未来阶段）：
    from tools.locator.model_hook import MLDetector
    detector = MLDetector(model_path="...")
    signals = detector.detect_page(page_text, page_num)

替换时 detector.py 只需改一行：
    # detector = RuleDetector()     # 当前
    detector = MLDetector(...)      # 未来
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ParamSignal:
    """一个参数信号。"""
    page: int
    signal_type: str       # "keyword" | "regex" | "model_classifier" | "model_vision"
    signal_value: str      # 匹配到的具体内容
    confidence: float       # 0.0 ~ 1.0


class PageDetector(ABC):
    """参数页检测器基类。

    Concrete implementations:
      - RuleDetector (当前): keywords + regex
      - MLDetector   (未来): classification model
      - LLMDetector  (未来): prompt-based detection
    """

    @abstractmethod
    def detect_page(self, text: str, page_num: int) -> list[ParamSignal]:
        """检测单页是否包含参数表。"""
        ...

    def batch_detect(self, pages: list[tuple[int, str]]) -> list[ParamSignal]:
        """批量检测，默认逐页调用 detect_page。"""
        results = []
        for page_num, text in pages:
            results.extend(self.detect_page(text, page_num))
        return results
