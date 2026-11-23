"""Flask 页面：首页、导入、核算查询。"""
import os

from flask import (
    Blueprint, current_app, flash, render_template, request,
)

from .db import get_db, init_db
from .importer import (
    MODE_LABELS, MODE_SHARDED, ImportError_, import_csv, station_mode,
)
from .query import QueryError, run_query

bp = Blueprint("web", __name__)


@bp.app_template_filter("secs")
def fmt_secs(value):
    """秒数展示：整数不带小数，浮点保留 3 位并去尾零。"""
    if value is None:
        return "—"
    if isinstance(value, int) or float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.3f}".rstrip("0").rstrip(".")


@bp.app_template_filter("mm")
def fmt_mm(value):
    if value is None:
        return "—"
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.3f}".rstrip("0").rstrip(".")


@bp.route("/")
def index():
    db = get_db()
    stations = db.execute(
        "SELECT base.station, "
        "       COALESCE(m.import_mode, 'full') AS import_mode, "
        "       COUNT(d.day) AS day_count, MIN(d.day) AS first_day, MAX(d.day) AS last_day, "
        "       COALESCE(SUM(d.measured_seconds), 0) AS measured, "
        "       COALESCE(SUM(d.unmeasured_seconds), 0) AS unmeasured "
        "FROM (SELECT station FROM station_meta "
        "      UNION SELECT station FROM daily_summary) AS base "
        "LEFT JOIN station_meta m ON m.station = base.station "
        "LEFT JOIN daily_summary d ON d.station = base.station "
        "GROUP BY base.station ORDER BY base.station"
    ).fetchall()
    shard_counts = {
        r["station"]: r["n"]
        for r in db.execute(
            "SELECT station, COUNT(*) AS n FROM shard GROUP BY station"
        )
    }
    return render_template("index.html", stations=stations,
                           shard_counts=shard_counts, labels=MODE_LABELS)


@bp.route("/import", methods=("GET", "POST"))
def import_page():
    result = None
    station = request.args.get("station", "")
    selected_mode = request.args.get("mode", "")
    if request.method == "POST":
        station = request.form.get("station", "")
        selected_mode = request.form.get("mode", "") or selected_mode
        file = request.files.get("csv_file")
        if not file or not file.filename:
            flash("请选择要导入的 CSV 文件。", "error")
            return render_template(
                "import.html", result=None, station=station,
                locked_mode=station_mode(get_db(), station.strip())
                if station.strip() else None,
                selected_mode=selected_mode, labels=MODE_LABELS,
            )
        raw = file.read()
        try:
            result = import_csv(get_db(), station, raw, mode=selected_mode)
        except ImportError_ as e:
            flash(str(e), "error")
        except Exception as e:  # 防御：任何失败都不能留下半截数据
            current_app.logger.exception("导入失败")
            flash(f"导入失败，整批未写入：{e}", "error")
        else:
            if result["mode"] == MODE_SHARDED:
                shard_msg = (
                    f"分片 #{result['shard_id']} 已收：{result['observation_count']} 条观测，"
                    f"重算 {result['day_count']} 个 UTC 日摘要"
                )
                if result["removed_interpolation"]:
                    shard_msg += (
                        f"；已撤掉分片 #{result['removed_gap'][0]} 与 "
                        f"#{result['removed_gap'][1]} 之间的插值"
                    )
                elif result["removed_gap"]:
                    shard_msg += (
                        f"；分片 #{result['removed_gap'][0]} 与 "
                        f"#{result['removed_gap'][1]} 之间原无插值（间隔超过 1 小时），"
                        f"改为分别与迟到片相邻核算"
                    )
                flash(shard_msg + "。", "ok")
            else:
                flash(
                    f"导入完成：{result['observation_count']} 条观测，"
                    f"写入 {result['day_count']} 个 UTC 日摘要，"
                    f"未测段 {result['unmeasured_gaps']} 个。",
                    "ok",
                )
    locked_mode = station_mode(get_db(), station.strip()) if station.strip() else None
    return render_template("import.html", result=result, station=station,
                           locked_mode=locked_mode, selected_mode=selected_mode,
                           labels=MODE_LABELS)


@bp.route("/query", methods=("GET", "POST"))
def query_page():
    form = {
        "station": request.args.get("station", ""),
        "start": request.args.get("start", ""),
        "end": request.args.get("end", ""),
        "threshold": request.args.get("threshold", ""),
    }
    result = None
    if request.method == "POST":
        form = {k: request.form.get(k, "") for k in form}
        try:
            result = run_query(
                get_db(), form["station"], form["start"],
                form["end"], form["threshold"],
            )
        except QueryError as e:
            flash(str(e), "error")
    return render_template("query.html", form=form, result=result)


@bp.before_app_request
def ensure_schema():
    # 首次访问自动建表，保证容器启动即可用。按数据库文件身份（路径 + inode）
    # 缓存；文件被替换或删除重建后会重新建表。
    db = get_db()
    path = db.execute("PRAGMA database_list").fetchone()["file"]
    try:
        st = os.stat(path)
        identity = (path, st.st_dev, st.st_ino)
    except OSError:
        identity = (path, None, None)
    if current_app.extensions.get("schema_ready_for") != identity:
        init_db(db)
        current_app.extensions["schema_ready_for"] = identity
