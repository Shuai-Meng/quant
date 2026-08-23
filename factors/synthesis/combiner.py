"""多因子合成器 v2

参考 awesome-systematic-trading 中 PyPortfolioOpt / vectorbt 的思路升级：
- 等权 / 静态加权 / IC_IR 加权 / 滚动IC加权
- 最大夏普组合优化（通过 deoptimization 逼近）
- 因子衰减分析
- 多期动态权重
"""
import pandas as pd
import numpy as np
from typing import Optional


class FactorCombiner:
    """多因子合成器 v2

    用法:
        combiner = FactorCombiner()
        combiner.add_factor("momentum", scores_df, weight=0.25)
        combiner.add_factor("reversal", scores_df, weight=0.15)
        combined = combiner.combine(method="ic_ir_weighted")
    """

    def __init__(self):
        self.factors: dict = {}
        self._ic_history: dict[str, list] = {}
        self._return_data: Optional[pd.DataFrame] = None

    def add_factor(self, name: str, scores: pd.DataFrame, weight: float = 1.0):
        """添加因子

        Parameters
        ----------
        name : str
            因子名称
        scores : DataFrame
            因子评分，必须包含 date, code, {name}_score 三列
        weight : float
            因子权重（用于 weighted 方法）
        """
        self.factors[name] = {"scores": scores.copy(), "weight": weight}

    def set_returns(self, returns: pd.DataFrame):
        """设置未来收益率数据用于IC计算

        Parameters
        ----------
        returns : DataFrame
            含 date, code, ret_next 列
        """
        self._return_data = returns

    def compute_ic(self, name: str) -> pd.Series:
        """计算单因子逐期IC（Spearman Rank Correlation）"""
        factor = self.factors[name]["scores"]
        score_col = self._find_score_col(name, factor)
        if score_col is None or self._return_data is None:
            return pd.Series(dtype=float)

        merged = pd.merge(
            factor[["date", "code", score_col]],
            self._return_data[["date", "code", "ret_next"]],
            on=["date", "code"], how="inner",
        ).dropna()

        if merged.empty:
            return pd.Series(dtype=float)

        ic = merged.groupby("date").apply(
            lambda g: g[score_col].corr(g["ret_next"], method="spearman")
        ).dropna()
        return ic

    def compute_all_ic(self) -> dict[str, pd.Series]:
        """计算所有因子的IC序列"""
        result = {}
        for name in self.factors:
            ic = self.compute_ic(name)
            if len(ic) > 0:
                result[name] = ic
                self._ic_history[name] = ic.values
        return result

    def get_ic_weights(self, window: int = 12, method: str = "ic_ir") -> dict[str, float]:
        """基于滚动IC计算动态权重

        Parameters
        ----------
        window : int
            滚动窗口期数
        method : str
            'ic_mean'  - 基于最近window期IC均值
            'ic_ir'    - 基于IC均值/IC标准差 (信息比率)
            'shrinkage' - 缩尾加权 (IC均值 + 正则化)

        Returns
        -------
        dict: {factor_name: weight}
        """
        weights = {}
        for name in self.factors:
            ic = self.compute_ic(name)
            if len(ic) < 3:
                weights[name] = self.factors[name]["weight"]
                continue

            recent = ic.iloc[-window:] if len(ic) > window else ic
            ic_mean = recent.mean()

            if method == "ic_ir":
                ic_std = recent.std()
                val = abs(ic_mean) / ic_std if ic_std > 0 else abs(ic_mean)
            elif method == "shrinkage":
                ic_std = recent.std()
                shrinkage = 0.3
                val = abs(ic_mean) * (1 - shrinkage) + shrinkage * abs(ic_mean) / (ic_std + 1e-8)
            else:
                val = abs(ic_mean)

            weights[name] = max(val, 0.001)

        total = sum(weights.values())
        if total > 0:
            return {k: v / total for k, v in weights.items()}
        return {k: 1.0 / len(weights) for k in weights}

    def get_correlation_matrix(self) -> pd.DataFrame:
        """计算因子间的截面相关性矩阵"""
        merged = self._merge_factors()
        if merged is None or len(merged) < 3:
            return pd.DataFrame()

        factor_cols = list(self.factors.keys())
        existing = [c for c in factor_cols if c in merged.columns]
        if len(existing) < 2:
            return pd.DataFrame()

        return merged[existing].corr()

    def optimize_weights(self, method: str = "max_sharpe", l2_reg: float = 0.1):
        """通过近似优化计算因子权重

        Parameters
        ----------
        method : str
            'max_sharpe' - 最大化夏普比率
            'risk_parity' - 风险平价
            'equal'      - 等权（作为比较基准）
        l2_reg : float
            L2 正则化参数，防止权重过度集中
        """
        corr = self.get_correlation_matrix()
        if corr.empty:
            return {k: 1.0 / len(self.factors) for k in self.factors}

        names = list(corr.columns)
        n = len(names)

        ic_series = {}
        for name in names:
            ic = self.compute_ic(name)
            ic_series[name] = abs(ic.mean()) if len(ic) > 0 else self.factors[name].get("weight", 1.0 / n)

        if method == "equal":
            return {k: 1.0 / n for k in names}

        if method == "risk_parity":
            try:
                inv_corr = np.linalg.inv(corr.values)
                raw = 1.0 / np.diag(inv_corr)
                raw = np.maximum(raw, 0.001)
                raw = raw / raw.sum()
                return dict(zip(names, raw))
            except Exception:
                return {k: 1.0 / n for k in names}

        # max_sharpe: 用IC作为期望收益的代理
        ic_vals = np.array([ic_series.get(name, 0) for name in names])
        ic_vals = np.maximum(ic_vals, 0.001)

        try:
            cov = np.diag(np.sqrt(np.diag(corr))) @ corr.values @ np.diag(np.sqrt(np.diag(corr)))
            inv_cov = np.linalg.inv(cov + np.eye(n) * l2_reg)
            raw = inv_cov @ ic_vals
            raw = np.maximum(raw, 0.001)
            raw = raw / raw.sum()
            return dict(zip(names, raw))
        except Exception:
            return {k: 1.0 / n for k in names}

    def combine(self, method: str = "weighted",
                date_col: str = "date", code_col: str = "code",
                ic_window: int = 12) -> pd.DataFrame:
        """合成为综合Alpha信号

        Parameters
        ----------
        method : str
            'weighted'         - 静态加权平均
            'equal'            - 等权平均
            'rank'             - 秩加权
            'ic_ir_weighted'   - IC/IR 动态加权
            'ic_mean_weighted' - IC 均值加权
            'max_sharpe'       - 最大夏普优化
            'risk_parity'      - 风险平价
        date_col, code_col : str
        ic_window : int
            IC计算窗口（用于动态权重方法）

        Returns
        -------
        DataFrame with date, code, alpha columns
        """
        if not self.factors:
            return pd.DataFrame()

        # 合并因子数据
        merged = self._merge_factors(date_col, code_col)
        if merged is None or merged.empty:
            return pd.DataFrame()

        factor_names = list(self.factors.keys())
        existing = [c for c in factor_names if c in merged.columns]

        if len(existing) == 0:
            return pd.DataFrame()

        # 获取权重
        if method == "equal":
            weights = {n: 1.0 / len(existing) for n in existing}
        elif method in ("ic_ir_weighted", "ic_mean_weighted", "ic_shrinkage"):
            sub = method.split("_")[1] if len(method.split("_")) > 1 else "ic_mean"
            sub_method = "ic_ir" if sub == "ir" else ("shrinkage" if sub == "shrinkage" else "ic_mean")
            weights = self.get_ic_weights(window=ic_window, method=sub_method)
        elif method in ("max_sharpe", "risk_parity"):
            weights = self.optimize_weights(method=method)
        else:
            # weighted / default: 使用配置的静态权重
            weights = {n: float(self.factors[n].get("weight", 1.0)) for n in existing}
            total = sum(weights.values())
            if total > 0:
                weights = {k: v / total for k, v in weights.items()}

        # 应用权重
        if method == "rank":
            for c in existing:
                merged[f"{c}_rank"] = merged.groupby(date_col)[c].rank(pct=True)
            merged["alpha"] = merged[[f"{c}_rank" for c in existing]].mean(axis=1)
        else:
            merged["alpha"] = 0.0
            for name in existing:
                w = weights.get(name, 0)
                if w > 0:
                    merged["alpha"] += merged[name].fillna(0) * w

        # 截面 z-score 标准化
        merged["alpha"] = merged.groupby(date_col)["alpha"].transform(
            lambda x: (x - x.mean()) / x.std() if x.std() > 0 else x - x.mean()
        )

        result_cols = [date_col, code_col, "alpha"] + existing
        return merged[result_cols].dropna(subset=["alpha"])

    def analyze_factor_decay(self, name: str, max_periods: int = 20) -> pd.Series:
        """因子衰减分析：IC随持有期变化"""
        factor = self.factors[name]["scores"]
        score_col = self._find_score_col(name, factor)
        if score_col is None or self._return_data is None:
            return pd.Series(dtype=float)

        merged = pd.merge(
            factor[["date", "code", score_col]],
            self._return_data[["date", "code"]],
            on=["date", "code"], how="inner",
        )

        ic_decay = {}
        for period in range(1, max_periods + 1):
            ret_col = f"ret_fwd_{period}d"
            if ret_col not in self._return_data.columns:
                merged_ret = self._return_data[["date", "code"]].copy()
                merged_ret[ret_col] = (
                    self._return_data.groupby("code")["ret_next"]
                    .transform(lambda x: x.rolling(period).apply(np.prod, raw=True).shift(-period))
                )
            else:
                merged_ret = self._return_data[["date", "code", ret_col]]

            combined = pd.merge(merged, merged_ret, on=["date", "code"], how="inner").dropna()
            if len(combined) > 10:
                ic = combined.groupby("date").apply(
                    lambda g: g[score_col].corr(g[ret_col], method="spearman")
                ).mean()
                ic_decay[period] = ic

        return pd.Series(ic_decay)

    @property
    def ic_summary(self) -> dict:
        """所有因子的IC汇总统计"""
        summary = {}
        for name in self.factors:
            ic = self.compute_ic(name)
            if len(ic) == 0:
                continue
            summary[name] = {
                "IC_mean": ic.mean(),
                "IC_std": ic.std(),
                "IR": ic.mean() / ic.std() if ic.std() > 0 else 0,
                "IC_hit_rate": (ic > 0).mean(),
                "N_periods": len(ic),
            }
        return summary

    def clear(self):
        """清除所有因子"""
        self.factors = {}
        self._ic_history = {}

    # ---- 内部方法 ----

    def _find_score_col(self, name: str, scores: pd.DataFrame) -> Optional[str]:
        score_col = f"{name}_score"
        if score_col in scores.columns:
            return score_col
        cols = [c for c in scores.columns if "score" in c]
        return cols[0] if cols else None

    def _merge_factors(self, date_col: str = "date", code_col: str = "code") -> Optional[pd.DataFrame]:
        merged = None
        for name, factor in self.factors.items():
            scores = factor["scores"]
            score_col = self._find_score_col(name, scores)
            if score_col is None:
                for col in scores.columns:
                    if col not in (date_col, code_col):
                        scores = scores.rename(columns={col: f"{name}_score"})
                        score_col = f"{name}_score"
                        break
            if score_col is None:
                continue

            weight = factor.get("weight", 1.0)
            subset = scores[[date_col, code_col, score_col]].copy()
            subset = subset.rename(columns={score_col: name})
            subset[name] = subset[name] * weight

            if merged is None:
                merged = subset
            else:
                merged = pd.merge(merged, subset, on=[date_col, code_col], how="outer")
        return merged
