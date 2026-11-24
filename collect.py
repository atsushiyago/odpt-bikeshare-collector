import csv
import gzip
import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests

# 取得対象エリア（横浜市全域＋大和市）
LAT_MIN, LAT_MAX = 35.31, 35.60
LON_MIN, LON_MAX = 139.42, 139.70

PROVIDERS = {
    "hellocycling": {
        "info_url": "https://api-public.odpt.org/api/v4/gbfs/hellocycling/station_information.json",
        "status_url": "https://api-public.odpt.org/api/v4/gbfs/hellocycling/station_status.json",
    },
    "docomo-cycle": {
        "info_url": "https://api-public.odpt.org/api/v4/gbfs/docomo-cycle/station_information.json",
        "status_url": "https://api-public.odpt.org/api/v4/gbfs/docomo-cycle/station_status.json",
    },
}

DATA_DIR = Path("data")
JST = timezone(timedelta(hours=9))


def collect_provider(name, urls, now_jst, params):
    timestamp_str = now_jst.strftime("%Y-%m-%d %H:%M:%S")
    date_str = now_jst.strftime("%Y-%m-%d")

    info_res = requests.get(urls["info_url"], params=params, timeout=30)
    info_res.raise_for_status()
    info_data = info_res.json()

    status_res = requests.get(urls["status_url"], params=params, timeout=30)
    status_res.raise_for_status()
    status_data = status_res.json()

    target_stations = {}
    for st in info_data.get("data", {}).get("stations", []):
        lat = st.get("lat")
        lon = st.get("lon")
        if lat is None or lon is None:
            continue
        if LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX:
            target_stations[st["station_id"]] = {
                "station_id": st["station_id"],
                "name": st.get("name", ""),
                "lat": lat,
                "lon": lon,
                "capacity": st.get("capacity"),
            }

    stations_file = DATA_DIR / f"stations_{name}.json"
    with stations_file.open("w", encoding="utf-8") as f:
        json.dump(
            sorted(target_stations.values(), key=lambda x: str(x["station_id"])),
            f,
            ensure_ascii=False,
            indent=2,
        )

    csv_file = DATA_DIR / f"status_{name}_{date_str}.csv"
    file_exists = csv_file.exists()

    rows = []
    for st in status_data.get("data", {}).get("stations", []):
        sid = st.get("station_id")
        if sid in target_stations:
            rows.append(
                [
                    timestamp_str,
                    sid,
                    st.get("num_bikes_available", ""),
                    st.get("num_docks_available", ""),
                    st.get("is_renting", ""),
                    st.get("is_returning", ""),
                ]
            )

    with csv_file.open("a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(
                [
                    "timestamp_jst",
                    "station_id",
                    "num_bikes_available",
                    "num_docks_available",
                    "is_renting",
                    "is_returning",
                ]
            )
        writer.writerows(rows)

    print(f"[{timestamp_str}] {name}: {len(rows)} ポート記録完了")


def compress_past_csvs(today_str):
    for csv_path in DATA_DIR.glob("status_*.csv"):
        if today_str not in csv_path.name:
            gz_path = csv_path.with_suffix(".csv.gz")
            with csv_path.open("rb") as f_in, gzip.open(gz_path, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            csv_path.unlink()
            print(f"圧縮完了: {gz_path.name}")


def main():
    DATA_DIR.mkdir(exist_ok=True)
    now_jst = datetime.now(JST)
    today_str = now_jst.strftime("%Y-%m-%d")

    api_key = os.environ.get("ODPT_API_KEY")
    params = {"acl:consumerKey": api_key} if api_key else None

    errors = []
    for name, urls in PROVIDERS.items():
        try:
            collect_provider(name, urls, now_jst, params)
        except Exception as e:
            print(f"エラー ({name}): {e}")
            errors.append((name, e))

    compress_past_csvs(today_str)

    if len(errors) == len(PROVIDERS):
        raise RuntimeError("すべての事業者のデータ取得に失敗しました")


if __name__ == "__main__":
    main()