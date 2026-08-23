"""行业/因子中性化处理

通过OLS回归提取剔除行业或已知因子影响后的纯净因子。
"""
import numpy as np
import pandas as pd
import statsmodels.api as sm


def neutralize_by_industry(df, date_col="date", factor_col="factor", industry_col="industry"):
    """行业中性化

    对每个截面日期做 OLS 回归:
        factor = α + Σβ_i * IndustryDummy_i + ε
    返回 ε = 剔除行业影响后的纯因子值

    Parameters
    ----------
    df : DataFrame
    date_col : str
        截面日期列
    factor_col : str
        因子值列
    industry_col : str
        行业列

    Returns
    -------
    DataFrame with added '{factor_col}_neu' column
    """
    result = df.copy()
    result[f"{factor_col}_neu"] = np.nan

    for date, group in result.groupby(date_col):
        group = group.dropna(subset=[factor_col])
        if len(group) < 10:
            continue
        dummies = pd.get_dummies(group[industry_col].fillna("UNKNOWN"), prefix="ind")
        X = sm.add_constant(dummies)
        y = group[factor_col].fillna(0)
        try:
            model = sm.OLS(y, X.astype(float)).fit()
            result.loc[group.index, f"{factor_col}_neu"] = model.resid.values
        except Exception:
            pass

    return result


def neutralize_by_factors(df, date_col="date", target_col="factor", control_cols=None):
    """多因子中性化

    控制多个已知因子后提取纯净因子:
        factor = α + Σβ_i * ControlFactor_i + ε
    返回 ε

    Parameters
    ----------
    df : DataFrame
    date_col : str
    target_col : str
        待检验因子
    control_cols : list
        控制因子列名列表

    Returns
    -------
    DataFrame with added '{target_col}_pure' column
    """
    if control_cols is None:
        return df

    result = df.copy()
    result[f"{target_col}_pure"] = np.nan

    for date, group in result.groupby(date_col):
        group = group.dropna(subset=[target_col] + control_cols)
        if len(group) < len(control_cols) + 5:
            continue
        X = sm.add_constant(group[control_cols].fillna(0))
        y = group[target_col].fillna(0)
        try:
            model = sm.OLS(y, X.astype(float)).fit()
            result.loc[group.index, f"{target_col}_pure"] = model.resid.values
        except Exception:
            pass

    return result
