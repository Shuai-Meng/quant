#!/usr/bin/env python3
"""生成今日交易信号（多因子 Alpha 选股）

用法:
    python run_signals.py --date 2026-08-26 --top 20 --stocks 300
    python run_signals.py --refresh          # 强制刷新数据缓存
"""
import argparse
import logging
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from signals.generate import generate_signals, save_signals


def main():
    parser = argparse.ArgumentParser(description="生成今日交易信号（多因子 Alpha 选股）")
    parser.add_argument("--date", type=str, default=None, help="目标日期 YYYY-MM-DD，默认今天")
    parser.add_argument("--top", type=int, default=20, help="选股数量")
    parser.add_argument("--stocks", type=int, default=300, help="抽样股票数")
    parser.add_argument("--lookback", type=int, default=80, help="回看交易日数")
    parser.add_argument("--refresh", action="store_true", help="强制刷新数据缓存")
    parser.add_argument("--method", default="weighted",
                        choices=["weighted", "equal", "rank", "max_sharpe", "risk_parity"],
                        help="因子合成方法")
    parser.add_argument("--out", default=None, help="输出 JSON 路径（默认 state/signals/latest.json）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    date_str = args.date or date.today().strftime("%Y-%m-%d")
    print(f"\n📊 生成 {date_str} 多因子 Alpha 信号...")

    df = generate_signals(
        date_str=date_str,
        top_n=args.top,
        n_stocks=args.stocks,
        lookback_days=args.lookback,
        refresh=args.refresh,
        method=args.method,
        verbose=True,
    )
    records = save_signals(df, out_path=args.out)

    print(f"\n=== {df['date'].iloc[0]} 多因子选股 Top {len(records)} ===")
    for i, (_, r) in enumerate(df.iterrows(), 1):
        print(f"  {i:>2}. {r['code']:<10} {r['name']:<6} "
              f"alpha={r['score']:+.3f} 涨跌={r['change_pct']:+.2f}%  {r['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
