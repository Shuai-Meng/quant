"""韧性基础设施

参考 openclaw-data-china-stock 的 circuit_breaker / source_health / upstream_spacing。
提供：熔断器、上游间距控制、质量门。
"""
import time
import logging
from datetime import datetime, timedelta
from enum import Enum

log = logging.getLogger("quant.resilience")


class CircuitState(Enum):
    CLOSED = 0       # 正常
    OPEN = 1         # 熔断中
    HALF_OPEN = 2    # 半开（试探）


class CircuitBreaker:
    """熔断器

    连续失败超过阈值后进入 OPEN 状态，拒绝请求一段时间。
    之后进入 HALF_OPEN 试探一次，成功则恢复 CLOSED。
    """

    def __init__(self, name="default", failure_threshold=3, recovery_timeout=60, half_open_limit=1):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_limit = half_open_limit

        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time = None
        self.opened_at = None
        self.half_open_attempts = 0
        self.total_failures = 0
        self.total_successes = 0

    def allow_request(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            if self._should_attempt_recovery():
                self.state = CircuitState.HALF_OPEN
                self.half_open_attempts = 0
                log.info(f"[{self.name}] Circuit transitioning OPEN -> HALF_OPEN")
                return True
            return False
        if self.state == CircuitState.HALF_OPEN:
            return self.half_open_attempts < self.half_open_limit
        return True

    def record_success(self):
        self.total_successes += 1
        if self.state == CircuitState.HALF_OPEN:
            self.half_open_attempts += 1
            if self._should_close():
                self.state = CircuitState.CLOSED
                self.failure_count = 0
                log.info(f"[{self.name}] Circuit recovered HALF_OPEN -> CLOSED")
        elif self.state == CircuitState.CLOSED:
            self.failure_count = 0

    def record_failure(self):
        self.total_failures += 1
        self.last_failure_time = datetime.now()

        if self.state == CircuitState.HALF_OPEN:
            self.state = CircuitState.OPEN
            self.opened_at = datetime.now()
            log.warning(f"[{self.name}] Circuit HALF_OPEN -> OPEN after probe failure")
        elif self.state == CircuitState.CLOSED:
            self.failure_count += 1
            if self.failure_count >= self.failure_threshold:
                self.state = CircuitState.OPEN
                self.opened_at = datetime.now()
                log.warning(
                    f"[{self.name}] Circuit OPENED after "
                    f"{self.failure_count}/{self.failure_threshold} failures"
                )

    def _should_attempt_recovery(self) -> bool:
        if self.opened_at is None:
            return True
        return (datetime.now() - self.opened_at).total_seconds() >= self.recovery_timeout

    def _should_close(self) -> bool:
        return self.half_open_attempts >= self.half_open_limit

    def status(self) -> dict:
        return {
            "name": self.name,
            "state": self.state.name,
            "failure_count": self.failure_count,
            "total_failures": self.total_failures,
            "total_successes": self.total_successes,
            "opened_at": self.opened_at.isoformat() if self.opened_at else None,
        }


class UpstreamSpacing:
    """上游调用间距控制

    避免短时间内密集请求同一数据源被限流。
    """

    def __init__(self, min_interval=0.1):
        self.min_interval = min_interval
        self._last_call = {}

    def wait(self, key="default"):
        now = time.time()
        last = self._last_call.get(key, 0)
        elapsed = now - last
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_call[key] = time.time()


class QualityGate:
    """数据质量门

    从上游获取数据后，通过多层质量检查才接受。
    """

    def __init__(self, config=None):
        self.config = config or {
            "min_rows": 1,
            "max_null_ratio": 0.5,
            "required_columns": [],
            "min_unique_codes": 0,
        }

    def check(self, df):
        """检查 DataFrame 是否通过质量门"""
        from .unified_contract import check_quality

        passed, quality = check_quality(df, self.config.get("min_rows", 1),
                                        self.config.get("max_null_ratio", 0.5))
        if not passed:
            return False, quality, "failed_min_rows_or_null"

        for col in self.config.get("required_columns", []):
            if col not in df.columns:
                return False, quality, f"missing_column:{col}"

        if self.config.get("min_unique_codes", 0) > 0:
            if "code" in df.columns and df["code"].nunique() < self.config["min_unique_codes"]:
                return False, quality, "insufficient_unique_codes"

        return True, quality, "ok"


class MultiSourceFetcher:
    """多源降级链

    按优先级顺序尝试多个数据源，通过质量门才接受。
    """

    def __init__(self):
        self._breakers = {}
        self._spacing = UpstreamSpacing()

    def fetch(self, chain, quality_config=None):
        """按顺序尝试数据源链

        Parameters
        ----------
        chain : list of (source_name, callable)
            [("tencent", tencent_fetch_func), ("akshare", akshare_fetch_func), ("cache", cache_func)]
        quality_config : dict
            传给 QualityGate 的配置

        Returns
        -------
        DataResult
        """
        from .unified_contract import DataResult

        gate = QualityGate(quality_config)
        attempt_counts = {}
        best_fail_result = None

        for source_name, fetch_func in chain:
            breaker = self._get_breaker(source_name)
            if not breaker.allow_request():
                log.debug(f"[{source_name}] Circuit OPEN, skipping")
                attempt_counts[source_name] = 0
                continue

            try:
                self._spacing.wait(source_name)
                start = time.time()
                result = fetch_func()
                elapsed = (time.time() - start) * 1000
                attempt_counts[source_name] = attempt_counts.get(source_name, 0) + 1

                if isinstance(result, DataResult):
                    if not result.success:
                        breaker.record_failure()
                        if best_fail_result is None:
                            best_fail_result = result
                        continue
                    df = result.data
                    result.elapsed_ms = elapsed
                else:
                    df = result

                passed, quality, reason = gate.check(df)
                if passed:
                    breaker.record_success()
                    return DataResult.ok(
                        df,
                        source=source_name,
                        fallback_route="primary" if source_name == chain[0][0] else f"fallback_{source_name}",
                        attempt_counts=attempt_counts,
                        elapsed_ms=elapsed,
                        data_quality=quality,
                    )
                else:
                    log.debug(f"[{source_name}] Quality gate failed: {reason}")
                    breaker.record_failure()

            except Exception as e:
                log.warning(f"[{source_name}] Exception: {e}")
                breaker.record_failure()
                attempt_counts[source_name] = attempt_counts.get(source_name, 0) + 1

        if best_fail_result is not None:
            return best_fail_result
        return DataResult.fail("All sources exhausted", attempt_counts=attempt_counts)

    def _get_breaker(self, name):
        if name not in self._breakers:
            self._breakers[name] = CircuitBreaker(name=name)
        return self._breakers[name]

    def get_breakers_status(self):
        return {name: cb.status() for name, cb in self._breakers.items()}
