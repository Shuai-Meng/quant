#!/usr/bin/env python3
"""生成今日交易信号

用法:
    python run_signals.py --date 2026-05-16 --top 20
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import argparse
from datetime import date, timedelta
from pipeline import QuantPipeline
from signals.generate import SignalGenerator
from factors.synthesis.combiner import FactorCombiner


def main():
    parser = argparse.ArgumentParser(description="生成今日交易信号")
    parser.add_argument("--date", type=str, default=None)
    parser.add_argument("--top", type=int, default=20)
    args = parser.parse_args()

    date_str = args.date or date.today().strftime("%Y-%m-%d")

    # 获取热点数据（每日实操时可用）
    pipe = QuantPipeline()
    hot_stocks = pipe.fetch_hot_stocks(date_str)

    print(f"\n📊 生成 {date_str} 交易信号...")
    print(f"(注意: 需连接真实数据源才能生成完整信号)")
    print(f"\n今日热点摘要: {len(hot_stocks) if hot_stocks is not None else 0} 只强势股")

    if hot_stocks is not None and not hot_stocks.empty:
        print("\n🔥 今日热点强势股 TOP 10:")
        for _, r in hot_stocks.sort_values("change_pct", ascending=False).head(10).iterrows():
            reason = r.get("reason_tags", "") or ""
            print(f"  {r['code']} {r.get('name','?')}  {r['change_pct']:+.2f}%  💡{reason[:60]}")


if __name__ == "__main__":
    main()
