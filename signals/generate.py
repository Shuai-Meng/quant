"""日常信号生成

在数据层和因子层之上，生成每日可交易的股票信号。
"""
import pandas as pd
import numpy as np
from datetime import date, timedelta


class SignalGenerator:
    """信号生成器

    结合多因子评分和筛选条件，输出每日买卖信号。
    """

    def __init__(self, factor_combiner, config=None):
        """
        Parameters
        ----------
        factor_combiner : FactorCombiner
            已配置好的因子合成器
        config : dict
        """
        self.combiner = factor_combiner
        self.config = config or {}
        self._latest_signal = None

    def generate(self, date_str=None, top_n=20, universe_filter=None):
        """生成信号

        Parameters
        ----------
        date_str : str
            日期 YYYY-MM-DD，默认今天
        top_n : int
            选股数量
        universe_filter : callable, optional
            额外的股票筛选函数

        Returns
        -------
        DataFrame with code, alpha, rank
        """
        if date_str is None:
            date_str = date.today().strftime("%Y-%m-%d")

        alpha = self.combiner.combine()
        if alpha is None or alpha.empty:
            return pd.DataFrame(columns=["code", "alpha", "rank"])

        # 筛选当日
        today_alpha = alpha[
            pd.to_datetime(alpha["date"]) == pd.Timestamp(date_str)
        ].copy()

        if today_alpha.empty:
            return pd.DataFrame(columns=["code", "alpha", "rank"])

        # 应用额外筛选
        if universe_filter:
            today_alpha = today_alpha[universe_filter(today_alpha)]

        # 排序选Top N
        today_alpha = today_alpha.sort_values("alpha", ascending=False)
        today_alpha["rank"] = range(1, len(today_alpha) + 1)
        self._latest_signal = today_alpha.head(top_n)

        return self._latest_signal[["code", "alpha", "rank"]]

    def get_top_stocks(self, n=10):
        """获取排名前N的股票"""
        if self._latest_signal is None:
            return []
        return self._latest_signal.head(n)["code"].tolist()

    def get_buy_list(self, current_holdings=None):
        """获取买入清单（去重已有持仓）"""
        if self._latest_signal is None:
            return []
        signals = self._latest_signal.copy()
        if current_holdings:
            signals = signals[~signals["code"].isin(current_holdings)]
        return signals["code"].tolist()
