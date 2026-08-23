#!/usr/bin/env python3
"""运行策略回测

用法:
    python run_backtest.py --strategy multi_factor                  # 多因子回测（默认）
    python run_backtest.py --strategy etf_momentum                  # ETF波动交易
    python run_backtest.py --strategy momentum --period 1y          # 动量策略，过去1年
    python run_backtest.py --strategy reversal --period 6m          # 反转策略，过去半年
    python run_backtest.py --strategy trend_following --period 3y   # 趋势跟踪，过去3年
    python run_backtest.py --simulate --strategy etf_momentum       # 模拟数据测试
    python run_backtest.py --list-strategies                        # 列出所有可用策略
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import numpy as np
import json
import argparse
from datetime import datetime
from pipeline import QuantPipeline
from backtest.performance import BacktestReporter, calc_performance_metrics
from backtest.strategy_runner import (
    StrategyBacktest,
    get_strategy,
    list_strategies,
    resolve_date_range,
)
from config.settings import MARKET
from data.storage.cache import get_cached_or_fetch, cache_get, cache_set, cache_clear


def _code_add_suffix(code):
    """给纯6位数字代码添加交易所后缀"""
    code = str(code).strip()
    if "." in code:
        return code.upper()
    prefix = code[:1]
    if prefix in ("6", "9"):
        return f"{code}.SH"
    elif prefix in ("0", "3"):
        return f"{code}.SZ"
    elif prefix in ("4", "8"):
        return f"{code}.BJ"
    return code


def get_stock_list():
    """获取全A股票列表（带缓存）"""
    from data.fetchers.akshare_fetcher import AkShareFetcher
    ak = AkShareFetcher()
    df = ak.get_stock_list()
    if df is None or df.empty:
        raise RuntimeError("无法获取股票列表")
    return df


def fetch_real_data(args):
    """获取真实行情数据用于回测"""
    # 1. 股票列表
    print("获取A股股票列表...")
    stock_df = get_cached_or_fetch("stock_list_full", get_stock_list)

    codes = [_code_add_suffix(c) for c in stock_df["stock_code"].tolist()]
    codes = sorted(set(c for c in codes if c and not c.startswith(("8", "9"))))
    print(f"  全市场: {len(codes)} 只股票（沪深主板+创业板）")

    # 2. 采样
    np.random.seed(42)
    n_sample = min(args.stocks, len(codes))
    idx = np.linspace(0, len(codes) - 1, n_sample, dtype=int)
    selected = [codes[i] for i in idx]
    print(f"  选取: {len(selected)} 只股票（按代码均匀采样）")

    # 3. K线数据
    cache_key = f"kline_{args.start}_{args.end}_n{len(selected)}"
    if args.refresh and cache_get(cache_key) is not None:
        cache_clear()

    def _do_fetch():
        print(f"  正在从腾讯接口获取日K线（{len(selected)} 只股票，约需 {len(selected)*0.15:.0f} 秒）...")
        from data.fetchers.tencent import TencentFetcher
        t = TencentFetcher()
        df = t.get_batch_kline(selected, args.start, args.end)
        if not df.empty:
            cache_set(cache_key, df)
        return df

    df = get_cached_or_fetch(cache_key, _do_fetch)
    if df.empty:
        raise RuntimeError("无法获取K线数据")

    stock_count = df["code"].nunique()
    date_count = df["date"].nunique()
    print(f"  K线数据: {len(df)} 行, {stock_count} 只股票, {date_count} 个交易日")

    df = df.sort_values(["code", "date"]).reset_index(drop=True)
    return df


def generate_simulated_data(n_stocks=50, freq="B"):
    """生成模拟日频回测数据

    生成多只股票的日频 OHLC 序列，包含趋势和噪声，
    使策略能基于 lookback 计算出有效信号。
    """
    print(f"生成模拟日频回测数据 ({n_stocks}只, 日频)...")
    np.random.seed(42)

    n_days = 756  # ~3年交易日
    dates = pd.bdate_range("2023-01-01", periods=n_days, freq="B")
    codes = [f"{1000+i:06d}.SZ" for i in range(n_stocks)]

    data = []
    for i, code in enumerate(codes):
        # Each stock gets a unique random walk with drift
        drift = np.random.uniform(-0.0005, 0.0015)
        vol = np.random.uniform(0.01, 0.025)
        price = np.random.uniform(5, 50)
        for dt in dates:
            ret = drift + vol * np.random.randn()
            price *= 1 + ret
            data.append({
                "date": dt,
                "code": code,
                "close": max(price, 0.1),
                "open": price * (1 + 0.002 * np.random.randn()),
                "high": price * (1 + abs(0.005 * np.random.randn())),
                "low": price * (1 - abs(0.005 * np.random.randn())),
                "volume": np.random.randint(1e5, 1e8),
                "market_cap": np.random.lognormal(21, 0.8) * price / 10,
            })

    df = pd.DataFrame(data)
    print(f"  数据: {len(df)} 行, {df['code'].nunique()} 只, {df['date'].nunique()} 个交易日")
    return df


def format_date(s):
    """Normalize date string to YYYY-MM-DD"""
    s = s.replace("-", "").replace("/", "")
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


def save_backtest_results(portfolio, strategy_name, config=None):
    """Save backtest results to state file for web UI display."""
    if portfolio is None or portfolio.empty:
        return

    BASE = os.path.dirname(os.path.abspath(__file__))
    out_dir = os.path.join(BASE, "state", "backtest_results")
    os.makedirs(out_dir, exist_ok=True)

    # Use dates from portfolio for the returns index
    returns = portfolio["return"].dropna() if "return" in portfolio.columns else pd.Series(dtype=float)
    if len(returns) > 0 and "date" in portfolio.columns:
        dates = portfolio.loc[returns.index, "date"]
        returns.index = pd.to_datetime(dates)

    metrics = calc_performance_metrics(returns) if len(returns) > 1 else {}

    # Build equity curve (sample to max 500 points)
    equity = []
    step = max(1, len(portfolio) // 500)
    for i in range(0, len(portfolio), step):
        row = portfolio.iloc[i]
        cum_ret = row.get("cum_return", 0)
        if isinstance(cum_ret, float) and (np.isnan(cum_ret) or np.isinf(cum_ret)):
            cum_ret = 0.0
        equity.append({
            "date": str(pd.Timestamp(row["date"]).date()) if "date" in row else "",
            "value": round(float(row["total_value"]), 2) if "total_value" in row else 0,
            "cum_return": round(float(cum_ret) * 100, 2),
        })

    # Build monthly returns table
    monthly_table = []
    if len(returns) > 5 and hasattr(returns.index, 'year'):
        monthly = returns.groupby([returns.index.year, returns.index.month]).apply(
            lambda x: (1 + x).prod() - 1
        )
        for (year, month), ret in monthly.items():
            if not pd.isna(ret):
                monthly_table.append({
                    "period": f"{year}-{month:02d}",
                    "return": round(ret * 100, 2),
                })

    # Build yearly returns
    yearly_table = []
    if len(returns) > 5 and hasattr(returns.index, 'year'):
        yearly = returns.groupby(returns.index.year).apply(
            lambda x: (1 + x).prod() - 1
        )
        for year, ret in yearly.items():
            if not pd.isna(ret):
                yearly_table.append({
                    "period": str(year),
                    "return": round(ret * 100, 2),
                })

    # Clean metrics for JSON (handle NaN, inf, etc.)
    clean = {}
    for k, v in metrics.items():
        if k.startswith("_"):
            continue
        if isinstance(v, float):
            if np.isnan(v) or np.isinf(v):
                continue
            clean[k] = round(v, 4)
        elif isinstance(v, dict):
            clean[k] = {kk: round(vv, 4) if isinstance(vv, float) else vv for kk, vv in v.items()}
        else:
            clean[k] = v

    result = {
        "strategy": strategy_name,
        "config": config or {},
        "updated": datetime.now().isoformat(),
        "metrics": clean,
        "equity": equity[-500:],
        "monthly_returns": monthly_table[-24:],   # last 24 months
        "yearly_returns": yearly_table,
        "n_trade_days": len(portfolio),
        "final_value": round(float(portfolio["total_value"].iloc[-1]), 2) if "total_value" in portfolio.columns else 0,
    }

    path = os.path.join(out_dir, "latest.json")
    with open(path, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"  结果已保存: {path}")


def main():
    parser = argparse.ArgumentParser(description="策略回测")

    # Strategy selection
    parser.add_argument("--strategy", type=str, default="multi_factor",
                        help="策略名称 (默认: multi_factor)")
    parser.add_argument("--list-strategies", action="store_true",
                        help="列出所有可用策略")

    # Date range
    parser.add_argument("--start", type=str, default=None,
                        help="起始日期 YYYYMMDD")
    parser.add_argument("--end", type=str, default=None,
                        help="结束日期 YYYYMMDD")
    parser.add_argument("--period", type=str, default=None,
                        help="快捷时间段: 6m, 1y, 3y, 5y, 10y")

    # Data source
    parser.add_argument("--stocks", type=int, default=300,
                        help="采样股票数量（0=全部）")
    parser.add_argument("--simulate", action="store_true",
                        help="使用模拟数据（不联网）")
    parser.add_argument("--refresh", action="store_true",
                        help="强制刷新缓存")

    args = parser.parse_args()

    # ── List strategies ──
    if args.list_strategies:
        print("\n可用策略:")
        print("-" * 60)
        for name, desc in sorted(list_strategies().items()):
            print(f"  {name:<24s} {desc}")
        print("-" * 60)
        print("\n使用: python run_backtest.py --strategy <名称>")
        return

    strategy_name = args.strategy

    # ── Resolve date range ──
    default_start = MARKET.get("start_date", "2020-01-01").replace("-", "")
    default_end = MARKET.get("end_date", "2026-05-15").replace("-", "")

    start_raw = args.start or default_start
    end_raw = args.end or default_end

    start = format_date(start_raw)
    end = format_date(end_raw)

    if args.period:
        start, end = resolve_date_range(start_date=start, end_date=end, period=args.period)
        print(f"时间段: {args.period} ({start} ~ {end})")
    else:
        print(f"时间段: {start} ~ {end}")

    # ── Fetch data ──
    pipe = QuantPipeline()

    try:
        if args.simulate:
            df = generate_simulated_data()
        else:
            args.start = start.replace("-", "")
            args.end = end.replace("-", "")
            df = fetch_real_data(args)
    except RuntimeError as e:
        print(f"数据获取失败: {e}")
        print("可尝试: python run_backtest.py --simulate (使用模拟数据)")
        sys.exit(1)

    pipe.data["daily"] = df

    # ── Run backtest ──
    print(f"\n策略: {strategy_name}")
    print("=" * 60)

    portfolio = None
    if strategy_name == "multi_factor":
        # Use existing pipeline flow (factor computation + combiner + engine)
        portfolio = pipe.multi_factor_backtest(start_date=start, end_date=end)
    else:
        # Use StrategyBacktest adapter
        try:
            strategy_config = None
            if args.simulate and "etf" in strategy_name:
                available = sorted(df["code"].unique())
                strategy_config = {"etf_pool": available[:min(5, len(available))]}
            strategy = get_strategy(strategy_name, config=strategy_config)
            print(f"策略描述: {list_strategies().get(strategy_name, '')}")
            if args.simulate:
                print(f"  数据代码数: {len(df['code'].unique())}")
        except ValueError as e:
            print(f"错误: {e}")
            sys.exit(1)

        runner = StrategyBacktest(strategy)
        portfolio = runner.run_and_report(df, start_date=start, end_date=end)

    save_backtest_results(portfolio, strategy_name, {"period": args.period, "stocks": args.stocks})


if __name__ == "__main__":
    main()
