"""Walk-Forward 优化分析

借鉴 Hikyuu 的 SYS_WalkForward 设计：
在滑动时间窗口上，训练窗口内优化参数，
测试窗口外评估绩效。

这是系统化交易中防止过拟合的关键方法。
"""
import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from itertools import product
from typing import Callable, Optional


@dataclass
class WalkForwardResult:
    """Walk-Forward 分析结果"""
    total_returns: list[float] = field(default_factory=list)
    train_periods: list[tuple] = field(default_factory=list)
    test_periods: list[tuple] = field(default_factory=list)
    best_params: list[dict] = field(default_factory=list)
    train_sharpes: list[float] = field(default_factory=list)
    test_sharpes: list[float] = field(default_factory=list)
    oos_returns: list[float] = field(default_factory=list)  # 各窗口OOS收益

    @property
    def n_windows(self) -> int:
        return len(self.total_returns)

    @property
    def avg_oos_sharpe(self) -> float:
        if not self.test_sharpes:
            return 0
        return np.mean(self.test_sharpes)

    @property
    def avg_oos_return(self) -> float:
        if not self.oos_returns:
            return 0
        return np.mean(self.oos_returns)

    @property
    def param_stability(self) -> float:
        """参数稳定性：最佳参数变化的频率"""
        if len(self.best_params) < 2:
            return 1.0
        changes = 0
        keys = list(self.best_params[0].keys())
        for i in range(1, len(self.best_params)):
            for k in keys:
                if self.best_params[i].get(k) != self.best_params[i-1].get(k):
                    changes += 1
        max_changes = (len(self.best_params) - 1) * len(keys)
        return 1.0 - (changes / max_changes) if max_changes > 0 else 1.0

    def to_dataframe(self) -> pd.DataFrame:
        return pd.DataFrame({
            "train_start": [p[0] for p in self.train_periods],
            "train_end": [p[1] for p in self.train_periods],
            "test_start": [p[0] for p in self.test_periods],
            "test_end": [p[1] for p in self.test_periods],
            "best_params": [str(p) for p in self.best_params],
            "train_sharpe": self.train_sharpes,
            "test_sharpe": self.test_sharpes,
            "oos_return": self.oos_returns,
        })

    def print_summary(self):
        print("\n" + "=" * 55)
        print("        Walk-Forward 分析结果")
        print("=" * 55)
        print(f"  窗口数:       {self.n_windows}")
        print(f"  平均OOS夏普:  {self.avg_oos_sharpe:.2f}")
        print(f"  平均OOS收益:  {self.avg_oos_return:.2%}")
        print(f"  参数稳定性:   {self.param_stability:.1%}")
        if self.test_sharpes:
            print(f"  OOS夏普范围:  [{min(self.test_sharpes):.2f}, {max(self.test_sharpes):.2f}]")
            positive = sum(1 for s in self.test_sharpes if s > 0)
            print(f"  OOS正夏普率:  {positive}/{self.n_windows} ({positive/self.n_windows:.0%})")
        print("=" * 55)


class WalkForwardOptimizer:
    """Walk-Forward 优化器

    用法:
        wf = WalkForwardOptimizer(
            strategy_func,         # 策略工厂函数 (params) -> TradingSystem
            param_grid,            # 参数网格 {"fast": [5,10,20], "slow": [30,60]}
            train_months=24,       # 训练期长度(月)
            test_months=6,         # 测试期长度(月)
            metric="sharpe",       # 优化目标
        )
        result = wf.run(data)
    """

    def __init__(self, strategy_func: Callable, param_grid: dict,
                 train_months: int = 24, test_months: int = 6,
                 metric: str = "sharpe", step_months: Optional[int] = None):
        self.strategy_func = strategy_func
        self.param_grid = param_grid
        self.train_months = train_months
        self.test_months = test_months
        self.step_months = step_months or test_months
        self.metric = metric

    def run(self, data: pd.DataFrame, eval_func: Optional[Callable] = None) -> WalkForwardResult:
        """运行 Walk-Forward 分析

        Parameters
        ----------
        data : DataFrame
            完整的市场数据 (date, code, OHLCV, ...)
        eval_func : callable, optional
            自定义评估函数 (system, data_slice) -> float

        Returns
        -------
        WalkForwardResult
        """
        dates = sorted(pd.to_datetime(data["date"].unique()))
        if len(dates) < self.train_months + self.test_months:
            raise ValueError(f"数据不足: {len(dates)} 天 < {self.train_months + self.test_months} 月")

        param_names = list(self.param_grid.keys())
        param_combinations = list(product(*self.param_grid.values()))
        param_dicts = [dict(zip(param_names, combo)) for combo in param_combinations]

        result = WalkForwardResult()

        # 按月滑动窗口
        start_idx = 0
        while start_idx + self.train_months + self.test_months <= len(dates):
            train_end = dates[start_idx + self.train_months]
            test_start = train_end
            test_end = dates[min(start_idx + self.train_months + self.test_months, len(dates) - 1)]

            train_mask = (data["date"] >= dates[start_idx]) & (data["date"] < train_end)
            test_mask = (data["date"] >= train_end) & (data["date"] <= test_end)

            train_data = data[train_mask]
            test_data = data[test_mask]

            if len(train_data) < 100 or len(test_data) < 20:
                start_idx += self.step_months
                continue

            # 训练：遍历参数组合
            best_score = -float("inf")
            best_params = param_dicts[0]
            best_train_sharpe = 0

            for params in param_dicts:
                system = self.strategy_func(params)
                score = self._evaluate(system, train_data, eval_func)

                if score > best_score:
                    best_score = score
                    best_params = params
                    best_train_sharpe = score

            # 测试：在外样本上评估最佳参数
            system = self.strategy_func(best_params)
            test_score = self._evaluate(system, test_data, eval_func)

            result.train_periods.append((dates[start_idx], train_end))
            result.test_periods.append((train_end, test_end))
            result.best_params.append(best_params)
            result.train_sharpes.append(best_train_sharpe)
            result.oos_returns.append(test_score)
            result.test_sharpes.append(test_score)
            result.total_returns.append(test_score)

            start_idx += self.step_months

        return result

    def _evaluate(self, system, data, eval_func=None):
        """评估系统在给定数据上的表现"""
        if eval_func:
            return eval_func(system, data)

        # 默认：夏普比率
        dates = sorted(pd.to_datetime(data["date"].unique()))
        daily_values = []
        cash = 1_000_000

        for date in dates:
            day_data = data[data["date"] == date]
            market_data = {
                "benchmark_close": data.groupby("date")["close"].mean().tolist(),
            }

            for _, row in day_data.iterrows():
                code = row["code"]
                req = system.run_one(code, row, market_data, cash)
                if not req or req.action == "hold":
                    continue
                cash, record = system.execute(req, cash)

            pos_value = system.get_position_value(
                day_data.set_index("code")["close"].to_dict()
            )
            daily_values.append(cash + pos_value)

        if len(daily_values) < 2:
            return -999

        returns = pd.Series(daily_values).pct_change().dropna()
        if len(returns) < 2 or returns.std() == 0:
            return -999

        sharpe = returns.mean() / returns.std() * np.sqrt(252)
        return sharpe


def grid_search(data, strategy_func, param_grid, target="sharpe"):
    """简单网格搜索（不滑动窗口）

    Parameters
    ----------
    data : DataFrame
    strategy_func : callable (params) -> TradingSystem
    param_grid : dict
    target : str

    Returns
    -------
    best_params, best_score, all_results
    """
    param_names = list(param_grid.keys())
    param_combinations = list(product(*param_grid.values()))

    best_score = -float("inf")
    best_params = None
    all_results = []

    wf = WalkForwardOptimizer(strategy_func, param_grid, train_months=1000, test_months=0)
    # 手动遍历
    for combo in param_combinations:
        params = dict(zip(param_names, combo))
        system = strategy_func(params)
        score = wf._evaluate(system, data)
        all_results.append({"params": params, "score": score})
        if score > best_score:
            best_score = score
            best_params = params

    return best_params, best_score, pd.DataFrame(all_results)
