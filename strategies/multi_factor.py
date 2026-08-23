"""策略基类和具体策略实现"""
import pandas as pd
import numpy as np
from abc import ABC, abstractmethod


class Strategy(ABC):
    """策略基类"""

    def __init__(self, name="base_strategy"):
        self.name = name
        self.positions = {}  # code -> shares
        self.trade_log = []

    @abstractmethod
    def generate_signals(self, data, date):
        """生成调仓信号

        Parameters
        ----------
        data : DataFrame
            全市场数据
        date : Timestamp
            当前日期

        Returns
        -------
        dict: {code: weight} 目标持仓权重
        """
        ...

    def on_before_market_open(self, date):
        """盘前回调"""
        pass

    def on_after_market_close(self, date, portfolio_value):
        """盘后回调"""
        pass


class MultiFactorStrategy(Strategy):
    """多因子复合策略

    使用综合Alpha评分，选Top N股票，等权或市值加权持仓。
    """

    def __init__(self, name="multi_factor", config=None):
        super().__init__(name)
        self.config = config or {
            "top_n": 20,
            "weight_method": "equal",
            "rebalance_freq": "monthly",
            "min_market_cap": 1e9,
            "max_industry_pct": 0.30,
        }

    def generate_signals(self, data, date):
        """基于因子评分生成信号"""
        df = data[data["date"] == pd.Timestamp(date)].copy()
        if df.empty:
            return self.positions

        top_n = self.config.get("top_n", 20)

        # 筛选条件
        min_cap = self.config.get("min_market_cap", 0)
        if min_cap > 0 and "market_cap" in df.columns:
            df = df[df["market_cap"] >= min_cap]

        df = df.sort_values("alpha", ascending=False).head(top_n)
        target = {}

        method = self.config.get("weight_method", "equal")
        if method == "equal":
            weight = 1.0 / max(len(df), 1)
            for _, row in df.iterrows():
                target[row["code"]] = weight
        elif method == "market_cap" and "market_cap" in df.columns:
            total_cap = df["market_cap"].sum()
            for _, row in df.iterrows():
                target[row["code"]] = row["market_cap"] / total_cap
        elif method == "alpha_weighted":
            total_alpha = df["alpha"].sum()
            if total_alpha > 0:
                for _, row in df.iterrows():
                    target[row["code"]] = max(row["alpha"], 0) / total_alpha
            else:
                weight = 1.0 / max(len(df), 1)
                for _, row in df.iterrows():
                    target[row["code"]] = weight

        return target


class MomentumStrategy(Strategy):
    """纯动量策略：买入过去N日涨幅最大的Top N"""

    def __init__(self, name="momentum", config=None):
        super().__init__(name)
        self.config = config or {"lookback": 20, "top_n": 20}

    def generate_signals(self, data, date):
        df = data[data["date"] == pd.Timestamp(date)].copy()
        if df.empty or "momentum" not in df.columns:
            return self.positions
        top_n = self.config.get("top_n", 20)
        df = df.sort_values("momentum", ascending=False).head(top_n)
        weight = 1.0 / max(len(df), 1)
        return {row["code"]: weight for _, row in df.iterrows()}


class ReversalStrategy(Strategy):
    """反转策略：买入过去N日跌幅最大的Top N"""

    def __init__(self, name="reversal", config=None):
        super().__init__(name)
        self.config = config or {"lookback": 5, "top_n": 20}

    def generate_signals(self, data, date):
        df = data[data["date"] == pd.Timestamp(date)].copy()
        if df.empty or "reversal" not in df.columns:
            return self.positions
        top_n = self.config.get("top_n", 20)
        df = df.sort_values("reversal", ascending=False).head(top_n)
        weight = 1.0 / max(len(df), 1)
        return {row["code"]: weight for _, row in df.iterrows()}
