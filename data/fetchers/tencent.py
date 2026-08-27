"""腾讯财经API数据获取器

使用 qt.gtimg.cn 接口获取A股实时行情和历史K线数据。
零鉴权，不封IP，适合个人研究者。
"""
import time
import urllib.request
import urllib.parse
import re
import pandas as pd
import numpy as np
from .base import DataFetcher, retry


def _code_to_tencent(code):
    """将股票代码转为腾讯格式"""
    code = str(code).strip()
    if code.startswith("6") or code.startswith("9") or code.startswith("5"):
        # 沪市A股(6)/B股(9)/基金(5，如510300、588000、560010)
        return f"sh{code}"
    elif code.startswith("0") or code.startswith("1") or code.startswith("2") or code.startswith("3"):
        # 深市A股(0)/基金(1，如159915)/B股(2)/创业板(3)
        return f"sz{code}"
    elif code.startswith("4") or code.startswith("8"):
        return f"bj{code}"
    return code


def _code_from_tencent(tcode):
    """从腾讯格式转回标准代码"""
    if tcode.startswith("sh"):
        return f"{tcode[2:]}.SH"
    elif tcode.startswith("sz"):
        return f"{tcode[2:]}.SZ"
    elif tcode.startswith("bj"):
        return f"{tcode[2:]}.BJ"
    return tcode


class TencentFetcher(DataFetcher):
    """腾讯财经数据获取器"""

    def __init__(self):
        super().__init__("tencent")
        self._headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0",
        }

    @retry(max_attempts=3, delay=1.0)
    def get_realtime_quote(self, codes):
        """获取实时行情

        Parameters
        ----------
        codes : list
            股票代码列表，如 ['000001.SZ', '600000.SH']

        Returns
        -------
        DataFrame with columns:
            code, name, price, pe, pb, market_cap, turnover, amplitude, high, low, open
        """
        if isinstance(codes, str):
            codes = [codes]

        t_codes = [_code_to_tencent(c.split(".")[0]) for c in codes]
        url = "http://qt.gtimg.cn/q=" + ",".join(t_codes)

        req = urllib.request.Request(url, headers=self._headers)
        resp = urllib.request.urlopen(req, timeout=10)
        raw = resp.read().decode("gbk")

        rows = []
        for line in raw.strip().split(";"):
            if "=" not in line or '"' not in line:
                continue
            key, val = line.split("=", 1)
            # 响应键名 v_sh510300 -> sh510300（vals[0] 是市场标志，非代码）
            t_code = key.split("_", 1)[-1] if "_" in key else key
            vals = val.split('"')[1].split("~")
            if len(vals) < 50:
                continue

            rows.append(
                {
                    "code": _code_from_tencent(t_code),
                    "name": vals[1],
                    "open": float(vals[5]) if vals[5] else 0,
                    "close": float(vals[3]) if vals[3] else 0,
                    "high": float(vals[33]) if vals[33] else 0,
                    "low": float(vals[34]) if vals[34] else 0,
                    "volume": float(vals[6]) if vals[6] else 0,
                    "amount": float(vals[37]) if vals[37] else 0,
                    "pe": float(vals[39]) if vals[39] else 0,
                    "pb": float(vals[55]) if vals[55] else 0,
                    "market_cap": float(vals[45]) if vals[45] else 0,
                    "total_shares": float(vals[44]) if vals[44] else 0,
                    "turnover": float(vals[38]) / 100 if vals[38] else 0,
                    "amplitude": float(vals[43]) if vals[43] else 0,
                    "change_pct": float(vals[32]) if vals[32] else 0,
                    "change_amt": float(vals[31]) if vals[31] else 0,
                }
            )

        return pd.DataFrame(rows)

    @retry(max_attempts=2, delay=2.0)
    def get_daily(self, code, start="20200101", end="20260515"):
        """获取日K线数据

        使用腾讯财经的日K线接口。

        Parameters
        ----------
        code : str
            股票代码，如 '000001.SZ'
        start, end : str
            日期范围 YYYYMMDD

        Returns
        -------
        DataFrame with date, open, high, low, close, volume
        """
        t_code = _code_to_tencent(code.split(".")[0])
        # Convert date format from YYYYMMDD to YYYY-MM-DD for the API
        _s = f"{start[:4]}-{start[4:6]}-{start[6:8]}" if len(start) == 8 else start
        _e = f"{end[:4]}-{end[4:6]}-{end[6:8]}" if len(end) == 8 else end
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={t_code},day,{_s},{_e},640,qfq"

        req = urllib.request.Request(url, headers=self._headers)
        resp = urllib.request.urlopen(req, timeout=10)
        import json

        data = json.loads(resp.read().decode("utf-8"))

        rows = []
        for key in ["data", data.get("code", t_code), "day"]:
            try:
                kl = data[key]
                break
            except (KeyError, TypeError):
                continue

        # Try different key paths
        if "data" in data:
            d = data["data"]
            stock_data = None
            for k in [t_code, code]:
                if k in d:
                    stock_data = d[k]
                    break
            if stock_data and "day" in stock_data:
                for item in stock_data["day"]:
                    rows.append(
                        {
                            "date": item[0],
                            "open": float(item[1]),
                            "close": float(item[2]),
                            "high": float(item[3]),
                            "low": float(item[4]),
                            "volume": float(item[5]) if len(item) > 5 else 0,
                        }
                    )
            elif stock_data and "qfqday" in stock_data:
                for item in stock_data["qfqday"]:
                    rows.append(
                        {
                            "date": item[0],
                            "open": float(item[1]),
                            "close": float(item[2]),
                            "high": float(item[3]),
                            "low": float(item[4]),
                            "volume": float(item[5]) if len(item) > 5 else 0,
                        }
                    )

        if rows:
            df = pd.DataFrame(rows)
            df["date"] = pd.to_datetime(df["date"])
            df["code"] = code
            return df.sort_values("date").reset_index(drop=True)

        return pd.DataFrame()

    def get_batch_kline(self, codes, start="20200101", end="20260515"):
        """批量获取K线数据"""
        dfs = []
        for code in codes:
            try:
                df = self.get_daily(code, start, end)
                if not df.empty:
                    dfs.append(df)
                time.sleep(0.1)
            except Exception as e:
                print(f"  {code}: {e}")
        if dfs:
            return pd.concat(dfs, ignore_index=True)
        return pd.DataFrame()
