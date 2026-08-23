"""东方财富数据获取器

提供龙虎榜、资金流向等数据。
"""
import time
import requests
import pandas as pd
import numpy as np
from datetime import date, timedelta
from .base import DataFetcher, retry


class EastMoneyFetcher(DataFetcher):
    """东方财富数据获取器"""

    def __init__(self):
        super().__init__("eastmoney")
        self._headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://data.eastmoney.com/",
        }

    @retry(max_attempts=3, delay=1.0)
    def get_dragon_tiger_board(self, date_str=None):
        """获取龙虎榜数据

        Parameters
        ----------
        date_str : str, optional
            日期 YYYY-MM-DD，默认昨天

        Returns
        -------
        DataFrame with 上榜股票, 净买额, 涨幅, 上榜原因
        """
        if date_str is None:
            date_str = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")

        url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
        params = {
            "reportName": "RPT_DAILYBILLBOARD_DETAILSNEW",
            "columns": "ALL",
            "filter": f"(TRADE_DATE>='{date_str}')(TRADE_DATE<='{date_str}')",
            "pageNumber": "1",
            "pageSize": "500",
            "sortTypes": "-1",
            "sortColumns": "BILLBOARD_NET_AMT",
            "source": "WEB",
            "client": "WEB",
        }
        r = requests.get(url, params=params, headers=self._headers, timeout=15)
        data = r.json()

        if not data.get("success") or not data.get("result"):
            return pd.DataFrame()

        stocks = []
        for row in data["result"].get("data", []):
            net_buy = (row.get("BILLBOARD_NET_AMT") or 0) / 10000
            stocks.append(
                {
                    "code": row.get("SECURITY_CODE", ""),
                    "name": row.get("SECURITY_NAME_ABBR", ""),
                    "reason": row.get("EXPLANATION", ""),
                    "change_pct": round(float(row.get("CHANGE_RATE") or 0), 2),
                    "net_buy_wan": round(net_buy, 1),
                    "buy_wan": round((row.get("BILLBOARD_BUY_AMT") or 0) / 10000, 1),
                    "sell_wan": round((row.get("BILLBOARD_SELL_AMT") or 0) / 10000, 1),
                    "date": date_str,
                }
            )

        return pd.DataFrame(stocks)

    @retry(max_attempts=3, delay=1.0)
    def get_individual_moneyflow(self, code, start="2020-01-01", end="2026-05-15"):
        """获取个股资金流向

        Parameters
        ----------
        code : str
            股票代码
        start, end : str
            日期范围

        Returns
        -------
        DataFrame
        """
        url = "https://push2.eastmoney.com/api/qt/stock/fflow/daykline/get"
        secid = f"1.{code}" if code.startswith("6") else f"0.{code}"
        params = {
            "secid": secid,
            "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55,f56,f57",
            "lmt": "500",
            "klt": "101",
        }
        try:
            r = requests.get(url, params=params, headers=self._headers, timeout=10)
            data = r.json()
            klines = data.get("data", {}).get("klines", [])
            rows = []
            for k in klines:
                parts = k.split(",")
                rows.append(
                    {
                        "date": parts[0],
                        "main_net": float(parts[1]) / 10000,
                        "retail_net": float(parts[3]) / 10000,
                        "main_pct": float(parts[5]) if len(parts) > 5 else 0,
                    }
                )
            return pd.DataFrame(rows)
        except Exception as e:
            return pd.DataFrame()
