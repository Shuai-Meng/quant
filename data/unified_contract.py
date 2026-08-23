"""统一返回契约

参考 openclaw-data-china-stock 设计。
所有数据获取函数返回标准 DataResult，便于多源降级追踪和调试。
"""
from __future__ import annotations
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any, Optional
import pandas as pd


@dataclass
class DataResult:
    success: bool
    data: Any                    # DataFrame / dict / list
    message: str = ""
    source: str = ""
    provider: str = ""
    count: int = 0
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    cache_hit: bool = False
    fallback_route: str = ""
    attempt_counts: dict[str, int] = field(default_factory=dict)
    data_quality: dict[str, Any] = field(default_factory=dict)
    error_code: Optional[str] = None
    elapsed_ms: float = 0

    @classmethod
    def ok(cls, data, source="", provider="", **kwargs):
        df = data if isinstance(data, pd.DataFrame) else pd.DataFrame()
        count = len(df) if not df.empty else (len(data) if isinstance(data, (list, dict)) else 0)

        # 如果调用方传了 data_quality 就用，否则自己算
        quality = kwargs.pop("data_quality", None)
        if quality is None:
            quality = {
                "null_ratio": float(df.isnull().mean().mean()) if not df.empty else 0,
                "empty": bool(df.empty if isinstance(df, pd.DataFrame) else False),
            }
        fallback = kwargs.pop("fallback_route", "primary")
        attempts = kwargs.pop("attempt_counts", {source: 1})

        return cls(
            success=True, data=data, source=source, provider=provider,
            count=count, data_quality=quality,
            fallback_route=fallback,
            attempt_counts=attempts,
            **kwargs,
        )

    @classmethod
    def fail(cls, message, error_code=None, attempt_counts=None, **kwargs):
        return cls(
            success=False, data=pd.DataFrame(), message=message,
            error_code=error_code or "UNKNOWN_ERROR",
            attempt_counts=attempt_counts or {},
            data_quality={"null_ratio": 1.0, "empty": True},
            **kwargs,
        )

    def unwrap(self):
        return self.data if self.success else None

    def __bool__(self):
        return self.success and self.count > 0

    def __repr__(self):
        status = "OK" if self.success else "FAIL"
        extra = f" ({self.fallback_route})" if self.fallback_route and self.fallback_route != "primary" else ""
        return f"DataResult({status}, source={self.source}{extra}, count={self.count})"


def check_quality(df, min_rows=1, max_null_ratio=0.5):
    if df is None or (isinstance(df, pd.DataFrame) and df.empty):
        return False, {"null_ratio": 1.0, "empty": True, "min_rows": 0, "required": min_rows}
    null_ratio = float(df.isnull().mean().mean()) if isinstance(df, pd.DataFrame) else 0
    n_rows = len(df) if isinstance(df, pd.DataFrame) else 0
    quality = {"null_ratio": null_ratio, "empty": False, "min_rows": n_rows}
    if n_rows < min_rows:
        return False, quality
    if null_ratio > max_null_ratio:
        return False, quality
    return True, quality
