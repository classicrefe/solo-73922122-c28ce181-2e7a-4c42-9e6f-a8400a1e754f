"""分片补传与整文件导入的等价性（属性测试）。

随机生成一天的观测序列，再切成若干互不重叠的分片、打乱到达顺序，
逐片走分片模式导入；最终逐日摘要与尾分布必须与一次性整文件导入完全一致。
"""
import random

import pytest

from app.importer import ImportError_, import_csv, parse_csv
from app.query import run_query


def make_rows(rng, start, count):
    """生成 count 条严格递增的观测，间隔含 <=1h 与 >1h 两类。

    首段固定 1800 秒，保证整文件模式也有可核算的有测时段。
    """
    rows = []
    ts = start
    level = rng.randint(-20, 200)
    for i in range(count):
        gap = 1800 if i == 0 else rng.choice(
            [60, 300, 900, 1800, 3599, 3600, 3601, 5400, 7200]
        )
        ts += gap
        level += rng.randint(-8, 8)
        rows.append((ts, level))
    return rows


def partition(rng, rows, max_shards=5):
    """把有序观测切成互不重叠的分片（每片 >=1 条，片段连续）。"""
    n = len(rows)
    if n <= 1:
        return [rows]
    k = rng.randint(1, min(max_shards, n))
    cuts = sorted(rng.sample(range(1, n), k - 1))
    shards = []
    prev = 0
    for c in cuts + [n]:
        shards.append(rows[prev:c])
        prev = c
    return shards


def csv_bytes(rows):
    return (
        "timestamp_utc,level_mm\n"
        + "\n".join(f"{_iso(ts)},{lvl}" for ts, lvl in rows)
        + "\n"
    ).encode()


def _iso(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def table_snapshot(db, station):
    daily = [
        tuple(r)
        for r in db.execute(
            "SELECT day, measured_seconds, unmeasured_seconds, min_level, max_level "
            "FROM daily_summary WHERE station = ? ORDER BY day", (station,)
        )
    ]
    tails = [
        tuple(r)
        for r in db.execute(
            "SELECT day, level_mm, ROUND(seconds, 6) FROM daily_level_tail "
            "WHERE station = ? ORDER BY day, level_mm", (station,)
        )
    ]
    return daily, tails


@pytest.mark.parametrize("seed", range(12))
def test_sharded_equals_full_import(db, seed):
    rng = random.Random(seed)
    # 跨午夜边界开始，覆盖多个 UTC 日
    start = 1767225600  # 2026-01-01T00:00:00Z
    start += rng.randint(0, 3) * 86400 - 3600  # 反复落在一日末尾附近
    rows = make_rows(rng, start, rng.randint(2, 24))
    shards = partition(rng, rows)

    full_station = f"FULL{seed}"
    shard_station = f"SHARD{seed}"

    full = import_csv(db, full_station, csv_bytes(rows), mode="full")
    assert full["mode"] == "full"

    # 打乱到达顺序；只保留至少含 1 条观测的片（这里都满足）
    order = list(range(len(shards)))
    rng.shuffle(order)
    for idx in order:
        import_csv(db, shard_station, csv_bytes(shards[idx]), mode="sharded")

    full_snap = table_snapshot(db, full_station)
    shard_snap = table_snapshot(db, shard_station)
    assert shard_snap == full_snap

    # 抽样若干警戒线，查询结果逐日一致
    lo = min(l for _, l in rows)
    hi = max(l for _, l in rows)
    days = sorted({r[0] for r in full_snap[0]})
    for h in sorted({lo - 1, lo, (lo + hi) // 2, hi, hi + 1}):
        for day in days[:2]:
            a = run_query(db, full_station, day, day, str(h))
            b = run_query(db, shard_station, day, day, str(h))
            assert a["measured_seconds"] == b["measured_seconds"]
            assert a["above_seconds"] == pytest.approx(b["above_seconds"])


def test_parse_shard_allows_single_row():
    rows = parse_csv(b"2026-01-01T00:00:00Z,5\n", min_rows=1)
    assert rows == [(1767225600, 5)]


def test_level_range_guard():
    # 绝对值越界
    with pytest.raises(ImportError_, match="超出允许范围"):
        parse_csv(b"2026-03-01T00:00:00Z,-20000000\n2026-03-01T01:00:00Z,0\n")
    # 跨度越界
    with pytest.raises(ImportError_, match="跨度"):
        parse_csv(b"2026-03-01T00:00:00Z,-60000\n2026-03-01T01:00:00Z,60000\n")
