#!/usr/bin/env python3
"""量化交易常驻后台服务

用法:
    python service.py daemon              # 前台运行守护进程（调度+API）
    python service.py status              # 查看当前状态
    python service.py signals             # 查看最新信号
    python service.py hot                 # 查看今日热点
    python service.py monitor             # 运行一次行情轮询（测试用）
    python service.py api --port 8888     # 仅启动 HTTP API
    python service.py run-once            # 执行一次当前时段任务
    python service.py backtest            # 触发回测

调度逻辑:
    - 盘前(9:00-9:25): 获取热点、计算信号
    - 开盘(9:30): 轮询行情、更新持仓
    - 盘中: 每30s更新持仓盈亏、风控检查
    - 收盘(15:00): 保存快照、跑因子
    - 盘后(15:00-16:00): 生成明日信号、数据缓存
    - 非交易日: 睡眠等待
"""
import sys
import os
import time
import json
import logging
import argparse
import subprocess
import threading
from datetime import datetime, date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from service.market_hours import (
    now_cst, today_cst, market_status, is_trade_day, is_market_open,
    seconds_until, MORNING_OPEN, MORNING_CLOSE, AFTERNOON_OPEN,
    AFTERNOON_CLOSE,
)
from service.portfolio import PortfolioTracker
from service.monitor import LiveMonitor
from service.scheduler import TaskScheduler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(BASE_DIR, "state")
os.makedirs(STATE_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(STATE_DIR, "service.log")),
    ],
)
log = logging.getLogger("quant.service")


# ============================================================
# 全局组件
# ============================================================

portfolio = PortfolioTracker()
monitor = LiveMonitor(portfolio)
scheduler = TaskScheduler()

# 信号输出目录
SIGNAL_DIR = os.path.join(STATE_DIR, "signals")
os.makedirs(SIGNAL_DIR, exist_ok=True)


# ============================================================
# 任务回调
# ============================================================

def task_pre_market(date=None):
    """盘前任务: 获取热点、刷新watchlist"""
    log.info("执行盘前任务...")
    _save_signals()


def task_market_open(date=None):
    """开盘任务: 打印持仓概览"""
    quotes = monitor.fetch_quotes()
    if quotes:
        portfolio.update_prices(quotes)
    log.info(f"开盘持仓: {len(portfolio.positions)} 只, 总市值 {portfolio.total_value:,.0f}")


def task_intraday(date=None, status=None):
    """盘中轮询: 更新行情、风控检查"""
    status = status or market_status()
    result = monitor.poll()

    n_pos = result["portfolio"]["n_positions"]
    if n_pos > 0:
        pnl = result["portfolio"]["pnl_pct"]
        log.info(f"[{status}] 持仓 {n_pos} 只 | 总盈亏 {pnl} | 行情 {result['n_quotes']} 只")

    if result["alerts"]:
        for a in result["alerts"]:
            log.warning(a)


def task_market_close(date=None):
    """收盘任务: 保存快照"""
    quotes = monitor.fetch_quotes()
    if quotes:
        portfolio.update_prices(quotes)
    snap = portfolio.snapshot()
    portfolio.daily_snapshots.append(snap)
    portfolio._save_state()
    log.info(f"收盘快照保存: 总市值 {portfolio.total_value:,.0f}, 盈亏 {portfolio.total_pnl_pct:.2%}")


def task_daily(date=None):
    """每日任务: 生成信号、缓存数据"""
    log.info("执行每日任务...")
    _save_signals()
    log.info("每日任务完成")


def task_after_market(date=None):
    """盘后任务: 数据分析"""
    log.info("盘后分析...")


# ============================================================
# 信号生成
# ============================================================

def _save_signals():
    """生成交易信号并保存到文件

    策略：多因子 Alpha 选股优先；若数据源不可用，回退到热点强势股 + 实时涨跌幅。
    """
    signal_file = os.path.join(SIGNAL_DIR, "latest.json")

    # 1. 多因子 Alpha 选股（技术/行为因子合成，Top 30）
    try:
        from signals.generate import generate_signals, save_signals

        df = generate_signals(top_n=30, n_stocks=300, verbose=False)
        if df is not None and not df.empty:
            records = save_signals(df, out_path=signal_file)
            log.info(f"多因子信号已保存: {len(records)} 条 (source=multi_factor)")
            return records
        log.warning("多因子信号为空，回退到热点信号")
    except Exception as e:
        log.error(f"多因子信号生成失败({e})，回退到热点信号")

    # 2. 回退：热点强势股 + 实时涨跌幅
    try:
        from data.fetchers.hexin import HexinFetcher
        from data.fetchers.tencent import TencentFetcher

        h = HexinFetcher()
        today = date.today().strftime("%Y-%m-%d")
        hot_df = h.get_harden_stocks(today)

        signal_data = []
        if hot_df is not None and not hot_df.empty:
            hot_df = hot_df.sort_values("change_pct", ascending=False).head(30)
            for _, r in hot_df.iterrows():
                signal_data.append({
                    "code": r["code"],
                    "name": r.get("name", ""),
                    "score": round(r.get("change_pct", 0), 2),
                    "change_pct": r.get("change_pct", 0),
                    "reason": r.get("reason_tags", "") or "",
                    "turnover": r.get("turnover", 0),
                    "source": "hot_topic",
                })

        # 补充实时行情 Top 涨跌幅
        if len(signal_data) < 20:
            t = TencentFetcher()
            stock_list = _get_sample_codes(100)
            quotes_df = t.get_realtime_quote(stock_list[:60])
            if not quotes_df.empty:
                top = quotes_df.sort_values("change_pct", ascending=False).head(20)
                existing = {s["code"] for s in signal_data}
                for _, r in top.iterrows():
                    if r["code"] not in existing:
                        signal_data.append({
                            "code": r["code"],
                            "name": r.get("name", ""),
                            "score": round(r.get("change_pct", 0), 2),
                            "change_pct": r.get("change_pct", 0),
                            "reason": "",
                            "turnover": r.get("turnover", 0),
                            "source": "realtime_top",
                        })

        with open(signal_file, "w") as f:
            json.dump(signal_data, f, ensure_ascii=False, indent=2)

        log.info(f"信号已保存: {signal_file} ({len(signal_data)} 条)")
        return signal_data
    except Exception as e:
        log.error(f"信号生成失败: {e}")
        return []


def _get_sample_codes(n=100):
    """获取采样股票代码列表（缓存）"""
    cache_file = os.path.join(STATE_DIR, "sample_codes.json")
    if os.path.exists(cache_file):
        with open(cache_file) as f:
            return json.load(f)
    try:
        from data.fetchers.akshare_fetcher import AkShareFetcher
        ak = AkShareFetcher()
        df = ak.get_stock_list()
        codes = []
        for c in df["stock_code"].tolist():
            c = str(c).strip()
            if c[:1] in ("6", "9"):
                codes.append(f"{c}.SH")
            elif c[:1] in ("0", "3"):
                codes.append(f"{c}.SZ")
        import random
        random.seed(42)
        selected = sorted(random.sample(codes, min(n, len(codes))))
        with open(cache_file, "w") as f:
            json.dump(selected, f)
        return selected
    except Exception:
        return ["000001.SZ", "000002.SZ", "600000.SH"]


# ============================================================
# CLI 命令
# ============================================================

def cmd_status():
    """打印当前状态"""
    print(f"\n{'='*50}")
    print(f"  Quant Service 状态")
    print(f"{'='*50}")
    print(f"  时间:     {now_cst().strftime('%Y-%m-%d %H:%M:%S CST')}")
    print(f"  市场:     {market_status()}")
    print(f"  交易日:   {'是' if is_trade_day() else '否'}")
    print(f"  交易中:   {'是' if is_market_open() else '否'}")

    quotes = monitor.fetch_quotes()
    if quotes:
        portfolio.update_prices(quotes)

    s = portfolio.get_summary()
    print(f"  总资产:   {s['total_value']:,.2f}")
    print(f"  现金:     {s['cash']:,.2f}")
    print(f"  持仓市值: {s['stock_value']:,.2f}")
    print(f"  总盈亏:   {s['pnl']:+,.2f} ({s['pnl_pct']})")
    print(f"  持仓数:   {s['n_positions']}")

    if s["positions"]:
        print(f"\n  持仓明细:")
        print(f"  {'代码':<12} {'名称':<8} {'股价':>8} {'盈亏':>8} {'占比':>6}")
        print(f"  {'-'*48}")
        for p in s["positions"]:
            print(f"  {p['code']:<12} {p['name']:<8} {p['price']:>8} {p['pnl_pct']:>8} {p['weight']:>6}")
    print(f"{'='*50}\n")


def cmd_signals():
    """打印最新信号"""
    signal_file = os.path.join(SIGNAL_DIR, "latest.json")
    if not os.path.exists(signal_file):
        print("无缓存信号，正在生成...")
        _save_signals()

    if os.path.exists(signal_file):
        with open(signal_file) as f:
            signals = json.load(f)
        print(f"\n{'='*50}")
        print(f"  最新交易信号 ({len(signals)} 条)")
        print(f"{'='*50}")
        print(f"  {'排名':<5} {'代码':<12} {'名称':<8} {'评分':>8} {'涨跌':>8}")
        print(f"  {'-'*45}")
        for i, s in enumerate(signals[:20], 1):
            print(f"  {i:<5} {s['code']:<12} {s['name']:<8} {s['score']:>8.4f} {s.get('change_pct',0):>+8.2f}%")
        print(f"{'='*50}\n")
    else:
        print("暂无信号")


def cmd_hot():
    """打印今日热点"""
    from data.fetchers.hexin import HexinFetcher
    h = HexinFetcher()
    today_str = date.today().strftime("%Y-%m-%d")
    df = h.get_harden_stocks(today_str)
    if df is not None and not df.empty:
        top = df.sort_values("change_pct", ascending=False).head(20)
        print(f"\n{'='*50}")
        print(f"  今日热点强势股 ({len(top)} 只)")
        print(f"{'='*50}")
        for _, r in top.iterrows():
            reason = r.get("reason_tags", "") or ""
            print(f"  {r['code']} {r.get('name','?')}  {r['change_pct']:+.2f}%  {reason[:40]}")
        print(f"{'='*50}\n")
    else:
        print("暂无热点数据")


def cmd_monitor():
    """执行一次行情轮询"""
    print("执行行情轮询...")
    result = monitor.poll()
    s = result["portfolio"]
    print(f"  总资产: {s['total_value']:,.2f}")
    print(f"  盈亏:   {s['pnl_pct']}")
    print(f"  行情:   {result['n_quotes']} 只")
    if result["alerts"]:
        for a in result["alerts"]:
            print(f"  [告警] {a}")


def cmd_backtest():
    """触发回测"""
    print("启动回测...")
    subprocess.run(
        [sys.executable, "run_backtest.py", "--stocks", "200"],
        cwd=BASE_DIR,
    )


# ============================================================
# 守护进程
# ============================================================

def run_daemon(enable_api=True, api_port=8888):
    """启动守护进程（前台运行）

    Args:
        enable_api: 是否启动 HTTP API
        api_port: API 监听端口
    """
    print(f"""
{'='*55}
  Quant Service Daemon
  启动时间: {now_cst().strftime('%Y-%m-%d %H:%M:%S CST')}
  市场状态: {market_status()}
{'='*55}
""")

    # 注册任务
    scheduler.on("pre_market", task_pre_market)
    scheduler.on("market_open", task_market_open)
    scheduler.on("intraday", task_intraday)
    scheduler.on("market_close", task_market_close)
    scheduler.on("daily", task_daily)
    scheduler.on("after_market", task_after_market)

    # 启动 API
    if enable_api:
        import uvicorn
        api_thread = threading.Thread(
            target=lambda: uvicorn.run(
                "service.api:app",
                host="0.0.0.0", port=api_port,
                log_level="warning",
            ),
            daemon=True,
        )
        api_thread.start()
        print(f"  HTTP API: http://localhost:{api_port}/api/status")
        print(f"  API 文档: http://localhost:{api_port}/docs")
        print()

    print("  按 Ctrl+C 停止服务\n")

    try:
        scheduler.run_forever()
    except KeyboardInterrupt:
        print("\n正在停止...")
        scheduler.stop()
        log.info("服务已停止")


# ============================================================
# 主入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Quant Service")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("daemon", help="前台运行守护进程")
    sub.add_parser("status", help="查看当前状态")
    sub.add_parser("signals", help="查看最新信号")
    sub.add_parser("hot", help="查看今日热点")
    sub.add_parser("monitor", help="运行一次行情轮询")
    sub.add_parser("run-once", help="执行一次当前时段任务")
    sub.add_parser("backtest", help="触发回测")

    api_parser = sub.add_parser("api", help="仅启动 HTTP API")
    api_parser.add_argument("--port", type=int, default=8888)

    args = parser.parse_args()

    if args.command == "daemon":
        run_daemon()
    elif args.command == "status":
        cmd_status()
    elif args.command == "signals":
        cmd_signals()
    elif args.command == "hot":
        cmd_hot()
    elif args.command == "monitor":
        cmd_monitor()
    elif args.command == "run-once":
        scheduler.run_once()
    elif args.command == "backtest":
        cmd_backtest()
    elif args.command == "api":
        import uvicorn
        print(f"HTTP API: http://localhost:{args.port}/api/status")
        uvicorn.run("service.api:app", host="0.0.0.0", port=args.port)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
