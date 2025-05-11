"""Simplified FAO-56 root-zone soil-water balance (depletion method).

Reference: Allen et al. (1998), FAO-56 Chapter 8.

    TAW = 1000 (theta_FC - theta_WP) Zr           (Eq. 82)  [mm]
    RAW = p TAW                                   (Eq. 83)  [mm]
    Ks  = (TAW - Dr) / ((1 - p) TAW)  if Dr > RAW, else 1    (Eq. 84)
    ETc_adj = Ks ETc                              (Eq. 81)  [mm/day]
    Dr,i = Dr,i-1 - P - I + ETc_adj               (Eq. 85, simplified)

    Dr  root-zone depletion: water missing relative to field capacity [mm]
        (0 = field capacity, TAW = wilting point)
    Zr  root depth [m],  theta_FC / theta_WP  [m3/m3],  p depletion fraction [-]
    P   daily precipitation [mm],  I  net irrigation depth [mm]

Limitations (deliberate simplifications):
  * Rainfall 100 % effective: RO = 0, no interception loss.
  * CR = 0, no groundwater contribution.
  * Water above field capacity is lost as deep percolation, not stored
    (Dr floored at 0).
  * Dr capped at TAW (wilting point).
  * Dr in mm is unchanged as roots deepen: new soil is assumed at field capacity.
  * p constant (Table 22 value for ETc ~ 5 mm/day; no ETc adjustment).
  * Root depth is linear from `root_depth_initial_m` at planting to
    `root_depth_max_m` at end of development (assumed schedule).
  * Irrigation counts only if explicitly supplied. The ESP32 potentiometer
    "soil moisture" percentage is not used: simulated and uncalibrated, not a
    rain, irrigation or water-volume measurement.
"""

from dataclasses import dataclass
from datetime import date, timedelta

from . import crop_model
from .validation import InvalidInputError, require_number, ConfigError


@dataclass(frozen=True)
class DailyForcing:

    day: date
    et0_mm: float
    precipitation_mm: float


@dataclass(frozen=True)
class DepletionStep:
    depletion_mm: float      # Dr at end of day
    ks: float                # water stress coefficient used
    etc_adjusted_mm: float   # Ks x ETc
    excess_water_mm: float   # water beyond field capacity (lost, not stored)
    capped_at_taw: bool      # True if Dr would have exceeded TAW


@dataclass(frozen=True)
class DayState:
    day: date
    days_after_planting: int
    stage: str
    kc: float
    et0_mm: float
    etc_mm: float
    ks: float
    etc_adjusted_mm: float
    precipitation_mm: float
    irrigation_mm: float
    root_depth_m: float
    taw_mm: float
    raw_mm: float
    depletion_start_mm: float
    depletion_end_mm: float
    excess_water_mm: float
    capped_at_taw: bool


def total_available_water(field_capacity, wilting_point, root_depth_m):
    """TAW = 1000 (theta_FC - theta_WP) Zr   [mm] (Eq. 82)."""
    fc = require_number("field capacity", field_capacity, 0.0, 0.6, "m3/m3")
    wp = require_number("wilting point", wilting_point, 0.0, 0.6, "m3/m3")
    zr = require_number("root depth", root_depth_m, 0.0, 5.0, "m")
    if wp >= fc:
        raise InvalidInputError("wilting point must be below field capacity")
    return 1000.0 * (fc - wp) * zr


def readily_available_water(depletion_fraction, taw_mm):
    """RAW = p TAW   [mm] (Eq. 83)."""
    p = require_number("depletion fraction p", depletion_fraction, 0.0, 1.0)
    taw = require_number("TAW", taw_mm, 0.0, 5000.0, "mm")
    return p * taw


def root_depth(crop, dap):
    """Assumed root depth [m]; linear until end of development stage."""
    growth_days = crop.initial_days + crop.development_days
    fraction = min(max(dap, 0) / growth_days, 1.0)
    return crop.root_depth_initial_m + fraction * (crop.root_depth_max_m - crop.root_depth_initial_m)


def stress_coefficient(depletion_mm, taw_mm, depletion_fraction):
    """Ks (Eq. 84): 1 while Dr <= RAW, then decreasing linearly to 0 at TAW."""
    raw = depletion_fraction * taw_mm
    if depletion_mm <= raw:
        return 1.0
    return max(0.0, (taw_mm - depletion_mm) / ((1.0 - depletion_fraction) * taw_mm))


def update_depletion(depletion_start_mm, etc_mm, precipitation_mm, irrigation_mm,
                     taw_mm, depletion_fraction):
    """One-day Dr update; equation in module docstring."""
    dr0 = require_number("depletion", depletion_start_mm, 0.0, 5000.0, "mm")
    etc = require_number("ETc", etc_mm, 0.0, 30.0, "mm/day")
    rain = require_number("precipitation", precipitation_mm, 0.0, 500.0, "mm")
    irrigation = require_number("irrigation", irrigation_mm, 0.0, 500.0, "mm")
    taw = require_number("TAW", taw_mm, 0.0, 5000.0, "mm")
    if taw <= 0.0:
        raise InvalidInputError("TAW must be positive")
    dr0 = min(dr0, taw)

    ks = stress_coefficient(dr0, taw, depletion_fraction)
    etc_adjusted = ks * etc
    unbounded = dr0 - rain - irrigation + etc_adjusted
    return DepletionStep(
        depletion_mm=min(max(unbounded, 0.0), taw),
        ks=ks,
        etc_adjusted_mm=etc_adjusted,
        excess_water_mm=max(-unbounded, 0.0),
        capped_at_taw=unbounded > taw,
    )


def run_water_balance(config, forcings, irrigation_events=None):
    """Run the balance day by day from `config.study.simulation_start_date`.

    `forcings` must be consecutive days starting on the start date; gaps raise,
    nothing is interpolated. `irrigation_events` maps date -> net depth [mm].
    Returns one DayState per day.
    """
    study, crop, soil = config.study, config.crop, config.soil
    irrigation_events = irrigation_events or {}
    if not forcings:
        raise InvalidInputError("no daily weather forcing supplied")

    states = []
    depletion = float(soil.initial_depletion_mm)
    expected_day = study.simulation_start_date
    for forcing in forcings:
        if forcing.day != expected_day:
            raise InvalidInputError(
                f"weather forcing is not consecutive: expected {expected_day}, got {forcing.day}"
            )
        dap = crop_model.days_after_planting(study.planting_date, forcing.day)
        stage = crop_model.growth_stage(crop, dap)
        kc = crop_model.crop_coefficient(crop, dap)
        etc = crop_model.crop_evapotranspiration(kc, forcing.et0_mm)
        zr = root_depth(crop, dap)
        taw = total_available_water(soil.field_capacity_m3_m3, soil.wilting_point_m3_m3, zr)
        raw = readily_available_water(crop.depletion_fraction_p, taw)
        if not states and depletion > taw:
            raise ConfigError(
                f"initial_depletion_mm ({depletion}) exceeds TAW ({taw:.1f} mm) on the start date"
            )
        irrigation = float(irrigation_events.get(forcing.day, 0.0))
        step = update_depletion(depletion, etc, forcing.precipitation_mm, irrigation,
                                taw, crop.depletion_fraction_p)
        states.append(DayState(
            day=forcing.day, days_after_planting=dap, stage=stage, kc=kc,
            et0_mm=forcing.et0_mm, etc_mm=etc, ks=step.ks,
            etc_adjusted_mm=step.etc_adjusted_mm,
            precipitation_mm=forcing.precipitation_mm, irrigation_mm=irrigation,
            root_depth_m=zr, taw_mm=taw, raw_mm=raw,
            depletion_start_mm=depletion, depletion_end_mm=step.depletion_mm,
            excess_water_mm=step.excess_water_mm, capped_at_taw=step.capped_at_taw,
        ))
        depletion = step.depletion_mm
        expected_day = forcing.day + timedelta(days=1)
    return states
