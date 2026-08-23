"""RSI因子：相对强弱指标"""
import pandas as pd
import numpy as np
from ..base import FactorCalculator


class RSIFactor(FactorCalculator):
    """RSI因子

    RSI = 100 - 100 / (1 + RS)
    RS = N日平均涨幅 / N日平均跌幅
    用于均值回归策略：RSI超卖(低) → 做多，RSI超买(高) → 做空
    """

    def calculate(self, data: pd.DataFrame) -> pd.Series:
        period = self.config.get("period", 14)
        close = data["close"]
        delta = close.diff()
        gain = delta.where(delta > 0, 0).ewm(span=period, adjust=False).mean()
        loss = (-delta.where(delta < 0, 0)).ewm(span=period, adjust=False).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        return rsi
