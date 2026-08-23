"""Python 标准库定时调度器：每日增量更新 + 每周缺口检测。

纯标准库实现（sched + subprocess），不依赖 crontab / systemd / IDE。
常驻后台运行，到点以子进程方式执行任务（进程隔离，子进程崩溃不影响调度器）：

    1) 每个交易日 17:30  执行 daily_update（子进程内自带交易日判断、进程锁、
       MySQL 日志与状态记录；非交易日自动秒退）
    2) 每周五 17:40      执行 verify --check-gaps --recent-days 14
       （全市场增量缺口健康检测：停机/漏拉会在当周暴露）

启动（前台调试）:
    .venv/bin/python -m datacenter.scheduler
启动（后台常驻）:
    nohup .venv/bin/python -m datacenter.scheduler >> data/logs/scheduler_stdout.log 2>&1 &
停止:
    pkill -f "datacenter.scheduler"
部署自检（立即跑一次 daily_update 后退出）:
    .venv/bin/python -m datacenter.scheduler --once
"""
import argparse
import datetime
import logging
import sched
import subprocess
import sys
import time
from pathlib import Path

from .config import LOG_DIR, QUANT_ROOT

RUN_HOUR, RUN_MINUTE = 17, 30        # 每个交易日 17:30 运行每日更新
VERIFY_HOUR, VERIFY_MINUTE = 17, 40  # 每周五 17:40 运行缺口检测
VERIFY_WEEKDAY = 4                   # 0=周一 ... 4=周五
VERIFY_RECENT_DAYS = 14              # 只查最近 14 天（忽略历史存量缺口噪声）
MAX_LOG_BYTES = 5 * 1024 * 1024      # 调度器日志 5MB 轮转

logger = logging.getLogger("datacenter.scheduler")


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / "scheduler.log"
    if log_file.exists() and log_file.stat().st_size > MAX_LOG_BYTES:
        log_file.replace(LOG_DIR / f"scheduler_{time.strftime('%Y%m%d_%H%M%S')}.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )


def next_run_dt(now: datetime.datetime | None = None) -> datetime.datetime:
    """下一次 RUN_HOUR:RUN_MINUTE 时刻；若今天已过则推到明天。"""
    now = now or datetime.datetime.now()
    nxt = now.replace(hour=RUN_HOUR, minute=RUN_MINUTE, second=0, microsecond=0)
    if nxt <= now:
        nxt += datetime.timedelta(days=1)
    return nxt


def next_verify_dt(now: datetime.datetime | None = None) -> datetime.datetime:
    """下一次周五 VERIFY_HOUR:VERIFY_MINUTE 时刻；今天已过/不是周五则顺延。"""
    now = now or datetime.datetime.now()
    nxt = now.replace(hour=VERIFY_HOUR, minute=VERIFY_MINUTE, second=0, microsecond=0)
    while nxt <= now or nxt.weekday() != VERIFY_WEEKDAY:
        nxt += datetime.timedelta(days=1)
    return nxt


def _run_subprocess(cmd: list[str], timeout_hours: int, tag: str) -> None:
    """通用子进程执行（隔离运行，捕获输出，滚动回显最近若干行）。"""
    logger.info("触发%s: %s", tag, " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(QUANT_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout_hours * 3600,
        )
        logger.info("%s 完成，退出码 %d", tag, proc.returncode)
        for line in (proc.stdout or "").splitlines()[-30:]:
            logger.info("[%s] %s", tag, line)
        for line in (proc.stderr or "").splitlines()[-10:]:
            logger.warning("[%s:stderr] %s", tag, line)
    except subprocess.TimeoutExpired:
        logger.error("%s 执行超时（>%dh），本次已终止", tag, timeout_hours)
    except Exception as e:
        logger.error("%s 执行异常: %s", tag, e)


def run_daily_update() -> None:
    """每日增量更新（内部自带交易日判断，非交易日秒退）。"""
    _run_subprocess(
        [sys.executable, "-m", "datacenter.daily_update"],
        timeout_hours=4,
        tag="daily",
    )


def run_verify_gaps() -> None:
    """每周缺口健康检测：全市场检查最近 N 天是否有新增缺口（漏拉/停机）。"""
    _run_subprocess(
        [sys.executable, "-m", "datacenter.verify",
         "--check-gaps", "--recent-days", str(VERIFY_RECENT_DAYS)],
        timeout_hours=2,
        tag="verify",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="每日增量更新调度器（Python 标准库 sched）")
    parser.add_argument("--once", action="store_true",
                        help="立即执行一次 daily_update 后退出（部署自检）")
    args = parser.parse_args()

    _setup_logging()
    if args.once:
        logger.info("自检模式：立即执行一次 daily_update")
        run_daily_update()
        logger.info("自检完成，退出")
        return

    logger.info(
        "定时调度器启动：每日 %02d:%02d daily_update；每周五 %02d:%02d 缺口检测 "
        "（子进程隔离，内部自带交易日判断）",
        RUN_HOUR, RUN_MINUTE, VERIFY_HOUR, VERIFY_MINUTE,
    )
    scheduler = sched.scheduler(time.time, time.sleep)

    def on_daily() -> None:
        run_daily_update()
        schedule_daily()  # 完成后安排下一次

    def on_verify() -> None:
        run_verify_gaps()
        schedule_verify()

    def _log_scheduled(what: str, nxt: datetime.datetime) -> None:
        delay = (nxt - datetime.datetime.now()).total_seconds()
        logger.info("已安排%s: %s（%.0f 分钟后）",
                    what, nxt.strftime("%Y-%m-%d %H:%M:%S"), delay / 60)

    def schedule_daily() -> None:
        nxt = next_run_dt()
        _log_scheduled("下一次每日更新", nxt)
        scheduler.enterabs(nxt.timestamp(), 1, on_daily)

    def schedule_verify() -> None:
        nxt = next_verify_dt()
        _log_scheduled("下一次缺口检测", nxt)
        scheduler.enterabs(nxt.timestamp(), 2, on_verify)

    schedule_daily()
    schedule_verify()
    try:
        scheduler.run()
    except KeyboardInterrupt:
        logger.info("收到中断信号，调度器退出")


if __name__ == "__main__":
    main()
