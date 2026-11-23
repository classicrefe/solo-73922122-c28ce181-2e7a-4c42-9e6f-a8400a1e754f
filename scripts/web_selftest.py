#!/usr/bin/env python3
"""跨午夜迟到分片的网页自测：走真实 Flask 路由（/import、/query）。

顺序：
1. 新站 S-DEMO 首次以“分片补传”上传 A 片；
2. 上传 B 片（与 A 片之间间隔 2 小时，超过 1 小时，记未测）；
3. 查询 2026-01-01~02、警戒线 120 —— 记录补传前结果；
4. 补传跨午夜的迟到片 L（落进 A、B 之间）；
5. 再次查询同一区间 —— 记录补传后结果，并核对新的相邻关系。

用临时数据库，不影响 data/ 下的数据。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from io import BytesIO  # noqa: E402

from app import create_app  # noqa: E402
from app.db import get_db, init_db  # noqa: E402

SAMPLES = os.path.join(os.path.dirname(__file__), "..", "samples")


def csv_bytes(name):
    with open(os.path.join(SAMPLES, name), "rb") as f:
        return f.read()


def post_import(client, station, filename, mode=None):
    data = {"station": station, "csv_file": (BytesIO(csv_bytes(filename)), filename)}
    if mode:
        data["mode"] = mode
    rv = client.post("/import", data=data, content_type="multipart/form-data",
                     follow_redirects=True)
    assert rv.status_code == 200, rv.status_code
    return rv.get_data(as_text=True)


def query(client, station, start, end, threshold):
    rv = client.post("/query", data={
        "station": station, "start": start, "end": end, "threshold": str(threshold),
    })
    assert rv.status_code == 200, rv.status_code
    return rv.get_data(as_text=True)


def kpis(page):
    """解析四个 KPI：区间总秒数 / 达标 / 有测 / 未测。"""
    import re
    return [
        int(v.replace(",", ""))
        for v in re.findall(r'<div class="kpi">[^<]*<b>([\d,]+)</b>', page)
    ]


def print_flashes(page, label):
    import re
    for cls, msg in re.findall(r'<div class="flash ([^"]+)">(.*?)</div>', page, re.S):
        print(f"  [{label}/{cls}] {msg.strip()}")


def main():
    tmp = tempfile.mkdtemp(prefix="waterlevel-selftest-")
    db_path = os.path.join(tmp, "selftest.db")
    app = create_app({"DATABASE": db_path, "TESTING": True})
    with app.app_context():
        init_db(get_db())

    client = app.test_client()
    station = "S-DEMO"

    print("== 1. 新站首次入库：选择分片补传，收 A 片 ==")
    print_flashes(post_import(client, station, "shard_a.csv", mode="sharded"), "A")

    print("== 2. 收 B 片（A→B 间隔 2 小时，> 1 小时，未测） ==")
    print_flashes(post_import(client, station, "shard_b.csv"), "B")

    before = query(client, station, "2026-01-01", "2026-01-02", 120)
    print("== 3. 补传前查询（警戒线 120mm） ==")
    print("  四个 KPI [总秒数, 达标, 有测, 未测]：", kpis(before))

    print("== 4. 补传跨午夜的迟到片 L（插入 A、B 之间） ==")
    late_page = post_import(client, station, "shard_late.csv")
    print_flashes(late_page, "L")
    print("  提示已解除 #1↔#2 的相邻关系：",
          "分片 #1 与 #2 之间原无插值" in late_page)

    after = query(client, station, "2026-01-01", "2026-01-02", 120)
    print("== 5. 补传后查询（同一区间 / 同一警戒线） ==")
    print("  四个 KPI [总秒数, 达标, 有测, 未测]：", kpis(after))

    before_kpi = kpis(before)
    after_kpi = kpis(after)
    ok = (
        before_kpi[2] == 3600
        and before_kpi[1] == 1800
        and after_kpi[2] == 10800
        and after_kpi[1] == 7200
        and "3,600" in after and "7,200" in after
    )
    print("\n自测结果：", "通过" if ok else "失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
