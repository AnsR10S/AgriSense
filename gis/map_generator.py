"""Folium map of the study location, with the latest stored assessment.

One marker at the configured coordinates (representative point near 6th of
October City). No surveyed field, no boundary, no soil/terrain layers, no
sensor coverage. Assessments are model output and shown with status/warnings.

DB access is read-only (mode=ro); nothing here creates or migrates tables.
DB-derived text is HTML-escaped before it reaches the page.

Run from project root:  python gis/map_generator.py  -> gis/field_map.html

TODO: replace the single point with surveyed field polygons (GPS/RTK) and
per-node positions once hardware is placed.
"""

import contextlib
import html
import json
import math
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = PROJECT_ROOT / "backend"
DB_PATH = BACKEND_DIR / "agrisense.db"
OUTPUT_PATH = Path(__file__).resolve().parent / "field_map.html"

# Config (coords, field id, crop) lives in the backend; no copies here.
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
from agrisense.config import DEFAULT_CONFIG  # noqa: E402

try:
    import folium
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "Folium is required for the GIS module. Run: pip install -r backend/requirements.txt"
    ) from exc

TABLE = "daily_assessments"
MARKER_TITLE = "AgriSense Representative Study Location"
OSM_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
OSM_ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'

# Column names must match backend/agrisense/assessment.py.
COLUMNS = (
    "id", "field_id", "assessment_date", "crop_id", "growth_stage", "days_after_planting",
    "et0_mm", "kc", "etc_mm", "root_depth_m", "taw_mm", "raw_mm", "depletion_mm",
    "recommendation", "irrigation_depth_mm", "status", "warnings", "created_at",
)

DB_MISSING = "DB_MISSING"
TABLE_MISSING = "TABLE_MISSING"
TABLE_EMPTY = "TABLE_EMPTY"
NO_MATCH = "NO_MATCH"
FOUND = "FOUND"


class GisError(RuntimeError):
    pass


@dataclass(frozen=True)
class AssessmentLookup:
    state: str
    message: str
    assessment: dict = None   # populated only when state == FOUND


# Folium drops popup HTML into a JS template literal, so besides HTML escaping
# the backtick, backslash, $ and braces become numeric character references.
_JS_TEMPLATE_UNSAFE = str.maketrans(
    {"`": "&#96;", "\\": "&#92;", "$": "&#36;", "{": "&#123;", "}": "&#125;"}
)


def esc(value):
    text = "" if value is None else str(value)
    return html.escape(text, quote=True).translate(_JS_TEMPLATE_UNSAFE)


def fmt_number(value, digits=2, unit=""):
    if value is None:
        return "n/a"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return esc(value)           # SQLite is dynamically typed; a REAL column can hold text
    if not math.isfinite(number):
        return esc(value)
    return f"{number:.{digits}f}{unit}"


def parse_warnings(raw):
    if raw is None or raw == "":
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return [str(raw)]
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def open_readonly(db_path):
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=10)


def _display_path(path):
    try:
        return Path(path).resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


def find_latest_assessment_in(conn, field_id, crop_id, db_label="the database", columns=COLUMNS):
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (TABLE,)).fetchone()
    if not exists:
        return AssessmentLookup(
            TABLE_MISSING,
            f"Table {TABLE} does not exist in {db_label}. "
            f"No assessment has been run and saved yet.")

    present = {row[1] for row in conn.execute(f"PRAGMA table_info({TABLE})")}
    missing = [c for c in columns if c not in present]
    if missing:
        raise GisError(f"{TABLE} is missing expected column(s): {', '.join(missing)}")

    total = conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
    if total == 0:
        return AssessmentLookup(
            TABLE_EMPTY,
            f"Table {TABLE} exists but contains no assessments. Assessments with "
            f"insufficient data are not stored, so it stays empty until a complete one is computed.")

    row = conn.execute(
        f"SELECT {', '.join(columns)} FROM {TABLE} WHERE field_id = ? AND crop_id = ? "
        "ORDER BY assessment_date DESC, created_at DESC, id DESC LIMIT 1",
        (field_id, crop_id),
    ).fetchone()
    if row is None:
        return AssessmentLookup(
            NO_MATCH,
            f"{TABLE} has {total} record(s), but none for field '{field_id}' and crop '{crop_id}'.")
    return AssessmentLookup(FOUND, "Latest matching assessment found.", dict(zip(columns, row)))


def find_latest_assessment(db_path, field_id, crop_id):
    db_path = Path(db_path)
    if not db_path.is_file():
        return AssessmentLookup(
            DB_MISSING, f"Database file not found at {_display_path(db_path)}.")
    with contextlib.closing(open_readonly(db_path)) as conn:
        return find_latest_assessment_in(conn, field_id, crop_id, _display_path(db_path))


LIGHT = "light"
DARK = "dark"
POPUP_MAX_HEIGHT = 380          # px; longer popups (many warnings) scroll inside the popup
# Colours for the HTML we add to the map (static values, never database text).
PALETTES = {
    LIGHT: dict(warn_bg="#fff3cd", warn_border="#e0a800", warn_text="inherit",
                bad_bg="#f8d7da", bad_border="#c82333", bad_text="inherit",
                muted="#555", box_bg="white", box_border="#888", box_text="inherit"),
    DARK: dict(warn_bg="#4a3c0f", warn_border="#c9a227", warn_text="#ffe9a6",
               bad_bg="#4a1f24", bad_border="#d6535f", bad_text="#ffd0d4",
               muted="#aab2bd", box_bg="#1f242d", box_border="#566070", box_text="#e6e9ee"),
}

# Both themes use the SAME unmodified OpenStreetMap raster tiles. Inverting or hue-rotating
# those tiles makes the basemap muddy and hard to read, so the dark theme restyles only the
# overlay chrome (popup, tooltip, zoom controls, attribution, scale). A third-party dark
# basemap was not used: CARTO's free dark tiles now return an "API KEY REQUIRED" image.
DARK_MAP_CSS = """<style>
.leaflet-popup-content-wrapper,.leaflet-popup-tip{background:#1f242d;color:#e6e9ee;
  border:1px solid #566070}
.leaflet-popup-close-button{color:#aab2bd !important}
.leaflet-tooltip{background:#1f242d;color:#e6e9ee;border-color:#566070}
.leaflet-bar a,.leaflet-bar a:hover{background:#2b313c;color:#e6e9ee;border-bottom-color:#566070}
.leaflet-control-attribution{background:rgba(31,36,45,.85) !important;color:#aab2bd}
.leaflet-control-attribution a{color:#8ab4f8}
.leaflet-control-scale-line{background:rgba(31,36,45,.75);color:#e6e9ee;border-color:#aab2bd}
</style>"""


def _palette(theme):
    if theme not in PALETTES:
        raise ValueError(f"unknown map theme {theme!r}; use {LIGHT!r} or {DARK!r}")
    return PALETTES[theme]


def _row(label, value_html, muted="#555"):
    return f"<tr><td style='padding-right:10px;color:{muted}'>{esc(label)}</td><td><b>{value_html}</b></td></tr>"


def _banner(p, kind, inner_html, extra_style=""):
    return (f"<div style='background:{p[kind + '_bg']};border:1px solid {p[kind + '_border']};"
            f"color:{p[kind + '_text']};padding:4px 6px;margin:6px 0;{extra_style}'>{inner_html}</div>")


def assessment_html(a, theme=LIGHT):
    p = _palette(theme)
    status = a["status"]
    parts = []
    if status == "PROVISIONAL":
        parts.append(_banner(p, "warn", "<b>PROVISIONAL</b> &ndash; computed from hypothetical placeholder "
                                        "parameters. Not scientifically validated."))
    elif status != "OK":
        parts.append(_banner(p, "bad", f"Status: <b>{esc(status)}</b>"))

    depth = a["irrigation_depth_mm"]
    m = p["muted"]
    rows = [
        _row("Assessment date", esc(a["assessment_date"]), m),
        _row("Crop", esc(a["crop_id"]), m),
        _row("Growth stage", esc(a["growth_stage"]) if a["growth_stage"] else "n/a", m),
        _row("ET0", fmt_number(a["et0_mm"], 2, " mm/day"), m),
        _row("Kc", fmt_number(a["kc"], 2), m),
        _row("ETc", fmt_number(a["etc_mm"], 2, " mm/day"), m),
        _row("Root-zone depletion", fmt_number(a["depletion_mm"], 1, " mm"), m),
        _row("RAW / TAW", f"{fmt_number(a['raw_mm'], 1, ' mm')} / {fmt_number(a['taw_mm'], 1, ' mm')}", m),
        _row("Recommendation", esc(a["recommendation"]), m),
        _row("Net irrigation depth", "n/a" if depth is None else fmt_number(depth, 1, " mm"), m),
        _row("Status", esc(status), m),
    ]
    parts.append("<table style='font-size:12px'>" + "".join(rows) + "</table>")

    warnings = parse_warnings(a["warnings"])
    if warnings:
        items = "".join(f"<li>{esc(w)}</li>" for w in warnings)
        parts.append(f"<div style='font-size:11px;margin-top:6px'><b>Warnings</b><ul style='margin:2px 0 0 16px;padding:0'>{items}</ul></div>")
    parts.append(
        f"<div style='font-size:11px;color:{m};margin-top:6px'>Model-based decision support only; "
        "not validated against field measurements. No irrigation is controlled.</div>")
    return "".join(parts)


def popup_html(study, crop, lookup, notice=None, theme=LIGHT):
    p = _palette(theme)
    head = (
        f"<div style='font-family:sans-serif;font-size:13px;min-width:260px'>"
        f"<h4 style='margin:0 0 4px 0'>{esc(MARKER_TITLE)}</h4>"
        f"<div style='font-size:12px'><b>Representative point &ndash; not a surveyed or physically "
        f"monitored field.</b> No field boundary has been established.</div>"
        f"<div style='font-size:12px;margin-top:4px'>{esc(study.description)}</div>"
        f"<div style='font-size:12px;margin-top:4px'>Field ID: <b>{esc(study.field_id)}</b> &middot; "
        f"Crop: <b>{esc(crop.name)}</b><br>"
        f"Lat/Lon: {study.latitude:.4f}, {study.longitude:.4f}</div><hr style='margin:6px 0'>"
    )
    notice_html = (_banner(p, "warn", f"<b>Not current:</b> {esc(notice)}", "font-size:12px")
                   if notice else "")
    if lookup.state == FOUND:
        body = notice_html + assessment_html(lookup.assessment, theme)
    else:
        body = (f"<div style='font-size:12px'><b>Assessment unavailable.</b><br>{esc(lookup.message)}</div>")
    return head + body + "</div>"


def info_box_html(lookup, notice=None, theme=LIGHT):
    _ = (lookup, notice)
    p = _palette(theme)
    return (
        f"<div style='position:fixed;bottom:58px;left:10px;z-index:9999;background:{p['box_bg']};"
        f"color:{p['box_text']};padding:6px 10px;border:1px solid {p['box_border']};border-radius:4px;"
        "font:12px sans-serif;max-width:min(420px,calc(100% - 60px))'>"
        f"<span style='color:{p['muted']}'>Representative location only; "
        "no surveyed field boundary.</span></div>"
    )


def build_map(config, lookup, notice=None, theme=LIGHT):
    _palette(theme)                                   # validate early
    study, crop = config.study, config.crop
    fmap = folium.Map(
        location=[study.latitude, study.longitude], zoom_start=11,
        tiles=OSM_URL, attr=OSM_ATTRIBUTION, max_zoom=19, control_scale=True,
    )
    if theme == DARK:
        fmap.get_root().header.add_child(folium.Element(DARK_MAP_CSS))
    popup = folium.Popup(folium.Html(popup_html(study, crop, lookup, notice, theme), script=True),
                         max_width=440, max_height=POPUP_MAX_HEIGHT, show=False)
    folium.Marker(
        location=[study.latitude, study.longitude],
        popup=popup, tooltip=MARKER_TITLE, title=MARKER_TITLE,
    ).add_to(fmap)
    fmap.get_root().html.add_child(folium.Element(info_box_html(lookup, notice, theme)))
    return fmap


def generate(db_path=DB_PATH, output_path=OUTPUT_PATH, config=DEFAULT_CONFIG):
    lookup = find_latest_assessment(db_path, config.study.field_id, config.crop.crop_id)
    fmap = build_map(config, lookup)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fmap.save(str(output_path))
    return output_path, lookup


def main():
    try:
        output_path, lookup = generate()
    except GisError as exc:
        print(f"[ERROR] {exc}")
        return 1
    print(f"Map saved to: {output_path}")
    if lookup.state == FOUND:
        a = lookup.assessment
        print(f"Assessment: included ({a['assessment_date']}, {a['crop_id']}, status {a['status']})")
    else:
        print(f"Assessment: NOT included [{lookup.state}] {lookup.message}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
