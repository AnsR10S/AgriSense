"""Daily assessment for one field. Writes `daily_assessments`.

Pipeline: stored daily weather -> FAO-56 ET0 -> wheat ETc -> root-zone water
balance from the configured start date -> recommendation -> SQLite.

Run from backend/:  python -m agrisense.assessment

Decision support only; nothing drives a pump. Weather is Open-Meteo model
data. The simulated ESP32 soil value is echoed for context and is never a
model input.

TODO: no ground truth yet. Once a calibrated probe is installed, compare
modelled depletion against measured soil water before trusting any output.
"""

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from . import crop_model, irrigation, water_balance
from .config import (
    ASSUMPTION_NOTES,
    DAILY_WEATHER_API_SOURCE,
    DEFAULT_CONFIG,
    config_fingerprint,
    config_to_dict,
    local_today,
)
from .et0 import calculate_et0, hpa_to_kpa
from .validation import AgriSenseError, InvalidInputError, OutOfSeasonError, require_number

DB_PATH = Path(__file__).resolve().parent.parent / "agrisense.db"
DB_BUSY_TIMEOUT_SECONDS = 30
MIN_DAYS_FOR_CONFIDENCE = 7  # shorter runs are dominated by the assumed initial depletion

STATUS_OK = "OK"
STATUS_PROVISIONAL = "PROVISIONAL"
STATUS_INSUFFICIENT = "INSUFFICIENT_DATA"

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS daily_assessments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    field_id TEXT NOT NULL,
    assessment_date TEXT NOT NULL,
    crop_id TEXT NOT NULL,
    growth_stage TEXT,
    days_after_planting INTEGER,
    et0_mm REAL,
    kc REAL,
    etc_mm REAL,
    ks REAL,
    root_depth_m REAL,
    taw_mm REAL,
    raw_mm REAL,
    depletion_mm REAL,
    recommendation TEXT NOT NULL,
    irrigation_depth_mm REAL,
    status TEXT NOT NULL,
    reasoning TEXT NOT NULL,
    openmeteo_et0_mm REAL,
    simulation_start_date TEXT NOT NULL,
    warnings TEXT,
    config_hash TEXT NOT NULL,
    config_json TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(field_id, assessment_date, crop_id, config_hash)
);
"""

INSERT_SQL = """
INSERT OR IGNORE INTO daily_assessments (
    field_id, assessment_date, crop_id, growth_stage, days_after_planting,
    et0_mm, kc, etc_mm, ks, root_depth_m, taw_mm, raw_mm, depletion_mm,
    recommendation, irrigation_depth_mm, status, reasoning, openmeteo_et0_mm,
    simulation_start_date, warnings, config_hash, config_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
"""


@dataclass
class Assessment:
    field_id: str
    assessment_date: date
    crop_id: str
    simulation_start_date: date
    config_hash: str
    config_json: str
    recommendation: str = irrigation.INSUFFICIENT_DATA
    status: str = STATUS_INSUFFICIENT
    reasoning: str = ""
    growth_stage: str = None
    days_after_planting: int = None
    et0_mm: float = None
    kc: float = None
    etc_mm: float = None
    ks: float = None
    root_depth_m: float = None
    taw_mm: float = None
    raw_mm: float = None
    depletion_mm: float = None
    irrigation_depth_mm: float = None
    openmeteo_et0_mm: float = None  # comparison only; never an input
    warnings: list = field(default_factory=list)


def init_db(db_path=DB_PATH):
    conn = sqlite3.connect(str(db_path), timeout=DB_BUSY_TIMEOUT_SECONDS)
    try:
        conn.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_SECONDS * 1000}")
        with conn:
            conn.execute(CREATE_TABLE_SQL)
    except sqlite3.Error:
        conn.close()
        raise
    return conn


def save_assessment(conn, assessment):
    if assessment.status == STATUS_INSUFFICIENT:
        raise ValueError("INSUFFICIENT_DATA assessments are not stored")
    with conn:
        cursor = conn.execute(INSERT_SQL, (
            assessment.field_id, assessment.assessment_date.isoformat(), assessment.crop_id,
            assessment.growth_stage, assessment.days_after_planting,
            assessment.et0_mm, assessment.kc, assessment.etc_mm, assessment.ks,
            assessment.root_depth_m, assessment.taw_mm, assessment.raw_mm,
            assessment.depletion_mm, assessment.recommendation,
            assessment.irrigation_depth_mm, assessment.status, assessment.reasoning,
            assessment.openmeteo_et0_mm, assessment.simulation_start_date.isoformat(),
            json.dumps(assessment.warnings), assessment.config_hash, assessment.config_json,
        ))
        return cursor.rowcount == 1


def load_weather_rows(conn, field_id, start, end):
    columns = (
        "date, temperature_max_c, temperature_min_c, humidity_max_pct, humidity_min_pct, "
        "surface_pressure_mean_hpa, wind_speed_10m_mean_m_s, solar_radiation_sum_mj_m2, "
        "precipitation_sum_mm, openmeteo_et0_mm"
    )
    cursor = conn.execute(
        f"SELECT {columns} FROM daily_weather_observations "
        "WHERE field_id = ? AND date BETWEEN ? AND ? AND api_source = ?",
        (field_id, start.isoformat(), end.isoformat(), DAILY_WEATHER_API_SOURCE),
    )
    names = [c[0] for c in cursor.description]
    rows = {r[0]: dict(zip(names, r)) for r in cursor.fetchall()}
    other = conn.execute(
        "SELECT COUNT(*) FROM daily_weather_observations "
        "WHERE field_id = ? AND date BETWEEN ? AND ? AND api_source != ?",
        (field_id, start.isoformat(), end.isoformat(), DAILY_WEATHER_API_SOURCE),
    ).fetchone()[0]
    return rows, other


def latest_simulated_soil_moisture(conn, field_id):
    """Latest valid simulated soil reading, display only. None if absent.

    FIXME: raw potentiometer percentage, not VWC. Needs a per-probe
    dry/wet calibration (capacitive probe, soil-specific) before it can be
    compared with TAW/depletion.
    """
    try:
        row = conn.execute(
            "SELECT timestamp, soil_moisture_pct FROM sensor_readings "
            "WHERE field_id = ? AND is_valid = 1 AND soil_moisture_pct IS NOT NULL "
            "ORDER BY timestamp DESC LIMIT 1", (field_id,)).fetchone()
    except sqlite3.Error:
        return None
    return {"timestamp": row[0], "soil_moisture_pct": row[1]} if row else None


def forcing_from_row(row, config):
    study = config.study
    pressure_hpa = row["surface_pressure_mean_hpa"]
    result = calculate_et0(
        tmax_c=row["temperature_max_c"],
        tmin_c=row["temperature_min_c"],
        rh_max_pct=row["humidity_max_pct"],
        rh_min_pct=row["humidity_min_pct"],
        pressure_kpa=None if pressure_hpa is None else hpa_to_kpa(pressure_hpa),
        wind_speed_m_s=row["wind_speed_10m_mean_m_s"],
        solar_radiation_mj_m2_day=row["solar_radiation_sum_mj_m2"],
        latitude_deg=study.latitude,
        elevation_m=study.elevation_m,
        day=date.fromisoformat(row["date"]),
        wind_height_m=study.wind_measurement_height_m,
    )
    rain = require_number("precipitation", row["precipitation_sum_mm"], 0.0, 500.0, "mm")
    forcing = water_balance.DailyForcing(date.fromisoformat(row["date"]), result.et0_mm_day, rain)
    return forcing


def _insufficient(assessment, reason):
    decision = irrigation.insufficient_data(reason)
    assessment.recommendation = decision.recommendation
    assessment.status = STATUS_INSUFFICIENT
    assessment.reasoning = decision.reasoning
    return assessment


def assess(conn, config=DEFAULT_CONFIG, assessment_date=None, irrigation_events=None, today=None):
    study, crop = config.study, config.crop
    irrigation_events = dict(irrigation_events or {})
    if today is None:
        today = local_today(study.timezone)
    if assessment_date is None:
        assessment_date = today - timedelta(days=1)

    a = Assessment(
        field_id=study.field_id, assessment_date=assessment_date, crop_id=crop.crop_id,
        simulation_start_date=study.simulation_start_date,
        config_hash=config_fingerprint(config, irrigation_events),
        config_json=json.dumps({"parameters": config_to_dict(config, irrigation_events),
                                "notes": list(ASSUMPTION_NOTES),
                                "weather_api_source": DAILY_WEATHER_API_SOURCE}, sort_keys=True),
    )

    # --- preconditions -----------------------------------------------------
    if assessment_date >= today:
        return _insufficient(a, f"{assessment_date} is not a completed day in {study.timezone} "
                                f"(local date today: {today}).")
    if assessment_date < study.simulation_start_date:
        return _insufficient(a, f"{assessment_date} is before the water-balance simulation start "
                                f"({study.simulation_start_date}).")
    dap = crop_model.days_after_planting(study.planting_date, assessment_date)
    try:
        a.growth_stage = crop_model.growth_stage(crop, dap)
    except OutOfSeasonError as exc:
        return _insufficient(a, f"outside the modelled crop cycle ({exc}).")
    a.days_after_planting = dap

    # --- weather coverage: every day from sim start must exist ------------
    try:
        rows, other_source = load_weather_rows(
            conn, study.field_id, study.simulation_start_date, assessment_date)
    except sqlite3.Error as exc:
        return _insufficient(a, f"daily weather could not be read from the database ({exc}).")

    forcings, problems = [], []
    day = study.simulation_start_date
    while day <= assessment_date:
        row = rows.get(day.isoformat())
        if row is None:
            problems.append(f"{day}: no {study.timezone} daily weather record")
        else:
            try:
                forcings.append(forcing_from_row(row, config))
            except InvalidInputError as exc:
                problems.append(f"{day}: {exc}")
        day += timedelta(days=1)
    if problems:
        shown = "; ".join(problems[:5]) + (f"; ... and {len(problems) - 5} more" if len(problems) > 5 else "")
        extra = (f" {other_source} stored row(s) in this period use another day definition "
                 f"(legacy UTC-day aggregates) and were not used." if other_source else "")
        return _insufficient(
            a, f"weather coverage is incomplete or invalid from the simulation start "
               f"{study.simulation_start_date} to {assessment_date} ({len(problems)} problem day(s)): "
               f"{shown}.{extra} Historical weather is never invented or back-filled here.")

    # --- water balance -----------------------------------------------------
    window = {d: mm for d, mm in irrigation_events.items()
              if study.simulation_start_date <= d <= assessment_date}
    for d in sorted(set(irrigation_events) - set(window)):
        a.warnings.append(f"Irrigation event on {d} is outside the simulated period and was ignored.")
    try:
        states = water_balance.run_water_balance(config, forcings, window)
    except InvalidInputError as exc:
        return _insufficient(a, f"invalid input to the water balance ({exc}).")
    final = states[-1]

    decision = irrigation.decide(final.depletion_end_mm, final.raw_mm, final.taw_mm, final.etc_mm)
    a.recommendation = decision.recommendation
    a.irrigation_depth_mm = decision.net_depth_mm
    a.et0_mm, a.kc, a.etc_mm, a.ks = final.et0_mm, final.kc, final.etc_mm, final.ks
    a.root_depth_m, a.taw_mm, a.raw_mm = final.root_depth_m, final.taw_mm, final.raw_mm
    a.depletion_mm = final.depletion_end_mm
    a.openmeteo_et0_mm = rows[assessment_date.isoformat()]["openmeteo_et0_mm"]

    # --- warnings, status, reasoning ---------------------------------------
    if study.parameters_are_assumptions:
        a.warnings.append("Planting date, initial depletion, soil water contents, root depths and "
                          "elevation are hypothetical placeholders, not measured field properties.")
    lag = (study.simulation_start_date - study.planting_date).days
    if lag > 0:
        a.warnings.append(f"The simulation starts {lag} day(s) after planting; the water balance "
                          f"before {study.simulation_start_date} is not reconstructed.")
    if len(states) < MIN_DAYS_FOR_CONFIDENCE:
        a.warnings.append(f"Only {len(states)} day(s) simulated (< {MIN_DAYS_FOR_CONFIDENCE}); "
                          f"depletion mostly reflects the assumed initial depletion.")
    if any(s.capped_at_taw for s in states):
        a.warnings.append("Depletion reached TAW (wilting point) on at least one day and was capped.")
    a.status = STATUS_PROVISIONAL if a.warnings else STATUS_OK

    parts = [
        f"{crop.name}, {a.growth_stage} stage, day {dap} after planting "
        f"(calendar-based model assumption).",
        f"ET0 = {a.et0_mm:.2f} mm/day (FAO-56 Penman-Monteith from Open-Meteo model data), "
        f"Kc = {a.kc:.2f}, ETc = {a.etc_mm:.2f} mm/day.",
    ]
    if final.ks < 1.0:
        parts.append(f"Water stress coefficient Ks = {final.ks:.2f}: actual crop ET is reduced.")
    parts.append(decision.reasoning)
    if a.openmeteo_et0_mm is not None and a.openmeteo_et0_mm > 0:
        diff = (a.et0_mm - a.openmeteo_et0_mm) / a.openmeteo_et0_mm * 100.0
        parts.append(f"For comparison only (not used): Open-Meteo ET0 = {a.openmeteo_et0_mm:.2f} "
                     f"mm/day; ours differs by {diff:+.0f} %.")
    parts.append(f"Water balance simulated for {len(states)} day(s) from {study.simulation_start_date} "
                 f"with an initial depletion of {config.soil.initial_depletion_mm:.1f} mm.")
    a.reasoning = " ".join(parts)
    return a


def format_assessment(a):
    def fmt(value, unit="", digits=2):
        return "n/a" if value is None else f"{value:.{digits}f}{unit}"

    lines = [
        "=" * 64,
        f"AgriSense assessment  |  field {a.field_id}  |  {a.assessment_date}",
        "=" * 64,
        f"Crop / stage     : {a.crop_id} / {a.growth_stage or 'n/a'}"
        + ("" if a.days_after_planting is None else f" (day {a.days_after_planting} after planting)"),
        f"ET0              : {fmt(a.et0_mm, ' mm/day')}",
        f"Kc               : {fmt(a.kc)}",
        f"ETc              : {fmt(a.etc_mm, ' mm/day')}",
        f"Root depth       : {fmt(a.root_depth_m, ' m')}",
        f"TAW / RAW        : {fmt(a.taw_mm, ' mm', 1)} / {fmt(a.raw_mm, ' mm', 1)}",
        f"Depletion (Dr)   : {fmt(a.depletion_mm, ' mm', 1)}",
        f"Recommendation   : {a.recommendation}",
        f"Net irrigation   : {fmt(a.irrigation_depth_mm, ' mm', 1)}",
        f"Status           : {a.status}",
        f"Reasoning        : {a.reasoning}",
    ]
    for w in a.warnings:
        lines.append(f"Warning          : {w}")
    lines.append("Note             : decision support only; no irrigation is controlled or applied.")
    return "\n".join(lines)


def parse_irrigation(items):
    events = {}
    for item in items or []:
        try:
            day_text, mm_text = item.split("=", 1)
            events[date.fromisoformat(day_text)] = require_number("irrigation", float(mm_text), 0.0, 500.0, "mm")
        except (ValueError, InvalidInputError) as exc:
            raise argparse.ArgumentTypeError(f"invalid --irrigation '{item}' (use YYYY-MM-DD=mm): {exc}")
    return events


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run one AgriSense agricultural assessment.")
    parser.add_argument("--date", type=date.fromisoformat,
                        help="completed local day to assess (default: yesterday, Africa/Cairo)")
    parser.add_argument("--db", default=str(DB_PATH), help="SQLite database (default: backend/agrisense.db)")
    parser.add_argument("--irrigation", action="append", metavar="YYYY-MM-DD=MM",
                        help="net irrigation actually applied (mm); may be repeated")
    args = parser.parse_args(argv)
    try:
        events = parse_irrigation(args.irrigation)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))

    try:
        conn = init_db(args.db)
    except sqlite3.Error as exc:
        print(f"[DB ERROR] Could not open database {args.db}: {exc}")
        return 1
    try:
        try:
            a = assess(conn, DEFAULT_CONFIG, args.date, events)
        except AgriSenseError as exc:
            print(f"[ERROR] {exc}")
            return 2
        print(format_assessment(a))
        sensor = latest_simulated_soil_moisture(conn, a.field_id)
        if sensor:
            print(f"[INFO] Latest SIMULATED potentiometer reading: {sensor['soil_moisture_pct']} % at "
                  f"{sensor['timestamp']} (uncalibrated; NOT used in this calculation; qualitative "
                  f"comparison only).")
        if a.status == STATUS_INSUFFICIENT:
            print("[NOT SAVED] Insufficient data: nothing was written to the database.")
        else:
            try:
                inserted = save_assessment(conn, a)
            except sqlite3.Error as exc:
                print(f"[DB ERROR] Could not save assessment: {exc}")
                return 1
            print("[INSERTED] Assessment saved to daily_assessments." if inserted else
                  "[DUPLICATE] An assessment with identical date and settings already exists; not saved again.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
