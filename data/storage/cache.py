"""本地 Parquet 缓存层

避免重复从API获取数据。
"""
import os
import pandas as pd
from pathlib import Path

CACHE_DIR = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) / "storage" / "cache"


def _ensure_cache_dir():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _sanitize_key(key):
    """将缓存key转为合法文件名"""
    return str(key).replace("/", "_").replace(":", "_").replace(" ", "_")


def cache_get(key):
    """获取缓存数据

    Parameters
    ----------
    key : str
        缓存键

    Returns
    -------
    DataFrame or None
    """
    _ensure_cache_dir()
    path = CACHE_DIR / f"{_sanitize_key(key)}.parquet"
    if path.exists():
        try:
            return pd.read_parquet(path)
        except Exception:
            return None
    return None


def cache_set(key, df):
    """设置缓存

    Parameters
    ----------
    key : str
        缓存键
    df : DataFrame
        要缓存的数据
    """
    _ensure_cache_dir()
    path = CACHE_DIR / f"{_sanitize_key(key)}.parquet"
    try:
        df.to_parquet(path, compression="zstd")
    except Exception as e:
        print(f"Cache write failed: {e}")


def cache_exists(key):
    """检查缓存是否存在"""
    _ensure_cache_dir()
    path = CACHE_DIR / f"{_sanitize_key(key)}.parquet"
    return path.exists()


def cache_clear():
    """清除所有缓存"""
    _ensure_cache_dir()
    for f in CACHE_DIR.glob("*.parquet"):
        f.unlink()


def get_cached_or_fetch(key, fetch_func, *args, **kwargs):
    """带缓存的获取模式

    Parameters
    ----------
    key : str
        缓存键
    fetch_func : callable
        数据获取函数
    args, kwargs : 传递给 fetch_func

    Returns
    -------
    DataFrame
    """
    cached = cache_get(key)
    if cached is not None:
        return cached
    df = fetch_func(*args, **kwargs)
    if df is not None and not df.empty:
        cache_set(key, df)
    return df
