"""Streamlit front end over the backend's SQLite DB. Integration only: no
physics or collection logic here.

    streamlit run dashboard/app.py     (from project root)

Invariants:
* DB access is read-only (`mode=ro`), one short-lived connection per load.
  The dashboard never emits write SQL. The assessment subprocess is the only
  writer of `daily_assessments`.
* Missing/stale/invalid data is reported as such, never filled in.
* Soil moisture is simulated and uncalibrated. Display only; never a
  calculation input.
"""

import contextlib
import math
import os
import re
import sqlite3
import subprocess
import sys
import threading
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = PROJECT_ROOT / "backend"
GIS_DIR = PROJECT_ROOT / "gis"
# backend/ and gis/ are not packages on any install path; import them in place.
for _path in (BACKEND_DIR, GIS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import altair as alt  # noqa: E402  (installed with Streamlit)
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
from streamlit_folium import st_folium  # noqa: E402

import map_generator as mg  # noqa: E402  (gis/map_generator.py)
from agrisense import irrigation  # noqa: E402
from agrisense.config import (  # noqa: E402
    ASSUMPTION_NOTES, DAILY_WEATHER_API_SOURCE, DEFAULT_CONFIG, TIMEZONE, local_today)
from agrisense.validation import AgriSenseError  # noqa: E402

DB_PATH = BACKEND_DIR / "agrisense.db"
ASSESSMENT_SCRIPT = BACKEND_DIR / "agrisense" / "assessment.py"
ASSESSMENT_MODULE = "agrisense.assessment"             # relative imports: must run via -m with cwd=backend/
ASSESSMENT_TIMEOUT_SECONDS = 120
OUTPUT_TAIL_CHARS = 20000

DAILY_HISTORY_DAYS = 14                                # completed Cairo days in the weather lane
SENSOR_LIMIT = 50
# TODO: 15 min assumes the ~5 s firmware publish cadence (3x margin is huge on purpose).
# Revisit for duty-cycled / LoRa nodes, where a long sleep interval looks identical to an outage.
SENSOR_STALE_AFTER = timedelta(minutes=15)

# Section/lookup states. The assessment lookup shares the map_generator ones.
OK = "OK"
EMPTY = "EMPTY"
NO_VALID = "NO_VALID"
NO_COMPLETED = "NO_COMPLETED"
TABLE_MISSING = "TABLE_MISSING"
DB_MISSING = "DB_MISSING"
SCHEMA_ERROR = "SCHEMA_ERROR"
DB_ERROR = "DB_ERROR"

SENSOR_COLUMNS = ("id", "device_id", "field_id", "timestamp", "temperature_c", "humidity_pct",
                  "soil_moisture_pct", "is_simulation", "sensor_status", "is_valid")
WEATHER_COLUMNS = ("field_id", "date", "temperature_max_c", "temperature_min_c", "humidity_max_pct",
                   "humidity_min_pct", "wind_speed_10m_mean_m_s", "solar_radiation_sum_mj_m2",
                   "precipitation_sum_mm", "openmeteo_et0_mm", "api_source", "created_at")
# Beyond mg.COLUMNS (map popup): what the evidence tab needs.
ASSESSMENT_COLUMNS = mg.COLUMNS + ("ks", "reasoning", "simulation_start_date", "openmeteo_et0_mm", "config_hash")

KNOWN_STATUSES = ("OK", "PROVISIONAL", "INSUFFICIENT_DATA")
KNOWN_RECOMMENDATIONS = (irrigation.IRRIGATE, irrigation.MONITOR, irrigation.NO_IRRIGATION,
                         irrigation.INSUFFICIENT_DATA)


# --- Formatting and freshness (pure) ---
# Escape set for literal rendering of DB text in st.markdown.
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~$:&])")


def display_path(path):
    try:
        return Path(path).resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


def md_safe(value):
    text = "" if value is None else str(value)
    return _MARKDOWN_SPECIAL.sub(r"\\\1", " ".join(text.split()))


def num(value, digits=2, unit=""):
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "invalid"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "invalid"
    if not math.isfinite(number):
        return "invalid"
    return f"{number:.{digits}f}{unit}"


def parse_utc(text):
    if not isinstance(text, str):
        return None
    try:
        parsed = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:       # naive: assuming UTC would silently skew freshness
        return None
    return parsed.astimezone(timezone.utc)


def humanize(delta):
    seconds = int(max(delta.total_seconds(), 0))
    if seconds < 90:
        return f"{seconds} s"
    minutes = seconds // 60
    if minutes < 120:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {minutes} min"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h"


def format_utc(dt):
    cairo = dt.astimezone(ZoneInfo(TIMEZONE))
    return f"{dt:%Y-%m-%d %H:%M:%S} UTC ({cairo:%H:%M} {TIMEZONE})"


@dataclass(frozen=True)
class Freshness:
    level: str      # FRESH | STALE | MISSING | UNKNOWN
    text: str

    @property
    def icon(self):
        return {"FRESH": "🟢", "STALE": "🟠", "MISSING": "🔴"}.get(self.level, "⚪")

    def __str__(self):
        return f"{self.icon} {self.text}"


def sensor_freshness(timestamp_text, now_utc, stale_after=SENSOR_STALE_AFTER):
    if timestamp_text is None:
        return Freshness("MISSING", "No sensor message stored")
    stamp = parse_utc(timestamp_text)
    if stamp is None:
        return Freshness("UNKNOWN", "Timestamp could not be parsed")
    age = now_utc - stamp
    if age < -timedelta(minutes=1):
        return Freshness("UNKNOWN", "Timestamp is in the future (device clock skew?)")
    limit = f"threshold {int(stale_after.total_seconds() // 60)} min"
    if age <= stale_after:
        return Freshness("FRESH", f"Fresh ({humanize(age)} old; {limit})")
    return Freshness("STALE", f"Stale ({humanize(age)} old; {limit})")


def daily_freshness(date_text, expected_day, noun):
    if date_text is None:
        return Freshness("MISSING", f"No {noun}")
    try:
        day = date.fromisoformat(str(date_text))
    except ValueError:
        return Freshness("UNKNOWN", f"{noun.capitalize()} date could not be parsed")
    if day == expected_day:
        return Freshness("FRESH", f"Up to date (latest completed {TIMEZONE} day)")
    if day > expected_day:
        return Freshness("UNKNOWN", f"Dated after the latest completed {TIMEZONE} day ({expected_day})")
    behind = (expected_day - day).days
    return Freshness("STALE", f"{behind} day(s) behind the latest completed {TIMEZONE} day ({expected_day})")


def assessment_notice(lookup, expected_day):
    if lookup.state != mg.FOUND:
        return None
    fresh = daily_freshness(lookup.assessment.get("assessment_date"), expected_day, "assessment")
    if fresh.level == "FRESH":
        return None
    return f"{fresh.text}. It may not describe current conditions."


# --- Read-only data loading: one connection, closed after the queries ---
@dataclass
class SensorData:
    state: str
    message: str = ""
    rows: list = field(default_factory=list)       # valid readings only, newest first, <= SENSOR_LIMIT
    total: int = 0
    valid: int = 0
    invalid: int = 0
    unknown_validity: int = 0
    latest_message_timestamp: str = None           # newest message regardless of is_valid


@dataclass
class WeatherData:
    state: str
    message: str = ""
    latest_date: str = None                        # newest completed day with the Cairo-day api_source
    latest_row: dict = None
    completed_rows: int = 0
    legacy_rows: int = 0                           # other api_source (UTC-day aggregates); the engine ignores them
    incomplete_rows: int = 0                       # Cairo-tagged, day not over yet
    history: list = field(default_factory=list)    # <= DAILY_HISTORY_DAYS completed days, oldest first


@dataclass(frozen=True)
class Coverage:
    first_day: date                                 # simulation start
    last_day: date                                  # assessment date
    expected: int
    present: int
    missing: tuple = ()                             # ISO dates with no Cairo-tagged row


@dataclass
class Snapshot:
    db_path: Path
    loaded_at: datetime
    today_local: date
    expected_day: date                              # latest completed local day
    db_exists: bool
    sensors: SensorData
    weather: WeatherData
    lookup: mg.AssessmentLookup
    coverage: Coverage = None                       # None: no assessment, or span unusable


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}   # f-string: callers pass literals only, never external input


def load_sensors(conn, field_id, limit=SENSOR_LIMIT):
    columns = _columns(conn, "sensor_readings")
    if not columns:
        return SensorData(TABLE_MISSING, "Table sensor_readings does not exist. Run backend/ingest.py first.")
    missing = [c for c in SENSOR_COLUMNS if c not in columns]
    if missing:
        return SensorData(SCHEMA_ERROR, f"sensor_readings is missing column(s): {', '.join(missing)}")
    total, valid, invalid, unknown = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(is_valid = 1), 0), COALESCE(SUM(is_valid = 0), 0), "
        "COALESCE(SUM(is_valid IS NULL), 0) FROM sensor_readings WHERE field_id = ?", (field_id,)).fetchone()
    data = SensorData(OK, total=total, valid=valid, invalid=invalid, unknown_validity=unknown)
    if total == 0:
        data.state, data.message = EMPTY, f"sensor_readings has no rows for field '{field_id}'."
        return data
    latest = conn.execute(
        "SELECT timestamp FROM sensor_readings WHERE field_id = ? ORDER BY timestamp DESC, id DESC LIMIT 1",
        (field_id,)).fetchone()
    data.latest_message_timestamp = latest[0] if latest else None
    cursor = conn.execute(
        "SELECT id, device_id, timestamp, temperature_c, humidity_pct, soil_moisture_pct, is_simulation, "
        "sensor_status, is_valid FROM sensor_readings WHERE field_id = ? AND is_valid = 1 "
        "ORDER BY timestamp DESC, id DESC LIMIT ?", (field_id, limit))
    names = [c[0] for c in cursor.description]
    data.rows = [dict(zip(names, row)) for row in cursor.fetchall()]
    if not data.rows:
        data.state = NO_VALID
        data.message = f"{total} stored message(s), but none passed validation (is_valid = 1)."
    return data


def load_weather(conn, field_id, today_local):
    columns = _columns(conn, "daily_weather_observations")
    if not columns:
        return WeatherData(TABLE_MISSING, "Table daily_weather_observations does not exist. "
                                          "Run backend/weather_daily.py first.")
    missing = [c for c in WEATHER_COLUMNS if c not in columns]
    if missing:
        return WeatherData(SCHEMA_ERROR, f"daily_weather_observations is missing column(s): {', '.join(missing)}")
    today = today_local.isoformat()
    total = conn.execute("SELECT COUNT(*) FROM daily_weather_observations WHERE field_id = ?",
                         (field_id,)).fetchone()[0]
    if total == 0:
        return WeatherData(EMPTY, f"daily_weather_observations has no rows for field '{field_id}'.")
    legacy = conn.execute(
        "SELECT COUNT(*) FROM daily_weather_observations WHERE field_id = ? AND api_source != ?",
        (field_id, DAILY_WEATHER_API_SOURCE)).fetchone()[0]
    completed, incomplete = conn.execute(
        "SELECT COALESCE(SUM(date < ?), 0), COALESCE(SUM(date >= ?), 0) FROM daily_weather_observations "
        "WHERE field_id = ? AND api_source = ?", (today, today, field_id, DAILY_WEATHER_API_SOURCE)).fetchone()
    data = WeatherData(OK, completed_rows=completed, legacy_rows=legacy, incomplete_rows=incomplete)
    cursor = conn.execute(
        f"SELECT {', '.join(WEATHER_COLUMNS)} FROM daily_weather_observations "
        "WHERE field_id = ? AND api_source = ? AND date < ? ORDER BY date DESC, id DESC LIMIT 1",
        (field_id, DAILY_WEATHER_API_SOURCE, today))
    row = cursor.fetchone()
    if row is None:
        data.state = NO_COMPLETED
        data.message = (f"No completed {TIMEZONE}-day record from the current weather collector "
                        f"(api_source '{DAILY_WEATHER_API_SOURCE}').")
    else:
        data.latest_row = dict(zip(WEATHER_COLUMNS, row))
        data.latest_date = data.latest_row["date"]
        cursor = conn.execute(
            f"SELECT {', '.join(WEATHER_COLUMNS)} FROM daily_weather_observations "
            "WHERE field_id = ? AND api_source = ? AND date < ? ORDER BY date DESC, id DESC LIMIT ?",
            (field_id, DAILY_WEATHER_API_SOURCE, today, DAILY_HISTORY_DAYS))
        data.history = [dict(zip(WEATHER_COLUMNS, r)) for r in reversed(cursor.fetchall())]
    return data


def load_coverage(conn, field_id, assessment):
    try:
        first = date.fromisoformat(str(assessment["simulation_start_date"]))
        last = date.fromisoformat(str(assessment["assessment_date"]))
    except ValueError:
        return None
    if last < first or not _columns(conn, "daily_weather_observations"):
        return None
    expected = [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]
    have = {r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM daily_weather_observations "
        "WHERE field_id = ? AND api_source = ? AND date BETWEEN ? AND ?",
        (field_id, DAILY_WEATHER_API_SOURCE, first.isoformat(), last.isoformat()))}
    missing = tuple(d for d in expected if d not in have)
    return Coverage(first, last, len(expected), len(expected) - len(missing), missing)


def sensor_cadence(rows):
    stamps = sorted(s for s in (parse_utc(r["timestamp"]) for r in rows) if s is not None)
    if len(stamps) < 2:
        return {"count": len(stamps), "span_s": None, "median_gap_s": None}
    gaps = sorted((b - a).total_seconds() for a, b in zip(stamps, stamps[1:]))
    middle = len(gaps) // 2
    median = gaps[middle] if len(gaps) % 2 else (gaps[middle - 1] + gaps[middle]) / 2
    return {"count": len(stamps), "span_s": (stamps[-1] - stamps[0]).total_seconds(), "median_gap_s": median}


def load_snapshot(db_path=DB_PATH, now_utc=None, config=DEFAULT_CONFIG):
    now_utc = now_utc or datetime.now(timezone.utc)
    today_local = local_today(TIMEZONE, now=now_utc)
    expected_day = today_local - timedelta(days=1)
    field_id, crop_id = config.study.field_id, config.crop.crop_id
    db_path = Path(db_path)

    if not db_path.is_file():
        message = f"Database file not found: {display_path(db_path)}"
        return Snapshot(db_path, now_utc, today_local, expected_day, False,
                        SensorData(DB_MISSING, message), WeatherData(DB_MISSING, message),
                        mg.AssessmentLookup(mg.DB_MISSING, message + "."))

    label = display_path(db_path)
    with contextlib.closing(mg.open_readonly(db_path)) as conn:
        def guarded(loader, make_error):
            try:
                return loader()
            except sqlite3.Error as exc:               # corrupt, locked, unreadable
                return make_error(f"Database error: {exc}")

        sensors = guarded(lambda: load_sensors(conn, field_id), lambda m: SensorData(DB_ERROR, m))
        weather = guarded(lambda: load_weather(conn, field_id, today_local), lambda m: WeatherData(DB_ERROR, m))
        try:
            lookup = mg.find_latest_assessment_in(conn, field_id, crop_id, label, columns=ASSESSMENT_COLUMNS)
        except mg.GisError as exc:                      # unexpected daily_assessments schema
            lookup = mg.AssessmentLookup(SCHEMA_ERROR, str(exc))
        except sqlite3.Error as exc:
            lookup = mg.AssessmentLookup(DB_ERROR, f"Database error: {exc}")
        coverage = None
        if lookup.state == mg.FOUND:
            try:
                coverage = load_coverage(conn, field_id, lookup.assessment)
            except sqlite3.Error:
                coverage = None                         # rendered as "not available"
    return Snapshot(db_path, now_utc, today_local, expected_day, True, sensors, weather, lookup, coverage)


def assessment_problems(a):
    problems = []
    status, recommendation = a.get("status"), a.get("recommendation")
    if status not in KNOWN_STATUSES:
        problems.append(f"Unrecognised status '{status}'.")
    if recommendation not in KNOWN_RECOMMENDATIONS:
        problems.append(f"Unrecognised recommendation '{recommendation}'.")
    if status in ("OK", "PROVISIONAL"):
        for key, label in (("et0_mm", "ET0"), ("kc", "Kc"), ("etc_mm", "ETc"), ("depletion_mm", "depletion"),
                           ("taw_mm", "TAW"), ("raw_mm", "RAW")):
            shown = num(a.get(key))
            if shown == "n/a":
                problems.append(f"{label} is missing from the stored record.")
            elif shown == "invalid":
                problems.append(f"{label} is not a valid number in the stored record.")
    if a.get("irrigation_depth_mm") is not None and num(a["irrigation_depth_mm"]) == "invalid":
        problems.append("Net irrigation depth is not a valid number in the stored record.")
    warnings = mg.parse_warnings(a.get("warnings"))
    if status == "PROVISIONAL" and not warnings:
        problems.append("Status is PROVISIONAL but no warnings are stored.")
    if status == "OK" and warnings:
        problems.append("Status is OK although warnings are stored.")
    return problems


# --- Assessment execution: subprocess boundary ---
@dataclass(frozen=True)
class RunResult:
    outcome: str
    returncode: int = None
    stdout: str = ""
    stderr: str = ""
    command: tuple = ()
    started_at: datetime = None
    duration_s: float = 0.0
    detail: str = ""


OUTCOME_TEXT = {
    "SAVED": ("success", "Assessment computed and saved to daily_assessments."),
    "DUPLICATE": ("info", "Assessment computed, but an identical record (same date and settings) already "
                          "exists, so nothing new was saved."),
    "INSUFFICIENT_DATA": ("warning", "INSUFFICIENT DATA: the assessment could not be computed and nothing was "
                                     "saved. The reason is in the output below (typically incomplete "
                                     f"{TIMEZONE} daily weather since the simulation start, or the day is "
                                     "outside the modelled window). No values were invented."),
    "COMPLETED_UNRECOGNISED": ("warning", "The assessment exited normally but its output was not in the expected "
                                          "format. Check the output below before relying on the result."),
    "DB_ERROR": ("error", "Database error (exit code 1): the assessment could not open or save to the database."),
    "ERROR": ("error", "The assessment reported an error (exit code 2) or rejected its arguments."),
    "FAILED": ("error", "The assessment process failed with an unexpected exit code."),
    "TIMEOUT": ("error", f"The assessment did not finish within {ASSESSMENT_TIMEOUT_SECONDS} s and was stopped."),
    "LAUNCH_ERROR": ("error", "The assessment process could not be started."),
    "INTERNAL_ERROR": ("error", "Unexpected dashboard error while running the assessment."),
}


def classify_run(returncode, stdout):
    if returncode == 0:
        if "[INSERTED]" in stdout:
            return "SAVED"
        if "[DUPLICATE]" in stdout:
            return "DUPLICATE"
        if "[NOT SAVED] Insufficient data" in stdout:
            return "INSUFFICIENT_DATA"
        return "COMPLETED_UNRECOGNISED"
    if returncode == 1:
        return "DB_ERROR"
    if returncode == 2:
        return "ERROR"
    return "FAILED"


def backend_reason(stdout):
    for line in (stdout or "").splitlines():
        if line.startswith("Reasoning"):
            return line.split(":", 1)[-1].strip() or None
    return None


def _tail(text):
    text = text or ""
    return text if len(text) <= OUTPUT_TAIL_CHARS else "...(truncated)...\n" + text[-OUTPUT_TAIL_CHARS:]


def _as_text(value):
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def run_assessment(db_path, extra_args=(), timeout=ASSESSMENT_TIMEOUT_SECONDS, command=None, cwd=BACKEND_DIR):
    started = datetime.now(timezone.utc)
    if command is None:
        if not ASSESSMENT_SCRIPT.is_file():
            return RunResult("LAUNCH_ERROR", started_at=started,
                             detail=f"Assessment script not found: {ASSESSMENT_SCRIPT}")
        # Package-relative imports rule out running by file path; use -m with cwd=backend/.
        command = [sys.executable, "-m", ASSESSMENT_MODULE, "--db", str(Path(db_path).resolve()), *extra_args]
    command = tuple(str(part) for part in command)
    env = {**os.environ, "PYTHONUTF8": "1"}
    clock = datetime.now(timezone.utc)
    try:
        done = subprocess.run(command, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, shell=False, env=env)
    except subprocess.TimeoutExpired as exc:
        return RunResult("TIMEOUT", None, _tail(_as_text(exc.stdout)), _tail(_as_text(exc.stderr)), command,
                         started, (datetime.now(timezone.utc) - clock).total_seconds(),
                         f"Stopped after {timeout} s.")
    except OSError as exc:
        return RunResult("LAUNCH_ERROR", None, "", "", command, started, 0.0, str(exc))
    duration = (datetime.now(timezone.utc) - clock).total_seconds()
    return RunResult(classify_run(done.returncode, done.stdout), done.returncode, _tail(done.stdout),
                     _tail(done.stderr), command, started, duration)


@dataclass(frozen=True)
class RunState:
    running: bool
    seq: int                       # bumped when a run finishes
    started_at: datetime
    result: RunResult


class AssessmentRunner:
    """Serialises assessment runs across browser sessions.

    FIXME: the lock is per-process only. Multiple Streamlit workers, or a CLI
    run against the same DB, can still overlap. SQLite's write lock is the real
    backstop; the loser gets a DB_ERROR (exit 1) rather than a clean refusal.
    """

    def __init__(self, run_func=run_assessment):
        self._lock = threading.Lock()
        self._run = run_func
        self._running = False
        self._seq = 0
        self._started_at = None
        self._result = None

    def state(self):
        with self._lock:
            return RunState(self._running, self._seq, self._started_at, self._result)

    def start(self, db_path, extra_args=()):
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._started_at = datetime.now(timezone.utc)
        threading.Thread(target=self._work, args=(db_path, tuple(extra_args)), daemon=True).start()
        return True

    def _work(self, db_path, extra_args):
        try:
            result = self._run(db_path, extra_args)
        except Exception:                  # surface it; must also fall through to release the guard
            result = RunResult("INTERNAL_ERROR", stderr=traceback.format_exc(), started_at=self._started_at)
        with self._lock:
            self._result = result
            self._seq += 1
            self._running = False


@st.cache_resource
def get_runner():
    return AssessmentRunner()


# --- Rendering ---
def _callout(kind, text):
    {"success": st.success, "info": st.info, "warning": st.warning, "error": st.error}[kind](text)


# Colour encodes provenance: orange simulated, blue external, green calculated, violet assumed.
LANE_COLORS = {"SIMULATED": "orange", "EXTERNAL": "blue", "CALCULATED": "green", "ASSUMED": "violet"}
MAP_HEIGHT = 580
SIMULATED_CHART_COLOR = "#39C6D8"
EXTERNAL_CHART_COLORS = {"Max temperature": "#E8EDF0", "Min temperature": "#96A3AD"}
EXTERNAL_BAR_COLOR = "#78B88A"
CHART_HEIGHT = 280
TAB_LABELS = ("Overview", "Sensor Monitoring", "Weather", "GIS", "Model & Evidence")
AGRI_RECOMMENDATION_KEY = "agri_recommendation"
AGRI_WATER_METRICS_KEY = "agri_water_metrics"

# Static CSS; never interpolate DB text here. Selectors target the Streamlit 1.67 DOM and may
# break on upgrade (stTab is a div[role=tab], not a button; keyed containers get .st-key-<key>).
RESPONSIVE_CSS = """<style>
[data-testid="stMainBlockContainer"] { padding-top: 4rem; }
[data-testid="stMainBlockContainer"] > [data-testid="stVerticalBlock"] { gap: 0.7rem; }
[data-testid="stHeading"] h1 { padding-top: 0.15rem; padding-bottom: 0.25rem; }
[data-testid="stMain"] hr { margin: 0.4rem 0 !important; }
[data-testid="stMetricValue"], [data-testid="stMetricValue"] * {
  white-space: normal !important; overflow: visible !important; text-overflow: clip !important;
  overflow-wrap: anywhere; line-height: 1.3; font-variant-numeric: tabular-nums;
}
[data-testid="stMetricLabel"], [data-testid="stMetricLabel"] * { white-space: normal !important; }
[data-testid="stMetricValue"] { font-size: 1.65rem; letter-spacing: -0.02em; }
[data-testid="stMetricLabel"] { font-size: 0.92rem; }
[data-testid="stCaptionContainer"] { font-size: 0.98rem; line-height: 1.45; }
[data-testid="stAlert"] { border-radius: 8px; }
.st-key-agri_recommendation [data-testid="stAlert"] p {
  font-size: 1.18rem; font-weight: 600; line-height: 1.35;
}
.st-key-agri_water_metrics {
  background: #191F25; border: 1px solid #303A43; border-radius: 8px;
  padding: 0.85rem 1rem 0.7rem; margin: 0.15rem 0 0.15rem;
}
.st-key-agri_water_metrics [data-testid="stMetricValue"] { font-size: 1.75rem; font-weight: 600; }
.st-key-agri_water_metrics [data-testid="stMetricLabel"] { font-size: 0.95rem; color: #96A3AD; }
[data-testid="stTabs"] [role="tablist"] {
  display: flex; flex-wrap: wrap; align-items: stretch; gap: 0.3rem;
  padding: 0.35rem; margin: 0.15rem 0 0.25rem;
  background: #191F25; border: 1px solid #303A43; border-radius: 8px;
}
[data-testid="stTab"] {
  padding: 0.5rem 0.9rem !important; min-height: 2.4rem; border-radius: 6px;
  color: #96A3AD !important; font-size: 0.98rem !important; font-weight: 500 !important;
  cursor: pointer;
}
[data-testid="stTab"] [data-testid="stMarkdownContainer"] p {
  margin: 0 !important; font-size: inherit !important; font-weight: inherit !important;
  color: inherit !important; line-height: 1.3;
}
[data-testid="stTab"][aria-selected="true"], [data-testid="stTab"][data-selected="true"] {
  background: #222A31; color: #E8EDF0 !important; font-weight: 600 !important;
  box-shadow: inset 0 -2px 0 #78B88A;
}
[data-testid="stTab"] .react-aria-SelectionIndicator { height: 0 !important; }
[data-testid="stTabPanel"] { padding-top: 0.65rem; }
@media (max-width: 640px) {
  [data-testid="stMetricValue"], [data-testid="stMetricValue"] * { font-size: 1.4rem !important; }
  [data-testid="stMainBlockContainer"] { padding-left: 1rem; padding-right: 1rem; }
  .st-key-agri_water_metrics [data-testid="stMetricValue"] { font-size: 1.45rem !important; }
  [data-testid="stTabs"] [role="tablist"] { gap: 0.25rem; padding: 0.3rem; }
  [data-testid="stTab"] { padding: 0.45rem 0.7rem !important; min-height: 2.25rem; }
}
</style>"""


def lane_badge(name):
    st.badge(name, color=LANE_COLORS[name])


def active_theme():
    base = st.get_option("theme.base")
    if base in ("light", "dark"):
        return base
    reported = getattr(getattr(st.context, "theme", None), "type", None)
    return "dark" if reported == "dark" else "light"


def weather_history_frame(history):
    frame = pd.DataFrame(history)
    if frame.empty:
        return frame
    frame["day"] = pd.to_datetime(frame["date"], format="%Y-%m-%d", errors="coerce")
    frame = frame.dropna(subset=["day"]).drop_duplicates("day", keep="last").set_index("day")
    for column in ("temperature_max_c", "temperature_min_c", "precipitation_sum_mm"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.reindex(pd.date_range(frame.index.min(), frame.index.max(), freq="D")).rename_axis("day")


def _observed_domain(values, pad_frac=0.15, min_pad=0.5):
    finite = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            finite.append(number)
    if not finite:
        return None
    lo, hi = min(finite), max(finite)
    pad = max((hi - lo) * pad_frac, min_pad)
    return [lo - pad, hi + pad]


def sensor_line_chart(plottable, column, y_title, color, zero=False, y_domain=None):
    data = pd.DataFrame({
        "time": plottable["time_utc"],
        "value": pd.to_numeric(plottable[column], errors="coerce"),
    })
    if not data["value"].notna().any():
        return None
    domain = y_domain if y_domain is not None else (_observed_domain(data["value"]) if not zero else None)
    scale = alt.Scale(zero=zero, nice=zero or y_domain is not None,
                      **({"domain": domain} if domain is not None else {}))
    return alt.Chart(data).mark_line(color=color, point=alt.OverlayMarkDef(filled=True, size=50, color=color)).encode(
        x=alt.X("time:T", title="Time (UTC)", axis=alt.Axis(format="%H:%M:%S", labelAngle=-25)),
        y=alt.Y("value:Q", title=y_title, scale=scale),
        tooltip=[alt.Tooltip("time:T", title="Time (UTC)", format="%Y-%m-%d %H:%M:%S"),
                 alt.Tooltip("value:Q", title=y_title, format=".1f")],
    ).properties(height=CHART_HEIGHT)


def daily_temperature_chart(frame):
    long = frame.reset_index().melt(id_vars="day", value_vars=["temperature_max_c", "temperature_min_c"],
                                    var_name="series", value_name="value")
    long["series"] = long["series"].map({"temperature_max_c": "Max temperature",
                                         "temperature_min_c": "Min temperature"})
    domain = _observed_domain(long["value"])
    scale = alt.Scale(zero=False, nice=False, **({"domain": domain} if domain is not None else {}))
    return alt.Chart(long).mark_line(point=alt.OverlayMarkDef(filled=True, size=70)).encode(
        x=alt.X("day:T", title="Cairo-local day", axis=alt.Axis(format="%m-%d", tickCount="day")),
        y=alt.Y("value:Q", title="Temperature (°C)", scale=scale),
        color=alt.Color("series:N", title=None, legend=alt.Legend(orient="top"),
                        scale=alt.Scale(domain=list(EXTERNAL_CHART_COLORS), range=list(EXTERNAL_CHART_COLORS.values()))),
        tooltip=[alt.Tooltip("day:T", title="Day", format="%Y-%m-%d"), "series:N",
                 alt.Tooltip("value:Q", title="°C", format=".1f")],
    ).properties(title="Daily maximum and minimum temperature", height=CHART_HEIGHT)


def daily_precipitation_chart(frame):
    top = max(1.0, float(frame["precipitation_sum_mm"].max()))        # floor so an all-dry period doesn't collapse the axis
    return alt.Chart(frame.reset_index()).mark_bar(color=EXTERNAL_BAR_COLOR).encode(
        x=alt.X("day:T", title="Cairo-local day", axis=alt.Axis(format="%m-%d", tickCount="day")),
        y=alt.Y("precipitation_sum_mm:Q", title="Precipitation (mm)", scale=alt.Scale(domain=[0, top], nice=True),
                axis=alt.Axis(format="~g")),
        tooltip=[alt.Tooltip("day:T", title="Day", format="%Y-%m-%d"),
                 alt.Tooltip("precipitation_sum_mm:Q", title="mm", format=".1f")],
    ).properties(title="Daily precipitation", height=CHART_HEIGHT)


def resolve_db_path():
    # Test seam: AppTest sets this session key to point at a temp DB. Not reachable from the
    # URL or environment, so production always resolves to DB_PATH.
    return Path(st.session_state.get("_test_db_path", DB_PATH))


def render_header(snapshot):
    study = DEFAULT_CONFIG.study
    st.title("AgriSense")
    st.caption("Precision irrigation decision support for winter wheat — research prototype. "
               "Decision support only: nothing here controls a pump or applies water.")
    place, crop, loaded = st.columns(3)
    place.metric("Study location", f"{study.latitude:.2f}°N, {study.longitude:.2f}°E")
    crop.metric("Modelled crop", DEFAULT_CONFIG.crop.name)
    loaded.metric("Data loaded", f"{snapshot.loaded_at:%Y-%m-%d %H:%M} UTC")
    st.caption(f"Representative study location, not a real monitored farm. {study.description} "
               "No field boundary has been surveyed and no physical sensors are deployed.")
    with st.sidebar:
        st.header("Data and settings")
        if st.button("Reload data", width="stretch"):
            st.rerun()
        st.caption(f"Database: `{display_path(snapshot.db_path)}` (opened read-only)")
        st.caption(f"Loaded: {format_utc(snapshot.loaded_at)}")
        st.caption(f"Latest completed {TIMEZONE} day: {snapshot.expected_day}")
        st.divider()
        st.subheader("How to read data labels")
        st.markdown("- **SIMULATED** - generated by the Wokwi/ESP32 simulation\n"
                    "- **EXTERNAL** - Open-Meteo model data, not station measurements\n"
                    "- **CALCULATED** - FAO-56 computations from the stored weather\n"
                    "- **ASSUMED** - placeholder model parameters, not field measurements")
    render_status(snapshot)


def render_status(snapshot):
    sensors, weather, lookup = snapshot.sensors, snapshot.weather, snapshot.lookup
    a = lookup.assessment if lookup.state == mg.FOUND else None
    sim_lane, ext_lane, calc_lane = st.columns(3)

    with sim_lane:
        lane_badge("SIMULATED")
        latest = sensors.latest_message_timestamp
        stamp = parse_utc(latest)
        shown = "none" if latest is None else (f"{stamp:%Y-%m-%d %H:%M:%S} UTC" if stamp else str(latest))
        st.metric("Latest sensor message (SIMULATED)", shown)
        if sensors.state in (OK, NO_VALID, EMPTY):
            st.caption(str(sensor_freshness(latest, snapshot.loaded_at)))
        else:
            st.caption(f"⚪ {sensors.message}")

    with ext_lane:
        lane_badge("EXTERNAL")
        st.metric("Latest completed weather day (EXTERNAL)", weather.latest_date or "none")
        if weather.state in (OK, NO_COMPLETED, EMPTY):
            st.caption(str(daily_freshness(weather.latest_date, snapshot.expected_day, "completed weather day")))
        else:
            st.caption(f"⚪ {weather.message}")
        if weather.legacy_rows:
            st.caption(f"{weather.legacy_rows} stored daily weather row(s) use another day definition "
                       f"(legacy UTC-day aggregates, api_source != '{DAILY_WEATHER_API_SOURCE}'). "
                       "They are ignored by the assessment and not counted here.")
        if weather.incomplete_rows:
            st.caption(f"{weather.incomplete_rows} {TIMEZONE}-tagged row(s) belong to a day that is not "
                       "complete yet and are not counted.")

    with calc_lane:
        lane_badge("CALCULATED")
        st.metric("Latest assessment date (CALCULATED)", a["assessment_date"] if a else "none")
        st.caption(str(daily_freshness(a["assessment_date"], snapshot.expected_day, "assessment")) if a
                   else f"🔴 {lookup.message}")
        st.metric("Latest assessment status", str(a["status"]) if a else "none")
        st.caption("OK / PROVISIONAL / INSUFFICIENT_DATA are explained with the irrigation assessment." if a else
                   "No stored assessment.")


def _sensor_frame(rows):
    frame = pd.DataFrame(rows)
    frame["time_utc"] = frame["timestamp"].map(parse_utc)
    return frame


def render_sensors(snapshot):
    lane_badge("SIMULATED")
    sensors = snapshot.sensors
    st.info("**SIMULATED DATA.** These readings come from a Wokwi/ESP32 simulation published over public MQTT. "
            "No physical sensor is deployed or calibrated. **Soil moisture is a simulated, uncalibrated "
            "potentiometer reading**, not a volumetric soil-water measurement. It is for monitoring only and is "
            "**not used** in the FAO-56 water balance or the irrigation recommendation.")
    st.metric("MQTT connection", "Unknown")
    st.caption("Not determinable: ingest.py stores received messages only; it writes no heartbeat, "
               "last-will or connection-state record. A recent message is not proof of a live "
               "connection, and an old one is not proof of a dropped one.")

    if sensors.state in (DB_MISSING, TABLE_MISSING, SCHEMA_ERROR, DB_ERROR, EMPTY):
        st.warning(f"No sensor data to show. {sensors.message}")
        return
    st.caption(f"Stored messages for this field: {sensors.total} - valid: {sensors.valid}, "
               f"invalid (out of range): {sensors.invalid}, validity not recorded: {sensors.unknown_validity}. "
               "Only valid readings are shown; nothing is substituted for the others.")
    if sensors.state == NO_VALID:
        st.warning(sensors.message)
        return

    latest = sensors.rows[0]
    fresh = sensor_freshness(latest["timestamp"], snapshot.loaded_at)
    if fresh.level != "FRESH":
        st.warning(f"Latest valid reading: {fresh}. The values below are the last stored ones, not live values.")
    cards = st.columns(3)
    cards[0].metric("Temperature (simulated)", num(latest["temperature_c"], 1, " °C"))
    cards[1].metric("Relative humidity (simulated)", num(latest["humidity_pct"], 1, " %"))
    # TODO: the "soil moisture" source is a potentiometer, so the raw % is not VWC. Needs a
    # per-probe calibration curve (capacitive probe: dry/wet air-water reference points)
    # before this can be shown as volumetric water content.
    cards[2].metric("Soil moisture (simulated, uncalibrated potentiometer)", num(latest["soil_moisture_pct"], 1, " %"))
    stamp = parse_utc(latest["timestamp"])
    st.caption(f"Reading time: {format_utc(stamp) if stamp else md_safe(latest['timestamp'])} - "
               f"device: {md_safe(latest['device_id'])} - sensor status: {md_safe(latest['sensor_status']) or 'not reported'}"
               f" - simulation flag: {'yes' if latest['is_simulation'] else 'no (as stored)'}")

    frame = _sensor_frame(sensors.rows)
    plottable = frame.dropna(subset=["time_utc"]).sort_values("time_utc")
    skipped = len(frame) - len(plottable)
    st.subheader(f"Last {len(frame)} valid readings (UTC)")
    cadence = sensor_cadence(sensors.rows)
    if cadence["median_gap_s"] is None:
        st.caption("Fewer than two usable timestamps: the spacing between readings cannot be determined.")
    else:
        st.caption(f"These {cadence['count']} readings span {humanize(timedelta(seconds=cadence['span_s']))}; "
                   f"the median gap between consecutive readings is {cadence['median_gap_s']:.0f} s. This is a short "
                   "recent window of high-frequency simulated telemetry, not a trend. Daily weather is on the "
                   "Weather tab, never on this seconds-level axis.")
    signals = (
        ("temperature_c", "Temperature (°C)", False, None,
         "Vertical axis follows the observed readings, not 0 °C."),
        ("humidity_pct", "Relative humidity (%)", True, [0, 100], None),
        ("soil_moisture_pct", "Soil moisture — simulated potentiometer (%)", True, [0, 100],
         "Uncalibrated simulated reading; not volumetric water content."),
    )
    for key, y_title, zero, domain, note in signals:
        chart = sensor_line_chart(plottable, key, y_title, SIMULATED_CHART_COLOR, zero=zero, y_domain=domain)
        if chart is None:
            st.caption("No numeric values stored.")
        else:
            st.altair_chart(chart, width="stretch")
            if note:
                st.caption(note)
    if skipped:
        st.caption(f"{skipped} reading(s) have an unparseable timestamp and are listed in the table but not charted.")

    table = pd.DataFrame({
        "Timestamp (UTC)": frame["timestamp"].astype(str),
        "Device": frame["device_id"].astype(str),
        "Temperature (°C)": frame["temperature_c"].map(lambda v: num(v, 1)),
        "Humidity (%)": frame["humidity_pct"].map(lambda v: num(v, 1)),
        "Soil moisture (simulated, uncalibrated, %)": frame["soil_moisture_pct"].map(lambda v: num(v, 1)),
        "Sensor status": frame["sensor_status"].map(lambda v: "not reported" if v is None else str(v)),
        "Simulated flag": frame["is_simulation"].map(lambda v: "yes" if v else "no"),
    })
    st.dataframe(table, hide_index=True, height=260)      # cells pre-formatted by num()


def render_weather(snapshot):
    lane_badge("EXTERNAL")
    weather = snapshot.weather
    st.caption("Open-Meteo model data, not station measurements: one aggregate per completed Cairo-local day, so this "
               "lane gains at most one point a day. It is drawn on its own day axis, never on the seconds-level "
               "sensor axis, and these values feed the FAO-56 calculation while the sensor readings do not.")
    if weather.state in (DB_MISSING, TABLE_MISSING, SCHEMA_ERROR, DB_ERROR, EMPTY):
        st.warning(f"No daily weather to show. {weather.message}")
        return
    if weather.state == NO_COMPLETED:
        st.info(weather.message)
        return

    fresh = daily_freshness(weather.latest_date, snapshot.expected_day, "completed weather day")
    frame = weather_history_frame(weather.history)
    if frame.empty:
        st.info("No completed Cairo-local daily weather records are available to chart.")
        return
    present = int(frame["temperature_max_c"].notna().sum())
    st.caption(f"Latest completed day: {weather.latest_date} - {fresh}. Showing {present} record(s) across "
               f"{len(frame)} calendar day(s) (up to the last {DAILY_HISTORY_DAYS} completed days). Days without a "
               "record are left as gaps, never filled in.")
    has_temp = frame[["temperature_max_c", "temperature_min_c"]].notna().any().any()
    has_rain = frame["precipitation_sum_mm"].notna().any()
    if has_temp and has_rain:
        left, right = st.columns(2)
        with left:
            st.altair_chart(daily_temperature_chart(frame), width="stretch")
            st.caption("Vertical axis follows the observed temperatures, not 0 °C.")
        with right:
            st.altair_chart(daily_precipitation_chart(frame), width="stretch")
            if (frame["precipitation_sum_mm"].dropna() == 0).all():
                st.caption("0 mm recorded on every day shown (dry period, not missing data).")
    elif has_temp:
        st.altair_chart(daily_temperature_chart(frame), width="stretch")
        st.caption("Vertical axis follows the observed temperatures, not 0 °C.")
        st.info("No numeric precipitation stored.")
    elif has_rain:
        st.altair_chart(daily_precipitation_chart(frame), width="stretch")
        if (frame["precipitation_sum_mm"].dropna() == 0).all():
            st.caption("0 mm recorded on every day shown (dry period, not missing data).")
        st.info("No numeric temperatures stored.")
    else:
        st.info("No numeric temperatures or precipitation stored for the days shown.")

    rows = weather.history[::-1]
    table = pd.DataFrame({
        "Day (Cairo)": [str(r["date"]) for r in rows],
        "Tmax (°C)": [num(r["temperature_max_c"], 1) for r in rows],
        "Tmin (°C)": [num(r["temperature_min_c"], 1) for r in rows],
        "RHmax (%)": [num(r["humidity_max_pct"], 0) for r in rows],
        "RHmin (%)": [num(r["humidity_min_pct"], 0) for r in rows],
        "Wind (m/s)": [num(r["wind_speed_10m_mean_m_s"], 2) for r in rows],
        "Solar (MJ/m²/day)": [num(r["solar_radiation_sum_mj_m2"], 1) for r in rows],
        "Rain (mm)": [num(r["precipitation_sum_mm"], 1) for r in rows],
        "Open-Meteo ET0 (mm/day, comparison only)": [num(r["openmeteo_et0_mm"], 2) for r in rows],
        "Stored (UTC)": [str(r["created_at"]) for r in rows],
    })
    st.dataframe(table, hide_index=True, height=260)


def _warning_list(a):
    return mg.parse_warnings(a["warnings"])


def _coverage_caption(coverage):
    if coverage is None:
        return "The stored weather days behind this assessment could not be counted."
    span = f"{coverage.first_day} to {coverage.last_day} ({TIMEZONE} days, EXTERNAL)"
    if coverage.missing:
        shown = ", ".join(str(d) for d in coverage.missing[:5])
        more = f" (+{len(coverage.missing) - 5} more)" if len(coverage.missing) > 5 else ""
        return f"{span}. Missing: {shown}{more}."
    return f"{span}. Every day present."


def render_stored_warnings(a):
    warnings = _warning_list(a)
    if warnings:
        for warning in warnings:
            st.warning(md_safe(warning))
    else:
        st.caption("No warnings stored for this assessment.")
    return warnings


def render_reliability_metrics(snapshot, a):
    warnings = _warning_list(a)
    coverage = snapshot.coverage
    weather = snapshot.weather
    st.markdown("**Reliability evidence** - recorded facts only, no score")
    cols = st.columns(3)
    cols[0].metric("Warnings recorded", str(len(warnings)))
    cols[0].caption("Listed in full below." if warnings else "None recorded for this assessment.")
    if coverage is None:
        cols[1].metric("Weather coverage", "not available")
        cols[1].caption(_coverage_caption(coverage))
    else:
        cols[1].metric("Weather coverage", f"{coverage.present} of {coverage.expected} days")
        cols[1].caption(_coverage_caption(coverage))
    cols[2].metric("Model assumptions", str(len(ASSUMPTION_NOTES)))
    cols[2].caption("Standing ASSUMED parameters, listed below. Not field measurements.")
    weather_text = (str(daily_freshness(weather.latest_date, snapshot.expected_day, "completed weather day"))
                    if weather.state in (OK, NO_COMPLETED, EMPTY) else f"unknown ({weather.message})")
    st.caption(f"Freshness - assessment (CALCULATED): "
               f"{daily_freshness(a['assessment_date'], snapshot.expected_day, 'assessment')}; "
               f"latest weather (EXTERNAL): {weather_text}. "
               "Simulated soil moisture is not an input to this assessment.")


def render_decision(snapshot, runner, run_state):
    lookup = snapshot.lookup
    lane_badge("CALCULATED")
    if lookup.state != mg.FOUND:
        kind = "error" if lookup.state in (SCHEMA_ERROR, DB_ERROR) else "info"
        with st.container(key=AGRI_RECOMMENDATION_KEY):
            _callout(kind, f"**No assessment to show.** {lookup.message}")
        if lookup.state in (mg.TABLE_MISSING, mg.TABLE_EMPTY, mg.NO_MATCH, mg.DB_MISSING):
            st.caption(f"An assessment needs complete {TIMEZONE} daily weather from the simulation start "
                       f"({DEFAULT_CONFIG.study.simulation_start_date}) to the day assessed. Insufficient-data "
                       "results are never stored, so this stays empty until a complete one is computed.")
        render_run_panel(snapshot, runner, run_state, show_result=False)
        return

    a = lookup.assessment
    status = a["status"]
    problems = assessment_problems(a)
    fresh = daily_freshness(a["assessment_date"], snapshot.expected_day, "assessment")
    rec = a["recommendation"]
    depth = a["irrigation_depth_mm"]

    with st.container(key=AGRI_RECOMMENDATION_KEY):
        if rec == irrigation.IRRIGATE:
            st.error(f"**Recommendation: IRRIGATE** - net depth {num(depth, 1, ' mm')}")
        elif rec == irrigation.MONITOR:
            st.warning("**Recommendation: MONITOR**")
        elif rec == irrigation.NO_IRRIGATION:
            st.success("**Recommendation: NO IRRIGATION**")
        else:
            st.info(f"**Recommendation: {md_safe(rec)}**")

    if status == "PROVISIONAL":
        st.warning("**PROVISIONAL.** Computed from hypothetical placeholder parameters (planting date, soil water "
                   "contents, root depths, initial depletion). Not validated against field measurements.")
    elif status == "OK":
        st.success("**OK.** No provisional-assumption warnings were recorded. This is still model-based decision "
                   "support, not a measured field condition.")
    elif status == "INSUFFICIENT_DATA":
        st.error("**INSUFFICIENT_DATA.** The backend does not normally store such a record; do not use its values.")
    else:
        st.error(f"**Unrecognised status:** {md_safe(status)}.")
    if fresh.level != "FRESH":
        st.warning(f"This assessment is for **{md_safe(a['assessment_date'])}** - {fresh.text}. "
                   "It is not presented as current.")
    for problem in problems:
        st.error(f"Record check: {md_safe(problem)}")

    water = (
        ("Root-zone depletion", num(a["depletion_mm"], 1, " mm")),
        ("RAW", num(a["raw_mm"], 1, " mm")),
        ("ET0 (calculated)", num(a["et0_mm"], 2, " mm/day")),
        ("ETc (calculated)", num(a["etc_mm"], 2, " mm/day")),
    )
    with st.container(key=AGRI_WATER_METRICS_KEY):
        for column, (label, value) in zip(st.columns(4), water):
            column.metric(label, value)

    render_run_panel(snapshot, runner, run_state, show_result=False)


def _freshness_line(label, text):
    st.caption(f"{label}: {text}")


def render_overview(snapshot):
    st.caption("Three data lanes with different sampling rates. They are never merged into one series "
               "or one 'latest' value. Simulated telemetry is judged in minutes (about every 5 s); "
               "external weather appears at most once a day; the assessment changes only when a new "
               "assessment is run.")
    sensors, weather, lookup = snapshot.sensors, snapshot.weather, snapshot.lookup
    if sensors.state in (OK, NO_VALID, EMPTY):
        _freshness_line("Sensors (SIMULATED)", sensor_freshness(sensors.latest_message_timestamp, snapshot.loaded_at))
    else:
        _freshness_line("Sensors (SIMULATED)", sensors.message)
    if weather.state in (OK, NO_COMPLETED, EMPTY):
        _freshness_line("Weather (EXTERNAL)",
                        daily_freshness(weather.latest_date, snapshot.expected_day, "completed weather day"))
    else:
        _freshness_line("Weather (EXTERNAL)", weather.message)
    if lookup.state == mg.FOUND:
        _freshness_line("Assessment (CALCULATED)",
                        daily_freshness(lookup.assessment["assessment_date"], snapshot.expected_day, "assessment"))
    else:
        _freshness_line("Assessment (CALCULATED)", lookup.message)
        st.caption("No stored assessment is available to summarise. Use Run New Assessment above when the "
                   f"completed {TIMEZONE} weather series is ready.")
        return

    a = lookup.assessment
    if a["reasoning"]:
        st.markdown(md_safe(a["reasoning"]))
    render_stored_warnings(a)
    coverage = snapshot.coverage
    if coverage is None:
        st.caption("Weather coverage: not available. " + _coverage_caption(coverage))
    else:
        st.caption(f"Weather coverage: {coverage.present} of {coverage.expected} days. "
                   + _coverage_caption(coverage))


def render_model_evidence(snapshot, runner, run_state):
    lookup = snapshot.lookup
    if lookup.state != mg.FOUND:
        st.caption("No stored assessment is available to inspect.")
        render_run_panel(snapshot, runner, run_state, show_button=False, show_result=True)
        return

    a = lookup.assessment
    depth = a["irrigation_depth_mm"]
    st.subheader("FAO-56 calculation")
    crop_row = (
        ("Crop", str(a["crop_id"])),
        ("Growth stage (model assumption)", str(a["growth_stage"] or "n/a")),
        ("Days after planting", num(a["days_after_planting"], 0)),
        ("Kc", num(a["kc"], 2)),
    )
    for column, (label, value) in zip(st.columns(4), crop_row):
        column.metric(label, value)
    balance = (
        ("TAW", num(a["taw_mm"], 1, " mm")),
        ("Net irrigation depth", num(depth, 1, " mm") if depth is not None else "none recommended"),
        ("Status", str(a["status"])),
        ("Assessment date", str(a["assessment_date"])),
    )
    for column, (label, value) in zip(st.columns(4), balance):
        column.metric(label, value)
    if a["openmeteo_et0_mm"] is not None:
        st.caption(f"For comparison only (EXTERNAL, not an input): Open-Meteo ET0 = "
                   f"{num(a['openmeteo_et0_mm'], 2, ' mm/day')}.")

    st.subheader("Reliability and model assumptions")
    render_reliability_metrics(snapshot, a)
    st.subheader("Warnings")
    render_stored_warnings(a)

    with st.expander("Model assumptions", expanded=False):
        st.subheader("Model assumptions")
        st.markdown("\n".join(f"- {md_safe(note)}" for note in ASSUMPTION_NOTES))
        st.caption(f"Water balance simulated from {md_safe(a['simulation_start_date'])}; settings fingerprint "
                   f"`{md_safe(a['config_hash'])}`; stored {md_safe(a['created_at'])} UTC. The assumptions listed are "
                   "those of the current backend configuration.")

    if a["reasoning"]:
        st.subheader("Reasoning (as stored)")
        st.markdown(md_safe(a["reasoning"]))

    render_run_panel(snapshot, runner, run_state, show_button=False, show_result=True)


def request_run(runner, db_path):
    if not runner.start(db_path):
        st.session_state["run_notice"] = "An assessment is already running; the request was ignored."


@st.fragment(run_every=1.0)
def poll_running(runner):
    state = runner.state()
    if state.running:
        st.info(f"Assessment running since {state.started_at:%H:%M:%S} UTC ... (the page refreshes when it finishes)")
        return
    st.rerun()      # done: reload snapshot and map from the DB


def render_run_panel(snapshot, runner, run_state, *, show_button=True, show_result=True):
    if show_button:
        left, right = st.columns([1, 3])
        with left:
            st.button("Run New Assessment", type="primary", key="run_assessment",
                      disabled=run_state.running or not snapshot.db_exists,
                      on_click=request_run, args=(runner, snapshot.db_path))
        with right:
            st.caption(f"Runs `python -m {ASSESSMENT_MODULE}` in a subprocess (working directory `backend`) for the "
                       f"latest completed {TIMEZONE} day ({snapshot.expected_day}). Only that module writes to the "
                       "database; this dashboard stays read-only." +
                       ("" if snapshot.db_exists else " Disabled: the database file does not exist."))
        notice = st.session_state.pop("run_notice", None)
        if notice:
            st.warning(notice)
        if run_state.running:
            poll_running(runner)
            return
    if show_result and not run_state.running and run_state.result is not None:
        render_run_result(run_state.result)


def render_run_result(result):
    kind, text = OUTCOME_TEXT.get(result.outcome, ("warning", result.outcome))
    st.markdown("**Last assessment run**")
    _callout(kind, text)
    cols = st.columns(3)
    cols[0].metric("Return code", "none" if result.returncode is None else str(result.returncode))
    cols[1].metric("Duration", f"{result.duration_s:.1f} s")
    cols[2].metric("Outcome", result.outcome)
    reason = backend_reason(result.stdout) if result.outcome == "INSUFFICIENT_DATA" else None
    if reason:
        st.markdown(f"**Reason reported by the assessment module:** {md_safe(reason)}")
    if result.detail:
        st.caption(md_safe(result.detail))
    if result.command:
        st.caption("Command: `" + " ".join(result.command) + "`")
    with st.expander("Standard output", expanded=False):
        st.code(result.stdout or "(empty)", language=None)
    with st.expander("Standard error", expanded=False):
        st.code(result.stderr or "(empty)", language=None)


def render_map(snapshot):
    st.caption("Representative study location")
    notice = assessment_notice(snapshot.lookup, snapshot.expected_day)
    theme = active_theme()
    fmap = mg.build_map(DEFAULT_CONFIG, snapshot.lookup, notice, theme=theme)
    if snapshot.lookup.state != mg.FOUND:
        st.caption("The map shows no assessment: " + md_safe(snapshot.lookup.message))
    elif notice:
        st.caption("The map marks the assessment as not current: " + md_safe(notice))
    st_folium(fmap, height=MAP_HEIGHT, use_container_width=True, returned_objects=[], key=f"field_map_{theme}")
    st.caption("The marker is a representative study point, not a surveyed field boundary or a physically "
               "monitored farm. No field polygon, soil or terrain data is shown. Map data: OpenStreetMap "
               "contributors.")


def main():
    st.set_page_config(page_title="AgriSense Dashboard", page_icon="🌾", layout="wide")
    st.html(RESPONSIVE_CSS)
    runner = get_runner()
    run_state = runner.state()
    try:
        snapshot = load_snapshot(resolve_db_path())
    except AgriSenseError as exc:   # e.g. tzdata missing: explain instead of showing a stack trace
        st.title("AgriSense")
        st.error(f"The dashboard could not load its data: {exc}")
        st.stop()

    render_header(snapshot)
    st.divider()
    render_decision(snapshot, runner, run_state)
    overview, sensors, weather, gis, evidence = st.tabs(TAB_LABELS)
    with overview:
        render_overview(snapshot)
    with sensors:
        render_sensors(snapshot)
    with weather:
        render_weather(snapshot)
    with gis:
        render_map(snapshot)
    with evidence:
        render_model_evidence(snapshot, runner, run_state)


if __name__ == "__main__":
    main()
