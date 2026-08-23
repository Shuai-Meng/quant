"""数据基座：基于 hikyuu 的 A 股全市场日线数据采集与存储。

模块结构:
    config.py       路径与常量配置
    init.py         初始化 hikyuu.ini / stock.db / HDF5 存储
    import_full.py  首次全量导入（股票代码表 + 全市场日线 + 权息 + 财务）
    daily_update.py 每日增量更新（SH/SZ pytdx + BJ akshare 双源）
    scheduler.py    常驻调度器（标准库 sched，交易日 17:30 自动触发 daily_update）
    verify.py       数据完整性校验
"""

__version__ = "0.1.0"
