"""SQLite 连接与建表。

只保存逐日摘要，不保存原始观测：
- daily_summary : 每站每天的有测/未测秒数、日内最低最高水位（含插值小数）
- daily_level_tail : 每站每天、每个整数毫米 h，水位达到或超过 h 的秒数（尾分布）
"""
import sqlite3

from flask import current_app, g


def get_db():
    if "db" not in g:
        conn = sqlite3.connect(
            current_app.config["DATABASE"],
            detect_types=sqlite3.PARSE_DECLTYPES,
            isolation_level=None,  # autocommit，事务在业务层显式控制
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


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
"""


def init_db(conn):
    conn.executescript(SCHEMA)


def init_app(app):
    app.teardown_appcontext(close_db)

    @app.cli.command("init-db")
    def init_db_command():
        init_db(get_db())
        print("数据库已初始化。")
