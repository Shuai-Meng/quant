"""AkShare数据获取器包装

提供股票列表、行业分类、财务数据、新闻等数据获取。
"""
import time
import os
import pandas as pd
import numpy as np
from .base import DataFetcher, retry


def _clear_proxy():
    """清除代理环境变量，akshare通过代理连接EastMoney不稳定"""
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                "ALL_PROXY", "all_proxy", "SOCKS_PROXY", "socks_proxy"):
        if key in os.environ:
            del os.environ[key]


class AkShareFetcher(DataFetcher):
    """AkShare数据获取器"""

    def __init__(self):
        super().__init__("akshare")
        _clear_proxy()

    @retry(max_attempts=3, delay=1.0)
    def get_stock_list(self):
        """获取A股股票列表"""
        import akshare as ak

        df = ak.stock_info_a_code_name()
        df = df.rename(columns={"code": "stock_code", "name": "stock_name"})
        return df

    @retry(max_attempts=3, delay=1.0)
    def get_industry_classification(self):
        """获取行业分类（同花顺行业）"""
        import akshare as ak

        df = ak.stock_board_industry_name_ths()
        return df

    @retry(max_attempts=3, delay=1.0)
    def get_stock_board_member(self, board_name):
        """获取同花顺行业板块成分股"""
        import akshare as ak

        df = ak.stock_board_industry_cons_ths(symbol=board_name)
        return df

    @retry(max_attempts=3, delay=2.0)
    def get_financial_data(self, code, start="20200101", end="20260515"):
        """获取个股财务数据"""
        import akshare as ak

        try:
            df = ak.stock_financial_abstract_ths(symbol=code, indicator="按报告期")
            if df is not None and not df.empty:
                df.columns = [str(c).strip() for c in df.columns]
                return df
        except Exception:
            pass
        return pd.DataFrame()

    @retry(max_attempts=3, delay=1.0)
    def get_hot_topics(self, date=None):
        """获取同花顺热点板块"""
        import akshare as ak

        df = ak.stock_board_concept_name_ths()
        return df

    @retry(max_attempts=3, delay=1.0)
    def get_live_news(self):
        """获取财联社实时快讯"""
        import akshare as ak

        df = ak.stock_info_global_cls()
        return df

    @retry(max_attempts=2, delay=1.0)
    def get_stock_info(self, code):
        """获取个股基本信息"""
        import akshare as ak

        try:
            if code.endswith(".SH") or code.endswith(".SZ"):
                code = code.split(".")[0]
            df = ak.stock_individual_info_em(symbol=code)
            return df
        except Exception as e:
            return pd.DataFrame()

    def get_all_stock_daily(self, codes, start="20200101", end="20260515"):
        """批量获取个股日行情"""
        import akshare as ak

        dfs = []
        for code in codes:
            try:
                symbol = code.split(".")[0] if "." in code else code
                df = ak.stock_zh_a_hist(
                    symbol=symbol, period="daily",
                    start_date=start, end_date=end, adjust="qfq"
                )
                if not df.empty:
                    df.columns = [str(c).strip() for c in df.columns]
                    df["code"] = code
                    df["date"] = pd.to_datetime(df["日期"])
                    df = df.rename(
                        columns={
                            "开盘": "open", "收盘": "close", "最高": "high",
                            "最低": "low", "成交量": "volume", "成交额": "amount",
                            "换手率": "turnover", "振幅": "amplitude",
                        }
                    )
                    dfs.append(df)
                time.sleep(0.3)
            except Exception as e:
                print(f"  {code}: {e}")
        if dfs:
            return pd.concat(dfs, ignore_index=True)
        return pd.DataFrame()
