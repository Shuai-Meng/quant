"""FactorSet: 因子集持久化管理

借鉴 Hikyuu 的 Factor + FactorSet 架构：
因子值可保存到磁盘/数据库，下次自动加载，避免重复计算。
"""
import os
import json
import logging
import hashlib
from datetime import datetime
from typing import Optional, Callable
import pandas as pd
import numpy as np

log = logging.getLogger("quant.factorset")

FACTOR_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "state", "factors")
os.makedirs(FACTOR_DIR, exist_ok=True)


class Factor:
    """单个因子

    封装因子元数据、计算逻辑、缓存值。
    """

    def __init__(self, name: str, calculator: Optional[Callable] = None,
                 config: Optional[dict] = None):
        self.name = name
        self.calculator = calculator
        self.config = config or {}
        self._values: Optional[pd.Series] = None
        self._meta = {
            "name": name,
            "config": config,
            "calculated_at": None,
            "n_valid": 0,
            "hash": "",
        }

    def calculate(self, data: pd.DataFrame) -> pd.Series:
        """计算因子值（如果已缓存则直接返回）"""
        cache_key = self._cache_key(data)
        if self._values is not None and self._meta.get("hash") == cache_key:
            return self._values

        if self.calculator:
            values = self.calculator(data, **self.config)
            self._values = values
            self._meta["calculated_at"] = datetime.now().isoformat()
            self._meta["n_valid"] = int(values.notna().sum())
            self._meta["hash"] = cache_key
            return values

        # 尝试从文件加载
        loaded = self._load_from_cache(cache_key)
        if loaded is not None:
            self._values = loaded
            self._meta["hash"] = cache_key
            return loaded

        raise ValueError(f"Factor '{self.name}' has no calculator and no cached data")

    def save(self):
        """保存因子值到磁盘"""
        if self._values is None:
            return

        path = self._get_cache_path()
        cache_dir = os.path.dirname(path)
        os.makedirs(cache_dir, exist_ok=True)

        # 保存为 parquet (更高效)
        df = self._values.reset_index(drop=True).to_frame("value")
        df.to_parquet(path, compression="zstd")

        # 保存元数据
        meta_path = path.replace(".parquet", ".meta.json")
        with open(meta_path, "w") as f:
            json.dump(self._meta, f, ensure_ascii=False, indent=2)

    def _load_from_cache(self, cache_key: str) -> Optional[pd.Series]:
        """从缓存加载"""
        path = self._get_cache_path()
        if not os.path.exists(path):
            return None

        meta_path = path.replace(".parquet", ".meta.json")
        if os.path.exists(meta_path):
            with open(meta_path) as f:
                self._meta = json.load(f)

        try:
            df = pd.read_parquet(path)
            return df["value"]
        except Exception:
            return None

    def _cache_key(self, data: pd.DataFrame) -> str:
        """生成缓存键（基于数据特征）"""
        h = hashlib.md5()
        h.update(str(len(data)).encode())
        h.update(str(data["date"].min()).encode())
        h.update(str(data["date"].max()).encode())
        h.update(self.name.encode())
        return h.hexdigest()[:12]

    def _get_cache_path(self) -> str:
        safe_name = self.name.replace("/", "_").replace(" ", "_")
        return os.path.join(FACTOR_DIR, f"{safe_name}.parquet")

    def __repr__(self):
        return f"Factor({self.name}, n_valid={self._meta.get('n_valid', 0)})"


class FactorSet:
    """因子集：管理一组因子

    用法:
        fs = FactorSet()
        fs.add(Factor("momentum", mom_calculator, {"lookback": 20}))
        fs.add(Factor("reversal", rev_calculator, {"lookback": 5}))
        scores = fs.calculate_all(data)
    """

    def __init__(self, name="default"):
        self.name = name
        self._factors: dict[str, Factor] = {}

    def add(self, factor: Factor):
        self._factors[factor.name] = factor

    def remove(self, name: str):
        self._factors.pop(name, None)

    def get(self, name: str) -> Optional[Factor]:
        return self._factors.get(name)

    def calculate_all(self, data: pd.DataFrame) -> dict[str, pd.Series]:
        """计算所有因子"""
        results = {}
        for name, factor in self._factors.items():
            try:
                results[name] = factor.calculate(data)
            except Exception as e:
                log.warning(f"Factor '{name}' calculation failed: {e}")
        return results

    def save_all(self):
        """保存所有因子"""
        for factor in self._factors.values():
            factor.save()

    def to_dataframe(self, data: pd.DataFrame, date_col="date", code_col="code"):
        """将因子计算为 DataFrame"""
        results = self.calculate_all(data)
        if not results:
            return pd.DataFrame()

        df = data[[date_col, code_col]].copy()
        for name, values in results.items():
            df[name] = values.values if hasattr(values, 'values') else values
        return df

    def names(self) -> list:
        return list(self._factors.keys())

    def __len__(self):
        return len(self._factors)

    def __repr__(self):
        return f"FactorSet({self.name}, {len(self._factors)} factors: {self.names()})"


def create_factor_set(factor_specs: dict, calculators: dict) -> FactorSet:
    """从配置创建 FactorSet

    Parameters
    ----------
    factor_specs : dict
        {name: {calculator: str, config: dict}}
    calculators : dict
        {name: callable}

    Returns
    -------
    FactorSet
    """
    fs = FactorSet()
    for name, spec in factor_specs.items():
        calc_name = spec.get("calculator", name)
        calc = calculators.get(calc_name)
        config = spec.get("config", {})
        fs.add(Factor(name, calc, config))
    return fs
