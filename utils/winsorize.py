"""去极值处理

MAD法和分位数法两种去极值方式。
"""
import numpy as np
import pandas as pd


def mad_winsorize(series, n=3):
    """MAD法去极值

    Parameters
    ----------
    series : Series
    n : float
        MAD倍数，默认3

    Returns
    -------
    Series
    """
    if len(series) == 0:
        return series
    median = series.median()
    mad = (series - median).abs().median()
    if mad == 0:
        return series
    upper = median + n * 1.4826 * mad
    lower = median - n * 1.4826 * mad
    return series.clip(lower, upper)


def quantile_winsorize(series, lower=0.01, upper=0.99):
    """分位数去极值"""
    if len(series) == 0:
        return series
    q_low = series.quantile(lower)
    q_high = series.quantile(upper)
    return series.clip(q_low, q_high)


def winsorize_by_group(df, value_col, group_col, method="mad", **kwargs):
    """分组去极值（通常按截面日期分组）"""
    if method == "mad":
        return df.groupby(group_col, group_keys=False)[value_col].transform(
            lambda x: mad_winsorize(x, **kwargs)
        )
    else:
        return df.groupby(group_col, group_keys=False)[value_col].transform(
            lambda x: quantile_winsorize(x, **kwargs)
        )
