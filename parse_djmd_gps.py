#!/usr/bin/env python3
"""
解析 DJI Action 5 Pro 视频的 djmd 流，提取所有 GPS 经纬度及对应的毫秒级时间戳。

依赖: pip install blackboxprotobuf

用法:
  python parse_djmd_gps.py
  (会读取同目录下的 DJI_20260211104059_0058_D.MP4 和 djmd.bin)

或:
  python parse_djmd_gps.py <video.mp4> <djmd.bin> [--output out.csv]
  python parse_djmd_gps.py --strict   # 仅输出高置信度 GPS（|lat|>=1, |lon|>=1）
  python parse_djmd_gps.py --debug    # 首包解码结果写入 debug_first_sample.json
"""

import struct
import sys
import os
import json
import csv
from pathlib import Path

# 可选：无 schema 解析 protobuf
try:
    import blackboxprotobuf
    HAS_BLACKBOX = True
except ImportError:
    HAS_BLACKBOX = False


# ---------- MP4 解析：获取指定 trak 的 sample 大小与时间 ----------
def read_atom(f):
    """读取一个 atom 的 size 和 type，不移动到 payload。"""
    head = f.read(8)
    if len(head) < 8:
        return None, None, 0
    size = struct.unpack(">I", head[:4])[0]
    atype = head[4:8].decode("latin-1")
    return size, atype, 8


def find_atom_in_file(path, target_type, max_bytes=100 * 1024 * 1024, tail_bytes=50 * 1024 * 1024):
    """
    在文件中查找 target_type atom，返回 (payload_start, payload_size)。
    支持大文件：moov 常在 mdat 之后（文件末尾），若前 100MB 未找到则从文件末尾读取再搜。
    """
    with open(path, "rb") as f:
        file_size = f.seek(0, 2)  # 获取文件大小
        f.seek(0)
        data = f.read(min(max_bytes, file_size))
        # 1) 先从头按 atom 顺序找
        offset = 0
        while offset + 8 <= len(data):
            size = struct.unpack(">I", data[offset : offset + 4])[0]
            atype = data[offset + 4 : offset + 8].decode("latin-1")
            if size < 8:
                break
            if atype == target_type:
                return offset + 8, size - 8
            offset += size
        # 2) 在当前 data 中搜索
        idx = 0
        while True:
            idx = data.find(target_type.encode("latin-1"), idx)
            if idx < 0:
                break
            if idx >= 4:
                size = struct.unpack(">I", data[idx - 4 : idx])[0]
                if 8 <= size <= len(data) - (idx - 4):
                    return idx + 4, size - 8
            idx += 1
        # 3) 大文件：moov 在末尾（ftyp + mdat 很大 + moov），从文件末尾读取
        if file_size > max_bytes and target_type == "moov":
            read_tail = min(tail_bytes, file_size)
            f.seek(file_size - read_tail)
            tail_data = f.read(read_tail)
            idx = 0
            while True:
                idx = tail_data.find(b"moov", idx)
                if idx < 0:
                    break
                if idx >= 4:
                    size = struct.unpack(">I", tail_data[idx - 4 : idx])[0]
                    if 8 <= size <= len(tail_data) - (idx - 4):
                        payload_start = file_size - read_tail + idx + 4
                        return payload_start, size - 8
                idx += 1
    return None


def read_box_payload(f, size):
    return f.read(size) if size > 0 else b""


def parse_stsz(data):
    """解析 stsz box：sample_count, [entry_size, ...]"""
    if len(data) < 8:
        return [], 0
    version_flags = struct.unpack(">I", data[:4])[0]
    sample_size = struct.unpack(">I", data[4:8])[0]
    sample_count = struct.unpack(">I", data[8:12])[0]
    sizes = []
    if sample_size != 0:
        sizes = [sample_size] * sample_count
    else:
        off = 12
        for _ in range(sample_count):
            if off + 4 > len(data):
                break
            sizes.append(struct.unpack(">I", data[off : off + 4])[0])
            off += 4
    return sizes, sample_count


def parse_stts(data):
    """解析 stts：返回 [(sample_count, sample_delta), ...]"""
    if len(data) < 8:
        return []
    version_flags = struct.unpack(">I", data[:4])[0]
    entry_count = struct.unpack(">I", data[4:8])[0]
    entries = []
    off = 8
    for _ in range(entry_count):
        if off + 8 > len(data):
            break
        count = struct.unpack(">I", data[off : off + 4])[0]
        delta = struct.unpack(">I", data[off + 4 : off + 8])[0]
        entries.append((count, delta))
        off += 8
    return entries


def parse_mdhd(data):
    """解析 mdhd，返回 (timescale,)。"""
    if len(data) < 20:
        return 24000
    version = struct.unpack(">I", data[:4])[0]
    if version == 0:
        # 32bit timescale at offset 12
        return struct.unpack(">I", data[12:16])[0]
    elif version == 1:
        return struct.unpack(">I", data[28:32])[0]
    return 24000


def find_boxes(data, box_type):
    """在 data 中递归查找所有 box_type 的 (payload_start, payload_len) 列表。"""
    result = []
    off = 0
    while off + 8 <= len(data):
        size = struct.unpack(">I", data[off : off + 4])[0]
        atype = data[off + 4 : off + 8].decode("latin-1")
        if size < 8:
            break
        payload_start = off + 8
        payload_len = size - 8
        if atype == box_type:
            result.append((payload_start, payload_len))
        if atype in ("moov", "trak", "mdia", "minf", "stbl"):
            sub = data[payload_start : payload_start + payload_len]
            for a, b in find_boxes(sub, box_type):
                result.append((payload_start + a, b))
        off += size
    return result


def get_trak_stbl(video_path, trak_index=2):
    """
    获取第 trak_index 个 trak（0-based）的 stbl 内的 stsz 和 stts，以及 mdhd 的 timescale。
    返回: (sizes_list, timescale, stts_entries_for_expand)
    """
    with open(video_path, "rb") as f:
        moov_pos = find_atom_in_file(video_path, "moov")
        if not moov_pos:
            raise SystemExit("未找到 moov")
        payload_start, payload_len = moov_pos
        f.seek(payload_start - 8)  # 回到 atom 头
        f.read(8)  # size + type
        moov_payload = f.read(payload_len)
    # 找所有 trak
    traks = find_boxes(moov_payload, "trak")
    if trak_index >= len(traks):
        raise SystemExit(f"视频中 trak 数量不足，需要第 {trak_index + 1} 个 trak")
    trak_start, trak_len = traks[trak_index]
    trak_data = moov_payload[trak_start : trak_start + trak_len]
    # 在该 trak 下找 stbl
    stbl_list = find_boxes(trak_data, "stbl")
    if not stbl_list:
        raise SystemExit("该 trak 下未找到 stbl")
    stbl_start, stbl_len = stbl_list[0]
    stbl_data = trak_data[stbl_start : stbl_start + stbl_len]
    # stsz / stts 可能在 stbl 直接子层
    stsz_list = find_boxes(stbl_data, "stsz")
    stts_list = find_boxes(stbl_data, "stts")
    if not stsz_list or not stts_list:
        raise SystemExit("未找到 stsz 或 stts")
    stsz_start, stsz_len = stsz_list[0]
    stts_start, stts_len = stts_list[0]
    stsz_payload = stbl_data[stsz_start : stsz_start + stsz_len]
    stts_payload = stbl_data[stts_start : stts_start + stts_len]
    sizes, _ = parse_stsz(stsz_payload)
    stts_entries = parse_stts(stts_payload)
    # timescale 来自同一 trak 的 mdia -> mdhd
    mdhd_list = find_boxes(trak_data, "mdhd")
    timescale = 24000
    if mdhd_list:
        mdhd_start, mdhd_len = mdhd_list[0]
        mdhd_payload = trak_data[mdhd_start : mdhd_start + mdhd_len]
        timescale = parse_mdhd(mdhd_payload)
    return sizes, timescale, stts_entries


def build_pts_ms(sizes, timescale, stts_entries):
    """根据 stts 展开每个 sample 的 duration，得到每个 sample 的 PTS（毫秒）。"""
    # 展开 stts: 每个 (count, delta) 表示 count 个 sample，每个 duration = delta
    durations = []
    for count, delta in stts_entries:
        durations.extend([delta] * count)
    # 若 duration 数量与 sizes 不一致，按 sizes 长度截断或用最后一个 delta 填充
    if len(durations) < len(sizes):
        last_delta = durations[-1] if durations else 0
        durations.extend([last_delta] * (len(sizes) - len(durations)))
    elif len(durations) > len(sizes):
        durations = durations[: len(sizes)]
    pts_ticks = 0
    pts_ms_list = []
    for d in durations:
        pts_ms_list.append(pts_ticks * 1000 // timescale)
        pts_ticks += d
    return pts_ms_list


def get_metadata_track_pts_ms(video_path, trak_index=2):
    """
    获取 MP4 中 metadata 轨（djmd）每个 sample 的 PTS（毫秒）。
    与 pyosmogps 使用的轨道一致（第 3 条轨，0-based index=2）。
    返回: list[int]，长度为 metadata sample 数量。
    """
    sizes, timescale, stts_entries = get_trak_stbl(video_path, trak_index=trak_index)
    return build_pts_ms(sizes, timescale, stts_entries)


def get_video_creation_time(video_path):
    """
    从 MP4 的 mvhd 盒读取视频创建时间（UTC）。
    该时间对应视频时间轴 0 点，即 timestamp_ms=0 的绝对时间。
    返回: datetime (UTC)，若解析失败则返回 None。
    """
    from datetime import datetime, timedelta, timezone
    with open(video_path, "rb") as f:
        moov_pos = find_atom_in_file(video_path, "moov")
        if not moov_pos:
            return None
        payload_start, payload_len = moov_pos
        f.seek(payload_start - 8)
        f.read(8)
        moov_payload = f.read(payload_len)
    mvhd_list = find_boxes(moov_payload, "mvhd")
    if not mvhd_list:
        return None
    mvhd_start, mvhd_len = mvhd_list[0]
    mvhd_data = moov_payload[mvhd_start : mvhd_start + mvhd_len]
    if len(mvhd_data) < 16:
        return None
    version = struct.unpack(">I", mvhd_data[:4])[0]
    # 1904-01-01 00:00:00 UTC 为 MP4 时间基准
    epoch_1904 = datetime(1904, 1, 1, tzinfo=timezone.utc)
    if version == 0:
        creation_sec = struct.unpack(">I", mvhd_data[8:12])[0]
    elif version == 1:
        if len(mvhd_data) < 24:
            return None
        creation_sec = struct.unpack(">Q", mvhd_data[8:16])[0]
    else:
        return None
    return epoch_1904 + timedelta(seconds=creation_sec)


# ---------- 从 protobuf 解码结果中查找 GPS（纬度、经度）----------
def is_valid_lat(x):
    try:
        v = float(x)
        return -90 <= v <= 90
    except (TypeError, ValueError):
        return False


def is_valid_lon(x):
    try:
        v = float(x)
        return -180 <= v <= 180
    except (TypeError, ValueError):
        return False


def is_plausible_gps(lat, lon):
    """过滤明显噪声：要求至少 0.1 度、非 NaN、且排除常见误检（如帧率 23.976）。"""
    try:
        la, lo = float(lat), float(lon)
        if la != la or lo != lo:  # NaN
            return False
        if abs(la) > 90 or abs(lo) > 180:
            return False
        if abs(la) < 0.01 or abs(lo) < 0.01:
            return False
        if abs(la) > 1e10 or abs(lo) > 1e10:
            return False
        # 排除常见误检：帧率等 (23.976, lon) 或 (lat, 23.976)
        for rate in (23.976, 24.0, 29.97, 30.0, 59.94, 60.0):
            if abs(la - rate) < 0.01 or abs(lo - rate) < 0.01:
                return False
        return True
    except (TypeError, ValueError):
        return False


def _normalize_value(v):
    """将可能为 1e-7 整型的值转为度数。支持 int/float 及字符串形式的数字。"""
    if isinstance(v, str):
        try:
            v = int(v)
        except ValueError:
            try:
                return float(v)
            except ValueError:
                return None
    if isinstance(v, int):
        if abs(v) > 1e6 and abs(v) < 2e9:
            return v / 1e7
        return float(v)
    if isinstance(v, float):
        if abs(v) > 1e6 and abs(v) < 2e9:
            return v / 1e7
        return v
    return None


def _all_numbers_from_obj(obj, out=None):
    """递归收集所有数值（含 1e7 整型）。"""
    if out is None:
        out = []
    if isinstance(obj, dict):
        for v in obj.values():
            _all_numbers_from_obj(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _all_numbers_from_obj(v, out)
    elif isinstance(obj, (int, float)):
        out.append(obj)
    return out


def find_gps_in_obj(obj, path=""):
    """
    在 blackboxprotobuf 返回的嵌套 dict/list 中递归查找可能是 (lat, lon) 的数对。
    返回 [(lat, lon), ...] 列表。
    支持 (lat, lon) 或 (lon, lat) 顺序，以及 int/1e7 与 float 两种表示。
    """
    out = []
    if isinstance(obj, dict):
        vals = list(obj.values())
        for i, v in enumerate(vals):
            out.extend(find_gps_in_obj(v, f"{path}.{i}"))
        for i in range(len(vals) - 1):
            a, b = _normalize_value(vals[i]), _normalize_value(vals[i + 1])
            if a is not None and b is not None:
                if is_valid_lat(a) and is_valid_lon(b):
                    out.append((a, b))
                elif is_valid_lat(b) and is_valid_lon(a):
                    out.append((b, a))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(find_gps_in_obj(v, f"{path}[{i}]"))
        for i in range(len(obj) - 1):
            a, b = _normalize_value(obj[i]), _normalize_value(obj[i + 1])
            if a is not None and b is not None:
                if is_valid_lat(a) and is_valid_lon(b):
                    out.append((a, b))
                elif is_valid_lat(b) and is_valid_lon(a):
                    out.append((b, a))
    return out


def decode_protobuf_safe(payload):
    """用 blackboxprotobuf 解码，失败返回 None。"""
    if not HAS_BLACKBOX or not payload:
        return None
    try:
        message, _ = blackboxprotobuf.protobuf_to_json(payload)
        return message
    except Exception:
        return None


def _decode_varint(data, start):
    """从 data[start:] 解码一个 varint，返回 (value, num_bytes)。"""
    n = 0
    shift = 0
    for i in range(min(10, len(data) - start)):
        b = data[start + i]
        n |= (b & 0x7F) << shift
        if b < 0x80:
            return n, i + 1
        shift += 7
    return None, 0


def _wire_extract_numbers(data, limit=5000):
    """从 protobuf wire 格式中递归提取所有数值（varint, fixed32, fixed64, float）。"""
    nums = []
    off = 0
    while off < len(data) and len(nums) < limit:
        if off + 1 > len(data):
            break
        tag, n = _decode_varint(data, off)
        if n == 0:
            break
        off += n
        wire_type = tag & 0x7
        field_num = tag >> 3
        if wire_type == 0:  # Varint
            v, n = _decode_varint(data, off)
            if n == 0:
                break
            off += n
            # 转为有符号（protobuf 负数为补码）
            if v >= 0x8000000000000000:
                v = v - 0x10000000000000000
            deg = v / 1e7
            if -90 <= deg <= 90 or -180 <= deg <= 180:
                nums.append(deg)
        elif wire_type == 1:  # 64-bit
            if off + 8 > len(data):
                break
            # 尝试 double
            try:
                d = struct.unpack("<d", data[off : off + 8])[0]
                if abs(d) < 1e10 and d == d:
                    nums.append(d)
            except Exception:
                pass
            off += 8
        elif wire_type == 5:  # 32-bit
            if off + 4 > len(data):
                break
            w = data[off : off + 4]
            try:
                fi = struct.unpack("<i", w)[0]
                deg = fi / 1e7
                if -90 <= deg <= 90 or -180 <= deg <= 180:
                    nums.append(deg)
            except Exception:
                pass
            try:
                ff = struct.unpack("<f", w)[0]
                if abs(ff) < 1e10 and ff == ff:
                    nums.append(ff)
            except Exception:
                pass
            off += 4
        elif wire_type == 2:  # Length-delimited
            length, n = _decode_varint(data, off)
            if n == 0 or off + n + length > len(data):
                break
            off += n
            sub = data[off : off + length]
            off += length
            nums.extend(_wire_extract_numbers(sub, limit - len(nums)))
        else:
            break
    return nums


def extract_gps_from_payload_raw(payload):
    """
    不依赖 schema：按 protobuf wire 解析出所有数值，再查找相邻的 (lat, lon) 对。
    """
    gps_list = []
    nums = _wire_extract_numbers(payload)
    for j in range(len(nums) - 1):
        a, b = nums[j], nums[j + 1]
        if is_valid_lat(a) and is_valid_lon(b):
            gps_list.append((a, b))
        elif is_valid_lat(b) and is_valid_lon(a):
            gps_list.append((b, a))
    return gps_list


def extract_gps_from_payload(payload):
    """从单个 djmd sample 的二进制 payload 中提取 GPS 列表。"""
    gps_list = []
    root = decode_protobuf_safe(payload)
    if root is not None:
        if isinstance(root, dict):
            root = [root]
        for item in root:
            gps_list.extend(find_gps_in_obj(item))
    if not gps_list:
        gps_list = extract_gps_from_payload_raw(payload)
    return gps_list


# ---------- 主流程 ----------
def main():
    script_dir = Path(__file__).resolve().parent
    video_path = script_dir / "DJI_20260211104059_0058_D.MP4"
    djmd_path = script_dir / "djmd.bin"
    output_path = script_dir / "gps_timestamps.csv"
    debug = False
    strict_gps = False  # 仅保留 |lat|>=1 且 |lon|>=1 的高置信度点

    args = sys.argv[1:]
    if "--debug" in args:
        args.remove("--debug")
        debug = True
    if "--strict" in args:
        args.remove("--strict")
        strict_gps = True
    if args and not args[0].startswith("-"):
        video_path = Path(args[0])
        args = args[1:]
    if args and not args[0].startswith("-"):
        djmd_path = Path(args[0])
        args = args[1:]
    while args and args[0] == "--output":
        args.pop(0)
        if args:
            output_path = Path(args.pop(0))

    if not video_path.is_file():
        print(f"视频文件不存在: {video_path}", file=sys.stderr)
        sys.exit(1)
    if not djmd_path.is_file():
        print(f"djmd 文件不存在: {djmd_path}", file=sys.stderr)
        sys.exit(1)
    if not HAS_BLACKBOX:
        print("请安装 blackboxprotobuf: pip install blackboxprotobuf", file=sys.stderr)
        sys.exit(1)

    # 1) 从 MP4 获取 djmd 轨道的 sample 大小与 PTS(ms)
    sizes, timescale, stts_entries = get_trak_stbl(str(video_path), trak_index=2)
    pts_ms_list = build_pts_ms(sizes, timescale, stts_entries)
    if len(sizes) != len(pts_ms_list):
        pts_ms_list = pts_ms_list[: len(sizes)]

    # 2) 读取 djmd.bin 并按 sizes 切分
    with open(djmd_path, "rb") as f:
        raw = f.read()
    offset = 0
    results = []  # [(timestamp_ms, lat, lon), ...]
    for i, (sz, pts_ms) in enumerate(zip(sizes, pts_ms_list)):
        if offset + sz > len(raw):
            break
        chunk = raw[offset : offset + sz]
        offset += sz
        if debug and i == 0 and HAS_BLACKBOX:
            try:
                msg, _ = blackboxprotobuf.protobuf_to_json(chunk)
                with open(script_dir / "debug_first_sample.json", "w", encoding="utf-8") as dbg:
                    json.dump(msg, dbg, indent=2, ensure_ascii=False, default=str)
                print("Debug: 已写入 debug_first_sample.json", file=sys.stderr)
            except Exception as e:
                print(f"Debug decode error: {e}", file=sys.stderr)
        gps_list = extract_gps_from_payload(chunk)
        for lat, lon in gps_list:
            if not is_plausible_gps(lat, lon):
                continue
            if strict_gps and (abs(lat) < 1 or abs(lon) < 1):
                continue
            results.append((pts_ms, lat, lon))

    # 去重：同一 ms 的相同经纬度只保留一条
    seen = set()
    unique = []
    for pts_ms, lat, lon in results:
        key = (pts_ms, round(lat, 7), round(lon, 7))
        if key not in seen:
            seen.add(key)
            unique.append((pts_ms, round(lat, 7), round(lon, 7)))

    # 3) 输出
    with open(output_path, "w", newline="", encoding="utf-8") as out:
        w = csv.writer(out)
        w.writerow(["timestamp_ms", "latitude", "longitude"])
        for pts_ms, lat, lon in unique:
            w.writerow([pts_ms, lat, lon])

    print(f"已解析 {len(unique)} 条 GPS 记录，已写入: {output_path}")
    # 同时打印前几条
    for pts_ms, lat, lon in unique[:20]:
        print(f"  {pts_ms} ms  ->  {lat}, {lon}")
    if len(unique) > 20:
        print(f"  ... 共 {len(unique)} 条")


if __name__ == "__main__":
    main()
