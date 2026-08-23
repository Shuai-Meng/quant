"""回测模块"""
from .engine import BacktestEngine
from .performance import calc_performance_metrics, BacktestReporter
from .cost import TradingCost
from .strategy_runner import (
    StrategyBacktest,
    get_strategy,
    list_strategies,
    register_strategy,
    resolve_date_range,
    STRATEGY_REGISTRY,
)
