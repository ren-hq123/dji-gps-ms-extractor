# dji-gps-ms-extractor
Extract GPS (lat/lon) with millisecond timestamps from DJI Action video files（I used these code for DJI action5pro with Osmo Action GPS Bluetooth Remote Controller）. Works with pyosmogps or raw djmd stream parsing. Output: CSV, optional GPX.

机型:	DJI Action 5 Pro（优先），其他 Osmo Action 系列需实测

轨道:	至少有第 3 轨（metadata/djmd）

内容:	录制时开启 GPS，视频内嵌有效 GPS 元数据

## extract_djigps_with_ms

从 DJI Action 5 Pro（及兼容机型）视频中提取 GPS 经纬度及毫秒级时间戳。支持 CSV 输出，可选 GPX。

Extract GPS coordinates with millisecond timestamps from DJI Action 5 Pro videos. Output: CSV, optional GPX.

---

## 功能 / Features

- 提取视频内嵌的 GPS (lat/lon) 及毫秒级 PTS
- 时间基准：MP4 mvhd 创建时间（非 timeinfo 整秒）
- 支持两种方式：**pyosmogps**（仅需视频）或 **djmd 原始流解析**（需 djmd.bin）
- 可选输出：CSV、GPX

---

## 依赖 / Requirements

- Python 3.7+
- **extract_gps_with_ms.py**（推荐）：`pip install pyosmogps`
- **parse_djmd_gps.py**：`pip install blackboxprotobuf`
- 可选 GPX：`pip install gpxpy`

---

## 方法一：extract_gps_with_ms.py（推荐，只需视频）

直接读取 MP4 视频，使用 pyosmogps 提取 GPS，搭配 MP4 metadata 轨时间戳。

```bash
python extract_gps_with_ms.py video.MP4
```

默认输出：`video.gps_ms.csv`

**选项：**

| 参数 | 说明 |
|------|------|
| `--output`, `-o` | 指定输出 CSV 路径 |
| `--gpx` | 同时输出 GPX 文件 |
| `--tz` | 时区偏移（小时），默认 8 |

**示例：**

```bash
python extract_gps_with_ms.py DJI_xxx.MP4
python extract_gps_with_ms.py DJI_xxx.MP4 -o out.csv
python extract_gps_with_ms.py DJI_xxx.MP4 -o out.csv --gpx track.gpx --tz 8
```

**输出 CSV 字段：** `timestamp_ms`, `time_ms`, `latitude`, `longitude`, `altitude`, `timeinfo`

---

## 方法二：parse_djmd_gps.py（需 djmd.bin）

适用于已单独提取 djmd 轨道的场景。需要视频文件和 djmd.bin。

**1. 用 ffmpeg 提取 djmd 轨道：**

```bash
ffmpeg -i video.MP4 -map 0:2 -c copy djmd.bin
```

（`0:2` 为 DJI Action 的 metadata 轨索引，若不同请自行调整）

**2. 运行脚本：**

```bash
python parse_djmd_gps.py video.MP4 djmd.bin
```

**默认输入：** 同目录下 `DJI_20260211104059_0058_D.MP4` 和 `djmd.bin`  
**默认输出：** `gps_timestamps.csv`

**选项：**

| 参数 | 说明 |
|------|------|
| `--output` | 指定输出 CSV 路径 |
| `--strict` | 仅保留高置信度 GPS（\|lat\|≥1 且 \|lon\|≥1） |
| `--debug` | 首包 protobuf 解码写入 debug_first_sample.json |

**示例：**

```bash
python parse_djmd_gps.py
python parse_djmd_gps.py video.MP4 djmd.bin -o gps.csv
python parse_djmd_gps.py video.MP4 djmd.bin --strict --output gps_strict.csv
```

**输出 CSV 字段：** `timestamp_ms`, `latitude`, `longitude`

---

## 输出示例 / Output Example

**extract_gps_with_ms.py 输出：**

| timestamp_ms | time_ms | latitude | longitude | altitude | timeinfo |
|--------------|---------|----------|-----------|----------|----------|
| 0 | 2026-02-11 18:40:59.000 | 31.xxxxx | 121.xxxxx | 10.5 | ... |
| 33 | 2026-02-11 18:40:59.033 | 31.xxxxx | 121.xxxxx | 10.5 | ... |

**parse_djmd_gps.py 输出：**

| timestamp_ms | latitude | longitude |
|--------------|----------|-----------|
| 0 | 31.xxxxx | 121.xxxxx |
| 33 | 31.xxxxx | 121.xxxxx |

---

## 两种方式对比 / Comparison

| 项目 | extract_gps_with_ms.py | parse_djmd_gps.py |
|------|------------------------|-------------------|
| 输入 | 只需 MP4 | MP4 + djmd.bin |
| 依赖 | pyosmogps | blackboxprotobuf |
| 输出 | CSV + 可选 GPX，含 altitude、time_ms | CSV，仅 timestamp_ms/lat/lon |
| 时间基准 | 使用 mvhd 创建时间 | 相对 PTS 毫秒 |

---

## License

MIT
