"""Strategy-based backtest runner.

Bridges Strategy subclasses with BacktestEngine.
Allows running any strategy from the library via CLI.

Usage:
    from backtest.strategy_runner import StrategyBacktest, get_strategy
    strategy = get_strategy("etf_momentum")
    runner = StrategyBacktest(strategy)
    portfolio = runner.run(data, start_date="2024-01-01", end_date="2025-01-01")
"""
import pandas as pd
import numpy as np

from backtest.engine import BacktestEngine
from backtest.performance import BacktestReporter

# ── Strategy registry ─────────────────────────────────────────

STRATEGY_REGISTRY = {}


def register_strategy(name, strategy_class, description=""):
    STRATEGY_REGISTRY[name] = {
        "class": strategy_class,
        "description": description,
    }


def get_strategy(name, config=None):
    if name not in STRATEGY_REGISTRY:
        raise ValueError(
            f"Unknown strategy: {name}. "
            f"Available: {list(STRATEGY_REGISTRY.keys())}"
        )
    cls = STRATEGY_REGISTRY[name]["class"]
    return cls(config=config)


def list_strategies():
    return {k: v["description"] for k, v in STRATEGY_REGISTRY.items()}


# ── Register all available strategies ────────────────────────

def _register_all():
    # strategies from multi_factor.py
    from strategies.multi_factor import (
        MultiFactorStrategy,
        MomentumStrategy,
        ReversalStrategy,
    )
    register_strategy("multi_factor", MultiFactorStrategy, "多因子复合策略 (alpha评分选股)")
    register_strategy("momentum", MomentumStrategy, "纯动量策略 (过去N日涨幅Top N)")
    register_strategy("reversal", ReversalStrategy, "反转策略 (过去N日跌幅Top N)")

    # strategies from awesome_strategies.py
    from strategies.awesome_strategies import (
        ShortTermReversalStrategy,
        LowVolatilityStrategy,
        TrendFollowingStrategy,
        SizeEffectStrategy,
        PairsTradingStrategy,
        CombinedEffectStrategy,
    )
    register_strategy("short_term_reversal", ShortTermReversalStrategy, "短期反转 (周度)")
    register_strategy("low_volatility", LowVolatilityStrategy, "低波动防御 (月度)")
    register_strategy("trend_following", TrendFollowingStrategy, "趋势跟踪 (均线)")
    register_strategy("size_effect", SizeEffectStrategy, "小市值效应")
    register_strategy("pairs_trading", PairsTradingStrategy, "配对交易 (行业偏离)")
    register_strategy("combined_effect", CombinedEffectStrategy, "复合效应 (动量+反转+低波)")

    # strategies from etf_momentum.py
    from strategies.etf_momentum import ETFMomentumStrategy
    register_strategy("etf_momentum", ETFMomentumStrategy, "ETF波动交易 (周度轮动)")


_register_all()

# ── Period presets ────────────────────────────────────────────

PERIOD_PRESETS = {
    "6m": 182,
    "1y": 365,
    "3y": 1095,
    "5y": 1825,
    "10y": 3653,
}


def resolve_date_range(start_date=None, end_date=None, period=None):
    """Resolve date range, supporting period presets.

    Parameters
    ----------
    start_date : str or None
    end_date : str or None
    period : str or None
        One of: 6m, 1y, 3y, 5y, 10y

    Returns
    -------
    (start_date_str, end_date_str) in YYYY-MM-DD format
    """
    if period:
        if period not in PERIOD_PRESETS:
            raise ValueError(f"Unknown period: {period}. Use: {list(PERIOD_PRESETS.keys())}")
        end = pd.Timestamp(end_date) if end_date else pd.Timestamp.now()
        start = end - pd.Timedelta(days=PERIOD_PRESETS[period])
        return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

    if end_date and not start_date:
        end = pd.Timestamp(end_date)
        start = end - pd.Timedelta(days=365)  # default 1 year
        return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

    return start_date, end_date


# ── StrategyBacktest adapter ─────────────────────────────────

class StrategyBacktest:
    """Bridge between Strategy classes and BacktestEngine.

    Calls strategy.generate_signals() for each date to build
    a signal DataFrame, then feeds it to BacktestEngine.

    Usage:
        strategy = ETFMomentumStrategy()
        runner = StrategyBacktest(strategy)
        portfolio = runner.run(data, start_date="...", end_date="...")
    """

    def __init__(self, strategy, config=None):
        self.strategy = strategy
        self.config = config or {}

    def run(self, data, start_date=None, end_date=None):
        """Run strategy backtest.

        Parameters
        ----------
        data : DataFrame
            Must contain columns: date, code, close, ...
        start_date, end_date : str or None

        Returns
        -------
        DataFrame : daily portfolio snapshot
        """
        data = data.copy()

        # Determine tradeable date range
        all_dates = sorted(data["date"].unique())
        trade_dates = all_dates

        if start_date:
            trade_dates = [d for d in all_dates if d >= pd.Timestamp(start_date)]
        if end_date:
            trade_dates = [d for d in trade_dates if d <= pd.Timestamp(end_date)]

        if not trade_dates:
            print("No trade dates in range.")
            return pd.DataFrame()

        # Build signal DataFrame by calling the strategy for each date
        # NOTE: strategy receives full data (including pre-start_date history)
        # so it can compute lookback-based indicators correctly.
        signal_rows = []
        for date in trade_dates:
            target = self.strategy.generate_signals(data, date)
            for code, weight in target.items():
                if weight > 0:
                    signal_rows.append({"date": date, "code": code, "alpha": weight})

        signal_df = (
            pd.DataFrame(signal_rows)
            if signal_rows
            else pd.DataFrame(columns=["date", "code", "alpha"])
        )

        # Configure engine
        # Use daily rebalance so the strategy's own timing governs trading
        engine_config = {
            "rebalance_freq": "daily",
            "top_n_stocks": 1,
            "commission": 0.0003,
            "stamp_tax": 0.001,
            "slippage": 0.001,
            "initial_capital": 1_000_000,
        }
        engine_config.update(self.config)

        engine = BacktestEngine(engine_config)
        engine.set_universe(data)
        engine.set_signal(signal_df, signal_col="alpha")

        portfolio = engine.run(start_date=start_date, end_date=end_date)
        return portfolio

    def run_and_report(self, data, start_date=None, end_date=None):
        """Run backtest and print performance report."""
        portfolio = self.run(data, start_date, end_date)
        if portfolio.empty:
            print("No results.")
            return portfolio

        reporter = BacktestReporter(portfolio)
        reporter.print_report()
        return portfolio
