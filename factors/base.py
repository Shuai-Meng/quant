"""因子计算器基类"""
from abc import ABC, abstractmethod
import pandas as pd


class FactorCalculator(ABC):
    """因子计算器基类

    所有因子计算器继承此类，实现 calculate() 方法。
    """

    def __init__(self, name: str, config: dict = None):
        self.name = name
        self.config = config or {}

    @abstractmethod
    def calculate(self, data: pd.DataFrame) -> pd.Series:
        """计算因子值

        Parameters
        ----------
        data : DataFrame
            按股票代码和时间排序的行情数据

        Returns
        -------
        Series of factor values, index aligned with data
        """
        ...

    def get_params(self) -> dict:
        return {"name": self.name, **self.config}
