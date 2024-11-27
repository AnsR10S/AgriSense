"""Tests for weather_daily.py (standard-library unittest, no network needed).

Run from the backend folder:
    python -m unittest tests.test_weather_daily -v

All tests use a temporary database; agrisense.db is never touched.
"""

import contextlib
import copy
import io
import sqlite3
import tempfile
import unittest
import zoneinfo
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

import requests

import weather_daily as wd

TODAY = date(2026, 11, 15)  # pretend Africa/Cairo local date so tests are reproducible


def make_response(dates=("2026-11-13", "2026-11-14", "2026-11-15")):
    n = len(dates)
    daily = {"time": list(dates)}
    for i, name in enumerate(wd.VARIABLES):
        daily[name] = [round(10.0 + i + d * 0.5, 2) for d in range(n)]
    units = {"time": "iso8601"}
    units.update({name: unit for name, (_col, unit) in wd.VARIABLES.items()})
    return {"daily_units": units, "daily": daily}


class TempDbTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "test.db"
        with contextlib.redirect_stdout(io.StringIO()):
            self.conn = wd.init_db(self.db_path)
        # Look up self.conn at cleanup time: some tests reopen the connection.
        self.addCleanup(lambda: self.conn.close())

    def count(self, table="daily_weather_observations"):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def collect(self, response, today=TODAY):
        with mock.patch.object(wd, "fetch_daily_weather", return_value=response), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            result = wd.collect_once(self.conn, "field_001", 29.97, 30.94, today=today)
        return result, out.getvalue()


class UnitValidationTests(unittest.TestCase):
    def test_valid_response_is_accepted(self):
        daily = wd.validate_response(make_response())
        self.assertEqual(daily["time"][0], "2026-11-13")

    def test_wrong_units_are_rejected(self):
        bad_units = {
            "wind_speed_10m_mean": "km/h",
            "surface_pressure_mean": "kPa",
            "shortwave_radiation_sum": "W/m\u00b2",   # power, not daily energy
            "precipitation_sum": "inch",
            "temperature_2m_max": "\u00b0F",
            "relative_humidity_2m_mean": "fraction",
            "et0_fao_evapotranspiration": "inch",
        }
        for variable, unit in bad_units.items():
            with self.subTest(variable=variable, unit=unit):
                data = make_response()
                data["daily_units"][variable] = unit
                with self.assertRaisesRegex(wd.WeatherError, "unexpected unit"):
                    wd.validate_response(data)

    def test_missing_unit_is_rejected(self):
        for variable in wd.VARIABLES:
            with self.subTest(variable=variable):
                data = make_response()
                del data["daily_units"][variable]
                with self.assertRaisesRegex(wd.WeatherError, "unit for"):
                    wd.validate_response(data)

    def test_radiation_is_expected_in_mj_per_m2(self):
        self.assertEqual(wd.VARIABLES["shortwave_radiation_sum"][1], "MJ/m\u00b2")
        self.assertEqual(wd.VARIABLES["wind_speed_10m_mean"][1], "m/s")
        self.assertEqual(wd.VARIABLES["surface_pressure_mean"][1], "hPa")
        self.assertEqual(wd.VARIABLES["precipitation_sum"][1], "mm")


class MissingDataTests(TempDbTestCase):
    def test_null_value_is_stored_as_null_not_replaced(self):
        data = make_response(("2026-11-14",))
        data["daily"]["surface_pressure_mean"][0] = None
        data["daily"]["et0_fao_evapotranspiration"][0] = None
        result, _ = self.collect(data)
        self.assertEqual(result["inserted"], 1)
        row = self.conn.execute(
            "SELECT surface_pressure_mean_hpa, openmeteo_et0_mm, temperature_max_c "
            "FROM daily_weather_observations").fetchone()
        self.assertIsNone(row[0])
        self.assertIsNone(row[1])
        self.assertIsNotNone(row[2])

    def test_zero_is_a_real_value_not_missing(self):
        data = make_response(("2026-11-14",))
        data["daily"]["precipitation_sum"][0] = 0.0
        self.collect(data)
        self.assertEqual(self.conn.execute(
            "SELECT precipitation_sum_mm FROM daily_weather_observations").fetchone()[0], 0.0)

    def test_all_null_day_is_not_stored(self):
        data = make_response(("2026-11-13", "2026-11-14"))
        for name in wd.VARIABLES:
            data["daily"][name][1] = None
        result, out = self.collect(data)
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(result["skipped_empty"], 1)
        self.assertEqual(self.count(), 1)
        self.assertIn("[SKIPPED] 2026-11-14", out)

    def test_non_finite_value_becomes_null(self):
        data = make_response(("2026-11-14",))
        data["daily"]["wind_speed_10m_mean"][0] = float("nan")
        self.collect(data)
        self.assertIsNone(self.conn.execute(
            "SELECT wind_speed_10m_mean_m_s FROM daily_weather_observations").fetchone()[0])

    def test_non_numeric_value_rejects_the_response(self):
        for bad in ("12.5", True, [1], {"v": 1}):
            with self.subTest(bad=bad):
                data = make_response(("2026-11-14",))
                data["daily"]["temperature_2m_max"][0] = bad
                result, out = self.collect(data)
                self.assertIsNone(result)
                self.assertIn("[FAILED]", out)
                self.assertEqual(self.count(), 0)


class DateHandlingTests(TempDbTestCase):
    def test_current_and_future_days_are_not_inserted(self):
        data = make_response(("2026-11-13", "2026-11-14", "2026-11-15", "2026-11-16"))
        result, out = self.collect(data)
        self.assertEqual(result["inserted"], 2)
        self.assertEqual(result["ignored"], 2)
        dates = [r[0] for r in self.conn.execute(
            "SELECT date FROM daily_weather_observations ORDER BY date")]
        self.assertEqual(dates, ["2026-11-13", "2026-11-14"])

    def test_stored_date_is_the_api_date(self):
        data = make_response(("2026-01-02",))
        self.collect(data, today=date(2026, 6, 1))
        self.assertEqual(self.conn.execute(
            "SELECT date FROM daily_weather_observations").fetchone()[0], "2026-01-02")


class CairoTimezoneTests(TempDbTestCase):
    def test_today_is_the_cairo_date_not_the_utc_date(self):
        # 23:30 UTC on 14 Nov is already 01:30 on 15 Nov in Cairo (UTC+2 in November).
        late_utc = datetime(2026, 11, 14, 23, 30, tzinfo=timezone.utc)
        self.assertEqual(late_utc.date(), date(2026, 11, 14))
        self.assertEqual(wd.local_today(wd.TIMEZONE, now=late_utc), date(2026, 11, 15))

    def test_cairo_summer_time_offset(self):
        # Egypt observes summer time (UTC+3): 21:30 UTC on 30 Jun is 00:30 on 1 Jul.
        now = datetime(2026, 6, 30, 21, 30, tzinfo=timezone.utc)
        self.assertEqual(wd.local_today(wd.TIMEZONE, now=now), date(2026, 7, 1))

    def test_collect_once_uses_the_cairo_date_to_exclude_today(self):
        data = make_response(("2026-11-14", "2026-11-15"))
        with mock.patch.object(wd, "local_today", return_value=date(2026, 11, 15)):
            with mock.patch.object(wd, "fetch_daily_weather", return_value=data), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                result = wd.collect_once(self.conn, "field_001", 29.97, 30.94)
        self.assertEqual(result["inserted"], 1)
        self.assertIn("Africa/Cairo", out.getvalue())

    def test_rows_are_tagged_with_the_cairo_day_source(self):
        self.collect(make_response(("2026-11-14",)))
        source = self.conn.execute("SELECT api_source FROM daily_weather_observations").fetchone()[0]
        self.assertEqual(source, "open-meteo;tz=Africa/Cairo")

    def test_legacy_utc_rows_are_not_modified(self):
        self.conn.execute(
            "INSERT INTO daily_weather_observations (field_id, date, temperature_max_c, api_source) "
            "VALUES ('field_001', '2026-11-14', 99.0, 'open-meteo')")
        self.conn.commit()
        result, _ = self.collect(make_response(("2026-11-14",)))
        self.assertEqual(result["duplicates"], 1)  # same date: ignored, legacy row untouched
        row = self.conn.execute(
            "SELECT temperature_max_c, api_source FROM daily_weather_observations").fetchone()
        self.assertEqual(row, (99.0, "open-meteo"))

    def test_missing_timezone_database_is_reported_not_raised(self):
        with mock.patch.object(wd, "fetch_daily_weather") as fetch, \
                mock.patch("agrisense.config.ZoneInfo",
                           side_effect=zoneinfo.ZoneInfoNotFoundError("none")), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            result = wd.collect_once(self.conn, "field_001", 29.97, 30.94)
        self.assertIsNone(result)
        self.assertIn("[FAILED]", out.getvalue())
        self.assertIn("tzdata", out.getvalue())
        fetch.assert_not_called()


class DuplicateTests(TempDbTestCase):
    def test_save_reports_insert_then_duplicate(self):
        daily = wd.validate_response(make_response(("2026-11-14",)))
        record = wd.process_daily_record("field_001", daily, 0)
        self.assertTrue(wd.save_daily_record(self.conn, record))
        self.assertFalse(wd.save_daily_record(self.conn, record))
        self.assertEqual(self.count(), 1)

    def test_repeated_run_creates_no_duplicates(self):
        data = make_response()
        first, out1 = self.collect(data)
        second, out2 = self.collect(data)
        self.assertEqual((first["inserted"], first["duplicates"]), (2, 0))
        self.assertEqual((second["inserted"], second["duplicates"]), (0, 2))
        self.assertEqual(self.count(), 2)
        self.assertIn("[INSERTED]", out1)
        self.assertNotIn("[INSERTED]", out2)
        self.assertIn("[DUPLICATE]", out2)

    def test_same_date_for_different_field_is_allowed(self):
        daily = wd.validate_response(make_response(("2026-11-14",)))
        self.assertTrue(wd.save_daily_record(self.conn, wd.process_daily_record("field_001", daily, 0)))
        self.assertTrue(wd.save_daily_record(self.conn, wd.process_daily_record("field_002", daily, 0)))

    def test_duplicate_does_not_overwrite_existing_row(self):
        data = make_response(("2026-11-14",))
        self.collect(data)
        changed = copy.deepcopy(data)
        changed["daily"]["temperature_2m_max"][0] = 99.0
        self.collect(changed)
        stored = self.conn.execute(
            "SELECT temperature_max_c FROM daily_weather_observations").fetchone()[0]
        self.assertNotEqual(stored, 99.0)


class InvalidResponseTests(TempDbTestCase):
    def assert_rejected(self, response, pattern=None):
        with self.assertRaisesRegex(wd.WeatherError, pattern or ""):
            wd.validate_response(response)

    def test_structurally_invalid_responses(self):
        cases = {
            "not an object": ([], "not an object"),
            "empty object": ({}, "no 'daily'"),
            "api error flag": ({"error": True, "reason": "boom"}, "API error: boom"),
            "daily not object": ({"daily": [], "daily_units": {}}, "no 'daily'"),
            "units missing": ({"daily": {"time": ["2026-11-14"]}}, "no 'daily_units'"),
        }
        for label, (response, pattern) in cases.items():
            with self.subTest(label=label):
                self.assert_rejected(response, pattern)

    def test_time_problems(self):
        data = make_response(); data["daily"]["time"] = "2026-11-14"
        self.assert_rejected(data, "daily.time")
        data = make_response(); data["daily"]["time"] = []
        self.assert_rejected(data, "daily.time")
        data = make_response(); data["daily"]["time"][0] = "14/11/2026"
        self.assert_rejected(data, "invalid date")
        data = make_response(); data["daily"]["time"][0] = "2026-11-14T00:00"
        self.assert_rejected(data, "invalid date")
        data = make_response(); data["daily"]["time"][1] = data["daily"]["time"][0]
        self.assert_rejected(data, "duplicate")
        data = make_response(); del data["daily"]["time"]
        self.assert_rejected(data, "daily.time")

    def test_missing_variable_or_wrong_length(self):
        for variable in wd.VARIABLES:
            with self.subTest(variable=variable):
                data = make_response(); del data["daily"][variable]
                self.assert_rejected(data, "missing from the response")
        data = make_response(); data["daily"]["precipitation_sum"] = data["daily"]["precipitation_sum"][:-1]
        self.assert_rejected(data, "values for")
        data = make_response(); data["daily"]["precipitation_sum"] = "0.0"
        self.assert_rejected(data, "not a list")

    def test_invalid_response_stores_nothing(self):
        data = make_response(); data["daily_units"]["wind_speed_10m_mean"] = "km/h"
        result, out = self.collect(data)
        self.assertIsNone(result)
        self.assertIn("[FAILED]", out)
        self.assertEqual(self.count(), 0)


class FetchErrorTests(TempDbTestCase):
    def test_fetch_failures_are_reported_and_store_nothing(self):
        failures = [requests.exceptions.Timeout(),
                    requests.exceptions.ConnectionError("dns failure"),
                    requests.exceptions.RequestException("other")]
        for exc in failures:
            with self.subTest(exc=type(exc).__name__):
                with mock.patch.object(wd.requests, "get", side_effect=exc), \
                        contextlib.redirect_stdout(io.StringIO()) as out:
                    result = wd.collect_once(self.conn, "field_001", 29.97, 30.94, today=TODAY)
                self.assertIsNone(result)
                self.assertIn("[FAILED]", out.getvalue())
                self.assertEqual(self.count(), 0)

    def test_http_error_includes_api_reason(self):
        response = mock.Mock(ok=False, status_code=400, reason="Bad Request")
        response.json.return_value = {"error": True, "reason": "Latitude must be in range"}
        with mock.patch.object(wd.requests, "get", return_value=response):
            with self.assertRaisesRegex(wd.WeatherError, "HTTP 400.*Latitude must be"):
                wd.fetch_daily_weather(999, 30.94)

    def test_non_json_body_is_rejected(self):
        response = mock.Mock(ok=True)
        response.json.side_effect = ValueError("no json")
        with mock.patch.object(wd.requests, "get", return_value=response):
            with self.assertRaisesRegex(wd.WeatherError, "not valid JSON"):
                wd.fetch_daily_weather(29.97, 30.94)

    def test_request_parameters(self):
        response = mock.Mock(ok=True)
        response.json.return_value = {}
        with mock.patch.object(wd.requests, "get", return_value=response) as get:
            wd.fetch_daily_weather(29.97, 30.94)
        params = get.call_args.kwargs["params"]
        self.assertEqual(get.call_args.args[0], "https://api.open-meteo.com/v1/forecast")
        self.assertEqual(params["timezone"], "Africa/Cairo")
        self.assertEqual(params["wind_speed_unit"], "ms")
        self.assertEqual(params["temperature_unit"], "celsius")
        self.assertEqual(params["precipitation_unit"], "mm")
        self.assertEqual(params["past_days"], 2)
        self.assertEqual(params["daily"].split(","), list(wd.VARIABLES))


class DatabaseTests(TempDbTestCase):
    def test_existing_tables_and_rows_are_preserved(self):
        self.conn.execute("CREATE TABLE sensor_readings (id INTEGER, note TEXT)")
        self.conn.execute("INSERT INTO sensor_readings VALUES (1, 'keep me')")
        self.conn.commit()
        self.conn.close()
        self.conn = wd.init_db(self.db_path)  # re-initialise on a populated DB
        self.assertEqual(self.conn.execute("SELECT note FROM sensor_readings").fetchone()[0], "keep me")

    def test_init_is_idempotent(self):
        wd.init_db(self.db_path).close()
        wd.init_db(self.db_path).close()
        tables = [r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='daily_weather_observations'")]
        self.assertEqual(tables, ["daily_weather_observations"])

    def test_sqlite_error_is_handled_without_crashing(self):
        self.conn.execute("DROP TABLE daily_weather_observations")
        self.conn.commit()
        result, out = self.collect(make_response())
        self.assertEqual(result["inserted"], 0)
        self.assertEqual(result["errors"], 2)
        self.assertIn("[DB ERROR]", out)

    def test_busy_timeout_is_configured(self):
        self.assertEqual(self.conn.execute("PRAGMA busy_timeout").fetchone()[0],
                         wd.DB_BUSY_TIMEOUT_SECONDS * 1000)


if __name__ == "__main__":
    unittest.main()
