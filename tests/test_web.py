"""网页端到端测试：导入演示、分片补传、重叠退回、查询核算。"""
import io
import re

SAMPLE = (
    "timestamp_utc,level_mm\n"
    "2026-01-01T23:30:00Z,100\n"
    "2026-01-02T00:00:00Z,120\n"
    "2026-01-02T00:30:00Z,120\n"
    "2026-01-02T02:00:00Z,60\n"
    "2026-01-02T03:00:00Z,80\n"
    "2026-01-02T03:30:00Z,80\n"
    "2026-01-02T04:00:00Z,50\n"
).encode("utf-8")

SHARD_A = (
    "timestamp_utc,level_mm\n"
    "2026-01-01T23:00:00Z,100\n"
    "2026-01-01T23:30:00Z,110\n"
).encode("utf-8")

SHARD_B = (
    "timestamp_utc,level_mm\n"
    "2026-01-02T01:30:00Z,130\n"
    "2026-01-02T02:00:00Z,140\n"
).encode("utf-8")

SHARD_LATE = (
    "timestamp_utc,level_mm\n"
    "2026-01-01T23:45:00Z,115\n"
    "2026-01-02T00:15:00Z,125\n"
    "2026-01-02T00:45:00Z,130\n"
).encode("utf-8")


def upload(client, station, data, mode=None):
    form = {"station": station, "csv_file": (io.BytesIO(data), "s001.csv")}
    if mode:
        form["mode"] = mode
    return client.post(
        "/import",
        data=form,
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def query(client, station, start, end, threshold):
    return client.post("/query", data={
        "station": station, "start": start, "end": end, "threshold": threshold,
    }).get_data(as_text=True)


def kpis(page):
    """解析查询结果四个 KPI：区间总秒数 / 达标 / 有测 / 未测。"""
    return [
        int(v.replace(",", ""))
        for v in re.findall(r'<div class="kpi">[^<]*<b>([\d,]+)</b>', page)
    ]


def test_pages_render(client):
    for path in ("/", "/import", "/query"):
        rv = client.get(path)
        assert rv.status_code == 200


def test_import_then_query_flow(client):
    rv = upload(client, "S001", SAMPLE)
    assert rv.status_code == 200
    text = rv.get_data(as_text=True)
    assert "导入完成" in text
    assert "2 个 UTC 日摘要" in text
    assert "未测段 1 个" in text  # 00:30 -> 02:00 共 5400s

    # 首页概览能看到站点
    home = client.get("/").get_data(as_text=True)
    assert "S001" in home

    # 查询警戒线 120：平水 1800s 达标（含跨午夜段端点不算时长）
    page = query(client, "S001", "2026-01-01", "2026-01-02", "120")
    assert "1,800" in page  # 达标秒数（千分位）
    assert kpis(page)[1] == 1800

    # 改变警戒线为 110，只读摘要即可得到新结果
    page = query(client, "S001", "2026-01-01", "2026-01-02", "110")
    # day1 跨午夜段 tail(110)=900s；day2 00:00 端点瞬时=120 不计时长，平水段水位120 共1800s
    assert "2,700" in page
    assert kpis(page)[1] == 2700


def test_overlap_batch_rejected_on_web(client):
    upload(client, "S001", SAMPLE)
    rv = upload(client, "S001", SAMPLE)
    page = rv.get_data(as_text=True)
    assert "重叠" in page and "整批退回" in page

    # 摘要仍是首次导入的那两天，没有翻倍
    page = query(client, "S001", "2026-01-01", "2026-01-02", "120")
    assert "1,800" in page
    assert kpis(page)[1] == 1800


def test_invalid_csv_shows_error(client):
    rv = upload(client, "S001", b"timestamp_utc,level_mm\n2026-01-01T00:00:00Z,100\n")
    assert "有效观测不足 2 条" in rv.get_data(as_text=True)


def test_query_validation_flash(client):
    upload(client, "S001", SAMPLE)
    rv = client.post("/query", data={
        "station": "S001", "start": "2026-01-03",
        "end": "2026-01-01", "threshold": "100",
    })
    assert "开始日期不能晚于结束日期" in rv.get_data(as_text=True)


def test_sharded_import_locks_mode_on_web(client):
    rv = upload(client, "SH9", SHARD_A, mode="sharded")
    page = rv.get_data(as_text=True)
    assert "分片 #1 已收" in page
    # 再次打开导入页，站点已锁定为分片补传，不再显示方式单选
    page = client.get("/import?station=SH9").get_data(as_text=True)
    assert "已锁定为" in page and "分片补传" in page
    assert 'name="mode"' not in page


def test_cross_midnight_late_shard_before_and_after_on_web(client):
    """网页自测：跨午夜补传前后核算结果。"""
    upload(client, "SL9", SHARD_A, mode="sharded")
    upload(client, "SL9", SHARD_B)

    page = query(client, "SL9", "2026-01-01", "2026-01-02", "120")
    assert "3,600" in page  # 补传前有测 1800 + 1800
    assert kpis(page)[2] == 3600

    rv = upload(client, "SL9", SHARD_LATE)
    page = rv.get_data(as_text=True)
    assert "分片 #3 已收" in page
    # A→B 间隔 2 小时本无可插值，迟到片将它们解除相邻并分别核算
    assert "原无插值（间隔超过 1 小时）" in page
    assert "重算 2 个 UTC 日摘要" in page

    page = query(client, "SL9", "2026-01-01", "2026-01-02", "120")
    assert "10,800" in page  # 1800+1800+900+2700+900+2700 均有测
    assert kpis(page)[1] == 7200  # 警戒线 120 达标：迟到片 01-02 段 2700 + 片间 2700 + 次片 1800
    assert kpis(page)[2] == 10800  # 有测总秒数
    # 逐日：01-01 3,600 秒有测；01-02 7,200 秒有测
    assert "3,600" in page and "7,200" in page


def test_shard_span_overlap_rejected_on_web(client):
    upload(client, "SO9", SHARD_A, mode="sharded")
    rv = upload(client, "SO9", SHARD_A)
    page = rv.get_data(as_text=True)
    assert "重叠" in page and "整批退回" in page
    # 仍只有一片
    assert "分片 #2" not in page


def test_late_shard_reports_removed_interpolation_on_web(client):
    upload(client, "SG9", (
        "timestamp_utc,level_mm\n"
        "2026-03-01T08:00:00Z,100\n"
        "2026-03-01T08:30:00Z,100\n"
    ).encode(), mode="sharded")
    upload(client, "SG9", (
        "timestamp_utc,level_mm\n"
        "2026-03-01T09:30:00Z,130\n"
        "2026-03-01T10:00:00Z,130\n"
    ).encode())
    before = query(client, "SG9", "2026-03-01", "2026-03-01", "100")
    assert "7,200" in before  # 1800 + 3600 + 1800
    assert kpis(before)[2] == 7200
    rv = upload(client, "SG9", (
        "timestamp_utc,level_mm\n"
        "2026-03-01T08:45:00Z,110\n"
        "2026-03-01T09:00:00Z,120\n"
    ).encode())
    page = rv.get_data(as_text=True)
    assert "已撤掉分片 #1 与 #2 之间的插值" in page
    after = query(client, "SG9", "2026-03-01", "2026-03-01", "100")
    assert "7,200" in after  # 1800 + 900 + 900 + 1800 + 1800
    assert kpis(after)[2] == 7200
    # 阈值 115 下不再含旧 08:30->09:30 插值：450 + 1800 + 1800
    assert kpis(query(client, "SG9", "2026-03-01", "2026-03-01", "115"))[1] == 4050
