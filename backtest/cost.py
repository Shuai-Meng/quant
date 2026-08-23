"""交易成本模型

精确模拟A股交易成本：佣金、印花税、过户费、滑点。
"""
import numpy as np
import pandas as pd


class TradingCost:
    """A股交易成本模型"""

    def __init__(self, config=None):
        self.config = config or {}
        self.commission_rate = self.config.get("commission_rate", 0.0003)  # 万三
        self.stamp_tax_rate = self.config.get("stamp_tax_rate", 0.001)  # 千一（卖出）
        self.transfer_fee = self.config.get("transfer_fee", 0.00002)  # 万0.2
        self.slippage_pct = self.config.get("slippage_pct", 0.001)  # 0.1%
        self.min_commission = self.config.get("min_commission", 5)  # 最低5元

    def buy_cost(self, price, shares):
        """计算买入成本

        Returns
        -------
        dict with total_cost, commission, transfer_fee, slippage
        """
        amount = price * shares
        commission = max(amount * self.commission_rate, self.min_commission)
        transfer = amount * self.transfer_fee
        slippage = amount * self.slippage_pct
        total = amount + commission + transfer + slippage
        return {
            "total": total,
            "amount": amount,
            "commission": commission,
            "transfer_fee": transfer,
            "slippage": slippage,
        }

    def sell_proceed(self, price, shares):
        """计算卖出所得

        Returns
        -------
        dict with net_proceed, commission, stamp_tax, transfer_fee, slippage
        """
        amount = price * shares
        commission = max(amount * self.commission_rate, self.min_commission)
        stamp_tax = amount * self.stamp_tax_rate
        transfer = amount * self.transfer_fee
        slippage = amount * self.slippage_pct
        net = amount - commission - stamp_tax - transfer - slippage
        return {
            "net": net,
            "amount": amount,
            "commission": commission,
            "stamp_tax": stamp_tax,
            "transfer_fee": transfer,
            "slippage": slippage,
        }

    def round_trip_cost(self, price, shares):
        """计算一次完整买卖的总成本（百分比）"""
        buy = self.buy_cost(price, shares)
        sell = self.sell_proceed(price, shares)
        total_cost = buy["commission"] + buy["transfer_fee"] + buy["slippage"] + \
                     sell["commission"] + sell["stamp_tax"] + sell["transfer_fee"] + \
                     sell["slippage"]
        return total_cost / (price * shares)
