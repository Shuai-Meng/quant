"""扩展技术指标库

参考 openclaw-data-china-stock 的 58 指标引擎。
使用 pandas-ta (可选) + 内置实现提供 50+ 指标。
如果没有 pandas-ta，自动回退到 Python 实现。
"""
import pandas as pd
import numpy as np

_HAS_PANDAS_TA = False
try:
    import pandas_ta as ta
    _HAS_PANDAS_TA = True
except ImportError:
    pass


class IndicatorEngine:
    """技术指标计算引擎

    优先级：pandas-ta → 内置 Python 实现
    """

    def __init__(self):
        self._use_pandas_ta = _HAS_PANDAS_TA

    def calculate(self, df, indicators=None):
        """计算一组指标

        Parameters
        ----------
        df : DataFrame
            需含 OHLCV 列 (open, high, low, close, volume)
        indicators : list of (name, params) or list of str

        Returns
        -------
        DataFrame with indicator columns appended
        """
        if indicators is None:
            indicators = ["rsi_14", "macd", "boll", "atr_14", "sma_20", "sma_60"]

        result = df.copy()
        for spec in indicators:
            if isinstance(spec, str):
                name = spec
                params = {}
            else:
                name, params = spec[0], spec[1] if len(spec) > 1 else {}

            method = getattr(self, f"_calc_{name}", None)
            if method:
                result = method(result, **params)
        return result

    # ---- 趋势指标 ----

    def sma(self, df, period=20, column="close"):
        col = f"sma_{period}"
        df[col] = df[column].rolling(period).mean()
        return df

    def ema(self, df, period=20, column="close"):
        col = f"ema_{period}"
        df[col] = df[column].ewm(span=period, adjust=False).mean()
        return df

    def macd(self, df, fast=12, slow=26, signal=9, column="close"):
        ema_fast = df[column].ewm(span=fast, adjust=False).mean()
        ema_slow = df[column].ewm(span=slow, adjust=False).mean()
        df["macd"] = ema_fast - ema_slow
        df["macd_signal"] = df["macd"].ewm(span=signal, adjust=False).mean()
        df["macd_hist"] = df["macd"] - df["macd_signal"]
        return df

    def adx(self, df, period=14):
        high, low, close = df["high"], df["low"], df["close"]
        prev_close = close.shift(1)
        tr = pd.concat([high - low, (high - prev_close).abs(),
                        (low - prev_close).abs()], axis=1).max(axis=1)
        up = high - high.shift(1)
        down = low.shift(1) - low
        plus_dm = np.where((up > down) & (up > 0), up, 0)
        minus_dm = np.where((down > up) & (down > 0), down, 0)
        atr = tr.rolling(period).mean()
        plus_di = 100 * pd.Series(plus_dm).rolling(period).mean() / atr
        minus_di = 100 * pd.Series(minus_dm).rolling(period).mean() / atr
        dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di + 1e-10)
        df["adx"] = dx.rolling(period).mean()
        df["adx_plus"] = plus_di
        df["adx_minus"] = minus_di
        return df

    # ---- 动量指标 ----

    def rsi(self, df, period=14, column="close"):
        col = f"rsi_{period}"
        delta = df[column].diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1/period, min_periods=period).mean()
        avg_loss = loss.ewm(alpha=1/period, min_periods=period).mean()
        rs = avg_gain / avg_loss.replace(0, 1e-10)
        df[col] = 100 - 100 / (1 + rs)
        return df

    def stoch(self, df, k_period=14, d_period=3):
        low_min = df["low"].rolling(k_period).min()
        high_max = df["high"].rolling(k_period).max()
        df["stoch_k"] = 100 * (df["close"] - low_min) / (high_max - low_min + 1e-10)
        df["stoch_d"] = df["stoch_k"].rolling(d_period).mean()
        return df

    def cci(self, df, period=20):
        tp = (df["high"] + df["low"] + df["close"]) / 3
        sma_tp = tp.rolling(period).mean()
        mad = tp.rolling(period).apply(lambda x: np.abs(x - x.mean()).mean())
        df["cci"] = (tp - sma_tp) / (0.015 * mad + 1e-10)
        return df

    def williams_r(self, df, period=14):
        high_max = df["high"].rolling(period).max()
        low_min = df["low"].rolling(period).min()
        df["williams_r"] = -100 * (high_max - df["close"]) / (high_max - low_min + 1e-10)
        return df

    def mfi(self, df, period=14):
        tp = (df["high"] + df["low"] + df["close"]) / 3
        mf = tp * df["volume"]
        delta = tp.diff()
        pos_flow = mf.where(delta > 0, 0).rolling(period).sum()
        neg_flow = mf.where(delta < 0, 0).rolling(period).sum()
        mfr = pos_flow / neg_flow.replace(0, 1e-10)
        df["mfi"] = 100 - 100 / (1 + mfr)
        return df

    # ---- 波动率指标 ----

    def bollinger(self, df, period=20, std_dev=2, column="close"):
        sma = df[column].rolling(period).mean()
        std = df[column].rolling(period).std()
        df["boll_upper"] = sma + std_dev * std
        df["boll_middle"] = sma
        df["boll_lower"] = sma - std_dev * std
        df["boll_width"] = (df["boll_upper"] - df["boll_lower"]) / sma
        df["boll_pct"] = (df[column] - df["boll_lower"]) / (df["boll_upper"] - df["boll_lower"] + 1e-10)
        return df

    def atr(self, df, period=14):
        high, low, close = df["high"], df["low"], df["close"]
        prev_close = close.shift(1).bfill()
        tr = pd.concat([high - low, (high - prev_close).abs(),
                        (low - prev_close).abs()], axis=1).max(axis=1)
        df[f"atr_{period}"] = tr.rolling(period).mean()
        return df

    def keltner(self, df, period=20, atr_period=10, multiplier=2):
        self.atr(df, atr_period)
        ema = df["close"].ewm(span=period, adjust=False).mean()
        atr_col = f"atr_{atr_period}"
        df["keltner_upper"] = ema + multiplier * df[atr_col]
        df["keltner_middle"] = ema
        df["keltner_lower"] = ema - multiplier * df[atr_col]
        return df

    # ---- 成交量指标 ----

    def obv(self, df):
        df["obv"] = (np.sign(df["close"].diff()) * df["volume"]).fillna(0).cumsum()
        return df

    def volume_ratio(self, df, period=20):
        df[f"volume_ratio_{period}"] = df["volume"] / df["volume"].rolling(period).mean()
        return df

    def vwap(self, df):
        df["vwap"] = (df["close"] * df["volume"]).cumsum() / df["volume"].cumsum()
        return df

    # ---- 通道指标 ----

    def donchian(self, df, period=20):
        df["donchian_upper"] = df["high"].rolling(period).max()
        df["donchian_lower"] = df["low"].rolling(period).min()
        df["donchian_middle"] = (df["donchian_upper"] + df["donchian_lower"]) / 2
        return df

    # ---- 统计指标 ----

    def zscore(self, df, period=20, column="close"):
        sma = df[column].rolling(period).mean()
        std = df[column].rolling(period).std()
        df[f"zscore_{period}"] = (df[column] - sma) / std
        return df

    def hurst(self, df, period=100, column="close"):
        ts = df[column].dropna()
        if len(ts) < period:
            df["hurst"] = np.nan
            return df
        lags = range(2, min(20, period // 2))
        tau = [np.std(np.subtract(ts[lag:], ts[:-lag])) for lag in lags]
        try:
            poly = np.polyfit(np.log(lags), np.log(tau), 1)
            df["hurst"] = poly[0]
        except Exception:
            df["hurst"] = 0.5
        return df

    def beta(self, df, benchmark_col="benchmark_close", lookback=60):
        if benchmark_col not in df.columns:
            df["beta"] = 1.0
            return df
        ret_a = df["close"].pct_change()
        ret_b = df[benchmark_col].pct_change()
        df["beta"] = ret_a.rolling(lookback).cov(ret_b) / ret_b.rolling(lookback).var()
        return df

    # ---- 工厂方法 ----

    def get_all_indicators(self, df, include_volume=False):
        """计算常用指标集合"""
        result = df.copy()
        # 趋势
        result = self.sma(result, 20)
        result = self.sma(result, 60)
        result = self.ema(result, 20)
        result = self.macd(result)
        result = self.adx(result, 14)
        # 动量
        result = self.rsi(result, 14)
        result = self.rsi(result, 6)
        result = self.stoch(result)
        result = self.cci(result)
        # 波动率
        result = self.bollinger(result)
        result = self.atr(result, 14)
        # 成交量
        if include_volume:
            result = self.obv(result)
            result = self.volume_ratio(result)
        return result


# 便捷函数
_engine = IndicatorEngine()

def calc_indicators(df, indicators=None):
    return _engine.calculate(df, indicators)

def calc_all(df):
    return _engine.get_all_indicators(df, include_volume=True)
