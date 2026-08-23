"""专业绩效分析模块

参考 quantstats / pyfolio 等专业库，提供完整的策略绩效分析：
- 收益指标: 年化收益、累计收益、月度/年度收益表
- 风险指标: 波动率、最大回撤、VaR、CVaR、下行波动率
- 风险调整: Sharpe、Sortino、Calmar、Omega、信息比率、稳定性
- 交易指标: 胜率、盈亏比、连续盈亏、换手率
- 对比指标: 基准对比、超额收益、Alpha/Beta
"""
import numpy as np
import pandas as pd


def calc_returns(prices_or_values):
    """从价格序列计算收益率"""
    s = pd.Series(prices_or_values)
    return s.pct_change().dropna()


def calc_cumulative_returns(returns):
    """累计收益率序列"""
    return (1 + returns).cumprod()


def _annual_factor(returns):
    """根据数据频率推断年化因子"""
    if isinstance(returns.index, pd.DatetimeIndex):
        n_days = (returns.index[-1] - returns.index[0]).days
        n = max(len(returns), 1)
        freq = n_days / n if n_days > 0 else 1
        if freq < 1.5:
            return 252
        elif freq < 10:
            return 52
        elif freq < 40:
            return 12
    return 252


def calc_performance_metrics(returns, benchmark_returns=None, periods_per_year=None):
    """计算全套专业绩效指标

    Parameters
    ----------
    returns : Series or array-like
        策略收益率序列
    benchmark_returns : Series, optional
        基准收益率序列（需对齐日期）
    periods_per_year : int, optional
        年化期数，默认根据数据频率自动推断

    Returns
    -------
    dict with comprehensive performance metrics
    """
    ret = pd.Series(returns).dropna()
    if len(ret) < 2:
        return {"N_Periods": len(ret)}

    if periods_per_year is None:
        periods_per_year = _annual_factor(ret)

    n = len(ret)

    # ---- 收益指标 ----
    total_ret = (1 + ret).prod() - 1
    ann_ret = (1 + ret).prod() ** (periods_per_year / n) - 1
    ann_vol = ret.std() * np.sqrt(periods_per_year)

    cum = (1 + ret).cumprod()
    mean_return = ret.mean()

    # 月度/年度收益
    if isinstance(ret.index, pd.DatetimeIndex):
        monthly = ret.resample("ME").apply(lambda x: (1 + x).prod() - 1) if len(ret) > 20 else pd.Series(dtype=float)
        yearly = ret.resample("YE").apply(lambda x: (1 + x).prod() - 1) if len(ret) > 252 else pd.Series(dtype=float)
        best_month = monthly.max() if len(monthly) > 0 else np.nan
        worst_month = monthly.min() if len(monthly) > 0 else np.nan
        positive_months = (monthly > 0).mean() if len(monthly) > 0 else np.nan
    else:
        monthly = yearly = pd.Series(dtype=float)
        best_month = worst_month = positive_months = np.nan

    # ---- 风险指标 ----
    # 最大回撤
    rolling_max = cum.expanding().max()
    drawdown = cum / rolling_max - 1
    max_dd = drawdown.min()
    avg_dd = drawdown[drawdown < 0].mean() if (drawdown < 0).any() else 0
    dd_length = _max_drawdown_duration(drawdown)

    # 下行风险
    downside = ret[ret < 0]
    downside_vol = downside.std() * np.sqrt(periods_per_year) if len(downside) > 0 else 0

    # VaR / CVaR
    var_95 = np.percentile(ret, 5)
    cvar_95 = ret[ret <= var_95].mean() if len(ret[ret <= var_95]) > 0 else var_95
    var_99 = np.percentile(ret, 1)

    # ---- 风险调整收益 ----
    sharpe = (ann_ret - 0.03) / ann_vol if ann_vol > 0 else 0  # 默认用3%无风险
    sortino = (ann_ret - 0.03) / downside_vol if downside_vol > 0 else 0
    calmar = ann_ret / abs(max_dd) if max_dd != 0 else np.nan

    # Omega比率
    threshold = 0
    omega = ret[ret > threshold].sum() / abs(ret[ret < threshold].sum()) if ret[ret < threshold].any() and ret[ret < threshold].sum() != 0 else np.nan

    # 尾部比率 (95% percentile)
    tail_95 = np.percentile(ret, 95)
    tail_ratio = abs(tail_95 / var_95) if var_95 != 0 else np.nan

    # 稳定性 (R² of cumulative returns vs time trend)
    from scipy import stats
    try:
        x = np.arange(len(cum)).reshape(-1, 1)
        y = cum.values.reshape(-1, 1)
        slope, _, r_value, _, _ = stats.linregress(x.flatten(), y.flatten())
        stability = r_value ** 2
    except Exception:
        stability = np.nan

    # ---- 交易指标 ----
    win_rate = (ret > 0).mean()
    avg_win = ret[ret > 0].mean() if (ret > 0).any() else 0
    avg_loss = ret[ret < 0].mean() if (ret < 0).any() else 0
    profit_loss_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else np.nan

    # 连续盈亏
    win_streaks, lose_streaks = _consecutive_streaks(ret)
    max_win_streak = max(win_streaks) if win_streaks else 0
    max_lose_streak = max(lose_streaks) if lose_streaks else 0

    # 盈亏波动 (获利期的标准差 / 亏损期的标准差)
    gain_std = ret[ret > 0].std() if (ret > 0).any() else 0
    loss_std = ret[ret < 0].std() if (ret < 0).any() else 1
    gain_to_pain = abs(ret.sum() / ret[ret < 0].sum()) if ret[ret < 0].any() and ret[ret < 0].sum() != 0 else np.nan

    # ---- 滚动指标 ----
    rolling_sharpe = _rolling_sharpe(ret, min(periods_per_year, n // 2))

    # ---- 基准对比 ----
    benchmark = {}
    if benchmark_returns is not None and len(benchmark_returns) > 1:
        bench_ret = pd.Series(benchmark_returns).dropna()
        bench_n = len(bench_ret)
        bench_total = (1 + bench_ret).prod() - 1
        bench_ann = (1 + bench_ret).prod() ** (periods_per_year / bench_n) - 1
        bench_vol = bench_ret.std() * np.sqrt(periods_per_year)

        # 对齐
        aligned = pd.concat([ret, bench_ret], axis=1).dropna()
        if len(aligned) > 1:
            aligned_ret = aligned.iloc[:, 0]
            aligned_bench = aligned.iloc[:, 1]
            excess = aligned_ret - aligned_bench
            excess_n = len(excess)
            excess_ann = (1 + excess).prod() ** (periods_per_year / excess_n) - 1

            # CAPM Alpha & Beta
            cov = np.cov(aligned_ret, aligned_bench)[0, 1]
            bench_var = np.var(aligned_bench)
            beta = cov / bench_var if bench_var > 0 else 1
            alpha = ann_ret - 0.03 - beta * (bench_ann - 0.03)

            # 信息比率
            tracking_error = excess.std() * np.sqrt(periods_per_year)
            info_ratio = excess_ann / tracking_error if tracking_error > 0 else 0

            # 捕获率
            up_capture = aligned_ret[aligned_bench > 0].mean() / aligned_bench[aligned_bench > 0].mean() if aligned_bench[aligned_bench > 0].any() else np.nan
            down_capture = aligned_ret[aligned_bench < 0].mean() / aligned_bench[aligned_bench < 0].mean() if aligned_bench[aligned_bench < 0].any() else np.nan

            benchmark = {
                "Benchmark_Total": bench_total,
                "Benchmark_Annual": bench_ann,
                "Benchmark_Vol": bench_vol,
                "Excess_Return": excess_ann,
                "Tracking_Error": tracking_error,
                "Information_Ratio": info_ratio,
                "Alpha_Jensen": alpha,
                "Beta": beta,
                "Up_Capture": up_capture if not (isinstance(up_capture, float) and np.isnan(up_capture)) else np.nan,
                "Down_Capture": down_capture if not (isinstance(down_capture, float) and np.isnan(down_capture)) else np.nan,
            }

    result = {
        # 收益
        "N_Periods": n,
        "Total_Return": total_ret,
        "Annual_Return": ann_ret,
        "Annual_Vol": ann_vol,
        "Mean_Return": mean_return,
        "Best_Month": best_month,
        "Worst_Month": worst_month,
        "Positive_Months_Pct": positive_months,
        # 风险
        "Max_Drawdown": max_dd,
        "Avg_Drawdown": avg_dd,
        "Max_DD_Days": dd_length,
        "Downside_Vol": downside_vol,
        "VaR_95": var_95,
        "CVaR_95": cvar_95,
        "VaR_99": var_99,
        # 风险调整
        "Sharpe": sharpe,
        "Sortino": sortino,
        "Calmar": calmar,
        "Omega": omega,
        "Tail_Ratio": tail_ratio,
        "Stability": stability,
        "Gain_to_Pain": gain_to_pain,
        # 交易
        "Win_Rate": win_rate,
        "Avg_Win": avg_win,
        "Avg_Loss": avg_loss,
        "Profit_Loss_Ratio": profit_loss_ratio,
        "Max_Win_Streak": max_win_streak,
        "Max_Lose_Streak": max_lose_streak,
        # 滚动
        "Rolling_Sharpe_Mean": rolling_sharpe.mean() if len(rolling_sharpe) > 0 else np.nan,
        "Rolling_Sharpe_Min": rolling_sharpe.min() if len(rolling_sharpe) > 0 else np.nan,
    }
    result.update(benchmark)

    # 可选保存明细
    result["_monthly_returns"] = monthly.dropna().to_dict() if len(monthly) > 0 else {}
    result["_yearly_returns"] = yearly.dropna().to_dict() if len(yearly) > 0 else {}

    return result


# ============================================================
# 辅助函数
# ============================================================

def _max_drawdown_duration(drawdown_series):
    """最大回撤持续时间（期数）"""
    is_dd = drawdown_series < 0
    if not is_dd.any():
        return 0
    max_len = 0
    cur_len = 0
    for x in is_dd:
        if x:
            cur_len += 1
            max_len = max(max_len, cur_len)
        else:
            cur_len = 0
    return max_len


def _consecutive_streaks(returns):
    """计算连续盈亏"""
    win_streaks, lose_streaks = [], []
    cur_win, cur_lose = 0, 0
    for r in returns:
        if r > 0:
            cur_win += 1
            if cur_lose > 0:
                lose_streaks.append(cur_lose)
            cur_lose = 0
        elif r < 0:
            cur_lose += 1
            if cur_win > 0:
                win_streaks.append(cur_win)
            cur_win = 0
    if cur_win > 0:
        win_streaks.append(cur_win)
    if cur_lose > 0:
        lose_streaks.append(cur_lose)
    return win_streaks, lose_streaks


def _rolling_sharpe(returns, window):
    """滚动夏普比率"""
    if len(returns) < window or window < 2:
        return pd.Series(dtype=float)
    roll = returns.rolling(window)
    roll_mean = roll.mean()
    roll_std = roll.std()
    return np.sqrt(252) * roll_mean / roll_std


def drawdown_series(returns):
    """计算回撤序列"""
    cum = (1 + returns).cumprod()
    return cum / cum.expanding().max() - 1


# ============================================================
# 报告生成器
# ============================================================

class BacktestReporter:
    """专业回测报告生成器

    支持终端打印 + HTML报告输出。
    """

    def __init__(self, portfolio_df, benchmark_returns=None):
        self.portfolio = portfolio_df
        self.benchmark = benchmark_returns
        self._perf = None

    @property
    def perf(self):
        if self._perf is None:
            self._perf = self.summary()
        return self._perf

    def summary(self):
        if "return" not in self.portfolio.columns:
            return {}
        ret = self.portfolio["return"].dropna()
        if "date" in self.portfolio.columns and not isinstance(ret.index, pd.DatetimeIndex):
            dates = self.portfolio.loc[ret.index, "date"]
            ret.index = pd.to_datetime(dates)
        bench = pd.Series(self.benchmark) if self.benchmark is not None else None
        perf = calc_performance_metrics(ret, bench)
        perf["Avg_Holdings"] = self.portfolio["n_holdings"].mean() if "n_holdings" in self.portfolio.columns else 0
        return perf

    def print_report(self):
        perf = self.perf
        if not perf:
            print("No performance data.")
            return

        def pct(v):
            if v is None or (isinstance(v, float) and np.isnan(v)):
                return "  N/A"
            return f"{v:7.2%}"
        def num(v, d=2):
            if v is None or (isinstance(v, float) and np.isnan(v)):
                return "  N/A"
            return f"{v:{d+4}.{d}f}"
        def yn(v):
            return "是" if v else "否"

        print()
        print("=" * 65)
        print("                    回 测 绩 效 报 告")
        print("=" * 65)

        print(f"\n── 收益指标 ──")
        print(f"  累计收益率:     {pct(perf.get('Total_Return'))}")
        print(f"  年化收益率:     {pct(perf.get('Annual_Return'))}")
        print(f"  年化波动率:     {pct(perf.get('Annual_Vol'))}")
        print(f"  最佳月度:       {pct(perf.get('Best_Month'))}")
        print(f"  最差月度:       {pct(perf.get('Worst_Month'))}")
        print(f"  月度胜率:       {pct(perf.get('Positive_Months_Pct'))}")

        print(f"\n── 风险指标 ──")
        print(f"  最大回撤:       {pct(perf.get('Max_Drawdown'))}")
        print(f"  平均回撤:       {pct(perf.get('Avg_Drawdown'))}")
        print(f"  最长回撤期:     {perf.get('Max_DD_Days', 0)} 天")
        print(f"  下行波动率:     {pct(perf.get('Downside_Vol'))}")
        print(f"  VaR (95%):      {pct(perf.get('VaR_95'))}")
        print(f"  CVaR (95%):     {pct(perf.get('CVaR_95'))}")
        print(f"  VaR (99%):      {pct(perf.get('VaR_99'))}")

        print(f"\n── 风险调整收益 ──")
        print(f"  Sharpe 比率:    {num(perf.get('Sharpe'))}")
        print(f"  Sortino 比率:   {num(perf.get('Sortino'))}")
        print(f"  Calmar 比率:    {num(perf.get('Calmar'))}")
        print(f"  Omega 比率:     {num(perf.get('Omega'))}")
        print(f"  Tail Ratio:     {num(perf.get('Tail_Ratio'))}")
        print(f"  稳定性 R²:      {num(perf.get('Stability'))}")
        print(f"  Gain/Pain:      {num(perf.get('Gain_to_Pain'))}")

        print(f"\n── 交易指标 ──")
        print(f"  胜率:           {pct(perf.get('Win_Rate'))}")
        print(f"  平均盈利:       {pct(perf.get('Avg_Win'))}")
        print(f"  平均亏损:       {pct(perf.get('Avg_Loss'))}")
        print(f"  盈亏比:         {num(perf.get('Profit_Loss_Ratio'))}")
        print(f"  最长连胜:       {perf.get('Max_Win_Streak', 0)} 期")
        print(f"  最长连亏:       {perf.get('Max_Lose_Streak', 0)} 期")
        print(f"  滚动Sharpe均值: {num(perf.get('Rolling_Sharpe_Mean'))}")
        print(f"  滚动Sharpe最低: {num(perf.get('Rolling_Sharpe_Min'))}")

        if "Avg_Holdings" in perf:
            print(f"  平均持仓数:     {perf['Avg_Holdings']:.0f}")

        if "Benchmark_Annual" in perf:
            print(f"\n── 基准对比 ──")
            print(f"  基准年化收益:   {pct(perf.get('Benchmark_Annual'))}")
            print(f"  超额收益:       {pct(perf.get('Excess_Return'))}")
            print(f"  Alpha (Jensen): {pct(perf.get('Alpha_Jensen'))}")
            print(f"  Beta:           {num(perf.get('Beta'))}")
            print(f"  信息比率:       {num(perf.get('Information_Ratio'))}")
            print(f"  上行捕获率:     {pct(perf.get('Up_Capture'))}")
            print(f"  下行捕获率:     {pct(perf.get('Down_Capture'))}")

        print("=" * 65)

    def to_html(self, title="回测绩效报告"):
        """生成 HTML 格式报告"""
        perf = self.perf
        if not perf:
            return "<p>No data</p>"

        def pct(v):
            if v is None or (isinstance(v, float) and np.isnan(v)):
                return "N/A"
            return f"{v:.2%}"

        def num(v, d=2):
            if v is None or (isinstance(v, float) and np.isnan(v)):
                return "N/A"
            return f"{v:.{d}f}"

        def metric_row(label, value, formatter=pct):
            return f"<tr><td>{label}</td><td style='text-align:right'>{formatter(value)}</td></tr>"

        rows = ""

        # 收益
        rows += "<tr><th colspan='2' style='background:#1a1d27'>收益指标</th></tr>"
        for k, label in [("Total_Return","累计收益"),("Annual_Return","年化收益"),("Annual_Vol","年化波动")]:
            rows += metric_row(label, perf.get(k), pct)

        # 风险
        rows += "<tr><th colspan='2' style='background:#1a1d27'>风险指标</th></tr>"
        for k, label in [("Max_Drawdown","最大回撤"),("VaR_95","VaR 95%"),("CVaR_95","CVaR 95%")]:
            rows += metric_row(label, perf.get(k), pct)

        # 风险调整
        rows += "<tr><th colspan='2' style='background:#1a1d27'>风险调整收益</th></tr>"
        for k, label in [("Sharpe","Sharpe"),("Sortino","Sortino"),("Calmar","Calmar"),("Omega","Omega")]:
            rows += metric_row(label, perf.get(k), num)

        # 交易
        rows += "<tr><th colspan='2' style='background:#1a1d27'>交易指标</th></tr>"
        for k, label in [("Win_Rate","胜率"),("Profit_Loss_Ratio","盈亏比")]:
            rows += metric_row(label, perf.get(k), pct if k == "Win_Rate" else num)

        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>
body{{background:#0f1117;color:#e0e0e0;font-family:-apple-system,sans-serif;padding:24px}}
h2{{color:#6366f1}}table{{border-collapse:collapse;width:100%;max-width:600px;margin:12px 0}}
td,th{{padding:8px 14px;border-bottom:1px solid #2a2d3a;font-size:13px}}
th{{text-align:center;color:#6b7280}}
</style></head><body>
<h2>{title}</h2>
<table>{rows}</table>
</body></html>"""

        return html


# ============================================================
# 资产增长效应策略 (参考 awesome-list, Sharpe 0.835)
# 按照总资产增长率排序选股
# ============================================================
def calc_asset_growth(total_assets_series):
    """计算资产增长率因子"""
    return total_assets_series.pct_change(1)
