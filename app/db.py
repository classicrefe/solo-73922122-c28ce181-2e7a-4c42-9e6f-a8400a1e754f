"""SQLite 连接与建表。

只保存逐日摘要与分片边界，不保存原始观测：
- daily_summary : 每站每天的有测/未测秒数、日内最低最高水位（含插值小数）
- daily_level_tail : 每站每天、每个整数毫米 h，水位达到或超过 h 的秒数（尾分布）

分片模式额外保存（仍不含原始观测）：
- station_meta : 站点首次入库选定的导入方式（full / sharded）
- shard : 每片的首末读数与观测条数
- shard_day_summary / shard_day_tail : 片内分段按 UTC 日的汇总
- gap_day_summary / gap_day_tail : 片与片之间插值段按 UTC 日的汇总

daily_* 始终由上述分片汇总在同一写事务内物化而成，查询只读 daily_*。
"""
import sqlite3
from contextlib import contextmanager

from flask import current_app, g


def get_db():
    if "db" not in g:
        conn = sqlite3.connect(
            current_app.config["DATABASE"],
            detect_types=sqlite3.PARSE_DECLTYPES,
            isolation_level=None,  # autocommit，事务在业务层显式控制
            timeout=10,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        # WAL 让读取方看到一份已提交快照，不被写事务阻断，也不会读到半成品。
        # 切换日志模式时需要独占，已有连接仍活跃时可能短暂失败；失败则沿用现有模式，
        # 只读快照语义在 rollback journal 下依然成立。
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError:
            pass
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA busy_timeout = 10000")
        g.db = conn
    return g.db


@contextmanager
def read_snapshot(conn):
    """在一个读事务内读取，保证多条 SELECT 看到同一已提交状态。

    连接本身是 autocommit；显式 BEGIN 后 SQLite 在首次读取时固定快照，
    直到 COMMIT。若调用方已处于事务中，则直接沿用该事务。
    """
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def close_db(_e=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_summary (
    station          TEXT    NOT NULL,
    day              TEXT    NOT NULL,          -- UTC 日期 YYYY-MM-DD
    measured_seconds INTEGER NOT NULL,          -- 有测（已插值覆盖）秒数
    unmeasured_seconds INTEGER NOT NULL,        -- 未测秒数，86400 - measured
    min_level        REAL    NOT NULL,          -- 日内插值最低水位（毫米）
    max_level        REAL    NOT NULL,          -- 日内插值最高水位（毫米）
    PRIMARY KEY (station, day)
);

CREATE TABLE IF NOT EXISTS daily_level_tail (
    station   TEXT NOT NULL,
    day       TEXT NOT NULL,
    level_mm  INTEGER NOT NULL,                 -- 整数毫米警戒线 h
    seconds   REAL NOT NULL,                    -- 当日水位 >= h 的累计秒数
    PRIMARY KEY (station, day, level_mm),
    FOREIGN KEY (station, day) REFERENCES daily_summary(station, day)
);

CREATE INDEX IF NOT EXISTS idx_tail_lookup
    ON daily_level_tail (station, day, level_mm);

-- 首次入库选定的导入方式：full（整文件一批）或 sharded（分片补传）
CREATE TABLE IF NOT EXISTS station_meta (
    station        TEXT NOT NULL PRIMARY KEY,
    import_mode    TEXT NOT NULL CHECK (import_mode IN ('full', 'sharded')),
    created_at     TEXT NOT NULL
);

-- 已接收的分片；只保存首末读数与条数，不保存中间观测
CREATE TABLE IF NOT EXISTS shard (
    station        TEXT    NOT NULL,
    shard_id       INTEGER NOT NULL,
    first_ts       INTEGER NOT NULL,
    last_ts        INTEGER NOT NULL,
    first_level    INTEGER NOT NULL,
    last_level     INTEGER NOT NULL,
    observation_count INTEGER NOT NULL,
    received_at    TEXT    NOT NULL,
    PRIMARY KEY (station, shard_id),
    FOREIGN KEY (station) REFERENCES station_meta(station)
);

CREATE INDEX IF NOT EXISTS idx_shard_span
    ON shard (station, first_ts, last_ts);

-- 片内分段（相邻观测间隔 <= 1 小时）按 UTC 日的汇总
CREATE TABLE IF NOT EXISTS shard_day_summary (
    station          TEXT    NOT NULL,
    shard_id         INTEGER NOT NULL,
    day              TEXT    NOT NULL,
    measured_seconds INTEGER NOT NULL,
    min_level        REAL    NOT NULL,
    max_level        REAL    NOT NULL,
    PRIMARY KEY (station, shard_id, day),
    FOREIGN KEY (station, shard_id) REFERENCES shard(station, shard_id)
);

CREATE TABLE IF NOT EXISTS shard_day_tail (
    station   TEXT    NOT NULL,
    shard_id  INTEGER NOT NULL,
    day       TEXT    NOT NULL,
    level_mm  INTEGER NOT NULL,
    seconds   REAL    NOT NULL,
    PRIMARY KEY (station, shard_id, day, level_mm),
    FOREIGN KEY (station, shard_id, day)
        REFERENCES shard_day_summary(station, shard_id, day)
);

-- 片间插值段按 UTC 日的汇总；left/right 为相邻两片的编号
CREATE TABLE IF NOT EXISTS gap_day_summary (
    station          TEXT    NOT NULL,
    left_shard_id    INTEGER NOT NULL,
    right_shard_id   INTEGER NOT NULL,
    day              TEXT    NOT NULL,
    measured_seconds INTEGER NOT NULL,
    min_level        REAL    NOT NULL,
    max_level        REAL    NOT NULL,
    PRIMARY KEY (station, left_shard_id, right_shard_id, day),
    FOREIGN KEY (station, left_shard_id) REFERENCES shard(station, shard_id),
    FOREIGN KEY (station, right_shard_id) REFERENCES shard(station, shard_id)
);

CREATE TABLE IF NOT EXISTS gap_day_tail (
    station        TEXT    NOT NULL,
    left_shard_id  INTEGER NOT NULL,
    right_shard_id INTEGER NOT NULL,
    day            TEXT    NOT NULL,
    level_mm       INTEGER NOT NULL,
    seconds        REAL    NOT NULL,
    PRIMARY KEY (station, left_shard_id, right_shard_id, day, level_mm),
    FOREIGN KEY (station, left_shard_id, right_shard_id, day)
        REFERENCES gap_day_summary(station, left_shard_id, right_shard_id, day)
);

CREATE INDEX IF NOT EXISTS idx_shard_day
    ON shard_day_summary (station, day);

CREATE INDEX IF NOT EXISTS idx_gap_day
    ON gap_day_summary (station, day);
"""


def init_db(conn):
    conn.executescript(SCHEMA)


def init_app(app):
    app.teardown_appcontext(close_db)

    @app.cli.command("init-db")
    def init_db_command():
        init_db(get_db())
        print("数据库已初始化。")
