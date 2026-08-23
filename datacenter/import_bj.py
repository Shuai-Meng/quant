"""北交所日线补救导入：akshare(东财) → bj_day.h5

背景：pytdx 公共通达信行情服务器不提供北交所行情（实测多个服务器 BJ market=2
返回空），导致首次全量导入时 bj_day.h5 新增 0 条。本脚本改用 akshare 东财接口
逐只补齐北交所日线，写入与 SH/SZ 相同的 hikyuu H5Record 格式。

特性：
- 幂等增量：按 H5 表最后日期续拉，重跑自动跳过已完成的股票
- 限速分批：每只请求间隔 + 每批休息，避免被东财限流
- 失败重试：单只最多 3 次，连续失败熔断
- 完成后自动生成周/月线索引并更新 stock.db 起止日期、写 MySQL 导入日志

用法：
    python -m datacenter.import_bj                # 全量补齐北交所
    python -m datacenter.import_bj --limit 5      # 只处理前 5 只（冒烟测试）
"""
import argparse
import os
import datetime
import logging
import sqlite3
import sys
import time

from pathlib import Path

from .config import DATA_DIR, LOG_DIR, MYSQL_CONFIG

try:
    from hikyuu.data.common_h5 import H5Record, update_hdf5_extern_data
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "hikyuu"))
    from hikyuu.data.common_h5 import H5Record, update_hdf5_extern_data

import tables as tb

logger = logging.getLogger("datacenter.import_bj")

# 北交所 A 股类型（hikyuu STOCKTYPE.A_BJ=11）；排除指数(2)
BJ_STOCK_TYPES = (11,)

# akshare/东财限速（东财对高频请求有限流，需要比 pytdx 更克制）
AK_REQ_INTERVAL = 1.2       # 相邻两只股票的最小间隔（秒）
AK_BATCH_SIZE = 50          # 每批股票数
AK_BATCH_BREAK = 30         # 批间休息（秒）
AK_MAX_RETRY = 3            # 单只失败最大重试次数
AK_MAX_CONSECUTIVE_FAIL = 10  # 连续失败熔断阈值

_H5_FILE = DATA_DIR / "bj_day.h5"


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_DIR / "datacenter_bj.log", encoding="utf-8"),
        ],
    )


def get_bj_stocks(conn: sqlite3.Connection, limit: int = 0):
    """从 stock.db 读取北交所 A 股列表（按插入顺序，与 SH/SZ 全量一致）。"""
    sql = (
        "select stockid, code, valid, type from stock "
        f"where marketid=3 and type in ({','.join(str(t) for t in BJ_STOCK_TYPES)}) "
        "order by stockid"
    )
    stocks = conn.execute(sql).fetchall()
    if limit:
        stocks = stocks[:limit]
    return stocks


def open_bj_h5():
    _H5_FILE.parent.mkdir(parents=True, exist_ok=True)
    return tb.open_file(
        str(_H5_FILE), "a",
        filters=tb.Filters(complevel=9, complib="zlib", shuffle=True),
    )


def get_table(h5file, code):
    """获取/创建 /data/BJ{code} 表（与 hikyuu get_h5table 一致）。"""
    try:
        group = h5file.get_node("/", "data")
    except tb.NoSuchNodeError:
        group = h5file.create_group("/", "data")
    tablename = "BJ" + code
    try:
        return h5file.get_node(group, tablename)
    except tb.NoSuchNodeError:
        return h5file.create_table(group, tablename, H5Record)


def fetch_kline(api, code: str, start_date: str, end_date: str):
    """拉取日线（不复权）：东财优先，失败自动切换新浪兜底。

    统一返回列：datetime(如 202601010000) / 开盘 / 收盘 / 最高 / 最低 /
    成交量(手) / 成交额(元)，按日期升序。
    """
    try:
        return _fetch_em(api, code, start_date, end_date)
    except Exception as e:
        logger.warning("%s 东财不可用(%s)，切换新浪数据源", code, e)
        try:
            return _fetch_sina(api, code, start_date, end_date)
        except Exception as e2:
            logger.warning("%s 新浪也不可用: %s", code, e2)
            raise


def _fetch_em(api, code: str, start_date: str, end_date: str):
    """东财接口：成交量单位手，成交额单位元。"""
    df = api.stock_zh_a_hist(
        symbol=code, period="daily",
        start_date=start_date, end_date=end_date, adjust="",
    )
    if df is None or df.empty:
        raise RuntimeError("东财返回空数据")
    df = df.copy()
    df["datetime"] = df["日期"].apply(lambda d: int(pd_date(d).strftime("%Y%m%d")) * 10000)
    df = df.sort_values("datetime").reset_index(drop=True)
    return df


def _fetch_sina(api, code: str, start_date: str, end_date: str):
    """新浪接口：成交量单位股（转手），成交额单位元。"""
    df = api.stock_zh_a_daily(
        symbol="bj" + code,
        start_date=start_date, end_date=end_date, adjust="",
    )
    if df is None or df.empty:
        raise RuntimeError("新浪返回空数据")
    df = df.copy()
    df["datetime"] = df["date"].apply(lambda d: int(pd_date(d).strftime("%Y%m%d")) * 10000)
    df["开盘"] = df["open"]
    df["收盘"] = df["close"]
    df["最高"] = df["high"]
    df["最低"] = df["low"]
    df["成交量"] = (df["volume"] / 100.0).round()  # 股 → 手
    df["成交额"] = df["amount"]
    df = df.sort_values("datetime").reset_index(drop=True)
    return df


def pd_date(v):
    """统一把 akshare 的日期列转成 date 对象（兼容 str / Timestamp / date）。"""
    if isinstance(v, datetime.datetime):
        return v.date()
    if isinstance(v, datetime.date):
        return v
    return datetime.datetime.strptime(str(v)[:10], "%Y-%m-%d").date()


def write_kline(h5file, code: str, df) -> int:
    """把 akshare 的 DataFrame 写入 H5（价格×1000，成交额元→千元，量单位手）。"""
    table = get_table(h5file, code)
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
    return added, last_dt


def update_stock_dates(conn: sqlite3.Connection, stockid: int, table) -> None:
    """用 H5 表的实际起止日期更新 stock.db。"""
    if table.nrows == 0:
        return
    start = int(table[0]["datetime"]) // 10000
    end = int(table[-1]["datetime"]) // 10000
    conn.execute(
        "update stock set valid=1, startdate=%d, enddate=%d where stockid=%d"
        % (start, end, stockid)
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="北交所日线补救导入（akshare 东财 → bj_day.h5）")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 只（0=全部）")
    parser.add_argument("--sleep", type=float, default=AK_REQ_INTERVAL, help="相邻请求间隔秒数")
    args = parser.parse_args()

    _setup_logging()
    # 绕过失效的本地代理（pytdx 直连已证明本机可直连外网；
    # akshare/requests 会读取代理环境变量，残留的失效代理会导致 ProxyError）
    for key in ("http_proxy", "https_proxy", "all_proxy", "ftp_proxy",
                "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY"):
        os.environ.pop(key, None)
    import akshare as ak

    sqlite_path = DATA_DIR / "stock.db"
    conn = sqlite3.connect(sqlite_path)
    h5file = open_bj_h5()

    today = datetime.date.today()
    today_str = today.strftime("%Y%m%d")
    stocks = get_bj_stocks(conn, args.limit)
    logger.info(
        "北交所补救导入开始：共 %d 只（%s），请求间隔 %.1fs，每 %d 只休息 %ds",
        len(stocks), _H5_FILE, args.sleep, AK_BATCH_SIZE, AK_BATCH_BREAK,
    )

    started = time.time()
    added_total, failed_total, skipped_total = 0, 0, 0
    failed_list, consecutive_fail = [], 0
    finish_flag = False

    for i, (stockid, code, valid, stktype) in enumerate(stocks, 1):
        if finish_flag:
            break
        last_dt = 0
        try:
            table = get_table(h5file, code)
            if table.nrows > 0:
                last_dt = int(table[-1]["datetime"])
                if last_dt >= int(today_str + "0000"):
                    skipped_total += 1
                    logger.info("[%d/%d] %s 已是最新，跳过", i, len(stocks), code)
                    consecutive_fail = 0
                    time.sleep(args.sleep)
                    continue

            # 增量拉取：从 H5 最后日期的下一天开始
            start_date = (
                datetime.datetime.strptime(str(last_dt // 10000), "%Y%m%d").date()
                + datetime.timedelta(days=1)
            ).strftime("%Y%m%d") if last_dt else "19900101"

            df = None
            for attempt in range(1, AK_MAX_RETRY + 1):
                try:
                    df = fetch_kline(ak, code, start_date, today_str)
                    break
                except Exception as e:
                    wait = 3 * attempt
                    logger.warning(
                        "[%d/%d] %s 第 %d 次拉取失败: %s，%ds 后重试",
                        i, len(stocks), code, attempt, e, wait,
                    )
                    time.sleep(wait)

            if df is None:
                raise RuntimeError("akshare 拉取失败（重试 %d 次后仍失败）" % AK_MAX_RETRY)

            added, prev_dt = write_kline(h5file, code, df)
            if added:
                update_stock_dates(conn, stockid, get_table(h5file, code))
                conn.commit()
                update_hdf5_extern_data(h5file, "BJ" + code, "DAY")
                added_total += added
                consecutive_fail = 0
                logger.info(
                    "[%d/%d] %s 写入 %d 条（%s→%s）",
                    i, len(stocks), code, added,
                    prev_dt // 10000 if prev_dt else "-",
                    int(get_table(h5file, code)[-1]["datetime"]) // 10000,
                )
            else:
                skipped_total += 1
                consecutive_fail = 0
                logger.info("[%d/%d] %s 无新增（可能停牌或数据源无该股）", i, len(stocks), code)
        except Exception as e:
            failed_total += 1
            consecutive_fail += 1
            failed_list.append(code)
            logger.error("[%d/%d] %s 导入失败: %s", i, len(stocks), code, e)
            if consecutive_fail >= AK_MAX_CONSECUTIVE_FAIL:
                logger.error("连续失败 %d 只，熔断停止", consecutive_fail)
                finish_flag = True
                break

        if i % AK_BATCH_SIZE == 0 and not finish_flag:
            elapsed = time.time() - started
            rate = i / elapsed if elapsed else 0
            eta = (len(stocks) - i) / rate / 60 if rate else 0
            logger.info(
                "进度 %d/%d (%.1f%%) 新增 %d 失败 %d | 速率 %.2f 只/s 预计剩余 %.0f 分钟 | 批间休息 %ds",
                i, len(stocks), i / len(stocks) * 100, added_total, failed_total,
                rate, eta, AK_BATCH_BREAK,
            )
            time.sleep(AK_BATCH_BREAK)
        else:
            time.sleep(args.sleep)

    elapsed_sec = time.time() - started
    logger.info(
        "北交所日线导入完成: 新增 %d 条, 失败 %d, 跳过 %d, 耗时 %.1f 分钟",
        added_total, failed_total, skipped_total, elapsed_sec / 60,
    )
    if failed_list:
        logger.info("失败清单: %s", ",".join(failed_list))

    # 写 MySQL 导入日志（失败不阻塞主流程）
    try:
        from .mysql_db import log_import
        log_import(
            "full", "BJ", added_total, failed_total, elapsed_sec,
            {"source": "akshare", "limit": args.limit},
        )
    except Exception as e:
        logger.warning("MySQL 导入日志写入失败（已降级）: %s", e)

    h5file.close()
    conn.close()


if __name__ == "__main__":
    main()
