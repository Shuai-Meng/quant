"""行情数据清洗

复权处理、涨跌停标记、停牌标记、收益率计算。
"""
import pandas as pd
import numpy as np


def adjust_price(df, method="qfq", price_col="close"):
    """计算复权价格

    Parameters
    ----------
    df : DataFrame
        需包含 date, close (或其他价格列)
    method : str
        'qfq' 前复权, 'hfq' 后复权

    Returns
    -------
    Series 复权后价格
    """
    if "adj_factor" not in df.columns:
        return df[price_col]
    if method == "qfq":
        # 前复权：最后一天的复权因子=1，往前算
        factor = df["adj_factor"] / df["adj_factor"].iloc[-1]
    else:
        factor = df["adj_factor"]
    return df[price_col] * factor


def mark_limit_status(df, price_col="close", pre_close_col="pre_close"):
    """标记涨跌停状态

    Returns
    -------
    DataFrame with columns: is_limit_up, is_limit_down
    """
    if pre_close_col not in df.columns:
        df["pre_close"] = df["close"].shift(1)
    result = df.copy()
    result["is_limit_up"] = (result[price_col] >= result[pre_close_col] * 1.098) & (
        result[pre_close_col] > 0
    )
    result["is_limit_down"] = (result[price_col] <= result[pre_close_col] * 0.902) & (
        result[pre_close_col] > 0
    )
    return result


def mark_suspended(df, volume_col="volume"):
    """标记停牌日（成交量为0或缺失）"""
    result = df.copy()
    result["is_suspended"] = (result[volume_col].fillna(0) == 0) | (
        result[volume_col].isna()
    )
    return result


def calc_daily_returns(df, price_col="close", group_col=None):
    """计算日收益率

    Parameters
    ----------
    df : DataFrame
        需包含 date 和价格列
    group_col : str, optional
        分组列（通常是股票代码），用于多只股票

    Returns
    -------
    DataFrame with 'daily_return' column
    """
    result = df.copy()
    if group_col and group_col in df.columns:
        result["daily_return"] = result.groupby(group_col)[price_col].pct_change()
    else:
        result["daily_return"] = result[price_col].pct_change()
    return result


def calc_monthly_returns(df, date_col="date", price_col="close", group_col="code"):
    """计算月收益率（基于每月最后一个交易日）"""
    result = df.copy()
    result["year_month"] = pd.to_datetime(result[date_col]).dt.to_period("M")

    def _monthly_ret(group):
        group = group.sort_values(date_col)
        monthly = group.groupby("year_month").last().reset_index()
        monthly["monthly_return"] = monthly[price_col].pct_change()
        return monthly

    if group_col and group_col in df.columns:
        results = []
        for name, group in result.groupby(group_col):
            mr = _monthly_ret(group)
            mr[group_col] = name
            results.append(mr)
        return pd.concat(results, ignore_index=True)
    else:
        return _monthly_ret(result)


def calc_forward_return(df, periods=1, price_col="close", group_col="code"):
    """计算未来N日收益

    用于因子检验：因子值对应的是未来收益
    """
    result = df.copy()
    if group_col in df.columns:
        result["ret_fwd"] = (
            result.groupby(group_col)[price_col]
            .pct_change(periods)
            .shift(-periods)
        )
    else:
        result["ret_fwd"] = result[price_col].pct_change(periods).shift(-periods)
    return result
