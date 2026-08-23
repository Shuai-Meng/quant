"""股票池过滤

ST/*ST、次新股、停牌、金融行业、微盘股过滤。
"""
import pandas as pd
import numpy as np


def filter_st(df, name_col="name"):
    """剔除 ST / *ST 股票

    Parameters
    ----------
    df : DataFrame
    name_col : str
        股票名称列

    Returns
    -------
    DataFrame
    """
    if name_col not in df.columns:
        return df
    return df[~df[name_col].str.contains("ST|*ST", na=False)].copy()


def filter_new_stocks(df, date_col="date", list_date_col="list_date", min_days=252):
    """剔除上市不足 min_days 天的股票（次新股）

    Parameters
    ----------
    df : DataFrame
    list_date_col : str
        上市日期列
    min_days : int
        最少上市天数

    Returns
    -------
    DataFrame
    """
    if list_date_col not in df.columns:
        return df
    df[list_date_col] = pd.to_datetime(df[list_date_col])
    df[date_col] = pd.to_datetime(df[date_col])
    df["days_listed"] = (df[date_col] - df[list_date_col]).dt.days
    return df[df["days_listed"] >= min_days].drop(columns=["days_listed"]).copy()


def filter_suspended(df, suspended_col="is_suspended"):
    """剔除停牌股票"""
    if suspended_col not in df.columns:
        return df
    return df[~df[suspended_col]].copy()


def filter_industry(df, industry_col="industry", exclude_keywords=None):
    """剔除特定行业（如金融行业）

    Parameters
    ----------
    df : DataFrame
    exclude_keywords : list
        需剔除的行业关键词，默认 ['银行', '保险', '证券']
    """
    if exclude_keywords is None:
        exclude_keywords = ["银行", "保险", "证券", "信托"]
    if industry_col not in df.columns:
        return df
    mask = ~df[industry_col].str.contains("|".join(exclude_keywords), na=False)
    return df[mask].copy()


def filter_market_cap(df, min_cap=1e9, cap_col="market_cap"):
    """剔除市值过小的股票（微盘股）

    Parameters
    ----------
    df : DataFrame
    min_cap : float
        最小市值
    cap_col : str
        市值列名

    Returns
    -------
    DataFrame
    """
    if cap_col not in df.columns:
        return df
    return df[df[cap_col] >= min_cap].copy()


def filter_limit_trade(df, limit_up_col="is_limit_up", limit_down_col="is_limit_down"):
    """标记涨跌停日不可交易"""
    result = df.copy()
    result["can_trade"] = True
    if limit_up_col in df.columns:
        result.loc[result[limit_up_col], "can_trade"] = False
    if limit_down_col in df.columns:
        result.loc[result[limit_down_col], "can_trade"] = False
    return result
