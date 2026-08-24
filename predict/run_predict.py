# -*- coding: utf-8 -*-
"""Kronos 个股价格走势预测 CLI（整合进 quant）。

用法:
    python -m predict.run_predict 600900
    python -m predict.run_predict 长江电力 --months 6 --samples 20 --chart
    python -m predict.run_predict 600900 --save-mysql

输出:
    - 控制台摘要
    - CSV: state/predicts/pred_{code}_summary.csv（date + OHLCV 的 P10~P90）
    - 可选图表: state/predicts/pred_{code}_chart.png
    - 可选写库: MySQL kronos_signal 表（--save-mysql）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from predict.kronos_engine import KronosEngine, find_stock

OUT_DIR = Path(__file__).resolve().parent.parent / "state" / "predicts"

# 图表中文字体（系统已装 Noto/AR PL UMing）
import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


def build_chart(market_code: str, name: str, summary: pd.DataFrame, hist: pd.DataFrame) -> str:
    """绘制历史 + 预测中位数 + P10~P90 / P25~P75 区间带，返回 PNG 路径。"""
    plt.rcParams["font.sans-serif"] = ["Noto Serif CJK SC", "AR PL UMing CN", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    hist = hist.iloc[-500:][["date", "close"]]
    fig, ax = plt.subplots(figsize=(13, 6.5))
    ax.plot(hist["date"], hist["close"], color="#1f6fb2", lw=1.3, label="历史收盘")
    ax.plot(summary["date"], summary["close_p50"], color="#d62728", lw=1.8, ls="--",
            label="预测中位数 (P50)")
    ax.fill_between(summary["date"], summary["close_p10"], summary["close_p90"],
                    color="#d62728", alpha=0.15, label="P10~P90 区间")
    ax.fill_between(summary["date"], summary["close_p25"], summary["close_p75"],
                    color="#d62728", alpha=0.20, label="P25~P75 区间")
    last_date = hist["date"].iloc[-1]
    ax.axvline(last_date, color="gray", ls=":", lw=1)
    ax.text(last_date, ax.get_ylim()[1] * 0.99, "预测起点", fontsize=10, color="gray")
    ax.set_title(f"Kronos 预测: {name} ({market_code}) 未来 {len(summary)} 个交易日",
                 fontsize=13)
    ax.set_xlabel("日期")
    ax.set_ylabel("收盘价 (元)")
    ax.legend(loc="upper left", fontsize=10)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    png_path = OUT_DIR / f"pred_{market_code.lower()}_chart.png"
    fig.savefig(png_path, dpi=130)
    plt.close(fig)
    return str(png_path)


def build_features_json(market_code: str, summary: pd.DataFrame, model_version: str) -> dict:
    """将预测序列压缩为 features_json（用于 kronos_signal 表）。"""
    idx = [0, len(summary) // 6, len(summary) // 2, len(summary) - 1]
    horizon = []
    for i in idx:
        r = summary.iloc[i]
        horizon.append({
            "date": str(pd.to_datetime(r["date"]).date()),
            "p10": round(float(r["close_p10"]), 3),
            "p50": round(float(r["close_p50"]), 3),
            "p90": round(float(r["close_p90"]), 3),
        })
    return {
        "stock_code": market_code,
        "pred_len": int(len(summary)),
        "horizon": horizon,
        "hi_p90": float(summary["close_p90"].max()),
        "lo_p10": float(summary["close_p10"].min()),
        "model_version": model_version,
    }


def render_summary(market_code: str, name: str, summary: pd.DataFrame,
                   last_close: float) -> None:
    """控制台摘要。"""
    dates = pd.to_datetime(summary["date"])
    print("=" * 62)
    print(f"Kronos 预测: {name}（{market_code}）")
    print(f"预测区间: {dates.iloc[0].date()} ~ {dates.iloc[-1].date()} "
          f"({len(summary)} 个交易日)")
    print(f"最新收盘: {last_close:.2f}")
    print("-" * 62)
    n = len(summary)
    for label, i in [("1个月", n // 6), ("3个月", n // 2), ("期末", n - 1)]:
        p50 = summary["close_p50"].iloc[i]
        p10 = summary["close_p10"].iloc[i]
        p90 = summary["close_p90"].iloc[i]
        chg = (p50 / last_close - 1) * 100
        print(f"  {label:<6} P50 {p50:>7.2f}  "
              f"(P10 {p10:>7.2f} ~ P90 {p90:>7.2f})  中位涨跌 {chg:+6.1f}%")
    print(f"  未来区间   {summary['close_p10'].min():.2f} ~ "
          f"{summary['close_p90'].max():.2f}")
    final_chg = (summary["close_p50"].iloc[-1] / last_close - 1) * 100
    print("-" * 62)
    print(f"  期末中位涨跌: {final_chg:+.1f}%")


def main() -> None:
    parser = argparse.ArgumentParser(description="Kronos 个股价格走势预测")
    parser.add_argument("query", help="股票代码（600900/SH600900）或名称（长江电力）")
    parser.add_argument("--months", type=int, default=6,
                        help="预测月数（按每月 21 个交易日折算），默认 6")
    parser.add_argument("--samples", type=int, default=20,
                        help="采样轨迹数（概率区间），默认 20")
    parser.add_argument("--lookback", type=int, default=400,
                        help="历史回望窗口（交易日），默认 400")
    parser.add_argument("--device", default="cuda:0",
                        help="推理设备，默认 cuda:0（可用 cpu）")
    parser.add_argument("--chart", action="store_true", help="同时输出预测图表 PNG")
    parser.add_argument("--save-mysql", action="store_true",
                        help="将预测结果写入 MySQL kronos_signal 表")
    parser.add_argument("--model", default=None, help="覆盖 Kronos 模型名（默认取配置）")
    args = parser.parse_args()

    found = find_stock(args.query)
    if not found:
        print(f"未在 stock.db 中找到: {args.query}", file=sys.stderr)
        raise SystemExit(1)
    market_code, name, _ = found
    pred_len = max(1, args.months * 21)

    engine = KronosEngine.get(device=args.device, model_name=args.model)
    print(f"加载模型 {engine.model_name} …", flush=True)
    summary, _, last_close, hist = engine.predict(
        market_code, pred_len=pred_len, lookback=args.lookback, n_samples=args.samples,
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = OUT_DIR / f"pred_{market_code.lower()}_summary.csv"
    summary.to_csv(csv_path, index=False)
    render_summary(market_code, name, summary, last_close)
    print(f"\nCSV 已保存: {csv_path}")

    if args.chart:
        png = build_chart(market_code, name, summary, hist)
        print(f"图表已保存: {png}")

    if args.save_mysql:
        model_version = f"{engine.model_name}"
        features = build_features_json(market_code, summary, model_version)
        direction = "up" if summary["close_p50"].iloc[-1] > last_close else "down"
        # 概率近似：P50 预测序列高于最新收盘的交易日占比（0~1）
        prob = float(np.mean(summary["close_p50"].values > last_close))
        if not 0.0 < prob < 1.0:
            prob = None
        trade_date = pd.to_datetime(hist["date"].iloc[-1]).date()
        from datacenter.mysql_db import save_kronos_signal
        save_kronos_signal(
            stock_code=market_code.lower(),
            trade_date=trade_date,
            signal_type=direction,
            probability=prob,
            features_json=features,
            model_version=model_version,
        )
        print(f"已写入 MySQL kronos_signal: {market_code.lower()} @ {trade_date} ({direction})")


if __name__ == "__main__":
    main()
