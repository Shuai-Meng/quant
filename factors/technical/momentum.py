"""动量因子：过去N日收益率"""
import pandas as pd
import numpy as np
from ..base import FactorCalculator


class MomentumFactor(FactorCalculator):
    """动量因子

    因子值 = 过去N日收益率
    高动量 = 过去N日涨幅大 → 预期未来延续趋势
    """

    def calculate(self, data: pd.DataFrame) -> pd.Series:
        lookback = self.config.get("lookback", 20)
        return data["close"].pct_change(lookback)
