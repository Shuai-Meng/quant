"""财务数据清洗与日期对齐

核心功能：防止未来函数——确保在时点t只能使用t之前已公告的财务数据。
"""
import pandas as pd
import numpy as np


def align_report_date(fin_df, trade_date):
    """财务数据日期对齐：筛选截至trade_date已公告的财报

    Parameters
    ----------
    fin_df : DataFrame
        需包含 announce_date 或 ann_date 列（公告日）
    trade_date : Timestamp or str
        当前交易日期

    Returns
    -------
    DataFrame，只包含公告日 <= trade_date 的数据
    """
    date_col = "ann_date" if "ann_date" in fin_df.columns else "announce_date"
    if date_col not in fin_df.columns:
        return fin_df
    return fin_df[pd.to_datetime(fin_df[date_col]) <= pd.Timestamp(trade_date)].copy()


def get_latest_financial(fin_df, date, group_col="code"):
    """在给定日期获取每个股票最新的财务数据

    Parameters
    ----------
    fin_df : DataFrame
        财务数据，需包含公告日期列和报告期列
    date : Timestamp
        当前日期
    group_col : str
        股票代码列名

    Returns
    -------
    DataFrame 每个股票最新一期的财务数据
    """
    df = align_report_date(fin_df, date)
    if df.empty:
        return df
    date_col = "ann_date" if "ann_date" in df.columns else "announce_date"
    if group_col in df.columns:
        df = df.sort_values(date_col).groupby(group_col).last().reset_index()
    return df


def calc_bp_ratio(fin_df, price_df, date, price_col="close"):
    """计算账面市值比 (Book-to-Price)

    严格对齐财务发布日期与交易日期。

    Parameters
    ----------
    fin_df : DataFrame
        财务数据，需包含 code, book_value (股东权益), ann_date
    price_df : DataFrame
        行情数据，需包含 code, date, close
    date : Timestamp
        计算日期

    Returns
    -------
    Series of BP ratios indexed by code
    """
    latest_fin = get_latest_financial(fin_df, date)
    price_slice = price_df[pd.to_datetime(price_df["date"]) == pd.Timestamp(date)]

    merged = pd.merge(
        latest_fin, price_slice, on="code", how="inner", suffixes=("_fin", "_prc")
    )
    if merged.empty:
        return pd.Series(dtype=float)

    book_col = "book_value" if "book_value" in merged.columns else "total_hldr_eqy_inc_min_int"
    mkt_cap = merged[price_col] * merged.get("total_shares", 1)

    bp = merged[book_col] / mkt_cap
    bp.index = merged["code"]
    return bp.replace([np.inf, -np.inf], np.nan)


def calc_ep_ratio(fin_df, price_df, date, price_col="close"):
    """计算盈利收益率 (Earnings-to-Price)"""
    latest_fin = get_latest_financial(fin_df, date)
    price_slice = price_df[pd.to_datetime(price_df["date"]) == pd.Timestamp(date)]

    merged = pd.merge(
        latest_fin, price_slice, on="code", how="inner", suffixes=("_fin", "_prc")
    )
    if merged.empty:
        return pd.Series(dtype=float)

    profit_col = "net_profit" if "net_profit" in merged.columns else "net_profit_is"
    if profit_col in merged.columns:
        mkt_cap = merged[price_col] * merged.get("total_shares", 1)
        ep = merged[profit_col] / mkt_cap
        ep.index = merged["code"]
        return ep.replace([np.inf, -np.inf], np.nan)
    return pd.Series(dtype=float)
