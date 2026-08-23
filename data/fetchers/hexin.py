"""同花顺数据获取器

提供同花顺热点强势股、北向资金流向、行业板块排名等数据。
"""
import time
import requests
import pandas as pd
import numpy as np
from datetime import date, timedelta
from .base import DataFetcher, retry


class HexinFetcher(DataFetcher):
    """同花顺数据获取器"""

    def __init__(self):
        super().__init__("hexin")
        self._headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }

    @retry(max_attempts=3, delay=1.0)
    def get_harden_stocks(self, date_str=None):
        """获取同花顺热点强势股（含题材归因）

        Parameters
        ----------
        date_str : str, optional
            日期 YYYY-MM-DD，默认昨天

        Returns
        -------
        DataFrame with code, name, reason(题材归因), zhangfu, huanshou, chengjiaoe
        """
        if date_str is None:
            date_str = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")

        url = (
            f"http://zx.10jqka.com.cn/event/api/getharden/"
            f"date/{date_str}/orderby/date/orderway/desc/charset/GBK/"
        )
        r = requests.get(url, headers=self._headers, timeout=10)
        data = r.json()
        rows = data.get("data", [])

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        rename_map = {
            "name": "name", "code": "code", "reason": "reason_tags",
            "close": "close", "zhangfu": "change_pct",
            "huanshou": "turnover", "chengjiaoe": "amount",
            "ddejingliang": "dde_net",
        }
        df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})
        df["change_pct"] = pd.to_numeric(df.get("change_pct", 0), errors="coerce")
        df["date"] = pd.Timestamp(date_str)
        return df

    @retry(max_attempts=3, delay=1.0)
    def get_northbound_flow(self, date_str=None):
        """获取北向资金流向

        Returns
        -------
        dict with hgt_total, sgt_total, hgt_flow, sgt_flow
        """
        headers = {
            **self._headers,
            "Host": "data.hexin.cn",
            "Referer": "https://data.hexin.cn/",
        }
        r = requests.get(
            "https://data.hexin.cn/market/hsgtApi/method/dayChart/",
            headers=headers,
            timeout=10,
        )
        d = r.json()
        hgt = [h for h in d.get("hgt", []) if h is not None]
        sgt = [s for s in d.get("sgt", []) if s is not None]

        if not hgt or not sgt:
            return {"hgt_total": 0, "sgt_total": 0, "hgt_flow": 0, "sgt_flow": 0}

        return {
            "hgt_total": hgt[-1],
            "sgt_total": sgt[-1],
            "hgt_flow": hgt[-1] - hgt[0],
            "sgt_flow": sgt[-1] - sgt[0],
        }

    @retry(max_attempts=3, delay=1.0)
    def get_industry_ranking(self):
        """获取同花顺行业板块涨跌排名"""
        import akshare as ak

        df = ak.stock_board_industry_summary_ths()
        if not df.empty:
            df = df.rename(
                columns={
                    "板块": "industry", "涨跌幅": "change_pct",
                    "上涨家数": "up_count", "下跌家数": "down_count",
                    "领涨股": "leader", "总成交额": "total_amount",
                }
            )
            df = df.sort_values("change_pct", ascending=False).reset_index(drop=True)
        return df
