"""A股交易日历"""
import os
import pandas as pd
import akshare as ak

# 清除代理，akshare 通过代理访问不稳定
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
           "ALL_PROXY", "all_proxy", "SOCKS_PROXY", "socks_proxy"):
    if _k in os.environ:
        del os.environ[_k]

_TRADE_CAL_CACHE = None


def get_trade_calendar(start_date="20000101", end_date="20261231"):
    """获取A股交易日历"""
    global _TRADE_CAL_CACHE
    if _TRADE_CAL_CACHE is not None:
        mask = (_TRADE_CAL_CACHE["cal_date"] >= pd.Timestamp(start_date)) & (
            _TRADE_CAL_CACHE["cal_date"] <= pd.Timestamp(end_date)
        )
        return _TRADE_CAL_CACHE[mask].copy()
    cal = ak.tool_trade_date_hist_sina()
    cal = cal.rename(columns={cal.columns[0]: "trade_date"})
    cal["cal_date"] = pd.to_datetime(cal["trade_date"])
    cal = cal.drop_duplicates(subset="cal_date").reset_index(drop=True)
    _TRADE_CAL_CACHE = cal
    mask = (cal["cal_date"] >= pd.Timestamp(start_date)) & (
        cal["cal_date"] <= pd.Timestamp(end_date)
    )
    return cal[mask].copy()


def get_monthly_trade_dates(start_date, end_date):
    """获取每月最后一个交易日"""
    cal = get_trade_calendar(start_date, end_date)
    monthly = (
        cal.groupby(cal["cal_date"].dt.to_period("M"))
        .agg(me_date=("cal_date", "max"))
        .reset_index(drop=True)
    )
    return monthly


def is_trade_day(date):
    """判断是否为交易日"""
    cal = get_trade_calendar("20000101", "20261231")
    return pd.Timestamp(date) in cal["cal_date"].values
