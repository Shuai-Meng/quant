"""基于 hikyuu 的个股分析工具。

用法（注意 hikyuu C++ 扩展在本机加载需 LD_PRELOAD，且 hikyuu 必须在所有第三方库之前导入）：

    LD_PRELOAD=/lib/x86_64-linux-gnu/libgcc_s.so.1 \
        .venv/bin/python -m datacenter.analyze_stock --code 600900

    LD_PRELOAD=/lib/x86_64-linux-gnu/libgcc_s.so.1 \
        .venv/bin/python -m datacenter.analyze_stock --code SH600900 --days 250
"""
import argparse
import sqlite3
import sys
from pathlib import Path

# ============================================================
# 注意：hikyuu 必须最先导入！
# 本机 glibc TLS 空间有限，若先导入 numpy/pandas/tables 再导入
# hikyuu 的 C++ 扩展 core312.so 会崩溃（cannot allocate memory in
# static TLS block），必须 LD_PRELOAD libgcc_s 并以 hikyuu 打头。
# ============================================================
import hikyuu as hk  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from datacenter.config import DATA_DIR  # noqa: E402

# 市场名前缀 → stock.db marketid
MKT_ID = {"SH": 1, "SZ": 2, "BJ": 3}


def read_h5_last(market_code: str):
    """从 H5 读取最后一根 K 线的原始成交额（千元）与成交量（手），换算为元/股。

    说明：hikyuu 2.8.2 读 H5 时 transAmount 按 ×0.1 换算（比真实值小 1 万倍，
    疑为其版本约定变更 bug），故成交额直接从 H5 原始值换算：千元→元。
    """
    import tables  # type: ignore

    market = market_code[:2].lower()
    p = DATA_DIR / f"{market}_day.h5"
    if not p.exists():
        return None, None
    try:
        with tables.open_file(str(p), "r") as f:
            t = f.get_node("/data", market_code)
            if t.nrows == 0:
                return None, None
            r = t[-1]
            amt_yuan = int(r["transAmount"]) * 1000.0   # 千元 → 元
            vol_share = int(r["transCount"]) * 100.0    # 手 → 股
            return amt_yuan, vol_share
    except Exception:
        return None, None


def find_stock(query: str):
    """根据代码或名称在 stock.db 中定位股票，返回 (market_code, name, marketid) 或 None。"""
    q = query.strip().upper()
    conn = sqlite3.connect(DATA_DIR / "stock.db")
    rows = None
    # 直接按表名匹配（SH600900 等）
    if len(q) == 8 and q[:2] in MKT_ID:
        rows = conn.execute(
            "SELECT code, name, marketid FROM stock WHERE code=? AND marketid=?",
            (q[2:], MKT_ID[q[:2]]),
        ).fetchall()
    # 按 6 位代码匹配
    if not rows and q.isdigit() and len(q) == 6:
        rows = conn.execute(
            "SELECT code, name, marketid FROM stock WHERE code=?", (q,)
        ).fetchall()
    # 按名称模糊匹配
    if not rows:
        rows = conn.execute(
            "SELECT code, name, marketid FROM stock WHERE name LIKE ?", (f"%{query}%",)
        ).fetchall()
    conn.close()
    if not rows:
        return None
    code, name, marketid = rows[0]
    market = {1: "SH", 2: "SZ", 3: "BJ"}[marketid]
    return f"{market}{code}", name, marketid


def _round(v):
    return round(float(v), 4) if v is not None else None


def analyze(market_code: str, name: str, recent_days: int = 250):
    """加载 K 线并计算技术指标，返回报告 dict。"""
    # 只加载目标股票，加速初始化
    hk.load_hikyuu(
        stock_list=[market_code.lower()],
        ktype_list=["day"],
        preload_num={"day_max": 100000},
        load_history_finance=False,
        load_weight=False,
        start_spot=False,
    )
    stk = hk.get_stock(market_code.lower())
    if stk is None or not stk.valid:
        raise RuntimeError(f"hikyuu 未找到有效股票 {market_code}")

    k = stk.get_kdata(hk.Query(0, None))
    n = len(k)
    if n < 50:
        raise RuntimeError(f"{market_code} 日线数据不足: {n} 条")

    c = hk.CLOSE(k)
    h_ = hk.HIGH(k)
    l_ = hk.LOW(k)
    v = hk.VOL(k)
    amt = hk.AMO(k)

    last_close = float(c[-1])
    prev_close = float(c[-2]) if n > 1 else last_close
    chg = (last_close - prev_close) / prev_close * 100

    # ---- 均线 ----
    mas = {}
    for win in (5, 10, 20, 60, 120, 250):
        if n >= win:
            mas[win] = float(hk.MA(c, win)[-1])
    ma_arr = [mas[w] for w in (5, 10, 20, 60) if w in mas]
    trend = "多头排列" if all(ma_arr[i] >= ma_arr[i + 1] for i in range(len(ma_arr) - 1)) else (
        "空头排列" if all(ma_arr[i] <= ma_arr[i + 1] for i in range(len(ma_arr) - 1)) else "纠缠")

    # ---- MACD ----
    macd = hk.MACD(c)
    dif, dea = float(macd.get_result(0)[-1]), float(macd.get_result(1)[-1])
    # hikyuu 2.8.2 的第三列柱 = DEA-DIF（符号与常规相反），仅用于展示
    mbar = float(macd.get_result(2)[-1])

    # ---- RSI ----
    rsi = {}
    for win in (6, 12, 24):
        if n > win:
            rsi[win] = float(hk.RSI(c, win)[-1])

    # ---- BOLL(20, 2) ----
    boll = {}
    try:
        mid = float(hk.MA(c, 20)[-1])
        sd = float(hk.STD(c, 20)[-1])
        boll = {"mid": mid, "up": mid + 2 * sd, "low": mid - 2 * sd}
    except Exception:
        pass

    # ---- ATR(14) ----
    atr = None
    try:
        tr = hk.TR(k) if hasattr(hk, "TR") else None
        if tr is not None and len(tr) >= 14:
            atr = float(hk.MA(tr, 14)[-1])
    except Exception:
        atr = None

    # ---- 区间统计 ----
    half = recent_days
    hi_52w = float(hk.HHV(h_, half)[-1])
    lo_52w = float(hk.LLV(l_, half)[-1])
    ret_20d = (last_close / float(c[-21]) - 1) * 100
    # 上市以来
    first_close = float(c[0])
    total_ret = (last_close / first_close - 1) * 100
    years = (int(str(k[-1].datetime)[:4]) - int(str(k[0].datetime)[:4])) or 1
    ann_ret = (pow(last_close / first_close, 1 / years) - 1) * 100 if last_close > 0 else 0

    last_amt, last_vol = read_h5_last(market_code)
    if last_amt is None:
        last_amt = float(amt[-1]) * 1e4  # hikyuu 读出值偏小 1e4，粗略还原
        last_vol = float(v[-1]) * 100.0  # 手 → 股

    return {
        "market_code": market_code, "name": name,
        "n": n, "start": str(k[0].datetime)[:10], "end": str(k[-1].datetime)[:10],
        "last_close": last_close, "prev_close": prev_close, "chg": chg,
        "mas": mas, "trend": trend,
        "dif": dif, "dea": dea, "mbar": mbar,
        "rsi": rsi, "boll": boll, "atr": atr,
        "hi_52w": hi_52w, "lo_52w": lo_52w,
        "ret_20d": ret_20d, "total_ret": total_ret, "ann_ret": ann_ret,
        "last_amt": last_amt, "last_vol": last_vol,
    }


def render(r: dict) -> str:
    lines = []
    A = lines.append
    A("=" * 64)
    A(f"  {r['name']}（{r['market_code']}）  hikyuu 技术分析")
    A("=" * 64)
    A(f"  数据区间    {r['start']} ~ {r['end']}（{r['n']} 条日线）")
    A(f"  最新收盘    {r['last_close']:.2f}  （{r['chg']:+.2f}%）")
    A("-" * 64)
    A("  均线系统")
    ma_txt = "  ".join(f"MA{w}={r['mas'][w]:.2f}" for w in sorted(r['mas']))
    A(f"    {ma_txt}")
    A(f"    趋势判断   {r['trend']}")
    A("-" * 64)
    A("  MACD(12,26,9)")
    A(f"    DIF={r['dif']:.4f}  DEA={r['dea']:.4f}  柱={r['mbar']:.4f}  "
      f"({'金叉' if r['dif'] > r['dea'] else '死叉'}区间)")
    A("  RSI")
    A(f"    RSI6={r['rsi'].get(6, float('nan')):.2f}  "
      f"RSI12={r['rsi'].get(12, float('nan')):.2f}  "
      f"RSI24={r['rsi'].get(24, float('nan')):.2f}")
    if r["boll"]:
        b = r["boll"]
        A(f"  BOLL(20,2)  上轨 {b['up']:.2f} / 中轨 {b['mid']:.2f} / 下轨 {b['low']:.2f}")
    if r["atr"]:
        A(f"  ATR(14)     {r['atr']:.3f}（{r['atr'] / r['last_close'] * 100:.2f}%）")
    A("-" * 64)
    A(f"  区间表现   近20日 {r['ret_20d']:+.2f}%   |   52周高 {r['hi_52w']:.2f} / 低 {r['lo_52w']:.2f}")
    A(f"  上市以来   {r['total_ret']:+.2f}%（年化 {r['ann_ret']:+.2f}%）")
    A(f"  最新成交   {r['last_amt'] / 1e8:.2f} 亿元  {r['last_vol'] / 1e6:.2f} 百万股")
    A("=" * 64)
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="hikyuu 个股技术分析")
    ap.add_argument("--code", required=True, help="代码/名称，如 600900、SH600900、sh600900、长江电力")
    ap.add_argument("--days", type=int, default=250, help="区间统计窗口（默认 250 交易日）")
    args = ap.parse_args()

    found = find_stock(args.code)
    if not found:
        print(f"未在 stock.db 中找到：{args.code}")
        sys.exit(1)
    market_code, name, _ = found
    print(f"已定位：{name}（{market_code}），正在加载 hikyuu 数据…", flush=True)

    r = analyze(market_code, name, recent_days=args.days)
    print(render(r))


if __name__ == "__main__":
    main()
