"""CSV 导入与逐日摘要核算。

规则：
- 单站有序 CSV，UTC 整秒时间戳，水位为整数毫米。
- 相邻观测间隔 <= 1 小时：段内按直线（线性）估算水位。
- 相邻观测间隔 > 1 小时：该段判为未测，不做插值。
- 跨午夜的观测段按两个 UTC 日实际占秒分摊；统计口径为 UTC 自然日（每天 86400 秒）。
- 只保存逐日摘要，原始观测不落库。
- 同站同一天已有摘要即视为重叠，整批退回（不写入任何新数据）。
"""
import csv
import io
import math
import re
from datetime import datetime, timezone

DAY_SECONDS = 86400
MAX_GAP_SECONDS = 3600
INF = float("inf")


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


def parse_csv(raw_bytes):
    """解析上传的 CSV，返回 [(epoch_seconds, level_mm_int), ...]。"""
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
    if len(rows) < 2:
        raise ImportError_("有效观测不足 2 条，无法核算相邻观测段。")
    return rows


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
    """从有序观测构建逐日摘要字典。"""
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


def import_rows(conn, station, rows):
    """整批写入；任一天与已有摘要重叠则整批回滚。

    返回写入的天数与未测段数量。
    """
    station = station.strip()
    if not station:
        raise ImportError_("站码不能为空。")
    summaries, unmeasured_notes = build_daily_summaries(rows)
    if not summaries:
        raise ImportError_("所有相邻观测间隔都超过 1 小时，没有任何可核算的有测时段。")

    days = sorted(summaries)
    conn.execute("BEGIN IMMEDIATE")
    try:
        placeholders = ",".join("?" for _ in days)
        existing = conn.execute(
            f"SELECT day FROM daily_summary WHERE station = ? AND day IN ({placeholders})",
            [station, *days],
        ).fetchall()
        if existing:
            overlap = ", ".join(r["day"] for r in existing)
            raise ImportError_(f"与已有摘要重叠（{overlap}），整批退回，未写入任何数据。")

        for day in days:
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
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {
        "days": days,
        "day_count": len(days),
        "unmeasured_gaps": len(unmeasured_notes),
        "observation_count": len(rows),
    }


def import_csv(conn, station, raw_bytes):
    rows = parse_csv(raw_bytes)
    return import_rows(conn, station, rows)
