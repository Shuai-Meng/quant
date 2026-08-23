"""YAML 配置加载器

参考 openclaw-data-china-stock 的 config_loader.py。
支持 ${ENV_VAR} 占位符解析、缓存、默认值合并。
"""
import os
import re
import threading
import time

_ENV_PATTERN = re.compile(r'\$\{([^}]+)\}')


def _resolve_env(value):
    """递归解析字符串中的 ${ENV_VAR} 占位符"""
    if not isinstance(value, str):
        return value

    def _replace(match):
        var = match.group(1)
        default = None
        if ':-' in var:
            var, default = var.split(':-', 1)
        return os.environ.get(var, default or '')

    resolved = _ENV_PATTERN.sub(_replace, value)
    return resolved


def resolve_config(obj):
    """递归解析 dict/list 中的环境变量"""
    if isinstance(obj, dict):
        return {k: resolve_config(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [resolve_config(v) for v in obj]
    elif isinstance(obj, str):
        return _resolve_env(obj)
    return obj


class ConfigLoader:
    """YAML 配置加载器

    缓存 60 秒，自动解析环境变量。
    """

    def __init__(self, config_dir=None, cache_ttl=60):
        if config_dir is None:
            config_dir = os.path.dirname(os.path.abspath(__file__))
        self.config_dir = config_dir
        self.cache_ttl = cache_ttl
        self._cache = {}
        self._cache_time = {}
        self._lock = threading.Lock()

    def load(self, filename, default=None):
        """加载 YAML 配置文件

        Parameters
        ----------
        filename : str
            相对于 config_dir 的文件名，不含路径则自动加 .yaml/.yml
        default : dict
            默认值，与加载的配置合并

        Returns
        -------
        dict
        """
        import yaml

        if not filename.endswith(('.yaml', '.yml')):
            for ext in ('.yaml', '.yml'):
                path = os.path.join(self.config_dir, filename + ext)
                if os.path.exists(path):
                    filename = filename + ext
                    break
            else:
                path = os.path.join(self.config_dir, filename + '.yaml')

        path = os.path.join(self.config_dir, filename)

        with self._lock:
            cached = self._cache.get(path)
            cache_time = self._cache_time.get(path, 0)
            if cached is not None and (time.time() - cache_time) < self.cache_ttl:
                return cached

        try:
            with open(path, 'r') as f:
                data = yaml.safe_load(f) or {}
        except FileNotFoundError:
            if default is not None:
                return default
            raise

        if default:
            data = _deep_merge(default, data)

        resolved = resolve_config(data)

        with self._lock:
            self._cache[path] = resolved
            self._cache_time[path] = time.time()

        return resolved

    def load_or_default(self, filename, default):
        try:
            return self.load(filename, default)
        except Exception:
            return default

    def invalidate(self, filename=None):
        with self._lock:
            if filename:
                path = os.path.join(self.config_dir, filename)
                self._cache.pop(path, None)
                self._cache_time.pop(path, None)
            else:
                self._cache.clear()
                self._cache_time.clear()


def _deep_merge(base, override):
    """深度合并两个 dict"""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


# 全局加载器
_loader = ConfigLoader()


def load_config(filename, default=None):
    return _loader.load(filename, default)


def load_settings():
    """加载主配置（兼容旧 settings.py）

    Returns
    -------
    dict with MARKET, TRADING_COST, RISK, FACTORS, SIGNAL_WEIGHTS
    """
    base = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, 'settings.yaml')

    if os.path.exists(path):
        return _loader.load('settings.yaml')
    else:
        # 回退到旧 settings.py
        try:
            from config import settings
            return {
                "MARKET": settings.MARKET,
                "TRADING_COST": settings.TRADING_COST,
                "RISK": settings.RISK,
                "FACTORS": settings.FACTORS,
                "SIGNAL_WEIGHTS": settings.SIGNAL_WEIGHTS,
            }
        except ImportError:
            return {}
