# -*- coding: utf-8 -*-
"""Kronos 预测引擎（逻辑整合进 quant 项目）。

数据流：HDF5 直读 → KronosPredictor 采样预测 → 概率区间聚合 → 结果表。
模型权重从 HuggingFace 加载（缓存于 ~/.cache/huggingface，全局共享），
代码来自外部 Kronos 仓库（vendor 引用，见 datacenter.config.KRONOS_HOME）。

注意：本模块禁止 import hikyuu，以免 C++ 扩展与 torch 的 TLS 加载顺序
相互干扰（hikyuu 必须先于 numpy/torch 导入，而预测进程不需要 hikyuu）。
"""
from __future__ import annotations

import os
import sqlite3
import sys

import numpy as np
import pandas as pd
import tables as tb

from datacenter.config import DATA_DIR, KRONOS_HOME, KRONOS_MODEL, KRONOS_TOKENIZER

# 成交额/成交量换算（H5 原始存储单位）
AMOUNT_UNIT = 1000.0   # 千元 → 元
VOL_UNIT = 100.0       # 手 → 股


def limit_rate_for(market_code: str) -> float:
    """按板块返回单日涨跌停幅度：主板 10%、创业板/科创板 20%、北证 30%。"""
    if market_code.startswith("BJ"):
        return 0.30
    code = market_code[2:]
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    return 0.10


def find_stock(query: str):
    """在 stock.db 定位股票，返回 (market_code, name, marketid) 或 None。

    直接使用 sqlite3 标准库，不依赖 hikyuu。
    支持：'600900'（6位代码）、'SH600900'（带市场前缀）、'长江电力'（名称模糊）。
    """
    q = query.strip().upper()
    mkt_id = {"SH": 1, "SZ": 2, "BJ": 3}
    conn = sqlite3.connect(str(DATA_DIR / "stock.db"))
    try:
        rows = None
        if len(q) == 8 and q[:2] in mkt_id:
            rows = conn.execute(
                "SELECT code, name, marketid FROM stock WHERE code=? AND marketid=?",
                (q[2:], mkt_id[q[:2]]),
            ).fetchall()
        if not rows and q.isdigit() and len(q) == 6:
            rows = conn.execute(
                "SELECT code, name, marketid FROM stock WHERE code=?", (q,)
            ).fetchall()
        if not rows:
            rows = conn.execute(
                "SELECT code, name, marketid FROM stock WHERE name LIKE ?",
                (f"%{query}%",),
            ).fetchall()
    finally:
        conn.close()
    if not rows:
        return None
    code, name, marketid = rows[0]
    market = {1: "SH", 2: "SZ", 3: "BJ"}[marketid]
    return f"{market}{code}", name, marketid


def read_h5(market_code: str) -> pd.DataFrame:
    """读取单只股票全量日线，返回 date/open/high/low/close/volume/amount。

    价格统一换算为元（H5 存储 ×1000），volume 为股（H5 存储为手），
    amount 为元（H5 存储为千元）。自动去重、排序、清洗 open==0。
    """
    market = market_code[:2].lower()
    p = DATA_DIR / f"{market}_day.h5"
    if not p.exists():
        raise FileNotFoundError(f"H5 数据文件不存在: {p}")
    with tb.open_file(str(p), "r") as f:
        t = f.get_node("/data", market_code)
        rows = t.read()

    df = pd.DataFrame(
        {
            # datetime 为 yyyyMMddHHmm（12 位），整除 10000 得 yyyyMMdd
            "date": pd.to_datetime((rows["datetime"] // 10000).astype(str), format="%Y%m%d"),
            "open": rows["openPrice"] / 1000.0,
            "high": rows["highPrice"] / 1000.0,
            "low": rows["lowPrice"] / 1000.0,
            "close": rows["closePrice"] / 1000.0,
            "volume": rows["transCount"] * VOL_UNIT,
            "amount": rows["transAmount"] * AMOUNT_UNIT,
        }
    )
    df = df.drop_duplicates(subset="date").sort_values("date").reset_index(drop=True)
    for c in ["open", "high", "low", "close"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
    bad_open = df["open"] == 0
    if bad_open.any():
        df.loc[bad_open, "open"] = df["close"].shift(1)
    df["open"] = df["open"].fillna(df["close"])
    return df


def apply_price_limits(pred_df: pd.DataFrame, last_close: float, limit_rate: float) -> pd.DataFrame:
    """对单条预测轨迹逐日应用 ±limit_rate 涨跌停约束（防止模型产生极端价格）。"""
    pred_df = pred_df.reset_index(drop=True)
    cols = ["open", "high", "low", "close"]
    pred_df[cols] = pred_df[cols].astype("float64")
    for i in range(len(pred_df)):
        up, down = last_close * (1 + limit_rate), last_close * (1 - limit_rate)
        for col in cols:
            v = pred_df.at[i, col]
            if pd.notna(v):
                pred_df.at[i, col] = float(min(max(v, down), up))
        last_close = float(pred_df.at[i, "close"])
    return pred_df


class KronosEngine:
    """Kronos 预测引擎。模型懒加载并在进程内复用（多次调用只下载/加载一次）。"""

    _instance: "KronosEngine | None" = None

    def __init__(self, model_name: str | None = None, tokenizer_name: str | None = None,
                 device: str = "cuda:0", max_context: int = 512):
        self.model_name = model_name or KRONOS_MODEL
        self.tokenizer_name = tokenizer_name or KRONOS_TOKENIZER
        self.device = device
        self.max_context = max_context
        self._predictor = None

    @classmethod
    def get(cls, **kw) -> "KronosEngine":
        """获取进程级单例（参数仅在首次调用生效）。"""
        if cls._instance is None:
            cls._instance = cls(**kw)
        return cls._instance

    def _load(self):
        """懒加载模型与 tokenizer（vendor 路径引用 Kronos 仓库）。"""
        if self._predictor is None:
            kronos_home = str(KRONOS_HOME)
            if not os.path.isdir(kronos_home):
                raise FileNotFoundError(
                    f"Kronos 仓库不存在: {kronos_home}（可通过环境变量 KRONOS_HOME 指定）"
                )
            if kronos_home not in sys.path:
                sys.path.insert(0, kronos_home)
            # hikyuu 约定：本模块不使用 hikyuu；Kronos 为纯 Python/torch，无 TLS 冲突
            from model import Kronos, KronosPredictor, KronosTokenizer  # type: ignore

            tokenizer = KronosTokenizer.from_pretrained(self.tokenizer_name)
            model = Kronos.from_pretrained(self.model_name)
            self._predictor = KronosPredictor(
                model, tokenizer, device=self.device, max_context=self.max_context
            )
        return self._predictor

    def predict(self, market_code: str, pred_len: int = 120, lookback: int = 400,
                n_samples: int = 20, T: float = 1.0, top_p: float = 0.9):
        """对单只股票做未来 pred_len 个交易日的概率预测。

        返回:
            summary: DataFrame（date + open/high/low/close/volume/amount 的 P10/25/50/75/90）
            preds:   每条采样轨迹的 DataFrame 列表（含涨跌停约束）
            last_close: 最新收盘价
            hist_df:  历史全量日线（供绘图/回测使用）
        """
        predictor = self._load()
        hist = read_h5(market_code)
        if len(hist) < lookback:
            lookback = len(hist)
        last_close = float(hist["close"].iloc[-1])
        limit_rate = limit_rate_for(market_code)

        x_df = hist.iloc[-lookback:][["open", "high", "low", "close", "volume", "amount"]]
        x_ts = hist.iloc[-lookback:]["date"].reset_index(drop=True)
        y_ts = pd.Series(pd.bdate_range(
            start=hist["date"].iloc[-1] + pd.Timedelta(days=1), periods=pred_len
        ))

        preds: list[pd.DataFrame] = []
        for _ in range(n_samples):
            p = predictor.predict(
                df=x_df, x_timestamp=x_ts, y_timestamp=y_ts,
                pred_len=pred_len, T=T, top_p=top_p, sample_count=1, verbose=False,
            )
            p = apply_price_limits(p, last_close, limit_rate)
            p["date"] = y_ts.values
            preds.append(p)

        # 概率区间聚合
        summary = pd.DataFrame({"date": y_ts})
        for col in ["open", "high", "low", "close", "volume", "amount"]:
            mat = np.stack([p[col].values for p in preds])
            for q in (10, 25, 50, 75, 90):
                summary[f"{col}_p{q}"] = np.percentile(mat, q, axis=0)
        summary["close_mean"] = np.mean(
            np.stack([p["close"].values for p in preds]), axis=0
        )
        return summary, preds, last_close, hist
