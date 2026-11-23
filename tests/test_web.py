"""网页端到端测试：导入演示、重叠退回、查询核算。"""
import io

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


def upload(client, station, data):
    return client.post(
        "/import",
        data={"station": station, "csv_file": (io.BytesIO(data), "s001.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )


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
    rv = client.post("/query", data={
        "station": "S001", "start": "2026-01-01",
        "end": "2026-01-02", "threshold": "120",
    })
    page = rv.get_data(as_text=True)
    assert "1,800" in page  # 达标秒数（千分位）

    # 改变警戒线为 110，只读摘要即可得到新结果
    rv = client.post("/query", data={
        "station": "S001", "start": "2026-01-01",
        "end": "2026-01-02", "threshold": "110",
    })
    page = rv.get_data(as_text=True)
    # day1 跨午夜段 tail(110)=900s；day2 00:00 端点瞬时=120 不计时长，平水段水位120 共1800s
    assert "2,700" in page


def test_overlap_batch_rejected_on_web(client):
    upload(client, "S001", SAMPLE)
    rv = upload(client, "S001", SAMPLE)
    page = rv.get_data(as_text=True)
    assert "重叠" in page and "整批退回" in page

    # 摘要仍是首次导入的那两天，没有翻倍
    rv = client.post("/query", data={
        "station": "S001", "start": "2026-01-01",
        "end": "2026-01-02", "threshold": "120",
    })
    assert "1,800" in rv.get_data(as_text=True)


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
