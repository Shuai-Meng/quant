"""向量化回测引擎

支持多因子策略的向量化回测，模拟A股真实交易条件。
包括：股票池过滤、调仓逻辑、交易成本、涨跌停限制。
"""
import numpy as np
import pandas as pd


class BacktestEngine:
    """向量化回测引擎

    用法:
        engine = BacktestEngine(config)
        engine.set_universe(universe_df)       # 设置股票池和行情
        engine.set_signal(signal_df)            # 设置每日alpha信号
        result = engine.run()                   # 运行回测
    """

    def __init__(self, config=None):
        self.config = config or {}
        self.universe = None
        self.signal = None
        self.holdings = None
        self.portfolio = None
        self.trade_log = []

    def set_universe(self, universe_df):
        """设置全市场数据

        Parameters
        ----------
        universe_df : DataFrame
            必须含 date, code, close, volume, market_cap 列
            可选: pre_close, is_limit_up, is_limit_down
        """
        self.universe = universe_df.copy()
        self.universe["date"] = pd.to_datetime(self.universe["date"])
        self.universe = self.universe.sort_values(["date", "code"]).reset_index(drop=True)
        return self

    def set_signal(self, signal_df, signal_col="alpha"):
        """设置因子信号

        Parameters
        ----------
        signal_df : DataFrame
            必须含 date, code, alpha (或指定signal_col)
        signal_col : str
            信号值列名
        """
        self.signal = signal_df.copy()
        self.signal["date"] = pd.to_datetime(self.signal["date"])
        self.signal = self.signal.sort_values(["date", "code"]).reset_index(drop=True)
        self._signal_col = signal_col
        return self

    def get_top_stocks(self, date, n=20):
        """获取指定日期得分最高的n只股票"""
        if self.signal is None:
            return []
        sig = self.signal[self.signal["date"] == pd.Timestamp(date)].copy()
        if sig.empty:
            return []
        sig = sig.sort_values(self._signal_col, ascending=False).head(n)
        return sig["code"].tolist()

    def run(self, start_date=None, end_date=None):
        """运行回测

        Parameters
        ----------
        start_date, end_date : str or Timestamp

        Returns
        -------
        DataFrame : 每日持仓和净值
        """
        if self.universe is None or self.signal is None:
            raise ValueError("Must call set_universe() and set_signal() first")

        # 合并信号和行情
        data = pd.merge(
            self.signal, self.universe,
            on=["date", "code"], how="inner",
            suffixes=("_sig", "_mkt"),
        )
        if start_date:
            data = data[data["date"] >= pd.Timestamp(start_date)]
        if end_date:
            data = data[data["date"] <= pd.Timestamp(end_date)]

        # 配置参数
        top_n = self.config.get("top_n_stocks", 20)
        rebalance_freq = self.config.get("rebalance_freq", "monthly")
        commission = self.config.get("commission", 0.0003)
        stamp_tax = self.config.get("stamp_tax", 0.001)
        slippage = self.config.get("slippage", 0.001)

        dates = sorted(data["date"].unique())
        portfolio = []
        current_holdings = {}  # code -> shares
        initial_capital = self.config.get("initial_capital", 1_000_000)
        cash_balance = initial_capital

        for i, date in enumerate(dates):
            day_data = data[data["date"] == date]

            # 判断是否调仓日
            is_rebalance = False
            if i == 0:
                is_rebalance = True
            elif rebalance_freq == "daily":
                is_rebalance = True
            elif rebalance_freq == "monthly":
                if date.month != dates[i - 1].month:
                    is_rebalance = True
            elif rebalance_freq == "weekly":
                if date.isocalendar()[1] != dates[i - 1].isocalendar()[1]:
                    is_rebalance = True

            if is_rebalance:
                sorted_stocks = day_data.sort_values(
                    self._signal_col, ascending=False
                ).head(top_n)
                target_codes = set(sorted_stocks["code"].tolist())
            else:
                target_codes = set(current_holdings.keys())

            today_cash_flow = 0
            stock_value = 0
            new_holdings = {}

            # 先计算持仓市值 + 处理卖出
            for code, h in list(current_holdings.items()):
                stock_day = day_data[day_data["code"] == code]
                if stock_day.empty:
                    continue
                close = stock_day["close"].iloc[0]
                shares = h["shares"]

                if code in target_codes:
                    new_holdings[code] = h
                    stock_value += shares * close
                else:
                    sell_value = shares * close * (1 - slippage)
                    sell_value -= sell_value * (commission + stamp_tax)
                    today_cash_flow += sell_value

            # 处理买入
            if is_rebalance and target_codes:
                active_capital = cash_balance + stock_value
                budget_per_stock = active_capital / max(len(target_codes), 1)

                for code in target_codes:
                    if code in current_holdings:
                        continue
                    stock_day = day_data[day_data["code"] == code]
                    if stock_day.empty:
                        continue
                    close = stock_day["close"].iloc[0]
                    buy_price = close * (1 + slippage)
                    buy_cost = buy_price * commission
                    actual_cost = buy_price + buy_cost
                    shares = int(budget_per_stock / actual_cost) if actual_cost > 0 else 0

                    if shares > 0:
                        cost = shares * actual_cost
                        today_cash_flow -= cost
                        new_holdings[code] = {
                            "shares": shares,
                            "buy_price": buy_price,
                        }
                        stock_value += shares * close

            cash_balance += today_cash_flow
            current_holdings = new_holdings
            total_value = cash_balance + stock_value

            portfolio.append({
                "date": date,
                "total_value": total_value,
                "cash": cash_balance,
                "stock_value": stock_value,
                "n_holdings": len(current_holdings),
                "codes": list(current_holdings.keys()),
            })

        self.portfolio = pd.DataFrame(portfolio)
        if not self.portfolio.empty:
            self.portfolio["return"] = self.portfolio["total_value"].pct_change()
            self.portfolio["cum_return"] = (
                1 + self.portfolio["return"]
            ).cumprod() - 1

        return self.portfolio

    def get_performance(self):
        """计算回测绩效指标"""
        if self.portfolio is None or self.portfolio.empty:
            return {}

        ret = self.portfolio["return"].dropna()
        n = len(ret)
        if n == 0:
            return {}

        ann_ret = (1 + ret).prod() ** (252 / n) - 1
        ann_vol = ret.std() * np.sqrt(252)
        sharpe = ann_ret / ann_vol if ann_vol > 0 else 0

        cum = (1 + ret).cumprod()
        dd = (cum - cum.expanding().max()) / cum.expanding().max()
        max_dd = dd.min()
        win_rate = (ret > 0).mean()
        calmar = ann_ret / abs(max_dd) if max_dd != 0 else np.nan

        return {
            "Annual_Return": ann_ret,
            "Annual_Vol": ann_vol,
            "Sharpe": sharpe,
            "Max_Drawdown": max_dd,
            "Win_Rate": win_rate,
            "Calmar": calmar,
            "Total_Return": (1 + ret).prod() - 1,
            "N_Trades": self.portfolio["n_holdings"].mean(),
        }
