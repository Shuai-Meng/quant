"""横截面标准化

z-score标准化和秩标准化。
"""
import numpy as np
import pandas as pd


def zscore_standardize(series):
    """横截面z-score标准化

    series 需已经过去极值处理
    """
    mean, std = series.mean(), series.std()
    if std == 0:
        return series - mean
    return (series - mean) / std


def rank_standardize(series):
    """秩标准化 (映射到0~1之间)"""
    ranks = series.rank()
    return (ranks - 1) / (len(ranks) - 1)


def standardize_by_group(df, value_col, group_col, method="zscore"):
    """按分组标准化（通常按截面日期）"""
    if method == "zscore":
        return df.groupby(group_col, group_keys=False)[value_col].transform(zscore_standardize)
    else:
        return df.groupby(group_col, group_keys=False)[value_col].transform(rank_standardize)
