"""风险管理层

包含：仓位计算（ATR/凯利）、回撤熔断、VaR计算、行业暴露检查。
"""
import numpy as np
import pandas as pd


def calc_atr(df, period=14):
    """计算ATR（平均真实波幅）

    Parameters
    ----------
    df : DataFrame
        需含 high, low, close 列
    period : int

    Returns
    -------
    Series
    """
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.rolling(period).mean()


def kelly_position_size(win_rate, avg_win, avg_loss):
    """凯利公式仓位计算

    f* = (p * b - q) / b
    where p = win_rate, q = 1-p, b = avg_win/avg_loss

    Parameters
    ----------
    win_rate : float
        胜率 (0~1)
    avg_win : float
        平均盈利比例
    avg_loss : float
        平均亏损比例（正值）

    Returns
    -------
    float: 凯利比例，建议取半凯利 (f*/2)
    """
    if avg_loss == 0:
        return 0
    b = avg_win / avg_loss
    q = 1 - win_rate
    kelly = (win_rate * b - q) / b if b > 0 else 0
    return max(0, min(kelly, 0.25))  # 上限25%，通常取半凯利


def atr_position_size(account_value, risk_pct, atr_value, entry_price):
    """基于ATR的仓位计算

    Position Size = (Account * Risk%) / (ATR * Multiplier)

    Parameters
    ----------
    account_value : float
        账户总价值
    risk_pct : float
        单笔风险比例（如0.01 = 1%）
    atr_value : float
        当前ATR值
    entry_price : float
        入场价格

    Returns
    -------
    int: 股数
    """
    if atr_value <= 0 or entry_price <= 0:
        return 0
    risk_amount = account_value * risk_pct
    shares = int(risk_amount / atr_value)
    return max(shares, 0)


class DrawdownController:
    """回撤熔断控制

    当组合回撤超过阈值时，触发减仓或空仓。
    """

    def __init__(self, max_drawdown=0.15, half_drawdown=0.10):
        """
        Parameters
        ----------
        max_drawdown : float
            最大回撤阈值（熔断清仓）
        half_drawdown : float
            半仓回撤阈值
        """
        self.max_dd = max_drawdown
        self.half_dd = half_drawdown
        self.peak_value = 0
        self.current_drawdown = 0

    def update(self, current_value):
        """更新回撤状态"""
        if current_value > self.peak_value:
            self.peak_value = current_value
        self.current_drawdown = (
            self.peak_value - current_value
        ) / self.peak_value if self.peak_value > 0 else 0

    def get_position_limit(self):
        """获取仓位限制

        Returns
        -------
        float: 0.0 (空仓) ~ 1.0 (满仓)
        """
        if self.current_drawdown >= self.max_dd:
            return 0.0  # 熔断
        elif self.current_drawdown >= self.half_dd:
            return 0.5  # 半仓
        return 1.0  # 正常

    def is_triggered(self):
        """是否触发了熔断"""
        return self.current_drawdown >= self.max_dd


class VaRCalculator:
    """VaR（在险价值）计算"""

    @staticmethod
    def historical_var(returns, confidence=0.95):
        """历史模拟法VaR"""
        if len(returns) == 0:
            return 0
        return np.percentile(returns, (1 - confidence) * 100)

    @staticmethod
    def parametric_var(returns, confidence=0.95):
        """参数法VaR（假设正态分布）"""
        if len(returns) == 0:
            return 0
        from scipy import stats
        mu = returns.mean()
        sigma = returns.std()
        z = stats.norm.ppf(1 - confidence)
        return mu + z * sigma

    @staticmethod
    def cvar(returns, confidence=0.95):
        """CVaR（条件VaR，即VaR之后的平均损失）"""
        if len(returns) == 0:
            return 0
        var = VaRCalculator.historical_var(returns, confidence)
        return returns[returns <= var].mean()


class ExposureChecker:
    """组合暴露检查

    检查组合在行业、因子方向的暴露是否超限。
    """

    def __init__(self, max_industry_pct=0.30):
        self.max_industry_pct = max_industry_pct

    def check_industry_exposure(self, holdings, industry_map):
        """检查行业暴露

        Parameters
        ----------
        holdings : dict
            {code: weight}
        industry_map : dict
            {code: industry}

        Returns
        -------
        dict: {industry: total_weight}
        dict: violations - 超限的行业
        """
        exposure = {}
        for code, weight in holdings.items():
            ind = industry_map.get(code, "Unknown")
            exposure[ind] = exposure.get(ind, 0) + weight

        violations = {
            ind: w for ind, w in exposure.items() if w > self.max_industry_pct
        }
        return exposure, violations

    def check_concentration(self, weights, top_pct=0.8):
        """检查集中度：前N只股票占比"""
        sorted_w = sorted(weights.values(), reverse=True)
        cumsum = np.cumsum(sorted_w)
        top_idx = np.searchsorted(cumsum, top_pct) + 1
        return {"top_n_pct": top_idx, "top_n": len(weights)}
