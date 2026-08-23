"""每日增量导入全 A 股日线数据（交易日收盘后由 scheduler.py 调度）。

链路设计（与首次全量保持一致、已验证）:
    - 代码表同步: hikyuu import_stock_name（SH/SZ/BJ 均走 akshare，自动增删改）
    - SH/SZ 日线 : pytdx 通达信（import_one_stock_data 按 H5 最后日期自动增量）
    - BJ 日线    : akshare 双数据源（东财优先，失败自动降级新浪；或 --source sina 直连新浪）
    - 幂等安全   : 重复运行/补数安全——每只股票都从「该股 H5 最后日期」与「上次成功
                   拉取日期」中较早者的下一天续拉，已是最新的直接跳过；服务器停机
                   数日/数周后重跑会自动补齐整个缺口，而不会只拉当天。

增量进度记录（本次需求核心）:
    state.json 的 last_data_date（按市场）记录最近一次确认成功更新到的数据日期，
    下次运行据此计算拉取窗口 [上次日期+1, 今天]。记录只在实际写入新数据时前进，
    且 --limit-stocks 冒烟模式不更新记录，避免误判全市场已更新。

交易日判断: 默认用 akshare 交易日历（新浪源）判断当天是否开市，
非交易日直接退出（不浪费请求）；--force 可跳过检查用于手动补数。

用法:
    python -m datacenter.daily_update                    # 常规每日更新（自动从上次日期续拉）
    python -m datacenter.daily_update --force            # 忽略交易日检查
    python -m datacenter.daily_update --markets SH,BJ    # 只更新指定市场
    python -m datacenter.daily_update --source sina      # BJ 直接用新浪（东财被限流时更快）
    python -m datacenter.daily_update --from-date 20260701  # 覆盖记录日期，从该日补拉到今天
    python -m datacenter.daily_update --limit-stocks 20  # 每市场只处理前 20 只（冒烟，不更新进度）
"""
import argparse
import datetime
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

from .config import (
    BATCH_BREAK,
    BATCH_SIZE,
    DATA_DIR,
    LOG_DIR,
    MARKETS,
    MAX_CONSECUTIVE_FAIL,
    STOCK_BREAK,
    STATE_FILE,
    TDX_MAX_REQ_PER_MIN,
    TDX_REQUEST_INTERVAL,
    TDX_TIMEOUT,
)
from .import_bj import (
    AK_BATCH_BREAK,
    AK_BATCH_SIZE,
    AK_MAX_RETRY,
    AK_REQ_INTERVAL,
    get_bj_stocks,
    _fetch_em,
    _fetch_sina,
    fetch_kline,
)
from .import_full import acquire_process_lock, connect_tdx, import_weight_and_finance

logger = logging.getLogger("datacenter.daily_update")

# BJ akshare 连续失败熔断阈值（import_bj 定义）
AK_MAX_CONSECUTIVE_FAIL = 10


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_DIR / "datacenter_daily.log", encoding="utf-8"),
        ],
    )


def _clean_proxy_env() -> None:
    """绕过失效的本地代理（akshare/requests 会读代理环境变量，残留失效代理导致 ProxyError）。"""
    for key in ("http_proxy", "https_proxy", "all_proxy", "ftp_proxy",
                "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY"):
        os.environ.pop(key, None)


def is_trade_date(d: datetime.date, force: bool = False) -> bool:
    """判断 d 是否 A 股交易日。akshare 交易日历失败时降级为工作日判断。"""
    if force:
        return True
    try:
        import akshare as ak
        df = ak.tool_trade_date_hist_sina()
        dates = set(str(v) for v in df["trade_date"].tolist())
        return d.isoformat() in dates
    except Exception as e:
        logger.warning("交易日历获取失败(%s)，降级为工作日判断", e)
        return d.weekday() < 5


def _add_days(dt_ymd: int, days: int) -> str:
    """int 日期（YYYYMMDD 或 YYYYMMDDHHMM）加 N 天 → 'YYYYMMDD' 字符串；入参 0/空返回 ''。"""
    if not dt_ymd:
        return ""
    d = datetime.datetime.strptime(str(int(dt_ymd))[:8], "%Y%m%d").date()
    return (d + datetime.timedelta(days=days)).strftime("%Y%m%d")


def _load_state() -> dict:
    """读取 state.json（不存在或损坏时返回空 dict）。"""
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("状态文件读取失败: %s", e)
    return {}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def sync_stock_names(api) -> None:
    """同步三市场股票/指数代码表（新上市自动加入，退市自动标记）。失败不阻塞主流程。"""
    from hikyuu.data.pytdx_to_h5 import import_index_name, import_stock_name
    sqlite_path = DATA_DIR / "stock.db"
    for market in MARKETS:
        try:
            conn = sqlite3.connect(sqlite_path)
            try:
                import_stock_name(conn, api, market)
            finally:
                conn.close()
            logger.info("已同步 %s 股票代码表", market)
        except Exception as e:
            logger.error("%s 股票代码表同步失败: %s", market, e)
    try:
        conn = sqlite3.connect(sqlite_path)
        try:
            import_index_name(conn)
        finally:
            conn.close()
        logger.info("已同步指数代码表")
    except Exception as e:
        logger.error("指数代码表同步失败: %s", e)


def update_tdx_market(api, market: str, args, from_date: str = "") -> dict:
    """SH/SZ 日线增量（pytdx）。

    增量起点由每只股票 H5 最后日期驱动（pytdx 服务器返回全量日K，等价于
    从上次数据日期续拉到今天）；from_date 为上次成功记录日期，仅用于日志与
    状态追踪。返回 {added, failed, skipped, elapsed_sec, max_data_date}。
    """
    from hikyuu.data.common_sqlite3 import get_stock_list
    from hikyuu.data.pytdx_to_h5 import import_one_stock_data, open_h5file, update_hdf5_extern_data

    today_str = datetime.date.today().strftime("%Y%m%d")
    sqlite_path = DATA_DIR / "stock.db"
    conn = sqlite3.connect(sqlite_path)
    try:
        stock_list = get_stock_list(conn, market, ("stock",))
    finally:
        conn.close()
    if args.limit_stocks:
        stock_list = stock_list[: args.limit_stocks]
    total = len(stock_list)
    logger.info("[%s] 日线增量开始：共 %d 只（pytdx 限速 %.1fs/次），窗口 [%s, %s]",
                market, total, TDX_REQUEST_INTERVAL, from_date or "各股H5末日后一天", today_str)
    if not total:
        return {"added": 0, "failed": 0, "skipped": 0, "elapsed_sec": 0.0, "max_data_date": 0}

    h5file = open_h5file(DATA_DIR, market, "DAY")
    added = failed = skipped = 0
    consec_fail = 0
    max_data_date = 0  # 本次确认写入到的最新数据日期（YYYYMMDD）
    started = time.time()
    try:
        for i, stock in enumerate(stock_list, 1):
            if stock[3] == 0 or len(stock[2]) != 6:
                skipped += 1
                continue
            conn = sqlite3.connect(sqlite_path)
            try:
                cnt, ok, last_dt_obj = import_one_stock_data(conn, api, h5file, market, "DAY", stock)
            finally:
                conn.close()
            if ok:
                consec_fail = 0
                if cnt > 0:
                    added += cnt
                    update_hdf5_extern_data(h5file, market.upper() + stock[2], "DAY")
                    n = int(getattr(last_dt_obj, "number", 0) or 0)
                    if n > max_data_date:
                        max_data_date = n
                else:
                    skipped += 1
            else:
                failed += 1
                consec_fail += 1
                logger.warning("[%s] %s 增量失败（第 %d/%d）", market, stock[2], i, total)
                if consec_fail >= MAX_CONSECUTIVE_FAIL:
                    logger.error("[%s] 连续失败 %d 只，疑似服务器异常，停止本市场", market, consec_fail)
                    break
            if STOCK_BREAK > 0:
                time.sleep(STOCK_BREAK)
            if i % BATCH_SIZE == 0:
                elapsed = time.time() - started
                rate = i / elapsed if elapsed else 0
                eta = (total - i) / rate / 60 if rate else 0
                logger.info(
                    "[%s] 进度 %d/%d (%.1f%%) 新增 %d 失败 %d | 速率 %.2f 只/s 预计剩余 %.0f 分钟 | 批间休息 %ds",
                    market, i, total, i / total * 100, added, failed, rate, eta, BATCH_BREAK,
                )
                time.sleep(BATCH_BREAK)
        elapsed = time.time() - started
        logger.info(
            "[%s] 日线增量完成: 新增 %d 条, 失败 %d, 跳过 %d, 耗时 %.1f 分钟",
            market, added, failed, skipped, elapsed / 60,
        )
        return {
            "added": added, "failed": failed, "skipped": skipped,
            "elapsed_sec": round(elapsed, 1),
            "max_data_date": max_data_date // 10000 if max_data_date else 0,
        }
    finally:
        h5file.close()


def build_bj_fetch(ak, source: str):
    """构造 BJ 单只拉取函数。source: auto(默认, 探测东财)/sina/em。"""
    if source == "sina":
        logger.info("BJ 数据源: 新浪（直接）")
        return lambda code, start, end: _fetch_sina(ak, code, start, end)
    if source == "em":
        logger.info("BJ 数据源: 东财（直接）")
        return lambda code, start, end: _fetch_em(ak, code, start, end)
    # auto: 探测东财一次，可用则东财优先+新浪兜底，否则本次全部直连新浪
    try:
        _fetch_em(ak, "920001", "20240801", "20240802")
        logger.info("东财可用，BJ 数据源: 东财优先 + 新浪兜底")
        return lambda code, start, end: fetch_kline(ak, code, start, end)
    except Exception as e:
        logger.warning("东财探测失败(%s)，BJ 本次直接使用新浪数据源", e)
        return lambda code, start, end: _fetch_sina(ak, code, start, end)


def get_table(h5file, market: str, code: str):
    """获取/创建 /data/{market}{code} 表（hikyuu H5Record 格式）。"""
    import tables as tb
    from hikyuu.data.common_h5 import H5Record
    try:
        group = h5file.get_node("/", "data")
    except tb.NoSuchNodeError:
        group = h5file.create_group("/", "data")
    tablename = market + code
    try:
        return h5file.get_node(group, tablename)
    except tb.NoSuchNodeError:
        return h5file.create_table(group, tablename, H5Record)


def write_kline(h5file, market: str, code: str, df) -> int:
    """把 akshare 的 DataFrame 写入 H5（价格×1000，成交额元→千元，量单位手）。"""
    table = get_table(h5file, market, code)
    last_dt = int(table[-1]["datetime"]) if table.nrows > 0 else 0
    added = 0
    row = table.row
    for rec in df.itertuples(index=False):
        dt = int(rec.datetime)
        if dt <= last_dt:
            continue
        row["datetime"] = dt
        row["openPrice"] = round(float(rec.开盘) * 1000)
        row["highPrice"] = round(float(rec.最高) * 1000)
        row["lowPrice"] = round(float(rec.最低) * 1000)
        row["closePrice"] = round(float(rec.收盘) * 1000)
        row["transAmount"] = round(float(rec.成交额) * 0.001)
        row["transCount"] = round(float(rec.成交量))
        row.append()
        added += 1
    if added:
        table.flush()
    return added


def update_bj_market(ak, args, today_str: str, from_date: str = "") -> dict:
    """BJ 日线增量（akshare 双数据源）。

    拉取窗口 = [min(该股 H5 最后日期, 上次成功记录日期) + 1, today]：
    - 两者都存在时取较早者，保证停机数日后自动补齐缺口、且不漏任何一天；
    - 一方为 0（新股 H5 空 / 尚无记录）时取另一方；
    - 皆无（新股且首次）从 19900101 起拉，akshare 只会返回该股真实存在的 K 线。
    返回 {added, failed, skipped, elapsed_sec, max_data_date}。
    """
    import tables as tb
    from hikyuu.data.common_h5 import update_hdf5_extern_data

    h5_path = DATA_DIR / "bj_day.h5"
    sqlite_path = DATA_DIR / "stock.db"
    h5file = tb.open_file(
        str(h5_path), "a",
        filters=tb.Filters(complevel=9, complib="zlib", shuffle=True),
    )
    conn = sqlite3.connect(sqlite_path)
    fetch = build_bj_fetch(ak, args.source)
    stocks = get_bj_stocks(conn, args.limit_stocks)
    total = len(stocks)
    from_dt = int(from_date) if from_date else 0
    max_data_date = 0  # 本次确认写入到的最新数据日期（YYYYMMDD）
    logger.info(
        "[BJ] 日线增量开始：共 %d 只（akshare，间隔 %.1fs/只，每 %d 只休息 %ds），窗口 [%s, %s]",
        total, args.sleep, AK_BATCH_SIZE, AK_BATCH_BREAK,
        _add_days(from_dt, 1) or "各股H5末日后一天", today_str,
    )
    added_total = failed_total = skipped_total = 0
    failed_list, consec_fail, started, finish_flag = [], 0, time.time(), False
    for i, (stockid, code, valid, stktype) in enumerate(stocks, 1):
        if finish_flag:
            break
        try:
            table = get_table(h5file, "BJ", code)
            last_dt = int(table[-1]["datetime"]) if table.nrows > 0 else 0
            if last_dt >= int(today_str + "0000"):
                skipped_total += 1
                consec_fail = 0
                time.sleep(args.sleep)
                continue
            # 拉取起点 = min(该股 H5 最后日期, 上次成功记录日期) + 1 天
            h5_last = last_dt // 10000 if last_dt else 0
            base_dt = min(h5_last, from_dt) if (h5_last and from_dt) else (h5_last or from_dt)
            start_date = _add_days(base_dt, 1) or "19900101"
            df, last_err = None, ""
            for attempt in range(1, AK_MAX_RETRY + 1):
                try:
                    df = fetch(code, start_date, today_str)
                    break
                except Exception as e:
                    last_err = str(e)
                    if "空数据" in last_err:
                        # 窗口内无交易（停牌/当日无数据），不算失败，不重试
                        break
                    logger.warning("[%d/%d] %s 第 %d 次拉取失败: %s，%ds 后重试",
                                   i, total, code, attempt, e, 3 * attempt)
                    time.sleep(3 * attempt)
            if df is None and "空数据" in last_err:
                logger.info("[%d/%d] %s 窗口内无交易（停牌或数据未更新），跳过", i, total, code)
                skipped_total += 1
                consec_fail = 0
                time.sleep(args.sleep)
                continue
            if df is None:
                raise RuntimeError("akshare 拉取失败（重试 %d 次后仍失败）" % AK_MAX_RETRY)
            added = write_kline(h5file, "BJ", code, df)
            if added:
                table = get_table(h5file, "BJ", code)
                if table.nrows > 0:
                    conn.execute(
                        "update stock set valid=1, startdate=%d, enddate=%d where stockid=%d"
                        % (int(table[0]["datetime"]) // 10000,
                           int(table[-1]["datetime"]) // 10000, stockid)
                    )
                    conn.commit()
                    cur_dt = int(table[-1]["datetime"]) // 10000
                    if cur_dt > max_data_date:
                        max_data_date = cur_dt
                update_hdf5_extern_data(h5file, "BJ" + code, "DAY")
                added_total += added
                consec_fail = 0
                logger.info("[%d/%d] %s 写入 %d 条（→%s）", i, total, code, added,
                            int(get_table(h5file, "BJ", code)[-1]["datetime"]) // 10000)
            else:
                skipped_total += 1
                consec_fail = 0
        except Exception as e:
            failed_total += 1
            consec_fail += 1
            failed_list.append(code)
            logger.error("[%d/%d] %s 导入失败: %s", i, total, code, e)
            if consec_fail >= AK_MAX_CONSECUTIVE_FAIL:
                logger.error("[BJ] 连续失败 %d 只，熔断停止", consec_fail)
                finish_flag = True
                break
        if i % AK_BATCH_SIZE == 0 and not finish_flag:
            elapsed = time.time() - started
            rate = i / elapsed if elapsed else 0
            eta = (total - i) / rate / 60 if rate else 0
            logger.info(
                "进度 %d/%d (%.1f%%) 新增 %d 失败 %d | 速率 %.2f 只/s 预计剩余 %.0f 分钟 | 批间休息 %ds",
                i, total, i / total * 100, added_total, failed_total, rate, eta, AK_BATCH_BREAK,
            )
            time.sleep(AK_BATCH_BREAK)
        else:
            time.sleep(args.sleep)
    elapsed_sec = time.time() - started
    logger.info(
        "[BJ] 日线增量结束: 新增 %d 条, 失败 %d, 跳过 %d, 耗时 %.1f 分钟",
        added_total, failed_total, skipped_total, elapsed_sec / 60,
    )
    if failed_list:
        logger.info("失败清单: %s", ",".join(failed_list))
    h5file.close()
    conn.close()
    return {
        "added": added_total, "failed": failed_total, "skipped": skipped_total,
        "elapsed_sec": round(elapsed_sec, 1),
        "max_data_date": max_data_date,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="每日增量导入全 A 股日线数据")
    parser.add_argument("--markets", type=str, default=",".join(MARKETS), help="要更新的市场，逗号分隔")
    parser.add_argument("--force", action="store_true", help="跳过交易日检查（手动补数）")
    parser.add_argument("--skip-names", action="store_true", help="跳过股票代码表同步")
    parser.add_argument("--skip-weight", action="store_true", help="跳过权息数据更新（SH/SZ）")
    parser.add_argument("--limit-stocks", type=int, default=0, help="每市场最多处理 N 只（0=不限）")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="pytdx 每批股票数")
    parser.add_argument("--batch-break", type=int, default=BATCH_BREAK, help="pytdx 批间休息秒数")
    parser.add_argument("--source", type=str, default="auto", choices=["auto", "sina", "em"],
                        help="BJ 数据源: auto=探测东财(默认), sina=直连新浪, em=直连东财")
    parser.add_argument("--sleep", type=float, default=AK_REQ_INTERVAL, help="BJ 相邻请求间隔秒数")
    parser.add_argument("--from-date", type=str, default="", metavar="YYYYMMDD",
                        help="覆盖上次成功记录日期，从该日期补拉到今天（手动补数；SH/SZ 仍按各股 H5 增量）")
    args = parser.parse_args()

    _setup_logging()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    markets = [m.upper() for m in args.markets.split(",") if m.strip().upper() in MARKETS]
    if not markets:
        logger.error("无效市场列表: %s", args.markets)
        sys.exit(1)
    if not (DATA_DIR / "stock.db").exists():
        logger.error("stock.db 不存在，请先执行全量导入（python -m datacenter.import_full）")
        sys.exit(1)

    today = datetime.date.today()
    today_str = today.strftime("%Y%m%d")
    if not is_trade_date(today, args.force):
        logger.info("%s 非交易日，退出（--force 可强制更新）", today_str)
        return

    # 读取上次成功拉取日期（按市场），作为本次拉取窗口起点
    state = _load_state()
    last_data_date = state.get("last_data_date") or {}

    def market_from_date(market: str) -> str:
        """本次该市场的拉取起点日期：--from-date 优先，否则用上次成功记录。"""
        if args.from_date:
            return args.from_date
        return last_data_date.get(market, "")

    if args.from_date:
        logger.info("手动指定 --from-date %s，将覆盖上次记录从该日期补拉到 %s", args.from_date, today_str)
    else:
        logger.info("上次成功数据日期（按市场）: %s", last_data_date or "无（首次运行，按 H5 末日后一天续拉）")

    _clean_proxy_env()
    import akshare as ak

    acquire_process_lock()
    started = time.time()
    stats = {}

    # 1) 连接 pytdx（SH/SZ 需要）；失败则跳过 SH/SZ，仅做 BJ
    api = None
    try:
        api, host = connect_tdx()
        logger.info("通达信连接成功: %s", host)
    except Exception as e:
        logger.error("通达信连接失败(%s)，本次跳过 SH/SZ，仅更新 BJ", e)

    try:
        # 2) 代码表同步（akshare 走通，不依赖 pytdx 北交所支持）
        if not args.skip_names:
            sync_stock_names(api)
            logger.info("股票/指数代码表同步完成")

        # 3) SH/SZ 日线增量（pytdx，按各股 H5 最后日期自动续拉）
        if api is not None:
            for market in markets:
                if market == "BJ":
                    continue
                stats[market] = update_tdx_market(api, market, args, market_from_date(market))
            # 权息数据（前复权基准，pytdx 链路；BJ 不支持会自动跳过并记日志）
            if not args.skip_weight and not args.limit_stocks:
                try:
                    import_weight_and_finance(api)
                except Exception as e:
                    logger.error("权息数据更新失败: %s", e)
        else:
            for market in markets:
                if market != "BJ":
                    stats[market] = {"added": 0, "failed": -1, "skipped": 0,
                                     "elapsed_sec": 0.0, "error": "pytdx 连接失败"}
    finally:
        if api is not None:
            api.close()

    # 4) BJ 日线增量（akshare，窗口 = [min(各股H5末, 上次记录) + 1, today]）
    if "BJ" in markets:
        stats["BJ"] = update_bj_market(ak, args, today_str, market_from_date("BJ"))

    # 5) MySQL 导入日志
    from .mysql_db import log_import
    for market, st in stats.items():
        if st.get("failed") == -1:
            logger.warning("[%s] 本次未更新（pytdx 不可用）", market)
            continue
        try:
            log_import("daily", market, st["added"], st["failed"], st["elapsed_sec"],
                       {"date": today_str, "source": args.source,
                        "markets": markets, "limit_stocks": args.limit_stocks,
                        "from_date": market_from_date(market)})
        except Exception as e:
            logger.warning("MySQL 导入日志写入失败（已降级）: %s", e)

    # 6) 状态文件：last_data_date 记录本次确认成功拉取到的数据日期（按市场，只前进）
    #    - 仅在实际写入新数据时更新（max_data_date>0 才可能前进），停牌/全部失败不误判
    #    - 冒烟模式（--limit-stocks）只跑部分股票，不更新该记录，避免误认为全市场已更新
    if not args.limit_stocks:
        for market, st in stats.items():
            if not st or st.get("failed") == -1:
                continue
            new_d = st.get("max_data_date")
            if not new_d:
                continue
            old_d = last_data_date.get(market, "")
            if not old_d or str(new_d) > str(old_d):
                last_data_date[market] = str(new_d)
        state["last_data_date"] = last_data_date
    state["last_daily_update"] = time.strftime("%Y-%m-%d %H:%M:%S")
    state["date"] = today_str
    state["daily"] = {m: {"added": s["added"], "failed": s["failed"],
                          "skipped": s.get("skipped", 0)} for m, s in stats.items()}
    _save_state(state)

    logger.info("每日增量更新完成，总耗时 %.1f 分钟", (time.time() - started) / 60)


if __name__ == "__main__":
    main()
