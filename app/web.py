"""Flask 页面：首页、导入、核算查询。"""
from flask import (
    Blueprint, current_app, flash, redirect, render_template, request, url_for,
)

from .db import get_db, init_db
from .importer import ImportError_, import_csv
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
        "SELECT station, COUNT(*) AS day_count, MIN(day) AS first_day, MAX(day) AS last_day, "
        "SUM(measured_seconds) AS measured, SUM(unmeasured_seconds) AS unmeasured "
        "FROM daily_summary GROUP BY station ORDER BY station"
    ).fetchall()
    return render_template("index.html", stations=stations)


@bp.route("/import", methods=("GET", "POST"))
def import_page():
    result = None
    if request.method == "POST":
        station = request.form.get("station", "")
        file = request.files.get("csv_file")
        if not file or not file.filename:
            flash("请选择要导入的 CSV 文件。", "error")
            return render_template("import.html", result=None)
        raw = file.read()
        try:
            result = import_csv(get_db(), station, raw)
        except ImportError_ as e:
            flash(str(e), "error")
        except Exception as e:  # 防御：任何失败都不能留下半截数据
            current_app.logger.exception("导入失败")
            flash(f"导入失败，整批未写入：{e}", "error")
        else:
            flash(
                f"导入完成：{result['observation_count']} 条观测，"
                f"写入 {result['day_count']} 个 UTC 日摘要，"
                f"未测段 {result['unmeasured_gaps']} 个。",
                "ok",
            )
    return render_template("import.html", result=result)


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
    # 首次访问自动建表，保证容器启动即可用
    if not current_app.extensions.get("schema_ready"):
        init_db(get_db())
        current_app.extensions["schema_ready"] = True
