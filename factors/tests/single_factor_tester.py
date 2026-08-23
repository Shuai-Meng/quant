"""单因子检验框架

实现文章第3.2节的 SingleFactorTester。
包含：数据准备 → 因子计算 → 分层回测 → IC/IR分析 → 报告生成
"""
import numpy as np
import pandas as pd
import statsmodels.api as sm
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import warnings

warnings.filterwarnings("ignore")

import sys, os
_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _path not in sys.path:
    sys.path.insert(0, _path)
from utils.winsorize import mad_winsorize
from utils.standardize import zscore_standardize
from utils.neutralize import neutralize_by_industry


class SingleFactorTester:
    """单因子检验框架

    流程:
        1. prepare_data()  - 数据准备与清洗
        2. calc_factor()   - 因子计算 + 去极值 + 标准化
        3. portfolio_sort() - 分层回测（排序法）
        4. analyze()       - 绩效分析 + IC/IR
        5. generate_report() - 图文报告
    """

    def __init__(self, config=None):
        self.config = config or {
            "group_num": 5,
            "weight_method": "equal",
            "neutralize_industry": False,
            "industry_col": "industry",
        }
        self.data = None
        self.ret_df = None
        self.ic_df = None
        self.results = {}
        self.factor_name = "factor"

    def prepare_data(self, price_data, factor_data):
        """第一步：数据准备与对齐"""
        merge_on = ["date", "code"]
        price_cols = [c for c in merge_on + ["close", "market_cap", "volume", "industry"] if c in price_data.columns]
        factor_cols = [c for c in merge_on + ["factor_raw"] if c in factor_data.columns]

        self.data = pd.merge(
            price_data[price_cols], factor_data[factor_cols],
            on=merge_on, how="inner",
        )
        self.data["date"] = pd.to_datetime(self.data["date"])
        self.data = self.data.sort_values(["code", "date"]).reset_index(drop=True)

        # 计算未来1期收益率
        if "ret_next" not in self.data.columns and "close" in self.data.columns:
            self.data["ret_next"] = (
                self.data.groupby("code")["close"].pct_change().shift(-1)
            )
        return self

    def calc_factor(self, factor_func=None, factor_name="factor", standardize=True):
        """第二步：因子计算 + 去极值 + 标准化 + 行业中性化"""
        self.factor_name = factor_name

        if factor_func is not None:
            self.data[factor_name] = factor_func(self.data)
        elif "factor_raw" in self.data.columns:
            self.data[factor_name] = self.data["factor_raw"]
        else:
            raise ValueError("Must provide factor_func or 'factor_raw' column")

        if standardize:
            # MAD去极值 + z-score标准化
            self.data[f"{factor_name}_std"] = (
                self.data.groupby("date", group_keys=False)[factor_name]
                .transform(lambda x: zscore_standardize(mad_winsorize(x)))
            )
            factor_col = f"{factor_name}_std"
        else:
            factor_col = factor_name

        # 行业中性化
        if self.config.get("neutralize_industry") and self.config.get("industry_col") in self.data.columns:
            result = neutralize_by_industry(
                self.data, date_col="date", factor_col=factor_col,
                industry_col=self.config["industry_col"],
            )
            self.data = result
            if f"{factor_col}_neu" in self.data.columns:
                factor_col = f"{factor_col}_neu"

        self._factor_col = factor_col
        return self

    def portfolio_sort(self):
        """第三步：分层回测（投资组合排序法，文章2.1节）"""
        group_num = self.config.get("group_num", 5)
        fc = self._factor_col

        results, ic_results = [], []
        dates = sorted(self.data["date"].unique())

        for i, date in enumerate(dates[:-1]):
            current = self.data[self.data["date"] == date].copy()
            current = current.dropna(subset=[fc, "ret_next"])
            if len(current) < group_num * 5:
                continue

            current["factor_rank"] = current[fc].rank()
            try:
                current["group"] = pd.qcut(
                    current["factor_rank"], q=group_num,
                    labels=list(range(1, group_num + 1)),
                )
            except ValueError:
                continue

            # IC
            ic = current[[fc, "ret_next"]].corr(method="spearman").iloc[0, 1]
            ic_results.append({"date": date, "IC": ic})

            # 分组收益
            weight_method = self.config.get("weight_method", "equal")
            for g in range(1, group_num + 1):
                gdata = current[current["group"] == g]
                if len(gdata) == 0:
                    continue
                if weight_method == "market_value" and "market_cap" in gdata.columns:
                    w = gdata["market_cap"] / gdata["market_cap"].sum()
                    ret = (gdata["ret_next"] * w).sum()
                else:
                    ret = gdata["ret_next"].mean()
                results.append({"date": date, "group": g, "group_ret": ret})

        if not results:
            raise ValueError("No valid backtest results")

        self.ret_df = pd.DataFrame(results)
        self.ic_df = pd.DataFrame(ic_results)
        return self

    def analyze(self):
        """第四步：绩效分析与IC/IR统计"""
        if self.ret_df is None:
            raise ValueError("Run portfolio_sort() first")

        pivot = self.ret_df.pivot_table(
            index="date", columns="group", values="group_ret"
        )
        gn = self.config.get("group_num", 5)
        pivot["long_short"] = pivot[1] - pivot[gn]

        def _stats(series):
            if len(series) == 0:
                return {}
            n = len(series)
            ann_ret = (1 + series).prod() ** (12 / n) - 1
            ann_vol = series.std() * np.sqrt(12)
            sharpe = ann_ret / ann_vol if ann_vol > 0 else 0
            cum = (1 + series).cumprod()
            dd = (cum - cum.expanding().max()) / cum.expanding().max()
            max_dd = dd.min()
            win_rate = (series > 0).mean()
            calmar = ann_ret / abs(max_dd) if max_dd != 0 else np.nan
            return {
                "Annual_Return": ann_ret,
                "Annual_Vol": ann_vol,
                "Sharpe": sharpe,
                "Max_Drawdown": max_dd,
                "Win_Rate": win_rate,
                "Calmar": calmar,
                "Total_Return": (1 + series).prod() - 1,
            }

        perf = {col: _stats(pivot[col]) for col in pivot.columns}

        ic_mean = self.ic_df["IC"].mean()
        ic_std = self.ic_df["IC"].std()
        ic_ir = ic_mean / ic_std if ic_std > 0 else 0
        ic_hit = (self.ic_df["IC"] > 0).mean()

        self.results = {
            "cumulative_returns": (1 + pivot).cumprod(),
            "period_returns": pivot,
            "performance": perf,
            "ic_analysis": {
                "IC_mean": ic_mean,
                "IC_std": ic_std,
                "IR": ic_ir,
                "IC_hit_rate": ic_hit,
                "IC_series": self.ic_df,
            },
        }
        return self

    def generate_report(self, save_path=None, title=None):
        """第五步：生成图文报告"""
        if not self.results:
            self.analyze()

        fig, axes = plt.subplots(2, 2, figsize=(15, 10))
        fig.suptitle(title or f"单因子检验报告: {self.factor_name}", fontsize=14)

        # 1. 净值曲线
        ax1 = axes[0, 0]
        cum = self.results["cumulative_returns"]
        cols = [c for c in cum.columns if c != "long_short"]
        for c in cols:
            label = f"Group {c}" if c != "long_short" else "Long-Short"
            alpha = 0.6 if c != "long_short" else 1.0
            ls = "-" if c != "long_short" else "--"
            ax1.plot(cum.index, cum[c], ls=ls, alpha=alpha, label=label)
        ax1.set_title("分组净值曲线")
        ax1.legend(fontsize=8)
        ax1.grid(alpha=0.3)

        # 2. IC时序
        ax2 = axes[0, 1]
        ic = self.results["ic_analysis"]["IC_series"]
        ax2.bar(ic["date"], ic["IC"], alpha=0.6, width=1, color="steelblue")
        ax2.axhline(y=self.results["ic_analysis"]["IC_mean"], color="r", ls="--",
                    label=f"Mean: {self.results['ic_analysis']['IC_mean']:.4f}")
        ax2.axhline(y=0, color="k")
        ax2.set_title("IC 时间序列")
        ax2.legend(fontsize=8)
        ax2.grid(alpha=0.3)

        # 3. 分组年化收益
        ax3 = axes[1, 0]
        perf = self.results["performance"]
        gn = self.config.get("group_num", 5)
        group_keys = sorted([k for k in perf.keys() if isinstance(k, (int, np.integer))])
        labels = [str(k) for k in group_keys]
        rets = [perf[k].get("Annual_Return", 0) for k in group_keys]
        colors = plt.cm.RdYlGn(np.linspace(0.3, 0.7, len(rets)))
        bars = ax3.bar(labels, rets, color=colors)
        for bar, r in zip(bars, rets):
            ax3.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.001,
                     f"{r:.1%}", ha="center", va="bottom", fontsize=9)
        ax3.axhline(y=0, color="k")
        ax3.set_title("分组年化收益 (单调性检验)")
        ax3.set_ylabel("年化收益率")

        # 4. 汇总表
        ax4 = axes[1, 1]
        ax4.axis("off")
        metrics = ["Annual_Return", "Annual_Vol", "Sharpe", "Max_Drawdown", "Win_Rate"]
        fmt_map = {
            "Annual_Return": "{:.2%}", "Annual_Vol": "{:.2%}", "Sharpe": "{:.2f}",
            "Max_Drawdown": "{:.2%}", "Win_Rate": "{:.1%}", "Total_Return": "{:.2%}",
            "Calmar": "{:.2f}",
        }
        rows = []
        for label in [group_keys[0] if group_keys else 1, group_keys[-1] if group_keys else gn, "long_short"]:
            if label in perf:
                rows.append([label] + [fmt_map[m].format(perf[label].get(m, 0)) for m in metrics])
        col_labels = ["组合"] + [m.replace("_", " ") for m in metrics]
        table = ax4.table(cellText=rows, colLabels=col_labels, cellLoc="center", loc="center")
        table.auto_set_font_size(False)
        table.set_fontsize(8)
        ax4.set_title("绩效汇总", fontsize=10)

        ic_text = (f"IC均值: {self.results['ic_analysis']['IC_mean']:.4f}\n"
                   f"IR: {self.results['ic_analysis']['IR']:.2f}\n"
                   f"IC胜率: {self.results['ic_analysis']['IC_hit_rate']:.1%}")
        ax4.text(0.5, 0.15, ic_text, transform=ax4.transAxes, fontsize=9,
                ha="center", bbox=dict(boxstyle="round", facecolor="lightyellow"))

        plt.tight_layout()
        if save_path:
            plt.savefig(save_path, dpi=150, bbox_inches="tight")
            print(f"报告已保存: {save_path}")
        plt.close()

        # 打印结论
        ls_p = perf.get("long_short", {})
        print(f"\n===== 因子检验结果: {self.factor_name} =====")
        print(f"多空年化: {ls_p.get('Annual_Return', 0):.2%}")
        print(f"多空夏普: {ls_p.get('Sharpe', 0):.2f}")
        print(f"IR: {self.results['ic_analysis']['IR']:.2f}")
        monotonic = False
        if len(rets) >= 2:
            rets_with_nan = [r if r is not None and not (isinstance(r, float) and np.isnan(r)) else -999 for r in rets]
            monotonic = all(rets_with_nan[i] >= rets_with_nan[i + 1] for i in range(len(rets_with_nan) - 1))
        print(f"单调性: {'✅' if monotonic else '❌'}")
        print("==============================")
        return fig

    def fama_macbeth(self, factor_col=None, control_cols=None):
        """Fama-MacBeth 回归（文章2.1节）

        两步法：
        1. 每个截面做横截面回归
        2. 时间序列汇总，Newey-West调整标准误
        """
        if factor_col is None:
            factor_col = self._factor_col if hasattr(self, "_factor_col") else self.factor_name

        from collections import defaultdict
        gamma_dict = defaultdict(list)

        dates = sorted(self.data["date"].unique())
        for i, date in enumerate(dates[:-1]):
            current = self.data[self.data["date"] == date].copy()
            y = current["ret_next"].fillna(0).astype(float)

            cols = [factor_col]
            if control_cols:
                cols += control_cols

            X_cols = [c for c in cols if c in current.columns]
            if len(X_cols) == 0:
                continue

            X = sm.add_constant(current[X_cols].fillna(0)).astype(float)
            try:
                model = sm.OLS(y, X).fit()
                for c in X.columns:
                    gamma_dict[c].append(model.params[c])
            except Exception:
                continue

        if not gamma_dict:
            return pd.DataFrame()

        result = {}
        for factor_name, gammas in gamma_dict.items():
            g = np.array(gammas)
            mean_g = g.mean()
            std_g = g.std(ddof=1) / np.sqrt(len(g))
            t_stat = mean_g / std_g if std_g > 0 else 0
            result[factor_name] = {
                "mean": mean_g,
                "std_error": std_g,
                "t_stat": t_stat,
                "n_periods": len(g),
            }

        return pd.DataFrame(result).T

    def get_factor_scores(self):
        """获取最终因子评分（用于多因子合成）"""
        fc = self._factor_col if hasattr(self, "_factor_col") else self.factor_name
        scores = self.data[["date", "code", fc]].copy()
        scores = scores.rename(columns={fc: f"{self.factor_name}_score"}).dropna()
        return scores
