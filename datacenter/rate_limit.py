"""通达信行情服务器请求限速。

背景：全量导入会对 TDX 服务器发起数万次请求，若不做限速，
长时间高频请求容易被服务器限流甚至拉黑 IP。本模块用装饰器包装
pytdx TdxHq_API 的联网方法，统一做两层限速：

1. 相邻请求最小间隔（REQUEST_INTERVAL，秒）
2. 每分钟请求数滑动窗口上限（MAX_REQ_PER_MIN）

同时记录请求统计，供进度日志估算速率。
"""
import logging
import threading
import time
from collections import deque

logger = logging.getLogger("datacenter.rate_limit")

# 需要限速的 pytdx 联网方法白名单（其余如 connect/close 不受限）
_RATE_LIMITED_METHODS = frozenset(
    {
        "get_security_bars",
        "get_index_bars",
        "get_security_count",
        "get_security_list",
        "get_finance_info",
        "get_finance_info_byte",
        "get_xdxr_info",
        "get_stock_basic_info",
        "get_index_count",
        "get_index_list",
    }
)


class RateLimitedAPI:
    """对 pytdx API 做统一限速的透明包装。

    用法:
        raw = TdxHq_API(multithread=False)
        raw.connect(ip, port, time_out=3)
        api = RateLimitedAPI(raw, request_interval=0.4, max_req_per_min=120)
        # 之后所有 import_* 调用都传 api，联网请求自动限速
    """

    def __init__(self, api, request_interval: float = 0.4, max_req_per_min: int = 120):
        self._api = api
        self.request_interval = request_interval
        self.max_req_per_min = max_req_per_min
        self._lock = threading.Lock()
        self._last_req_time = 0.0
        self._recent = deque()  # 滑动窗口：最近请求的单调时钟时间戳
        self.req_count = 0
        self.total_wait = 0.0

    # ---- 限速核心 ----
    def _throttle(self) -> None:
        with self._lock:
            now = time.monotonic()

            # 1) 相邻请求最小间隔
            wait = self.request_interval - (now - self._last_req_time)
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
                self.total_wait += wait
            self._last_req_time = now

            # 2) 每分钟请求数滑动窗口
            while self._recent and now - self._recent[0] > 60.0:
                self._recent.popleft()
            if len(self._recent) >= self.max_req_per_min:
                # 阻塞到最早的请求滑出 60s 窗口
                need = self._recent[0] + 60.0 - now
                if need > 0:
                    time.sleep(need)
                    now = time.monotonic()
                    self.total_wait += need
                self._last_req_time = now
                while self._recent and now - self._recent[0] > 60.0:
                    self._recent.popleft()

            self._recent.append(now)
            self.req_count += 1

    # ---- 透明代理 ----
    def __getattr__(self, name: str):
        attr = getattr(self._api, name)
        if name in _RATE_LIMITED_METHODS and callable(attr):

            def wrapper(*args, **kwargs):
                self._throttle()
                return attr(*args, **kwargs)

            return wrapper
        return attr

    # ---- 透传连接生命周期 ----
    def connect(self, *args, **kwargs):
        return self._api.connect(*args, **kwargs)

    def close(self):
        try:
            self._api.close()
        except Exception:
            pass

    # ---- 统计 ----
    def stats(self) -> dict:
        with self._lock:
            return {
                "req_count": self.req_count,
                "total_wait": round(self.total_wait, 1),
                "avg_req_per_min": (
                    round(self.req_count / max(self.total_wait + 1, 1) * 60, 1)
                    if self.total_wait > 0
                    else 0
                ),
            }
