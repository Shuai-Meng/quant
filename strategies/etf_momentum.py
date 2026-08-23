"""ETF波动交易策略

每周五收盘后评估各ETF过去20日涨幅，选取最强的持有。
周一开盘执行轮动，结合8%回撤风控。

用法:
    from strategies.etf_momentum import ETFMomentumStrategy
    strategy = ETFMomentumStrategy()
    target = strategy.generate_signals(data, date)
"""
import pandas as pd
import numpy as np
from .multi_factor import Strategy


class ETFMomentumStrategy(Strategy):
    """ETF波动交易策略

    在ETF池中轮动持有20日涨幅最强的品种。
    每周五收盘前（weekday==4）用当日 close 评估排名，
    周一开盘（weekday==0）用当日 close 模拟执行。

    State machine (self._pending):
      "hold"  = 保持当前持仓不动
      "enter" = 空仓时准备买入 self._pending_target
      "exit"  = 有持仓但需卖出，不再买入
      "rotate" = 卖出旧持仓，买入 self._pending_target
    """

    _DEFAULTS = {
        "etf_pool": ["510300.SH", "159915.SZ", "588000.SH", "510050.SH"],
        "lookback": 20,
        "money_market_annual": 0.10,
        "max_drawdown": 0.08,
    }

    def __init__(self, name="etf_momentum", config=None):
        super().__init__(name)
        self.config = {**self._DEFAULTS, **(config or {})}
        self._entry_prices = {}
        self._pending = "hold"
        self._pending_target = None

    # ── helpers ────────────────────────────────────────────────

    def _calc_20d_returns(self, data, date, codes):
        """计算各 code 截至 date 的过去 lookback 日涨幅"""
        date = pd.Timestamp(date)
        lookback = self.config["lookback"]
        result = {}
        for code in codes:
            sub = data[data["code"] == code].sort_values("date")
            dates = sub["date"].values
            if date not in dates:
                continue
            pos = list(dates).index(date)
            if pos < lookback:
                continue
            result[code] = (
                sub.iloc[pos]["close"] / sub.iloc[pos - lookback]["close"] - 1
            )
        return result

    def _get_close(self, data, code, date):
        sub = data[(data["code"] == code) & (data["date"] == pd.Timestamp(date))]
        return float(sub["close"].iloc[0]) if not sub.empty else None

    # ── signal generation ──────────────────────────────────────

    def generate_signals(self, data, date):
        date = pd.Timestamp(date)
        cfg = self.config
        codes = cfg["etf_pool"]
        mm_20d = (1 + cfg["money_market_annual"]) ** (cfg["lookback"] / 252) - 1

        held = list(self.positions.keys())[0] if self.positions else None

        # ── daily: drawdown check ──
        if held and held in self._entry_prices:
            price = self._get_close(data, held, date)
            if price and price / self._entry_prices[held] - 1 < -cfg["max_drawdown"]:
                self.positions = {}
                self._entry_prices = {}
                self._pending = "wait"
                self._pending_target = None
                return {}

        # ── daily: compute 20d returns ──
        ret_20d = self._calc_20d_returns(data, date, codes)
        if not ret_20d:
            return self.positions

        ranked = sorted(ret_20d.items(), key=lambda x: -x[1])
        best_code, best_ret = ranked[0]

        # ── Friday: evaluation ──
        if date.weekday() == 4:
            if not held:
                self._pending = "enter" if best_ret > mm_20d else "wait"
                self._pending_target = best_code if best_ret > mm_20d else None
            elif held == best_code:
                self._pending = "hold"
                self._pending_target = None
            else:
                held_ret = ret_20d.get(held, -999)
                held_rank = next(i for i, (c, _) in enumerate(ranked) if c == held)
                should_exit = held_rank == len(ranked) - 1 or held_ret < 0
                if should_exit:
                    self._pending = "rotate" if best_ret > mm_20d else "exit"
                    self._pending_target = best_code if best_ret > mm_20d else None
                else:
                    self._pending = "hold"
                    self._pending_target = None
            return self.positions

        # ── Monday: execution ──
        if date.weekday() == 0:
            if self._pending in ("enter", "rotate") and self._pending_target:
                price = self._get_close(data, self._pending_target, date)
                if price:
                    self.positions = {self._pending_target: 1.0}
                    self._entry_prices = {self._pending_target: price}
            elif self._pending == "exit":
                self.positions = {}
                self._entry_prices = {}
            self._pending = "hold"
            self._pending_target = None
            return self.positions

        return self.positions
