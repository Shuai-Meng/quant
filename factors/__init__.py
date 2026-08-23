"""因子计算器注册

所有因子计算器统一在此注册，便于集中管理。
"""
from .technical.momentum import MomentumFactor
from .technical.reversal import ReversalFactor
from .technical.volume_ratio import VolumeRatioFactor
from .technical.rsi import RSIFactor
from .technical.ma_trend import MATrendFactor
from .behavioral.turnover import TurnoverTrendFactor
from .behavioral.amplitude import AmplitudeFactor
from .behavioral.hot_topic import HotTopicFactor
from .fundamental.value import ValueFactor
from .fundamental.quality import QualityFactor

FACTOR_REGISTRY = {
    "momentum": MomentumFactor,
    "reversal": ReversalFactor,
    "volume_ratio": VolumeRatioFactor,
    "rsi": RSIFactor,
    "ma_trend": MATrendFactor,
    "turnover_trend": TurnoverTrendFactor,
    "amplitude": AmplitudeFactor,
    "hot_topic": HotTopicFactor,
    "value_bp": ValueFactor,
    "quality": QualityFactor,
}


def get_factor_calculator(name, config=None):
    """获取因子计算器实例"""
    cls = FACTOR_REGISTRY.get(name)
    if cls is None:
        available = ", ".join(FACTOR_REGISTRY.keys())
        raise ValueError(f"Unknown factor '{name}'. Available: {available}")
    return cls(name=name, config=config)
