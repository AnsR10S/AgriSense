"""Tests for dashboard/app.py (standard-library unittest + Streamlit's headless AppTest).

Run from the project root, with the packages from backend/requirements.txt installed:
    python -m unittest discover -s dashboard -v

Every test uses temporary SQLite databases. The real backend/agrisense.db is never written;
setUpModule/tearDownModule prove it by hashing the file. All rows are SYNTHETIC TEST DATA.
AppTest runs the script headlessly: this is NOT browser-level verification.
"""

import contextlib
import hashlib
import io
import json
import re
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import folium
import streamlit as st
from streamlit.testing.v1 import AppTest

import app
import ingest                      # backend modules: used only to build fixture schemas
import weather_daily
import map_generator as mg
from agrisense import assessment as backend_assessment
from agrisense.config import DAILY_WEATHER_API_SOURCE, DEFAULT_CONFIG, TIMEZONE, local_today

APP_FILE = str(Path(app.__file__).resolve())
REAL_DB = app.BACKEND_DIR / "agrisense.db"
UTC = timezone.utc
NOW = datetime(2026, 11, 20, 10, 0, 0, tzinfo=UTC)          # fixed clock for load_snapshot tests
FIELD = DEFAULT_CONFIG.study.field_id
CROP = DEFAULT_CONFIG.crop.crop_id

_REAL_DB_HASH = None


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest() if Path(path).exists() else None


def setUpModule():
    global _REAL_DB_HASH
    _REAL_DB_HASH = file_hash(REAL_DB)


def tearDownModule():
    assert file_hash(REAL_DB) == _REAL_DB_HASH, "the real backend/agrisense.db was modified by a test"


ASSESSMENT_INSERT = """
INSERT INTO daily_assessments (
    field_id, assessment_date, crop_id, growth_stage, days_after_planting, et0_mm, kc, etc_mm, ks,
    root_depth_m, taw_mm, raw_mm, depletion_mm, recommendation, irrigation_depth_mm, status, reasoning,
    openmeteo_et0_mm, simulation_start_date, warnings, config_hash, config_json, created_at)
VALUES (:field_id, :assessment_date, :crop_id, :growth_stage, :days_after_planting, :et0_mm, :kc, :etc_mm, :ks,
    :root_depth_m, :taw_mm, :raw_mm, :depletion_mm, :recommendation, :irrigation_depth_mm, :status, :reasoning,
    :openmeteo_et0_mm, :simulation_start_date, :warnings, :config_hash, :config_json, :created_at)
"""
WEATHER_INSERT = (
    "INSERT INTO daily_weather_observations (field_id, date, temperature_max_c, temperature_min_c, "
    "humidity_max_pct, humidity_min_pct, surface_pressure_mean_hpa, wind_speed_10m_mean_m_s, "
    "solar_radiation_sum_mj_m2, precipitation_sum_mm, openmeteo_et0_mm, api_source) "
    "VALUES (?, ?, 26.0, 14.0, 95.0, 42.0, 999.5, 2.5, 14.0, 0.0, 3.0, ?)")
SENSOR_INSERT = (
    "INSERT INTO sensor_readings (device_id, field_id, timestamp, temperature_c, humidity_pct, "
    "soil_moisture_pct, is_simulation, sensor_status, is_valid) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)")


def stamp(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def assessment_row(**overrides):
    row = dict(
        field_id=FIELD, assessment_date="2026-11-19", crop_id=CROP, growth_stage="initial",
        days_after_planting=4, et0_mm=2.97, kc=0.7, etc_mm=2.08, ks=1.0, root_depth_m=0.26, taw_mm=30.9,
        raw_mm=17.0, depletion_mm=4.4, recommendation="NO_IRRIGATION", irrigation_depth_mm=None,
        status="PROVISIONAL", reasoning="SYNTHETIC reasoning text.", openmeteo_et0_mm=3.06,
        simulation_start_date="2026-11-15",
        warnings=json.dumps(["SYNTHETIC placeholder-parameter warning.", "Only 5 day(s) simulated."]),
        config_hash="hash_a", config_json="{}", created_at="2026-11-20 08:00:00")
    row.update(overrides)
    return row


class DbTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.db = self.dir / "test.db"

    def make_db(self, sensors=True, weather=True, assessments=True):
        with contextlib.redirect_stdout(io.StringIO()):
            if sensors:
                ingest.init_db(self.db).close()
            if weather:
                weather_daily.init_db(self.db).close()
            if assessments:
                backend_assessment.init_db(self.db).close()
        if not (sensors or weather or assessments):
            sqlite3.connect(self.db).close()           # an empty database file with no tables

    def execute(self, sql, params=()):
        conn = sqlite3.connect(self.db)
        conn.execute(sql, params)
        conn.commit()
        conn.close()

    def add_sensor(self, ts, temp=22.5, hum=55.0, soil=41.0, valid=1, status="normal", sim=1,
                   field=FIELD, device="agrisense_001"):
        self.execute(SENSOR_INSERT, (device, field, ts, temp, hum, soil, sim, status, valid))

    def add_weather(self, day, source=DAILY_WEATHER_API_SOURCE, field=FIELD):
        self.execute(WEATHER_INSERT, (field, day.isoformat(), source))

    def add_assessment(self, **overrides):
        self.execute(ASSESSMENT_INSERT, assessment_row(**overrides))

    def snapshot(self, now=NOW):
        return app.load_snapshot(self.db, now_utc=now)


class FormattingTests(unittest.TestCase):
    def test_md_safe_neutralises_markdown_and_collapses_newlines(self):
        out = app.md_safe("<script>alert(1)</script> **bold** [x](http://e.com) `c` $y$ :smile:\nnext")
        for raw in ("<script>", "**bold**", "[x](http", "`c`", "$y$", ":smile:"):
            self.assertNotIn(raw, out)
        self.assertNotIn("\n", out)
        self.assertEqual(app.md_safe(None), "")

    def test_num(self):
        self.assertEqual(app.num(2.345, 2, " mm"), "2.35 mm")
        self.assertEqual(app.num("3.2", 1), "3.2")
        self.assertEqual(app.num(None), "n/a")
        for bad in ("abc", float("nan"), float("inf"), True, [1]):
            self.assertEqual(app.num(bad), "invalid")

    def test_parse_utc(self):
        self.assertEqual(app.parse_utc("2026-11-15T00:07:17Z"), datetime(2026, 11, 15, 0, 7, 17, tzinfo=UTC))
        self.assertEqual(app.parse_utc("2026-11-15T02:07:17+02:00"), datetime(2026, 11, 15, 0, 7, 17, tzinfo=UTC))
        for bad in (None, "", "garbage", "2026-11-15T00:07:17", 12345):     # naive: do not guess a zone
            self.assertIsNone(app.parse_utc(bad))

    def test_humanize(self):
        self.assertEqual(app.humanize(timedelta(seconds=42)), "42 s")
        self.assertEqual(app.humanize(timedelta(minutes=5)), "5 min")
        self.assertEqual(app.humanize(timedelta(hours=3, minutes=51)), "3 h 51 min")
        self.assertEqual(app.humanize(timedelta(days=2, hours=4)), "2 d 4 h")

    def test_sensor_freshness_levels(self):
        now = NOW
        self.assertEqual(app.sensor_freshness(stamp(now - timedelta(minutes=2)), now).level, "FRESH")
        self.assertEqual(app.sensor_freshness(stamp(now - timedelta(minutes=16)), now).level, "STALE")
        self.assertEqual(app.sensor_freshness(None, now).level, "MISSING")
        self.assertEqual(app.sensor_freshness("garbage", now).level, "UNKNOWN")
        self.assertEqual(app.sensor_freshness(stamp(now + timedelta(hours=1)), now).level, "UNKNOWN")
        self.assertIn("🟠", str(app.sensor_freshness(stamp(now - timedelta(hours=1)), now)))

    def test_daily_freshness_levels(self):
        expected = date(2026, 11, 19)
        self.assertEqual(app.daily_freshness("2026-11-19", expected, "x").level, "FRESH")
        stale = app.daily_freshness("2026-11-16", expected, "x")
        self.assertEqual(stale.level, "STALE")
        self.assertIn("3 day(s) behind", stale.text)
        self.assertEqual(app.daily_freshness(None, expected, "x").level, "MISSING")
        self.assertEqual(app.daily_freshness("bad", expected, "x").level, "UNKNOWN")
        self.assertEqual(app.daily_freshness("2026-11-20", expected, "x").level, "UNKNOWN")

    def test_assessment_notice(self):
        expected = date(2026, 11, 19)
        found = lambda day: mg.AssessmentLookup(mg.FOUND, "", assessment_row(assessment_date=day))
        self.assertIsNone(app.assessment_notice(found("2026-11-19"), expected))
        self.assertIn("behind", app.assessment_notice(found("2026-11-10"), expected))
        self.assertIsNone(app.assessment_notice(mg.AssessmentLookup(mg.TABLE_EMPTY, "x"), expected))


class AssessmentProblemTests(unittest.TestCase):
    def test_complete_record_has_no_problems(self):
        self.assertEqual(app.assessment_problems(assessment_row()), [])
        self.assertEqual(app.assessment_problems(assessment_row(status="OK", warnings="[]")), [])

    def test_each_kind_of_incomplete_or_inconsistent_record_is_reported(self):
        cases = {
            "ET0 is missing": assessment_row(et0_mm=None),
            "RAW is not a valid number": assessment_row(raw_mm="oops"),
            "Unrecognised status": assessment_row(status="MAYBE"),
            "Unrecognised recommendation": assessment_row(recommendation="WATER_NOW"),
            "no warnings are stored": assessment_row(warnings=None),
            "although warnings are stored": assessment_row(status="OK"),
            "Net irrigation depth is not a valid number": assessment_row(irrigation_depth_mm="x"),
        }
        for expected, row in cases.items():
            with self.subTest(expected):
                self.assertTrue(any(expected in p for p in app.assessment_problems(row)), expected)

    def test_insufficient_data_record_is_not_flagged_for_missing_numbers(self):
        row = assessment_row(status="INSUFFICIENT_DATA", recommendation="INSUFFICIENT_DATA", et0_mm=None,
                             kc=None, etc_mm=None, depletion_mm=None, taw_mm=None, raw_mm=None, warnings="[]")
        self.assertEqual(app.assessment_problems(row), [])


class LoadSnapshotStateTests(DbTestCase):
    def test_missing_database_file(self):
        snap = self.snapshot()
        self.assertFalse(snap.db_exists)
        self.assertEqual(snap.sensors.state, app.DB_MISSING)
        self.assertEqual(snap.weather.state, app.DB_MISSING)
        self.assertEqual(snap.lookup.state, mg.DB_MISSING)
        self.assertFalse(self.db.exists())                      # read-only: never created

    def test_database_with_no_tables(self):
        self.make_db(sensors=False, weather=False, assessments=False)
        snap = self.snapshot()
        self.assertEqual(snap.sensors.state, app.TABLE_MISSING)
        self.assertEqual(snap.weather.state, app.TABLE_MISSING)
        self.assertEqual(snap.lookup.state, mg.TABLE_MISSING)

    def test_empty_tables(self):
        self.make_db()
        snap = self.snapshot()
        self.assertEqual(snap.sensors.state, app.EMPTY)
        self.assertEqual(snap.weather.state, app.EMPTY)
        self.assertEqual(snap.lookup.state, mg.TABLE_EMPTY)

    def test_expected_day_is_the_latest_completed_cairo_day(self):
        self.make_db()
        late = datetime(2026, 11, 20, 22, 30, tzinfo=UTC)       # already 2026-11-21 in Cairo
        snap = self.snapshot(late)
        self.assertEqual(snap.today_local, date(2026, 11, 21))
        self.assertEqual(snap.expected_day, date(2026, 11, 20))

    def test_unreadable_file_is_reported_not_raised(self):
        self.db.write_bytes(b"this is not a database" * 50)
        snap = self.snapshot()
        self.assertEqual(snap.sensors.state, app.DB_ERROR)
        self.assertEqual(snap.weather.state, app.DB_ERROR)
        self.assertEqual(snap.lookup.state, app.DB_ERROR)
        self.assertIn("Database error", snap.sensors.message)

    def test_unexpected_table_structures_are_schema_errors(self):
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE sensor_readings (id INTEGER, field_id TEXT)")
        conn.execute("CREATE TABLE daily_weather_observations (id INTEGER, field_id TEXT)")
        conn.execute("CREATE TABLE daily_assessments (id INTEGER, field_id TEXT)")
        conn.commit()
        conn.close()
        snap = self.snapshot()
        self.assertEqual(snap.sensors.state, app.SCHEMA_ERROR)
        self.assertEqual(snap.weather.state, app.SCHEMA_ERROR)
        self.assertEqual(snap.lookup.state, app.SCHEMA_ERROR)
        self.assertIn("missing expected column", snap.lookup.message)


class LoadSnapshotSensorTests(DbTestCase):
    def setUp(self):
        super().setUp()
        self.make_db()

    def test_fifty_most_recent_valid_readings_newest_first(self):
        for i in range(60):
            self.add_sensor(stamp(datetime(2026, 11, 20, 8, 0, tzinfo=UTC) + timedelta(seconds=5 * i)), temp=float(i))
        sensors = self.snapshot().sensors
        self.assertEqual(sensors.state, app.OK)
        self.assertEqual(len(sensors.rows), 50)
        self.assertEqual(sensors.rows[0]["temperature_c"], 59.0)
        self.assertEqual(sensors.rows[-1]["temperature_c"], 10.0)
        self.assertEqual(sensors.total, 60)

    def test_invalid_and_unvalidated_rows_are_counted_but_never_shown(self):
        self.add_sensor("2026-11-20T09:00:00Z", temp=20.0)
        self.add_sensor("2026-11-20T09:05:00Z", temp=99.0, valid=0)           # newest message, invalid
        self.add_sensor("2026-11-20T09:04:00Z", temp=88.0, valid=None)
        sensors = self.snapshot().sensors
        self.assertEqual((sensors.total, sensors.valid, sensors.invalid, sensors.unknown_validity), (3, 1, 1, 1))
        self.assertEqual([r["temperature_c"] for r in sensors.rows], [20.0])
        self.assertEqual(sensors.latest_message_timestamp, "2026-11-20T09:05:00Z")   # newest message of any kind

    def test_only_invalid_readings(self):
        self.add_sensor("2026-11-20T09:00:00Z", valid=0)
        sensors = self.snapshot().sensors
        self.assertEqual(sensors.state, app.NO_VALID)
        self.assertEqual(sensors.rows, [])

    def test_other_fields_are_ignored(self):
        self.add_sensor("2026-11-20T09:00:00Z", field="field_999")
        self.assertEqual(self.snapshot().sensors.state, app.EMPTY)

    def test_null_values_are_kept_as_null(self):
        self.add_sensor("2026-11-20T09:00:00Z", temp=None, hum=None, soil=None, status=None)
        row = self.snapshot().sensors.rows[0]
        self.assertIsNone(row["temperature_c"])
        self.assertIsNone(row["sensor_status"])


class LoadSnapshotWeatherTests(DbTestCase):
    def setUp(self):
        super().setUp()
        self.make_db()

    def test_latest_completed_cairo_day_ignores_legacy_and_incomplete_rows(self):
        self.add_weather(date(2026, 11, 17))
        self.add_weather(date(2026, 11, 18))
        self.add_weather(date(2026, 11, 19), source="open-meteo")            # legacy UTC-day row
        self.add_weather(date(2026, 11, 20))                                  # today (Cairo): incomplete
        weather = self.snapshot().weather
        self.assertEqual(weather.state, app.OK)
        self.assertEqual(weather.latest_date, "2026-11-18")
        self.assertEqual((weather.completed_rows, weather.legacy_rows, weather.incomplete_rows), (2, 1, 1))
        self.assertEqual(weather.latest_row["api_source"], DAILY_WEATHER_API_SOURCE)
        fresh = app.daily_freshness(weather.latest_date, date(2026, 11, 19), "x")
        self.assertEqual(fresh.level, "STALE")

    def test_only_legacy_rows_means_no_completed_day(self):
        self.add_weather(date(2026, 11, 13), source="open-meteo")
        weather = self.snapshot().weather
        self.assertEqual(weather.state, app.NO_COMPLETED)
        self.assertIsNone(weather.latest_date)
        self.assertEqual(weather.legacy_rows, 1)

    def test_fresh_when_yesterday_is_present(self):
        self.add_weather(date(2026, 11, 19))
        weather = self.snapshot().weather
        self.assertEqual(app.daily_freshness(weather.latest_date, date(2026, 11, 19), "x").level, "FRESH")


class LoadSnapshotAssessmentTests(DbTestCase):
    def setUp(self):
        super().setUp()
        self.make_db()

    def test_latest_assessment_with_extended_columns(self):
        self.add_assessment(assessment_date="2026-11-18", config_hash="a")
        self.add_assessment(assessment_date="2026-11-19", config_hash="b", recommendation="MONITOR")
        lookup = self.snapshot().lookup
        self.assertEqual(lookup.state, mg.FOUND)
        self.assertEqual(lookup.assessment["assessment_date"], "2026-11-19")
        for key in ("reasoning", "simulation_start_date", "config_hash", "ks", "openmeteo_et0_mm"):
            self.assertIn(key, lookup.assessment)

    def test_assessment_for_other_field_or_crop_does_not_match(self):
        self.add_assessment(field_id="field_999")
        self.add_assessment(crop_id="maize", config_hash="b")
        self.assertEqual(self.snapshot().lookup.state, mg.NO_MATCH)


class ReadOnlyAccessTests(DbTestCase):
    def setUp(self):
        super().setUp()
        self.make_db()
        self.add_sensor("2026-11-20T09:00:00Z")
        self.add_weather(date(2026, 11, 19))
        self.add_assessment()

    def test_load_uses_one_read_only_connection_to_the_given_file_and_only_reads(self):
        statements, uris = [], []
        real_connect = sqlite3.connect

        def spy(database, *args, **kwargs):
            uris.append((database, kwargs.get("uri")))
            conn = real_connect(database, *args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn

        with mock.patch.object(sqlite3, "connect", spy):
            snap = self.snapshot()
        self.assertEqual(snap.lookup.state, mg.FOUND)
        self.assertEqual(len(uris), 1, "exactly one connection per data load")
        target, uri_flag = uris[0]
        self.assertTrue(uri_flag)
        self.assertTrue(target.startswith("file:"))
        self.assertTrue(target.endswith("?mode=ro"))
        self.assertIn(self.db.name, target)
        self.assertTrue(statements)
        for statement in statements:
            self.assertRegex(statement.lstrip().upper(), r"^(SELECT|PRAGMA)\b", statement)

    def test_load_leaves_the_database_file_byte_identical(self):
        before = file_hash(self.db)
        self.snapshot()
        self.assertEqual(file_hash(self.db), before)

    def test_connection_is_closed_after_loading(self):
        opened = []
        real_open = mg.open_readonly

        def tracking(path):
            conn = real_open(path)
            opened.append(conn)
            return conn

        with mock.patch.object(mg, "open_readonly", tracking):
            self.snapshot()
        self.assertEqual(len(opened), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")

    def test_dashboard_source_contains_no_writes_and_no_other_database(self):
        source = Path(app.__file__).read_text(encoding="utf-8")
        for forbidden in ("INSERT INTO", "UPDATE ", "DELETE FROM", "CREATE TABLE", "ALTER TABLE", "DROP TABLE",
                          "sqlite3.connect", "shell=True", "data/agrisense.db", "data\\agrisense.db"):
            self.assertNotIn(forbidden, source)

    def test_database_selection(self):
        self.assertEqual(app.DB_PATH, app.PROJECT_ROOT / "backend" / "agrisense.db")
        self.assertEqual(app.DB_PATH, mg.DB_PATH)
        self.assertEqual(app.DB_PATH, backend_assessment.DB_PATH)
        self.assertEqual(app.load_snapshot.__defaults__[0], app.DB_PATH)


FAKE_CLOCK = ("import sys; from datetime import date; import agrisense.assessment as a; "
              "a.local_today = lambda *args, **kwargs: date(2026, 11, 25); sys.exit(a.main(sys.argv[1:]))")


class ClassifyRunTests(unittest.TestCase):
    def test_outcomes(self):
        self.assertEqual(app.classify_run(0, "x\n[INSERTED] Assessment saved"), "SAVED")
        self.assertEqual(app.classify_run(0, "[DUPLICATE] exists"), "DUPLICATE")
        self.assertEqual(app.classify_run(0, "[NOT SAVED] Insufficient data: nothing"), "INSUFFICIENT_DATA")
        self.assertEqual(app.classify_run(0, "something else"), "COMPLETED_UNRECOGNISED")
        self.assertEqual(app.classify_run(1, ""), "DB_ERROR")
        self.assertEqual(app.classify_run(2, ""), "ERROR")
        self.assertEqual(app.classify_run(7, ""), "FAILED")

    def test_every_outcome_has_an_explanation(self):
        for outcome in ("SAVED", "DUPLICATE", "INSUFFICIENT_DATA", "COMPLETED_UNRECOGNISED", "DB_ERROR", "ERROR",
                        "FAILED", "TIMEOUT", "LAUNCH_ERROR", "INTERNAL_ERROR"):
            self.assertIn(outcome, app.OUTCOME_TEXT)


class SubprocessCallTests(DbTestCase):
    def test_subprocess_is_called_safely(self):
        completed = mock.Mock(returncode=0, stdout="[INSERTED]", stderr="")
        with mock.patch.object(app.subprocess, "run", return_value=completed) as run:
            result = app.run_assessment(self.db)
        args, kwargs = run.call_args
        command = args[0]
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(command[1:3], ("-m", "agrisense.assessment"))
        self.assertEqual(command[command.index("--db") + 1], str(self.db.resolve()))
        self.assertTrue(Path(command[command.index("--db") + 1]).is_absolute())
        self.assertIs(kwargs["shell"], False)
        self.assertTrue(kwargs["capture_output"])
        self.assertEqual(kwargs["cwd"], str(app.BACKEND_DIR))
        self.assertTrue(Path(kwargs["cwd"]).is_absolute())
        self.assertGreater(kwargs["timeout"], 0)
        self.assertEqual(result.outcome, "SAVED")

    def test_assessment_script_exists_at_an_absolute_path(self):
        self.assertTrue(app.ASSESSMENT_SCRIPT.is_absolute())
        self.assertTrue(app.ASSESSMENT_SCRIPT.is_file())
        self.assertEqual(app.ASSESSMENT_SCRIPT, Path(backend_assessment.__file__).resolve())

    def test_missing_assessment_script_is_a_launch_error(self):
        with mock.patch.object(app, "ASSESSMENT_SCRIPT", self.dir / "nope.py"):
            result = app.run_assessment(self.db)
        self.assertEqual(result.outcome, "LAUNCH_ERROR")
        self.assertIn("not found", result.detail)

    def test_stdout_and_stderr_are_captured_separately(self):
        code = "import sys; print('to-out'); print('to-err', file=sys.stderr); sys.exit(0)"
        result = app.run_assessment(self.db, command=[sys.executable, "-c", code])
        self.assertIn("to-out", result.stdout)
        self.assertNotIn("to-err", result.stdout)
        self.assertIn("to-err", result.stderr)
        self.assertNotIn("to-out", result.stderr)

    def test_timeout(self):
        started = time.monotonic()
        result = app.run_assessment(self.db, command=[sys.executable, "-c", "import time; time.sleep(30)"],
                                    timeout=1)
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(result.outcome, "TIMEOUT")
        self.assertIsNone(result.returncode)

    def test_launch_failure(self):
        result = app.run_assessment(self.db, command=[str(self.dir / "no_such_python.exe")])
        self.assertEqual(result.outcome, "LAUNCH_ERROR")
        self.assertTrue(result.detail)


class RealAssessmentModuleTests(DbTestCase):

    def test_insufficient_data_is_reported_and_nothing_is_stored(self):
        self.make_db(assessments=False)                       # no weather rows at all
        result = app.run_assessment(self.db)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(result.outcome, "INSUFFICIENT_DATA")
        self.assertIn("INSUFFICIENT_DATA", result.stdout)
        self.assertIn("[NOT SAVED]", result.stdout)
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM daily_assessments").fetchone()[0], 0)
        conn.close()

    def test_argument_error_exit_code_two(self):
        self.make_db()
        result = app.run_assessment(self.db, extra_args=["--date", "not-a-date"])
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.outcome, "ERROR")
        self.assertTrue(result.stderr)

    def test_unopenable_database_exit_code_one(self):
        result = app.run_assessment(self.dir)                  # a directory is not a database
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.outcome, "DB_ERROR")
        self.assertIn("[DB ERROR]", result.stdout)

    def test_successful_run_then_duplicate_then_dashboard_reads_it_back(self):
        self.make_db(assessments=False)
        day = DEFAULT_CONFIG.study.simulation_start_date      # the engine needs every day from the configured start
        while day <= date(2026, 11, 20):
            self.add_weather(day)
            day += timedelta(days=1)
        command = [sys.executable, "-c", FAKE_CLOCK, "--db", str(self.db), "--date", "2026-11-20"]
        first = app.run_assessment(self.db, command=command)
        self.assertEqual((first.returncode, first.outcome), (0, "SAVED"), first.stdout + first.stderr)
        second = app.run_assessment(self.db, command=command)
        self.assertEqual(second.outcome, "DUPLICATE")

        snap = app.load_snapshot(self.db, now_utc=datetime(2026, 11, 25, 10, 0, tzinfo=UTC))
        self.assertEqual(snap.lookup.state, mg.FOUND)
        a = snap.lookup.assessment
        self.assertEqual(a["assessment_date"], "2026-11-20")
        self.assertEqual(a["status"], "PROVISIONAL")
        self.assertEqual(app.assessment_problems(a), [])
        self.assertTrue(mg.parse_warnings(a["warnings"]))
        self.assertEqual(snap.weather.latest_date, "2026-11-20")


class AssessmentRunnerTests(unittest.TestCase):
    def wait_idle(self, runner, timeout=10):
        deadline = time.monotonic() + timeout
        while runner.state().running:
            self.assertLess(time.monotonic(), deadline, "runner never finished")
            time.sleep(0.01)

    def test_second_start_is_refused_while_running_and_result_is_recorded(self):
        gate = threading.Event()
        calls = []

        def slow(db, extra):
            calls.append(db)
            gate.wait(10)
            return app.RunResult("SAVED", 0)

        runner = app.AssessmentRunner(slow)
        self.assertTrue(runner.start("db"))
        self.assertTrue(runner.state().running)
        self.assertFalse(runner.start("db"))
        self.assertFalse(runner.start("db"))
        gate.set()
        self.wait_idle(runner)
        state = runner.state()
        self.assertEqual((len(calls), state.seq, state.result.outcome), (1, 1, "SAVED"))
        self.assertTrue(runner.start("db"))                    # free again after finishing
        self.wait_idle(runner)
        self.assertEqual(runner.state().seq, 2)

    def test_simultaneous_starts_from_many_threads_run_once(self):
        gate = threading.Event()
        runs = []

        def slow(db, extra):
            runs.append(1)
            gate.wait(10)
            return app.RunResult("SAVED", 0)

        runner = app.AssessmentRunner(slow)
        accepted, barrier = [], threading.Barrier(12)

        def click():
            barrier.wait()
            accepted.append(runner.start("db"))

        threads = [threading.Thread(target=click) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        gate.set()
        self.wait_idle(runner)
        self.assertEqual(accepted.count(True), 1)
        self.assertEqual(len(runs), 1)

    def test_unexpected_exception_is_recorded_and_releases_the_guard(self):
        def boom(db, extra):
            raise RuntimeError("kaboom")

        runner = app.AssessmentRunner(boom)
        self.assertTrue(runner.start("db"))
        self.wait_idle(runner)
        state = runner.state()
        self.assertEqual(state.result.outcome, "INTERNAL_ERROR")
        self.assertIn("kaboom", state.result.stderr)
        self.assertTrue(runner.start("db"))
        self.wait_idle(runner)


class AppTestCase(DbTestCase):
    def setUp(self):
        super().setUp()
        st.cache_resource.clear()                              # fresh process-wide AssessmentRunner
        self.addCleanup(st.cache_resource.clear)

    def run_app(self, with_db=True, folium_mock=None):
        at = AppTest.from_file(APP_FILE, default_timeout=60)
        if with_db:
            at.session_state["_test_db_path"] = str(self.db)
        if folium_mock is not None:
            with mock.patch("streamlit_folium.st_folium", folium_mock):
                at.run()
        else:
            at.run()
        return at

    @staticmethod
    def text_of(at):
        parts = []
        for kind in ("markdown", "caption", "info", "warning", "error", "success"):
            parts += [str(e.value) for e in getattr(at, kind)]
        parts += [f"{m.label}: {m.value}" for m in at.metric]
        return "\n".join(parts)

    @staticmethod
    def metric(at, label_start):
        return next(m for m in at.metric if m.label.startswith(label_start))

    @staticmethod
    def tab(at, label):
        matches = [t for t in at.tabs if t.label == label]
        if len(matches) != 1:
            raise AssertionError(f"expected one tab {label!r}, got {[t.label for t in at.tabs]}")
        return matches[0]

    @classmethod
    def tab_text(cls, tab):
        return cls.text_of(tab)

    def assert_no_exception(self, at):
        self.assertEqual([e.value for e in at.exception], [])

    def yesterday(self):
        return local_today(TIMEZONE) - timedelta(days=1)


class StartupAndEmptyStateTests(AppTestCase):
    def test_startup_against_the_real_project_database_without_overrides(self):
        at = self.run_app(with_db=False)
        self.assert_no_exception(at)
        self.assertEqual([t.label for t in at.tabs], list(app.TAB_LABELS))
        self.assertFalse(at.button(key="run_assessment") is None)
        self.assertFalse(any(getattr(b, "key", None) == "run_assessment"
                             for t in at.tabs for b in t.button))
        self.assertTrue(any(m.label.startswith("MQTT connection")
                            for m in self.tab(at, "Sensor Monitoring").metric))
        self.assertFalse(any(m.label.startswith("MQTT connection")
                             for m in self.tab(at, "Overview").metric))
        self.assertIn("Representative study location", self.tab_text(self.tab(at, "GIS")))
        sidebar = " ".join(c.value for c in at.sidebar.caption)
        self.assertIn("backend/agrisense.db", sidebar)
        self.assertIn("read-only", sidebar)

    def test_missing_database_file(self):
        at = self.run_app()
        self.assert_no_exception(at)
        text = self.text_of(at)
        self.assertIn("Database file not found", text)
        self.assertIn("No sensor data to show", text)
        self.assertEqual(self.metric(at, "Latest assessment date").value, "none")
        self.assertTrue(at.button(key="run_assessment").disabled)
        self.assertFalse(self.db.exists())

    def test_database_without_tables(self):
        self.make_db(sensors=False, weather=False, assessments=False)
        at = self.run_app()
        self.assert_no_exception(at)
        text = self.text_of(at)
        self.assertIn("Table sensor_readings does not exist", text)
        self.assertIn("Table daily_weather_observations does not exist", text)
        self.assertIn("daily_assessments does not exist", text)

    def test_empty_tables_show_informative_empty_states(self):
        self.make_db()
        at = self.run_app()
        self.assert_no_exception(at)
        text = self.text_of(at)
        self.assertIn("no rows for field", text)
        self.assertIn("contains no assessments", text)
        self.assertEqual(self.metric(at, "Latest sensor message").value, "none")
        self.assertFalse(at.button(key="run_assessment").disabled)

    def test_unreadable_database_does_not_crash(self):
        self.db.write_bytes(b"not a database" * 100)
        at = self.run_app()
        self.assert_no_exception(at)
        self.assertIn("Database error", self.text_of(at))


class ProvenanceTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.make_db()

    def test_mqtt_status_is_unknown_with_an_explanation_even_with_fresh_messages(self):
        self.add_sensor(stamp(datetime.now(UTC) - timedelta(seconds=10)))
        at = self.run_app()
        self.assertEqual(self.metric(at, "MQTT connection").value, "Unknown")
        text = self.text_of(at)
        self.assertIn("Not determinable", text)
        self.assertIn("heartbeat", text)
        mqtt_values = [m.value for m in at.metric if "MQTT" in m.label]
        self.assertEqual(mqtt_values, ["Unknown"])            # never "Connected"/"Disconnected"
        for claim in ("Connected", "Disconnected", "MQTT is connected", "broker connected"):
            self.assertNotIn(claim, text)

    def test_fresh_and_stale_sensor_indicators(self):
        self.add_sensor(stamp(datetime.now(UTC) - timedelta(seconds=30)))
        text = self.text_of(self.run_app())
        self.assertIn("🟢 Fresh", text)
        self.assertNotIn("not live values", text)

        self.db.unlink()
        self.make_db()
        self.add_sensor(stamp(datetime.now(UTC) - timedelta(hours=3)))
        text = self.text_of(self.run_app())
        self.assertIn("🟠 Stale", text)
        self.assertIn("not live values", text)

    def test_weather_freshness_fresh_stale_legacy(self):
        self.add_weather(self.yesterday())
        at = self.run_app()
        self.assertEqual(self.metric(at, "Latest completed weather day").value, self.yesterday().isoformat())
        self.assertIn("Up to date", self.text_of(at))

        self.db.unlink()
        self.make_db()
        self.add_weather(self.yesterday() - timedelta(days=3))
        self.add_weather(self.yesterday(), source="open-meteo")           # legacy: must not count as fresh
        at = self.run_app()
        text = self.text_of(at)
        self.assertEqual(self.metric(at, "Latest completed weather day").value,
                         (self.yesterday() - timedelta(days=3)).isoformat())
        self.assertIn("3 day(s) behind", text)
        self.assertIn("legacy UTC-day aggregates", text)

    def test_no_completed_weather_day(self):
        self.add_weather(self.yesterday(), source="open-meteo")
        at = self.run_app()
        self.assertEqual(self.metric(at, "Latest completed weather day").value, "none")
        self.assertIn("No completed", self.text_of(at))

    def test_assessment_date_and_status_cards(self):
        self.add_assessment(assessment_date=self.yesterday().isoformat(), status="PROVISIONAL")
        at = self.run_app()
        self.assertEqual(self.metric(at, "Latest assessment date").value, self.yesterday().isoformat())
        self.assertEqual(self.metric(at, "Latest assessment status").value, "PROVISIONAL")
        self.assertIn("Up to date", self.text_of(at))


class SensorDisplayTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.make_db()

    def test_cards_values_labels_and_simulation_warnings(self):
        base = datetime.now(UTC) - timedelta(minutes=1)
        self.add_sensor(stamp(base - timedelta(seconds=10)), temp=20.0, hum=50.0, soil=30.0)
        self.add_sensor(stamp(base), temp=22.5, hum=55.5, soil=41.0, status="normal")
        at = self.run_app()
        self.assert_no_exception(at)
        self.assertEqual(self.metric(at, "Temperature").value, "22.5 °C")
        self.assertEqual(self.metric(at, "Relative humidity").value, "55.5 %")
        soil = self.metric(at, "Soil moisture")
        self.assertEqual(soil.value, "41.0 %")
        self.assertIn("simulated", soil.label)
        self.assertIn("uncalibrated", soil.label)
        self.assertIn("potentiometer", soil.label)
        text = self.text_of(at)
        self.assertIn("SIMULATED DATA", text)
        self.assertIn("not used", text)
        plain = text.replace("\\", "")                          # markdown escapes are display-neutral
        self.assertIn("agrisense_001", plain)
        self.assertIn("sensor status: normal", plain)
        self.assertEqual(len(at.dataframe), 1)
        self.assertEqual(len(at.dataframe[0].value), 2)
        self.assertEqual(len(self.tab(at, "Sensor Monitoring").dataframe), 1)
        self.assertEqual(len(self.tab(at, "Overview").dataframe), 0)
        self.assertEqual(self.metric(at, "Latest sensor message").value[-3:], "UTC")

    def test_missing_values_are_not_replaced(self):
        self.add_sensor(stamp(datetime.now(UTC)), temp=None, hum=None, soil=None, status=None)
        at = self.run_app()
        self.assert_no_exception(at)
        self.assertEqual(self.metric(at, "Temperature").value, "n/a")
        self.assertEqual(self.metric(at, "Soil moisture").value, "n/a")
        self.assertIn("not reported", self.text_of(at))

    def test_invalid_readings_are_excluded_and_reported(self):
        self.add_sensor(stamp(datetime.now(UTC)), temp=99.0, valid=0)
        self.add_sensor(stamp(datetime.now(UTC) - timedelta(seconds=20)), temp=21.0)
        at = self.run_app()
        self.assertEqual(self.metric(at, "Temperature").value, "21.0 °C")
        self.assertIn("invalid (out of range): 1", self.text_of(at))

    def test_only_invalid_readings(self):
        self.add_sensor(stamp(datetime.now(UTC)), valid=0)
        at = self.run_app()
        self.assert_no_exception(at)
        self.assertIn("none passed validation", self.text_of(at))
        self.assertFalse([m for m in at.metric if m.label.startswith("Temperature")])

    def test_text_in_numeric_columns_and_bad_timestamps_do_not_crash(self):
        self.add_sensor("not-a-timestamp", temp="warm", hum=50.0, soil=10.0)
        self.add_sensor(stamp(datetime.now(UTC)), temp=21.0)
        at = self.run_app()
        self.assert_no_exception(at)
        self.assertIn("unparseable timestamp", self.text_of(at))

    def test_charts_are_drawn_for_each_signal(self):
        for i in range(5):
            self.add_sensor(stamp(datetime.now(UTC) - timedelta(seconds=10 * i)), temp=20 + i, soil=10 + i)
        at = self.run_app()
        self.assertEqual(len(at.get("vega_lite_chart")), 3)    # temperature, humidity, soil
        self.assertEqual(len(self.tab(at, "Sensor Monitoring").get("vega_lite_chart")), 3)
        self.assertEqual(len(self.tab(at, "Overview").get("vega_lite_chart")), 0)
        self.assertEqual(len(self.tab(at, "Weather").get("vega_lite_chart")), 0)


class AssessmentDisplayTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.make_db()

    def show(self, **overrides):
        self.add_assessment(assessment_date=overrides.pop("assessment_date", self.yesterday().isoformat()), **overrides)
        at = self.run_app()
        self.assert_no_exception(at)
        return at, self.text_of(at)

    def test_provisional_assessment_shows_all_fields_and_prominent_warnings(self):
        at, text = self.show()
        self.assertEqual(self.metric(at, "ET0").value, "2.97 mm/day")
        self.assertEqual(self.metric(at, "ETc").value, "2.08 mm/day")
        self.assertEqual(self.metric(at, "Root-zone depletion").value, "4.4 mm")
        self.assertEqual(self.metric(at, "TAW").value, "30.9 mm")
        self.assertEqual(self.metric(at, "RAW").value, "17.0 mm")
        self.assertEqual(self.metric(at, "Days after planting").value, "4")
        self.assertEqual(self.metric(at, "Crop").value, "winter_wheat")
        self.assertEqual(self.metric(at, "Growth stage").value, "initial")
        self.assertEqual(self.metric(at, "Net irrigation depth").value, "none recommended")
        banners = " ".join(w.value for w in at.warning)
        self.assertIn("PROVISIONAL", banners)
        self.assertIn("hypothetical placeholder parameters", banners)
        self.assertIn("SYNTHETIC placeholder", banners)            # each stored warning is a visible warning
        self.assertIn("Only 5 day(s) simulated", banners.replace("\\", ""))
        self.assertIn("Model assumptions", [s.value for s in at.subheader])
        self.assertIn("hypothetical placeholders", text)           # assumptions in the page, not a tooltip
        self.assertIn("Reasoning (as stored)", [s.value for s in at.subheader])
        self.assertEqual([t.label for t in at.tabs], list(app.TAB_LABELS))
        self.assertTrue(any("NO IRRIGATION" in s.value for s in at.success))
        for label in ("Overview", "Sensor Monitoring", "Weather", "GIS"):
            pane = self.tab(at, label)
            self.assertFalse(any("NO IRRIGATION" in str(e.value)
                                 for e in list(pane.success) + list(pane.error) + list(pane.warning)))
        model = self.tab(at, "Model & Evidence")
        self.assertIn("FAO-56 calculation", [s.value for s in model.subheader])
        self.assertIn("Reliability and model assumptions", [s.value for s in model.subheader])
        self.assertTrue(any(m.label.startswith("Root-zone depletion") for m in at.metric))
        self.assertFalse(any(m.label.startswith("Root-zone depletion") for m in model.metric))
        self.assertTrue(any(m.label.startswith("Crop") for m in model.metric))

    def test_ok_status(self):
        at, text = self.show(status="OK", warnings="[]", recommendation="NO_IRRIGATION")
        self.assertTrue(any("OK." in s.value for s in at.success))
        self.assertFalse(any("PROVISIONAL." in w.value for w in at.warning))
        self.assertIn("No warnings stored", text)
        self.assertEqual(self.metric(at, "Status").value, "OK")

    def test_insufficient_data_record_is_clearly_marked(self):
        at, text = self.show(status="INSUFFICIENT_DATA", recommendation="INSUFFICIENT_DATA", et0_mm=None,
                             kc=None, etc_mm=None, depletion_mm=None, taw_mm=None, raw_mm=None, warnings="[]")
        self.assertTrue(any("INSUFFICIENT_DATA" in e.value and "do not use" in e.value for e in at.error))
        self.assertEqual(self.metric(at, "ET0").value, "n/a")

    def test_irrigate_recommendation_shows_net_depth(self):
        at, _ = self.show(recommendation="IRRIGATE", irrigation_depth_mm=18.3, depletion_mm=20.1)
        self.assertTrue(any("IRRIGATE" in e.value and "18.3 mm" in e.value for e in at.error))
        self.assertEqual(self.metric(at, "Net irrigation depth").value, "18.3 mm")

    def test_monitor_and_no_irrigation_callouts(self):
        at, _ = self.show(recommendation="MONITOR")
        self.assertTrue(any("MONITOR" in w.value for w in at.warning))

    def test_incomplete_record_is_flagged_not_hidden(self):
        at, _ = self.show(et0_mm=None, raw_mm="oops")
        errors = " ".join(e.value for e in at.error)
        self.assertIn("ET0 is missing", errors)
        self.assertIn("RAW is not a valid number", errors.replace("\\", ""))
        self.assertEqual(self.metric(at, "RAW").value, "invalid")

    def test_old_assessment_is_not_presented_as_current(self):
        at, _ = self.show(assessment_date=(self.yesterday() - timedelta(days=6)).isoformat())
        banners = " ".join(w.value for w in at.warning)
        self.assertIn("is not presented as current", banners)
        self.assertIn("6 day(s) behind", banners)

    def test_hostile_database_text_is_displayed_literally(self):
        evil = "<script>alert(1)</script> **x** [l](http://e.com) `c` $z$ :smile:"
        at, _ = self.show(warnings=json.dumps([evil]), reasoning=evil)
        rendered = " ".join([w.value for w in at.warning] + [m.value for m in at.markdown])
        for raw in ("<script>", "**x**", "[l](http", "`c`", "$z$", ":smile:"):
            self.assertNotIn(raw, rendered)
        self.assertIn("alert", rendered)

    def test_malformed_warnings_json_does_not_crash(self):
        at, _ = self.show(warnings="{not json")
        self.assertTrue(any("not json" in w.value for w in at.warning))

    def test_no_matching_assessment_empty_state(self):
        self.add_assessment(field_id="field_999")
        at = self.run_app()
        self.assert_no_exception(at)
        self.assertIn("none for field", self.text_of(at))
        self.assertFalse([m for m in at.metric if m.label.startswith("ET0")])

    def test_unexpected_assessment_schema_is_an_error_not_a_crash(self):
        self.execute("DROP TABLE daily_assessments")
        self.execute("CREATE TABLE daily_assessments (id INTEGER, field_id TEXT)")
        at = self.run_app()
        self.assert_no_exception(at)
        self.assertTrue(any("missing expected column" in e.value for e in at.error))


class MapIntegrationTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.make_db()

    def rendered_map(self, folium_mock):
        self.assertEqual(folium_mock.call_count, 1)
        args, kwargs = folium_mock.call_args
        self.assertIsInstance(args[0], folium.Map)
        self.assertEqual(kwargs["returned_objects"], [])
        self.assertEqual(kwargs["height"], app.MAP_HEIGHT)
        return args[0].get_root().render()

    def test_map_reuses_the_generator_and_reflects_the_latest_assessment(self):
        self.add_assessment(assessment_date=self.yesterday().isoformat(), recommendation="MONITOR",
                            config_hash="old", created_at="2026-11-01 00:00:00")
        mock_map = mock.Mock()
        at = self.run_app(folium_mock=mock_map)
        self.assert_no_exception(at)
        page = self.rendered_map(mock_map)
        self.assertEqual(page.count("L.marker("), 1)
        self.assertIn(mg.MARKER_TITLE, page)
        self.assertIn("not a surveyed or physically monitored field", page)
        self.assertIn(self.yesterday().isoformat(), page)
        self.assertIn("MONITOR", page)
        self.assertNotIn("Not current", page)
        for forbidden in ("L.polygon", "L.polyline", "L.rectangle", "L.circle", "GeoJson", "L.imageOverlay"):
            self.assertNotIn(forbidden, page)
        self.assertIn(f"[{DEFAULT_CONFIG.study.latitude}, {DEFAULT_CONFIG.study.longitude}]", page)

    def test_stale_assessment_is_marked_not_current_on_the_map(self):
        old = (self.yesterday() - timedelta(days=5)).isoformat()
        self.add_assessment(assessment_date=old)
        mock_map = mock.Mock()
        self.run_app(folium_mock=mock_map)
        page = self.rendered_map(mock_map)
        self.assertIn("Not current", page)
        self.assertIn("5 day(s) behind", page)
        self.assertIn(old, page)

    def test_map_without_assessment_says_so_and_invents_nothing(self):
        mock_map = mock.Mock()
        self.run_app(folium_mock=mock_map)
        page = self.rendered_map(mock_map)
        self.assertEqual(page.count("L.marker("), 1)
        self.assertIn("Assessment unavailable", page)
        self.assertNotIn("ET0", page)

    def test_map_is_built_even_when_database_is_missing(self):
        self.db.unlink()
        mock_map = mock.Mock()
        at = self.run_app(folium_mock=mock_map)
        self.assert_no_exception(at)
        self.assertIn("Database file not found", self.rendered_map(mock_map))

    def test_standalone_generator_still_produces_the_same_kind_of_file(self):
        self.add_assessment()
        out = self.dir / "map.html"
        _, lookup = mg.generate(self.db, out)
        self.assertEqual(lookup.state, mg.FOUND)
        self.assertEqual(out.read_text(encoding="utf-8").count("L.marker("), 1)

    def test_disclaimer_is_visible_on_the_page(self):
        text = self.text_of(self.run_app())
        self.assertIn("not a real monitored farm", text)
        self.assertIn("not a surveyed field boundary or a physically monitored farm", text)


class RedesignHelperTests(DbTestCase):
    def rows(self, *stamps):
        return [{"timestamp": s} for s in stamps]

    def test_sensor_cadence_is_computed_from_stored_timestamps_only(self):
        t = datetime(2026, 11, 20, 10, 0, 0, tzinfo=UTC)
        out = app.sensor_cadence(self.rows(*[stamp(t + timedelta(seconds=5 * i)) for i in (4, 0, 1, 3, 2)]))
        self.assertEqual(out, {"count": 5, "span_s": 20.0, "median_gap_s": 5.0})
        odd = app.sensor_cadence(self.rows(stamp(t), stamp(t + timedelta(seconds=5)), stamp(t + timedelta(seconds=35))))
        self.assertEqual(odd["median_gap_s"], 17.5)

    def test_sensor_cadence_with_too_little_information(self):
        t = datetime(2026, 11, 20, 10, 0, 0, tzinfo=UTC)
        self.assertEqual(app.sensor_cadence([])["median_gap_s"], None)
        self.assertEqual(app.sensor_cadence(self.rows(stamp(t)))["median_gap_s"], None)
        self.assertEqual(app.sensor_cadence(self.rows(stamp(t), "garbage", None))["count"], 1)

    def test_weather_history_frame_leaves_missing_days_as_gaps(self):
        history = [{"date": "2026-11-10", "temperature_max_c": 25.0, "temperature_min_c": 12.0,
                    "precipitation_sum_mm": 0.0},
                   {"date": "2026-11-13", "temperature_max_c": 27.0, "temperature_min_c": 14.0,
                    "precipitation_sum_mm": 1.5}]
        frame = app.weather_history_frame(history)
        self.assertEqual(len(frame), 4)                                   # 10, 11, 12, 13
        self.assertTrue(frame.loc["2026-11-11", ["temperature_max_c", "temperature_min_c",
                                                 "precipitation_sum_mm"]].isna().all())
        self.assertEqual(frame.loc["2026-11-13", "temperature_max_c"], 27.0)
        self.assertTrue(app.weather_history_frame([]).empty)

    def test_observed_domain_pads_the_range_and_does_not_force_zero(self):
        self.assertEqual(app._observed_domain([18.0, 22.0], pad_frac=0.15, min_pad=0.5), [17.4, 22.6])
        self.assertIsNone(app._observed_domain([]))
        self.assertIsNone(app._observed_domain(["warm", None]))
        self.assertEqual(app._observed_domain([20.0, 20.0], min_pad=0.5), [19.5, 20.5])

    def test_weather_history_frame_survives_text_and_bad_dates(self):
        frame = app.weather_history_frame([{"date": "bad", "temperature_max_c": 1, "temperature_min_c": 1,
                                            "precipitation_sum_mm": 1},
                                           {"date": "2026-11-10", "temperature_max_c": "warm",
                                            "temperature_min_c": 12.0, "precipitation_sum_mm": None}])
        self.assertEqual(len(frame), 1)
        self.assertTrue(frame["temperature_max_c"].isna().all())
        self.assertEqual(frame["temperature_min_c"].iloc[0], 12.0)

    def test_history_is_cairo_days_only_oldest_first_and_limited(self):
        self.make_db()
        end = date(2026, 11, 19)
        for i in range(20):
            self.add_weather(end - timedelta(days=i))
        self.add_weather(date(2026, 11, 20))                  # NOW is 2026-11-20 10:00 UTC: that day is not complete
        history = self.snapshot().weather.history
        dates = [r["date"] for r in history]
        self.assertEqual(len(dates), app.DAILY_HISTORY_DAYS)
        self.assertEqual(dates, sorted(dates))
        self.assertEqual(dates[-1], "2026-11-19")
        self.assertNotIn("2026-11-20", dates)

    def test_legacy_rows_never_enter_the_history(self):
        self.make_db()
        self.add_weather(date(2026, 11, 18))
        self.add_weather(date(2026, 11, 19), source="open-meteo")
        self.assertEqual([r["date"] for r in self.snapshot().weather.history], ["2026-11-18"])

    def test_coverage_counts_cairo_tagged_days_and_lists_missing_ones(self):
        self.make_db()
        for day in (15, 16, 18, 19):
            self.add_weather(date(2026, 11, day))
        self.add_weather(date(2026, 11, 17), source="open-meteo")            # legacy tag: does not count
        self.add_assessment(assessment_date="2026-11-19", simulation_start_date="2026-11-15")
        cov = self.snapshot().coverage
        self.assertEqual((cov.expected, cov.present), (5, 4))
        self.assertEqual(cov.missing, ("2026-11-17",))
        self.assertEqual((cov.first_day, cov.last_day), (date(2026, 11, 15), date(2026, 11, 19)))

    def test_coverage_is_none_without_an_assessment_or_with_unusable_dates(self):
        self.make_db()
        self.assertIsNone(self.snapshot().coverage)
        self.add_assessment(assessment_date="2026-11-19", simulation_start_date="not-a-date")
        self.assertIsNone(self.snapshot().coverage)

    def test_coverage_is_none_when_the_weather_table_is_missing(self):
        self.make_db(weather=False)
        self.add_assessment(assessment_date="2026-11-19", simulation_start_date="2026-11-15")
        self.assertIsNone(self.snapshot().coverage)

    def test_active_theme(self):
        from types import SimpleNamespace

        def theme(base, reported):
            with mock.patch.object(st, "get_option", return_value=base), \
                    mock.patch.object(st, "context", SimpleNamespace(theme=SimpleNamespace(type=reported))):
                return app.active_theme()

        # A configured theme wins: the browser reports its OS preference ('light') even for a dark-configured page.
        self.assertEqual(theme("dark", "light"), "dark")
        self.assertEqual(theme("light", "dark"), "light")
        # No configured theme: fall back to what the browser reports.
        self.assertEqual(theme(None, "dark"), "dark")
        self.assertEqual(theme(None, "light"), "light")
        self.assertEqual(theme(None, None), "light")

    def test_project_streamlit_config_is_dark_by_default(self):
        import tomllib
        config = tomllib.loads((app.PROJECT_ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8"))
        theme = config["theme"]
        self.assertEqual(theme["base"], "dark")
        self.assertEqual(theme["backgroundColor"], "#101418")
        self.assertEqual(theme["secondaryBackgroundColor"], "#191F25")
        self.assertEqual(theme["textColor"], "#E8EDF0")
        self.assertEqual(theme["borderColor"], "#303A43")
        self.assertEqual(theme["primaryColor"], "#78B88A")
        self.assertEqual(theme["blueColor"], "#39C6D8")
        self.assertEqual(theme["yellowColor"], "#E6AD55")
        self.assertEqual(theme["redColor"], "#E47777")

    def test_responsive_css_is_static_and_covers_metrics_and_narrow_screens(self):
        self.assertIn("stMetricValue", app.RESPONSIVE_CSS)
        self.assertIn('[data-testid="stTab"]', app.RESPONSIVE_CSS)
        self.assertIn("st-key-agri_water_metrics", app.RESPONSIVE_CSS)
        self.assertIn("@media (max-width: 640px)", app.RESPONSIVE_CSS)
        self.assertTrue(app.RESPONSIVE_CSS.startswith("<style>") and app.RESPONSIVE_CSS.endswith("</style>"))

    def test_every_provenance_label_has_a_colour_and_they_differ(self):
        self.assertEqual(set(app.LANE_COLORS), {"SIMULATED", "EXTERNAL", "CALCULATED", "ASSUMED"})
        self.assertEqual(len(set(app.LANE_COLORS.values())), 4)


class RedesignDisplayTests(AppTestCase):
    FORBIDDEN = ("confidence", "certainty", "probab", "likelihood", "reliability score", "accuracy")

    def setUp(self):
        super().setUp()
        self.make_db()

    def everything(self, at):
        return (self.text_of(at) + " " + " ".join(str(h.value) for h in list(at.header) + list(at.subheader))).lower()

    def seed_weather(self, days=5):
        end = self.yesterday()
        for i in range(days):
            self.add_weather(end - timedelta(days=i))

    def test_five_tabs_keep_the_recommendation_visible_above_them(self):
        self.seed_weather(5)
        self.add_sensor(stamp(datetime.now(UTC) - timedelta(seconds=10)))
        self.add_assessment(assessment_date=self.yesterday().isoformat())
        at = self.run_app()
        self.assert_no_exception(at)
        self.assertEqual(list(app.TAB_LABELS),
                         ["Overview", "Sensor Monitoring", "Weather", "GIS", "Model & Evidence"])
        self.assertEqual([t.label for t in at.tabs], list(app.TAB_LABELS))
        self.assertTrue(any("NO IRRIGATION" in s.value for s in at.success))
        self.assertFalse(any(getattr(b, "key", None) == "run_assessment"
                             for t in at.tabs for b in t.button))
        self.assertIn("SIMULATED DATA", self.tab_text(self.tab(at, "Sensor Monitoring")))
        self.assertIn("Open-Meteo model data", self.tab_text(self.tab(at, "Weather")))
        self.assertIn("FAO-56 calculation", [s.value for s in self.tab(at, "Model & Evidence").subheader])
        self.assertEqual(len(self.tab(at, "Overview").get("vega_lite_chart")), 0)
        self.assertGreaterEqual(len(self.tab(at, "Sensor Monitoring").get("vega_lite_chart")), 1)
        self.assertGreaterEqual(len(self.tab(at, "Weather").get("vega_lite_chart")), 1)

    def test_three_provenance_lanes_with_their_own_cadence(self):
        at = self.run_app()
        self.assert_no_exception(at)
        badges = [m.value for m in at.markdown if "-badge[" in str(m.value)]
        for label, color in (("SIMULATED", "orange"), ("EXTERNAL", "blue"), ("CALCULATED", "green")):
            self.assertIn(f":{color}-badge[{label}]", badges)       # colour = provenance, consistently
        text = self.text_of(at)
        self.assertIn("about every 5 s", text)
        self.assertIn("at most once a day", text)
        self.assertIn("only when a new assessment is run", text.replace("\n", " "))
        self.assertIn("never merged", text)

    def test_sensor_section_states_the_observed_cadence(self):
        base = datetime.now(UTC) - timedelta(minutes=1)
        for i in range(6):
            self.add_sensor(stamp(base - timedelta(seconds=5 * i)))
        text = self.text_of(self.run_app())
        self.assertIn("median gap between consecutive readings is 5 s", text)
        self.assertIn("not a trend", text)

    def test_single_sensor_reading_does_not_claim_a_cadence(self):
        self.add_sensor(stamp(datetime.now(UTC) - timedelta(seconds=10)))
        text = self.text_of(self.run_app())
        self.assertIn("spacing between readings cannot be determined", text)

    def test_daily_weather_history_is_a_separate_section_with_its_own_charts_and_table(self):
        self.seed_weather(5)
        for i in range(4):
            self.add_sensor(stamp(datetime.now(UTC) - timedelta(seconds=5 * i)))
        at = self.run_app()
        self.assert_no_exception(at)
        self.assertEqual(len(at.get("vega_lite_chart")), 3 + 2)           # 3 sensor charts + temperature + rain
        self.assertEqual(len(self.tab(at, "Sensor Monitoring").get("vega_lite_chart")), 3)
        self.assertEqual(len(self.tab(at, "Weather").get("vega_lite_chart")), 2)
        self.assertEqual(len(self.tab(at, "Overview").get("vega_lite_chart")), 0)
        self.assertEqual(len(self.tab(at, "GIS").get("vega_lite_chart")), 0)
        self.assertEqual(len(at.dataframe), 2)                             # sensor table + daily weather table
        self.assertEqual(len(self.tab(at, "Sensor Monitoring").dataframe), 1)
        self.assertEqual(len(self.tab(at, "Weather").dataframe), 1)
        daily = at.dataframe[1].value
        self.assertEqual(len(daily), 5)
        self.assertIn("Open-Meteo ET0 (mm/day, comparison only)", list(daily.columns))
        text = self.text_of(at)
        self.assertIn("never on the seconds-level sensor axis", text.replace("\n", " "))
        self.assertIn("Days without a record are left as gaps", text)

    def test_zero_rain_is_stated_as_a_dry_period_not_missing_data(self):
        self.seed_weather(3)                                               # fixture rain is 0.0 mm
        at = self.run_app()
        self.assert_no_exception(at)
        self.assertIn("dry period, not missing data", self.text_of(at))
        self.assertEqual(len(at.get("vega_lite_chart")), 2)                # chart is still drawn
        self.assertEqual(len(self.tab(at, "Weather").get("vega_lite_chart")), 2)
        self.assertEqual(len(self.tab(at, "Overview").get("vega_lite_chart")), 0)

    def test_daily_weather_gaps_are_reported_not_filled(self):
        end = self.yesterday()
        self.add_weather(end)
        self.add_weather(end - timedelta(days=3))
        at = self.run_app()
        self.assert_no_exception(at)
        self.assertIn("Showing 2 record(s) across 4 calendar day(s)", self.text_of(at))
        self.assertEqual(len(at.dataframe[0].value), 2)                    # table lists only stored days

    def test_daily_weather_empty_and_missing_states(self):
        self.assertIn("No daily weather to show", self.text_of(self.run_app()))
        self.db.unlink()
        self.make_db(weather=False)
        at = self.run_app()
        self.assert_no_exception(at)
        self.assertIn("No daily weather to show", self.text_of(at))

    def test_only_legacy_weather_gets_the_no_completed_day_message_in_the_weather_section(self):
        self.add_weather(self.yesterday(), source="open-meteo")
        at = self.run_app()
        self.assert_no_exception(at)
        self.assertEqual(len(at.get("vega_lite_chart")), 0)
        self.assertIn("No completed", self.text_of(at))

    def test_reliability_evidence_uses_recorded_facts_only(self):
        end = self.yesterday()
        start = end - timedelta(days=4)
        for i in (0, 1, 3, 4):                                             # one day missing
            self.add_weather(start + timedelta(days=i))
        self.add_assessment(assessment_date=end.isoformat(), simulation_start_date=start.isoformat())
        at = self.run_app()
        self.assert_no_exception(at)
        self.assertEqual(self.metric(at, "Warnings recorded").value, "2")
        self.assertEqual(self.metric(at, "Weather coverage").value, "4 of 5 days")
        text = self.text_of(at)
        self.assertIn((start + timedelta(days=2)).isoformat(), text)       # the missing day is named
        self.assertIn("Missing:", text)
        self.assertEqual(self.metric(at, "Model assumptions").value, str(len(app.ASSUMPTION_NOTES)))
        self.assertIn("Freshness - assessment (CALCULATED)", text)
        self.assertIn("no score", text)

    def test_reliability_coverage_complete_and_unavailable(self):
        end = self.yesterday()
        start = end - timedelta(days=2)
        for i in range(3):
            self.add_weather(start + timedelta(days=i))
        self.add_assessment(assessment_date=end.isoformat(), simulation_start_date=start.isoformat())
        at = self.run_app()
        self.assertEqual(self.metric(at, "Weather coverage").value, "3 of 3 days")
        self.assertIn("Every day present", self.text_of(at))

        self.db.unlink()
        self.make_db()
        self.add_assessment(assessment_date=end.isoformat(), simulation_start_date="not-a-date")
        at = self.run_app()
        self.assert_no_exception(at)
        self.assertEqual(self.metric(at, "Weather coverage").value, "not available")

    def test_reliability_panel_is_absent_without_an_assessment(self):
        at = self.run_app()
        self.assertFalse([m for m in at.metric if m.label.startswith("Weather coverage")])
        self.assertNotIn("reliability evidence", self.everything(at))
        self.assertNotIn("FAO-56 calculation", [s.value for s in at.subheader])

    def test_no_invented_confidence_or_certainty_wording_anywhere(self):
        end = self.yesterday()
        self.seed_weather(5)
        self.add_sensor(stamp(datetime.now(UTC) - timedelta(seconds=10)))
        for status, warnings in (("PROVISIONAL", json.dumps(["w1"])), ("OK", "[]")):
            self.add_assessment(assessment_date=end.isoformat(), status=status, warnings=warnings,
                                simulation_start_date=(end - timedelta(days=4)).isoformat())
            everything = self.everything(self.run_app())
            for word in self.FORBIDDEN:
                self.assertNotIn(word, everything, word)
            self.execute("DELETE FROM daily_assessments")

    def test_map_is_dark_by_default_with_osm_tiles_and_one_marker(self):
        mock_map = mock.Mock()
        real_get_option = st.get_option
        # AppTest does not read the project's .streamlit/config.toml (tested separately): supply its theme.
        with mock.patch.object(st, "get_option",
                               side_effect=lambda key: "dark" if key == "theme.base" else real_get_option(key)):
            at = self.run_app(folium_mock=mock_map)
        self.assert_no_exception(at)
        page = mock_map.call_args[0][0].get_root().render()
        self.assertNotIn("filter:invert", page)                         # tiles stay readable OSM, not inverted
        self.assertNotIn(".leaflet-tile-pane{filter", page)
        self.assertIn(".leaflet-popup-content-wrapper", page)            # dark overlay chrome only
        self.assertNotIn(".openPopup()", page)                          # popup stays closed until clicked
        self.assertIn("tile.openstreetmap.org", page)
        self.assertIn("openstreetmap.org/copyright", page)
        self.assertNotIn("cartocdn", page)                               # no key-gated third-party tiles
        self.assertEqual(page.count("L.marker("), 1)
        self.assertIn("OpenStreetMap contributors", self.text_of(at))
        self.assertIn("OpenStreetMap contributors", self.tab_text(self.tab(at, "GIS")))
        self.assertEqual(mock_map.call_args.kwargs["height"], app.MAP_HEIGHT)

    def test_sidebar_explains_all_four_provenance_labels(self):
        at = self.run_app()
        side = " ".join(m.value for m in at.sidebar.markdown)
        for label in ("SIMULATED", "EXTERNAL", "CALCULATED", "ASSUMED"):
            self.assertIn(label, side)


class RunButtonTests(AppTestCase):
    def wait_for_result(self, at, timeout=90):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            at.run()
            if any("Last assessment run" in m.value for m in at.markdown):
                return at
            time.sleep(0.5)
        self.fail("the assessment run never finished")

    def test_click_runs_the_assessment_module_and_reports_insufficient_data(self):
        self.make_db(assessments=False)                       # no weather -> honest INSUFFICIENT_DATA
        at = self.run_app()
        self.assertFalse(at.button(key="run_assessment").disabled)
        before = file_hash(REAL_DB)
        at.button(key="run_assessment").click().run()
        self.assert_no_exception(at)
        at = self.wait_for_result(at)
        self.assert_no_exception(at)
        text = self.text_of(at)
        self.assertIn("INSUFFICIENT DATA", text)
        self.assertIn("nothing was saved", text)
        self.assertEqual(self.metric(at, "Return code").value, "0")
        self.assertEqual(self.metric(at, "Outcome").value, "INSUFFICIENT_DATA")
        codes = [c.value for c in at.code]
        self.assertTrue(any("[NOT SAVED]" in c for c in codes), codes)
        self.assertIn("(empty)", codes)                       # empty stderr is shown as such
        self.assertIn("Command:", text)
        self.assertIn("agrisense.assessment", text)
        self.assertIn(str(self.db.resolve()), text)           # the temp DB, not the real one
        self.assertEqual(file_hash(REAL_DB), before)
        self.assertFalse(at.button(key="run_assessment").disabled)    # usable again afterwards
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM daily_assessments").fetchone()[0], 0)
        conn.close()

    def render_result(self, result_expr):
        script = ("import sys\n"
                  f"sys.path.insert(0, r'{Path(app.__file__).resolve().parent}')\n"
                  "import app\n"
                  f"app.render_run_result({result_expr})\n")
        at = AppTest.from_string(script, default_timeout=30).run()
        self.assert_no_exception(at)
        return at

    def test_failed_results_are_explained_with_code_and_separate_outputs(self):
        cases = (
            ("RunResult('ERROR', 2, 'out text', 'Traceback boom', ('py', '-m', 'x'), None, 1.5)", "2", "exit code 2"),
            ("RunResult('DB_ERROR', 1, '[DB ERROR] nope', '', ('py',), None, 0.2)", "1", "exit code 1"),
            ("RunResult('FAILED', 9, '', 'crash', ('py',), None, 0.2)", "9", "unexpected exit code"),
            ("RunResult('TIMEOUT', None, '', '', ('py',), None, 120.0, 'Stopped after 120 s.')", "none", "did not finish"),
            ("RunResult('LAUNCH_ERROR', None, '', '', ('py',), None, 0.0, 'cannot start')", "none", "could not be started"),
        )
        for expr, code, explanation in cases:
            with self.subTest(expr[:30]):
                at = self.render_result("app." + expr)
                self.assertEqual(self.metric(at, "Return code").value, code)
                self.assertTrue(any(explanation in e.value for e in at.error), [e.value for e in at.error])
                self.assertEqual(len(at.code), 2)             # stdout and stderr are shown separately

    def test_outputs_are_shown_verbatim_and_separately(self):
        at = self.render_result("app.RunResult('ERROR', 2, 'out text', 'Traceback boom', ('py',), None, 1.5)")
        self.assertEqual([c.value for c in at.code], ["out text", "Traceback boom"])

    def test_backend_reason_is_surfaced_for_insufficient_data(self):
        self.assertEqual(app.backend_reason("x\nReasoning        : 2026-11-14 is before the start.\ny"),
                         "2026-11-14 is before the start.")
        self.assertIsNone(app.backend_reason("no such line"))
        self.assertIsNone(app.backend_reason(None))
        at = self.render_result("app.RunResult('INSUFFICIENT_DATA', 0, 'Reasoning        : weather is **missing**', "
                                "'', ('py',), None, 0.5)")
        text = " ".join(m.value for m in at.markdown)
        self.assertIn("Reason reported by the assessment module", text)
        self.assertNotIn("**missing**", text.split("module:**")[1])          # DB/backend text is escaped

    def test_insufficient_data_result_is_a_warning_not_a_success(self):
        at = self.render_result("app.RunResult('INSUFFICIENT_DATA', 0, '[NOT SAVED]', '', ('py',), None, 0.5)")
        self.assertTrue(any("INSUFFICIENT DATA" in w.value for w in at.warning))
        self.assertFalse(at.success)


if __name__ == "__main__":
    unittest.main()
