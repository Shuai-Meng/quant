"""数据源健康监控

参考 openclaw-data-china-stock 的 source_health.py。
记录各数据源的成功率、延迟、最后状态，支持历史滚动。
"""
import json
import os
import time
import threading
from datetime import datetime
from dataclasses import dataclass, field, asdict


HEALTH_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "state", "source_health.jsonl")


@dataclass
class HealthSnapshot:
    source: str
    status: str = "unknown"       # "ok" | "degraded" | "down"
    success_rate_1h: float = 1.0
    success_rate_24h: float = 1.0
    avg_latency_ms: float = 0
    total_calls: int = 0
    total_successes: int = 0
    total_failures: int = 0
    last_success: str = ""
    last_failure: str = ""
    last_error: str = ""
    updated: str = field(default_factory=lambda: datetime.now().isoformat())


class SourceHealthTracker:
    """数据源健康追踪器

    线程安全，支持 JSONL 持久化。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._sources: dict[str, dict] = {}
        self._events: list[dict] = []  # 最近 1000 条事件

    def record(self, source: str, success: bool, latency_ms: float = 0, error: str = ""):
        now = time.time()
        event = {
            "time": datetime.now().isoformat(),
            "source": source,
            "success": success,
            "latency_ms": round(latency_ms, 1),
            "error": error[:200] if error else "",
        }

        with self._lock:
            if source not in self._sources:
                self._sources[source] = {
                    "total_calls": 0, "total_successes": 0, "total_failures": 0,
                    "recent_events": [], "last_success": "", "last_failure": "",
                    "last_error": "", "latencies": [],
                }

            s = self._sources[source]
            s["total_calls"] += 1
            s["recent_events"].append((now, success, latency_ms))
            s["latencies"].append(latency_ms)

            if success:
                s["total_successes"] += 1
                s["last_success"] = event["time"]
            else:
                s["total_failures"] += 1
                s["last_failure"] = event["time"]
                s["last_error"] = error[:200]

            # 只保留最近 10000 事件
            if len(s["recent_events"]) > 10000:
                s["recent_events"] = s["recent_events"][-5000:]
            if len(s["latencies"]) > 10000:
                s["latencies"] = s["latencies"][-5000:]

            self._events.append(event)
            if len(self._events) > 1000:
                self._events = self._events[-500:]

            # 每 100 次记录持久化一次
            if s["total_calls"] % 100 == 0:
                self._persist()

    def get_snapshot(self, source: str) -> HealthSnapshot:
        with self._lock:
            s = self._sources.get(source, {})
            if not s:
                return HealthSnapshot(source=source, status="unknown")

            now = time.time()
            recent = [(t, ok, lat) for t, ok, lat in s["recent_events"] if now - t < 3600]
            recent_24h = [(t, ok, lat) for t, ok, lat in s["recent_events"] if now - t < 86400]

            rate_1h = sum(1 for _, ok, _ in recent for _ in [1] if ok) / max(len(recent), 1)
            rate_24h = sum(1 for _, ok, _ in recent_24h for _ in [1] if ok) / max(len(recent_24h), 1)

            latencies = [lat for _, _, lat in recent if lat > 0]
            avg_lat = sum(latencies) / max(len(latencies), 1)

            if rate_1h < 0.5:
                status = "down"
            elif rate_1h < 0.9:
                status = "degraded"
            else:
                status = "ok"

            return HealthSnapshot(
                source=source,
                status=status,
                success_rate_1h=rate_1h,
                success_rate_24h=rate_24h,
                avg_latency_ms=avg_lat,
                total_calls=s["total_calls"],
                total_successes=s["total_successes"],
                total_failures=s["total_failures"],
                last_success=s["last_success"],
                last_failure=s["last_failure"],
                last_error=s["last_error"],
            )

    def get_all_snapshots(self) -> dict[str, HealthSnapshot]:
        return {name: self.get_snapshot(name) for name in self._sources}

    def get_summary(self) -> dict:
        snapshots = self.get_all_snapshots()
        result = {}
        for name, snap in snapshots.items():
            result[name] = {
                "status": snap.status,
                "success_rate_1h": f"{snap.success_rate_1h:.1%}",
                "avg_latency_ms": round(snap.avg_latency_ms, 1),
                "total_calls": snap.total_calls,
            }
        return result

    def _persist(self):
        try:
            os.makedirs(os.path.dirname(HEALTH_FILE), exist_ok=True)
            with open(HEALTH_FILE, "w") as f:
                for event in self._events[-100:]:
                    f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception:
            pass


# 全局实例
_global_tracker = SourceHealthTracker()


def record(source, success, latency_ms=0, error=""):
    _global_tracker.record(source, success, latency_ms, error)


def get_summary():
    return _global_tracker.get_summary()


def get_snapshot(source):
    return _global_tracker.get_snapshot(source)
