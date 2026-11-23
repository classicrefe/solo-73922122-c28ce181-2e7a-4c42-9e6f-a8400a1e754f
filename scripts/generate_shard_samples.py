#!/usr/bin/env python3
"""生成分片补传演示 CSV（UTC 整秒，整数毫米）。

写出三片，演示：乱序到达、跨 UTC 午夜、同一 UTC 日多次上传、
迟到片插入两片之间后撤掉原片间插值并重算日摘要。

- samples/shard_a.csv : 2026-01-01 23:00 -> 23:30（100 -> 110）
- samples/shard_b.csv : 2026-01-02 01:30 -> 02:00（130 -> 140）
- samples/shard_late.csv : 2026-01-01 23:45 -> 2026-01-02 00:45，跨午夜迟到片

按 A -> B -> late 顺序从网页上传即复现“补传前后核算”。
"""
import csv
import os

SHARDS = {
    "shard_a.csv": [
        ("2026-01-01T23:00:00Z", 100),
        ("2026-01-01T23:30:00Z", 110),
    ],
    "shard_b.csv": [
        ("2026-01-02T01:30:00Z", 130),
        ("2026-01-02T02:00:00Z", 140),
    ],
    "shard_late.csv": [
        ("2026-01-01T23:45:00Z", 115),
        ("2026-01-02T00:15:00Z", 125),
        ("2026-01-02T00:45:00Z", 130),
    ],
}


def main():
    out_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "samples"))
    os.makedirs(out_dir, exist_ok=True)
    for name, rows in SHARDS.items():
        path = os.path.join(out_dir, name)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["timestamp_utc", "level_mm"])
            w.writerows(rows)
        print(f"已写出 {path}（{len(rows)} 条观测）")


if __name__ == "__main__":
    main()
