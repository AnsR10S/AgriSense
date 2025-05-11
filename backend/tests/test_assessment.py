"""Tests for the irrigation decision rules, the assessment pipeline and its database table.

All weather rows below are SYNTHETIC TEST DATA created inside temporary
databases. The real agrisense.db is never opened by these tests.
"""

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import weather_daily
from agrisense import assessment as asm
from agrisense import et0, irrigation
from agrisense.config import (AppConfig, DAILY_WEATHER_API_SOURCE, SoilConfig, StudyConfig,
                              config_fingerprint)

START = date(2026, 11, 13)
TODAY = date(2026, 11, 18)          # pretend Africa/Cairo local date
LAST_DAY = date(2026, 11, 17)       # last completed day -> 5 simulated days (13..17)
CONFIG = AppConfig()
# Same schema as ingest.py (copied so these tests do not need paho-mqtt installed).
SENSOR_READINGS_SQL = """
CREATE TABLE IF NOT EXISTS sensor_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    field_id TEXT NOT NULL,
    timestamp DATETIME NOT NULL,
    temperature_c REAL,
    humidity_pct REAL,
    soil_moisture_pct REAL,
    is_simulation BOOLEAN NOT NULL DEFAULT 1,
    sensor_status TEXT,
    is_valid BOOLEAN DEFAULT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(device_id, field_id, timestamp)
);
"""
INSERT_WEATHER = (
    "INSERT INTO daily_weather_observations (field_id, date, temperature_max_c, temperature_min_c, "
    "humidity_max_pct, humidity_min_pct, surface_pressure_mean_hpa, wind_speed_10m_mean_m_s, "
    "solar_radiation_sum_mj_m2, precipitation_sum_mm, openmeteo_et0_mm, api_source) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def weather_row(day, **overrides):
    values = dict(field_id="field_001", date=day.isoformat(), tmax=26.0, tmin=14.0, rhmax=95.0, rhmin=42.0,
                  pressure=999.5, wind=2.5, rs=14.0, precip=0.0, om_et0=3.0, source=DAILY_WEATHER_API_SOURCE)
    values.update(overrides)
    return (values["field_id"], values["date"], values["tmax"], values["tmin"], values["rhmax"],
            values["rhmin"], values["pressure"], values["wind"], values["rs"], values["precip"],
            values["om_et0"], values["source"])


class DbTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "test.db"
        with contextlib.redirect_stdout(io.StringIO()):
            weather = weather_daily.init_db(self.db_path)    # real daily weather schema
        weather.execute(SENSOR_READINGS_SQL)
        weather.commit()
        weather.close()
        self.conn = asm.init_db(self.db_path)
        self.addCleanup(self.conn.close)

    def add_weather(self, first=START, last=LAST_DAY, **overrides):
        day = first
        while day <= last:
            self.conn.execute(INSERT_WEATHER, weather_row(day, **overrides))
            day += timedelta(days=1)
        self.conn.commit()

    def assess(self, config=CONFIG, day=LAST_DAY, events=None, today=TODAY):
        return asm.assess(self.conn, config, day, events, today=today)

    def count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class IrrigationDecisionTests(unittest.TestCase):
    def test_irrigate_when_depletion_reaches_raw(self):
        d = irrigation.decide(depletion_mm=40.0, raw_mm=40.0, taw_mm=80.0, etc_mm=3.0)
        self.assertEqual(d.recommendation, irrigation.IRRIGATE)
        self.assertEqual(d.net_depth_mm, 40.0)
        self.assertIn("RAW", d.reasoning)

    def test_irrigation_depth_equals_depletion_rounded(self):
        d = irrigation.decide(52.34, 40.0, 80.0, 3.0)
        self.assertEqual(d.net_depth_mm, 52.3)

    def test_monitor_when_raw_would_be_reached_within_a_day(self):
        d = irrigation.decide(depletion_mm=38.0, raw_mm=40.0, taw_mm=80.0, etc_mm=3.0)
        self.assertEqual(d.recommendation, irrigation.MONITOR)
        self.assertIsNone(d.net_depth_mm)

    def test_no_irrigation_when_well_below_raw(self):
        d = irrigation.decide(depletion_mm=10.0, raw_mm=40.0, taw_mm=80.0, etc_mm=3.0)
        self.assertEqual(d.recommendation, irrigation.NO_IRRIGATION)
        self.assertIsNone(d.net_depth_mm)
        self.assertIn("10.0 days", d.reasoning)

    def test_no_irrigation_with_zero_etc(self):
        d = irrigation.decide(10.0, 40.0, 80.0, 0.0)
        self.assertEqual(d.recommendation, irrigation.NO_IRRIGATION)

    def test_insufficient_data_decision_has_no_depth_and_explains(self):
        d = irrigation.insufficient_data("weather missing")
        self.assertEqual(d.recommendation, irrigation.INSUFFICIENT_DATA)
        self.assertIsNone(d.net_depth_mm)
        self.assertIn("weather missing", d.reasoning)

    def test_reasoning_always_states_the_numbers(self):
        for args in ((50, 40, 80, 3), (38, 40, 80, 3), (5, 40, 80, 3)):
            text = irrigation.decide(*args).reasoning
            self.assertIn("RAW = 40.0 mm", text)
            self.assertIn("TAW = 80.0 mm", text)


class AssessmentWithCompleteDataTests(DbTestCase):
    def test_complete_data_gives_a_populated_provisional_assessment(self):
        self.add_weather()
        a = self.assess()
        self.assertEqual(a.field_id, "field_001")
        self.assertEqual(a.assessment_date, LAST_DAY)
        self.assertEqual(a.crop_id, "winter_wheat")
        self.assertEqual(a.growth_stage, "initial")
        self.assertEqual(a.days_after_planting, 4)
        self.assertEqual(a.status, asm.STATUS_PROVISIONAL)     # hypothetical defaults -> warnings
        self.assertIn(a.recommendation, (irrigation.NO_IRRIGATION, irrigation.MONITOR, irrigation.IRRIGATE))
        for value in (a.et0_mm, a.kc, a.etc_mm, a.taw_mm, a.raw_mm, a.depletion_mm, a.root_depth_m):
            self.assertIsNotNone(value)
        self.assertAlmostEqual(a.etc_mm, a.kc * a.et0_mm)
        self.assertAlmostEqual(a.raw_mm, 0.55 * a.taw_mm)
        self.assertTrue(a.warnings)
        self.assertIn("calendar-based", a.reasoning)

    def test_et0_uses_converted_units_and_matches_independent_calculation(self):
        self.add_weather()
        a = self.assess()
        expected = et0.calculate_et0(
            tmax_c=26.0, tmin_c=14.0, rh_max_pct=95.0, rh_min_pct=42.0,
            pressure_kpa=99.95,                       # 999.5 hPa / 10
            wind_speed_m_s=2.5, solar_radiation_mj_m2_day=14.0,
            latitude_deg=29.97, elevation_m=193.0, day=LAST_DAY, wind_height_m=10.0).et0_mm_day
        self.assertAlmostEqual(a.et0_mm, expected, places=9)

    def test_openmeteo_et0_is_never_used_as_an_input(self):
        self.add_weather(om_et0=3.0)
        baseline = self.assess()
        self.conn.execute("UPDATE daily_weather_observations SET openmeteo_et0_mm = 99.0")
        self.conn.commit()
        changed = self.assess()
        self.assertEqual(baseline.et0_mm, changed.et0_mm)
        self.assertEqual(baseline.depletion_mm, changed.depletion_mm)
        self.assertEqual(changed.openmeteo_et0_mm, 99.0)         # kept for comparison only
        self.assertIn("comparison only", changed.reasoning)

    def test_simulated_potentiometer_reading_is_not_an_input(self):
        self.add_weather()
        low = self.assess()
        self.conn.executemany(
            "INSERT INTO sensor_readings (device_id, field_id, timestamp, soil_moisture_pct, is_valid) "
            "VALUES ('dev', 'field_001', ?, ?, 1)",
            [("2026-11-17T10:00:00Z", 2.0), ("2026-11-17T11:00:00Z", 98.0)])
        self.conn.commit()
        high = self.assess()
        self.assertEqual((low.et0_mm, low.depletion_mm, low.recommendation),
                         (high.et0_mm, high.depletion_mm, high.recommendation))
        self.assertEqual(asm.latest_simulated_soil_moisture(self.conn, "field_001")["soil_moisture_pct"], 98.0)

    def test_latest_soil_moisture_handles_missing_data(self):
        self.assertIsNone(asm.latest_simulated_soil_moisture(self.conn, "field_001"))
        self.conn.execute("DROP TABLE sensor_readings")
        self.assertIsNone(asm.latest_simulated_soil_moisture(self.conn, "field_001"))

    def test_default_assessment_date_is_the_previous_local_day(self):
        self.add_weather()
        a = asm.assess(self.conn, CONFIG, None, None, today=TODAY)
        self.assertEqual(a.assessment_date, TODAY - timedelta(days=1))

    def test_explicit_irrigation_changes_depletion(self):
        self.add_weather()
        dry = self.assess()
        wet = self.assess(events={date(2026, 11, 16): 3.0})
        self.assertLess(wet.depletion_mm, dry.depletion_mm)
        self.assertNotEqual(config_fingerprint(CONFIG), config_fingerprint(CONFIG, {date(2026, 11, 16): 3.0}))

    def test_irrigation_outside_the_simulated_period_is_ignored_with_warning(self):
        self.add_weather()
        a = self.assess(events={date(2026, 12, 25): 10.0})
        self.assertTrue(any("outside the simulated period" in w for w in a.warnings))

    def test_status_ok_only_without_warnings(self):
        verified = replace(CONFIG, study=replace(CONFIG.study, parameters_are_assumptions=False))
        self.add_weather(last=date(2026, 11, 19))
        a = self.assess(config=verified, day=date(2026, 11, 19), today=date(2026, 11, 20))
        self.assertEqual(a.warnings, [])
        self.assertEqual(a.status, asm.STATUS_OK)

    def test_irrigate_recommendation_includes_net_depth_equal_to_depletion(self):
        thin = replace(CONFIG, soil=SoilConfig(field_capacity_m3_m3=0.20, wilting_point_m3_m3=0.15))
        self.add_weather()
        a = self.assess(config=thin)
        self.assertEqual(a.recommendation, irrigation.IRRIGATE)
        self.assertEqual(a.irrigation_depth_mm, round(a.depletion_mm, 1))
        self.assertGreaterEqual(a.depletion_mm, a.raw_mm)

    def test_rain_resets_depletion_in_the_assessment(self):
        self.add_weather()
        self.conn.execute("UPDATE daily_weather_observations SET precipitation_sum_mm = 30.0 WHERE date = ?",
                          (LAST_DAY.isoformat(),))
        self.conn.commit()
        a = self.assess()
        self.assertEqual(a.depletion_mm, 0.0)
        self.assertEqual(a.recommendation, irrigation.NO_IRRIGATION)

    def test_warning_when_simulation_starts_after_planting(self):
        study = replace(CONFIG.study, simulation_start_date=date(2026, 11, 15))
        later = replace(CONFIG, study=study)
        self.add_weather(first=date(2026, 11, 15))
        a = self.assess(config=later)
        self.assertTrue(any("starts 2 day(s) after planting" in w for w in a.warnings))

    def test_warning_for_short_simulation(self):
        self.add_weather()
        a = self.assess()
        self.assertTrue(any("Only 5 day(s) simulated" in w for w in a.warnings))


class InsufficientDataTests(DbTestCase):
    def assert_insufficient(self, a, *fragments):
        self.assertEqual(a.status, asm.STATUS_INSUFFICIENT)
        self.assertEqual(a.recommendation, irrigation.INSUFFICIENT_DATA)
        self.assertIsNone(a.irrigation_depth_mm)
        self.assertIsNone(a.depletion_mm)
        self.assertTrue(a.reasoning.startswith("No defensible recommendation"))
        for fragment in fragments:
            self.assertIn(fragment, a.reasoning)

    def test_empty_weather_table(self):
        self.assert_insufficient(self.assess(), "2026-11-13: no Africa/Cairo daily weather record")

    def test_gap_in_the_weather_history(self):
        self.add_weather()
        self.conn.execute("DELETE FROM daily_weather_observations WHERE date = '2026-11-15'")
        self.conn.commit()
        self.assert_insufficient(self.assess(), "2026-11-15: no Africa/Cairo", "never invented or back-filled")

    def test_first_available_record_is_not_treated_as_the_start_date(self):
        self.add_weather(first=date(2026, 11, 15))            # data begin 2 days after the configured start
        a = self.assess()
        self.assert_insufficient(a, "2026-11-13", "2026-11-14", "2 problem day(s)")

    def test_missing_variable_blocks_the_recommendation(self):
        self.add_weather()
        self.conn.execute("UPDATE daily_weather_observations SET surface_pressure_mean_hpa = NULL "
                          "WHERE date = '2026-11-16'")
        self.conn.commit()
        self.assert_insufficient(self.assess(), "2026-11-16", "pressure is missing")

    def test_missing_precipitation_is_not_assumed_to_be_zero(self):
        self.add_weather()
        self.conn.execute("UPDATE daily_weather_observations SET precipitation_sum_mm = NULL WHERE date = '2026-11-14'")
        self.conn.commit()
        self.assert_insufficient(self.assess(), "2026-11-14", "precipitation is missing")

    def test_invalid_values_block_the_recommendation(self):
        cases = {
            "humidity": ("humidity_max_pct", 150.0, "RHmax"),
            "temperature": ("temperature_max_c", 99.0, "Tmax"),
            "pressure_in_wrong_unit": ("surface_pressure_mean_hpa", 99.9, "pressure"),
            "radiation": ("solar_radiation_sum_mj_m2", 400.0, "solar radiation"),
            "wind": ("wind_speed_10m_mean_m_s", -3.0, "wind speed"),
            "rain": ("precipitation_sum_mm", -1.0, "precipitation"),
        }
        for label, (column, value, fragment) in cases.items():
            with self.subTest(label=label):
                self.conn.execute("DELETE FROM daily_weather_observations")
                self.add_weather()
                self.conn.execute(f"UPDATE daily_weather_observations SET {column} = ? WHERE date = '2026-11-15'",
                                  (value,))
                self.conn.commit()
                self.assert_insufficient(self.assess(), "2026-11-15", fragment)

    def test_non_finite_stored_value_is_rejected(self):
        self.add_weather()
        self.conn.execute("UPDATE daily_weather_observations SET wind_speed_10m_mean_m_s = ? WHERE date = '2026-11-15'",
                          (float("inf"),))
        self.conn.commit()
        self.assert_insufficient(self.assess(), "2026-11-15", "wind speed")

    def test_legacy_utc_rows_are_excluded_and_reported(self):
        self.add_weather(source="open-meteo")                 # legacy tag = UTC-day aggregation
        self.assert_insufficient(self.assess(), "legacy UTC-day", "5 stored row(s)")

    def test_a_day_that_is_not_complete_cannot_be_assessed(self):
        self.add_weather(last=TODAY)
        self.assert_insufficient(self.assess(day=TODAY), "not a completed day", "Africa/Cairo")
        self.assert_insufficient(self.assess(day=TODAY + timedelta(days=3)), "not a completed day")

    def test_date_before_the_simulation_start(self):
        self.assert_insufficient(self.assess(day=date(2026, 11, 12)), "before the water-balance simulation start")

    def test_date_beyond_the_modelled_season(self):
        far = START + timedelta(days=300)
        self.assert_insufficient(self.assess(day=far, today=far + timedelta(days=1)), "outside the modelled crop cycle")

    def test_missing_weather_table_is_reported(self):
        self.conn.execute("DROP TABLE daily_weather_observations")
        self.assert_insufficient(self.assess(), "could not be read")

    def test_insufficient_assessment_is_not_saved(self):
        a = self.assess()
        with self.assertRaises(ValueError):
            asm.save_assessment(self.conn, a)
        self.assertEqual(self.count("daily_assessments"), 0)

    def test_many_problems_are_summarised(self):
        a = self.assess(day=date(2026, 11, 26), today=date(2026, 12, 1))   # 14 days, none stored
        self.assertIn("14 problem day(s)", a.reasoning)
        self.assertIn("... and 9 more", a.reasoning)


class AssessmentDatabaseTests(DbTestCase):
    def setUp(self):
        super().setUp()
        self.add_weather()
        self.a = self.assess()

    def test_insert_then_duplicate(self):
        self.assertTrue(asm.save_assessment(self.conn, self.a))
        self.assertFalse(asm.save_assessment(self.conn, self.a))
        self.assertEqual(self.count("daily_assessments"), 1)

    def test_stored_values_are_complete_and_distinguishable(self):
        asm.save_assessment(self.conn, self.a)
        row = self.conn.execute("SELECT field_id, assessment_date, crop_id, growth_stage, et0_mm, kc, etc_mm, taw_mm, "
                                "raw_mm, depletion_mm, recommendation, irrigation_depth_mm, status, reasoning, "
                                "simulation_start_date, warnings, config_json FROM daily_assessments").fetchone()
        self.assertEqual(row[:4], ("field_001", "2026-11-17", "winter_wheat", "initial"))
        self.assertAlmostEqual(row[4], self.a.et0_mm)
        self.assertAlmostEqual(row[9], self.a.depletion_mm)
        self.assertEqual(row[10], self.a.recommendation)
        self.assertEqual(row[12], "PROVISIONAL")
        self.assertEqual(row[14], "2026-11-13")
        self.assertTrue(json.loads(row[15]))
        config = json.loads(row[16])
        self.assertEqual(config["parameters"]["study"]["planting_date"], "2026-11-13")
        self.assertEqual(config["parameters"]["soil"]["field_capacity_m3_m3"], 0.23)
        self.assertEqual(config["weather_api_source"], DAILY_WEATHER_API_SOURCE)
        self.assertTrue(any("model-derived" in n for n in config["notes"]))

    def test_irrigate_depth_is_stored(self):
        thin = replace(CONFIG, soil=SoilConfig(field_capacity_m3_m3=0.20, wilting_point_m3_m3=0.15))
        a = self.assess(config=thin)
        asm.save_assessment(self.conn, a)
        stored = self.conn.execute("SELECT recommendation, irrigation_depth_mm FROM daily_assessments").fetchone()
        self.assertEqual(stored, ("IRRIGATE", a.irrigation_depth_mm))

    def test_different_settings_are_kept_as_separate_records(self):
        other = replace(CONFIG, soil=SoilConfig(field_capacity_m3_m3=0.30, wilting_point_m3_m3=0.12))
        asm.save_assessment(self.conn, self.a)
        self.assertTrue(asm.save_assessment(self.conn, self.assess(config=other)))
        self.assertEqual(self.count("daily_assessments"), 2)

    def test_existing_tables_and_rows_are_not_modified(self):
        before_weather = self.conn.execute("SELECT * FROM daily_weather_observations ORDER BY id").fetchall()
        self.conn.execute("INSERT INTO sensor_readings (device_id, field_id, timestamp, is_simulation) "
                          "VALUES ('d', 'field_001', '2026-11-17T00:00:00Z', 1)")
        self.conn.commit()
        before_sensor = self.conn.execute("SELECT * FROM sensor_readings").fetchall()
        asm.save_assessment(self.conn, self.a)
        asm.init_db(self.db_path).close()
        self.assertEqual(before_weather, self.conn.execute("SELECT * FROM daily_weather_observations ORDER BY id").fetchall())
        self.assertEqual(before_sensor, self.conn.execute("SELECT * FROM sensor_readings").fetchall())
        tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"sensor_readings", "daily_weather_observations", "daily_assessments"} <= tables)
        self.assertNotIn("weather_observations", tables)   # owned by weather.py; never created here

    def test_busy_timeout_is_configured(self):
        self.assertEqual(self.conn.execute("PRAGMA busy_timeout").fetchone()[0], asm.DB_BUSY_TIMEOUT_SECONDS * 1000)

    def test_default_database_is_the_shared_agrisense_db_next_to_the_scripts(self):
        self.assertEqual(asm.DB_PATH, Path(weather_daily.__file__).resolve().parent / "agrisense.db")
        self.assertEqual(weather_daily.DB_PATH, asm.DB_PATH)


class CommandLineTests(DbTestCase):
    def run_cli(self, *args):
        argv = ["--db", str(self.db_path), *args]
        with mock.patch.object(asm, "local_today", return_value=TODAY), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = asm.main(argv)
        return code, out.getvalue()

    def test_insufficient_data_run_prints_reason_and_saves_nothing(self):
        code, out = self.run_cli("--date", "2026-11-17")
        self.assertEqual(code, 0)
        self.assertIn("INSUFFICIENT_DATA", out)
        self.assertIn("[NOT SAVED]", out)
        self.assertEqual(self.count("daily_assessments"), 0)

    def test_successful_run_inserts_and_repeat_reports_duplicate(self):
        self.add_weather()
        code, first = self.run_cli("--date", "2026-11-17")
        self.assertEqual(code, 0)
        for label in ("ET0", "Kc", "ETc", "TAW / RAW", "Depletion (Dr)", "Recommendation", "Status", "Reasoning"):
            self.assertIn(label, first)
        self.assertIn("decision support only", first)
        self.assertIn("[INSERTED]", first)
        _, second = self.run_cli("--date", "2026-11-17")
        self.assertIn("[DUPLICATE]", second)
        self.assertNotIn("[INSERTED]", second)
        self.assertEqual(self.count("daily_assessments"), 1)

    def test_irrigation_argument(self):
        self.add_weather()
        self.run_cli("--date", "2026-11-17", "--irrigation", "2026-11-16=3.5")
        stored = json.loads(self.conn.execute("SELECT config_json FROM daily_assessments").fetchone()[0])
        self.assertEqual(stored["parameters"]["irrigation_events_mm"], {"2026-11-16": 3.5})

    def test_invalid_irrigation_argument_is_rejected(self):
        for bad in ("2026-11-16", "x=3", "2026-11-16=-4", "2026-11-16=abc"):
            with self.subTest(bad=bad):
                with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                    self.run_cli("--irrigation", bad)

    def test_simulated_sensor_value_is_shown_as_context_only(self):
        self.add_weather()
        self.conn.execute("INSERT INTO sensor_readings (device_id, field_id, timestamp, soil_moisture_pct, is_valid) "
                          "VALUES ('d', 'field_001', '2026-11-17T10:00:00Z', 42.0, 1)")
        self.conn.commit()
        _, out = self.run_cli("--date", "2026-11-17")
        self.assertIn("SIMULATED potentiometer", out)
        self.assertIn("NOT used", out)


if __name__ == "__main__":
    unittest.main()
