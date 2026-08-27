"""日常信号生成

在数据层和因子层之上，生成每日可交易的股票信号。

信号来源（按优先级）：
1. 多因子 Alpha 选股（默认）：全市场抽样 → 批量日K → 技术/行为因子
   → MAD去极值 + 截面标准化 + 方向修正 → 加权合成 Alpha → Top N 选股
2. 热点题材选股：同花顺强势股（service.py 在多因子数据不可用时回退）

CLI 用法:
    python -m signals.generate --top 20 --stocks 300 --refresh
"""
import argparse
import json
import logging
import os
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from config import settings
from data.storage.cache import cache_get, cache_set
from factors.behavioral.amplitude import AmplitudeFactor
from factors.synthesis.combiner import FactorCombiner
from factors.technical.ma_trend import MATrendFactor
from factors.technical.momentum import MomentumFactor
from factors.technical.reversal import ReversalFactor
from factors.technical.rsi import RSIFactor
from factors.technical.volume_ratio import VolumeRatioFactor
from utils.standardize import standardize_by_group
from utils.winsorize import winsorize_by_group

log = logging.getLogger("signals.generate")

SIGNALS_FILE = os.path.join(BASE_DIR, "state", "signals", "latest.json")

# 因子方向：1 = 值越大越看多；-1 = 值越大越看空
FACTOR_DIRECTIONS = {
    "momentum": 1,         # 20日动量：趋势延续
    "reversal": 1,         # 5日超跌反转（因子值 = -5日收益）
    "volume_ratio": 1,     # 量比突增：资金关注
    "rsi": -1,             # RSI 高 = 超买回调风险
    "ma_trend": 1,         # 均线多头排列
    "amplitude": -1,       # 高振幅 = 波动风险高，期望收益低
}


class SignalGenerator:
    """信号生成器

    结合多因子评分和筛选条件，输出每日买卖信号。
    """

    def __init__(self, factor_combiner, config=None):
        """
        Parameters
        ----------
        factor_combiner : FactorCombiner
            已配置好的因子合成器
        config : dict
        """
        self.combiner = factor_combiner
        self.config = config or {}
        self._latest_signal = None

    def generate(self, date_str=None, top_n=20, universe_filter=None):
        """生成信号

        Parameters
        ----------
        date_str : str
            日期 YYYY-MM-DD，默认今天
        top_n : int
            选股数量
        universe_filter : callable, optional
            额外的股票筛选函数

        Returns
        -------
        DataFrame with code, alpha, rank
        """
        if date_str is None:
            date_str = date.today().strftime("%Y-%m-%d")

        alpha = self.combiner.combine()
        if alpha is None or alpha.empty:
            return pd.DataFrame(columns=["code", "alpha", "rank"])

        # 筛选当日
        today_alpha = alpha[
            pd.to_datetime(alpha["date"]) == pd.Timestamp(date_str)
        ].copy()

        if today_alpha.empty:
            return pd.DataFrame(columns=["code", "alpha", "rank"])

        # 应用额外筛选
        if universe_filter:
            today_alpha = today_alpha[universe_filter(today_alpha)]

        # 排序选Top N
        today_alpha = today_alpha.sort_values("alpha", ascending=False)
        today_alpha["rank"] = range(1, len(today_alpha) + 1)
        self._latest_signal = today_alpha.head(top_n)

        return self._latest_signal[["code", "alpha", "rank"]]

    def get_top_stocks(self, n=10):
        """获取排名前N的股票"""
        if self._latest_signal is None:
            return []
        return self._latest_signal.head(n)["code"].tolist()

    def get_buy_list(self, current_holdings=None):
        """获取买入清单（去重已有持仓）"""
        if self._latest_signal is None:
            return []
        signals = self._latest_signal.copy()
        if current_holdings:
            signals = signals[~signals["code"].isin(current_holdings)]
        return signals["code"].tolist()


def _factor_calculators(data):
    """构建可计算的因子（依据数据中实际存在的列）"""
    if "close" not in data.columns:
        return {}
    calcs = {
        "momentum": MomentumFactor(name="momentum", config=settings.FACTORS.get("momentum", {})),
        "reversal": ReversalFactor(name="reversal", config=settings.FACTORS.get("reversal", {})),
        "volume_ratio": VolumeRatioFactor(name="volume_ratio", config=settings.FACTORS.get("volume_ratio", {})),
        "rsi": RSIFactor(name="rsi", config=settings.FACTORS.get("rsi", {})),
        "ma_trend": MATrendFactor(name="ma_trend", config=settings.FACTORS.get("ma_trend", {})),
        "amplitude": AmplitudeFactor(name="amplitude", config=settings.FACTORS.get("amplitude", {})),
    }
    return calcs


def _calc_factors(daily, calculators):
    """按股票逐只计算因子，避免拼接数据跨股票串线"""
    for name, calc in calculators.items():
        series = []
        for code, g in daily.groupby("code"):
            g = g.sort_values("date")
            try:
                s = calc.calculate(g)
                series.append(pd.Series(np.asarray(s), index=g.index))
            except Exception:
                continue
        if series:
            daily[name] = pd.concat(series).reindex(daily.index)
    return daily


def _factor_reason(name, raw_value):
    """根据因子原始值生成中文理由"""
    try:
        if name == "momentum":
            return f"20日动量 {raw_value * 100:+.1f}%"
        if name == "reversal":
            return f"5日超跌 {abs(raw_value) * 100:.1f}%"
        if name == "volume_ratio":
            return f"量比 {raw_value:.2f}"
        if name == "rsi":
            return f"RSI超卖({raw_value:.0f})"
        if name == "ma_trend":
            return "均线多头" if raw_value > 0 else "均线转强"
        if name == "amplitude":
            return f"低振幅({raw_value * 100:.1f}%)"
    except (TypeError, ValueError):
        pass
    return name


def _full_code(c):
    """归一化股票代码为 '000001.SZ' 格式"""
    c = str(c).strip().lower()
    if "." in c:
        return c.upper()
    if c.startswith("sz"):
        return c[2:] + ".SZ"
    if c.startswith("sh"):
        return c[2:] + ".SH"
    return c


def _pick_universe(stock_df, n_stocks, random_seed=42):
    """构建股票池：过滤北交所/ST，随机抽样

    Returns
    -------
    (codes, name_map) : 股票代码列表 + {code: name} 映射
    """
    stock_df = stock_df.rename(columns=str)
    if "stock_code" not in stock_df.columns and "code" in stock_df.columns:
        stock_df = stock_df.rename(columns={"code": "stock_code"})
    if "stock_name" not in stock_df.columns:
        stock_df["stock_name"] = ""

    name_map = {}
    pool = []
    for _, row in stock_df.iterrows():
        c = str(row["stock_code"]).strip().zfill(6)
        if c[:1] == "6":
            full = f"{c}.SH"
        elif c[:1] in ("0", "3"):
            full = f"{c}.SZ"
        else:  # 北交所/科创板前缀等
            continue
        nm = str(row.get("stock_name", "")).strip()
        if "ST" in nm.upper() or "退" in nm:
            continue
        pool.append(full)
        name_map[full] = nm

    if not pool:
        raise RuntimeError("股票池为空")

    if len(pool) > n_stocks:
        rng = np.random.default_rng(random_seed)
        pool = sorted(rng.choice(pool, n_stocks, replace=False).tolist())
    return pool, name_map


def generate_signals(date_str=None, top_n=20, n_stocks=300, lookback_days=80,
                     refresh=False, method="weighted", verbose=True, random_seed=42):
    """多因子 Alpha 选股主流程

    Parameters
    ----------
    date_str : str, optional
        目标日期 YYYY-MM-DD，默认今天
    top_n : int
        选股数量
    n_stocks : int
        抽样股票数（控制拉取耗时）
    lookback_days : int
        回看交易日数
    refresh : bool
        强制刷新数据缓存
    method : str
        因子合成方法（weighted/equal/rank/max_sharpe/risk_parity）
    verbose : bool
        是否打印进度

    Returns
    -------
    DataFrame: code, name, score(alpha), change_pct, turnover, reason, source, date
    """
    from pipeline import QuantPipeline

    if date_str is None:
        date_str = date.today().strftime("%Y-%m-%d")

    pipe = QuantPipeline()

    # 1. 股票池
    stock_df = pipe.fetch_universe(refresh=refresh)
    if stock_df is None or stock_df.empty:
        raise RuntimeError("无法获取股票列表")
    codes, name_map = _pick_universe(stock_df, n_stocks, random_seed=random_seed)
    if verbose:
        log.info("股票池: %d 只（抽样 %d）", len(name_map), len(codes))

    # 2. 日K线（带本地缓存，key 含股票集合指纹，避免不同抽样互相误用）
    start = (pd.Timestamp(date_str) - timedelta(days=lookback_days * 2)).strftime("%Y%m%d")
    end = date_str.replace("-", "")
    import hashlib
    codes_fp = hashlib.md5("|".join(sorted(codes)).encode()).hexdigest()[:8]
    cache_key = f"daily_{start}_{end}_{codes_fp}"
    daily = None if refresh else cache_get(cache_key)
    if daily is None or daily.empty:
        if verbose:
            log.info("拉取日K线 %d 只 %s ~ %s ...", len(codes), start, end)
        daily = pipe.tencent.get_batch_kline(codes, start, end)
        if daily is None or daily.empty:
            raise RuntimeError("无法获取日K线数据")
        cache_set(cache_key, daily)
    elif verbose:
        log.info("日K线从缓存加载: %d 行", len(daily))

    daily = daily.sort_values(["code", "date"]).reset_index(drop=True)
    daily["date"] = pd.to_datetime(daily["date"])
    daily["pct"] = daily.groupby("code")["close"].pct_change()

    # 3. 因子计算（逐只股票，避免串线）
    calculators = _factor_calculators(daily)
    daily = _calc_factors(daily, calculators)
    factor_names = [n for n in FACTOR_DIRECTIONS if n in daily.columns]
    if not factor_names:
        raise RuntimeError("无可用因子")

    # 4. 截面标准化 + 方向修正 + 加权合成
    combiner = FactorCombiner()
    for name in factor_names:
        w = winsorize_by_group(daily, name, "date", method="mad", n=4)
        z = standardize_by_group(
            daily.assign(**{f"{name}_w": w}), f"{name}_w", "date", method="zscore"
        )
        score = pd.Series(
            np.asarray(z) * FACTOR_DIRECTIONS[name], index=daily.index
        )
        score_df = daily[["date", "code"]].assign(**{f"{name}_score": score}).dropna()
        weight = float(settings.FACTORS.get(name, {}).get("weight", 1.0))
        combiner.add_factor(name, score_df, weight=weight)

    alpha = combiner.combine(method=method)
    if alpha is None or alpha.empty:
        raise RuntimeError("因子合成结果为空")

    # 5. 最新截面 Top N
    last_date = alpha["date"].max()
    today = alpha[alpha["date"] == last_date].sort_values("alpha", ascending=False)
    top = today.head(top_n).copy()
    if top.empty:
        raise RuntimeError(f"{last_date} 无有效选股结果")

    # 6. 理由生成：按因子贡献取 top2
    last_raw = daily[daily["date"] == last_date][["code"] + factor_names].copy()
    top = top.merge(last_raw, on="code", how="left", suffixes=("", "_raw"))

    def _mk_reason(row):
        contribs = []
        for f in factor_names:
            c = row.get(f)
            if pd.isna(c):
                continue
            contribs.append((float(c), f))
        contribs.sort(reverse=True)
        return "；".join(_factor_reason(f, row.get(f"{f}_raw")) for _, f in contribs[:2])

    top["reason"] = top.apply(_mk_reason, axis=1)
    top["name"] = top["code"].map(lambda c: name_map.get(c, ""))

    # 7. 行情信息：日K涨跌幅 + 实时行情补充（尽力而为）
    pct_map = daily[daily["date"] == last_date].set_index("code")["pct"]
    top["change_pct"] = top["code"].map(pct_map).fillna(0.0)
    top["turnover"] = np.nan
    try:
        rt = pipe.tencent.get_realtime_quote(top["code"].tolist())
        if rt is not None and not rt.empty:
            rt = rt.copy()
            if "code" in rt.columns:
                rt["code"] = rt["code"].map(_full_code)
            rt = rt.set_index("code")
            for col in ("change_pct", "turnover", "name"):
                if col in rt.columns:
                    top[col] = top["code"].map(rt[col]).fillna(top[col])
    except Exception as e:
        log.warning("实时行情补充失败: %s", e)

    # 8. 输出
    out = top[["code", "name", "alpha", "change_pct", "reason"]].copy()
    out = out.rename(columns={"alpha": "score"})
    out["turnover"] = top.get("turnover", np.nan)
    out["source"] = "multi_factor"
    out["date"] = pd.Timestamp(last_date).strftime("%Y-%m-%d")
    return out.reset_index(drop=True)


def save_signals(signals_df, out_path=None):
    """保存信号到 JSON（兼容 web 端交易信号格式）

    Returns
    -------
    list[dict] : 保存的记录
    """
    out_path = out_path or SIGNALS_FILE
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    records = []
    for _, row in signals_df.iterrows():
        records.append({
            "code": str(row.get("code", "")),
            "name": str(row.get("name", "")),
            "score": float(row.get("score", 0.0) or 0.0),
            "change_pct": float(row.get("change_pct", 0.0) or 0.0),
            "reason": str(row.get("reason", "")),
            "turnover": None if pd.isna(row.get("turnover")) else float(row.get("turnover")),
            "source": str(row.get("source", "multi_factor")),
        })
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    log.info("信号已保存: %s (%d 条)", out_path, len(records))
    return records


def main(argv=None):
    parser = argparse.ArgumentParser(description="多因子 Alpha 选股信号生成")
    parser.add_argument("--date", default=None, help="目标日期 YYYY-MM-DD，默认今天")
    parser.add_argument("--top", type=int, default=20, help="选股数量")
    parser.add_argument("--stocks", type=int, default=300, help="抽样股票数")
    parser.add_argument("--lookback", type=int, default=80, help="回看交易日数")
    parser.add_argument("--refresh", action="store_true", help="强制刷新数据缓存")
    parser.add_argument("--method", default="weighted",
                        choices=["weighted", "equal", "rank", "max_sharpe", "risk_parity"],
                        help="因子合成方法")
    parser.add_argument("--out", default=SIGNALS_FILE, help="输出 JSON 路径")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    df = generate_signals(date_str=args.date, top_n=args.top, n_stocks=args.stocks,
                          lookback_days=args.lookback, refresh=args.refresh,
                          method=args.method, verbose=True)
    records = save_signals(df, out_path=args.out)
    print(f"\n=== {df['date'].iloc[0]} 多因子选股 Top {len(records)} ===")
    print(df.to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
