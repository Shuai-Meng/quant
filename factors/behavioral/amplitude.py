"""振幅因子：日内波动率"""
import pandas as pd
import numpy as np
from ..base import FactorCalculator


class AmplitudeFactor(FactorCalculator):
    """振幅因子

    因子值 = 过去N日的日内振幅均值 (high - low) / close
    高振幅 → 多空分歧大、不确定性高 → 散户彩票偏好 → 未来收益低
    """

    def calculate(self, data: pd.DataFrame) -> pd.Series:
        lookback = self.config.get("lookback", 10)
        amp = (data["high"] - data["low"]) / data["close"].replace(0, np.nan)
        return amp.rolling(lookback, min_periods=5).mean()
