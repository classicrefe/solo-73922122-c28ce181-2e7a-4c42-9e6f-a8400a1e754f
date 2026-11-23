"""核心核算规则测试。"""
import sqlite3

import pytest

from app.importer import (
    DAY_SECONDS, ImportError_, build_daily_summaries, import_csv, import_rows,
    parse_csv, station_mode,
)
from app.query import QueryError, run_query


def csv_bytes(rows, header=True):
    lines = []
    if header:
        lines.append("timestamp_utc,level_mm")
    lines.extend(f"{ts},{lvl}" for ts, lvl in rows)
    return ("\n".join(lines) + "\n").encode("utf-8")


def test_linear_tail_distribution_basic(db):
    # 10:00 水位100 -> 11:00 水位120，3600s 整小时线性上升
    rows = parse_csv(csv_bytes([
        ("2026-03-01T10:00:00Z", 100),
        ("2026-03-01T11:00:00Z", 120),
    ]))
    summaries, _ = build_daily_summaries(rows)
    day = summaries["2026-03-01"]
    assert day["measured_seconds"] == 3600
    assert day["unmeasured_seconds"] == DAY_SECONDS - 3600
    assert day["min_level"] == 100 and day["max_level"] == 120
    # tail(110)：从 10:30 起 >=110，共 1800s
    assert day["tails"][110] == pytest.approx(1800.0)
    # tail(101)：比例 (120-101)/20 = 0.95 -> 3420s
    assert day["tails"][101] == pytest.approx(3420.0)
    # 低端整数 100：整段（除零测度起点）达标，存 3600s
    assert day["tails"][100] == pytest.approx(3600.0)
    # 120 为高端整数：除端点测度外达标 0 秒，不落正秒数行
    assert 120 not in day["tails"]


def test_query_uses_only_summaries_and_threshold_changes(db):
    rows = parse_csv(csv_bytes([
        ("2026-03-01T10:00:00Z", 100),
        ("2026-03-01T11:00:00Z", 120),
    ]))
    import_rows(db, "S1", rows)

    r100 = run_query(db, "S1", "2026-03-01", "2026-03-01", "100")
    # 最低水位 == 100，整段有测 3600s 全部达标
    assert r100["above_seconds"] == pytest.approx(3600.0)
    r110 = run_query(db, "S1", "2026-03-01", "2026-03-01", "110")
    assert r110["above_seconds"] == pytest.approx(1800.0)
    r121 = run_query(db, "S1", "2026-03-01", "2026-03-01", "121")
    assert r121["above_seconds"] == 0.0
    # 改变警戒线不产生任何写入：摘要表行数不变
    n_tail = db.execute("SELECT COUNT(*) c FROM daily_level_tail").fetchone()["c"]
    run_query(db, "S1", "2026-03-01", "2026-03-01", "105")
    assert db.execute("SELECT COUNT(*) c FROM daily_level_tail").fetchone()["c"] == n_tail


def test_flat_segment_equal_threshold_counts(db):
    rows = parse_csv(csv_bytes([
        ("2026-03-01T08:00:00Z", 90),
        ("2026-03-01T08:30:00Z", 90),   # 平水 90，1800s
        ("2026-03-01T09:00:00Z", 70),   # 30 分钟降到 70
    ]))
    import_rows(db, "S2", rows)
    r = run_query(db, "S2", "2026-03-01", "2026-03-01", "90")
    # 平水 1800s 全部算达标；下降段除零测度端点外不达标
    assert r["above_seconds"] == pytest.approx(1800.0)
    r80 = run_query(db, "S2", "2026-03-01", "2026-03-01", "80")
    # 平水 1800s + 下降段 (90-80)/(90-70)=0.5 * 1800 = 900s
    assert r80["above_seconds"] == pytest.approx(2700.0)


def test_midnight_split(db):
    # 23:30 100 -> 次日 00:00 120，1800s 段按 23:30-24:00 全归当日
    rows = parse_csv(csv_bytes([
        ("2026-01-01T23:30:00Z", 100),
        ("2026-01-02T00:00:00Z", 120),
        ("2026-01-02T00:30:00Z", 120),
    ]))
    summaries, _ = build_daily_summaries(rows)
    d1, d2 = summaries["2026-01-01"], summaries["2026-01-02"]
    assert d1["measured_seconds"] == 1800
    assert d1["unmeasured_seconds"] == DAY_SECONDS - 1800
    assert d2["measured_seconds"] == 1800
    # 当日片段水位 100 -> 120；tail(110)=900s
    assert d1["tails"][110] == pytest.approx(900.0)
    # 次日整日只有平水 120：tail(120) 存整段 1800s
    assert d2["tails"][120] == pytest.approx(1800.0)

    import_rows(db, "S3", rows)
    r = run_query(db, "S3", "2026-01-01", "2026-01-02", "120")
    # day1 仅端点瞬时达 120 -> 0；day2 平水 1800s
    assert r["above_seconds"] == pytest.approx(1800.0)
    assert r["measured_seconds"] == 3600
    assert r["unmeasured_seconds"] == 2 * DAY_SECONDS - 3600


def test_gap_over_one_hour_is_unmeasured(db):
    rows = parse_csv(csv_bytes([
        ("2026-03-01T00:00:00Z", 50),
        ("2026-03-01T01:00:00Z", 60),   # 恰好 3600s，有测
        ("2026-03-01T02:00:01Z", 60),   # 3601s，未测
        ("2026-03-01T03:00:00Z", 70),   # 3599s，有测
    ]))
    summaries, notes = build_daily_summaries(rows)
    assert len(notes) == 1
    day = summaries["2026-03-01"]
    assert day["measured_seconds"] == 3600 + 3599
    assert day["unmeasured_seconds"] == DAY_SECONDS - day["measured_seconds"]


def test_overlap_rejects_whole_batch_and_keeps_old(db):
    rows1 = parse_csv(csv_bytes([
        ("2026-03-01T10:00:00Z", 100),
        ("2026-03-01T11:00:00Z", 120),
    ]))
    import_rows(db, "S4", rows1)
    before = db.execute("SELECT COUNT(*) c FROM daily_summary").fetchone()["c"]

    rows2 = parse_csv(csv_bytes([
        ("2026-03-01T20:00:00Z", 10),
        ("2026-03-01T21:00:00Z", 20),
        ("2026-03-02T00:00:00Z", 30),
    ]))
    with pytest.raises(ImportError_, match="重叠"):
        import_rows(db, "S4", rows2)
    after = db.execute("SELECT COUNT(*) c FROM daily_summary").fetchone()["c"]
    assert before == after == 1
    # 原摘要内容未被改动
    old = db.execute(
        "SELECT * FROM daily_summary WHERE station='S4' AND day='2026-03-01'"
    ).fetchone()
    assert old["measured_seconds"] == 3600


def test_different_station_same_day_is_allowed(db):
    rows = parse_csv(csv_bytes([
        ("2026-03-01T10:00:00Z", 100),
        ("2026-03-01T11:00:00Z", 120),
    ]))
    import_rows(db, "A", rows)
    import_rows(db, "B", rows)  # 不同站，同日不冲突
    n = db.execute("SELECT COUNT(*) c FROM daily_summary").fetchone()["c"]
    assert n == 2


def test_validation_errors():
    with pytest.raises(ImportError_):
        parse_csv("timestamp_utc,level_mm\n2026-03-01T10:00:00,100\n".encode())
    with pytest.raises(ImportError_):
        parse_csv(b"2026-03-01T10:00:00Z,100.5\n2026-03-01T11:00:00Z,120\n")
    with pytest.raises(ImportError_):
        parse_csv(b"2026-03-01T11:00:00Z,100\n2026-03-01T10:00:00Z,120\n")
    with pytest.raises(ImportError_):
        parse_csv(b"2026-03-01T10:00:00.5Z,100\n2026-03-01T11:00:00Z,120\n")
    with pytest.raises(ImportError_):
        parse_csv(b"2026-03-01T10:00:00+08:00,100\n2026-03-01T11:00:00+08:00,120\n")
    # 无表头也能解析
    rows = parse_csv(b"2026-03-01T10:00:00Z,100\n2026-03-01T11:00:00Z,120\n")
    assert len(rows) == 2


def test_all_gaps_unmeasured_rejected(db):
    rows = parse_csv(csv_bytes([
        ("2026-03-01T00:00:00Z", 50),
        ("2026-03-01T02:00:00Z", 60),
        ("2026-03-01T04:00:00Z", 70),
    ]))
    with pytest.raises(ImportError_, match="没有任何可核算的有测时段"):
        import_rows(db, "S8", rows)
    assert db.execute("SELECT COUNT(*) c FROM daily_summary").fetchone()["c"] == 0


def test_tail_completeness_with_low_day_minimum_and_flat_piece(db):
    """回归：整日最低为负整数、另有高平水段时，中间警戒线不得漏计。"""
    rows = parse_csv(csv_bytes([
        ("2026-03-01T03:50:56Z", -22),
        ("2026-03-01T04:00:56Z", -7),    # 600s 上升段
        ("2026-03-01T04:30:56Z", 29),    # 1800s 上升段
        ("2026-03-01T05:30:57Z", 29),    # 3601s -> 未测
        ("2026-03-01T06:10:57Z", 29),    # 2400s 平水 29（上一条未测，本间隔 2400s 有测）
    ]))
    import_rows(db, "S9", rows)
    # 有测总时长 600 + 1800 + 2400 = 4800s
    r = run_query(db, "S9", "2026-03-01", "2026-03-01", "-21")
    assert r["measured_seconds"] == 4800
    # 警戒线 -21：仅首段 (-22 -> -7) 最初 600*(1/15)=40s 低于 -21
    assert r["above_seconds"] == pytest.approx(4760.0)
    # 警戒线 0：首段最高 -7 全程不达标；第二段 (-7 -> 29) 达标占比 29/36，
    # 1800*29/36 = 1450s；平水段 2400s 全达标
    r0 = run_query(db, "S9", "2026-03-01", "2026-03-01", "0")
    assert r0["above_seconds"] == pytest.approx(3850.0)
    # 警戒线 29：平水段恰好等于 29 整段达标；斜坡端点零测度不计
    r29 = run_query(db, "S9", "2026-03-01", "2026-03-01", "29")
    assert r29["above_seconds"] == pytest.approx(2400.0)


def test_date_range_with_missing_days(db):
    rows = parse_csv(csv_bytes([
        ("2026-03-01T10:00:00Z", 100),
        ("2026-03-01T11:00:00Z", 120),
    ]))
    import_rows(db, "S5", rows)
    r = run_query(db, "S5", "2026-03-01", "2026-03-03", "100")
    assert len(r["per_day"]) == 3
    assert r["total_span_seconds"] == 3 * DAY_SECONDS
    # 无数据的两天全天未测
    assert r["unmeasured_seconds"] == 2 * DAY_SECONDS + (DAY_SECONDS - 3600)
    assert r["per_day"][1]["has_data"] is False
    assert r["per_day"][1]["unmeasured_seconds"] == DAY_SECONDS


def test_query_input_validation(db):
    rows = parse_csv(csv_bytes([
        ("2026-03-01T10:00:00Z", 100),
        ("2026-03-01T11:00:00Z", 120),
    ]))
    import_rows(db, "S6", rows)
    with pytest.raises(QueryError):
        run_query(db, "S6", "2026-03-05", "2026-03-01", "100")
    with pytest.raises(QueryError):
        run_query(db, "S6", "2026-03-01", "2026-03-01", "abc")
    # 负整数警戒线合法
    r = run_query(db, "S6", "2026-03-01", "2026-03-01", "-50")
    assert r["above_seconds"] == pytest.approx(3600.0)


def test_import_csv_end_to_end(db, tmp_path):
    path = tmp_path / "x.csv"
    path.write_bytes(csv_bytes([
        ("2026-03-01T10:00:00Z", 100),
        ("2026-03-01T11:00:00Z", 120),
    ]))
    result = import_csv(db, "S7", path.read_bytes())
    assert result["day_count"] == 1
    with pytest.raises(ImportError_):
        import_csv(db, "S7", path.read_bytes())  # 重复导入被拒


# ---------------------------------------------------------------- 分片补传

def shard_csv(rows):
    return csv_bytes(rows)


def test_sharded_first_import_locks_mode(db):
    rows = parse_csv(shard_csv([
        ("2026-03-01T10:00:00Z", 100),
        ("2026-03-01T11:00:00Z", 120),
    ]))
    result = import_csv(db, "SH1", shard_csv([
        ("2026-03-01T10:00:00Z", 100),
        ("2026-03-01T11:00:00Z", 120),
    ]), mode="sharded")
    assert result["mode"] == "sharded"
    assert station_mode(db, "SH1") == "sharded"
    # 此后不传 mode 也沿用分片；传 full 被拒
    with pytest.raises(ImportError_, match="必须沿用"):
        import_csv(db, "SH1", shard_csv([
            ("2026-03-02T10:00:00Z", 100),
            ("2026-03-02T11:00:00Z", 120),
        ]), mode="full")
    # 分片方式下可再次补传不重叠的片
    result2 = import_csv(db, "SH2", shard_csv([
        ("2026-03-02T10:00:00Z", 100),
        ("2026-03-02T11:00:00Z", 120),
    ]), mode="sharded")
    assert result2["mode"] == "sharded"
    assert station_mode(db, "SH2") == "sharded"


def test_full_station_cannot_switch_to_sharded(db):
    rows = parse_csv(shard_csv([
        ("2026-03-01T10:00:00Z", 100),
        ("2026-03-01T11:00:00Z", 120),
    ]))
    import_rows(db, "FULL1", rows)
    with pytest.raises(ImportError_, match="必须沿用"):
        import_csv(db, "FULL1", shard_csv([
            ("2026-03-02T10:00:00Z", 100),
            ("2026-03-02T11:00:00Z", 120),
        ]), mode="sharded")
    # 无 station_meta 的历史摘要（老库）也按整文件处理，不能改成 sharded
    db.execute(
        "INSERT INTO daily_summary (station, day, measured_seconds, unmeasured_seconds, "
        "min_level, max_level) VALUES ('LEGACY', '2026-03-01', 3600, 82800, 10, 20)"
    )
    with pytest.raises(ImportError_, match="必须沿用"):
        import_csv(db, "LEGACY", shard_csv([
            ("2026-03-02T10:00:00Z", 100),
            ("2026-03-02T11:00:00Z", 120),
        ]), mode="sharded")


def test_out_of_order_shards_and_same_day_multiple_uploads(db):
    import_csv(db, "SO1", shard_csv([
        ("2026-03-01T08:00:00Z", 100),
        ("2026-03-01T09:00:00Z", 120),
    ]), mode="sharded")
    # 同一 UTC 日再传一片（时间在后），片间 1 小时内插值
    r = import_csv(db, "SO1", shard_csv([
        ("2026-03-01T09:30:00Z", 120),
        ("2026-03-01T10:00:00Z", 60),
    ]))
    assert r["shard_id"] == 2
    # 片间 gap 1->2 间隔 1800s <= 3600s，有测（平水 120）
    q = run_query(db, "SO1", "2026-03-01", "2026-03-01", "120")
    assert q["measured_seconds"] == 3600 + 1800 + 1800
    # 首片升段与次片降段都只在端点瞬时达 120（零测度）；片间平水 120 共 1800s
    assert q["above_seconds"] == pytest.approx(1800)


def test_shard_span_overlap_rejected_including_endpoints(db):
    import_csv(db, "SO2", shard_csv([
        ("2026-03-01T08:00:00Z", 100),
        ("2026-03-01T09:00:00Z", 120),
    ]), mode="sharded")
    # 端点恰好相接也算重叠（含两端）
    with pytest.raises(ImportError_, match="重叠"):
        import_csv(db, "SO2", shard_csv([
            ("2026-03-01T09:00:00Z", 120),
            ("2026-03-01T10:00:00Z", 60),
        ]))
    # 时间范围被完全包含也算重叠
    with pytest.raises(ImportError_, match="重叠"):
        import_csv(db, "SO2", shard_csv([
            ("2026-03-01T08:10:00Z", 105),
            ("2026-03-01T08:20:00Z", 110),
        ]))
    assert db.execute("SELECT COUNT(*) c FROM shard WHERE station='SO2'").fetchone()["c"] == 1


def test_shard_span_overlap_rolls_back_whole_batch(db):
    import_csv(db, "SO3", shard_csv([
        ("2026-03-01T08:00:00Z", 100),
        ("2026-03-01T09:00:00Z", 120),
    ]), mode="sharded")
    before = db.execute(
        "SELECT COUNT(*) c FROM daily_summary WHERE station='SO3'"
    ).fetchone()["c"]
    with pytest.raises(ImportError_, match="重叠"):
        import_csv(db, "SO3", shard_csv([
            ("2026-03-01T09:00:00Z", 999),  # 重叠
            ("2026-03-02T10:00:00Z", 120),  # 本可独立核算的一天也不写入
        ]))
    after = db.execute(
        "SELECT COUNT(*) c FROM daily_summary WHERE station='SO3'"
    ).fetchone()["c"]
    assert before == after == 1
    assert db.execute("SELECT COUNT(*) c FROM shard WHERE station='SO3'").fetchone()["c"] == 1


def test_late_shard_between_two_rebuilds_gap_and_days(db):
    """迟到片插入两片之间：撤旧片间插值，按新相邻观测修正日摘要。"""
    import_csv(db, "SL1", shard_csv([
        ("2026-01-01T23:00:00Z", 100),
        ("2026-01-01T23:30:00Z", 110),
    ]), mode="sharded")
    import_csv(db, "SL1", shard_csv([
        ("2026-01-02T01:30:00Z", 130),
        ("2026-01-02T02:00:00Z", 140),
    ]))
    # 插入前：片间 23:30->01:30 间隔 7200s > 3600s，未测
    before = run_query(db, "SL1", "2026-01-01", "2026-01-02", "120")
    assert before["measured_seconds"] == 1800 + 1800

    r = import_csv(db, "SL1", shard_csv([
        ("2026-01-01T23:45:00Z", 115),
        ("2026-01-02T00:15:00Z", 125),
        ("2026-01-02T00:45:00Z", 130),
    ]))
    assert r["removed_gap"] == (1, 2)
    assert r["days"] == ["2026-01-01", "2026-01-02"]
    # 片内 1800+1800，迟到片自身 900+2700，两条新片间段 900+2700
    after = run_query(db, "SL1", "2026-01-01", "2026-01-02", "120")
    assert after["measured_seconds"] == 1800 + 1800 + 900 + 2700 + 900 + 2700
    # 旧片间插值行已删除，只保留两条新相邻段
    gaps = db.execute(
        "SELECT left_shard_id, right_shard_id FROM gap_day_summary "
        "WHERE station='SL1' ORDER BY left_shard_id"
    ).fetchall()
    assert [(g["left_shard_id"], g["right_shard_id"]) for g in gaps] == [(1, 3), (3, 2)]
    # 跨午夜：2026-01-01 有测 3600s，2026-01-02 有测 7200s
    day1 = after["per_day"][0]
    day2 = after["per_day"][1]
    assert day1["measured_seconds"] == 3600
    assert day2["measured_seconds"] == 7200
    # 01-01 警戒线 112：片间 1->3 段 110->115 中后 3/5（540s），
    # 迟到片 23:45->24:00 段 115->120 全段 900s；首片最高 110 不计
    q = run_query(db, "SL1", "2026-01-01", "2026-01-01", "112")
    assert q["above_seconds"] == pytest.approx(540.0 + 900.0)


def test_shard_with_all_unmeasured_is_accepted(db):
    # 片内两条观测间隔 > 1 小时：该片可接收（全未测），后续片照常插值
    r = import_csv(db, "SU1", shard_csv([
        ("2026-03-01T00:00:00Z", 10),
        ("2026-03-01T03:00:00Z", 20),
    ]), mode="sharded")
    assert r["observation_count"] == 2
    assert r["day_count"] == 0
    # 再补一片 03:30，与上一片末条 03:00 间隔 1800s，有测
    import_csv(db, "SU1", shard_csv([
        ("2026-03-01T03:30:00Z", 20),
        ("2026-03-01T04:00:00Z", 30),
    ]))
    q = run_query(db, "SU1", "2026-03-01", "2026-03-01", "0")
    assert q["measured_seconds"] == 1800 + 1800


def test_single_observation_shard_accepted(db):
    r = import_csv(db, "SU2", shard_csv([
        ("2026-03-01T00:00:00Z", 10),
    ]), mode="sharded")
    assert r["day_count"] == 0
    assert db.execute("SELECT COUNT(*) c FROM shard WHERE station='SU2'").fetchone()["c"] == 1


def test_gap_exactly_one_hour_interpolates(db):
    import_csv(db, "SG1", shard_csv([
        ("2026-03-01T08:00:00Z", 100),
        ("2026-03-01T08:30:00Z", 100),
    ]), mode="sharded")
    import_csv(db, "SG1", shard_csv([
        ("2026-03-01T09:30:00Z", 130),
        ("2026-03-01T10:00:00Z", 130),
    ]))
    # 片间 08:30->09:30 恰好 3600s：直线 100->130
    q = run_query(db, "SG1", "2026-03-01", "2026-03-01", "115")
    assert q["measured_seconds"] == 1800 + 3600 + 1800
    # 08:30-09:30 中 >=115 的部分：(130-115)/30 * 3600 = 1800s；次片平水 130 共 1800s
    assert q["above_seconds"] == pytest.approx(1800 + 1800)


def test_shard_write_failure_rolls_back_receipt_and_summary(db, monkeypatch):
    import_csv(db, "SF1", shard_csv([
        ("2026-03-01T08:00:00Z", 100),
        ("2026-03-01T09:00:00Z", 120),
    ]), mode="sharded")
    import app.importer as importer

    def boom(*_a, **_k):
        raise RuntimeError("模拟写摘要失败")

    monkeypatch.setattr(importer, "rebuild_day_summary", boom)
    with pytest.raises(RuntimeError, match="模拟写摘要失败"):
        import_csv(db, "SF1", shard_csv([
            ("2026-03-01T10:00:00Z", 120),
            ("2026-03-01T11:00:00Z", 130),
        ]))
    # 收片记录也没留下：分片仍只有 1 个，片间 gap 为空
    assert db.execute("SELECT COUNT(*) c FROM shard WHERE station='SF1'").fetchone()["c"] == 1
    assert db.execute(
        "SELECT COUNT(*) c FROM gap_day_summary WHERE station='SF1'"
    ).fetchone()["c"] == 0
    # 原摘要不变：仍只有首片 3600s
    q = run_query(db, "SF1", "2026-03-01", "2026-03-01", "0")
    assert q["measured_seconds"] == 3600


def test_incremental_rebuild_only_touches_affected_days(db):
    """补传只重算新片和变动的片间段涉及的 UTC 日。"""
    import_csv(db, "SD1", shard_csv([
        ("2026-03-01T08:00:00Z", 100),
        ("2026-03-01T09:00:00Z", 120),
    ]), mode="sharded")
    import_csv(db, "SD1", shard_csv([
        ("2026-03-05T08:00:00Z", 100),
        ("2026-03-05T09:00:00Z", 120),
    ]))
    day5_before = db.execute(
        "SELECT * FROM daily_summary WHERE station='SD1' AND day='2026-03-05'"
    ).fetchone()
    # 在 03-01 与 03-05 之间补一片（间隔远大于 1 小时，不影响现有两头）
    r = import_csv(db, "SD1", shard_csv([
        ("2026-03-03T08:00:00Z", 50),
        ("2026-03-03T09:00:00Z", 60),
    ]))
    assert r["days"] == ["2026-03-03"]
    day5_after = db.execute(
        "SELECT * FROM daily_summary WHERE station='SD1' AND day='2026-03-05'"
    ).fetchone()
    assert dict(day5_before) == dict(day5_after)


def test_late_shard_removes_previously_interpolated_gap(db):
    """原片间 <=1 小时已插值；迟到片插入后撤掉原插值，按新段重算。"""
    import_csv(db, "SG2", shard_csv([
        ("2026-03-01T08:00:00Z", 100),
        ("2026-03-01T08:30:00Z", 100),
    ]), mode="sharded")
    import_csv(db, "SG2", shard_csv([
        ("2026-03-01T09:30:00Z", 130),
        ("2026-03-01T10:00:00Z", 130),
    ]))
    before = run_query(db, "SG2", "2026-03-01", "2026-03-01", "100")
    assert before["measured_seconds"] == 1800 + 3600 + 1800
    # 迟到片 08:45-09:00，片内 900s；新片间 08:30->08:45 900s、09:00->09:30 1800s
    r = import_csv(db, "SG2", shard_csv([
        ("2026-03-01T08:45:00Z", 110),
        ("2026-03-01T09:00:00Z", 120),
    ]))
    assert r["removed_gap"] == (1, 2)
    after = run_query(db, "SG2", "2026-03-01", "2026-03-01", "100")
    assert after["measured_seconds"] == 1800 + 900 + 900 + 1800 + 1800
    # 原 3600s 插值中 08:30->09:30 的旧尾分布不再参与：警戒线 115 的新值
    q = run_query(db, "SG2", "2026-03-01", "2026-03-01", "115")
    # 首片平水 100 不达标；片间1 900s 段 100->110 全不达标；迟到片 110->120 中 >=115 为 450s；
    # 片间2 09:00->09:30 平水 120 全达标 1800s；次片平水 130 1800s
    assert q["above_seconds"] == pytest.approx(450.0 + 1800.0 + 1800.0)


def test_query_reads_single_committed_snapshot(db):
    """查询的日摘要与尾分布来自同一读快照：两次 SELECT 之间发生的提交不可见。"""
    import_csv(db, "SS1", shard_csv([
        ("2026-03-01T10:00:00Z", 100),
        ("2026-03-01T11:00:00Z", 120),
    ]), mode="sharded")

    db_path = db.execute("PRAGMA database_list").fetchone()["file"]
    other = sqlite3.connect(db_path, isolation_level=None)
    other.execute("PRAGMA journal_mode = WAL")

    def write_between_selects(statement):
        # 第一次读 daily_summary 已固定快照；在随后的 tail 读取执行前，
        # 用另一个连接提交一笔修改，本查询仍应看到快照。
        if not write_between_selects.done and "FROM daily_level_tail" in statement:
            write_between_selects.done = True
            other.execute("BEGIN IMMEDIATE")
            other.execute(
                "UPDATE daily_level_tail SET seconds = 0 "
                "WHERE station = 'SS1' AND level_mm = 110"
            )
            other.execute("COMMIT")

    write_between_selects.done = False
    db.set_trace_callback(write_between_selects)
    try:
        q = run_query(db, "SS1", "2026-03-01", "2026-03-01", "110")
    finally:
        db.set_trace_callback(None)
        other.close()
    assert write_between_selects.done is True
    assert q["above_seconds"] == pytest.approx(1800.0)


def test_legacy_station_still_imports_and_queries(app):
    """老库（只有 daily_* 表、没有 station_meta）照常导入与查询。"""
    from app.db import get_db, init_db
    from app.importer import station_mode

    path = app.config["DATABASE"]
    with app.app_context():
        db = get_db()
        db.execute(
            "INSERT INTO daily_summary (station, day, measured_seconds, "
            "unmeasured_seconds, min_level, max_level) "
            "VALUES ('OLD', '2026-01-01', 3600, 82800, 100, 120)"
        )
        db.execute(
            "INSERT INTO daily_level_tail (station, day, level_mm, seconds) "
            "VALUES ('OLD', '2026-01-01', 110, 1800)"
        )
        init_db(db)  # 重复建表幂等，补上分片相关表
        assert station_mode(db, "OLD") == "full"
        q = run_query(db, "OLD", "2026-01-01", "2026-01-01", "110")
        assert q["above_seconds"] == pytest.approx(1800.0)

        # 老站照常整文件导入新的一天
        result = import_csv(
            db, "OLD",
            b"2026-01-02T10:00:00Z,100\n2026-01-02T11:00:00Z,120\n",
        )
        assert result["days"] == ["2026-01-02"]
        assert station_mode(db, "OLD") == "full"
        # 老站不能改成 sharded
        with pytest.raises(ImportError_, match="必须沿用"):
            import_csv(
                db, "OLD",
                b"2026-01-03T10:00:00Z,100\n2026-01-03T11:00:00Z,120\n",
                mode="sharded",
            )
