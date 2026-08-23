"""HTTP API 服务（FastAPI）

提供持仓查询、信号获取、回测触发等接口。

启动:
    uvicorn service.api:app --host 0.0.0.0 --port 8888
"""
import os
import sys
import json
import subprocess
import threading
from datetime import datetime, date
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from service.market_hours import market_status, is_trade_day, is_market_open, now_cst
from service.portfolio import PortfolioTracker
from service.monitor import LiveMonitor

app = FastAPI(title="Quant Service API", version="0.1.0")

# 静态文件服务 (web界面)
_WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
app.mount("/static", StaticFiles(directory=_WEB_DIR), name="static")


@app.get("/")
async def root():
    return FileResponse(os.path.join(_WEB_DIR, "index.html"))
portfolio = PortfolioTracker()
monitor = LiveMonitor(portfolio)

# 信号缓存
_latest_signals: list[dict] = []
_latest_hot_stocks: list[dict] = []
_service_status = {
    "started_at": datetime.now().isoformat(),
    "version": "0.1.0",
}


# ---- 依赖注入 ----

def get_state_path(subdir: str):
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(base, "state", subdir)
    os.makedirs(p, exist_ok=True)
    return p


# ---- API 路由 ----

@app.get("/api/status")
async def api_status():
    """服务状态 + 市场状态"""
    return {
        "service": _service_status,
        "market": {
            "status": market_status(),
            "is_trade_day": is_trade_day(),
            "is_market_open": is_market_open(),
            "now": now_cst().isoformat(),
        },
        "portfolio_summary": {
            "total_value": portfolio.total_value,
            "cash": portfolio.cash,
            "pnl": portfolio.total_pnl,
            "pnl_pct": f"{portfolio.total_pnl_pct:.2%}",
            "n_positions": len(portfolio.positions),
        },
    }


@app.get("/api/positions")
async def api_positions():
    """当前持仓列表"""
    quotes = monitor.fetch_quotes()
    if quotes:
        portfolio.update_prices(quotes)
    return portfolio.get_summary()


@app.get("/api/signals")
async def api_signals(
    top_n: int = Query(20, ge=1, le=100),
    refresh: bool = False,
):
    """获取最新交易信号

    信号文件: state/signals/latest.json
    """
    signal_file = os.path.join(get_state_path("signals"), "latest.json")
    if refresh or not os.path.exists(signal_file):
        try:
            result = subprocess.run(
                [sys.executable, "-m", "signals.generate"],
                capture_output=True, text=True, timeout=120,
            )
        except Exception as e:
            raise HTTPException(500, f"信号生成失败: {e}")

    if os.path.exists(signal_file):
        with open(signal_file) as f:
            signals = json.load(f)
        return {"signals": signals[:top_n], "count": len(signals), "updated": True}
    return {"signals": [], "count": 0, "updated": False}


@app.get("/api/hot-stocks")
async def api_hot_stocks():
    """当日热点强势股"""
    from data.fetchers.hexin import HexinFetcher
    h = HexinFetcher()
    today_str = date.today().strftime("%Y-%m-%d")
    try:
        df = h.get_harden_stocks(today_str)
        if df is not None and not df.empty:
            hot = df.sort_values("change_pct", ascending=False).head(20)
            return {
                "date": today_str,
                "count": len(hot),
                "stocks": hot.to_dict(orient="records"),
            }
    except Exception as e:
        pass
    return {"date": today_str, "count": 0, "stocks": []}


@app.get("/api/northbound")
async def api_northbound():
    """北向资金流向"""
    from data.fetchers.hexin import HexinFetcher
    h = HexinFetcher()
    try:
        df = h.get_northbound_flow()
        if df is not None and not df.empty:
            row = df.iloc[-1].to_dict()
            return {"date": str(row.get("date", "")), "data": row}
    except Exception as e:
        pass
    return {}


@app.get("/api/performance")
async def api_performance():
    """绩效快照"""
    snap = monitor.poll()
    return {
        "time": snap["time"],
        "portfolio": snap["portfolio"],
        "alerts": snap["alerts"],
    }


@app.get("/api/alerts")
async def api_alerts():
    """最近告警记录"""
    return {"alerts": monitor.get_alerts()}


@app.post("/api/backtest")
async def api_backtest(
    req: dict = None,
    stocks: int = Query(200, ge=50, le=1000),
    start: str = "20220101",
    end: str = "20260515",
    strategy: str = Query("multi_factor", description="策略名称"),
    period: str = Query(None, description="快捷时间段: 6m,1y,3y,5y,10y"),
):
    """触发回测任务（支持策略选择和快捷时间段）"""
    if req:
        stocks = req.get("stocks", stocks)
        start = req.get("start", start)
        end = req.get("end", end)
        strategy = req.get("strategy", strategy)
        period = req.get("period", period)

    result = {"status": "started", "strategy": strategy}

    def _run():
        try:
            cmd = [sys.executable, "run_backtest.py",
                   "--strategy", strategy,
                   "--stocks", str(stocks),
                   "--start", start.replace("-", ""),
                   "--end", end.replace("-", "")]
            if period:
                cmd += ["--period", period]
            if strategy != "multi_factor":
                cmd += ["--simulate"]
            subprocess.run(
                cmd,
                cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                timeout=600,
            )
        except Exception as e:
            pass

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    result["message"] = f"回测已提交: strategy={strategy}, stocks={stocks}, {start}~{end}"
    return result


@app.get("/api/backtest/results")
async def api_backtest_results():
    """获取最新回测结果"""
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "state", "backtest_results", "latest.json",
    )
    if os.path.exists(path):
        with open(path) as f:
            data = json.load(f)
        return data
    return {"status": "no_results", "message": "尚无回测结果，请先运行回测"}


@app.get("/api/factors")
async def api_factors(factor: str = "momentum"):
    """单因子检验结果"""
    report_file = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "reports", f"factor_{factor}.png"
    )
    exists = os.path.exists(report_file)
    return {
        "factor": factor,
        "report_exists": exists,
        "report_path": report_file if exists else None,
    }


@app.post("/api/trade")
async def api_trade(req: dict):
    """手动交易接口"""
    action = req.get("action")
    code = req.get("code", "")
    shares = req.get("shares", 0)
    price = req.get("price", 0)

    if action not in ("buy", "sell"):
        raise HTTPException(400, "action must be 'buy' or 'sell'")

    if action == "buy":
        ok = portfolio.buy(code, shares, price)
        return {"success": ok, "cash_remaining": portfolio.cash}
    else:
        proceeds = portfolio.sell(code, shares, price)
        return {"success": True, "proceeds": proceeds}


@app.get("/api/health")
async def health():
    return {"ok": True}


# ============================================================
# 标的池管理
# ============================================================
_WATCHLIST_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "state", "watchlist.json",
)


def _load_watchlist():
    if not os.path.exists(_WATCHLIST_FILE):
        return {"items": [], "presets": {}}
    with open(_WATCHLIST_FILE) as f:
        return json.load(f)


def _save_watchlist(data):
    os.makedirs(os.path.dirname(_WATCHLIST_FILE), exist_ok=True)
    with open(_WATCHLIST_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


@app.get("/api/watchlist")
async def api_watchlist():
    """获取标的池"""
    return _load_watchlist()


@app.post("/api/watchlist")
async def api_watchlist_add(req: dict):
    """操作标的池

    Body:
        {"action": "add"|"remove"|"clear"|"load_preset",
         "code": "...", "name": "...", "type": "ETF"|"STOCK",
         "group": "..."}
         or {"action": "load_preset", "preset_name": "宽基ETF"}
    """
    action = req.get("action", "")
    data = _load_watchlist()

    if action == "add":
        code = req.get("code", "").strip().upper()
        if not code:
            raise HTTPException(400, "code required")
        existing = [i for i in data["items"] if i["code"] == code]
        if existing:
            return {"success": False, "message": f"{code} already in pool"}
        data["items"].append({
            "code": code,
            "name": req.get("name", ""),
            "type": req.get("type", "STOCK"),
            "group": req.get("group", ""),
        })

    elif action == "remove":
        code = req.get("code", "").strip().upper()
        data["items"] = [i for i in data["items"] if i["code"] != code]

    elif action == "update":
        code = req.get("code", "").strip().upper()
        for item in data["items"]:
            if item["code"] == code:
                for field in ("name", "type", "group"):
                    if field in req:
                        item[field] = req[field]
                break

    elif action == "clear":
        data["items"] = []

    elif action == "load_preset":
        preset_name = req.get("preset_name", "")
        presets = data.get("presets", {})
        codes = presets.get(preset_name, [])
        added = 0
        for code in codes:
            if not any(i["code"] == code for i in data["items"]):
                data["items"].append({
                    "code": code, "name": "",
                    "type": "ETF" if code[0] in "15" else "STOCK",
                    "group": preset_name,
                })
                added += 1
        return {"success": True, "message": f"Added {added} from preset '{preset_name}'"}

    else:
        raise HTTPException(400, f"Unknown action: {action}")

    _save_watchlist(data)
    return {"success": True, "count": len(data["items"]), "items": data["items"]}


@app.get("/api/watchlist/quotes")
async def api_watchlist_quotes():
    """获取标的池实时行情"""
    wl = _load_watchlist()
    codes = [i["code"] for i in wl["items"]]
    if not codes:
        return {"quotes": []}

    try:
        from data.fetchers.tencent import TencentFetcher
        t = TencentFetcher()
        df = t.get_realtime_quote(codes[:60])
        if df.empty:
            return {"quotes": []}

        quotes = []
        for _, r in df.iterrows():
            quotes.append({
                "code": r["code"], "name": r.get("name", ""),
                "price": round(r.get("close", 0), 3),
                "change_pct": round(r.get("change_pct", 0), 2),
                "pe": round(r.get("pe", 0), 1),
                "pb": round(r.get("pb", 0), 2),
                "market_cap": round(r.get("market_cap", 0) / 1e8, 1),
                "turnover": round(r.get("turnover", 0), 2),
            })
        return {"quotes": quotes}
    except Exception as e:
        return {"quotes": [], "error": str(e)}


# ============================================================
# 策略管理
# ============================================================
_STRATEGY_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "state", "strategies",
)

def _list_strategies():
    os.makedirs(_STRATEGY_DIR, exist_ok=True)
    strategies = []
    for fname in sorted(os.listdir(_STRATEGY_DIR)):
        if fname.endswith(".json"):
            try:
                with open(os.path.join(_STRATEGY_DIR, fname)) as f:
                    strategies.append(json.load(f))
            except Exception:
                pass
    return sorted(strategies, key=lambda x: x.get("status", "") != "active")


def _save_strategy(s: dict):
    os.makedirs(_STRATEGY_DIR, exist_ok=True)
    path = os.path.join(_STRATEGY_DIR, f"{s['id']}.json")
    with open(path, "w") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)


def _delete_strategy(strategy_id: str):
    path = os.path.join(_STRATEGY_DIR, f"{strategy_id}.json")
    if os.path.exists(path):
        os.remove(path)


@app.get("/api/strategies")
async def api_strategies():
    """获取所有策略"""
    return {"strategies": _list_strategies()}


@app.post("/api/strategies")
async def api_strategies_manage(req: dict):
    """策略管理

    Body:
        {"action": "save", "strategy": {...}}
        {"action": "delete", "id": "trend_etf"}
        {"action": "duplicate", "id": "trend_etf", "new_id": "trend_v2"}
        {"action": "toggle", "id": "trend_etf"}
    """
    action = req.get("action", "")

    if action == "save":
        s = req.get("strategy", {})
        if not s.get("id"):
            raise HTTPException(400, "strategy.id required")
        s["updated"] = datetime.now().isoformat()
        if "created" not in s:
            s["created"] = s["updated"]
        _save_strategy(s)
        return {"success": True, "id": s["id"]}

    elif action == "delete":
        sid = req.get("id", "")
        if not sid:
            raise HTTPException(400, "id required")
        _delete_strategy(sid)
        return {"success": True}

    elif action == "duplicate":
        sid = req.get("id", "")
        new_id = req.get("new_id", f"{sid}_copy")
        strategies = _list_strategies()
        original = next((s for s in strategies if s["id"] == sid), None)
        if not original:
            raise HTTPException(404, f"Strategy {sid} not found")
        original["id"] = new_id
        original["name"] = f"{original['name']} (副本)"
        original["status"] = "draft"
        original["created"] = datetime.now().isoformat()
        original["updated"] = original["created"]
        _save_strategy(original)
        return {"success": True, "id": new_id}

    elif action == "toggle":
        sid = req.get("id", "")
        strategies = _list_strategies()
        s = next((x for x in strategies if x["id"] == sid), None)
        if not s:
            raise HTTPException(404, f"Strategy {sid} not found")
        s["status"] = "draft" if s.get("status") == "active" else "active"
        s["updated"] = datetime.now().isoformat()
        _save_strategy(s)
        return {"success": True, "status": s["status"]}

    else:
        raise HTTPException(400, f"Unknown action: {action}")


@app.post("/api/strategies/run")
async def api_strategies_run(req: dict):
    """运行策略回测

    Body:
        {"id": "trend_etf"}           - 使用 JSON 保存的参数运行
        {"strategy": "etf_momentum", "period": "1y"}   - 快捷时间段
        {"strategy": "momentum", "stocks": 200, "start": "20240101", "end": "20260515"}
    """
    sid = req.get("id", "")
    strategy = req.get("strategy", req.get("type", ""))
    period = req.get("period", "")
    stocks = req.get("stocks", 200)

    if sid:
        strategies = _list_strategies()
        s = next((x for x in strategies if x["id"] == sid), None)
        if not s:
            raise HTTPException(404, f"Strategy {sid} not found")
        strategy = s["type"]
        params = s.get("params", {})
        stocks = params.get("stocks", params.get("initial_capital", 1000000))
    else:
        params = req.get("params", {})

    start = req.get("start", params.get("start_date", "20240101"))
    end = req.get("end", params.get("end_date", "20260515"))

    result = {"status": "started", "strategy": strategy}

    def _run():
        import subprocess, os
        BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cmd = [sys.executable, "run_backtest.py",
               "--strategy", strategy,
               "--stocks", str(stocks),
               "--start", start.replace("-", ""),
               "--end", end.replace("-", "")]
        if period:
            cmd += ["--period", period]
        if strategy != "multi_factor":
            cmd += ["--simulate"]
        try:
            subprocess.run(cmd, cwd=BASE, timeout=600)
        except Exception as e:
            pass

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    result["message"] = f"回测已提交: strategy={strategy}"
    return result


@app.get("/api/strategies/types")
async def api_strategy_types():
    """策略类型元数据（含注册表中的所有策略）"""
    try:
        from backtest.strategy_runner import list_strategies, STRATEGY_REGISTRY
        types = []
        for sid, info in sorted(STRATEGY_REGISTRY.items()):
            types.append({
                "id": sid,
                "name": sid,
                "desc": info["description"],
                "category": "ETF" if "etf" in sid else "股票",
            })
        return {"types": types, "categories": ["ETF", "股票", "混合", "基准"]}
    except Exception:
        return {"types": [], "categories": []}
