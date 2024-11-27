"""Open-Meteo daily weather collector.

Fetches the latest COMPLETED daily records for the study location and stores
them in the shared SQLite db (agrisense.db, next to this script).

Provenance: all values are Open-Meteo model output for a grid cell near the
site, not station or field measurements. `openmeteo_et0_mm` is Open-Meteo's own
ET0, stored as-is for comparison only; no agronomic calculation happens here.

TODO: bias-check these model values against a local weather station before
trusting the ET0 / water-balance outputs.

Day definition: aggregates are over Africa/Cairo local days (timezone=Africa/Cairo
in the request) and "today" is the Cairo date. Rows carry api_source
'open-meteo;tz=Africa/Cairo'.
Legacy rows (api_source exactly 'open-meteo') were aggregated over UTC days.
They are left in place but the assessment ignores them.

Run:  python weather_daily.py      (stop with Ctrl+C)
"""

import argparse
import math
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

from agrisense.config import DAILY_WEATHER_API_SOURCE, DEFAULT_CONFIG, TIMEZONE, local_today
from agrisense.validation import AgriSenseError

# Defaults come from agrisense/config.py: one definition for all scripts.
FIELD_ID = DEFAULT_CONFIG.study.field_id
LATITUDE = DEFAULT_CONFIG.study.latitude
LONGITUDE = DEFAULT_CONFIG.study.longitude
# Representative agricultural study location near 6th of October City, Egypt.

API_URL = "https://api.open-meteo.com/v1/forecast"
API_SOURCE = DAILY_WEATHER_API_SOURCE  # 'open-meteo;tz=Africa/Cairo'
REQUEST_TIMEOUT = (5, 30)  # (connect, read) seconds
PAST_DAYS = 2

REPEAT_INTERVAL_SECONDS = 24 * 60 * 60  # 24 hours

DB_PATH = Path(__file__).resolve().parent / "agrisense.db"
# ingest.py and weather.py write to this file from other processes; wait for
# their lock instead of failing immediately.
DB_BUSY_TIMEOUT_SECONDS = 30

# API variable -> (database column, unit the API must report).
# Units are checked against "daily_units"; any other unit is rejected, not
# converted or mixed.
VARIABLES = {
    "temperature_2m_max": ("temperature_max_c", "\u00b0C"),
    "temperature_2m_min": ("temperature_min_c", "\u00b0C"),
    "relative_humidity_2m_max": ("humidity_max_pct", "%"),
    "relative_humidity_2m_min": ("humidity_min_pct", "%"),
    "relative_humidity_2m_mean": ("humidity_mean_pct", "%"),
    "surface_pressure_mean": ("surface_pressure_mean_hpa", "hPa"),
    "wind_speed_10m_mean": ("wind_speed_10m_mean_m_s", "m/s"),
    "shortwave_radiation_sum": ("solar_radiation_sum_mj_m2", "MJ/m\u00b2"),  # energy per day, NOT W/m2
    "precipitation_sum": ("precipitation_sum_mm", "mm"),
    "et0_fao_evapotranspiration": ("openmeteo_et0_mm", "mm"),
}

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS daily_weather_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    field_id TEXT NOT NULL,
    date TEXT NOT NULL,
    temperature_max_c REAL,
    temperature_min_c REAL,
    humidity_max_pct REAL,
    humidity_min_pct REAL,
    humidity_mean_pct REAL,
    surface_pressure_mean_hpa REAL,
    wind_speed_10m_mean_m_s REAL,
    solar_radiation_sum_mj_m2 REAL,
    precipitation_sum_mm REAL,
    openmeteo_et0_mm REAL,
    api_source TEXT NOT NULL DEFAULT 'open-meteo',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(field_id, date)
);
"""

INSERT_SQL = """
INSERT OR IGNORE INTO daily_weather_observations (
    field_id, date,
    temperature_max_c, temperature_min_c,
    humidity_max_pct, humidity_min_pct, humidity_mean_pct,
    surface_pressure_mean_hpa, wind_speed_10m_mean_m_s,
    solar_radiation_sum_mj_m2, precipitation_sum_mm, openmeteo_et0_mm,
    api_source
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
"""

# Column order used by INSERT_SQL after (field_id, date) and before api_source.
VALUE_COLUMNS = [column for column, _unit in VARIABLES.values()]


class WeatherError(Exception):
    pass


def init_db(db_path=DB_PATH):
    db_path = Path(db_path)
    if not db_path.exists():
        print(f"[DB] {db_path.name} not found - it will be created.")
    conn = sqlite3.connect(str(db_path), timeout=DB_BUSY_TIMEOUT_SECONDS)
    try:
        # Redundant with connect(timeout); kept so the intent is visible.
        conn.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_SECONDS * 1000}")
        with conn:  # commits on success, rolls back on error
            conn.execute(CREATE_TABLE_SQL)
    except sqlite3.Error:
        conn.close()
        raise
    return conn


def save_daily_record(conn, record):
    params = (
        [record["field_id"], record["date"]]
        + [record[column] for column in VALUE_COLUMNS]
        + [record["api_source"]]
    )
    with conn:  # one transaction per record
        cursor = conn.execute(INSERT_SQL, params)
        # INSERT OR IGNORE: rowcount is 1 when inserted, 0 when ignored.
        return cursor.rowcount == 1


def fetch_daily_weather(latitude, longitude):
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "daily": ",".join(VARIABLES),
        "timezone": TIMEZONE,  # daily aggregates over Africa/Cairo local days
        "wind_speed_unit": "ms",
        "temperature_unit": "celsius",
        "precipitation_unit": "mm",
        "past_days": PAST_DAYS,
        # Today only (incomplete, filtered out later); avoids 6 forecast days.
        "forecast_days": 1,
    }
    try:
        response = requests.get(API_URL, params=params, timeout=REQUEST_TIMEOUT)
    except requests.exceptions.Timeout:
        raise WeatherError("request timed out")
    except requests.exceptions.ConnectionError as exc:
        raise WeatherError(f"connection failed: {exc}")
    except requests.exceptions.RequestException as exc:
        raise WeatherError(f"request failed: {exc}")

    if not response.ok:
        # Open-Meteo explains errors as {"error": true, "reason": "..."}.
        detail = ""
        try:
            detail = response.json().get("reason", "")
        except (ValueError, AttributeError):
            pass
        raise WeatherError(
            f"HTTP {response.status_code} {response.reason}" + (f" - {detail}" if detail else "")
        )

    try:
        return response.json()
    except ValueError:
        raise WeatherError("response body is not valid JSON")


def _parse_date(value):
    if isinstance(value, str) and len(value) == 10:
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            pass
    raise WeatherError(f"invalid date in 'daily.time': {value!r}")


def validate_response(data):
    if not isinstance(data, dict):
        raise WeatherError("response JSON is not an object")
    if data.get("error"):
        raise WeatherError(f"API error: {data.get('reason', 'unknown reason')}")

    daily = data.get("daily")
    units = data.get("daily_units")
    if not isinstance(daily, dict):
        raise WeatherError("response has no 'daily' object")
    if not isinstance(units, dict):
        raise WeatherError("response has no 'daily_units' object")

    times = daily.get("time")
    if not isinstance(times, list) or not times:
        raise WeatherError("'daily.time' is missing, not a list, or empty")
    parsed_dates = [_parse_date(t) for t in times]
    if len(set(parsed_dates)) != len(parsed_dates):
        raise WeatherError("'daily.time' contains duplicate dates")

    for api_name, (_column, expected_unit) in VARIABLES.items():
        if api_name not in daily:
            raise WeatherError(f"'daily.{api_name}' is missing from the response")
        if api_name not in units:
            raise WeatherError(f"unit for '{api_name}' is missing from 'daily_units'")
        if units[api_name] != expected_unit:
            raise WeatherError(
                f"unexpected unit for {api_name}: got {units[api_name]!r}, "
                f"expected {expected_unit!r}"
            )
        values = daily[api_name]
        if not isinstance(values, list):
            raise WeatherError(f"'daily.{api_name}' is not a list")
        if len(values) != len(times):
            raise WeatherError(
                f"'daily.{api_name}' has {len(values)} values for {len(times)} dates"
            )
    return daily


def _clean_value(api_name, date_str, value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WeatherError(f"non-numeric value for {api_name} on {date_str}: {value!r}")
    value = float(value)
    return value if math.isfinite(value) else None


def process_daily_record(field_id, daily, index):
    date_str = daily["time"][index]
    record = {"field_id": field_id, "date": date_str, "api_source": API_SOURCE}
    non_null = 0
    for api_name, (column, _unit) in VARIABLES.items():
        value = _clean_value(api_name, date_str, daily[api_name][index])
        record[column] = value
        if value is not None:
            non_null += 1
    return record if non_null else None


def build_records(field_id, daily, today):
    records, ignored_dates, empty_dates = [], [], []
    for index, date_str in enumerate(daily["time"]):
        if _parse_date(date_str) >= today:
            ignored_dates.append(date_str)
            continue
        record = process_daily_record(field_id, daily, index)
        if record is None:
            empty_dates.append(date_str)
        else:
            records.append(record)
    return records, ignored_dates, empty_dates


def collect_once(conn, field_id, latitude, longitude, today=None):
    print(f"[FETCH] Requesting daily weather for {field_id} ({latitude}, {longitude}) ...")
    try:
        if today is None:
            # Clock only decides which days are complete (Cairo date). Stored
            # dates always come from the API.
            today = local_today(TIMEZONE)
        data = fetch_daily_weather(latitude, longitude)
        daily = validate_response(data)
        records, ignored, empty = build_records(field_id, daily, today)
    except (WeatherError, AgriSenseError) as exc:
        print(f"[FAILED] {exc}. No data stored this cycle.")
        return None
    except Exception as exc:  # a bad cycle must not kill the scheduler
        print(f"[FAILED] Unexpected error: {exc!r}. No data stored this cycle.")
        return None

    if ignored:
        print(f"[INFO] Ignoring incomplete/future day(s) ({TIMEZONE}): {', '.join(ignored)}")
    for date_str in empty:
        print(f"[SKIPPED] {date_str}: all values are null; nothing stored.")

    inserted = duplicates = errors = 0
    for record in records:
        summary = (
            f"{record['field_id']} {record['date']} | "
            f"Tmax={record['temperature_max_c']} C, Tmin={record['temperature_min_c']} C, "
            f"RHmean={record['humidity_mean_pct']} %, "
            f"P={record['surface_pressure_mean_hpa']} hPa, "
            f"Wind={record['wind_speed_10m_mean_m_s']} m/s, "
            f"Rs={record['solar_radiation_sum_mj_m2']} MJ/m2/day, "
            f"Rain={record['precipitation_sum_mm']} mm, "
            f"OM-ET0={record['openmeteo_et0_mm']} mm"
        )
        try:
            was_inserted = save_daily_record(conn, record)
        except sqlite3.Error as exc:
            errors += 1
            print(f"[DB ERROR] Could not save {record['date']}: {exc}")
            continue
        if was_inserted:
            inserted += 1
            print(f"[INSERTED] {summary}")
        else:
            duplicates += 1
            print(f"[DUPLICATE] Already stored, ignored: {record['date']}")
        nulls = [c for c in VALUE_COLUMNS if record[c] is None]
        if was_inserted and nulls:
            print(f"[WARNING] {record['date']}: null values stored as NULL: {', '.join(nulls)}")

    print(f"[DONE] inserted={inserted}, duplicates={duplicates}, "
          f"skipped_empty={len(empty)}, db_errors={errors}")
    return {"inserted": inserted, "duplicates": duplicates, "skipped_empty": len(empty),
            "ignored": len(ignored), "errors": errors}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Collect daily Open-Meteo weather into agrisense.db")
    parser.add_argument("--field-id", default=FIELD_ID, help=f"default: {FIELD_ID}")
    parser.add_argument("--latitude", type=float, default=LATITUDE, help=f"default: {LATITUDE}")
    parser.add_argument("--longitude", type=float, default=LONGITUDE, help=f"default: {LONGITUDE}")
    args = parser.parse_args(argv)
    if not args.field_id.strip():
        parser.error("--field-id must not be empty")
    if not -90 <= args.latitude <= 90:
        parser.error("--latitude must be between -90 and 90")
    if not -180 <= args.longitude <= 180:
        parser.error("--longitude must be between -180 and 180")
    return args


def main(argv=None):
    args = parse_args(argv)
    try:
        conn = init_db()
    except sqlite3.Error as exc:
        print(f"[DB ERROR] Could not initialise database at {DB_PATH}: {exc}")
        return 1

    print(f"[DB] Using database: {DB_PATH}")
    print("[INFO] Open-Meteo daily values are model-derived, not station measurements.")
    print(f"[INFO] Repeating every {REPEAT_INTERVAL_SECONDS // 3600} hours. Press Ctrl+C to stop.")
    try:
        while True:  # first fetch happens immediately, then wait
            collect_once(conn, args.field_id.strip(), args.latitude, args.longitude)
            print(f"[WAIT] Next fetch in {REPEAT_INTERVAL_SECONDS // 3600} hours.")
            time.sleep(REPEAT_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print("\n[INFO] Stopping (Ctrl+C) ...")
    finally:
        conn.close()
        print("[DB] Connection closed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
