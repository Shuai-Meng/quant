"""预测引擎（Kronos 逻辑整合模块）。

- kronos_engine.KronosEngine: 基于 Kronos 基础模型的个股价格走势预测
- run_predict: 命令行入口（python -m predict.run_predict）

设计约定：
- 数据直接读数据基座 HDF5（data/hkstore/*_day.h5），不经过 CSV 中转
- 本模块不 import hikyuu（避免 C++ 扩展与 torch 的 TLS 加载顺序问题），
  股票定位使用 sqlite3 直查 stock.db
- Kronos 模型代码按 vendor 路径引用（datacenter.config.KRONOS_HOME）
"""
