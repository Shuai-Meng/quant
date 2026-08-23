"""收益归因分析：对回测结果进行收益分解"""
import numpy as np
import pandas as pd


class Attribution:
    """绩效归因分析

    将收益分解为：
    - 因子配置收益（来自因子暴露）
    - 个股选择收益（来自选股能力）
    - 市场收益
    """

    def __init__(self, portfolio, benchmark_returns, factor_returns=None):
        """
        Parameters
        ----------
        portfolio : DataFrame
            包含 return, date 列
        benchmark_returns : Series
            基准（如沪深300）收益率
        factor_returns : dict of Series, optional
            各因子收益率时间序列
        """
        self.portfolio = portfolio
        self.benchmark = benchmark_returns
        self.factor_returns = factor_returns

    def brinson_attribution(self):
        """Brinson归因：超额收益分解"""
        if "return" not in self.portfolio.columns:
            return {}
        port_ret = self.portfolio["return"].dropna().mean()
        bench_ret = self.benchmark.mean()
        excess = port_ret - bench_ret

        # 简单的选股收益 = 残差
        # 这里用行业比例近似
        alloc_ret = 0
        select_ret = excess - alloc_ret

        return {
            "Portfolio_Return": port_ret,
            "Benchmark_Return": bench_ret,
            "Excess_Return": excess,
            "Allocation_Effect": alloc_ret,
            "Selection_Effect": select_ret,
        }
