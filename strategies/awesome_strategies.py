"""系统化交易策略库

移植自 awesome-systematic-trading 的高夏普策略，适配 A 股市场：
- ShortTermReversal: 短期反转 (Sharpe 0.816)
- LowVolatility: 低波动因子 (Sharpe 0.717)  
- TrendFollowing: 趋势跟踪 (Sharpe 0.569)
- AssetGrowth: 资产增长效应 (Sharpe 0.835)
- PairsTrading: 配对交易 (Sharpe 0.634)
- SizeEffect: 小市值效应 (Sharpe 0.747)
"""
import pandas as pd
import numpy as np
from .multi_factor import Strategy


class ShortTermReversalStrategy(Strategy):
    """短期反转策略 (参考 awesome-list, Sharpe 0.816)

    买入过去一周跌幅最大的股票，持有1周。
    A股散户占比高，反转效应更强。
    """

    def __init__(self, name="short_term_reversal", config=None):
        super().__init__(name)
        self.config = config or {
            "lookback": 5,
            "top_n": 20,
            "rebalance_freq": "weekly",
            "min_volume": 1e6,
        }

    def generate_signals(self, data, date):
        df = data[data["date"] == pd.Timestamp(date)].copy()
        if df.empty:
            return self.positions

        # 计算过去N日收益率（反转 = 负收益 → 高因子值）
        if "return" not in df.columns and "close" in data.columns:
            df["return"] = data.groupby("code")["close"].pct_change(
                self.config["lookback"]
            ).loc[df.index]

        if "return" not in df.columns:
            return self.positions

        # 过滤低流动性
        if "volume" in df.columns:
            df = df[df["volume"] >= self.config.get("min_volume", 0)]

        # 反转：跌幅越大，得分越高
        df["reversal_score"] = -df["return"].fillna(0)

        top_n = self.config.get("top_n", 20)
        df = df.sort_values("reversal_score", ascending=False).head(top_n)

        weight = 1.0 / max(len(df), 1)
        return {row["code"]: weight for _, row in df.iterrows()}


class LowVolatilityStrategy(Strategy):
    """低波动策略 (参考 awesome-list, Sharpe 0.717)

    买入过去60天波动率最低的股票，持有1个月。
    低波动异象在A股显著存在。
    """

    def __init__(self, name="low_volatility", config=None):
        super().__init__(name)
        self.config = config or {
            "vol_lookback": 60,
            "top_n": 30,
            "rebalance_freq": "monthly",
        }

    def generate_signals(self, data, date):
        df = data[data["date"] == pd.Timestamp(date)].copy()
        if df.empty or "close" not in data.columns:
            return self.positions

        # 计算过去N日波动率
        vol_lookback = self.config["vol_lookback"]
        returns = data.groupby("code")["close"].pct_change()
        volatility = returns.groupby("code").transform(
            lambda x: x.rolling(vol_lookback).std()
        )
        df["volatility"] = volatility.loc[df.index]

        df = df.dropna(subset=["volatility"])
        # 低波动 → 高得分
        df["low_vol_score"] = -df["volatility"]

        top_n = self.config.get("top_n", 30)
        df = df.sort_values("low_vol_score", ascending=False).head(top_n)

        weight = 1.0 / max(len(df), 1)
        return {row["code"]: weight for _, row in df.iterrows()}


class TrendFollowingStrategy(Strategy):
    """趋势跟踪策略 (参考 awesome-list, Sharpe 0.569)

    买入价格在均线上方且均线斜率向上的股票。
    """

    def __init__(self, name="trend_following", config=None):
        super().__init__(name)
        self.config = config or {
            "ma_short": 20,
            "ma_long": 60,
            "top_n": 20,
            "rebalance_freq": "monthly",
        }

    def generate_signals(self, data, date):
        df = data[data["date"] == pd.Timestamp(date)].copy()
        if df.empty or "close" not in data.columns:
            return self.positions

        ma_short = self.config["ma_short"]
        ma_long = self.config["ma_long"]

        # 计算均线
        def _calc_ma(group, window):
            return group.rolling(window, min_periods=window).mean()

        ma_s = data.groupby("code")["close"].transform(
            lambda x: _calc_ma(x, ma_short)
        )
        ma_l = data.groupby("code")["close"].transform(
            lambda x: _calc_ma(x, ma_long)
        )

        df["ma_short"] = ma_s.loc[df.index]
        df["ma_long"] = ma_l.loc[df.index]
        df["close_raw"] = df["close"]

        df = df.dropna(subset=["ma_short", "ma_long"])

        # 趋势信号: 短期均线在长期均线上方 + 价格在短期均线上方
        df["trend_score"] = (
            (df["close_raw"] > df["ma_short"]).astype(float)
            + (df["ma_short"] > df["ma_long"]).astype(float)
            + (df["ma_short"] - df["ma_long"]) / df["close_raw"]
        )

        top_n = self.config.get("top_n", 20)
        df = df.sort_values("trend_score", ascending=False).head(top_n)

        weight = 1.0 / max(len(df), 1)
        return {row["code"]: weight for _, row in df.iterrows()}


class SizeEffectStrategy(Strategy):
    """小市值效应策略 (参考 awesome-list, Sharpe 0.747)

    买入市值最小的股票，年化超额显著。
    A股壳价值长期存在，小市值效应尤为突出。
    """

    def __init__(self, name="size_effect", config=None):
        super().__init__(name)
        self.config = config or {
            "top_n": 30,
            "rebalance_freq": "monthly",
            "min_price": 5,  # 过滤仙股
            "min_days_listed": 252,
        }

    def generate_signals(self, data, date):
        df = data[data["date"] == pd.Timestamp(date)].copy()
        if df.empty:
            return self.positions

        # 需要 market_cap 列
        if "market_cap" not in df.columns:
            return self.positions

        # 过滤
        min_price = self.config.get("min_price", 0)
        if min_price > 0 and "close" in df.columns:
            df = df[df["close"] >= min_price]

        # 小市值 → 高得分
        df["size_score"] = -np.log(df["market_cap"].fillna(1e12))

        top_n = self.config.get("top_n", 30)
        df = df.sort_values("size_score", ascending=False).head(top_n)

        weight = 1.0 / max(len(df), 1)
        return {row["code"]: weight for _, row in df.iterrows()}


class PairsTradingStrategy(Strategy):
    """配对交易策略 (参考 awesome-list, Sharpe 0.634)

    在相关性高的股票对之间，买入低估的、卖出高估的。
    简化版：计算每只股票与同行业均值偏离度，买入偏离度最低的。
    """

    def __init__(self, name="pairs_trading", config=None):
        super().__init__(name)
        self.config = config or {
            "lookback": 20,
            "top_n": 10,
            "rebalance_freq": "daily",
            "entry_threshold": 2.0,  # z-score 阈值
        }

    def generate_signals(self, data, date):
        df = data[data["date"] == pd.Timestamp(date)].copy()
        if df.empty or "close" not in data.columns:
            return self.positions

        lookback = self.config["lookback"]

        # 计算每只股票过去N日的标准化收益
        returns = data.groupby("code")["close"].pct_change()
        rolling_mean = returns.groupby("code").transform(
            lambda x: x.rolling(lookback).mean()
        )
        rolling_std = returns.groupby("code").transform(
            lambda x: x.rolling(lookback).std()
        )

        df["rolling_mean"] = rolling_mean.loc[df.index]
        df["rolling_std"] = rolling_std.loc[df.index]

        df = df.dropna(subset=["rolling_mean", "rolling_std"])

        # 最近一期异常收益（偏离均值程度）
        df["latest_ret"] = returns.loc[df.index]
        df["abnormal"] = (df["latest_ret"] - df["rolling_mean"]) / df["rolling_std"].replace(0, 1)

        entry_threshold = self.config.get("entry_threshold", 2.0)

        # 做多：异常大幅下跌（跌幅显著偏离均值）
        df["pair_score"] = -df["abnormal"]
        df = df[df["abnormal"] < 0]  # 只买入异常下跌的

        top_n = self.config.get("top_n", 10)
        df = df.sort_values("pair_score", ascending=False).head(top_n)

        weight = 1.0 / max(len(df), 1)
        return {row["code"]: weight for _, row in df.iterrows()}


class CombinedEffectStrategy(Strategy):
    """复合效应策略：动量 + 反转 + 波动率的组合

    参考 awesome-list 中 combining momentum/reversal with volatility effect (Sharpe 0.375)
    """

    def __init__(self, name="combined_effect", config=None):
        super().__init__(name)
        self.config = config or {
            "mom_lookback": 20,
            "rev_lookback": 5,
            "vol_lookback": 60,
            "top_n": 25,
            "rebalance_freq": "monthly",
        }

    def generate_signals(self, data, date):
        df = data[data["date"] == pd.Timestamp(date)].copy()
        if df.empty or "close" not in data.columns:
            return self.positions

        returns = data.groupby("code")["close"].pct_change()

        # 动量
        mom = returns.groupby("code").transform(
            lambda x: x.rolling(self.config["mom_lookback"]).apply(np.prod, raw=True)
        )

        # 反转
        rev = returns.groupby("code").transform(
            lambda x: x.rolling(self.config["rev_lookback"]).apply(np.prod, raw=True)
        )

        # 波动率
        vol = returns.groupby("code").transform(
            lambda x: x.rolling(self.config["vol_lookback"]).std()
        )

        df["momentum"] = mom.loc[df.index]
        df["reversal"] = rev.loc[df.index]
        df["volatility"] = vol.loc[df.index]

        df = df.dropna(subset=["momentum", "reversal", "volatility"])

        # 综合得分：动量(正向) + 反转(负向，取负号) + 低波动(负向，取负号)
        df["combined_score"] = (
            df["momentum"].fillna(0) * 0.4
            - df["reversal"].fillna(0) * 0.3
            - df["volatility"].fillna(0) / df["volatility"].std() * 0.3
        )

        top_n = self.config.get("top_n", 25)
        df = df.sort_values("combined_score", ascending=False).head(top_n)

        weight = 1.0 / max(len(df), 1)
        return {row["code"]: weight for _, row in df.iterrows()}
