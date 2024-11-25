"""Study configuration for the physics layer.

Every value marked "ASSUMPTION" is a placeholder, not a measured property of a
real farm. The location is a representative point near 6th of October City,
Egypt, not a monitored field. Calculation code takes everything via AppConfig,
so swapping in verified data needs no code change.

Sources
-------
FAO-56: Allen, R.G., Pereira, L.S., Raes, D., Smith, M. (1998). Crop
evapotranspiration - Guidelines for computing crop water requirements.
FAO Irrigation and Drainage Paper 56. https://www.fao.org/4/X0490E/x0490e00.htm
"""

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .validation import AgriSenseError, ConfigError

TIMEZONE = "Africa/Cairo"


def local_today(timezone_name=TIMEZONE, now=None):
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        raise AgriSenseError(
            f"time zone database has no '{timezone_name}'. On Windows run: pip install tzdata")
    return (now.astimezone(zone) if now else datetime.now(zone)).date()

# Tag on daily rows aggregated over Africa/Cairo days. Legacy rows aggregated
# over UTC days carry plain 'open-meteo'; the tag keeps them out of the engine.
DAILY_WEATHER_API_SOURCE = f"open-meteo;tz={TIMEZONE}"


@dataclass(frozen=True)
class StudyConfig:
    field_id: str = "field_001"
    description: str = (
        "Representative agricultural study location near 6th of October City, "
        "Egypt. Not a surveyed or physically monitored farm."
    )
    latitude: float = 29.97
    longitude: float = 30.94
    timezone: str = TIMEZONE

    # PROVISIONAL: Open-Meteo grid-cell elevation (response "elevation" = 193.0 m,
    # fetched 2026-11-14). Its surface pressure refers to this same model
    # elevation, so staying consistent with it matters more than a surveyed value.
    elevation_m: float = 193.0

    # Open-Meteo wind_speed_10m is at 10 m; scaled to 2 m (FAO-56 Eq. 47).
    wind_measurement_height_m: float = 10.0

    # ASSUMPTION: not a planting record. FAO-56 Table 11 lists November planting
    # for Mediterranean winter wheat.
    planting_date: date = date(2026, 11, 13)

    # Balance starts here with `initial_depletion_mm`; never inferred from the
    # first weather record.
    simulation_start_date: date = date(2026, 11, 13)

    # Flip to False only after every placeholder above is replaced by real data.
    parameters_are_assumptions: bool = True

    def __post_init__(self):
        if not self.field_id.strip():
            raise ConfigError("field_id must not be empty")
        if not -89.0 <= self.latitude <= 89.0:
            raise ConfigError("latitude must be between -89 and 89 degrees")
        if not -180.0 <= self.longitude <= 180.0:
            raise ConfigError("longitude must be between -180 and 180 degrees")
        if not -430.0 <= self.elevation_m <= 9000.0:
            raise ConfigError("elevation_m is outside -430..9000 m")
        if not 1.0 <= self.wind_measurement_height_m <= 100.0:
            raise ConfigError("wind_measurement_height_m must be between 1 and 100 m")
        if self.simulation_start_date < self.planting_date:
            raise ConfigError("simulation_start_date must not be before planting_date")


@dataclass(frozen=True)
class CropConfig:

    crop_id: str = "winter_wheat"
    name: str = "Winter Wheat"
    kc_ini: float = 0.70
    kc_mid: float = 1.15
    kc_end: float = 0.25
    initial_days: int = 30
    development_days: int = 140
    mid_days: int = 40
    late_days: int = 30
    depletion_fraction_p: float = 0.55
    root_depth_initial_m: float = 0.25
    root_depth_max_m: float = 1.5

    def __post_init__(self):
        for name in ("kc_ini", "kc_mid", "kc_end"):
            if not 0.0 < getattr(self, name) <= 2.0:
                raise ConfigError(f"{name} must be in (0, 2]")
        for name in ("initial_days", "development_days", "mid_days", "late_days"):
            if not isinstance(getattr(self, name), int) or getattr(self, name) < 1:
                raise ConfigError(f"{name} must be a positive integer number of days")
        if not 0.0 < self.depletion_fraction_p < 1.0:
            raise ConfigError("depletion_fraction_p must be between 0 and 1 (exclusive)")
        if not 0.0 < self.root_depth_initial_m <= self.root_depth_max_m:
            raise ConfigError("root depths must satisfy 0 < initial <= max")
        if self.root_depth_max_m > 5.0:
            raise ConfigError("root_depth_max_m is implausibly large (> 5 m)")

    @property
    def season_days(self):
        return self.initial_days + self.development_days + self.mid_days + self.late_days


@dataclass(frozen=True)
class SoilConfig:
    """Root-zone soil parameters.

    ASSUMPTION: no soil survey exists for this location. FC 0.23 and WP 0.11
    m3/m3 are mid-points of the FAO-56 Table 19 sandy-loam ranges (theta_FC
    0.18-0.28, theta_WP 0.06-0.16), i.e. 120 mm available water per metre.
    Initial depletion 0 mm = root zone at field capacity on the start date
    (e.g. pre-sowing irrigation). None of this is measured.

    FIXME: placeholders until gravimetric soil sampling and a field survey
    exist. The water balance is only as good as FC/WP, root depths and stage
    lengths.
    """

    field_capacity_m3_m3: float = 0.23
    wilting_point_m3_m3: float = 0.11
    initial_depletion_mm: float = 0.0

    def __post_init__(self):
        if not 0.0 < self.wilting_point_m3_m3 < self.field_capacity_m3_m3 <= 0.6:
            raise ConfigError("soil water contents must satisfy 0 < WP < FC <= 0.6 (m3/m3)")
        if self.initial_depletion_mm < 0.0:
            raise ConfigError("initial_depletion_mm must not be negative")


@dataclass(frozen=True)
class AppConfig:
    study: StudyConfig = field(default_factory=StudyConfig)
    crop: CropConfig = field(default_factory=CropConfig)
    soil: SoilConfig = field(default_factory=SoilConfig)


DEFAULT_CONFIG = AppConfig()

ASSUMPTION_NOTES = (
    "Open-Meteo values are model-derived weather data, not weather-station measurements.",
    "Planting date, simulation start, initial depletion, soil water contents and root depths "
    "are hypothetical placeholders unless replaced by verified data.",
    "Elevation is the Open-Meteo grid-cell elevation, not a surveyed value.",
    "Growth stages are calendar-day model assumptions, not observations of crop development.",
    "Rainfall is treated as fully effective (no runoff, interception or capillary rise).",
    "The ESP32 potentiometer value is a simulated, uncalibrated observation and is not an input.",
)


def config_to_dict(config, irrigation_events=None):
    data = asdict(config)
    data["irrigation_events_mm"] = {
        d.isoformat(): float(mm) for d, mm in sorted((irrigation_events or {}).items())
    }
    return json.loads(json.dumps(data, default=str))


def config_fingerprint(config, irrigation_events=None):
    payload = json.dumps(config_to_dict(config, irrigation_events), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
