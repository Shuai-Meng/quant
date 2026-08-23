"""仓位管理 v2

参考 awesome-systematic-trading 中的 risk management 思路升级：
- ATR 止损/止盈（多倍ATR跟踪止损）
- Kelly 最优仓位 (全凯利 / 半凯利 / 四分之一凯利)
- 固定分数资金管理
- 波动率目标仓位
- 动态仓位缩放 (基于连续盈亏)
- 组合层面风险预算
"""
import numpy as np
import pandas as pd


def calc_atr(high, low, close, period=14):
    """计算平均真实波幅"""
    high, low, close = map(pd.Series, [high, low, close])
    prev_close = close.shift(1).bfill()
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=1).mean()


def atr_stop_loss(entry_price, atr_value, multiplier=2.0, direction="long"):
    """ATR 止损价位

    Parameters
    ----------
    entry_price : float
    atr_value : float
    multiplier : float
        ATR倍数，通常1.5~3.0
    direction : 'long' or 'short'

    Returns
    -------
    float: 止损价
    """
    if direction == "long":
        return entry_price - atr_value * multiplier
    return entry_price + atr_value * multiplier


def atr_take_profit(entry_price, atr_value, multiplier=4.0, direction="long"):
    """ATR 止盈价位"""
    if direction == "long":
        return entry_price + atr_value * multiplier
    return entry_price - atr_value * multiplier


def trailing_stop(prices, atr_series, multiplier=2.0, direction="long"):
    """ATR 跟踪止损线

    在持仓过程中动态上移（做多）或下移（做空）的止损线。
    """
    stop_line = pd.Series(index=prices.index, dtype=float)
    if len(prices) < 2:
        return stop_line

    if direction == "long":
        stop = prices.iloc[0] - atr_series.iloc[0] * multiplier
        for i in range(len(prices)):
            candidate = prices.iloc[i] - atr_series.iloc[i] * multiplier
            stop = max(stop, candidate)
            stop_line.iloc[i] = stop
    else:
        stop = prices.iloc[0] + atr_series.iloc[0] * multiplier
        for i in range(len(prices)):
            candidate = prices.iloc[i] + atr_series.iloc[i] * multiplier
            stop = min(stop, candidate)
            stop_line.iloc[i] = stop

    return stop_line


def kelly_position_size(win_rate, avg_win, avg_loss, fraction=0.5):
    """凯利公式仓位计算

    f* = (p * b - q) / b
    where p = win_rate, q = 1-p, b = avg_win / abs(avg_loss)

    Parameters
    ----------
    win_rate : float
        胜率 (0~1)
    avg_win : float
        平均盈利比例（正值）
    avg_loss : float
        平均亏损比例（负值或正值，内部取绝对值）
    fraction : float
        凯利分数，0.5 = 半凯利，0.25 = 四分之一凯利
    """
    avg_loss_abs = abs(avg_loss) if avg_loss != 0 else 0.01
    if avg_loss_abs == 0:
        return 0
    b = avg_win / avg_loss_abs
    q = 1 - win_rate
    kelly = (win_rate * b - q) / b if b > 0 else 0
    return max(0, min(kelly * fraction, 1.0))


def fixed_fraction_size(account_value, risk_pct, stop_loss_pct):
    """固定分数仓位

    Position = (Account * Risk%) / StopLoss%

    Parameters
    ----------
    account_value : float
        账户总资产
    risk_pct : float
        单笔风险比例 (e.g., 0.01 = 1%)
    stop_loss_pct : float
        止损幅度百分比 (e.g., 0.05 = 5%)
    """
    if stop_loss_pct <= 0:
        return 0
    return account_value * risk_pct / stop_loss_pct


def volatility_target_size(account_value, target_vol, current_vol, max_leverage=1.0):
    """波动率目标仓位

    根据当前波动率与目标波动率的比值调整仓位。

    Parameters
    ----------
    account_value : float
    target_vol : float
        目标年化波动率 (e.g., 0.15 = 15%)
    current_vol : float
        当前年化波动率
    max_leverage : float
        最大杠杆

    Returns
    -------
    float: 目标持仓金额
    """
    if current_vol <= 0:
        return account_value * max_leverage
    ratio = target_vol / current_vol
    return account_value * min(ratio, max_leverage)


def consecutive_adjustment(base_size, wins, losses, win_mult=1.2, lose_mult=0.8,
                          max_mult=2.0, min_mult=0.25):
    """基于连续盈亏动态调仓

    Top trader psychology: 连续盈利 → 适度加仓；连续亏损 → 减仓保护。

    Parameters
    ----------
    base_size : float
        基础仓位金额
    wins, losses : int
        最近连续盈利/亏损次数
    win_mult, lose_mult : float
        盈利/亏损时的调整乘数
    max_mult, min_mult : float
        最大最小倍数

    Returns
    -------
    float: 调整后仓位金额
    """
    if wins > 0:
        adj = min(win_mult ** min(wins, 5), max_mult)
    elif losses > 0:
        adj = max(lose_mult ** min(losses, 5), min_mult)
    else:
        adj = 1.0
    return base_size * adj


def equal_weight_budget(n_stocks, max_pct=0.10):
    """等权预算

    Parameters
    ----------
    n_stocks : int
        持仓数量
    max_pct : float
        单票最大仓位比例

    Returns
    -------
    float: 每只股票的仓位比例
    """
    return min(1.0 / max(n_stocks, 1), max_pct)


def erc_budget(cov_matrix, max_iter=50, tol=1e-6):
    """等风险贡献 (ERC) 仓位预算

    Parameters
    ----------
    cov_matrix : ndarray (n, n)
        协方差矩阵
    max_iter : int
        最大迭代次数
    tol : float
        收敛容差

    Returns
    -------
    ndarray: 各资产仓位权重
    """
    n = cov_matrix.shape[0]
    w = np.ones(n) / n

    for _ in range(max_iter):
        sigma_w = cov_matrix @ w
        risk_contrib = w * sigma_w
        marginal_risk = sigma_w
        total_risk = np.sqrt(w @ sigma_w)

        if total_risk < 1e-8:
            break

        target_rc = total_risk / n
        delta = (risk_contrib - target_rc) / marginal_risk
        w = w - 0.5 * delta
        w = np.maximum(w, 0.001)
        w = w / w.sum()

        if np.max(np.abs(risk_contrib - target_rc)) < tol * total_risk:
            break

    return w / w.sum()


def inverse_vol_weight(vol_series):
    """反波动率加权

    Parameters
    ----------
    vol_series : Series
        各资产年化波动率

    Returns
    -------
    Series: 权重
    """
    inv_vol = 1.0 / vol_series.replace(0, np.nan)
    return inv_vol / inv_vol.sum()


def max_position_limit(n_stocks, max_pct=0.10):
    """单一股票仓位上限"""
    return min(1.0 / max(n_stocks, 1), max_pct)
