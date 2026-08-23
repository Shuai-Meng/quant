"""初始化数据基座：创建目录、hikyuu.ini、stock.db 与板块定义。

用法: python -m datacenter.init
"""
import logging
import shutil
import sqlite3
import sys
from pathlib import Path

from .config import DATA_DIR, HIKYUU_HOME, INI_PATH, LOG_DIR, STATE_FILE

logger = logging.getLogger("datacenter.init")


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_DIR / "datacenter_init.log", encoding="utf-8"),
        ],
    )


def create_hikyuu_ini() -> None:
    """生成 hikyuu.ini（HDF5 存储方案），指向 DATA_DIR。"""
    from hikyuu.data.hku_config_template import hdf5_template

    HIKYUU_HOME.mkdir(parents=True, exist_ok=True)
    ini = hdf5_template.format(
        dir=DATA_DIR,
        reload_time="00:00",
        quotation_server="ipc:///tmp/hikyuu_real.ipc",
        lazy_preload="False",
        day=True,
        week=True,
        month=True,
        quarter=False,
        halfyear=False,
        year=False,
        min1=False,
        min5=False,
        min15=False,
        min30=False,
        min60=False,
        hour2=False,
        timeline=False,
        trans=False,
        day_max=100000,
        week_max=100000,
        month_max=100000,
        quarter_max=100000,
        halfyear_max=100000,
        year_max=100000,
        min1_max=5120,
        min5_max=5120,
        min15_max=5120,
        min30_max=5120,
        min60_max=5120,
        hour2_max=5120,
        timeline_max=5120,
        trans_max=5120,
    )
    INI_PATH.write_text(ini, encoding="utf-8")
    logger.info("已生成 hikyuu.ini -> %s (数据目录: %s)", INI_PATH, DATA_DIR)


def copy_block_files() -> None:
    """复制 hikyuu 自带的板块定义（zsbk/hybk/dybk/gnbk 等 ini）到数据目录。"""
    import hikyuu

    src = Path(hikyuu.__file__).resolve().parent / "config" / "block"
    dst = DATA_DIR / "block"
    if dst.exists():
        logger.info("板块目录已存在: %s", dst)
        return
    if src.exists():
        shutil.copytree(src, dst)
        (dst / "__init__.py").unlink(missing_ok=True)
        logger.info("已复制板块定义 -> %s", dst)
    else:
        logger.warning("未找到 hikyuu 自带的板块定义目录: %s", src)


def create_stock_db() -> None:
    """创建/升级 stock.db（股票代码表、权息、财务数据表结构）。"""
    from hikyuu.data.pytdx_to_h5 import create_database

    db_path = DATA_DIR / "stock.db"
    conn = sqlite3.connect(db_path)
    try:
        create_database(conn)
        conn.commit()
        logger.info("已初始化 stock.db -> %s", db_path)
    finally:
        conn.close()


def init_mysql_biz_db() -> None:
    """初始化 MySQL 业务库（策略/回测/Kronos 信号/导入日志）。不可用时降级。"""
    from .mysql_db import init_biz_db

    if not init_biz_db():
        logger.warning("MySQL 业务库不可用，业务元数据将不落库（K 线导入不受影响）")


def main() -> None:
    _setup_logging()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    create_hikyuu_ini()
    copy_block_files()
    create_stock_db()
    init_mysql_biz_db()
    logger.info("数据基座初始化完成。下一步: python -m datacenter.import_full")


if __name__ == "__main__":
    main()
