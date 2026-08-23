"""存量缺口修复：对 verify --check-gaps 报告的缺口标的，用 akshare(新浪优先) 补拉。

数据来源：重新执行 check_gaps()（对照 A 股交易日历）得到逐股缺口区间；
对每只标的取其全部缺口区间的 [最早起点, 最晚终点] 一次拉取，write_kline 幂等合并：
    - 真实漏拉（如 2007-09-05 系统性缺日）→ 新浪能拉到该日，写入后缺口消失
    - 停牌日（如 2015-07 千股停牌）→ akshare 返回无该日行情，自然跳过
    - 数据源本身缺失的历史（1990s 早期部分标的）→ 拉不到，保持原样（下次校验仍报告）

个股走 stock_zh_a_daily（新浪，东财被限流时的稳定通道）；
指数（SH 000xxx / SZ 399xxx）走 stock_zh_index_daily。

用法:
    python -m datacenter.repair_gaps                    # 全市场修复全部缺口标的
    python -m datacenter.repair_gaps --limit-stocks 50  # 只修前 50 只（冒烟）
    python -m datacenter.repair_gaps --markets SH,BJ    # 只修指定市场
    python -m datacenter.repair_gaps --sleep 2.0        # 调整限速
"""
import argparse
import datetime
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

from .config import DATA_DIR, LOG_DIR

try:
    from hikyuu.data.common_h5 import H5Record, update_hdf5_extern_data
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "hikyuu"))
    from hikyuu.data.common_h5 import H5Record, update_hdf5_extern_data

import tables as tb

logger = logging.getLogger("datacenter.repair_gaps")

AK_REQ_INTERVAL = 1.2
AK_BATCH_SIZE = 50
AK_BATCH_BREAK = 30
AK_MAX_RETRY = 3
AK_MAX_CONSECUTIVE_FAIL = 10

H5_FILES = {m: DATA_DIR / f"{m.lower()}_day.h5" for m in ("SH", "SZ", "BJ")}


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_DIR / "repair_gaps.log", encoding="utf-8"),
        ],
    )


def _clean_proxy_env() -> None:
    for key in ("http_proxy", "https_proxy", "all_proxy", "ftp_proxy",
                "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY"):
        os.environ.pop(key, None)


def _pd_date(v):
    """统一把 akshare 的日期列转成 date 对象（兼容 date/str/int，多格式）。"""
    if isinstance(v, datetime.datetime):
        return v.date()
    if isinstance(v, datetime.date):
        return v
    s = str(v)[:10]
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return datetime.datetime.strptime(s, "%Y-%m-%d").date()


def _add_days(dt_ymd: int, days: int) -> str:
    if not dt_ymd:
        return ""
    d = datetime.datetime.strptime(str(int(dt_ymd))[:8], "%Y%m%d").date()
    return (d + datetime.timedelta(days=days)).strftime("%Y%m%d")


def is_index(market: str, code: str) -> bool:
    """指数判定：SH 000xxx（上证系列）；SZ 399xxx（深证系列）；BJ 899xxx（北证系列）。"""
    return (
        (market == "SH" and code.startswith("000"))
        or (market == "SZ" and code.startswith("399"))
        or (market == "BJ" and code.startswith("899"))
    )


def get_table(h5file, market: str, code: str):
    """获取/创建 /data/{market}{code} 表。"""
    try:
        group = h5file.get_node("/", "data")
    except tb.NoSuchNodeError:
        group = h5file.create_group("/", "data")
    tablename = market + code
    try:
        return h5file.get_node(group, tablename)
    except tb.NoSuchNodeError:
        return h5file.create_table(group, tablename, H5Record)


def fetch_daily(ak, market: str, code: str, start_date: str, end_date: str):
    """拉取标的日线，统一返回列：datetime / 开盘 / 收盘 / 最高 / 最低 / 成交量(手) / 成交额(元)。

    个股用 stock_zh_a_daily（新浪，symbol 带市场前缀）；指数用 stock_zh_index_daily。
    """
    if is_index(market, code):
        symbol = ("sh" if market == "SH" else "sz" if market == "SZ" else "bj") + code
        df = ak.stock_zh_index_daily(symbol=symbol)
        if df is None or df.empty:
            raise RuntimeError(f"{symbol} 指数接口返回空数据")
        df = df.copy()
        df = df[(df["date"] >= _pd_date(start_date)) & (df["date"] <= _pd_date(end_date))]
        if df.empty:
            raise RuntimeError(f"{symbol} 指数区间无数据")
        df["datetime"] = df["date"].apply(lambda d: int(_pd_date(d).strftime("%Y%m%d")) * 10000)
        df["开盘"] = df["open"]
        df["收盘"] = df["close"]
        df["最高"] = df["high"]
        df["最低"] = df["low"]
        df["成交量"] = df["volume"] if "volume" in df.columns else 0
        df["成交额"] = 0  # 指数接口无成交额
        return df.sort_values("datetime").reset_index(drop=True)

    symbol = market.lower() + code
    df = ak.stock_zh_a_daily(symbol=symbol, start_date=start_date, end_date=end_date, adjust="")
    if df is None or df.empty:
        raise RuntimeError(f"{symbol} 个股接口返回空数据")
    df = df.copy()
    df["datetime"] = df["date"].apply(lambda d: int(_pd_date(d).strftime("%Y%m%d")) * 10000)
    df["开盘"] = df["open"]
    df["收盘"] = df["close"]
    df["最高"] = df["high"]
    df["最低"] = df["low"]
    df["成交量"] = (df["volume"] / 100.0).round()  # 股 → 手
    df["成交额"] = df["amount"]
    return df.sort_values("datetime").reset_index(drop=True)


def write_kline(h5file, market: str, code: str, df) -> int:
    """把 akshare DataFrame 幂等写入 H5（价格×1000，成交额元→千元，量单位手）。

    与增量追加不同，存量修复会插入**历史中间**的缺口，故采用"整表合并重建"：
    旧记录 + 新记录 → 按 datetime 排序去重 → 整表重写，保证表内始终有序。
    返回实际新增条数。
    """
    import numpy as np

    table = get_table(h5file, market, code)
    new_recs = np.zeros(len(df), dtype=table.dtype)
    for j, rec in enumerate(df.itertuples(index=False)):
        new_recs[j]["datetime"] = int(rec.datetime)
        new_recs[j]["openPrice"] = round(float(rec.开盘) * 1000)
        new_recs[j]["highPrice"] = round(float(rec.最高) * 1000)
        new_recs[j]["lowPrice"] = round(float(rec.最低) * 1000)
        new_recs[j]["closePrice"] = round(float(rec.收盘) * 1000)
        new_recs[j]["transAmount"] = round(float(rec.成交额) * 0.001)
        new_recs[j]["transCount"] = round(float(rec.成交量))
    merged = np.concatenate([table.read(), new_recs]) if table.nrows else new_recs
    merged = np.sort(merged, order="datetime")
    # 相邻去重（datetime 相同只保留一条）
    keep = np.concatenate([[True], merged["datetime"][1:] != merged["datetime"][:-1]])
    merged = merged[keep]
    added = len(merged) - table.nrows
    if added <= 0:
        return 0
    table.remove_rows(0, table.nrows)
    table.append(merged)
    table.flush()
    return added


def collect_targets(limit_stocks: int = 0, markets=None):
    """从最新缺口报告聚合修复目标：code(6位) -> {market, start, end}（合并全部缺口区间）。

    gap 报告中的 code 是完整表名（含 SH/SZ/BJ 前缀），这里统一剥离为 6 位代码。
    """
    from .verify import check_gaps

    rep = check_gaps()
    targets = {}
    for market, full_code, prev, nxt, _ in rep["stock_gap_rows"]:
        if markets and market not in markets:
            continue
        code = full_code[2:]  # 去掉市场前缀
        if code in targets:
            t = targets[code]
            t["start"] = min(t["start"], _add_days(prev, 1))
            t["end"] = max(t["end"], _add_days(nxt, -1))
        else:
            targets[code] = {
                "market": market,
                "start": _add_days(prev, 1),
                "end": _add_days(nxt, -1),
            }
    codes = sorted(targets)
    if limit_stocks:
        codes = codes[:limit_stocks]
    return targets, codes


def main() -> None:
    parser = argparse.ArgumentParser(description="存量缺口修复（akshare → H5）")
    parser.add_argument("--markets", type=str, default="SH,SZ,BJ", help="要修复的市场，逗号分隔")
    parser.add_argument("--limit-stocks", type=int, default=0, help="只修复前 N 只（冒烟）")
    parser.add_argument("--sleep", type=float, default=AK_REQ_INTERVAL, help="相邻请求间隔秒数")
    args = parser.parse_args()

    _setup_logging()
    _clean_proxy_env()
    import akshare as ak

    markets = [m.strip().upper() for m in args.markets.split(",") if m.strip()]
    targets, codes = collect_targets(args.limit_stocks, markets)
    total = len(codes)
    logger.info("缺口修复开始：共 %d 只标的（%s），请求间隔 %.1fs，每 %d 只休息 %ds",
                total, ",".join(markets), args.sleep, AK_BATCH_SIZE, AK_BATCH_BREAK)
    if not total:
        logger.info("无缺口标的，退出")
        return

    sqlite_path = DATA_DIR / "stock.db"
    conn = sqlite3.connect(sqlite_path)
    h5files = {m: tb.open_file(str(H5_FILES[m]), "a",
                               filters=tb.Filters(complevel=9, complib="zlib", shuffle=True))
               for m in markets if H5_FILES[m].exists()}

    added_total = failed_total = empty_total = 0
    failed_list, consecutive_fail = [], 0
    started = time.time()
    try:
        for i, code in enumerate(codes, 1):
            t = targets[code]
            market = t["market"]
            h5file = h5files.get(market)
            if h5file is None:
                continue
            df = None
            try:
                for attempt in range(1, AK_MAX_RETRY + 1):
                    try:
                        df = fetch_daily(ak, market, code, t["start"], t["end"])
                        break
                    except RuntimeError as e:
                        # 空数据（停牌/无行情）不重试，直接跳过
                        logger.info("[%d/%d] %s%s %s", i, total, market, code, e)
                        df = None
                        break
                    except Exception as e:
                        if attempt < AK_MAX_RETRY:
                            time.sleep(args.sleep * 2)
                        else:
                            logger.warning("[%d/%d] %s%s 拉取失败: %s", i, total, market, code, e)
                if df is not None and not df.empty:
                    added = write_kline(h5file, market, code, df)
                    if added:
                        update_hdf5_extern_data(h5file, market + code, "DAY")
                        table = get_table(h5file, market, code)
                        if table.nrows > 0:
                            conn.execute(
                                "update stock set valid=1, startdate=%d, enddate=%d "
                                "where marketid=%d and code='%s'"
                                % (int(table[0]["datetime"]) // 10000,
                                   int(table[-1]["datetime"]) // 10000,
                                   (1 if market == "SH" else 2 if market == "SZ" else 3),
                                   code)
                            )
                            conn.commit()
                        added_total += added
                        consecutive_fail = 0
                        logger.info("[%d/%d] %s%s 补入 %d 条 [%s, %s]",
                                    i, total, market, code, added, t["start"], t["end"])
                    else:
                        empty_total += 1  # 区间内没有新数据（停牌/数据源无）
                else:
                    empty_total += 1
                    consecutive_fail = 0
            except Exception as e:
                failed_total += 1
                consecutive_fail += 1
                failed_list.append((market, code, str(e)))
                if consecutive_fail >= AK_MAX_CONSECUTIVE_FAIL:
                    logger.error("连续失败 %d 只，疑似数据源异常，熔断停止", consecutive_fail)
                    break
            if args.sleep > 0:
                time.sleep(args.sleep)
            if i % AK_BATCH_SIZE == 0:
                elapsed = time.time() - started
                rate = i / elapsed if elapsed else 0
                eta = (total - i) / rate / 60 if rate else 0
                logger.info("进度 %d/%d (%.1f%%) 补入 %d 条 失败 %d | 速率 %.2f 只/s 预计剩余 %.0f 分钟",
                            i, total, i / total * 100, added_total, failed_total, rate, eta)
                time.sleep(AK_BATCH_BREAK)
    finally:
        for h5file in h5files.values():
            h5file.close()
        conn.close()

    elapsed = time.time() - started
    logger.info("缺口修复完成: 补入 %d 条, 无新数据 %d 只(停牌/数据源无), 失败 %d 只, 耗时 %.1f 分钟",
                added_total, empty_total, failed_total, elapsed / 60)
    if failed_list:
        logger.warning("失败清单（前 20）: %s", failed_list[:20])


if __name__ == "__main__":
    main()
