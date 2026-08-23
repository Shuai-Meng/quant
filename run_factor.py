#!/usr/bin/env python3
"""运行单因子检验

用法:
    python run_factor.py --factor momentum                     # 真实数据（默认200只股票）
    python run_factor.py --factor reversal --stocks 500        # 指定股票数量
    python run_factor.py --factor rsi --simulate               # 模拟数据（不联网）
    python run_factor.py --factor momentum --refresh           # 强制刷新缓存
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import numpy as np
import argparse
from pipeline import QuantPipeline
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
    """获取真实行情数据"""
    # 1. 股票列表（缓存一天）
    print("获取A股股票列表...")
    stock_df = get_cached_or_fetch("stock_list_full", get_stock_list)

    codes = [_code_add_suffix(c) for c in stock_df["stock_code"].tolist()]
    codes = sorted(set(c for c in codes if c and not c.startswith(("8", "9"))))
    print(f"  全市场: {len(codes)} 只股票")

    # 2. 按股票数量分层采样（保证沪深主板覆盖）
    np.random.seed(42)
    n_sample = min(args.stocks, len(codes))
    idx = np.linspace(0, len(codes) - 1, n_sample, dtype=int)
    selected = [codes[i] for i in idx]
    print(f"  选取: {len(selected)} 只股票（按代码均匀采样）")

    # 3. K线数据（带缓存）
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


def generate_simulated_data():
    """生成模拟测试数据"""
    print("生成模拟测试数据...")
    np.random.seed(42)
    n_stocks = 100
    n_dates = 120

    dates = pd.date_range("2015-01-01", periods=n_dates, freq="ME")
    codes = [f"{i:06d}.SZ" if i % 2 == 0 else f"{i:06d}.SH" for i in range(n_stocks)]

    data_rows = []
    for dt in dates:
        for code in codes:
            base = np.random.lognormal(mean=3, sigma=0.3)
            noise = np.random.normal(0, 0.02, 1)[0]
            close = base * (1 + noise) * (1 + 0.005 * np.random.randn())
            data_rows.append({
                "date": dt,
                "code": code,
                "close": close,
                "volume": np.random.randint(1e5, 1e8),
                "high": close * (1 + abs(np.random.normal(0, 0.02))),
                "low": close * (1 - abs(np.random.normal(0, 0.02))),
                "market_cap": np.random.lognormal(20, 1),
                "turnover": np.random.uniform(0.01, 0.10),
            })

    df = pd.DataFrame(data_rows)
    print(f"模拟数据: {len(df)} 行, {df['code'].nunique()} 只, {df['date'].nunique()} 个月")
    return df


def main():
    parser = argparse.ArgumentParser(description="单因子检验")
    parser.add_argument("--factor", type=str, default="momentum",
                        choices=["momentum", "reversal", "volume_ratio", "rsi", "ma_trend"])
    parser.add_argument("--start", type=str, default=MARKET.get("start_date", "20200101").replace("-", ""))
    parser.add_argument("--end", type=str, default=MARKET.get("end_date", "20260515").replace("-", ""))
    parser.add_argument("--stocks", type=int, default=200, help="采样股票数量（0=全部）")
    parser.add_argument("--report", type=str, default=None)
    parser.add_argument("--refresh", action="store_true", help="强制刷新缓存")
    parser.add_argument("--simulate", action="store_true", help="使用模拟数据（不联网）")
    args = parser.parse_args()

    pipe = QuantPipeline()

    try:
        if args.simulate:
            df = generate_simulated_data()
        else:
            df = fetch_real_data(args)
    except RuntimeError as e:
        print(f"数据获取失败: {e}")
        print("可尝试: python run_factor.py --simulate (使用模拟数据)")
        sys.exit(1)

    pipe.data["daily"] = df
    pipe.run_factor_test(
        factor_name=args.factor,
        config={"group_num": 5, "weight_method": "equal"},
    )


if __name__ == "__main__":
    main()
