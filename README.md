# 🏭 A股本地量化交易系统

## 架构

```
┌─────────────────────────────────────────────┐
│  data/fetchers/      数据获取层              │
│  ├── tencent.py      腾讯API（实时+历史K线）  │
│  ├── akshare.py      AkShare包装器           │
│  ├── hexin.py        同花顺（热点+北向资金）  │
│  └── eastmoney.py    东财（龙虎榜+资金流向）  │
├─────────────────────────────────────────────┤
│  data/cleaners/      数据清洗层              │
│  ├── price.py        复权/收益率/涨跌停标记  │
│  ├── financial.py    财务日期对齐（防未来函） │
│  └── universe.py     ST/次新/行业过滤        │
├─────────────────────────────────────────────┤
│  factors/            因子层                  │
│  ├── technical/      动量/反转/量比/RSI/均线 │
│  ├── fundamental/    BP/EP价值 + ROE质量     │
│  ├── behavioral/     换手率/振幅/题材热度    │
│  ├── tests/          SingleFactorTester      │
│  │                   排序法+FM回归+IC/IR     │
│  └── synthesis/      FactorCombiner多因子合成│
├─────────────────────────────────────────────┤
│  backtest/           回测层                  │
│  ├── engine.py       向量化回测引擎          │
│  ├── cost.py         交易成本模型            │
│  └── performance.py  绩效指标+报告           │
├─────────────────────────────────────────────┤
│  signals/            信号层                  │
│  └── generate.py     每日信号生成            │
├─────────────────────────────────────────────┤
│  strategies/         策略层                  │
│  └── multi_factor.py 多因子/动量/反转策略    │
├─────────────────────────────────────────────┤
│  risk/               风控层                  │
│  └── risk_manager.py  ATR/凯利/熔断/VaR     │
│     position_sizing.py 仓位计算             │
├─────────────────────────────────────────────┤
│  utils/              工具层                  │
│     winsorize.py     去极值(MAD/分位数)     │
│     standardize.py   z-score标准化          │
│     neutralize.py    行业/因子中性化         │
│     calendar.py      交易日历                │
├─────────────────────────────────────────────┤
│  pipeline.py         主流程（端到端流水线）   │
│  run_factor.py       单因子检验入口          │
│  run_backtest.py     多因子回测入口          │
│  run_signals.py      每日信号生成入口        │
└─────────────────────────────────────────────┘
```

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 1. 单因子检验（先用模拟数据试跑）
python run_factor.py --factor momentum

python run_factor.py --factor reversal

python run_factor.py --factor volume_ratio

python run_factor.py --factor rsi

# 2. 多因子回测
python run_backtest.py

# 3. 每日信号（连接真实数据）
python run_signals.py
```

## 方法论依据

本系统基于微信公众号专栏《A股因子投资实战》系列文章实现：

| 章节 | 文章 | 系统模块 |
|:----|:-----|:--------|
| 1.1 | 因子的本质：风险补偿 vs 市场无效 | 因子选择原则 |
| 1.2 | 多因子模型演进：CAPM→FF→Barra | factors/synthesis/ |
| 1.3 | A股特殊性 | data/cleaners/ |
| 1.4 | 实战流程全景图 | pipeline.py |
| 2.1 | 排序法+FM回归 | factors/tests/ |
| 2.2 | 辨别伪因子 | backtest/ + factors/tests/ |
| 2.3 | 数据源详解 | data/fetchers/ |
| 2.4 | 价值因子复现案例 | 架构示范 |
| 3.1 | 标准化五步流程 | factors/tests/ |
| 3.2 | 代码模板：SingleFactorTester | factors/tests/single_factor_tester.py |
| 3.3 | 价值因子深度剖析 | factors/fundamental/ |

## Skills 对接

- **a-stock-data** → 数据获取代码参考
- **backtest-expert** → 过拟合检查方法论
- **jl-wealth-management** → bet-sizing/ATR止损
- **trading-signals** → 信号监控模式

## 数据源（零鉴权，不封IP）

| 数据 | 来源 | 接口 |
|:----|:----|:----|
| 实时行情 | qt.gtimg.cn（腾讯） | tencent.py |
| 日K线 | web.ifzq.gtimg.cn（腾讯） | tencent.py |
| 同花顺热点 | 10jqka.com | hexin.py |
| 北向资金 | data.hexin.cn | hexin.py |
| 龙虎榜 | eastmoney.com | eastmoney.py |
| 股票列表/行业 | akshare | akshare_fetcher.py |

## License

MIT
