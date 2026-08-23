"""数据完整性校验：覆盖度统计 + 新鲜度抽样 + 全市场逐股缺口检测。

三种校验（力度递增）:
    1) stat_tables   覆盖度统计：各市场标的数与日线总行数（H5 Table 节点）
    2) check_freshness 新鲜度抽样：随机抽样 N 只，查最后一条日线的日期
    3) check_gaps    全市场缺口检测：逐股对照 A 股交易日历检查序列连续性，
                     定位缺失交易日与系统性缺日（--check-gaps 开启）

用法:
    python -m datacenter.verify               # 覆盖度 + 抽样新鲜度
    python -m datacenter.verify --check-gaps  # 额外执行全市场逐股缺口检测
    python -m datacenter.verify --samples 50  # 调整抽样数
    python -m datacenter.verify --limit-stocks 500  # 缺口检测只查前 500 只（调试）
"""
import argparse
import collections
import datetime
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path

import numpy as np  # type: ignore

from .config import DATA_DIR, LOG_DIR, MARKETS

logger = logging.getLogger("datacenter.verify")


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def stat_tables() -> dict:
    """统计各市场 H5 文件中的标的数量与总 K 线条数（日线节点为 Table 类型）。

    修复：旧实现用 _f_iter_nodes("Array") 迭代，匹配不到 Table 节点，统计恒为 0。
    """
    import tables  # type: ignore

    result = {}
    for market in MARKETS:
        h5 = DATA_DIR / f"{market.lower()}_day.h5"
        if not h5.exists():
            result[market] = None
            continue
        n_stocks = 0
        n_rows = 0
        with tables.open_file(str(h5), "r") as f:
            for t in f.walk_nodes("/data", "Table"):
                n_stocks += 1
                n_rows += t.nrows
        result[market] = {"stocks": n_stocks, "rows": n_rows}
    return result


def check_freshness(samples: int) -> list:
    """抽样股票，直接读 H5 日线表末行日期，检查数据新鲜度（不依赖 hikyuu 运行时）。"""
    import tables  # type: ignore

    conn = sqlite3.connect(DATA_DIR / "stock.db")
    # type: 1=沪深股票, 11=北证股票（920xxx）；marketid: 1=SH, 2=SZ, 3=BJ
    rows = conn.execute(
        "SELECT marketid, code FROM stock WHERE valid=1 AND type IN (1, 11) "
        "ORDER BY RANDOM() LIMIT ?",
        (samples,),
    ).fetchall()
    conn.close()

    mkt_prefix = {1: "SH", 2: "SZ", 3: "BJ"}
    h5files = {}
    try:
        for market in MARKETS:
            p = DATA_DIR / f"{market.lower()}_day.h5"
            if p.exists():
                h5files[market] = tables.open_file(str(p), "r")
    except Exception as e:
        logger.warning("H5 打开失败，抽样新鲜度检查中止: %s", e)
        return []

    try:
        today = datetime.date.today()
        reports = []
        for marketid, code in rows:
            market = mkt_prefix.get(marketid)
            last_dt = None
            h5 = h5files.get(market)
            if h5 is not None:
                try:
                    t = h5.get_node("/data", f"{market}{code}")
                    if t.nrows:
                        last_dt = int(t[-1]["datetime"])  # YYYYMMDDHHMM
                except Exception:
                    last_dt = None
            if last_dt is None:
                reports.append((f"{market}{code}", None, "无K线"))
                continue
            ymd = last_dt // 10000
            d = datetime.date(ymd // 10000, (ymd // 100) % 100, ymd % 100)
            gap = (today - d).days
            reports.append((f"{market}{code}", d.strftime("%Y-%m-%d"), f"{gap}天前"))
        return reports
    finally:
        for h5 in h5files.values():
            h5.close()


def load_trade_days() -> np.ndarray:
    """A 股交易日历（YYYYMMDD int 数组，升序）。

    优先读本地缓存 data/state/trade_calendar.json（含今年日期即复用），
    否则拉 akshare 并缓存；akshare 失败时降级为工作日序列（节假日将误报缺口，
    日志中会给出明确警告）。
    """
    today = datetime.date.today()
    cache = DATA_DIR.parent / "state" / "trade_calendar.json"
    if cache.exists():
        try:
            days = json.loads(cache.read_text(encoding="utf-8"))
            if days and any(int(d) >= today.year * 10000 for d in days):
                return np.array([int(d) for d in days], dtype=np.int64)
        except Exception:
            pass
    try:
        import akshare as ak

        df = ak.tool_trade_date_hist_sina()
        days = sorted(int(str(d).replace("-", "")) for d in df["trade_date"])
    except Exception as e:
        logger.warning("akshare 交易日历获取失败，降级为工作日序列（节假日将误报缺口）: %s", e)
        days = [int(str(d).replace("-", ""))
                for d in np.busday_range("1990-01-01", today.isoformat())]
    if days:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(days), encoding="utf-8")
    return np.array(days, dtype=np.int64)


def check_gaps(limit_stocks: int = 0, top_by_date: int = 15, recent_days: int = 0) -> dict:
    """全市场逐股检查日线序列连续性（对照 A 股交易日历），返回缺口报告。

    原理:
        1) 每只股票的 datetime 列（YYYYMMDDHHMM）整除 10000 得到日期数组 dates；
        2) idx = np.searchsorted(trade_days, dates)：每个日期在交易日历中的下标
           （交易日历是严格升序数组，该操作整体向量化）；
        3) gap = np.diff(idx) - 1：相邻两根 K 线之间跳过的交易日数量；
        4) gap>0 即该区间存在缺失交易日——可能为真实缺口，也可能是停牌
           （停牌日无 K 线属正常，无法从行情本身区分，故按"缺口候选"报告）；
        5) 对所有缺口区间内的具体缺失日期做聚合，若某天大量股票同时缺失
           即为系统性缺日（典型：数据源某天漏拉 / 该市场当天整体无数据）。

    recent_days > 0 时只检查最近 N 天的序列（忽略早期历史数据的既有缺口，
    用于每日/每周增量健康监控）；=0 表示全量检查。

    判读指南:
        - 系统性缺日（单日缺失股票数极大）→ 数据源问题，需补拉那天；
        - 单只股票零星缺日 → 大概率停牌，可从公告/股本异动核实；
        - 缺口区间横跨多个交易日且股票数量少 → 疑似真实缺口，需补数据；
        - 早期年份（2000 年前后）大范围缺日 → 历史数据源本身不全，属存量问题。
    """
    import tables  # type: ignore

    trade_days = load_trade_days()
    cutoff = 0
    if recent_days > 0:
        cutoff = int((datetime.date.today() - datetime.timedelta(days=recent_days)).strftime("%Y%m%d"))
    total_stocks = total_gap_pairs = stocks_with_gap = 0
    gap_rows = []  # (market, code, prev, next, n_missing)
    date_counter = collections.Counter()  # 缺失日期 -> 受影响股票数

    for market in MARKETS:
        h5 = DATA_DIR / f"{market.lower()}_day.h5"
        if not h5.exists():
            continue
        with tables.open_file(str(h5), "r") as f:
            codes = sorted(t.name for t in f.walk_nodes("/data", "Table"))
            if limit_stocks:
                codes = codes[:limit_stocks]
            for code in codes:
                node = f.get_node("/data", code)
                dates = node.read(field="datetime") // 10000
                if recent_days > 0:
                    dates = dates[dates >= cutoff]
                if len(dates) < 2:
                    total_stocks += 1
                    continue
                total_stocks += 1
                idx = np.searchsorted(trade_days, dates)
                gap = np.diff(idx) - 1
                pos = np.flatnonzero(gap > 0)
                if len(pos):
                    stocks_with_gap += 1
                    total_gap_pairs += int(len(pos))
                    for p in pos:
                        prev, nxt = int(dates[p]), int(dates[p + 1])
                        gap_rows.append((market, code, prev, nxt, int(gap[p])))
                        for d in trade_days[idx[p] + 1: idx[p + 1]]:
                            date_counter[int(d)] += 1

    report = {
        "checked_date": datetime.date.today().strftime("%Y-%m-%d"),
        "stocks_checked": total_stocks,
        "stocks_with_gap": stocks_with_gap,
        "gap_pairs": total_gap_pairs,
        "stock_gap_rows": gap_rows,
        "top_missing_dates": date_counter.most_common(top_by_date),
    }
    # 逐股缺口清单落盘（logs/gap_report.json），屏幕只打汇总
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with (LOG_DIR / "gap_report.json").open("w", encoding="utf-8") as fp:
        json.dump(report, fp, ensure_ascii=False, indent=2, default=str)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="数据完整性校验")
    parser.add_argument("--samples", type=int, default=20, help="抽样股票数")
    parser.add_argument("--check-gaps", action="store_true",
                        help="执行全市场逐股连续性缺口检测（对照 A 股交易日历）")
    parser.add_argument("--limit-stocks", type=int, default=0,
                        help="缺口检测只处理前 N 只标的（调试用）")
    parser.add_argument("--recent-days", type=int, default=0,
                        help="缺口检测只看最近 N 天（忽略早期历史既有缺口，用于增量健康监控）")
    args = parser.parse_args()

    _setup_logging()
    stats = stat_tables()
    for market, st in stats.items():
        if st is None:
            logger.warning("%s: 无 H5 文件（未导入）", market)
        else:
            logger.info("%s: %d 只标的, %d 条日线", market, st["stocks"], st["rows"])

    try:
        reports = check_freshness(args.samples)
        logger.info("=== 抽样 %d 只股票的最后日线日期 ===", len(reports))
        stale = 0
        for code, last, note in reports:
            logger.info("  %s: last=%s (%s)", code, last, note)
            if last is None or (note and note.startswith(("无",))):
                stale += 1
        logger.info("抽样新鲜度检查完成（%d/%d 只滞后）", stale, len(reports))
    except Exception as e:
        logger.error("新鲜度校验失败（hikyuu 运行时未初始化?）: %s", e)

    if args.check_gaps:
        if args.recent_days:
            logger.info("=== 全市场缺口检测：最近 %d 天窗口 ===", args.recent_days)
        else:
            logger.info("=== 全市场逐股缺口检测（对照 A 股交易日历，全量扫描）===")
        rep = check_gaps(args.limit_stocks, recent_days=args.recent_days)
        logger.info("检查 %d 只标的，其中 %d 只存在缺口候选，共 %d 段缺口区间",
                    rep["stocks_checked"], rep["stocks_with_gap"], rep["gap_pairs"])
        if rep["stocks_with_gap"] == 0:
            logger.info("结论: 未发现连续性缺口（序列完整）")
        else:
            logger.info("缺失日期聚合 Top %d（单日缺失股票数，判断系统性缺日）:", len(rep["top_missing_dates"]))
            for d, n in rep["top_missing_dates"]:
                flag = "  <== 系统性缺日，疑似数据源漏拉" if n > 50 else ""
                logger.info("  %s: %d 只缺失%s", d, n, flag)
            logger.info("逐股缺口明细已写入 %s", LOG_DIR / "gap_report.json")


if __name__ == "__main__":
    main()
