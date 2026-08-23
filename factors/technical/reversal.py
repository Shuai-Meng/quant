"""反转因子：短期收益率的负值

A股个人投资者占比高，过度交易导致价格短期超调后快速修正。
"""
import pandas as pd
import numpy as np
from ..base import FactorCalculator


class ReversalFactor(FactorCalculator):
    """短期反转因子

    因子值 = -过去N日收益率
    短期涨幅大的 → 预期回调
    短期跌幅大的 → 预期反弹
    """

    def calculate(self, data: pd.DataFrame) -> pd.Series:
        lookback = self.config.get("lookback", 5)
        momentum = data["close"].pct_change(lookback)
        return -momentum
