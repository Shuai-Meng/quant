"""交易时段任务调度器"""
import time
import logging
from datetime import datetime
from typing import Callable

from .market_hours import (
    now_cst, today_cst, market_status, is_trade_day,
    poll_interval, seconds_until, MORNING_OPEN,
    MORNING_CLOSE, AFTERNOON_OPEN, AFTERNOON_CLOSE,
)

log = logging.getLogger("quant.service")


class TaskScheduler:
    """基于A股交易时段的轻量级任务调度器

    不使用cron/Airflow等外部依赖，适合个人单机部署。
    """

    def __init__(self):
        self._tasks: dict[str, list[Callable]] = {
            "pre_market": [],
            "market_open": [],
            "market_close": [],
            "after_market": [],
            "daily": [],
            "intraday": [],
        }
        self._running = False
        self._last_trade_day = None
        self._last_status = None

    def on(self, event: str, callback: Callable):
        """注册事件回调

        Args:
            event: 事件名
                - 'pre_market': 盘前 (9:00-9:25)
                - 'market_open': 开盘 (9:30)
                - 'market_close': 收盘 (15:00)
                - 'after_market': 盘后 (15:00-16:00)
                - 'daily': 每日触发一次 (收盘后)
                - 'intraday': 盘中轮询 (交易时段每30s)
        """
        if event in self._tasks:
            self._tasks[event].append(callback)
        return self

    def stop(self):
        self._running = False

    def _trigger(self, event: str, **kwargs):
        for cb in self._tasks.get(event, []):
            try:
                cb(**kwargs)
            except Exception as e:
                log.error(f"Task [{event}] {cb.__name__} error: {e}")

    def run_forever(self):
        """启动调度循环（阻塞）"""
        self._running = True
        log.info("TaskScheduler started")

        while self._running:
            status = market_status()
            now = now_cst()

            # 交易日检测：每日盘前触发一次
            if is_trade_day(today_cst()):
                if self._last_trade_day != today_cst():
                    self._last_trade_day = today_cst()
                    log.info(f"交易日: {today_cst()}")

                    if status in ("pre_market", "morning_session"):
                        self._trigger("pre_market", date=now)

            # 状态切换触发
            if self._last_status != status:
                log.info(f"市场状态切换: {self._last_status} -> {status}")
                self._last_status = status

                if status == "morning_session":
                    self._trigger("market_open", date=now)
                elif status == "post_market" and is_trade_day(today_cst()):
                    self._trigger("market_close", date=now)
                    self._trigger("daily", date=now)

            # 盘中轮询
            if status in ("morning_session", "afternoon_session"):
                self._trigger("intraday", date=now, status=status)

            # 盘后任务
            if status == "post_market" and is_trade_day(today_cst()):
                self._trigger("after_market", date=now)

            # 动态休眠
            interval = poll_interval()
            time.sleep(interval)

    def run_once(self):
        """执行一次当前时段的任务（用于手动触发）"""
        status = market_status()
        now = now_cst()
        log.info(f"手动执行: status={status}")

        if status == "pre_market":
            self._trigger("pre_market", date=now)
        elif status in ("morning_session", "afternoon_session"):
            self._trigger("market_open", date=now)
            self._trigger("intraday", date=now, status=status)
        elif status == "post_market":
            self._trigger("market_close", date=now)
            self._trigger("daily", date=now)
            self._trigger("after_market", date=now)
