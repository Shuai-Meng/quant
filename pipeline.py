"""主流程：端到端因子回测流水线

一条命令完成：
1. 数据获取与清洗
2. 因子计算与检验
3. 多因子合成
4. 策略回测
5. 绩效报告
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
from datetime import datetime, date, timedelta

from data.fetchers.tencent import TencentFetcher
from data.fetchers.hexin import HexinFetcher
from data.fetchers.eastmoney import EastMoneyFetcher
from data.fetchers.akshare_fetcher import AkShareFetcher
from data.calendar import get_trade_calendar, get_monthly_trade_dates
from data.cleaners.price import calc_forward_return, mark_limit_status, calc_daily_returns
from data.cleaners.universe import filter_st, filter_new_stocks
from data.storage.cache import get_cached_or_fetch, cache_set, cache_get
from factors.technical.momentum import MomentumFactor
from factors.technical.reversal import ReversalFactor
from factors.technical.volume_ratio import VolumeRatioFactor
from factors.technical.rsi import RSIFactor
from factors.technical.ma_trend import MATrendFactor
from factors.behavioral.turnover import TurnoverTrendFactor
from factors.behavioral.amplitude import AmplitudeFactor
from factors.behavioral.hot_topic import HotTopicFactor
from factors.fundamental.value import ValueFactor
from factors.fundamental.quality import QualityFactor
from factors.tests.single_factor_tester import SingleFactorTester
from factors.synthesis.combiner import FactorCombiner
from backtest.engine import BacktestEngine
from backtest.performance import calc_performance_metrics, BacktestReporter
from utils.winsorize import mad_winsorize
from utils.standardize import zscore_standardize, standardize_by_group
from config import settings


class QuantPipeline:
    """量化研究主流程"""

    def __init__(self, config=None):
        self.config = config or {}
        self.tencent = TencentFetcher()
        self.hexin = HexinFetcher()
        self.eastmoney = EastMoneyFetcher()
        self.akshare = AkShareFetcher()
        self.data = {}
        self.factor_scores = {}

    def fetch_universe(self, refresh=False):
        """获取全市场股票列表"""
        key = "stock_list"
        if refresh:
            df = self.akshare.get_stock_list()
        else:
            df = get_cached_or_fetch(key, self.akshare.get_stock_list)
        self.data["stock_list"] = df
        print(f"股票列表: {len(df)} 只")
        return df

    def fetch_daily_data(self, codes, start="20200101", end="20260515", refresh=False):
        """批量获取日K线数据"""
        key = f"daily_{start}_{end}"
        if refresh or not cache_get(key):
            print("正在获取日K线数据（首次可能需要较长时间）...")
            df = self.tencent.get_batch_kline(codes, start, end)
            if not df.empty:
                cache_set(key, df)
        else:
            df = cache_get(key)
            print(f"从缓存加载日K线: {len(df)} 行")

        self.data["daily"] = df
        return df

    def calc_all_factors(self, data):
        """计算所有因子并返回评分"""
        scores = {}

        # 技术因子
        for name, cls in [
            ("momentum", MomentumFactor),
            ("reversal", ReversalFactor),
            ("volume_ratio", VolumeRatioFactor),
            ("rsi", RSIFactor),
            ("ma_trend", MATrendFactor),
        ]:
            params = settings.FACTORS.get(name, {})
            calculator = cls(name=name, config=params)
            factor_values = calculator.calculate(data)
            # 标准化的数据按截面分组才合适，这里返回原始值
            scores[name] = factor_values
            print(f"  {name}: {factor_values.notna().sum()} 个有效值")

        # 行为因子
        for name, cls in [
            ("turnover_trend", TurnoverTrendFactor),
            ("amplitude", AmplitudeFactor),
        ]:
            calculator = cls(name=name)
            factor_values = calculator.calculate(data)
            scores[name] = factor_values

        return scores

    def run_factor_test(self, factor_name="momentum", data=None, config=None):
        """运行单因子检验

        使用 SingleFactorTester（文章2.1+3.2节）
        """
        if data is None:
            data = self.data.get("daily")
        if data is None:
            raise ValueError("No data available")

        tester = SingleFactorTester(config or {})

        # 准备价格和因子数据
        price_data = data[["date", "code", "close", "volume"]].copy()
        price_data["market_cap"] = data.get("market_cap", 1)
        price_data["ret_next"] = data.groupby("code")["close"].pct_change().shift(-1)

        # 计算因子
        factor_map = {
            "momentum": MomentumFactor,
            "reversal": ReversalFactor,
            "volume_ratio": VolumeRatioFactor,
            "rsi": RSIFactor,
            "ma_trend": MATrendFactor,
        }
        if factor_name not in factor_map:
            raise ValueError(f"Unknown factor: {factor_name}")

        calc = factor_map[factor_name](name=factor_name, config=settings.FACTORS.get(factor_name, {}))
        factor_values = calc.calculate(data)

        factor_data = data[["date", "code"]].copy()
        factor_data["factor_raw"] = factor_values

        # 运行检验
        try:
            (tester.prepare_data(price_data, factor_data)
                  .calc_factor(factor_name=factor_name)
                  .portfolio_sort()
                  .analyze()
                  .generate_report(
                      save_path=f"reports/factor_{factor_name}.png",
                      title=f"因子检验: {factor_name}",
                  ))
            return tester
        except ValueError as e:
            print(f"  因子检验跳过: {e}")
            return None

    def multi_factor_backtest(self, start_date="2020-01-01", end_date="2026-05-15"):
        """多因子回测完整流程"""
        print("=" * 60)
        print("多因子策略回测")
        print("=" * 60)

        # 1. 准备数据
        data = self.data.get("daily")
        if data is None:
            raise ValueError("请先获取数据")

        # 2. 过滤股票池
        # 简单过滤：剔除上市不足1年（用数据长度近似）
        data = data.sort_values(["code", "date"])

        # 3. 计算所有因子
        print("\n计算因子...")
        for name, calc_class in [
            ("momentum", MomentumFactor),
            ("reversal", ReversalFactor),
            ("volume_ratio", VolumeRatioFactor),
            ("ma_trend", MATrendFactor),
        ]:
            calc = calc_class(name=name, config=settings.FACTORS.get(name, {}))
            data[name] = calc.calculate(data)

        # 4. 多因子合成
        print("\n因子标准化与合成...")
        combiner = FactorCombiner()

        for name in settings.FACTORS:
            if name in data.columns:
                # 每个截面做z-score标准化
                data[f"{name}_z"] = standardize_by_group(
                    data, name, "date", method="zscore"
                )
                score_col = f"{name}_z"
                weight = settings.FACTORS[name].get("weight", 1.0)
                score_df = data[["date", "code", score_col]].dropna().copy()
                score_df = score_df.rename(columns={score_col: f"{name}_score"})
                combiner.add_factor(name, score_df, weight=weight)
                print(f"  {name}: weight={weight}")

        # 5. 运行回测
        print("\n运行回测...")
        alpha = combiner.combine()
        if alpha is None or alpha.empty:
            print("因子合成失败，无有效信号")
            return None

        # 行情数据用作 universe（不含 alpha，避免列名冲突）
        engine = BacktestEngine(self.config)
        engine.set_universe(data)
        engine.set_signal(alpha, signal_col="alpha")
        portfolio = engine.run(start_date=start_date, end_date=end_date)

        # 6. 绩效报告
        print("\n绩效报告:")
        reporter = BacktestReporter(portfolio)
        reporter.print_report()

        self.data["portfolio"] = portfolio
        self.data["alpha"] = alpha
        return portfolio

    def fetch_hot_stocks(self, date_str=None):
        """获取当日热点强势股"""
        if date_str is None:
            date_str = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
        df = self.hexin.get_harden_stocks(date_str)
        if not df.empty:
            print(f"\n🔥 {date_str} 热点强势股 ({len(df)}只)")
            top = df.sort_values("change_pct", ascending=False).head(15)
            for _, r in top.iterrows():
                reason = r.get("reason_tags", "") or ""
                print(f"  {r['code']} {r.get('name','?')}  {r['change_pct']:+.2f}%  💡{reason[:50]}")
        return df
