"""实时持仓跟踪器"""
import os
import json
from datetime import datetime
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

STATE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state", "portfolio.json")


@dataclass
class Position:
    code: str
    name: str = ""
    shares: int = 0
    avg_cost: float = 0.0
    current_price: float = 0.0
    market_value: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    weight: float = 0.0


class PortfolioTracker:
    """实时持仓跟踪器

    管理当前持仓、现金、交易记录，支持JSON持久化。
    """

    def __init__(self, initial_capital: float = 1_000_000):
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.positions: Dict[str, Position] = {}
        self.trade_history: List[dict] = []
        self.daily_snapshots: List[dict] = []
        self._load_state()

    def _load_state(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as f:
                    data = json.load(f)
                self.cash = data.get("cash", self.initial_capital)
                self.trade_history = data.get("trade_history", [])
                self.daily_snapshots = data.get("daily_snapshots", [])
                for d in data.get("positions", []):
                    pos = Position(**d)
                    self.positions[pos.code] = pos
            except Exception:
                pass

    def _save_state(self):
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        data = {
            "cash": self.cash,
            "positions": [asdict(p) for p in self.positions.values()],
            "trade_history": self.trade_history[-1000:],
            "daily_snapshots": self.daily_snapshots[-500:],
        }
        with open(STATE_FILE, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)

    def update_prices(self, quotes: dict):
        """用实时行情更新持仓市值

        Args:
            quotes: {code: {"name": str, "close": float, "change_pct": float}}
        """
        total_value = self.cash
        for code, pos in self.positions.items():
            if code in quotes:
                q = quotes[code]
                pos.name = q.get("name", pos.name)
                pos.current_price = q.get("close", pos.current_price)
                pos.market_value = pos.shares * pos.current_price
                pos.pnl = pos.market_value - (pos.shares * pos.avg_cost)
                pos.pnl_pct = (pos.current_price / pos.avg_cost - 1) if pos.avg_cost > 0 else 0
                total_value += pos.market_value

        for code, pos in self.positions.items():
            pos.weight = pos.market_value / total_value if total_value > 0 else 0

    def buy(self, code: str, shares: int, price: float, name: str = ""):
        cost = shares * price
        if cost > self.cash:
            return False
        self.cash -= cost
        if code in self.positions:
            pos = self.positions[code]
            total_shares = pos.shares + shares
            pos.avg_cost = ((pos.avg_cost * pos.shares) + cost) / total_shares
            pos.shares = total_shares
        else:
            self.positions[code] = Position(
                code=code, name=name, shares=shares,
                avg_cost=price, current_price=price,
                market_value=cost,
            )
        self.trade_history.append({
            "time": datetime.now().isoformat(),
            "action": "BUY", "code": code, "shares": shares, "price": price,
        })
        self._save_state()
        return True

    def sell(self, code: str, shares: Optional[int] = None, price: float = 0):
        if code not in self.positions:
            return 0
        pos = self.positions[code]
        shares = shares or pos.shares
        shares = min(shares, pos.shares)
        proceeds = shares * price
        self.cash += proceeds
        pos.shares -= shares
        if pos.shares <= 0:
            del self.positions[code]
        else:
            pos.market_value = pos.shares * pos.current_price
        self.trade_history.append({
            "time": datetime.now().isoformat(),
            "action": "SELL", "code": code, "shares": shares, "price": price,
        })
        self._save_state()
        return proceeds

    @property
    def total_value(self):
        return self.cash + sum(p.market_value for p in self.positions.values())

    @property
    def total_pnl(self):
        return self.total_value - self.initial_capital

    @property
    def total_pnl_pct(self):
        return self.total_pnl / self.initial_capital if self.initial_capital > 0 else 0

    def snapshot(self):
        return {
            "time": datetime.now().isoformat(),
            "total_value": self.total_value,
            "cash": self.cash,
            "stock_value": sum(p.market_value for p in self.positions.values()),
            "pnl": self.total_pnl,
            "pnl_pct": self.total_pnl_pct,
            "n_positions": len(self.positions),
            "positions": {c: asdict(p) for c, p in self.positions.items()},
        }

    def get_summary(self):
        return {
            "total_value": round(self.total_value, 2),
            "cash": round(self.cash, 2),
            "stock_value": round(sum(p.market_value for p in self.positions.values()), 2),
            "pnl": round(self.total_pnl, 2),
            "pnl_pct": f"{self.total_pnl_pct:.2%}",
            "n_positions": len(self.positions),
            "positions": [
                {
                    "code": p.code, "name": p.name, "shares": p.shares,
                    "price": round(p.current_price, 2),
                    "cost": round(p.avg_cost, 2),
                    "pnl_pct": f"{p.pnl_pct:.2%}",
                    "weight": f"{p.weight:.1%}",
                }
                for p in sorted(self.positions.values(), key=lambda x: -x.market_value)
            ],
        }
