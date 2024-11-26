"""Open-Meteo current-weather collector.

Polls *current* conditions for the study location and stores them in the shared
SQLite db (agrisense.db, next to this script).

Point-in-time snapshots only: not a complete daily dataset, so unusable on
their own for FAO-56 ET0.

Run:  python weather.py      (stop with Ctrl+C)
"""

import math
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import requests


# Configuration:
FIELD_ID = "field_001"
LATITUDE = 29.97
LONGITUDE = 30.94
# Representative agricultural study location near 6th of October City, Egypt.

API_URL = "https://api.open-meteo.com/v1/forecast"
API_SOURCE = "open-meteo"
REQUEST_TIMEOUT = (5, 20)  # (connect, read) seconds

FETCH_INTERVAL_SECONDS = 30 * 60  # 30 minutes

DB_PATH = Path(__file__).resolve().parent / "agrisense.db"
# ingest.py writes to this file from another process; wait for its lock instead of failing immediately.
DB_BUSY_TIMEOUT_SECONDS = 30

# API variable -> (database column, unit string the API must report).
# Units are checked against the response's "current_units" so a wrong unit (e.g. wind in km/h) is rejected, never stored.
VARIABLES = {
    "temperature_2m": ("temperature_c", "\u00b0C"),
    "relative_humidity_2m": ("humidity_pct", "%"),
    "surface_pressure": ("surface_pressure_hpa", "hPa"),
    "wind_speed_10m": ("wind_speed_m_s", "m/s"),
    "shortwave_radiation": ("solar_radiation_w_m2", "W/m\u00b2"),
    "precipitation": ("precipitation_mm", "mm"),
}

REQUEST_PARAMS = {
    "latitude": LATITUDE,
    "longitude": LONGITUDE,
    "current": ",".join(VARIABLES),
    "timezone": "UTC",
    "wind_speed_unit": "ms",
}

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS weather_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    field_id TEXT NOT NULL,
    timestamp DATETIME NOT NULL,
    temperature_c REAL,
    humidity_pct REAL,
    surface_pressure_hpa REAL,
    wind_speed_m_s REAL,
    solar_radiation_w_m2 REAL,
    precipitation_mm REAL,
    api_source TEXT DEFAULT 'open-meteo',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(field_id, timestamp)
);
"""

INSERT_SQL = """
INSERT OR IGNORE INTO weather_observations (
    field_id, timestamp,
    temperature_c, humidity_pct, surface_pressure_hpa,
    wind_speed_m_s, solar_radiation_w_m2, precipitation_mm,
    api_source
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
"""


class WeatherError(Exception):
    # Fetch or validation failure.
    pass


# Database:
def init_db(db_path=DB_PATH):
    # Connect and ensure weather_observations exists.
    db_path = Path(db_path)
    if not db_path.exists():
        print(f"[DB] {db_path.name} not found - it will be created "
              f"(ingest.py creates sensor_readings separately).")
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


def save_observation(conn, observation):
    # Insert one observation. True if inserted, False if duplicate.
    with conn:  # one transaction per record
        cursor = conn.execute(
            INSERT_SQL,
            (
                observation["field_id"],
                observation["timestamp"],
                observation["temperature_c"],
                observation["humidity_pct"],
                observation["surface_pressure_hpa"],
                observation["wind_speed_m_s"],
                observation["solar_radiation_w_m2"],
                observation["precipitation_mm"],
                observation["api_source"],
            ),
        )
        # INSERT OR IGNORE: rowcount is 1 when inserted, 0 when ignored.
        return cursor.rowcount == 1


# Fetching:
def fetch_current_weather():
    # GET current weather; return decoded JSON.
    # Raises WeatherError on timeout, connection failure, HTTP error, an API-reported error, or a non-JSON body.
    try:
        response = requests.get(API_URL, params=REQUEST_PARAMS, timeout=REQUEST_TIMEOUT)
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


# Validation:
def _normalise_timestamp(value):
    # API time (UTC, '2026-11-14T23:45') -> '2026-11-14T23:45:00Z'.
    # Matches the format ingest.py stores. Only the format changes, not the time.
    if not isinstance(value, str):
        raise WeatherError(f"'current.time' is missing or not a string: {value!r}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise WeatherError(f"'current.time' is not valid ISO 8601: {value!r}")
    if parsed.tzinfo is not None and parsed.utcoffset().total_seconds() != 0:
        raise WeatherError(f"'current.time' is not UTC: {value!r}")
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_number(value):
    # Finite float, or None for null/non-numeric. Never a substitute value.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def validate_response(data):
    # Validate the response and build a db-ready record.
    # Raises WeatherError on bad structure, timestamp or units, or when every measurement is null. Single null measurements are stored as NULL, never filled in.
    if not isinstance(data, dict):
        raise WeatherError("response JSON is not an object")
    if data.get("error"):
        raise WeatherError(f"API error: {data.get('reason', 'unknown reason')}")

    current = data.get("current")
    units = data.get("current_units")
    if not isinstance(current, dict):
        raise WeatherError("response has no 'current' object")
    if not isinstance(units, dict):
        raise WeatherError("response has no 'current_units' object")

    record = {
        "field_id": FIELD_ID,
        "timestamp": _normalise_timestamp(current.get("time")),
        "api_source": API_SOURCE,
    }

    missing = []
    for api_name, (column, expected_unit) in VARIABLES.items():
        if api_name not in current:
            raise WeatherError(f"'current.{api_name}' is missing from the response")
        actual_unit = units.get(api_name)
        if actual_unit != expected_unit:
            raise WeatherError(
                f"unexpected unit for {api_name}: got {actual_unit!r}, expected {expected_unit!r}"
            )
        value = _clean_number(current[api_name])
        if value is None:
            missing.append(api_name)
        record[column] = value

    if len(missing) == len(VARIABLES):
        raise WeatherError("all measurements are null; nothing to store")

    record["_missing"] = missing  # reporting only; not stored
    return record


# One fetch-and-store attempt:
def collect_once(conn):
    # One fetch -> validate -> save cycle; prints the outcome.
    print(f"[FETCH] Requesting current weather for {FIELD_ID} "
          f"({LATITUDE}, {LONGITUDE}) ...")
    try:
        data = fetch_current_weather()
        record = validate_response(data)
    except WeatherError as exc:
        print(f"[FAILED] {exc}. No data stored this cycle.")
        return False
    except Exception as exc:  # a bad cycle must not kill the scheduler
        print(f"[FAILED] Unexpected error: {exc!r}. No data stored this cycle.")
        return False

    try:
        inserted = save_observation(conn, record)
    except sqlite3.Error as exc:
        print(f"[DB ERROR] Could not save observation: {exc}")
        return False

    summary = (
        f"{record['field_id']} @ {record['timestamp']} | "
        f"T={record['temperature_c']} C, RH={record['humidity_pct']} %, "
        f"P={record['surface_pressure_hpa']} hPa, Wind={record['wind_speed_m_s']} m/s, "
        f"SW={record['solar_radiation_w_m2']} W/m2, Precip={record['precipitation_mm']} mm"
    )
    if not inserted:
        print(f"[DUPLICATE] Observation already stored, ignored: {summary}")
    else:
        print(f"[INSERTED] {summary}")
    if record["_missing"]:
        print(f"[WARNING] Null values from API stored as NULL: {', '.join(record['_missing'])}")
    return inserted


# Main:
def main():
    try:
        conn = init_db()
    except sqlite3.Error as exc:
        print(f"[DB ERROR] Could not initialise database at {DB_PATH}: {exc}")
        return 1

    print(f"[DB] Using database: {DB_PATH}")
    print(f"[INFO] Fetching every {FETCH_INTERVAL_SECONDS // 60} minutes. Press Ctrl+C to stop.")
    try:
        while True:  # first fetch happens immediately, then wait
            collect_once(conn)
            print(f"[WAIT] Next fetch in {FETCH_INTERVAL_SECONDS // 60} minutes.")
            time.sleep(FETCH_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print("\n[INFO] Stopping (Ctrl+C) ...")
    finally:
        conn.close()
        print("[DB] Connection closed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
