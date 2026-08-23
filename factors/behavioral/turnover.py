"""换手率趋势因子：当日换手率 / 过去N日均换手率"""
import pandas as pd
import numpy as np
from ..base import FactorCalculator


class TurnoverTrendFactor(FactorCalculator):
    """换手率趋势因子

    因子值 = 当日换手率 / 过去N日均换手率
    换手率突增 → 交投活跃、散户注意力驱动 → 短期高估后回调
    """

    def calculate(self, data: pd.DataFrame) -> pd.Series:
        lookback = self.config.get("lookback", 20)
        turnover = data.get("turnover", data.get("turnover_rate"))
        if turnover is None:
            return pd.Series(0, index=data.index)
        avg = turnover.rolling(lookback, min_periods=5).mean()
        return turnover / avg.replace(0, np.nan)
