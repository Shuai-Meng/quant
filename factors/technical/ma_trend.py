"""均线趋势因子：短期均线 / 长期均线 - 1"""
import pandas as pd
import numpy as np
from ..base import FactorCalculator


class MATrendFactor(FactorCalculator):
    """均线趋势因子

    因子值 = 短期均线 / 长期均线 - 1
    大于0 → 股价处于上升趋势
    小于0 → 股价处于下降趋势
    """

    def calculate(self, data: pd.DataFrame) -> pd.Series:
        short = self.config.get("short", 5)
        long_ = self.config.get("long", 20)
        close = data["close"]
        ma_short = close.rolling(short, min_periods=short).mean()
        ma_long = close.rolling(long_, min_periods=long_).mean()
        return ma_short / ma_long - 1
