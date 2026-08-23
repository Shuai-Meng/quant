"""价值因子：BP (Book-to-Price) / EP (Earnings-to-Price)"""
import pandas as pd
import numpy as np
from ..base import FactorCalculator


class ValueFactor(FactorCalculator):
    """价值因子

    使用 BP 或 EP 作为价值指标。
    高 BP/EP = 股票更"便宜" → 预期未来收益更高（经典价值溢价）
    """

    def calculate(self, data: pd.DataFrame) -> pd.Series:
        metric = self.config.get("metric", "bp")
        col_map = {"bp": "bp", "ep": "ep", "pe_inv": "pe_inv"}
        col = col_map.get(metric, "bp")
        if col in data.columns:
            return data[col]
        return pd.Series(0, index=data.index)
