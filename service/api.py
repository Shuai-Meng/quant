"""HTTP API 服务（FastAPI）

提供持仓查询、信号获取、回测触发等接口。

启动:
    uvicorn service.api:app --host 0.0.0.0 --port 8888
"""
import os
import sys
import json
import logging
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

logger = logging.getLogger("service.api")
app = FastAPI(title="Quant Service API", version="0.1.0")

# ---- 公网暴露防护：Basic Auth + 只读模式（环境变量控制）----
# BASIC_AUTH_USER / BASIC_AUTH_PASS 同时设置时启用鉴权（未设置则不鉴权，便于本地调试）
# READ_ONLY=1 时禁止一切写操作（交易/回测/策略/预测/标的池修改等）
import base64
import secrets as _secrets

_BASIC_USER = os.environ.get("BASIC_AUTH_USER", "")
_BASIC_PASS = os.environ.get("BASIC_AUTH_PASS", "")
_READ_ONLY = os.environ.get("READ_ONLY", "") == "1"
_AUTH_ENABLED = bool(_BASIC_USER and _BASIC_PASS)

# 只读模式下拦截的路径前缀（写接口）
_WRITE_PREFIXES = ("/api/trade", "/api/backtest", "/api/strategies",
                   "/api/predicts/run", "/api/watchlist")


def _check_basic_auth(authorization: str) -> bool:
    if not authorization.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(authorization[6:]).decode("utf-8")
        user, _, passwd = decoded.partition(":")
    except Exception:
        return False
    return _secrets.compare_digest(user, _BASIC_USER) and _secrets.compare_digest(passwd, _BASIC_PASS)


@app.middleware("http")
async def auth_middleware(request, call_next):
    if _AUTH_ENABLED:
        auth = request.headers.get("Authorization", "")
        if not _check_basic_auth(auth):
            return JSONResponse(
                {"detail": "Unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Quant"'},
            )
    if _READ_ONLY and request.method != "GET":
        for prefix in _WRITE_PREFIXES:
            if request.url.path.startswith(prefix):
                return JSONResponse(
                    {"detail": "read-only mode: 写操作已禁用"}, status_code=403
                )
    return await call_next(request)

# 静态文件服务 (web界面)
_WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
app.mount("/static", StaticFiles(directory=_WEB_DIR), name="static")
# state 目录（预测图表 PNG 等产物）
_STATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state")
if os.path.isdir(_STATE_DIR):
    app.mount("/state", StaticFiles(directory=_STATE_DIR), name="state")


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
    # 只读模式下禁止强制刷新（避免被公网滥用触发子进程）
    refresh = refresh and not _READ_ONLY
    if refresh or not os.path.exists(signal_file):
        try:
            result = subprocess.run(
                [sys.executable, "-m", "signals.generate",
                 "--top", "30", "--stocks", "300"],
                capture_output=True, text=True, timeout=300,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr[-500:] or f"exit {result.returncode}")
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


def _normalize_code(code: str) -> str:
    """将裸代码补全市场后缀，如 600900 -> 600900.SH、159915 -> 159915.SZ"""
    code = (code or "").strip().upper()
    if not code or "." in code or not code.isdigit():
        return code
    first = code[0]
    if first in "569":
        return f"{code}.SH"   # 沪市A股(6)/B股(9)/基金(5)
    if first in "0123":
        return f"{code}.SZ"   # 深市A股(0)/基金(1)/B股(2)/创业板(3)
    if first in "48":
        return f"{code}.BJ"   # 北交所
    return code


def _load_watchlist():
    """读取标的池：MySQL 优先，失败自动降级回 JSON 文件。"""
    data = None
    try:
        from datacenter.mysql_db import list_watchlist_items, list_watchlist_presets
        data = {"items": list_watchlist_items(), "presets": list_watchlist_presets()}
    except Exception:
        logger.warning("标的池 MySQL 读取失败，降级 JSON 文件", exc_info=True)
    if data is None:
        # 降级：JSON 文件
        if not os.path.exists(_WATCHLIST_FILE):
            return {"items": [], "presets": {}}
        with open(_WATCHLIST_FILE) as f:
            data = json.load(f)
    # 统一代码格式（兼容历史数据中的裸代码，如 600900 -> 600900.SH）
    for item in data.get("items", []):
        item["code"] = _normalize_code(item["code"])
    return data


def _save_watchlist(data):
    """保存标的池：MySQL 优先，失败自动降级回 JSON 文件。"""
    try:
        from datacenter.mysql_db import save_watchlist_items, save_watchlist_presets
        save_watchlist_items(data.get("items", []))
        save_watchlist_presets(data.get("presets", {}))
        return
    except Exception:
        logger.warning("标的池 MySQL 写入失败，降级 JSON 文件", exc_info=True)
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
        code = _normalize_code(req.get("code", "").strip().upper())
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
        code = _normalize_code(req.get("code", "").strip().upper())
        data["items"] = [i for i in data["items"] if i["code"] != code]

    elif action == "update":
        code = _normalize_code(req.get("code", "").strip().upper())
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


# ============================================================
# Kronos AI 预测
# ============================================================
_PREDICT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "state", "predicts",
)
# 同一时间只允许一个预测任务（模型加载 + GPU 推理串行化）
_predict_lock = threading.Lock()


def _predict_summary_from_csv(csv_path: str) -> dict | None:
    """从预测 CSV 构建列表项摘要（不读 H5，保证扫描够快）。"""
    try:
        import pandas as pd
        df = pd.read_csv(csv_path)
        if df.empty:
            return None
        first, last = df.iloc[0], df.iloc[-1]
        market_code = os.path.basename(csv_path).replace("pred_", "").replace("_summary.csv", "")
        last_close = float(first["close_p50"])  # 预测首日中位数 ≈ 最新收盘
        final_p50 = float(last["close_p50"])
        prob = float(((df["close_p50"] > last_close).mean()))
        return {
            "code": market_code,
            "name": "",
            "start_date": str(pd.to_datetime(first["date"]).date()),
            "end_date": str(pd.to_datetime(last["date"]).date()),
            "pred_len": int(len(df)),
            "last_close": round(last_close, 3),
            "final_p50": round(final_p50, 3),
            "final_chg": round((final_p50 / last_close - 1) * 100, 2),
            "direction": "up" if final_p50 > last_close else "down",
            "prob": round(prob, 4) if 0.0 < prob < 1.0 else None,
            "hi_p90": round(float(df["close_p90"].max()), 3),
            "lo_p10": round(float(df["close_p10"].min()), 3),
            "file_time": datetime.fromtimestamp(os.path.getmtime(csv_path)).strftime("%Y-%m-%d %H:%M"),
            "has_chart": os.path.exists(csv_path.replace("_summary.csv", "_chart.png")),
        }
    except Exception:
        return None


def _predict_paths(market_code: str) -> tuple[str, str]:
    """返回 (csv_path, chart_path)，market_code 如 sh600900。"""
    mc = market_code.lower()
    csv_path = os.path.join(_PREDICT_DIR, f"pred_{mc}_summary.csv")
    chart_path = os.path.join(_PREDICT_DIR, f"pred_{mc}_chart.png")
    return csv_path, chart_path


@app.get("/api/predicts")
async def api_predicts():
    """已生成的 AI 预测列表（扫描 state/predicts）"""
    if not os.path.isdir(_PREDICT_DIR):
        return {"predicts": [], "count": 0}
    predicts = []
    for fname in sorted(os.listdir(_PREDICT_DIR)):
        if fname.endswith("_summary.csv"):
            item = _predict_summary_from_csv(os.path.join(_PREDICT_DIR, fname))
            if item:
                try:
                    from predict.kronos_engine import find_stock
                    found = find_stock(item["code"])
                    item["name"] = found[1] if found else ""
                except Exception:
                    pass
                predicts.append(item)
    predicts.sort(key=lambda x: x["file_time"], reverse=True)
    return {"predicts": predicts, "count": len(predicts)}


@app.get("/api/predicts/signals")
async def api_predict_signals(limit: int = Query(50, ge=1, le=500)):
    """Kronos 预测信号历史（MySQL kronos_signal 表，可用性自动探测）"""
    try:
        from datacenter.mysql_db import list_kronos_signals, ping
        rows = list_kronos_signals(limit=limit)
        return {"available": True, "signals": rows, "count": len(rows)}
    except Exception as e:
        return {"available": False, "signals": [], "count": 0, "error": str(e)}


@app.get("/api/predicts/{code}")
async def api_predict_detail(code: str):
    """单个预测详情：历史收盘 + 未来 P10~P90 全序列（供 echarts 绘图）"""
    csv_path, _ = _predict_paths(code)
    if not os.path.exists(csv_path):
        raise HTTPException(404, f"预测不存在: {code}（先通过 POST /api/predicts/run 生成）")

    import pandas as pd
    summary = pd.read_csv(csv_path)
    market_code = code.upper() if code[:2].upper() in ("SH", "SZ", "BJ") else f"SH{code}"
    name = ""
    try:
        from predict.kronos_engine import find_stock, read_h5
        found = find_stock(market_code)
        if found:
            name = found[1]
        hist = read_h5(market_code).iloc[-500:]
        history = [
            {"date": str(d.date()), "close": round(float(c), 3)}
            for d, c in zip(pd.to_datetime(hist["date"]), hist["close"])
        ]
    except Exception:
        history = []

    prediction = []
    for _, r in summary.iterrows():
        prediction.append({
            "date": str(pd.to_datetime(r["date"]).date()),
            "p10": round(float(r["close_p10"]), 3),
            "p25": round(float(r["close_p25"]), 3),
            "p50": round(float(r["close_p50"]), 3),
            "p75": round(float(r["close_p75"]), 3),
            "p90": round(float(r["close_p90"]), 3),
            "mean": round(float(r.get("close_mean", r["close_p50"])), 3),
        })

    last_close = float(history[-1]["close"]) if history else float(summary["close_p50"].iloc[0])
    final_p50 = float(summary["close_p50"].iloc[-1])
    return {
        "code": code,
        "name": name,
        "last_close": round(last_close, 3),
        "final_p50": round(final_p50, 3),
        "final_chg": round((final_p50 / last_close - 1) * 100, 2),
        "direction": "up" if final_p50 > last_close else "down",
        "prob": round(float((summary["close_p50"] > last_close).mean()), 4) if len(summary) else None,
        "hi_p90": round(float(summary["close_p90"].max()), 3),
        "lo_p10": round(float(summary["close_p10"].min()), 3),
        "start_date": str(pd.to_datetime(summary["date"].iloc[0]).date()),
        "end_date": str(pd.to_datetime(summary["date"].iloc[-1]).date()),
        "pred_len": int(len(summary)),
        "history": history,
        "prediction": prediction,
        "chart_url": f"/state/predicts/pred_{code.lower()}_chart.png"
        if os.path.exists(_predict_paths(code)[1]) else None,
    }


class PredictRunRequest(BaseModel):
    query: str = "600900"
    months: int = 6
    samples: int = 20
    lookback: int = 400
    device: str = "cuda:0"
    save_mysql: bool = True
    chart: bool = True


@app.post("/api/predicts/run")
async def api_predict_run(req: PredictRunRequest):
    """触发一次 Kronos 预测（同步执行，首次加载模型/下载权重耗时较长）"""
    if not _predict_lock.acquire(blocking=False):
        raise HTTPException(409, "已有预测任务正在执行，请稍候再试")
    try:
        # 确保模型权重可从镜像下载（本地缓存被清空时）
        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        from predict.run_predict import run_prediction
        result = run_prediction(
            req.query, months=req.months, samples=req.samples,
            lookback=req.lookback, device=req.device,
            chart=req.chart, save_mysql=req.save_mysql, verbose=False,
        )
        return {"success": True, "predict": result}
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, f"预测失败: {e}")
    finally:
        _predict_lock.release()


@app.delete("/api/predicts/{code}")
async def api_predict_delete(code: str):
    """删除指定股票的预测产物（CSV + 图表 PNG）"""
    csv_path, chart_path = _predict_paths(code)
    removed = []
    if os.path.exists(csv_path):
        os.remove(csv_path)
        removed.append(os.path.basename(csv_path))
    if os.path.exists(chart_path):
        os.remove(chart_path)
        removed.append(os.path.basename(chart_path))
    return {"success": True, "removed": removed}
