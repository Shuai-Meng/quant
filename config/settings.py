"""全局配置"""
MARKET = {
    "start_date": "2020-01-01",
    "end_date": "2026-05-15",
    "stock_pool": "全A",
    "rebalance_freq": "monthly",
    "top_n_stocks": 20,
    "min_market_cap": 1e9,
    "min_days_listed": 252,
}

TRADING_COST = {
    "commission_rate": 0.0003,
    "stamp_tax_rate": 0.001,
    "transfer_fee": 0.00002,
    "slippage_pct": 0.001,
    "min_commission": 5,
}

RISK = {
    "max_position_pct": 0.10,
    "max_industry_pct": 0.30,
    "max_drawdown": 0.15,
    "max_daily_loss": 0.03,
    "risk_per_trade_pct": 0.01,
    "target_leverage": 1.0,
}

FACTORS = {
    "momentum": {"lookback": 20, "weight": 0.25},
    "reversal": {"lookback": 5, "weight": 0.15},
    "volume_ratio": {"lookback": 20, "weight": 0.10},
    "rsi": {"period": 14, "oversold": 30, "overbought": 70, "weight": 0.10},
    "ma_trend": {"short": 5, "long": 20, "weight": 0.15},
    "turnover_trend": {"lookback": 20, "weight": 0.10},
    "amplitude": {"lookback": 10, "weight": 0.05},
    "value_bp": {"weight": 0.10},
}

SIGNAL_WEIGHTS = {
    "technical": 0.50,
    "fund_flow": 0.25,
    "topic_heat": 0.25,
}
