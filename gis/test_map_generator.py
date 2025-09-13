"""Tests for gis/map_generator.py (standard-library unittest, no internet needed).

Run from the project root:
    python -m unittest discover -s gis -v

Every test uses a temporary directory with temporary SQLite databases and a
temporary output file. The real backend/agrisense.db and gis/field_map.html
are never read or written. Assessment rows are SYNTHETIC TEST DATA.
"""

import contextlib
import hashlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
import webbrowser
from pathlib import Path
from unittest import mock

import map_generator as mg
from agrisense import assessment as backend_assessment   # real schema creator (tests only)
from agrisense.config import DEFAULT_CONFIG

STUDY, CROP = DEFAULT_CONFIG.study, DEFAULT_CONFIG.crop

INSERT_SQL = """
INSERT INTO daily_assessments (
    field_id, assessment_date, crop_id, growth_stage, days_after_planting, et0_mm, kc, etc_mm, ks,
    root_depth_m, taw_mm, raw_mm, depletion_mm, recommendation, irrigation_depth_mm, status, reasoning,
    openmeteo_et0_mm, simulation_start_date, warnings, config_hash, config_json, created_at)
VALUES (:field_id, :assessment_date, :crop_id, :growth_stage, :days_after_planting, :et0_mm, :kc, :etc_mm, :ks,
    :root_depth_m, :taw_mm, :raw_mm, :depletion_mm, :recommendation, :irrigation_depth_mm, :status, :reasoning,
    :openmeteo_et0_mm, :simulation_start_date, :warnings, :config_hash, :config_json, :created_at)
"""


def make_row(**overrides):
    row = dict(
        field_id=STUDY.field_id, assessment_date="2026-11-20", crop_id=CROP.crop_id, growth_stage="initial",
        days_after_planting=5, et0_mm=2.97, kc=0.7, etc_mm=2.08, ks=1.0, root_depth_m=0.26, taw_mm=30.9,
        raw_mm=17.0, depletion_mm=4.4, recommendation="NO_IRRIGATION", irrigation_depth_mm=None,
        status="PROVISIONAL", reasoning="test", openmeteo_et0_mm=3.06, simulation_start_date="2026-11-15",
        warnings=json.dumps(["Placeholder soil parameters.", "Only 5 day(s) simulated."]),
        config_hash="hash_a", config_json="{}", created_at="2026-11-21 08:00:00",
    )
    row.update(overrides)
    return row


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class GisTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.db_path = self.dir / "agrisense.db"
        self.out_path = self.dir / "out" / "field_map.html"

    def create_db(self, with_assessment_table=True, rows=()):
        if with_assessment_table:
            backend_assessment.init_db(self.db_path).close()
        else:
            conn = sqlite3.connect(self.db_path)
            conn.execute("CREATE TABLE sensor_readings (id INTEGER PRIMARY KEY, note TEXT)")
            conn.commit()
            conn.close()
        if rows:
            conn = sqlite3.connect(self.db_path)
            for row in rows:
                conn.execute(INSERT_SQL, row)
            conn.commit()
            conn.close()

    def generate(self):
        path, lookup = mg.generate(self.db_path, self.out_path)
        return path, lookup, path.read_text(encoding="utf-8")


class MissingDataTests(GisTestCase):
    def test_missing_database_file_still_produces_a_map_and_creates_no_database(self):
        path, lookup, page = self.generate()
        self.assertEqual(lookup.state, mg.DB_MISSING)
        self.assertFalse(self.db_path.exists())               # read-only: never created
        self.assertIn(mg.MARKER_TITLE, page)
        self.assertIn("Assessment unavailable", page)
        self.assertIn("Database file not found", page)

    def test_missing_assessment_table(self):
        self.create_db(with_assessment_table=False)
        before = sha256(self.db_path)
        _, lookup, page = self.generate()
        self.assertEqual(lookup.state, mg.TABLE_MISSING)
        self.assertIn("does not exist", page)
        self.assertEqual(sha256(self.db_path), before)         # untouched
        conn = sqlite3.connect(self.db_path)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        self.assertNotIn("daily_assessments", tables)          # not created by the GIS module

    def test_empty_assessment_table(self):
        self.create_db()
        _, lookup, page = self.generate()
        self.assertEqual(lookup.state, mg.TABLE_EMPTY)
        self.assertIn("contains no assessments", page)

    def test_assessment_for_a_different_field(self):
        self.create_db(rows=[make_row(field_id="field_999")])
        _, lookup, page = self.generate()
        self.assertEqual(lookup.state, mg.NO_MATCH)
        self.assertIn("none for field &#x27;field_001&#x27;", page)     # message is HTML-escaped
        self.assertNotIn("2026-11-20", page)

    def test_assessment_for_a_different_crop(self):
        self.create_db(rows=[make_row(crop_id="maize")])
        _, lookup, page = self.generate()
        self.assertEqual(lookup.state, mg.NO_MATCH)
        self.assertIn("crop &#x27;winter_wheat&#x27;", page)

    def test_the_four_unavailable_states_are_distinguishable(self):
        outcomes = []

        def record():
            lookup = self.generate()[1]
            outcomes.append((lookup.state, lookup.message))

        record()                                              # no database file
        self.create_db(with_assessment_table=False)
        record()                                              # no assessment table
        self.db_path.unlink()
        self.create_db()
        record()                                              # empty table
        self.db_path.unlink()
        self.create_db(rows=[make_row(field_id="other")])
        record()                                              # no matching field
        self.assertEqual(len({state for state, _ in outcomes}), 4)
        self.assertEqual(len({message for _, message in outcomes}), 4)

    def test_unexpected_schema_fails_loudly(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE daily_assessments (id INTEGER, field_id TEXT)")
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(mg.GisError, "missing expected column"):
            mg.generate(self.db_path, self.out_path)

    def test_a_file_that_is_not_a_database_is_not_swallowed(self):
        self.db_path.write_bytes(b"this is not a sqlite database" * 20)
        with self.assertRaises(sqlite3.DatabaseError):
            mg.generate(self.db_path, self.out_path)


class ValidAssessmentTests(GisTestCase):
    def setUp(self):
        super().setUp()
        self.create_db(rows=[make_row()])

    def test_assessment_is_included_with_all_requested_fields(self):
        _, lookup, page = self.generate()
        self.assertEqual(lookup.state, mg.FOUND)
        for expected in ("2026-11-20", "winter_wheat", "initial", "2.97 mm/day", "0.70", "2.08 mm/day",
                         "4.4 mm", "17.0 mm", "30.9 mm", "NO_IRRIGATION", "PROVISIONAL",
                         "Placeholder soil parameters.", "Only 5 day(s) simulated."):
            self.assertIn(expected, page)

    def test_provisional_status_is_clearly_visible(self):
        _, _, page = self.generate()
        self.assertIn("Not scientifically validated", page)
        self.assertIn("hypothetical placeholder parameters", page)

    def test_status_ok_has_no_provisional_banner_but_keeps_the_disclaimer(self):
        self.db_path.unlink()
        self.create_db(rows=[make_row(status="OK", warnings="[]")])
        _, _, page = self.generate()
        self.assertNotIn("Not scientifically validated", page)
        self.assertIn("not validated against field measurements", page)

    def test_net_irrigation_depth_is_shown_when_available(self):
        self.db_path.unlink()
        self.create_db(rows=[make_row(recommendation="IRRIGATE", irrigation_depth_mm=18.3)])
        _, _, page = self.generate()
        self.assertIn("18.3 mm", page)
        self.assertIn("IRRIGATE", page)

    def test_missing_depth_is_shown_as_not_available(self):
        _, _, page = self.generate()
        self.assertIn("Net irrigation depth", page)
        self.assertIn("n/a", page)

    def test_null_values_do_not_crash(self):
        self.db_path.unlink()
        self.create_db(rows=[make_row(growth_stage=None, et0_mm=None, kc=None, warnings=None)])
        _, lookup, page = self.generate()
        self.assertEqual(lookup.state, mg.FOUND)
        self.assertIn("n/a", page)

    def test_generation_leaves_database_file_byte_identical(self):
        before = sha256(self.db_path)
        self.generate()
        self.assertEqual(sha256(self.db_path), before)

    def test_connection_is_read_only(self):
        with contextlib.closing(mg.open_readonly(self.db_path)) as conn:
            with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                conn.execute("DELETE FROM daily_assessments")
            with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                conn.execute("CREATE TABLE x (a)")

    def test_another_fields_newer_assessment_does_not_leak_into_the_map(self):
        self.db_path.unlink()
        self.create_db(rows=[make_row(), make_row(field_id="field_002", recommendation="IRRIGATE",
                                                   assessment_date="2026-12-01")])
        _, _, page = self.generate()
        self.assertNotIn("2026-12-01", page)


class LatestSelectionTests(GisTestCase):
    def latest(self, rows):
        self.create_db(rows=rows)
        return mg.find_latest_assessment(self.db_path, STUDY.field_id, CROP.crop_id).assessment

    def test_latest_assessment_date_wins_regardless_of_insert_order(self):
        a = self.latest([make_row(assessment_date="2026-11-22", config_hash="n", recommendation="MONITOR"),
                         make_row(assessment_date="2026-11-20", config_hash="o", recommendation="IRRIGATE"),
                         make_row(assessment_date="2026-11-21", config_hash="m")])
        self.assertEqual((a["assessment_date"], a["recommendation"]), ("2026-11-22", "MONITOR"))

    def test_same_date_uses_latest_created_at(self):
        a = self.latest([make_row(config_hash="x", created_at="2026-11-21 09:00:00", recommendation="MONITOR"),
                         make_row(config_hash="y", created_at="2026-11-21 10:30:00", recommendation="IRRIGATE"),
                         make_row(config_hash="z", created_at="2026-11-21 08:00:00")])
        self.assertEqual(a["recommendation"], "IRRIGATE")

    def test_same_date_and_created_at_uses_highest_id(self):
        a = self.latest([make_row(config_hash="x", recommendation="NO_IRRIGATION"),
                         make_row(config_hash="y", recommendation="MONITOR")])
        self.assertEqual(a["recommendation"], "MONITOR")
        self.assertEqual(a["id"], 2)

    def test_only_matching_field_and_crop_are_eligible(self):
        a = self.latest([make_row(assessment_date="2026-12-31", field_id="other"),
                         make_row(assessment_date="2026-12-30", crop_id="maize", config_hash="b"),
                         make_row(assessment_date="2026-11-20", config_hash="c")])
        self.assertEqual(a["assessment_date"], "2026-11-20")


class EscapingTests(GisTestCase):
    SCRIPT = "<script>alert('xss')</script>"
    IMG = '<img src=x onerror="alert(1)">'
    JS_BREAKOUT = "`);alert(document.cookie);//${alert(1)}\\"

    def test_hostile_database_text_is_escaped_everywhere(self):
        self.create_db(rows=[make_row(
            growth_stage=self.SCRIPT,
            recommendation=self.IMG,
            status="PROVISIONAL" + self.JS_BREAKOUT,
            assessment_date=self.SCRIPT,
            warnings=json.dumps([self.SCRIPT, self.IMG, self.JS_BREAKOUT]),
            et0_mm=self.SCRIPT,                      # SQLite lets text sit in a REAL column
            taw_mm=self.IMG,
        )])
        _, lookup, page = self.generate()
        self.assertEqual(lookup.state, mg.FOUND)
        for raw in (self.SCRIPT, self.IMG, "<script>alert", "<img src=x", "${alert(1)}", "`);alert"):
            self.assertNotIn(raw, page)
        self.assertIn("&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;", page)
        self.assertIn("&lt;img src=x onerror=&quot;alert(1)&quot;&gt;", page)
        self.assertIn("&#96;);alert(document.cookie);//&#36;&#123;alert(1)&#125;&#92;", page)

    def test_no_stray_backticks_or_template_expressions_from_data(self):
        self.create_db(rows=[make_row(warnings=json.dumps([self.JS_BREAKOUT]))])
        hostile = self.generate()[2]
        self.db_path.unlink()
        self.create_db(rows=[make_row(warnings=json.dumps(["harmless"]))])
        harmless = self.generate()[2]
        self.assertEqual(hostile.count("`"), harmless.count("`"))      # no extra JS string delimiters
        self.assertEqual(hostile.count("${"), harmless.count("${"))
        self.assertEqual(hostile.count("<script"), harmless.count("<script"))

    def test_malformed_warnings_json_is_shown_escaped_not_parsed(self):
        self.create_db(rows=[make_row(warnings="{not json " + self.SCRIPT)])
        _, _, page = self.generate()
        self.assertNotIn(self.SCRIPT, page)
        self.assertIn("{not json", page.replace("&#123;", "{"))

    def test_non_list_warnings_json_is_tolerated(self):
        self.assertEqual(mg.parse_warnings('"single"'), ["single"])
        self.assertEqual(mg.parse_warnings('{"a": 1}'), ["{'a': 1}"])
        self.assertEqual(mg.parse_warnings(None), [])
        self.assertEqual(mg.parse_warnings(""), [])
        self.assertEqual(mg.parse_warnings("[1, null]"), ["1", "None"])

    def test_esc_helper(self):
        self.assertEqual(mg.esc(None), "")
        self.assertEqual(mg.esc("<b>&\"'"), "&lt;b&gt;&amp;&quot;&#x27;")
        for char in "`\\${}":
            self.assertNotIn(char, mg.esc(char * 3))

    def test_fmt_number_escapes_non_numbers(self):
        self.assertEqual(mg.fmt_number(None), "n/a")
        self.assertEqual(mg.fmt_number("2.5", 1, " mm"), "2.5 mm")
        self.assertEqual(mg.fmt_number("<b>x</b>"), "&lt;b&gt;x&lt;/b&gt;")
        self.assertEqual(mg.fmt_number(float("nan")), "nan")
        self.assertEqual(mg.fmt_number(float("inf")), "inf")

    def test_config_text_is_escaped_too(self):
        from dataclasses import replace
        evil = replace(DEFAULT_CONFIG, study=replace(DEFAULT_CONFIG.study, description=self.SCRIPT))
        path, _ = mg.generate(self.db_path, self.out_path, config=evil)
        self.assertNotIn(self.SCRIPT, path.read_text(encoding="utf-8"))


class MapContentAndOutputTests(GisTestCase):
    def setUp(self):
        super().setUp()
        self.create_db(rows=[make_row()])
        self.path, self.lookup, self.page = self.generate()

    def test_output_file_is_generated_in_the_requested_location(self):
        self.assertEqual(self.path, self.out_path)
        self.assertTrue(self.out_path.is_file())
        self.assertGreater(self.out_path.stat().st_size, 1000)
        self.assertIn("<html", self.page.lower())

    def test_output_parent_directory_is_created_if_needed(self):
        deep = self.dir / "a" / "b" / "map.html"
        mg.generate(self.db_path, deep)
        self.assertTrue(deep.is_file())

    def test_map_is_centred_on_the_configured_coordinates(self):
        self.assertIn(f"[{STUDY.latitude}, {STUDY.longitude}]", self.page)

    def test_coordinates_are_not_duplicated_in_the_module(self):
        source = Path(mg.__file__).read_text(encoding="utf-8")
        for literal in (str(STUDY.latitude), str(STUDY.longitude)):
            self.assertNotIn(literal, source)
        self.assertIs(mg.DEFAULT_CONFIG, DEFAULT_CONFIG)

    def test_openstreetmap_tiles_and_attribution(self):
        self.assertIn("tile.openstreetmap.org", self.page)
        # Folium JSON-encodes the attribution string (< > & become \u003c \u003e \u0026).
        self.assertIn("openstreetmap.org/copyright", self.page)
        self.assertIn("OpenStreetMap\\u003c/a\\u003e contributors", self.page)

    def test_long_popups_scroll_instead_of_being_clipped(self):
        self.assertIn(f'"maxHeight": {mg.POPUP_MAX_HEIGHT}', self.page)

    def test_popup_is_not_open_on_load(self):
        self.assertNotIn(".openPopup()", self.page)

    def test_info_box_is_placed_above_the_scale_control(self):
        self.assertIn("position:fixed;bottom:58px", self.page)       # scale control occupies the bottom ~45 px

    def test_exactly_one_marker_titled_as_required(self):
        self.assertEqual(self.page.count("L.marker("), 1)
        self.assertIn("AgriSense Representative Study Location", self.page)
        self.assertGreaterEqual(self.page.count(mg.MARKER_TITLE), 3)   # title option, tooltip, popup heading

    def test_popup_states_the_location_is_representative_and_unsurveyed(self):
        self.assertIn("Representative point", self.page)
        self.assertIn("not a surveyed or physically monitored field", self.page)
        self.assertIn("No field boundary has been established", self.page)

    def test_no_fabricated_geometries_or_layers(self):
        for forbidden in ("L.polygon", "L.polyline", "L.rectangle", "L.circle", "L.geoJson", "GeoJson",
                          "L.imageOverlay", "L.heatLayer", "L.tileLayer.wms"):
            self.assertNotIn(forbidden, self.page)
        self.assertEqual(self.page.count("L.tileLayer("), 1)

    def test_no_external_gis_assumptions_in_the_module(self):
        source = Path(mg.__file__).read_text(encoding="utf-8")
        for forbidden in ("geopandas", "rasterio", "shapely"):
            self.assertNotIn(forbidden, source)

    def test_browser_is_never_opened(self):
        with mock.patch.object(webbrowser, "open") as opened:
            mg.generate(self.db_path, self.out_path)
        opened.assert_not_called()
        self.assertNotIn("webbrowser", Path(mg.__file__).read_text(encoding="utf-8"))


class ThemeTests(unittest.TestCase):

    def page(self, lookup, theme, notice=None):
        return mg.build_map(DEFAULT_CONFIG, lookup, notice, theme=theme).get_root().render()

    def found(self, **overrides):
        return mg.AssessmentLookup(mg.FOUND, "", make_row(**overrides))

    def test_light_is_the_default_and_uses_openstreetmap_tiles(self):
        default = mg.build_map(DEFAULT_CONFIG, self.found()).get_root().render()
        self.assertIn("tile.openstreetmap.org", default)
        self.assertNotIn(".leaflet-popup-content-wrapper", default)       # dark-only CSS is absent
        self.assertNotIn("filter:invert", default)
        self.assertNotIn(".leaflet-tile-pane{filter", default)

    def test_dark_keeps_readable_osm_tiles_without_inversion(self):
        page = self.page(self.found(), mg.DARK)
        self.assertIn("tile.openstreetmap.org", page)             # no key-gated third-party basemap
        self.assertNotIn("cartocdn", page)
        self.assertIn("openstreetmap.org/copyright", page)        # data licence still credited
        self.assertEqual(page.count("L.tileLayer("), 1)
        self.assertNotIn("filter:invert", page)
        self.assertNotIn(".leaflet-tile-pane{filter", page)
        self.assertNotIn("hue-rotate", page)
        self.assertIn(".leaflet-popup-content-wrapper", page)     # overlay chrome is restyled, tiles are not
        self.assertNotIn(".openPopup()", page)

    def test_dark_map_has_the_same_single_marker_and_no_invented_geometry(self):
        page = self.page(self.found(), mg.DARK)
        self.assertEqual(page.count("L.marker("), 1)
        self.assertIn(f"[{STUDY.latitude}, {STUDY.longitude}]", page)
        for forbidden in ("L.polygon", "L.polyline", "L.rectangle", "L.circle", "GeoJson", "L.imageOverlay"):
            self.assertNotIn(forbidden, page)
        self.assertIn("not a surveyed or physically monitored field", page)
        self.assertIn("Representative point", page)

    def test_dark_popup_and_info_box_use_dark_palettes(self):
        page = self.page(self.found(), mg.DARK)
        self.assertIn(mg.PALETTES[mg.DARK]["warn_bg"], page)     # PROVISIONAL banner
        self.assertIn(mg.PALETTES[mg.DARK]["box_bg"], page)      # info box
        self.assertIn(".leaflet-popup-content-wrapper", page)    # popup/zoom control restyled
        light = self.page(self.found(), mg.LIGHT)
        self.assertNotIn(mg.PALETTES[mg.DARK]["warn_bg"], light)
        self.assertIn(mg.PALETTES[mg.LIGHT]["warn_bg"], light)

    def test_dark_theme_keeps_every_state_message(self):
        for lookup, expected in ((mg.AssessmentLookup(mg.TABLE_EMPTY, "contains no assessments"), "Assessment unavailable"),
                                 (self.found(), "Not current")):
            with self.subTest(expected):
                page = self.page(lookup, mg.DARK, notice="Not current: 5 day(s) behind." if expected == "Not current" else None)
                self.assertIn(expected, page)

    def test_hostile_text_is_still_escaped_in_the_dark_theme(self):
        page = self.page(self.found(warnings=json.dumps(["`${alert(1)}` </script><script>x"])), mg.DARK)
        self.assertNotIn("</script><script>x", page)

    def test_unknown_theme_is_rejected(self):
        with self.assertRaises(ValueError):
            mg.build_map(DEFAULT_CONFIG, self.found(), theme="sepia")

    def test_standalone_generator_stays_light(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "m.html"
            mg.generate(Path(tmp) / "none.db", out)
            self.assertIn("tile.openstreetmap.org", out.read_text(encoding="utf-8"))


class CommandLineTests(GisTestCase):
    def run_main(self):
        with mock.patch.object(mg, "DB_PATH", self.db_path), mock.patch.object(mg, "OUTPUT_PATH", self.out_path):
            # generate()'s defaults were bound at definition time; call through the patched constants.
            original = mg.generate
            with mock.patch.object(mg, "generate", lambda: original(self.db_path, self.out_path)), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                code = mg.main()
        return code, out.getvalue()

    def test_confirmation_when_assessment_is_included(self):
        self.create_db(rows=[make_row()])
        code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn(f"Map saved to: {self.out_path}", out)
        self.assertIn("Assessment: included (2026-11-20, winter_wheat, status PROVISIONAL)", out)

    def test_confirmation_when_assessment_is_not_included(self):
        code, out = self.run_main()                          # database missing
        self.assertEqual(code, 0)
        self.assertIn(f"Map saved to: {self.out_path}", out)
        self.assertIn("Assessment: NOT included [DB_MISSING]", out)

    def test_schema_problem_returns_error_code(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE daily_assessments (id INTEGER)")
        conn.commit()
        conn.close()
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("[ERROR]", out)


class ProjectPathTests(unittest.TestCase):
    def test_default_paths_resolve_from_the_module_location(self):
        root = Path(mg.__file__).resolve().parent.parent
        self.assertEqual(mg.PROJECT_ROOT, root)
        self.assertEqual(mg.DB_PATH, root / "backend" / "agrisense.db")
        self.assertEqual(mg.OUTPUT_PATH, root / "gis" / "field_map.html")

    def test_default_database_is_the_backend_database_used_by_the_backend(self):
        self.assertEqual(mg.DB_PATH, backend_assessment.DB_PATH)

    def test_backend_is_importable_regardless_of_working_directory(self):
        self.assertIn(str(mg.BACKEND_DIR), sys.path)


if __name__ == "__main__":
    unittest.main()
