"""量比因子：当日成交量 / 过去N日均量"""
import pandas as pd
import numpy as np
from ..base import FactorCalculator


class VolumeRatioFactor(FactorCalculator):
    """量比因子

    因子值 = 当日成交量 / 过去N日均量
    量比突增 → 资金关注度提升 → 短期动量
    """

    def calculate(self, data: pd.DataFrame) -> pd.Series:
        lookback = self.config.get("lookback", 20)
        volume = data["volume"]
        avg_vol = volume.rolling(lookback, min_periods=5).mean()
        return volume / avg_vol.replace(0, np.nan)
