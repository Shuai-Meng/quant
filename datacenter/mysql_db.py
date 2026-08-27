"""MySQL 业务库：策略配置 / 回测记录 / Kronos 预测信号 / 数据导入日志。

存储分工:
    - K 线主存储      -> HDF5（data/hkstore/*_day.h5）
    - 代码表/权息/财务 -> SQLite（stock.db，hikyuu 直接连接）
    - 业务元数据      -> MySQL（本模块，quant 库）

MySQL 不可用时不阻塞任何流程：所有函数内部捕获异常并降级为警告日志，
K 线导入照常进行。

用法:
    python -m datacenter.mysql_db --init     # 初始化数据库与业务表（幂等）
"""
import argparse
import logging
import time

import mysql.connector

from .config import MYSQL_CONFIG

logger = logging.getLogger("datacenter.mysql_db")

# 建库语句（仅 --init 时执行；业务库须在连接前已存在）
CREATE_DATABASE_SQL = (
    "CREATE DATABASE IF NOT EXISTS `{db}` "
    "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci"
).format(db=MYSQL_CONFIG["database"])

# 业务表 DDL（幂等：IF NOT EXISTS）
DDL_STATEMENTS = [
    # 策略配置：策略名唯一
    """
    CREATE TABLE IF NOT EXISTS strategy (
        id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
        name VARCHAR(128) NOT NULL COMMENT '策略名称',
        description VARCHAR(512) DEFAULT NULL COMMENT '策略说明',
        config_json JSON NULL COMMENT '策略参数配置',
        status TINYINT NOT NULL DEFAULT 1 COMMENT '1启用 0停用',
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uk_name (name)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='策略配置'
    """,
    # 回测记录：每次回测一行，结果以 JSON 保存
    """
    CREATE TABLE IF NOT EXISTS backtest_run (
        id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
        strategy_id INT UNSIGNED NULL COMMENT '关联 strategy.id',
        strategy_name VARCHAR(128) NOT NULL COMMENT '策略名（冗余，防策略删除）',
        start_date VARCHAR(10) NULL COMMENT '回测区间起始 YYYY-MM-DD',
        end_date VARCHAR(10) NULL COMMENT '回测区间结束 YYYY-MM-DD',
        params_json JSON NULL COMMENT '本次回测参数',
        result_json JSON NULL COMMENT '回测结果（收益/回撤/胜率等）',
        status TINYINT NOT NULL DEFAULT 1 COMMENT '1成功 0失败',
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        KEY idx_strategy (strategy_id),
        KEY idx_created (created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='回测记录'
    """,
    # Kronos 预测信号：每股每日一条
    """
    CREATE TABLE IF NOT EXISTS kronos_signal (
        id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
        stock_code VARCHAR(12) NOT NULL COMMENT '证券代码 如 sh600000',
        trade_date DATE NOT NULL COMMENT '交易日',
        signal_type VARCHAR(32) NOT NULL COMMENT 'up/down/flat',
        probability DECIMAL(6,4) NULL COMMENT '预测概率(0~1)',
        features_json JSON NULL COMMENT '输入特征快照',
        model_version VARCHAR(64) DEFAULT NULL COMMENT '模型版本',
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uk_stock_date (stock_code, trade_date),
        KEY idx_date (trade_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Kronos 预测信号'
    """,
    # 数据导入日志：记录每次全量/增量导入
    """
    CREATE TABLE IF NOT EXISTS import_log (
        id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
        kind VARCHAR(32) NOT NULL COMMENT 'full/daily',
        market VARCHAR(8) NOT NULL COMMENT 'SH/SZ/BJ',
        added_records INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '新增 K 线条数',
        failed INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '失败股票数',
        elapsed_sec FLOAT NOT NULL DEFAULT 0 COMMENT '耗时(秒)',
        detail_json JSON NULL COMMENT '服务器/批次等详情',
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        KEY idx_kind_created (kind, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='数据导入日志'
    """,
    # 标的池：每只标的一行（code 唯一），group 为分组名
    """
    CREATE TABLE IF NOT EXISTS watchlist_item (
        id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
        code VARCHAR(12) NOT NULL COMMENT '证券代码 如 600900.SH / 510300.SH',
        name VARCHAR(64) NOT NULL DEFAULT '' COMMENT '标的名称',
        type VARCHAR(16) NOT NULL DEFAULT 'STOCK' COMMENT 'STOCK/ETF',
        group_name VARCHAR(64) NOT NULL DEFAULT '' COMMENT '分组名',
        sort_order INT NOT NULL DEFAULT 0 COMMENT '排序权重（保持添加顺序）',
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uk_code (code),
        KEY idx_group (group_name)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='标的池'
    """,
    # 标的池预设组：preset_name -> 代码列表
    """
    CREATE TABLE IF NOT EXISTS watchlist_preset (
        id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
        preset_name VARCHAR(64) NOT NULL COMMENT '预设组名',
        code VARCHAR(12) NOT NULL COMMENT '证券代码 如 510300.SH',
        sort_order INT NOT NULL DEFAULT 0 COMMENT '组内排序',
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uk_preset_code (preset_name, code),
        KEY idx_preset (preset_name)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='标的池预设组'
    """,
]


def get_connection(autocommit: bool = True):
    """建立业务库连接；失败抛异常（由调用方决定是否降级）。"""
    cfg = dict(MYSQL_CONFIG)
    cfg["autocommit"] = autocommit
    return mysql.connector.connect(**cfg)


def ping() -> bool:
    """探测 MySQL 是否可用（不抛异常）。"""
    try:
        conn = get_connection()
        conn.ping(reconnect=False)
        conn.close()
        return True
    except Exception as e:
        logger.warning("MySQL 不可用: %s", e)
        return False


def init_biz_db() -> bool:
    """初始化业务库与全部业务表（幂等）。返回是否成功。"""
    try:
        # 先连不指定库的服务器连接来建库（若库不存在）
        cfg = dict(MYSQL_CONFIG)
        cfg.pop("database", None)
        cfg["autocommit"] = True
        conn = mysql.connector.connect(**cfg)
        try:
            cur = conn.cursor()
            cur.execute(CREATE_DATABASE_SQL)
        finally:
            conn.close()

        conn = get_connection(autocommit=True)
        try:
            cur = conn.cursor()
            for stmt in DDL_STATEMENTS:
                cur.execute(stmt)
        finally:
            conn.close()
        logger.info("MySQL 业务库初始化完成: %s@%s:%s/%s",
                    MYSQL_CONFIG["user"], MYSQL_CONFIG["host"],
                    MYSQL_CONFIG["port"], MYSQL_CONFIG["database"])
        return True
    except Exception as e:
        logger.error("MySQL 业务库初始化失败: %s", e)
        return False


def log_import(kind: str, market: str, added_records: int = 0, failed: int = 0,
               elapsed_sec: float = 0.0, detail: dict | None = None) -> None:
    """记录一次导入日志；MySQL 不可用时静默降级。"""
    try:
        import json

        conn = get_connection(autocommit=True)
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO import_log (kind, market, added_records, failed, elapsed_sec, detail_json)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                (kind, market, int(added_records), int(failed), float(elapsed_sec),
                 json.dumps(detail, ensure_ascii=False) if detail else None),
            )
        finally:
            conn.close()
    except Exception as e:
        logger.warning("写入 import_log 失败（已降级，不影响导入）: %s", e)


def save_kronos_signal(stock_code: str, trade_date, signal_type: str,
                       probability=None, features_json=None,
                       model_version=None) -> None:
    """写入/更新一条 Kronos 预测信号（kronos_signal 表，UPSERT）。

    - stock_code: 证券代码，如 sh600900
    - trade_date: 交易日（date/str/None 均可）
    - signal_type: up/down/flat
    - probability: 预测概率(0~1)
    - features_json: dict 或 None（预测序列快照，自动 JSON 序列化）
    MySQL 不可用时静默降级，不阻塞预测流程。
    """
    try:
        import json
        if trade_date is not None and not isinstance(trade_date, str):
            trade_date = trade_date.isoformat()
        conn = get_connection(autocommit=True)
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO kronos_signal"
                " (stock_code, trade_date, signal_type, probability, features_json, model_version)"
                " VALUES (%s, %s, %s, %s, %s, %s)"
                " ON DUPLICATE KEY UPDATE"
                " signal_type=VALUES(signal_type), probability=VALUES(probability),"
                " features_json=VALUES(features_json), model_version=VALUES(model_version)",
                (
                    stock_code,
                    trade_date,
                    signal_type,
                    float(probability) if probability is not None else None,
                    json.dumps(features_json, ensure_ascii=False) if features_json else None,
                    model_version,
                ),
            )
        finally:
            conn.close()
    except Exception as e:
        logger.warning("写入 kronos_signal 失败（已降级，不影响预测）: %s", e)


def list_kronos_signals(limit: int = 50, stock_code: str | None = None) -> list[dict]:
    """查询 Kronos 预测信号历史（kronos_signal 表）。

    - limit: 最多返回条数
    - stock_code: 可选，按证券代码过滤（如 sh600900）
    返回 dict 列表；features_json 自动解析为 features 字段。
    MySQL 不可用时抛异常（由调用方决定降级策略）。
    """
    import json

    conn = get_connection(autocommit=True)
    try:
        cur = conn.cursor(dictionary=True)
        sql = ("SELECT id, stock_code, trade_date, signal_type, probability,"
               " features_json, model_version, created_at"
               " FROM kronos_signal")
        params: list = []
        if stock_code:
            sql += " WHERE stock_code=%s"
            params.append(stock_code)
        sql += " ORDER BY trade_date DESC, id DESC LIMIT %s"
        params.append(int(limit))
        cur.execute(sql, params)
        rows = cur.fetchall()
        for r in rows:
            f = r.pop("features_json", None)
            if f:
                try:
                    r["features"] = json.loads(f)
                except Exception:
                    r["features"] = None
        return rows
    finally:
        conn.close()


def list_watchlist_items() -> list[dict]:
    """读取标的池全部条目（watchlist_item 表），按 sort_order 排序。

    MySQL 不可用时抛异常（由调用方决定降级策略，如回退 JSON 文件）。
    """
    conn = get_connection(autocommit=True)
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT code, name, type, group_name FROM watchlist_item"
            " ORDER BY sort_order ASC, id ASC",
        )
        rows = cur.fetchall()
        return [
            {
                "code": r["code"],
                "name": r["name"] or "",
                "type": r["type"] or "STOCK",
                "group": r["group_name"] or "",
            }
            for r in rows
        ]
    finally:
        conn.close()


def save_watchlist_items(items: list[dict]) -> None:
    """全量替换标的池条目（watchlist_item 表，事务）。

    - items: [{code, name, type, group}, ...]，顺序即展示顺序
    MySQL 不可用时抛异常（由调用方决定降级策略）。
    """
    conn = get_connection(autocommit=False)
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM watchlist_item")
        for idx, it in enumerate(items):
            cur.execute(
                "INSERT INTO watchlist_item (code, name, type, group_name, sort_order)"
                " VALUES (%s, %s, %s, %s, %s)",
                (
                    str(it.get("code", "")),
                    str(it.get("name", "") or ""),
                    str(it.get("type", "STOCK") or "STOCK"),
                    str(it.get("group", "") or ""),
                    idx,
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_watchlist_presets() -> dict[str, list[str]]:
    """读取标的池预设组（watchlist_preset 表），返回 {组名: [代码...]}。

    MySQL 不可用时抛异常（由调用方决定降级策略）。
    """
    conn = get_connection(autocommit=True)
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT preset_name, code FROM watchlist_preset"
            " ORDER BY preset_name ASC, sort_order ASC, id ASC",
        )
        presets: dict[str, list[str]] = {}
        for r in cur.fetchall():
            presets.setdefault(r["preset_name"], []).append(r["code"])
        return presets
    finally:
        conn.close()


def save_watchlist_presets(presets: dict[str, list[str]]) -> None:
    """全量替换标的池预设组（watchlist_preset 表，事务）。

    - presets: {组名: [代码...], ...}，列表顺序即组内顺序
    MySQL 不可用时抛异常（由调用方决定降级策略）。
    """
    conn = get_connection(autocommit=False)
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM watchlist_preset")
        for name, codes in (presets or {}).items():
            for idx, code in enumerate(codes):
                cur.execute(
                    "INSERT INTO watchlist_preset (preset_name, code, sort_order)"
                    " VALUES (%s, %s, %s)",
                    (str(name), str(code), idx),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="MySQL 业务库初始化")
    parser.add_argument("--init", action="store_true", help="初始化数据库与业务表")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    if args.init:
        ok = init_biz_db()
        print("OK" if ok else "FAILED")
        raise SystemExit(0 if ok else 1)
    print("可用操作: --init")


if __name__ == "__main__":
    main()
