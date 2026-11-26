from bisect import bisect_left
from collections import defaultdict
import csv
from datetime import datetime, timedelta, timezone
import glob
import gzip
import json
import math
import os
from pathlib import Path

import jpholiday

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

BUS_MASTER_PATH = DATA_DIR / "bus_master.json"
OUTPUT_PATH = DATA_DIR / "analysis_summary.json"

STATION_FILES = {
    "docomo-cycle": DATA_DIR / "stations_docomo-cycle.json",
    "hellocycling": DATA_DIR / "stations_hellocycling.json",
}

MIN_DELAY_SEC = 180
MAX_DELAY_SEC = 5400
MATCH_WINDOW_SEC = 600  # 前後10分
PORT_RADIUS_M = 300.0
JST = timezone(timedelta(hours=9))
SEGMENT_KEYS = ("all", "weekday", "holiday")


def is_year_end_new_year(dt: datetime) -> bool:
    """年末年始（12/29〜1/3）の休日ダイヤ期間かどうかを返す。"""
    return (dt.month == 12 and dt.day >= 29) or (dt.month == 1 and dt.day <= 3)


def classify_day_segment(dt: datetime, date_str: str = "") -> str:
    """日付と曜日（土日・日本の祝日・年末年始）から 'holiday' または 'weekday' を返す。"""
    if dt.weekday() in (5, 6) or jpholiday.is_holiday(dt.date()) or is_year_end_new_year(dt):
        return "holiday"
    return "weekday"


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2.0 * r * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def parse_jst_epoch(ts_str: str) -> float:
    dt = datetime.strptime(ts_str.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)
    return dt.timestamp()


def parse_bool(val) -> bool:
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("true", "1", "yes")


def parse_int(val, default: int = 0) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def resolve_csv_paths(pattern: str) -> list[Path]:
    """同一日の .csv と .csv.gz が共存する場合でも重複なく対象ファイル一覧を返す。"""
    by_day_key: dict[str, Path] = {}
    for raw_path in sorted(glob.glob(str(DATA_DIR / pattern))):
        p = Path(raw_path)
        if not (p.name.endswith(".csv") or p.name.endswith(".csv.gz")):
            continue
        key = p.name[:-3] if p.name.endswith(".gz") else p.name
        # 非圧縮(.csv)があればそちらを優先
        if key not in by_day_key or not p.name.endswith(".gz"):
            by_day_key[key] = p
    return [by_day_key[k] for k in sorted(by_day_key)]


def iter_csv_rows(path: Path):
    opener = gzip.open if path.name.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", newline="") as f:
        yield from csv.DictReader(f)


def load_bikeshare_stations() -> list[tuple[str, str, float, float]]:
    stations: list[tuple[str, str, float, float]] = []
    for provider, path in STATION_FILES.items():
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        for st in data:
            sid = str(st.get("station_id") or "").strip()
            lat = st.get("lat")
            lon = st.get("lon")
            if not sid or lat is None or lon is None:
                continue
            stations.append((provider, sid, float(lat), float(lon)))
    return stations


def build_stop_port_index(
    stops: dict[str, dict], stations: list[tuple[str, str, float, float]]
) -> tuple[dict[str, list[tuple[str, str]]], dict[str, int | None]]:
    """各バス停から半径300m以内のポート一覧および最寄りポート距離を事前計算する。"""
    nearby_map: dict[str, list[tuple[str, str]]] = {}
    nearest_dist_map: dict[str, int | None] = {}

    for stop_id, st_info in stops.items():
        s_lat = st_info["lat"]
        s_lon = st_info["lon"]
        near_ports: list[tuple[str, str]] = []
        min_dist: float | None = None

        for provider, port_id, p_lat, p_lon in stations:
            d_lat = abs(p_lat - s_lat)
            d_lon = abs(p_lon - s_lon)
            # 高速フィルタ: 最寄り距離算出にも十分広い範囲のときだけ厳密距離を算出、
            # ただし全ポート最寄り距離のため最初は簡易下限と併用する
            if min_dist is None or (d_lat <= 0.05 and d_lon <= 0.05) or (d_lat <= 0.004 and d_lon <= 0.004):
                dist = haversine_m(s_lat, s_lon, p_lat, p_lon)
                if min_dist is None or dist < min_dist:
                    min_dist = dist
                if dist <= PORT_RADIUS_M:
                    near_ports.append((provider, port_id))

        # 万が一近傍0.05度以内に1件もなかった場合のみ全件から最寄りを求める
        if min_dist is None and stations:
            for _, _, p_lat, p_lon in stations:
                dist = haversine_m(s_lat, s_lon, p_lat, p_lon)
                if min_dist is None or dist < min_dist:
                    min_dist = dist

        nearby_map[stop_id] = near_ports
        nearest_dist_map[stop_id] = int(round(min_dist)) if min_dist is not None else None

    return nearby_map, nearest_dist_map


def load_bikeshare_snapshots(
    provider: str,
) -> tuple[list[float], dict[float, dict[str, tuple[bool, int]]]]:
    snapshots: dict[float, dict[str, tuple[bool, int]]] = defaultdict(dict)
    ts_str_cache: dict[str, float] = {}

    for path in resolve_csv_paths(f"status_{provider}_*.csv*"):
        for row in iter_csv_rows(path):
            ts_str = (row.get("timestamp_jst") or "").strip()
            sid = (row.get("station_id") or "").strip()
            if not ts_str or not sid:
                continue
            epoch = ts_str_cache.get(ts_str)
            if epoch is None:
                epoch = parse_jst_epoch(ts_str)
                ts_str_cache[ts_str] = epoch
            is_renting = parse_bool(row.get("is_renting", ""))
            bikes = parse_int(row.get("num_bikes_available"), 0)
            snapshots[epoch][sid] = (is_renting, bikes)

    sorted_epochs = sorted(snapshots.keys())
    return sorted_epochs, snapshots


def find_closest_snapshot(
    target_epoch: float,
    sorted_epochs: list[float],
    snapshots: dict[float, dict[str, tuple[bool, int]]],
) -> dict[str, tuple[bool, int]] | None:
    if not sorted_epochs:
        return None
    idx = bisect_left(sorted_epochs, target_epoch)
    best_epoch = None
    best_diff = MATCH_WINDOW_SEC + 1.0

    for cand_idx in (idx - 1, idx):
        if 0 <= cand_idx < len(sorted_epochs):
            cand_epoch = sorted_epochs[cand_idx]
            diff = abs(cand_epoch - target_epoch)
            if diff <= MATCH_WINDOW_SEC and diff < best_diff:
                best_diff = diff
                best_epoch = cand_epoch

    if best_epoch is None:
        return None
    return snapshots[best_epoch]


def pct(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round((numerator / denominator) * 100.0, 1)


def create_segment_accumulator() -> dict:
    """区分（all / weekday / holiday）ごとの集計アキュムレータを初期化する。"""
    return {
        "total_valid_delays": 0,
        "overall_counts": {"available": 0, "empty": 0, "no_port": 0},
        "valid_timestamps": set(),
        "min_date": None,
        "max_date": None,
        "stop_stats": defaultdict(
            lambda: {
                "delay_count": 0,
                "total_delay_sec": 0,
                "available_count": 0,
                "empty_count": 0,
                "no_port_count": 0,
            }
        ),
        "hourly_stats": [
            {
                "hour": h,
                "delay_count": 0,
                "total_delay_sec": 0,
                "available_count": 0,
                "empty_count": 0,
                "no_port_count": 0,
            }
            for h in range(24)
        ],
        "route_stats": defaultdict(
            lambda: {
                "route_name": "",
                "delay_count": 0,
                "total_delay_sec": 0,
                "available_count": 0,
                "empty_count": 0,
                "no_port_count": 0,
            }
        ),
    }


def record_collected_date(acc: dict, date_part: str) -> None:
    """収集対象となった日付をアキュムレータの集計期間（min_date / max_date）へ反映する。"""
    if acc["min_date"] is None or date_part < acc["min_date"]:
        acc["min_date"] = date_part
    if acc["max_date"] is None or date_part > acc["max_date"]:
        acc["max_date"] = date_part


def record_delay_event(
    acc: dict,
    *,
    ts_str: str,
    date_part: str,
    hour: int,
    stop_id: str,
    delay_sec: int,
    status_cat: str,
    route_id: str,
    route_name: str,
) -> None:
    """単一の有効遅延イベントをアキュムレータへ加算する。"""
    acc["total_valid_delays"] += 1
    acc["overall_counts"][status_cat] += 1
    acc["valid_timestamps"].add(ts_str)
    record_collected_date(acc, date_part)

    count_key = f"{status_cat}_count"

    st_acc = acc["stop_stats"][stop_id]
    st_acc["delay_count"] += 1
    st_acc["total_delay_sec"] += delay_sec
    st_acc[count_key] += 1

    hr_acc = acc["hourly_stats"][hour]
    hr_acc["delay_count"] += 1
    hr_acc["total_delay_sec"] += delay_sec
    hr_acc[count_key] += 1

    rt_acc = acc["route_stats"][route_id]
    rt_acc["route_name"] = route_name
    rt_acc["delay_count"] += 1
    rt_acc["total_delay_sec"] += delay_sec
    rt_acc[count_key] += 1


def build_segment_summary(
    acc: dict,
    *,
    now_jst: str,
    stops: dict[str, dict],
    nearest_dist_map: dict[str, int | None],
) -> dict:
    """アキュムレータから meta / problem_stops / hourly_trends / route_summary を構築する。"""
    total_valid_delays = acc["total_valid_delays"]
    overall_counts = acc["overall_counts"]
    min_date = acc["min_date"]
    max_date = acc["max_date"]
    date_range = f"{min_date} 〜 {max_date}" if min_date and max_date else ""

    meta = {
        "updated_at_jst": now_jst,
        "date_range": date_range,
        "total_snapshots": len(acc["valid_timestamps"]),
        "total_valid_delays": total_valid_delays,
        "overall_available_rate": pct(overall_counts["available"], total_valid_delays),
        "overall_empty_rate": pct(overall_counts["empty"], total_valid_delays),
        "overall_no_port_rate": pct(overall_counts["no_port"], total_valid_delays),
    }

    problem_candidates = []
    for stop_id, st_acc in acc["stop_stats"].items():
        issue_count = st_acc["empty_count"] + st_acc["no_port_count"]
        if issue_count <= 0:
            continue
        stop_info = stops[stop_id]
        issue_type = (
            "no_port" if st_acc["no_port_count"] >= st_acc["empty_count"] else "empty"
        )
        cnt = st_acc["delay_count"]
        problem_candidates.append(
            {
                "stop_id": stop_id,
                "stop_name": stop_info["name"],
                "lat": stop_info["lat"],
                "lon": stop_info["lon"],
                "delay_count": cnt,
                "avg_delay_min": round((st_acc["total_delay_sec"] / cnt) / 60.0, 1) if cnt else 0.0,
                "available_count": st_acc["available_count"],
                "empty_count": st_acc["empty_count"],
                "no_port_count": st_acc["no_port_count"],
                "nearest_port_dist_m": nearest_dist_map.get(stop_id),
                "issue_type": issue_type,
                "_issue_count": issue_count,
            }
        )

    problem_candidates.sort(
        key=lambda x: (x["_issue_count"], x["delay_count"], x["avg_delay_min"]),
        reverse=True,
    )
    problem_stops = [
        {k: v for k, v in item.items() if k != "_issue_count"}
        for item in problem_candidates[:30]
    ]

    hourly_trends = []
    for hr_acc in acc["hourly_stats"]:
        cnt = hr_acc["delay_count"]
        avg_min = round((hr_acc["total_delay_sec"] / cnt) / 60.0, 1) if cnt else 0.0
        hourly_trends.append(
            {
                "hour": hr_acc["hour"],
                "delay_count": cnt,
                "avg_delay_min": avg_min,
                "available_count": hr_acc["available_count"],
                "empty_count": hr_acc["empty_count"],
                "no_port_count": hr_acc["no_port_count"],
                "available_rate": pct(hr_acc["available_count"], cnt),
            }
        )

    route_list = []
    for route_id, rt_acc in acc["route_stats"].items():
        cnt = rt_acc["delay_count"]
        route_list.append(
            {
                "route_id": route_id,
                "route_name": rt_acc["route_name"],
                "delay_count": cnt,
                "avg_delay_min": round((rt_acc["total_delay_sec"] / cnt) / 60.0, 1) if cnt else 0.0,
                "available_rate": pct(rt_acc["available_count"], cnt),
                "empty_rate": pct(rt_acc["empty_count"], cnt),
                "no_port_rate": pct(rt_acc["no_port_count"], cnt),
            }
        )
    route_list.sort(key=lambda x: (x["delay_count"], x["avg_delay_min"]), reverse=True)
    route_summary = route_list[:30]

    return {
        "meta": meta,
        "problem_stops": problem_stops,
        "hourly_trends": hourly_trends,
        "route_summary": route_summary,
    }


def main():
    if not BUS_MASTER_PATH.exists():
        raise FileNotFoundError(f"バス停・系統マスタが見つかりません: {BUS_MASTER_PATH}")

    with BUS_MASTER_PATH.open("r", encoding="utf-8") as f:
        bus_master = json.load(f)

    stops: dict[str, dict] = bus_master.get("stops", {})
    trips: dict[str, dict] = bus_master.get("trips", {})

    stations = load_bikeshare_stations()
    nearby_map, nearest_dist_map = build_stop_port_index(stops, stations)

    provider_epochs: dict[str, list[float]] = {}
    provider_snapshots: dict[str, dict[float, dict[str, tuple[bool, int]]]] = {}
    for provider in STATION_FILES:
        epochs, snaps = load_bikeshare_snapshots(provider)
        provider_epochs[provider] = epochs
        provider_snapshots[provider] = snaps

    # 区分別（all / weekday / holiday）集計用アキュムレータ
    accumulators = {key: create_segment_accumulator() for key in SEGMENT_KEYS}

    # 有効遅延が0件の日の収集分も集計期間に含めるため、全スナップショット日付を反映
    seen_collected_dates: set[str] = set()
    for epochs in provider_epochs.values():
        for epoch in epochs:
            dt = datetime.fromtimestamp(epoch, JST)
            date_str = dt.strftime("%Y-%m-%d")
            if date_str in seen_collected_dates:
                continue
            seen_collected_dates.add(date_str)
            day_seg = classify_day_segment(dt, date_str)
            for seg_key in ("all", day_seg):
                record_collected_date(accumulators[seg_key], date_str)

    # 同一タイムスタンプのパース・区分判定および前後10分スナップショット突合のキャッシュ
    ts_parse_cache: dict[str, tuple[float, int, str, str]] = {}
    matched_snap_cache: dict[float, dict[str, dict[str, tuple[bool, int]] | None]] = {}

    for path in resolve_csv_paths("bus_delay_yokohama_*.csv*"):
        for row in iter_csv_rows(path):
            ts_str = (row.get("timestamp_jst") or "").strip()
            if not ts_str:
                continue

            parsed = ts_parse_cache.get(ts_str)
            if parsed is None:
                dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)
                date_str = ts_str[:10]
                parsed = (dt.timestamp(), dt.hour, date_str, classify_day_segment(dt, date_str))
                ts_parse_cache[ts_str] = parsed
                if date_str not in seen_collected_dates:
                    seen_collected_dates.add(date_str)
                    for seg_key in ("all", parsed[3]):
                        record_collected_date(accumulators[seg_key], date_str)
            bus_epoch, hour, date_part, day_segment = parsed

            delay_sec = parse_int(row.get("delay_sec"), -1)
            if not (MIN_DELAY_SEC <= delay_sec <= MAX_DELAY_SEC):
                continue

            stop_id = (row.get("stop_id") or "").strip()
            if stop_id not in stops:
                continue

            snap_by_provider = matched_snap_cache.get(bus_epoch)
            if snap_by_provider is None:
                snap_by_provider = {
                    prov: find_closest_snapshot(
                        bus_epoch, provider_epochs[prov], provider_snapshots[prov]
                    )
                    for prov in STATION_FILES
                }
                matched_snap_cache[bus_epoch] = snap_by_provider

            # 少なくとも1つの事業者スナップショットが前後10分以内に存在することを確認
            if not any(snap is not None for snap in snap_by_provider.values()):
                continue

            near_ports = nearby_map.get(stop_id, [])
            if not near_ports:
                status_cat = "no_port"
            else:
                has_available = False
                for prov, port_id in near_ports:
                    snap = snap_by_provider.get(prov)
                    if snap is None:
                        continue
                    port_state = snap.get(port_id)
                    if port_state is None:
                        continue
                    is_renting, bikes = port_state
                    if is_renting and bikes >= 1:
                        has_available = True
                        break
                status_cat = "available" if has_available else "empty"

            trip_id = (row.get("trip_id") or "").strip()
            trip_info = trips.get(trip_id)
            if trip_info:
                route_id = trip_info["route_id"]
                route_name = trip_info["route_name"]
            else:
                route_id = "unknown"
                route_name = "不明系統"

            for seg_key in ("all", day_segment):
                record_delay_event(
                    accumulators[seg_key],
                    ts_str=ts_str,
                    date_part=date_part,
                    hour=hour,
                    stop_id=stop_id,
                    delay_sec=delay_sec,
                    status_cat=status_cat,
                    route_id=route_id,
                    route_name=route_name,
                )

    now_jst = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    segments = {
        key: build_segment_summary(
            accumulators[key],
            now_jst=now_jst,
            stops=stops,
            nearest_dist_map=nearest_dist_map,
        )
        for key in SEGMENT_KEYS
    }
    all_summary = segments["all"]
    summary = {
        **all_summary,
        "segments": segments,
    }

    tmp_path = OUTPUT_PATH.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, OUTPUT_PATH)

    all_meta = all_summary["meta"]
    weekday_cnt = segments["weekday"]["meta"]["total_valid_delays"]
    holiday_cnt = segments["holiday"]["meta"]["total_valid_delays"]
    print(
        f"分析サマリー生成完了: {OUTPUT_PATH} "
        f"(有効遅延={all_meta['total_valid_delays']}件 "
        f"[平日={weekday_cnt}件, 土日祝={holiday_cnt}件], "
        f"代替可能={all_meta['overall_available_rate']}%, "
        f"在庫切れ={all_meta['overall_empty_rate']}%, "
        f"ポート空白={all_meta['overall_no_port_rate']}%)"
    )


if __name__ == "__main__":
    main()
