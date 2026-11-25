import csv
import gzip
import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from google.transit import gtfs_realtime_pb2

GTFS_RT_URL = os.environ.get("YOKOHAMA_GTFS_RT_URL", "")
ODPT_API_KEY = os.environ.get("ODPT_API_KEY", "")
MIN_DELAY_SEC = 180  # 3分以上の遅延を記録

DATA_DIR = Path("data")
JST = timezone(timedelta(hours=9))


def compress_past_bus_csvs(today_str):
    for csv_path in DATA_DIR.glob("bus_delay_*.csv"):
        if today_str not in csv_path.name:
            gz_path = csv_path.with_suffix(".csv.gz")
            with csv_path.open("rb") as f_in, gzip.open(gz_path, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            csv_path.unlink()
            print(f"圧縮完了: {gz_path.name}")


def main():
    if not GTFS_RT_URL:
        print("YOKOHAMA_GTFS_RT_URL が未設定のためスキップします")
        return

    DATA_DIR.mkdir(exist_ok=True)
    now_jst = datetime.now(JST)
    timestamp_str = now_jst.strftime("%Y-%m-%d %H:%M:%S")
    date_str = now_jst.strftime("%Y-%m-%d")

    params = {"acl:consumerKey": ODPT_API_KEY} if ODPT_API_KEY else None
    res = requests.get(GTFS_RT_URL, params=params, timeout=30)
    res.raise_for_status()

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(res.content)
    feed_ts = feed.header.timestamp or int(time.time())

    rows = []
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        tu = entity.trip_update
        tid = tu.trip.trip_id

        for stu in tu.stop_time_update:
            delay = max(stu.arrival.delay, stu.departure.delay)
            if delay < MIN_DELAY_SEC:
                continue

            arr_ts = stu.arrival.time or stu.departure.time
            if arr_ts and not (feed_ts - 300 <= arr_ts <= feed_ts + 1800):
                continue

            rows.append([timestamp_str, tid, stu.stop_id, delay])

    csv_file = DATA_DIR / f"bus_delay_yokohama_{date_str}.csv"
    file_exists = csv_file.exists()

    with csv_file.open("a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["timestamp_jst", "trip_id", "stop_id", "delay_sec"])
        writer.writerows(rows)

    compress_past_bus_csvs(date_str)
    print(f"[{timestamp_str}] 横浜市営バス遅延: {len(rows)} 件記録完了")


if __name__ == "__main__":
    main()