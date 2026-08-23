"""组件化交易系统

借鉴 Hikyuu 的 SYS_Simple 架构：将交易策略拆分为 8 个独立组件，
每个组件可独立替换、跨股票共享。
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
import pandas as pd
import numpy as np


# ============================================================
# 交易指令
# ============================================================

@dataclass
class TradeRequest:
    """统一交易指令"""
    code: str
    action: str          # "buy" | "sell" | "hold"
    shares: int = 0
    price: float = 0.0   # 目标价格（可不同于实际成交价）
    urgency: float = 1.0  # 0~1，越高越优先执行
    reason: str = ""
    timestamp: str = ""

    def __bool__(self):
        return self.action != "hold" and self.shares > 0


@dataclass
class TradeRecord:
    """成交记录"""
    code: str
    action: str
    shares: int
    price: float          # 实际成交价
    cost: float = 0.0     # 交易成本
    signal_price: float = 0.0  # 信号价格
    reason: str = ""
    date: str = ""


# ============================================================
# 组件基类
# ============================================================

class SystemComponent(ABC):
    """系统组件基类

    所有组件可被多个 System 共享（shared reference pattern from Hikyuu）。
    """

    def __init__(self, name=""):
        self.name = name or self.__class__.__name__

    @abstractmethod
    def reset(self):
        """重置组件状态"""
        pass

    def __repr__(self):
        return f"{self.__class__.__name__}({self.name})"


# ============================================================
# 市场环境 (Environment)
# ============================================================

class Environment(SystemComponent):
    """市场环境判断：当前是否适合交易？

    EV_TwoLine: 快线在慢线上方 → 牛市环境
    """

    def __init__(self, name="env"):
        super().__init__(name)
        self._valid = True

    def is_valid(self, market_data: dict) -> bool:
        """判断当前市场环境是否可交易"""
        return self._valid and self._calculate(market_data)

    def _calculate(self, market_data: dict) -> bool:
        return True

    def reset(self):
        self._valid = True


class EV_TwoLine(Environment):
    """双均线环境：快线 > 慢线 → 可交易"""

    def __init__(self, fast=20, slow=60, name="EV_TwoLine"):
        super().__init__(name)
        self.fast = fast
        self.slow = slow

    def _calculate(self, market_data):
        benchmark = market_data.get("benchmark_close", [])
        if len(benchmark) < self.slow:
            return True  # 数据不够，默认允许
        fast_ma = np.mean(benchmark[-self.fast:])
        slow_ma = np.mean(benchmark[-self.slow:])
        return fast_ma > slow_ma


# ============================================================
# 系统条件 (Condition)
# ============================================================

class Condition(SystemComponent):
    """系统有效条件：当前是否应该持有/交易此股票"""

    def is_valid(self, stock_data: pd.Series) -> bool:
        return self._calculate(stock_data)

    def _calculate(self, stock_data) -> bool:
        return True

    def reset(self):
        pass


# ============================================================
# 信号 (Signal)
# ============================================================

class SignalBase(SystemComponent):
    """信号生成器基类

    返回 (action, urgency)，action: 'buy'/'sell'/'hold'
    """

    def get_signal(self, stock_data: pd.Series, holdings: dict) -> tuple:
        """生成交易信号

        Returns
        -------
        (action: str, urgency: float)
        """
        return self._calculate(stock_data, holdings)

    def _calculate(self, stock_data, holdings) -> tuple:
        return ("hold", 0)

    def reset(self):
        pass


class SG_Cross(SignalBase):
    """金叉死叉信号"""

    def __init__(self, fast=5, slow=20, name="SG_Cross"):
        super().__init__(name)
        self.fast = fast
        self.slow = slow

    def _calculate(self, stock_data, holdings):
        close = stock_data.get("close", 0)
        # 需要调用方在 stock_data 中提供 precomputed 均线
        fast_ma = stock_data.get(f"ma_{self.fast}", close)
        slow_ma = stock_data.get(f"ma_{self.slow}", close)
        if fast_ma > slow_ma:
            return ("buy", 0.8)
        return ("sell", 0.6)


class SG_Bool(SignalBase):
    """布尔信号：基于任意布尔指标"""

    def __init__(self, buy_expr=None, sell_expr=None, name="SG_Bool"):
        super().__init__(name)
        self.buy_expr = buy_expr or (lambda d: False)
        self.sell_expr = sell_expr or (lambda d: False)

    def _calculate(self, stock_data, holdings):
        if self.buy_expr(stock_data):
            return ("buy", 0.7)
        if self.sell_expr(stock_data):
            return ("sell", 0.7)
        return ("hold", 0)


# ============================================================
# 资金管理 (MoneyManager)
# ============================================================

class MoneyManager(SystemComponent):
    """资金管理器：决定买卖多少"""

    def get_buy_shares(self, cash: float, price: float, risk_per_trade: float = 0.01) -> int:
        return self._calculate_buy(cash, price, risk_per_trade)

    def get_sell_shares(self, holdings: int, price: float) -> int:
        return self._calculate_sell(holdings, price)

    def _calculate_buy(self, cash, price, risk_per_trade) -> int:
        return 0

    def _calculate_sell(self, holdings, price) -> int:
        return holdings

    def reset(self):
        pass


class MM_FixedCount(MoneyManager):
    """固定股数"""

    def __init__(self, shares=100, name="MM_FixedCount"):
        super().__init__(name)
        self.target_shares = shares

    def _calculate_buy(self, cash, price, risk_per_trade):
        return min(self.target_shares, int(cash / price))


class MM_FixedPercent(MoneyManager):
    """固定资金比例"""

    def __init__(self, percent=0.1, name="MM_FixedPercent"):
        super().__init__(name)
        self.percent = percent

    def _calculate_buy(self, cash, price, risk_per_trade):
        return int(cash * self.percent / price)


class MM_FixedRisk(MoneyManager):
    """固定风险金额（基于止损距离）"""

    def __init__(self, risk_amount=10000, name="MM_FixedRisk"):
        super().__init__(name)
        self.risk_amount = risk_amount
        self._stop_loss_pct = 0.05  # 默认5%止损

    def set_stop_loss_pct(self, pct):
        self._stop_loss_pct = pct

    def _calculate_buy(self, cash, price, risk_per_trade):
        risk_per_share = price * self._stop_loss_pct
        if risk_per_share <= 0:
            return 0
        return min(int(self.risk_amount / risk_per_share), int(cash / price))


class MM_Kelly(MoneyManager):
    """凯利公式仓位"""

    def __init__(self, win_rate=0.5, avg_win=0.05, avg_loss=0.03, fraction=0.5,
                 name="MM_Kelly"):
        super().__init__(name)
        self.win_rate = win_rate
        self.avg_win = avg_win
        self.avg_loss = avg_loss
        self.fraction = fraction

    def _calculate_buy(self, cash, price, risk_per_trade):
        b = self.avg_win / self.avg_loss
        q = 1 - self.win_rate
        kelly = max(0, (self.win_rate * b - q) / b) if b > 0 else 0
        kelly = kelly * self.fraction
        return min(int(cash * kelly / price), int(cash / price))


# ============================================================
# 止损/止盈 (StopLoss)
# ============================================================

class StopLoss(SystemComponent):
    """止损/止盈"""

    def check(self, entry_price: float, current_price: float,
              holding_days: int = 0) -> tuple:
        """返回 (should_exit: bool, reason: str)"""
        return self._calculate(entry_price, current_price, holding_days)

    def _calculate(self, entry_price, current_price, holding_days) -> tuple:
        return (False, "")

    def reset(self):
        pass


class ST_FixedPercent(StopLoss):
    """固定比例止损"""

    def __init__(self, loss_pct=0.05, profit_pct=None, name="ST_FixedPercent"):
        super().__init__(name)
        self.loss_pct = loss_pct
        self.profit_pct = profit_pct

    def _calculate(self, entry_price, current_price, holding_days):
        change = current_price / entry_price - 1
        if change <= -self.loss_pct:
            return (True, f"stop_loss {-change:.1%}")
        if self.profit_pct and change >= self.profit_pct:
            return (True, f"take_profit {change:.1%}")
        return (False, "")


class ST_Saftyloss(StopLoss):
    """安全止损：从最高点回落一定比例"""

    def __init__(self, trail_pct=0.05, name="ST_Saftyloss"):
        super().__init__(name)
        self.trail_pct = trail_pct
        self._high_watermark = 0

    def _calculate(self, entry_price, current_price, holding_days):
        self._high_watermark = max(self._high_watermark, current_price)
        if current_price < self._high_watermark * (1 - self.trail_pct):
            return (True, f"trailing_stop from {self._high_watermark:.2f}")
        return (False, "")

    def reset(self):
        self._high_watermark = 0


# ============================================================
# 盈利目标 (ProfitGoal)
# ============================================================

class ProfitGoal(SystemComponent):
    """盈利目标"""

    def check(self, entry_price, current_price, holding_days=0) -> tuple:
        return self._calculate(entry_price, current_price, holding_days)

    def _calculate(self, entry_price, current_price, holding_days) -> tuple:
        return (False, "")

    def reset(self):
        pass


class PG_FixedPercent(ProfitGoal):
    """固定百分比止盈"""

    def __init__(self, target_pct=0.20, name="PG_FixedPercent"):
        super().__init__(name)
        self.target_pct = target_pct

    def _calculate(self, entry_price, current_price, holding_days):
        if current_price / entry_price - 1 >= self.target_pct:
            return (True, f"profit_goal {self.target_pct:.0%}")
        return (False, "")


class PG_FixedHoldDays(ProfitGoal):
    """固定持仓天数"""

    def __init__(self, max_days=20, name="PG_FixedHoldDays"):
        super().__init__(name)
        self.max_days = max_days

    def _calculate(self, entry_price, current_price, holding_days):
        if holding_days >= self.max_days:
            return (True, f"hold_days_exceeded {holding_days}d")
        return (False, "")


# ============================================================
# 滑点 (Slippage)
# ============================================================

class Slippage(SystemComponent):
    """滑点模型"""

    def get_real_price(self, target_price: float, action: str) -> float:
        """返回考虑滑点后的实际成交价"""
        return self._calculate(target_price, action)

    def _calculate(self, target_price, action) -> float:
        return target_price

    def reset(self):
        pass


class SP_FixedPercent(Slippage):
    """固定比例滑点"""

    def __init__(self, pct=0.001, name="SP_FixedPercent"):
        super().__init__(name)
        self.pct = pct

    def _calculate(self, target_price, action):
        if action == "buy":
            return target_price * (1 + self.pct)
        return target_price * (1 - self.pct)


class SP_FixedValue(Slippage):
    """固定金额滑点"""

    def __init__(self, value=0.01, name="SP_FixedValue"):
        super().__init__(name)
        self.value = value

    def _calculate(self, target_price, action):
        if action == "buy":
            return target_price + self.value
        return target_price - self.value


# ============================================================
# 交易系统 (System)
# ============================================================

class TradingSystem:
    """组件化交易系统（借鉴 Hikyuu SYS_Simple）

    System = { Environment + Condition + Signal +
               MoneyManager + StopLoss + ProfitGoal + Slippage }
    """

    def __init__(self, name="System", config=None):
        self.name = name
        self.config = config or {}
        self.env: Optional[Environment] = None
        self.cond: Optional[Condition] = None
        self.sg: Optional[SignalBase] = None
        self.mm: Optional[MoneyManager] = None
        self.st: Optional[StopLoss] = None
        self.pg: Optional[ProfitGoal] = None
        self.sp: Optional[Slippage] = None

        # 运行时状态
        self._positions: dict[str, dict] = {}
        self._trade_requests: list[TradeRequest] = []
        self._trade_records: list[TradeRecord] = []

    def set_env(self, env: Environment):
        self.env = env; return self

    def set_cond(self, cond: Condition):
        self.cond = cond; return self

    def set_sg(self, sg: SignalBase):
        self.sg = sg; return self

    def set_mm(self, mm: MoneyManager):
        self.mm = mm; return self

    def set_st(self, st: StopLoss):
        self.st = st; return self

    def set_pg(self, pg: ProfitGoal):
        self.pg = pg; return self

    def set_sp(self, sp: Slippage):
        self.sp = sp; return self

    def run_one(self, code: str, stock_data: pd.Series,
                market_data: dict, cash: float) -> TradeRequest:
        """对一只股票运行一次交易决策

        Returns
        -------
        TradeRequest
        """
        # 1. 环境检查
        if self.env and not self.env.is_valid(market_data):
            return TradeRequest(code=code, action="hold")

        # 2. 条件检查
        if self.cond and not self.cond.is_valid(stock_data):
            return TradeRequest(code=code, action="hold")

        # 3. 当前持仓信息
        pos = self._positions.get(code, {})
        entry_price = pos.get("entry_price", 0)
        buy_date = pos.get("buy_date", "")

        # 4. 止损/止盈检查（已有持仓时）
        if code in self._positions:
            current_price = stock_data.get("close", 0)
            if self.st:
                exit_signal, reason = self.st.check(entry_price, current_price)
                if exit_signal:
                    shares = pos.get("shares", 0)
                    return TradeRequest(
                        code=code, action="sell", shares=shares,
                        price=current_price, urgency=0.9, reason=reason,
                    )
            if self.pg:
                exit_signal, reason = self.pg.check(entry_price, current_price)
                if exit_signal:
                    shares = pos.get("shares", 0)
                    return TradeRequest(
                        code=code, action="sell", shares=shares,
                        price=current_price, urgency=0.8, reason=reason,
                    )

        # 5. 信号生成
        if self.sg:
            action, urgency = self.sg.get_signal(stock_data, self._positions)
        else:
            action, urgency = "hold", 0

        if action == "hold":
            return TradeRequest(code=code, action="hold")

        # 6. 资金管理
        current_price = stock_data.get("close", 0)
        if self.mm:
            if action == "buy":
                shares = self.mm.get_buy_shares(cash, current_price)
            else:
                shares = self.mm.get_sell_shares(
                    pos.get("shares", 0), current_price
                )
        else:
            shares = int(cash / current_price / 10)

        return TradeRequest(
            code=code, action=action, shares=shares,
            price=current_price, urgency=urgency,
            reason=f"signal:{action}",
        )

    def execute(self, request: TradeRequest, cash: float) -> tuple:
        """执行交易指令（含滑点）

        Returns
        -------
        (updated_cash: float, TradeRecord or None)
        """
        if not request:
            return cash, None

        price = request.price
        if self.sp:
            price = self.sp.get_real_price(request.price, request.action)

        cost = 0
        if request.action == "buy":
            cost = price * request.shares
            if cost > cash:
                request.shares = int(cash / price)
                cost = price * request.shares
            cash -= cost
            self._positions[request.code] = {
                "shares": request.shares,
                "entry_price": price,
                "buy_date": request.timestamp,
            }
        elif request.action == "sell":
            proceeds = price * request.shares
            cash += proceeds
            if request.code in self._positions:
                del self._positions[request.code]

        record = TradeRecord(
            code=request.code, action=request.action,
            shares=request.shares, price=price,
            cost=cost, signal_price=request.price,
            reason=request.reason,
        )
        self._trade_records.append(record)
        return cash, record

    def get_positions(self) -> dict:
        return self._positions

    def get_position_value(self, current_prices: dict) -> float:
        value = 0
        for code, pos in self._positions.items():
            price = current_prices.get(code, pos.get("entry_price", 0))
            value += pos["shares"] * price
        return value

    def reset(self):
        self._positions.clear()
        self._trade_requests.clear()
        self._trade_records.clear()
        for comp in [self.env, self.cond, self.sg, self.mm, self.st, self.pg, self.sp]:
            if comp:
                comp.reset()
