"""实时行情监控 + 风控"""
import os
import sys
import logging
from datetime import datetime
from typing import Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from .portfolio import PortfolioTracker
from risk.risk_manager import DrawdownController, ExposureChecker
from data.fetchers.tencent import TencentFetcher

log = logging.getLogger("quant.service")


class LiveMonitor:
    """实时行情监控器

    负责：
    1. 轮询实时行情更新持仓市值
    2. 回撤熔断检测
    3. 行业暴露检测
    4. 热点异动捕捉
    """

    def __init__(self, portfolio: PortfolioTracker):
        self.portfolio = portfolio
        self.tencent = TencentFetcher()
        self.drawdown = DrawdownController(max_drawdown=0.15, half_drawdown=0.10)
        self.exposure = ExposureChecker(max_industry_pct=0.30)
        self._last_alert: Optional[str] = None
        self._alert_history: list[dict] = []
        self._latest_quotes: dict = {}
        self._watchlist: list[str] = []

    def set_watchlist(self, codes: list[str]):
        self._watchlist = codes[:]

    def fetch_quotes(self) -> dict:
        """获取实时行情"""
        all_codes = list(set(
            list(self.portfolio.positions.keys()) + self._watchlist
        ))
        if not all_codes:
            return {}
        try:
            df = self.tencent.get_realtime_quote(all_codes[:60])
            if df.empty:
                return {}
            quotes = {}
            for _, row in df.iterrows():
                quotes[row["code"]] = {
                    "name": row.get("name", ""),
                    "close": row.get("close", 0),
                    "open": row.get("open", 0),
                    "high": row.get("high", 0),
                    "low": row.get("low", 0),
                    "volume": row.get("volume", 0),
                    "change_pct": row.get("change_pct", 0),
                    "turnover": row.get("turnover", 0),
                    "pe": row.get("pe", 0),
                    "pb": row.get("pb", 0),
                    "market_cap": row.get("market_cap", 0),
                }
            return quotes
        except Exception as e:
            log.warning(f"获取行情失败: {e}")
            return {}

    def check_risk(self) -> dict:
        """执行风控检查

        Returns:
            dict with:
                alerts: list of alert strings
                triggered: bool (是否需要强制处理)
        """
        alerts = []
        portfolio_value = self.portfolio.total_value

        # 1. 回撤检查
        self.drawdown.update(portfolio_value)
        if self.drawdown.is_triggered():
            alerts.append(f"[熔断] 回撤触发! 当前限制: {self.drawdown.get_position_limit():.0%}")

        # 2. 单日亏损
        if len(self.portfolio.daily_snapshots) >= 1:
            prev = self.portfolio.daily_snapshots[-1]["total_value"]
            daily_loss = (portfolio_value - prev) / prev
            if daily_loss < -0.05:
                alerts.append(f"[告警] 单日跌幅 {daily_loss:.2%}")

        # 3. 单票集中度
        for code, pos in self.portfolio.positions.items():
            if pos.weight > 0.20:
                alerts.append(f"[告警] {pos.name}({code}) 占比 {pos.weight:.1%} > 20%")

        return {"alerts": alerts, "triggered": len(alerts) > 0}

    def poll(self) -> dict:
        """执行一次完整的监控轮询"""
        quotes = self.fetch_quotes()
        self._latest_quotes = quotes

        if quotes:
            self.portfolio.update_prices(quotes)

        risk = self.check_risk()

        status = {
            "time": datetime.now().isoformat(),
            "portfolio": self.portfolio.get_summary(),
            "alerts": risk["alerts"],
            "n_quotes": len(quotes),
        }

        for alert in risk["alerts"]:
            if alert != self._last_alert:
                log.warning(alert)
                print(f"\n{'='*50}\n{alert}\n{'='*50}")
                self._alert_history.append({"time": datetime.now().isoformat(), "alert": alert})

        if risk["alerts"]:
            self._last_alert = risk["alerts"][0]

        return status

    def get_latest_quotes(self) -> dict:
        return self._latest_quotes

    def get_alerts(self) -> list:
        return self._alert_history[-20:]
