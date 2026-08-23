# datacenter — A 股日线数据基座

基于 hikyuu（C++/Python 量化框架）+ pytdx（通达信协议）的 A 股全市场日线数据采集与存储模块。

## 数据架构

```
K 线数据      -> HDF5 文件（quant/data/hkstore/sh_day.h5、sz_day.h5、bj_day.h5）
股票代码表    -> SQLite（stock.db，含沪深北股票 + 指数 + 权息 + 财务，hikyuu 直连）
板块定义      -> SQLite（block 表，行业/概念/地域板块）
业务元数据    -> MySQL（quant 库：strategy / backtest_run / kronos_signal / import_log）
```

选型说明：hikyuu 支持 HDF5 / MySQL / SQLite 三种 K 线驱动，**日线场景推荐 HDF5**
（内存映射读取最快、零依赖、约 4000 万条日线仅 1.5~2GB）；MySQL 不适合 K 线主存储
（行存储膨胀、分钟线性能差），但适合放业务元数据（策略配置、回测结果、预测信号）。
业务库连接参数在 `datacenter/config.py`（`MYSQL_*`，密码可用环境变量
`HK_MYSQL_PASSWORD` 覆盖），MySQL 不可用时自动降级，不影响 K 线导入。

## 使用步骤

```bash
cd /home/ms/programs/finace/quant

# 1. 初始化（生成 hikyuu.ini、stock.db、板块定义、MySQL 业务库）
.venv/bin/python -m datacenter.init

# 单独初始化 MySQL 业务库（幂等，可重复执行）
.venv/bin/python -m datacenter.mysql_db --init

# 2. 首次全量导入（沪深北全市场日线 + 代码表 + 权息 + 财务，约 2 小时）
#    内置防封 IP 限速：请求间隔 0.5s、每分钟 ≤120 次、每 200 只休息 20s
.venv/bin/python -m datacenter.import_full

# 冒烟测试（只导 3 只样本股票，验证链路）
.venv/bin/python -m datacenter.import_full --smoke

# 小步快跑：本次只导 500 只，剩余下次再跑（H5 增量幂等，自动续传）
.venv/bin/python -m datacenter.import_full --limit-stocks 500

# 3. 每日增量更新（从「上次成功拉取日期」续拉到今天，停机数日自动补齐缺口；
#    重复运行安全，同样限速。SH/SZ 走 pytdx，BJ 走 akshare 双数据源（东财优先，失败自动切新浪））
.venv/bin/python -m datacenter.daily_update

# 手动补数：从指定日期补拉到今天（覆盖 state.json 的上次记录日期）
.venv/bin/python -m datacenter.daily_update --force --from-date 20260701

# 4. 数据完整性校验（覆盖度统计 + 抽样新鲜度检查）
.venv/bin/python -m datacenter.verify

# 全市场逐股缺口检测（对照 A 股交易日历，全量；约 1-2 分钟）
.venv/bin/python -m datacenter.verify --check-gaps

# 只看最近 N 天（增量健康监控，忽略历史存量缺口噪声）
.venv/bin/python -m datacenter.verify --check-gaps --recent-days 14

# 5. 存量缺口修复（修复 verify --check-gaps 报告的真实缺口，如 2007-09-05 系统性缺日）
#    幂等安全：停牌日/数据源无数据自动跳过；冒烟先跑前 50 只
.venv/bin/python -m datacenter.repair_gaps --limit-stocks 50
.venv/bin/python -m datacenter.repair_gaps            # 全量
```

## 每日自动更新（Python 标准库调度器，无需 cron）

`datacenter/scheduler.py` 用标准库 `sched` 实现常驻调度器，两个任务：
每个交易日 17:30 以子进程触发 `daily_update`（隔离运行；非交易日由内部交易日历
自动跳过）；每周五 17:40 以子进程触发 `verify --check-gaps --recent-days 14`
（增量缺口健康检测，漏拉/停机当周即暴露）。子进程 stdout/stderr 回传调度器日志。

```bash
# 后台常驻启动（服务器重启后需重新启动，或用 systemd/@reboot 挂自启）
# 注意：stdout 重定向到独立文件，勿与 scheduler.log 同名（会重复行）
nohup .venv/bin/python -m datacenter.scheduler >> data/logs/scheduler_stdout.log 2>&1 &

# 停止
pkill -f "datacenter.scheduler"

# 部署自检：立即执行一次 daily_update 后退出（验证链路）
.venv/bin/python -m datacenter.scheduler --once

# 不想用常驻进程，也可以直接调 daily_update（可配合任何外部调度）
.venv/bin/python -m datacenter.daily_update
```

调度器日志：`quant/data/logs/scheduler.log`（超 5MB 自动轮转归档）。
状态记录：`quant/data/state/datacenter_state.json`，其中 `last_data_date`（按市场）
记录最近一次确认成功更新到的数据日期，下次运行自动从该日期的下一天续拉到当天——
停机数日/数周重跑会自动补齐整个缺口，而不是只拉当天。记录只在实际写入新数据时前进，
`--limit-stocks` 冒烟模式不更新该记录。
补数：服务器停机后自动补齐缺口；手动补数可 `--force` 跳过交易日检查，
必要时加 `--from-date YYYYMMDD` 指定补数起点。

## 常用参数

| 命令 | 参数 | 说明 |
|---|---|---|
| import_full | `--markets SH,SZ` | 只导入指定市场 |
| import_full | `--smoke` | 冒烟测试（3 只样本） |
| import_full | `--skip-weight` | 跳过权息导入 |
| import_full | `--limit-stocks 500` | 本次只导 500 只（分批续传） |
| import_full | `--batch-size 100` / `--batch-break 30` | 调整分批节奏（防封 IP） |
| daily_update | `--markets SH` | 只更新指定市场 |
| daily_update | `--source sina` | BJ 数据源直连新浪（东财被限流时更快） |
| daily_update | `--force` | 忽略交易日检查（手动补数） |
| daily_update | `--from-date 20260701` | 覆盖上次记录日期，从该日补拉到今天（手动补数） |
| daily_update | `--limit-stocks 20` | 每市场只处理前 20 只（冒烟，不更新 last_data_date） |
| daily_update | `--skip-names` / `--skip-weight` | 跳过代码表 / 权息更新 |
| verify | `--samples 50` | 抽样股票数 |
| verify | `--check-gaps` | 全市场逐股连续性缺口检测（对照 A 股交易日历，全量） |
| verify | `--recent-days 90` | 缺口检测只看最近 N 天（增量健康监控，忽略历史存量缺口） |
| verify | `--limit-stocks 500` | 缺口检测只处理前 500 只（调试） |
| repair_gaps | `--limit-stocks 50` | 只修复前 N 只缺口标的（冒烟） |
| repair_gaps | `--markets SH,BJ` | 只修复指定市场 |
| repair_gaps | `--sleep 2.0` | 调整请求间隔（新浪默认 1.2s） |

## 数据缺口检测机制（verify --check-gaps）

原理：对每只股票读 H5 的 datetime 列，用 `np.searchsorted` 把每个日期映射到
A 股交易日历（akshare，缓存于 `data/state/trade_calendar.json`）的下标，
相邻 K 线下标差减 1 即为跳过的交易日数。>0 即缺口候选；再按缺失日期聚合，
单日大量股票同缺 = 系统性缺日（数据源漏拉），零星缺失 = 大概率停牌。
全量扫描 1900 万条日线约 1-2 分钟，逐股明细落盘 `data/logs/gap_report.json`。

存量缺口修复（`repair_gaps.py`）：从 `gap_report.json` 聚合缺口标的，对每只
取其全部缺口区间 [最早起点, 最晚终点] 一次拉取，整表合并重建（支持插入历史
中间缺口，保证表内 datetime 有序）。个股走新浪 `stock_zh_a_daily`，指数
（SH 000xxx / SZ 399xxx）走 `stock_zh_index_daily`。停牌日（数据源无当日行情）
与数据源本身缺失的历史自动跳过。首次修复发现 3302 只标的 / 81954 段缺口，
含 2007-09-05（1355 只）、2000-02-29（794 只）等系统性漏拉日与 1991-1993
早期序列；2015-07 千股停牌为正常历史事件（拉不到数据，自动跳过）。
修复完成后重跑 `verify --check-gaps` 验证。

已知限制：新浪接口对极早期（1990s）部分标的可能无数据，此类缺口无法修复，
会保留在报告中（属数据源本身缺失）。

## 防封 IP 限速机制

所有通达信联网请求经 `rate_limit.py` 的 `RateLimitedAPI` 统一节流：

- **相邻请求最小间隔** 0.5s（`TDX_REQUEST_INTERVAL`）
- **每分钟请求数上限** 120 次（`TDX_MAX_REQ_PER_MIN`，滑动窗口，超标自动等待）
- **每批 200 只股票后休息 20s**（`BATCH_SIZE` / `BATCH_BREAK`），并打印进度/速率/ETA
- 每只股票后额外休息 0.2s（`STOCK_BREAK`）
- 连续失败 ≥50 只自动停止该市场（`MAX_CONSECUTIVE_FAIL`），避免服务器异常时空转

上述参数都在 `datacenter/config.py` 中，可按需调整。全市场约 2.5 万次请求，
按当前限速全程约 2 小时。进程锁（`/tmp/datacenter_import.lock`）防止多实例并发写 H5
（并发会导致 HDF5 文件锁冲突，曾导致导入静默失败）。

## MySQL 业务库

| 表 | 用途 |
|---|---|
| `strategy` | 策略配置（名称/说明/参数 JSON） |
| `backtest_run` | 回测记录（区间/参数/结果 JSON） |
| `kronos_signal` | Kronos 预测信号（每股每日一条，unique 约束防重复） |
| `import_log` | 数据导入日志（每次 full/daily 导入自动写入） |

连接参数：`127.0.0.1:3306`，用户 `quant`，库 `quant`（可在 `config.py` 修改，
密码用环境变量 `HK_MYSQL_PASSWORD` 覆盖）。每次全量/增量导入结束会自动向
`import_log` 写入一条记录（MySQL 不可用时静默降级，不阻塞导入）。

## 数据使用（hikyuu 运行时）

```python
# 注意（本机实测）：
# 1) hikyuu 必须最先导入！若先导入 numpy/pandas/tables 再导入 hikyuu，
#    glibc TLS 空间不足会崩溃（cannot allocate memory in static TLS block）
# 2) 运行前需 LD_PRELOAD libgcc_s（见 analyze_stock.sh）
# 3) 2.8.2 已无 hk.init()，须显式 load_hikyuu() 加载数据驱动

import hikyuu as hk
hk.load_hikyuu(
    stock_list=["sh600900"],        # 只加载目标股票，加速
    ktype_list=["day"],
    preload_num={"day_max": 100000},
    load_history_finance=False,
    load_weight=False,
    start_spot=False,
)
stk = hk.get_stock("sh600900")
k = stk.get_kdata(hk.Query(0, None))   # 全部日线；Query(-250, None) 取最近 250 根
c = hk.CLOSE(k)
ma20 = hk.MA(c, 20)
macd = hk.MACD(c)
rsi = hk.RSI(c)
```

已封装好的个股分析工具（含均线/MACD/RSI/BOLL/ATR/区间统计）：

```bash
cd /home/ms/programs/finace/quant
./analyze_stock.sh --code 600900            # 按代码（自动识别沪深北）
./analyze_stock.sh --code 长江电力          # 按名称模糊匹配
./analyze_stock.sh --code SH600900 --days 120
```

## hikyuu 2.8.2 本机已知问题

- **TLS 崩溃**：`LD_PRELOAD=/lib/x86_64-linux-gnu/libgcc_s.so.1` + hikyuu 最先导入
  （`analyze_stock.sh` 已封装）。否则 import 或调用 C++ 扩展时核心转储。
- **必须 load_hikyuu()**：仅 `import hikyuu` 时 StockManager 未初始化，`get_stock`
  返回的 Stock 内部状态不完整，`set_krecord_list`/`set_kdata_from_df` 会崩溃
  （崩溃点在 Stock::setKRecordList 访问未初始化的 mutex 表）。
- **成交额单位 bug**：2.8.2 读 H5 时 `transAmount × 0.1`，比真实值小 1 万倍
  （H5 存储约定为千元）。价格、成交量不受影响。展示成交额请从 H5 原始值换算
  （见 `analyze_stock.py::read_h5_last`：`transAmount×1000` 元、`transCount×100` 股）。
- **MACD 柱符号**：2.8.2 第三列柱 = `DEA-DIF`（与常规相反），判断金叉/死叉请
  直接比较 `DIF` 与 `DEA`。

## 已知限制

- **北交所（BJ）**：pytdx 公共服务器不提供 BJ 行情，故每日更新中 BJ 走 akshare
  双数据源（`stock_zh_a_hist` 东财优先，失败自动切 `stock_zh_a_daily` 新浪；
  新浪成交量单位为"股"，脚本已 /100 转"手"）。东财被限流时可用 `--source sina` 直连新浪。
- 首次全量导入依赖 akshare 拉取股票代码表，需联网且耗时几分钟。
- 数据源为通达信公共行情服务器，盘中数据以收盘为准；建议收盘后（17:30 后）更新。
