"""核心核算规则测试。"""
import pytest

from app.importer import (
    DAY_SECONDS, ImportError_, build_daily_summaries, import_csv, import_rows,
    parse_csv,
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
