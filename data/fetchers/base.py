"""数据获取器基类 v2

集成统一返回契约、源健康追踪和韧性基础设施。
"""
import time
import logging
from functools import wraps

log = logging.getLogger("quant.fetcher")


def retry(max_attempts=3, delay=1.0, backoff=2.0):
    """重试装饰器"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_error = None
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_error = e
                    if attempt < max_attempts - 1:
                        wait = delay * (backoff ** attempt)
                        log.debug(f"Retry {attempt+1}/{max_attempts} after {wait:.1f}s: {e}")
                        time.sleep(wait)
            raise last_error or RuntimeError("retry exhausted")
        return wrapper
    return decorator


class DataFetcher:
    """数据获取器基类 v2

    集成：
    - 统一返回契约 (DataResult)
    - 源健康追踪 (source_health)
    - 健康包装器 (healthy_call)
    """

    def __init__(self, name="base"):
        self.name = name

    def _ok(self, data, provider="", **kwargs):
        """构建成功结果"""
        from ..unified_contract import DataResult
        return DataResult.ok(data, source=self.name, provider=provider, **kwargs)

    def _fail(self, message, error_code=None, **kwargs):
        """构建失败结果"""
        from ..unified_contract import DataResult
        return DataResult.fail(message, error_code=error_code, source=self.name, **kwargs)

    def healthy_call(self, func, *args, **kwargs):
        """健康包装：自动记录源健康状态

        装饰器替代方案，用于需要额外参数控制的场景。
        """
        from ..source_health import record as _record
        start = time.time()
        try:
            result = func(*args, **kwargs)
            elapsed = (time.time() - start) * 1000
            if isinstance(result, dict) and "success" in result:
                _record(self.name, result["success"], elapsed)
            else:
                _record(self.name, True, elapsed)
            return result
        except Exception as e:
            elapsed = (time.time() - start) * 1000
            _record(self.name, False, elapsed, str(e))
            raise
