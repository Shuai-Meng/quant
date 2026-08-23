"""组件化交易系统

借鉴 Hikyuu 的系统分解模式，将策略拆分为可插拔组件：
  System = { Environment + Condition + Signal +
             MoneyManager + StopLoss + ProfitGoal + Slippage }
"""
