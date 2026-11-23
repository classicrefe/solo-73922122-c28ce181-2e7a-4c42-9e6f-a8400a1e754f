#!/usr/bin/env python3
"""生成演示用单站 CSV（UTC 整秒，整数毫米）。

生成 samples/s001_waterlevel.csv，覆盖 2026-01-01/02 两个 UTC 日，包含：
- 跨午夜线性段（23:30 100mm -> 次日 00:00 120mm）
- 平水段（00:00-00:30 恒定 120mm，用于验证“恰好等于警戒线算达标”）
- >1 小时缺口（00:30 -> 02:00，共 5400 秒，判未测）
- 上升段、下降段
"""
import csv
import os

ROWS = [
    ("2026-01-01T23:30:00Z", 100),
    ("2026-01-02T00:00:00Z", 120),  # 跨午夜线性段
    ("2026-01-02T00:30:00Z", 120),  # 平水段 120mm
    ("2026-01-02T02:00:00Z", 60),   # 与上一条间隔 5400s -> 未测
    ("2026-01-02T03:00:00Z", 80),   # 3600s 上升段
    ("2026-01-02T03:30:00Z", 80),   # 平水段 80mm
    ("2026-01-02T04:00:00Z", 50),   # 下降段
]


def main():
    out_dir = os.path.join(os.path.dirname(__file__), "..", "samples")
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "s001_waterlevel.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["timestamp_utc", "level_mm"])
        w.writerows(ROWS)
    print(f"已写出 {path}（{len(ROWS)} 条观测）")


if __name__ == "__main__":
    main()
