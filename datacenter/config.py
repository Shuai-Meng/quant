"""数据基座路径与常量配置。"""
import os
from pathlib import Path

# 项目根目录（quant/）
QUANT_ROOT = Path(__file__).resolve().parent.parent

# hikyuu 数据目录（HDF5 + stock.db + block），可用环境变量覆盖
DATA_DIR = Path(os.getenv("HK_DATA_DIR", str(QUANT_ROOT / "data" / "hkstore")))

# hikyuu 用户配置目录
HIKYUU_HOME = Path.home() / ".hikyuu"
INI_PATH = HIKYUU_HOME / "hikyuu.ini"

# 运行日志目录
LOG_DIR = QUANT_ROOT / "data" / "logs"

# 状态记录（最近一次导入时间等）
STATE_FILE = QUANT_ROOT / "data" / "state" / "datacenter_state.json"

# A 股三个市场（hikyuu 支持的 pytdx 市场）
MARKETS = ["SH", "SZ", "BJ"]

# A 股日线起始日期（1990-12-19 深交所开市），hikyuu int 格式 YYYYMMDDHHMM
START_DATE = 199012190000

# 通达信服务器连接超时（秒）
TDX_TIMEOUT = 3

# 首次全量导入时 K 线数据起始日期；增量更新时自动从 H5 最后一条继续
DEFAULT_START_DATE = 199012190000

# ===== 通达信服务器请求限速（避免被限流/拉黑 IP）=====
# 相邻两次网络请求的最小间隔（秒）
TDX_REQUEST_INTERVAL = 0.5
# 每分钟请求数硬上限（滑动窗口，超过则阻塞等待）
TDX_MAX_REQ_PER_MIN = 120
# 每只股票导入完成后的休息（秒）；0 表示不额外休息（请求间隔已保证节奏）
STOCK_BREAK = 0.2
# 每批处理的股票数（达到后进入批间休息）
BATCH_SIZE = 200
# 批与批之间的休息（秒），让服务器喘息
BATCH_BREAK = 20
# 连续失败达到该数量后停止当前市场（服务器异常时避免空转）
MAX_CONSECUTIVE_FAIL = 50

# ===== MySQL 业务库（策略配置/回测记录/Kronos 信号/导入日志）=====
# 定位：K 线主存储用 HDF5，代码表用 SQLite（hikyuu 直连），
# MySQL 承载业务元数据。密码可用环境变量 HK_MYSQL_PASSWORD 覆盖。
MYSQL_HOST = os.getenv("HK_MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("HK_MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("HK_MYSQL_USER", "quant")
MYSQL_PASSWORD = os.getenv("HK_MYSQL_PASSWORD", "qt3302")
MYSQL_DB = os.getenv("HK_MYSQL_DB", "quant")

# mysql-connector-python 连接参数
MYSQL_CONFIG = {
    "host": MYSQL_HOST,
    "port": MYSQL_PORT,
    "user": MYSQL_USER,
    "password": MYSQL_PASSWORD,
    "database": MYSQL_DB,
    "charset": "utf8mb4",
    "autocommit": False,
    "use_pure": True,
}
