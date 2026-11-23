"""CSV 导入与逐日摘要核算。

规则：
- 单站有序 CSV，UTC 整秒时间戳，水位为整数毫米。
- 相邻观测间隔 <= 1 小时：段内按直线（线性）估算水位。
- 相邻观测间隔 > 1 小时：该段判为未测，不做插值。
- 跨午夜的观测段按两个 UTC 日实际占秒分摊；统计口径为 UTC 自然日（每天 86400 秒）。
- 只保存逐日摘要与分片首末读数，原始观测不落库。

两种导入方式，站点在**首次入库时选定**并固定，此后沿用：
- full（整文件）：一次上传覆盖该站全部观测；任一天与已有摘要重叠即整批退回。
- sharded（分片补传）：每次上传一个分片，允许乱序到达、同一 UTC 日多次上传；
  同站分片的 [首条, 末条] 时间范围（含两端）一旦重叠即整批退回。
  片内、片间相邻观测间隔 <= 1 小时按直线插值，> 1 小时记未测。
  迟到分片插入两片之间时，撤掉原片间插值，重算新旧相邻段；
  只重算新片自身与变动的片间段所涉及的 UTC 日。

分片模式仍不保存中间观测：每片只留首末读数，连同片内/片间按日汇总落库；
查询所用的 daily_summary / daily_level_tail 由这些汇总在同一写事务内物化。
"""
import csv
import io
import math
import re
from contextlib import contextmanager
from datetime import datetime, timezone

DAY_SECONDS = 86400
MAX_GAP_SECONDS = 3600
INF = float("inf")

# 尾分布按整数毫米逐级生成；限制同一文件内水位跨度，避免极端值造成巨量行。
MAX_LEVEL_SPAN = 100_000          # 同一文件最高与最低水位之差（毫米）
MAX_ABS_LEVEL = 10_000_000        # 单个水位绝对值上限（毫米）

MODE_FULL = "full"
MODE_SHARDED = "sharded"
MODES = (MODE_FULL, MODE_SHARDED)
MODE_LABELS = {MODE_FULL: "整文件导入", MODE_SHARDED: "分片补传"}

_EPS = 1e-9


class ImportError_(Exception):
    """导入阶段的用户可读错误（整批拒绝）。"""


_INT_RE = re.compile(r"^[+-]?\d+$")


def parse_timestamp(text, line_no):
    """解析 UTC 整秒 ISO-8601 时间戳，返回 epoch 秒（int）。"""
    text = text.strip()
    if not text:
        raise ImportError_(f"第 {line_no} 行：时间戳为空。")
    # 只接受秒级精度，拒绝小数秒
    if re.search(r"\d[\.,]\d+", text):
        raise ImportError_(f"第 {line_no} 行：只接受 UTC 整秒时间戳，发现小数秒：{text}")
    normalized = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        raise ImportError_(f"第 {line_no} 行：无法识别的时间戳：{text}")
    if dt.tzinfo is None:
        raise ImportError_(f"第 {line_no} 行：时间戳必须带 UTC 时区（以 Z 或 +00:00 结尾）：{text}")
    if dt.utcoffset().total_seconds() != 0:
        raise ImportError_(f"第 {line_no} 行：只接受 UTC 时间戳：{text}")
    epoch = int(dt.timestamp())
    if datetime.fromtimestamp(epoch, tz=timezone.utc).timestamp() != epoch:
        raise ImportError_(f"第 {line_no} 行：时间戳必须为整秒：{text}")
    return epoch


def parse_level(text, line_no):
    text = text.strip()
    if not _INT_RE.match(text):
        raise ImportError_(f"第 {line_no} 行：水位必须是整数毫米：{text or '(空)'}")
    return int(text)


def parse_csv(raw_bytes, min_rows=2):
    """解析上传的 CSV，返回 [(epoch_seconds, level_mm_int), ...]。

    整文件模式要求至少 2 条观测；分片模式允许 1 条（片内可全为未测）。
    """
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ImportError_("文件不是有效的 UTF-8 文本。")
    reader = csv.reader(io.StringIO(text))
    rows = []
    last_ts = None
    for line_no, row in enumerate(reader, start=1):
        if not row or all(not c.strip() for c in row):
            continue  # 跳过空行
        if len(row) < 2:
            raise ImportError_(f"第 {line_no} 行：至少需要“时间戳,水位”两列。")
        ts_text, level_text = row[0], row[1]
        # 首行若为表头（时间戳列非数字）则跳过
        if line_no == 1 and not _looks_like_timestamp(ts_text):
            continue
        ts = parse_timestamp(ts_text, line_no)
        level = parse_level(level_text, line_no)
        if last_ts is not None and ts <= last_ts:
            raise ImportError_(f"第 {line_no} 行：时间戳必须严格递增，发现 {ts} 不晚于上一条。")
        rows.append((ts, level))
        last_ts = ts
    if len(rows) < min_rows:
        if min_rows >= 2:
            raise ImportError_("有效观测不足 2 条，无法核算相邻观测段。")
        raise ImportError_("文件中没有任何有效观测。")
    _check_level_range(rows)
    return rows


def _check_level_range(rows):
    levels = [level for _, level in rows]
    lo, hi = min(levels), max(levels)
    if lo < -MAX_ABS_LEVEL or hi > MAX_ABS_LEVEL:
        raise ImportError_(
            f"水位超出允许范围（±{MAX_ABS_LEVEL:,} mm）：{lo} ~ {hi}。"
        )
    if hi - lo > MAX_LEVEL_SPAN:
        raise ImportError_(
            f"水位跨度 {hi - lo} mm 超过上限 {MAX_LEVEL_SPAN:,} mm，请拆分文件后再导入。"
        )


def _looks_like_timestamp(text):
    text = text.strip()
    if not text:
        return False
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def utc_day_bounds(epoch):
    """返回 epoch 所在 UTC 日的 0 点 epoch 和日期字符串。"""
    day_index = math.floor(epoch / DAY_SECONDS)
    start = day_index * DAY_SECONDS
    day = datetime.fromtimestamp(start, tz=timezone.utc).strftime("%Y-%m-%d")
    return start, day


def _add_tail(tails, h, seconds):
    """把一段秒数累加到尾分布中整数水位 h 上（seconds 为负则忽略）。"""
    if seconds > 0:
        tails[h] = tails.get(h, 0.0) + seconds


def accumulate_segment(t1, l1, t2, l2, days):
    """把一个观测段 (t1,l1)->(t2,l2) 线性估算并按 UTC 日分摊进 days。

    days: {day: {"measured": int, "min": float, "max": float,
                  "pieces": [(piece_seconds, lo, hi, flat_level_or_None)]}}
    尾分布在所有片段收集完、日内极值确定后由 finalize_tails 统一生成。
    段最长 3600 秒，最多跨一个午夜边界。
    """
    duration = t2 - t1
    slope = (l2 - l1) / duration
    day1_start, day1 = utc_day_bounds(t1)
    _, day2 = utc_day_bounds(t2)

    def level_at(epoch):
        return l1 + slope * (epoch - t1)

    if day1 == day2:
        pieces = [(day1, t1, t2)]
    else:
        day2_start = day1_start + DAY_SECONDS
        pieces = [(day1, t1, day2_start), (day2, day2_start, t2)]

    for day, seg_start, seg_end in pieces:
        piece_seconds = seg_end - seg_start
        if piece_seconds <= 0:
            continue  # 段端点恰好落在午夜边界时产生的零长度片段
        entry = days.setdefault(
            day, {"measured": 0, "min": INF, "max": -INF, "pieces": []}
        )
        entry["measured"] += piece_seconds
        la, lb = level_at(seg_start), level_at(seg_end)
        lo, hi = (la, lb) if la <= lb else (lb, la)
        entry["min"] = min(entry["min"], lo)
        entry["max"] = max(entry["max"], hi)
        entry["pieces"].append(
            (piece_seconds, lo, hi, l1 if l1 == l2 else None)
        )


def finalize_tails(days):
    """依据日内全部片段生成整数毫米尾分布 tails。

    语义：tails[h] = 全天水位 >= h 的累计秒数（与查询口径一致的完整值）。
    每个片段独立向其“可能达标”的整数区间贡献：
    - h 严格低于片段低端：整段达标 -> 加整段秒数；
    - h 落在片段水位区间内部：线性段按 (hi-h)/(hi-lo) 比例，平水段整段；
    - h 高于片段高端：贡献 0，不写。
    为避免无界负水位，只存 h >= ceil(整日最低水位) 的行；更低的 h 由查询侧
    依据 min_level 按整段有测秒数处理。
    """
    for entry in days.values():
        tails = {}
        h_floor = math.ceil(entry["min"])  # 低于它的 h 一定整天达标，无需存行
        for piece_seconds, lo, hi, flat_level in entry["pieces"]:
            first_internal = math.floor(lo) + 1
            if flat_level is not None:
                # 平水段水位 L：h <= L 全部整段达标，恰好 h == L 也算达标
                for h in range(h_floor, flat_level + 1):
                    _add_tail(tails, h, float(piece_seconds))
                continue
            span = hi - lo
            # h < 片段低端：整段达标（含低端恰为整数 h 的情形，除零测度起点）
            for h in range(h_floor, first_internal):
                _add_tail(tails, h, float(piece_seconds))
            # 区间内部整数：线性比例，升段降段同一公式
            for h in range(first_internal, math.floor(hi) + 1):
                if h > hi:
                    continue
                _add_tail(tails, h, piece_seconds * (hi - h) / span)
        entry["tails"] = tails


def build_daily_summaries(rows):
    """从有序观测构建逐日摘要字典。

    片内/整文件都用这一套：间隔 > 1 小时的相邻观测不插值（不计有测），
    因此一个分片完全没有可核算段时返回空字典，由调用方决定是否接收。
    """
    days = {}
    unmeasured_notes = []
    for (t1, l1), (t2, l2) in zip(rows, rows[1:]):
        gap = t2 - t1
        if gap <= 0:
            raise ImportError_("观测时间戳必须严格递增。")
        if gap > MAX_GAP_SECONDS:
            # 未测段：不插值；其秒数在落库时按 86400 - measured 自然计入未测
            unmeasured_notes.append((t1, t2, gap))
            continue
        accumulate_segment(t1, l1, t2, l2, days)

    finalize_tails(days)
    result = {}
    for day, entry in days.items():
        measured = entry["measured"]
        result[day] = {
            "measured_seconds": measured,
            "unmeasured_seconds": DAY_SECONDS - measured,
            "min_level": entry["min"],
            "max_level": entry["max"],
            "tails": dict(sorted(entry["tails"].items())),
        }
    return result, unmeasured_notes


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_station(station):
    station = (station or "").strip()
    if not station:
        raise ImportError_("站码不能为空。")
    return station


def normalize_mode(mode):
    mode = (mode or "").strip().lower()
    if not mode:
        return None
    if mode not in MODES:
        raise ImportError_("导入方式只能是“整文件导入”或“分片补传”。")
    return mode


def station_mode(conn, station):
    """返回站点已锁定的导入方式；无记录且已有摘要的老站按 full 处理。"""
    row = conn.execute(
        "SELECT import_mode FROM station_meta WHERE station = ?", (station,)
    ).fetchone()
    if row is not None:
        return row["import_mode"]
    legacy = conn.execute(
        "SELECT 1 FROM daily_summary WHERE station = ? LIMIT 1", (station,)
    ).fetchone()
    return MODE_FULL if legacy is not None else None


@contextmanager
def _write_transaction(conn):
    """写事务；任何异常都回滚，调用方的收片记录与摘要一起保持原状。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def insert_station_summaries(conn, station, summaries):
    """把一天或多天的 daily_* 摘要写入（调用方负责事务与旧行清理）。"""
    for day in sorted(summaries):
        s = summaries[day]
        conn.execute(
            "INSERT INTO daily_summary "
            "(station, day, measured_seconds, unmeasured_seconds, min_level, max_level) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (station, day, s["measured_seconds"], s["unmeasured_seconds"],
             s["min_level"], s["max_level"]),
        )
        conn.executemany(
            "INSERT INTO daily_level_tail (station, day, level_mm, seconds) "
            "VALUES (?, ?, ?, ?)",
            [(station, day, h, secs) for h, secs in s["tails"].items()],
        )


def import_rows(conn, station, rows):
    """整文件模式：整批写入；任一天与已有摘要重叠则整批回滚。

    返回写入的天数与未测段数量。
    """
    station = normalize_station(station)
    summaries, unmeasured_notes = build_daily_summaries(rows)
    if not summaries:
        raise ImportError_("所有相邻观测间隔都超过 1 小时，没有任何可核算的有测时段。")

    days = sorted(summaries)
    with _write_transaction(conn):
        _ensure_station_mode(conn, station, MODE_FULL)
        placeholders = ",".join("?" for _ in days)
        existing = conn.execute(
            f"SELECT day FROM daily_summary WHERE station = ? AND day IN ({placeholders})",
            [station, *days],
        ).fetchall()
        if existing:
            overlap = ", ".join(r["day"] for r in existing)
            raise ImportError_(f"与已有摘要重叠（{overlap}），整批退回，未写入任何数据。")

        insert_station_summaries(conn, station, summaries)
    return {
        "mode": MODE_FULL,
        "days": days,
        "day_count": len(days),
        "unmeasured_gaps": len(unmeasured_notes),
        "observation_count": len(rows),
    }


def _ensure_station_mode(conn, station, mode):
    """在写事务内锁定/校验站点导入方式。"""
    row = conn.execute(
        "SELECT import_mode FROM station_meta WHERE station = ?", (station,)
    ).fetchone()
    if row is not None:
        if row["import_mode"] != mode:
            raise ImportError_(
                f"站 {station} 首次入库已选择“{MODE_LABELS[row['import_mode']]}”，"
                f"此后必须沿用该方式；本次为“{MODE_LABELS[mode]}”，整批退回。"
            )
        return
    legacy = conn.execute(
        "SELECT 1 FROM daily_summary WHERE station = ? LIMIT 1", (station,)
    ).fetchone()
    if legacy is not None and mode != MODE_FULL:
        raise ImportError_(
            f"站 {station} 已有整文件导入的摘要，不能改为分片补传；整批退回。"
        )
    conn.execute(
        "INSERT INTO station_meta (station, import_mode, created_at) VALUES (?, ?, ?)",
        (station, mode, _now_iso()),
    )


def _shard_conflicts(conn, station, first_ts, last_ts):
    """找与 [first_ts, last_ts]（含两端）重叠的已有分片。"""
    return conn.execute(
        "SELECT shard_id, first_ts, last_ts FROM shard "
        "WHERE station = ? AND first_ts <= ? AND last_ts >= ? "
        "ORDER BY first_ts",
        (station, last_ts, first_ts),
    ).fetchall()


def _write_component(conn, table, tail_table, key_cols, key_values, summaries):
    """写一天组分（片内或片间）的按日汇总与尾分布。"""
    if not summaries:
        return set()
    cols = ", ".join(key_cols + ("day", "measured_seconds", "min_level", "max_level"))
    tail_cols = ", ".join(key_cols + ("day", "level_mm", "seconds"))
    marks = ", ".join("?" for _ in range(len(key_cols) + 4))
    tail_marks = ", ".join("?" for _ in range(len(key_cols) + 3))
    for day in sorted(summaries):
        s = summaries[day]
        conn.execute(
            f"INSERT INTO {table} ({cols}) VALUES ({marks})",
            (*key_values, day, s["measured_seconds"], s["min_level"], s["max_level"]),
        )
        conn.executemany(
            f"INSERT INTO {tail_table} ({tail_cols}) VALUES ({tail_marks})",
            [(*key_values, day, h, secs) for h, secs in s["tails"].items()],
        )
    return set(summaries)


def _delete_gap(conn, station, left_id, right_id):
    """删除一对旧相邻分片间的插值汇总；返回 (涉及日集合, 是否确有插值行)。"""
    days = {
        r["day"]
        for r in conn.execute(
            "SELECT DISTINCT day FROM gap_day_summary "
            "WHERE station = ? AND left_shard_id = ? AND right_shard_id = ?",
            (station, left_id, right_id),
        )
    }
    conn.execute(
        "DELETE FROM gap_day_tail WHERE station = ? AND left_shard_id = ? AND right_shard_id = ?",
        (station, left_id, right_id),
    )
    cur = conn.execute(
        "DELETE FROM gap_day_summary WHERE station = ? AND left_shard_id = ? AND right_shard_id = ?",
        (station, left_id, right_id),
    )
    return days, cur.rowcount > 0


def _load_components(conn, station, day):
    """读取某 UTC 日全部组分（片内 + 片间）的摘要与尾分布。"""
    comps = []
    shard_rows = conn.execute(
        "SELECT shard_id, measured_seconds, min_level, max_level FROM shard_day_summary "
        "WHERE station = ? AND day = ?",
        (station, day),
    ).fetchall()
    for r in shard_rows:
        tails = {
            t["level_mm"]: t["seconds"]
            for t in conn.execute(
                "SELECT level_mm, seconds FROM shard_day_tail "
                "WHERE station = ? AND shard_id = ? AND day = ?",
                (station, r["shard_id"], day),
            )
        }
        comps.append((r["measured_seconds"], r["min_level"], r["max_level"], tails))

    gap_rows = conn.execute(
        "SELECT left_shard_id, right_shard_id, measured_seconds, min_level, max_level "
        "FROM gap_day_summary WHERE station = ? AND day = ?",
        (station, day),
    ).fetchall()
    for r in gap_rows:
        tails = {
            t["level_mm"]: t["seconds"]
            for t in conn.execute(
                "SELECT level_mm, seconds FROM gap_day_tail "
                "WHERE station = ? AND left_shard_id = ? AND right_shard_id = ? AND day = ?",
                (station, r["left_shard_id"], r["right_shard_id"], day),
            )
        }
        comps.append((r["measured_seconds"], r["min_level"], r["max_level"], tails))
    return comps


def _merge_components(comps):
    """把同一 UTC 日的多个组分合并成一条 daily 摘要。

    组分之间的时段互不重叠。合并尾分布时：某组分当日最低水位已 >= h，
    该组分全部有测秒数都达标；最高水位 < h 记 0；其余读该组分的 tail(h)。
    """
    if not comps:
        return None
    measured = sum(c[0] for c in comps)
    if measured <= 0:
        return None
    min_level = min(c[1] for c in comps)
    max_level = max(c[2] for c in comps)
    tails = {}
    h_start = math.ceil(min_level)
    h_end = math.floor(max_level)
    for h in range(h_start, h_end + 1):
        total = 0.0
        for comp_measured, cmin, cmax, ctail in comps:
            if h <= cmin + _EPS:
                total += comp_measured
            elif h > cmax + _EPS:
                continue
            else:
                total += ctail.get(h, 0.0)
        if total > 0:
            tails[h] = total
    return {
        "measured_seconds": measured,
        "unmeasured_seconds": DAY_SECONDS - measured,
        "min_level": min_level,
        "max_level": max_level,
        "tails": dict(sorted(tails.items())),
    }


def rebuild_day_summary(conn, station, day):
    """只用已落库的组分重建 one UTC 日的 daily_*；返回是否有摘要。"""
    conn.execute(
        "DELETE FROM daily_level_tail WHERE station = ? AND day = ?", (station, day)
    )
    conn.execute(
        "DELETE FROM daily_summary WHERE station = ? AND day = ?", (station, day)
    )
    merged = _merge_components(_load_components(conn, station, day))
    if merged is not None:
        insert_station_summaries(conn, station, {day: merged})
    return merged is not None


def import_shard(conn, station, rows):
    """分片模式：接收一个乱序到达的分片并增量修正涉及的 UTC 日。

    一个写事务内完成：收片记录、片内汇总、撤旧片间插值、建新片间插值、
    重算受影响日的 daily_*。任何失败都回滚，收片与摘要保持原状。
    """
    station = normalize_station(station)
    shard_summaries, unmeasured_notes = build_daily_summaries(rows)
    first_ts, first_level = rows[0]
    last_ts, last_level = rows[-1]
    removed_interpolation = False

    with _write_transaction(conn):
        _ensure_station_mode(conn, station, MODE_SHARDED)
        conflicts = _shard_conflicts(conn, station, first_ts, last_ts)
        if conflicts:
            ids = "、".join(f"#{r['shard_id']}" for r in conflicts)
            raise ImportError_(
                f"新分片时间范围 [{_fmt_ts(first_ts)}, {_fmt_ts(last_ts)}]（含端点）"
                f"与已有分片 {ids} 重叠，整批退回，未写入任何数据。"
            )

        left = conn.execute(
            "SELECT shard_id, last_ts, last_level FROM shard "
            "WHERE station = ? AND last_ts < ? ORDER BY last_ts DESC LIMIT 1",
            (station, first_ts),
        ).fetchone()
        right = conn.execute(
            "SELECT shard_id, first_ts, first_level FROM shard "
            "WHERE station = ? AND first_ts > ? ORDER BY first_ts ASC LIMIT 1",
            (station, last_ts),
        ).fetchone()

        next_id = conn.execute(
            "SELECT COALESCE(MAX(shard_id), 0) + 1 AS n FROM shard WHERE station = ?",
            (station,),
        ).fetchone()["n"]
        conn.execute(
            "INSERT INTO shard (station, shard_id, first_ts, last_ts, first_level, "
            "last_level, observation_count, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (station, next_id, first_ts, last_ts, first_level, last_level,
             len(rows), _now_iso()),
        )

        affected = _write_component(
            conn, "shard_day_summary", "shard_day_tail",
            ("station", "shard_id"), (station, next_id), shard_summaries,
        )

        # 迟到片插在两片之间：先撤掉原片间插值，再按新的相邻观测重建。
        if left is not None and right is not None:
            gap_days, removed_interpolation = _delete_gap(
                conn, station, left["shard_id"], right["shard_id"]
            )
            affected |= gap_days

        if left is not None:
            affected |= _write_gap(
                conn, station,
                left["shard_id"], next_id,
                left["last_ts"], left["last_level"], first_ts, first_level,
            )
        if right is not None:
            affected |= _write_gap(
                conn, station,
                next_id, right["shard_id"],
                last_ts, last_level, right["first_ts"], right["first_level"],
            )

        for day in sorted(affected):
            rebuild_day_summary(conn, station, day)

    return {
        "mode": MODE_SHARDED,
        "shard_id": next_id,
        "days": sorted(affected),
        "day_count": len(affected),
        "unmeasured_gaps": len(unmeasured_notes),
        "observation_count": len(rows),
        "first_ts": first_ts,
        "last_ts": last_ts,
        "left_shard_id": left["shard_id"] if left is not None else None,
        "right_shard_id": right["shard_id"] if right is not None else None,
        "removed_gap": (
            (left["shard_id"], right["shard_id"])
            if left is not None and right is not None else None
        ),
        "removed_interpolation": removed_interpolation,
        "measured_seconds": sum(
            s["measured_seconds"] for s in shard_summaries.values()
        ),
    }


def _write_gap(conn, station, left_id, right_id, t1, l1, t2, l2):
    """计算并写入一个片间段；间隔 <= 1 小时才插值。"""
    summaries, _ = build_daily_summaries([(t1, l1), (t2, l2)])
    return _write_component(
        conn, "gap_day_summary", "gap_day_tail",
        ("station", "left_shard_id", "right_shard_id"),
        (station, left_id, right_id), summaries,
    )


def _fmt_ts(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def import_csv(conn, station, raw_bytes, mode=None):
    """导入入口：按站点首次选定（或用本次请求首次选定）的方式处理。"""
    station = normalize_station(station)
    requested = normalize_mode(mode)
    locked = station_mode(conn, station)
    if locked is not None and requested is not None and requested != locked:
        raise ImportError_(
            f"站 {station} 首次入库已选择“{MODE_LABELS[locked]}”，"
            f"此后必须沿用该方式；本次为“{MODE_LABELS[requested]}”，整批退回。"
        )
    effective = locked or requested or MODE_FULL
    if effective == MODE_SHARDED:
        rows = parse_csv(raw_bytes, min_rows=1)
        return import_shard(conn, station, rows)
    rows = parse_csv(raw_bytes, min_rows=2)
    return import_rows(conn, station, rows)
