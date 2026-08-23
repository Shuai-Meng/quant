"""质量因子：ROE/毛利率等基本面质量指标"""
import pandas as pd
import numpy as np
from ..base import FactorCalculator


class QualityFactor(FactorCalculator):
    """质量因子

    使用 ROE 或毛利率衡量公司质量。
    高质量公司 → 盈利更稳健、风险较低 → 防御性溢价
    """

    def calculate(self, data: pd.DataFrame) -> pd.Series:
        metric = self.config.get("metric", "roe")
        col_map = {"roe": "roe", "gross_margin": "gross_margin", "profit_margin": "profit_margin"}
        col = col_map.get(metric, "roe")
        if col in data.columns:
            return data[col]
        return pd.Series(0, index=data.index)
