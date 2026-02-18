#!/usr/bin/env python3
"""
从 DJI Action 5 Pro 等视频中提取 GPS 经纬度及对应的毫秒级时间戳。

使用 pyosmogps 正确解析 djmd 得到 (lat, lon)，再用 MP4 容器中 metadata 轨的
stts/timescale 计算每个 sample 的 PTS（毫秒），按 sample 顺序一一对应后输出。
时间基准为视频在 MP4 中的创建时间（mvhd），非 timeinfo 的整秒。

依赖: pip install pyosmogps
      （可选 GPX 输出: pip install gpxpy）

用法:
  python extract_gps_with_ms.py <视频路径> [--output 输出.csv] [--gpx 输出.gpx] [--tz 8]
"""

import argparse
import csv
import sys
from pathlib import Path

from pyosmogps import OsmoGps

# 从同目录的 parse_djmd_gps 复用 MP4 解析
from parse_djmd_gps import get_metadata_track_pts_ms, get_video_creation_time


def extract_gps_with_millisecond_timestamps(
    video_path,
    output_csv=None,
    output_gpx=None,
    timezone_offset=8,
):
    video_path = Path(video_path)
    if not video_path.is_file():
        print(f"错误: 找不到视频文件 {video_path}", file=sys.stderr)
        return False

    # 1) 用 pyosmogps 提取 GPS（不 resample，保留与 metadata sample 一一对应）
    print(f"正在用 pyosmogps 从 {video_path} 提取 GPS...")
    gps = OsmoGps([str(video_path)], timezone_offset=timezone_offset)
    gps_data = getattr(gps, "gps_data", None) or []
    if not gps_data:
        print("未提取到任何 GPS 点，请确认视频包含 GPS 元数据且已连接遥控器。", file=sys.stderr)
        return False

    # 2) 从同一视频的 metadata 轨获取每个 sample 的 PTS（毫秒）
    print("正在从 MP4 读取 metadata 轨时间戳...")
    pts_ms_list = get_metadata_track_pts_ms(str(video_path), trak_index=2)

    # 3) 按索引匹配：gps_data[i] 对应 pts_ms_list[i]
    n_gps = len(gps_data)
    n_pts = len(pts_ms_list)
    if n_gps != n_pts:
        print(
            f"注意: GPS 点数 ({n_gps}) 与 metadata sample 数 ({n_pts}) 不一致，按较短长度匹配。",
            file=sys.stderr,
        )
    n = min(n_gps, n_pts)
    from datetime import timedelta
    # 以视频在 MP4 中的创建时间（mvhd）作为 timestamp_ms=0 的基准，而非 timeinfo 的整秒
    base_time = get_video_creation_time(str(video_path))
    if base_time is None:
        print("警告: 无法读取视频创建时间(mvhd)，将使用第一条 timeinfo 作为 0ms 基准。", file=sys.stderr)
        for i in range(n):
            t0 = gps_data[i].get("timeinfo")
            if t0 not in (None, ""):
                break
        else:
            t0 = None
        base_time = t0
    rows = []
    for i in range(n):
        timeinfo = gps_data[i].get("timeinfo") or ""
        row = {
            "timestamp_ms": pts_ms_list[i],
            "latitude": gps_data[i]["latitude"],
            "longitude": gps_data[i]["longitude"],
            "altitude": gps_data[i].get("altitude", ""),
            "timeinfo": timeinfo,
        }
        # 绝对时间（毫秒级）= 视频创建时间(UTC) + timestamp_ms
        if base_time is not None:
            try:
                t = base_time + timedelta(milliseconds=pts_ms_list[i])
                row["time_ms"] = t.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]  # 保留到毫秒 .000
            except Exception:
                row["time_ms"] = ""
        else:
            row["time_ms"] = ""
        rows.append(row)

    # 4) 输出 CSV
    if output_csv is None:
        output_csv = video_path.with_suffix(".gps_ms.csv")
    output_csv = Path(output_csv)
    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["timestamp_ms", "time_ms", "latitude", "longitude", "altitude", "timeinfo"],
        )
        w.writeheader()
        w.writerows(rows)
    print(f"已写入 {len(rows)} 条记录到 {output_csv}")

    # 5) 可选：输出 GPX
    if output_gpx:
        output_gpx = Path(output_gpx)
        _save_gpx_with_ms(rows, output_gpx)
        print(f"已写入 GPX: {output_gpx}")

    return True


def _save_gpx_with_ms(rows, output_gpx):
    """将带 timestamp_ms 的行写入 GPX，优先使用已计算的 time_ms。"""
    try:
        import gpxpy
        import gpxpy.gpx
        from datetime import datetime
    except ImportError:
        print("未安装 gpxpy，跳过 GPX 输出。可执行: pip install gpxpy", file=sys.stderr)
        return
    gpx = gpxpy.gpx.GPX()
    gpx.creator = "extract_gps_with_ms.py (pyosmogps + PTS)"
    track = gpxpy.gpx.GPXTrack()
    gpx.tracks.append(track)
    segment = gpxpy.gpx.GPXTrackSegment()
    track.segments.append(segment)
    for r in rows:
        lat = r["latitude"]
        lon = r["longitude"]
        alt = r.get("altitude")
        if alt == "" or alt is None:
            alt = 0
        time_ms_str = r.get("time_ms", "")
        point_time = None
        if time_ms_str:
            try:
                point_time = datetime.strptime(time_ms_str, "%Y-%m-%d %H:%M:%S.%f")
            except ValueError:
                try:
                    point_time = datetime.strptime(time_ms_str, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    pass
        point = gpxpy.gpx.GPXTrackPoint(
            latitude=lat,
            longitude=lon,
            elevation=alt,
            time=point_time,
        )
        segment.points.append(point)
    with open(output_gpx, "w", encoding="utf-8") as f:
        f.write(gpx.to_xml())


def main():
    parser = argparse.ArgumentParser(
        description="从 DJI Action 视频提取 GPS 及毫秒级时间戳（pyosmogps + MP4 PTS）"
    )
    parser.add_argument("video", help="输入视频路径")
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="输出 CSV 路径（默认: 视频同目录下 视频名.gps_ms.csv）",
    )
    parser.add_argument(
        "--gpx",
        default=None,
        help="可选：同时输出 GPX 路径",
    )
    parser.add_argument(
        "--tz",
        type=int,
        default=8,
        help="时区偏移（小时），默认 8",
    )
    args = parser.parse_args()

    ok = extract_gps_with_millisecond_timestamps(
        args.video,
        output_csv=args.output,
        output_gpx=args.gpx,
        timezone_offset=args.tz,
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
