"""题材热度因子：基于同花顺热点数据"""
import pandas as pd
import numpy as np
from ..base import FactorCalculator


class HotTopicFactor(FactorCalculator):
    """题材热度因子

    需要外部输入热点评分（从同花顺harden API获取）。
    因子值来自hexin.get_harden_stocks()中的reason_tags分析。
    """

    def __init__(self, name="hot_topic", config=None, topic_scores=None):
        super().__init__(name, config)
        self.topic_scores = topic_scores or {}

    def calculate(self, data: pd.DataFrame) -> pd.Series:
        if self.topic_scores and "code" in data.columns:
            return data["code"].map(self.topic_scores).fillna(0)
        return pd.Series(0, index=data.index)
