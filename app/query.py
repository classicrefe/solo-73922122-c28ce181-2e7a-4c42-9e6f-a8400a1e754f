"""警戒线核算查询：只读逐日摘要，不触碰原始观测。

任意整数毫米警戒线 h 的达标累计秒数：
- 当日最低水位 >= h：整个有测时段都达标 -> measured_seconds
- 当日最高水位 <  h：全天不达标 -> 0
- 其余情形：尾分布 daily_level_tail 中 tail(station, day, h) 的秒数
恰好等于警戒线的平水段在导入时已整体计入 tail(h)。
"""
from datetime import datetime, timedelta

DAY_SECONDS = 86400


class QueryError(Exception):
    pass


def parse_day(text, field_name):
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        raise QueryError(f"{field_name} 格式应为 YYYY-MM-DD。")


def daterange(start_day, end_day):
    cur = start_day
    while cur <= end_day:
        yield cur
        cur += timedelta(days=1)


def threshold_seconds_for_day(summary_row, tail_seconds, h):
    """依据一天的摘要判定达标秒数。"""
    if summary_row is None:
        return 0.0  # 无任何摘要的日期按“无有测”处理
    # 容差兜底浮点累计误差：最低水位（名义上）达到警戒线即整段有测时间达标
    if summary_row["min_level"] + 1e-9 >= h:
        return float(summary_row["measured_seconds"])
    if summary_row["max_level"] + 1e-9 < h:
        return 0.0
    return float(tail_seconds or 0.0)


def run_query(conn, station, start_text, end_text, threshold_text):
    station = (station or "").strip()
    if not station:
        raise QueryError("请填写站码。")
    start_day = parse_day(start_text, "开始日期")
    end_day = parse_day(end_text, "结束日期")
    if start_day > end_day:
        raise QueryError("开始日期不能晚于结束日期。")
    try:
        h = int(str(threshold_text).strip())
    except (ValueError, TypeError):
        raise QueryError("警戒线必须是整数毫米。")

    days = list(daterange(start_day, end_day))
    total_span = len(days) * DAY_SECONDS

    summaries = {
        r["day"]: r
        for r in conn.execute(
            "SELECT * FROM daily_summary WHERE station = ? "
            "AND day >= ? AND day <= ? ORDER BY day",
            (station, start_day.isoformat(), end_day.isoformat()),
        )
    }
    tail_rows = conn.execute(
        "SELECT day, seconds FROM daily_level_tail "
        "WHERE station = ? AND level_mm = ? AND day >= ? AND day <= ?",
        (station, h, start_day.isoformat(), end_day.isoformat()),
    ).fetchall()
    tails = {r["day"]: r["seconds"] for r in tail_rows}

    per_day = []
    total_above = 0.0
    total_measured = 0
    total_unmeasured = 0
    for d in days:
        key = d.isoformat()
        row = summaries.get(key)
        above = threshold_seconds_for_day(row, tails.get(key), h)
        if row is None:
            measured, unmeasured, lo, hi = 0, DAY_SECONDS, None, None
        else:
            measured = row["measured_seconds"]
            unmeasured = row["unmeasured_seconds"]
            lo, hi = row["min_level"], row["max_level"]
        total_above += above
        total_measured += measured
        total_unmeasured += unmeasured
        per_day.append({
            "day": key,
            "measured_seconds": measured,
            "unmeasured_seconds": unmeasured,
            "above_seconds": above,
            "min_level": lo,
            "max_level": hi,
            "has_data": row is not None,
        })

    return {
        "station": station,
        "start": start_day.isoformat(),
        "end": end_day.isoformat(),
        "threshold_mm": h,
        "total_span_seconds": total_span,
        "above_seconds": total_above,
        "measured_seconds": total_measured,
        "unmeasured_seconds": total_unmeasured,
        "per_day": per_day,
    }
