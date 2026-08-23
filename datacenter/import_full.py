"""首次全量导入 A 股全市场日线数据（hikyuu + pytdx/通达信）。

流程:
    1. 连接通达信行情服务器（自动挑选延迟最低的，全请求限速）
    2. 建/升级 stock.db 结构
    3. 导入股票代码表（沪深北 + 指数，基于 akshare）
    4. 全市场日线 K 线 -> HDF5（sh_day.h5 / sz_day.h5 / bj_day.h5）
       —— 分批拉取 + 请求限速 + 批间休息，避免被服务器限流/拉黑 IP
    5. 导入权息数据（前复权基准）
    6. 导入财务数据（可选）

防封 IP 设计:
    - 所有 pytdx 请求经 RateLimitedAPI 统一限速（间隔 0.4s + 120 次/分钟）
    - 每 BATCH_SIZE(200) 只股票后休息 BATCH_BREAK(20s)
    - 支持 --limit-stocks 限定单次处理量，配合 H5 增量幂等实现"小步快跑"断点续传
    - 进程文件锁防止多实例并发写 H5（并发会文件锁冲突）

用法:
    python -m datacenter.import_full            # 全量导入
    python -m datacenter.import_full --smoke    # 冒烟测试：只导少量股票验证链路
    python -m datacenter.import_full --markets SH,SZ   # 只导指定市场
    python -m datacenter.import_full --limit-stocks 500   # 本次只导 500 只，下次再跑剩余
"""
import argparse
import fcntl
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path

from .config import (
    BATCH_BREAK,
    BATCH_SIZE,
    DATA_DIR,
    DEFAULT_START_DATE,
    LOG_DIR,
    MARKETS,
    MAX_CONSECUTIVE_FAIL,
    STOCK_BREAK,
    STATE_FILE,
    TDX_MAX_REQ_PER_MIN,
    TDX_REQUEST_INTERVAL,
    TDX_TIMEOUT,
)
from .rate_limit import RateLimitedAPI

logger = logging.getLogger("datacenter.import_full")

LOCK_FILE = Path("/tmp/datacenter_import.lock")


def acquire_process_lock() -> None:
    """获取进程锁，防止多个导入实例并发写 H5（会文件锁冲突）。

    拿不到锁直接退出，避免像之前双进程互相踩踏。
    """
    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.error("已有另一个导入进程在运行（%s 被占用），本次退出", LOCK_FILE)
        sys.exit(1)
    logger.info("已获取进程锁 %s", LOCK_FILE)

logger = logging.getLogger("datacenter.import_full")


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_DIR / "datacenter_import.log", encoding="utf-8"),
        ],
    )


def connect_tdx(max_try: int = 3):
    """连接延迟最低的通达信服务器，返回 (限速包装后的 api, host)。失败时抛异常。

    hikyuu 的 search_best_tdx() 返回按延迟排序的候选列表，
    每个元素是 (success, elapsed, ip, port) 四元组。
    返回的 api 已用 RateLimitedAPI 包装，所有联网请求自动限速。
    """
    from hikyuu.data.common_pytdx import search_best_tdx
    from pytdx.hq import TdxHq_API

    for attempt in range(1, max_try + 1):
        logger.info("第 %d 次探测通达信服务器...", attempt)
        try:
            candidates = search_best_tdx()
            for ok, elapsed, ip, port in candidates:
                if not ok:
                    continue
                raw = TdxHq_API(multithread=False)
                if raw.connect(ip, port, time_out=TDX_TIMEOUT):
                    logger.info(
                        "选中服务器 %s:%s (延迟 %.2fs)，请求限速 %.1fs/次 上限 %d/分钟",
                        ip, port, elapsed, TDX_REQUEST_INTERVAL, TDX_MAX_REQ_PER_MIN,
                    )
                    return RateLimitedAPI(raw, TDX_REQUEST_INTERVAL, TDX_MAX_REQ_PER_MIN), ip
                raw.close()
        except Exception as e:
            logger.warning("探测异常: %s", e)
    raise RuntimeError("无法连接任何通达信服务器")


def import_names(connect, api) -> None:
    """导入股票/指数代码表（新上市自动加入，退市自动标记）。"""
    from hikyuu.data.pytdx_to_h5 import import_index_name, import_stock_name

    for market in MARKETS:
        try:
            import_stock_name(connect, api, market)
            logger.info("已导入 %s 股票代码表", market)
        except Exception as e:
            logger.error("%s 股票代码表导入失败: %s", market, e)
    try:
        import_index_name(connect)
        logger.info("已导入指数代码表")
    except Exception as e:
        logger.error("指数代码表导入失败: %s", e)


def import_kdata(api, markets, start_date=DEFAULT_START_DATE, limit_stocks=None,
                 batch_size=BATCH_SIZE, batch_break=BATCH_BREAK) -> None:
    """全市场日线导入：分批 + 限速 + 进度/ETA + 失败熔断。

    不直接用 hikyuu 的 import_data（其内部无限速、无分批、连续失败 20 只即整体退出），
    而是逐只调用 import_one_stock_data（内部按 H5 最后日期自动增量，重复运行安全，
    中断后重跑自动跳过已完成的股票）。

    限速节奏:
        - 每个联网请求之间 >= TDX_REQUEST_INTERVAL（由 RateLimitedAPI 保证）
        - 每分钟请求数 <= TDX_MAX_REQ_PER_MIN（滑动窗口）
        - 每只股票后休息 STOCK_BREAK
        - 每 BATCH_SIZE 只股票后休息 BATCH_BREAK，并打印进度/速率/ETA
        - 连续失败 >= MAX_CONSECUTIVE_FAIL 停止本市场
    """
    from hikyuu.data.common_sqlite3 import get_stock_list
    from hikyuu.data.pytdx_to_h5 import import_one_stock_data, open_h5file, update_hdf5_extern_data

    sqlite_path = DATA_DIR / "stock.db"
    stats = {}
    for market in markets:
        conn = sqlite3.connect(sqlite_path)
        stock_list = get_stock_list(conn, market, ("stock",))
        conn.close()
        if not stock_list:
            logger.warning("[%s] 股票列表为空，跳过", market)
            stats[market] = {"added": 0, "failed": 0, "skipped": 0, "elapsed_sec": 0.0}
            continue
        if limit_stocks:
            stock_list = stock_list[:limit_stocks]

        total = len(stock_list)
        logger.info(
            "[%s] 开始日线导入，共 %d 只股票（限速 %.1fs/请求, %d 次/分钟, 每 %d 只休息 %ds）",
            market, total, TDX_REQUEST_INTERVAL, TDX_MAX_REQ_PER_MIN, batch_size, batch_break,
        )
        h5file = open_h5file(DATA_DIR, market, "DAY")
        added = skipped = failed = 0
        consec_fail = 0
        started = time.time()
        try:
            for i, stock in enumerate(stock_list):
                # stock: (stockid, marketid, code, valid, type)；valid=0 或代码非 6 位则跳过
                if stock[3] == 0 or len(stock[2]) != 6:
                    skipped += 1
                    continue

                conn = sqlite3.connect(sqlite_path)
                try:
                    cnt, ok, _ = import_one_stock_data(conn, api, h5file, market, "DAY", stock, start_date)
                finally:
                    conn.close()

                if ok:
                    added += cnt
                    consec_fail = 0
                    if cnt > 0:
                        # 更新周/月线索引（纯本地 H5 计算，不联网，不受限速）
                        update_hdf5_extern_data(h5file, market.upper() + stock[2], "DAY")
                else:
                    failed += 1
                    consec_fail += 1
                    logger.warning("[%s] %s 导入失败（第 %d/%d）", market, stock[2], i + 1, total)
                    if consec_fail >= MAX_CONSECUTIVE_FAIL:
                        logger.error("[%s] 连续失败 %d 只，疑似服务器异常，停止本市场",
                                     market, consec_fail)
                        break

                if STOCK_BREAK > 0:
                    time.sleep(STOCK_BREAK)

                # 批次进度 + 批间休息
                if (i + 1) % batch_size == 0:
                    elapsed = time.time() - started
                    rate = (i + 1) / elapsed if elapsed > 0 else 0
                    eta_min = (total - i - 1) / rate / 60 if rate > 0 else 0
                    s = api.stats()
                    logger.info(
                        "[%s] 进度 %d/%d (%.1f%%) 新增 %d 失败 %d | 速率 %.1f 只/s 预计剩余 %.0f 分钟"
                        " | 请求 %d 次 限速等待 %.0fs | 批间休息 %ds",
                        market, i + 1, total, (i + 1) / total * 100, added, failed,
                        rate, eta_min, s["req_count"], s["total_wait"], batch_break,
                    )
                    time.sleep(batch_break)

            elapsed = time.time() - started
            stats[market] = {
                "added": added, "failed": failed, "skipped": skipped,
                "elapsed_sec": round(elapsed, 1),
            }
            logger.info(
                "[%s] 日线导入完成: 新增 %d 条, 失败 %d, 跳过 %d, 耗时 %.1f 分钟, 请求 %d 次",
                market, added, failed, skipped, elapsed / 60, api.stats()["req_count"],
            )
        finally:
            h5file.close()
    return stats


def import_weight_and_finance(api) -> None:
    """导入权息数据（前复权/后复权计算基准）与财务数据。"""
    sqlite_path = DATA_DIR / "stock.db"
    for market in MARKETS:
        conn = sqlite3.connect(sqlite_path)
        try:
            try:
                from hikyuu.data.pytdx_weight_to_sqlite import pytdx_import_weight_to_sqlite

                pytdx_import_weight_to_sqlite(api, conn, market)
                conn.commit()
                logger.info("已导入 %s 权息数据", market)
            except Exception as e:
                logger.error("%s 权息数据导入失败: %s", market, e)
        finally:
            conn.close()


def smoke_test(api, sqlite_path) -> None:
    """冒烟测试：各市场导 1 只股票，验证 pytdx 连接 + HDF5 写入链路。"""
    import sqlite3

    from hikyuu.data.pytdx_to_h5 import import_one_stock_data, open_h5file

    samples = {"SH": "600000", "SZ": "000001", "BJ": "830799"}
    market_id = {"SH": 1, "SZ": 2, "BJ": 3}  # hikyuu stock 表 marketid 定义
    for market, code in samples.items():
        try:
            conn = sqlite3.connect(sqlite_path)
            row = conn.execute(
                "SELECT stockid, marketid, code, valid, type FROM stock WHERE marketid=? AND code=?",
                (market_id[market], code),
            ).fetchone()
            conn.close()
            if not row:
                logger.warning("跳过 %s%s: 不在代码表（先跑 import_names）", market, code)
                continue
            h5file = open_h5file(DATA_DIR, market, "DAY")
            conn2 = sqlite3.connect(sqlite_path)
            try:
                cnt, ok, _ = import_one_stock_data(conn2, api, h5file, market, "DAY", row)
                logger.info("冒烟 %s%s: 写入 %d 条, ok=%s", market, code, cnt, ok)
            finally:
                conn2.close()
                h5file.close()
        except Exception as e:
            logger.error("冒烟 %s%s 失败: %s", market, code, e)


def main() -> None:
    parser = argparse.ArgumentParser(description="首次全量导入 A 股日线数据")
    parser.add_argument("--smoke", action="store_true", help="冒烟测试（只导少量股票）")
    parser.add_argument("--markets", type=str, default=",".join(MARKETS), help="要导入的市场，逗号分隔")
    parser.add_argument("--skip-names", action="store_true", help="跳过股票代码表导入")
    parser.add_argument("--skip-weight", action="store_true", help="跳过权息数据导入")
    parser.add_argument("--limit-stocks", type=int, default=0,
                        help="本次最多处理 N 只股票（0=不限），配合 H5 增量实现分批续传")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="每批股票数")
    parser.add_argument("--batch-break", type=int, default=BATCH_BREAK, help="批间休息秒数")
    args = parser.parse_args()

    _setup_logging()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    markets = [m.upper() for m in args.markets.split(",") if m.strip().upper() in MARKETS]
    if not markets:
        logger.error("无效市场列表: %s", args.markets)
        sys.exit(1)

    sqlite_path = DATA_DIR / "stock.db"
    if not sqlite_path.exists():
        logger.error("stock.db 不存在，请先执行: python -m datacenter.init")
        sys.exit(1)

    acquire_process_lock()
    api, host = connect_tdx()
    started = time.time()
    try:
        conn = sqlite3.connect(sqlite_path)
        from hikyuu.data.pytdx_to_h5 import create_database

        create_database(conn)
        conn.commit()
        conn.close()

        if not args.skip_names:
            conn = sqlite3.connect(sqlite_path)
            import_names(conn, api)
            conn.close()

        if args.smoke:
            smoke_test(api, sqlite_path)
            return

        kdata_stats = import_kdata(
            api, markets,
            start_date=DEFAULT_START_DATE,
            limit_stocks=args.limit_stocks or None,
            batch_size=args.batch_size,
            batch_break=args.batch_break,
        )
        if not args.skip_weight:
            import_weight_and_finance(api)

        # 写 MySQL 导入日志（MySQL 不可用时自动降级，不影响主流程）
        from .mysql_db import log_import

        for market, st in kdata_stats.items():
            log_import("full", market, st["added"], st["failed"], st["elapsed_sec"],
                       {"server": host, "markets": markets, "limit_stocks": args.limit_stocks})

        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "last_full_import": time.strftime("%Y-%m-%d %H:%M:%S"),
            "server": host,
            "markets": markets,
            "limit_stocks": args.limit_stocks,
            "elapsed_sec": round(time.time() - started, 1),
        }
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("全量导入完成，耗时 %.1f 秒", time.time() - started)
    finally:
        api.close()


if __name__ == "__main__":
    main()
