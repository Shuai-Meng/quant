"""A股交易时段管理"""
from datetime import datetime, time, timedelta
import pytz

CST = pytz.timezone("Asia/Shanghai")

MORNING_OPEN = time(9, 30)
MORNING_CLOSE = time(11, 30)
AFTERNOON_OPEN = time(13, 0)
AFTERNOON_CLOSE = time(15, 0)


def now_cst():
    return datetime.now(CST)


def today_cst():
    return now_cst().date()


def _is_weekday(dt):
    return dt.weekday() < 5


def is_trade_day(date=None):
    """判断是否为A股交易日（含日历查询）"""
    if date is None:
        date = today_cst()
    if not _is_weekday(date):
        return False
    from data.calendar import is_trade_day as _cal_is_trade
    return _cal_is_trade(date)


def market_status():
    """返回当前市场状态

    Returns:
        str: 'pre_market' | 'morning_session' | 'lunch_break' |
             'afternoon_session' | 'post_market' | 'holiday'
    """
    now = now_cst()

    if not _is_weekday(now):
        return "holiday"

    t = now.time()

    if t < MORNING_OPEN:
        return "pre_market"
    elif MORNING_OPEN <= t <= MORNING_CLOSE:
        return "morning_session"
    elif MORNING_CLOSE < t < AFTERNOON_OPEN:
        return "lunch_break"
    elif AFTERNOON_OPEN <= t <= AFTERNOON_CLOSE:
        return "afternoon_session"
    else:
        return "post_market"


def is_market_open():
    """市场是否正在交易"""
    return market_status() in ("morning_session", "afternoon_session")


def seconds_until(target_time):
    """距离下一个目标时间的秒数"""
    now = now_cst()
    target_dt = now.replace(hour=target_time.hour, minute=target_time.minute, second=0, microsecond=0)
    if target_dt <= now:
        target_dt += timedelta(days=1)
    return (target_dt - now).total_seconds()


def next_event():
    """返回下一个关键时间点及名称"""
    status = market_status()
    if status == "pre_market":
        return MORNING_OPEN, "开盘 (9:30)"
    elif status == "morning_session":
        return MORNING_CLOSE, "午休 (11:30)"
    elif status == "lunch_break":
        return AFTERNOON_OPEN, "下午开盘 (13:00)"
    elif status == "afternoon_session":
        return AFTERNOON_CLOSE, "收盘 (15:00)"
    else:
        return MORNING_OPEN, "下一个开盘 (9:30)"


def poll_interval():
    """根据当前时段返回推荐的轮询间隔（秒）"""
    status = market_status()
    if status in ("morning_session", "afternoon_session"):
        return 30
    elif status in ("pre_market", "lunch_break"):
        return 120
    else:
        return 300
