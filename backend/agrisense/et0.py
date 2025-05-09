"""FAO-56 Penman-Monteith daily grass reference evapotranspiration (ET0).

Reference: Allen et al. (1998), FAO Irrigation and Drainage Paper 56, Chapters
3 and 4. Equation numbers below refer to that document.

    ET0 = [0.408 D (Rn - G) + g (900 / (T + 273)) u2 (es - ea)]
          / [D + g (1 + 0.34 u2)]                                   (Eq. 6)

    ET0  reference evapotranspiration            [mm/day]
    Rn   net radiation at the crop surface       [MJ/m2/day]
    G    soil heat flux (0 for daily steps)      [MJ/m2/day]
    T    mean daily air temperature at 2 m       [deg C]
    u2   wind speed at 2 m height                [m/s]
    es   saturation vapour pressure              [kPa]
    ea   actual vapour pressure                  [kPa]
    D    slope of the saturation curve           [kPa/deg C]
    g    psychrometric constant                  [kPa/deg C]

Inputs are daily Open-Meteo values: model output, not station measurements.
Missing, non-finite or implausible inputs raise; nothing is imputed. Open-Meteo's
own ET0 is never used as input.

TODO: wind is a 10 m model value scaled via Eq. 47 and Rs is model shortwave.
Replace with an on-site 2 m anemometer and a pyranometer / net-radiation
sensor when hardware is deployed.
"""

import math
from dataclasses import dataclass
from datetime import date

from .validation import InvalidInputError, require_number

GSC_MJ_M2_MIN = 0.0820          # solar constant [MJ/m2/min]
STEFAN_BOLTZMANN = 4.903e-9     # [MJ/K4/m2/day]
ALBEDO_GRASS = 0.23             # reference-crop albedo [-]
KELVIN_RADIATION = 273.16       # used in the net longwave term (Eq. 39)
KELVIN_ET0 = 273.0              # used in Eq. 6

ET0_MAX_PLAUSIBLE_MM_DAY = 20.0  # above this, assume bad input


@dataclass(frozen=True)
class ET0Result:

    et0_mm_day: float
    t_mean_c: float
    u2_m_s: float
    delta_kpa_c: float
    gamma_kpa_c: float
    es_kpa: float
    ea_kpa: float
    ra_mj_m2_day: float
    rs_mj_m2_day: float
    rso_mj_m2_day: float
    rns_mj_m2_day: float
    rnl_mj_m2_day: float
    rn_mj_m2_day: float
    clipped_to_zero: bool


def hpa_to_kpa(pressure_hpa):
    """hPa -> kPa (1 kPa = 10 hPa), range-checked."""
    return require_number("pressure", pressure_hpa, 500.0, 1100.0, "hPa") / 10.0


def wind_speed_at_2m(wind_speed_m_s, height_m=10.0):
    """Wind speed at 2 m from a measurement at `height_m` (Eq. 47).

        u2 = uz * 4.87 / ln(67.8 z - 5.42)      [m/s]

    Log wind profile over short grass; zero wind stays zero.
    """
    uz = require_number("wind speed", wind_speed_m_s, 0.0, 50.0, "m/s")
    z = require_number("wind measurement height", height_m, 1.0, 100.0, "m")
    return uz * 4.87 / math.log(67.8 * z - 5.42)


def saturation_vapour_pressure(temperature_c):
    """e0(T) = 0.6108 exp(17.27 T / (T + 237.3))   [kPa] (Eq. 11)."""
    return 0.6108 * math.exp(17.27 * temperature_c / (temperature_c + 237.3))


def mean_saturation_vapour_pressure(tmax_c, tmin_c):
    """es = [e0(Tmax) + e0(Tmin)] / 2   [kPa] (Eq. 12)."""
    return (saturation_vapour_pressure(tmax_c) + saturation_vapour_pressure(tmin_c)) / 2.0


def actual_vapour_pressure(tmax_c, tmin_c, rh_max_pct, rh_min_pct):
    """ea = [e0(Tmin) RHmax/100 + e0(Tmax) RHmin/100] / 2   [kPa] (Eq. 17)."""
    return (
        saturation_vapour_pressure(tmin_c) * rh_max_pct / 100.0
        + saturation_vapour_pressure(tmax_c) * rh_min_pct / 100.0
    ) / 2.0


def slope_saturation_vapour_pressure(t_mean_c):
    """D = 4098 e0(T) / (T + 237.3)^2   [kPa/deg C] (Eq. 13)."""
    return 4098.0 * saturation_vapour_pressure(t_mean_c) / (t_mean_c + 237.3) ** 2


def psychrometric_constant(pressure_kpa):
    """g = 0.665e-3 P   [kPa/deg C] (Eq. 8), with P in kPa."""
    return 0.665e-3 * pressure_kpa


def pressure_from_elevation(elevation_m):
    """P = 101.3 ((293 - 0.0065 z) / 293)^5.26   [kPa] (Eq. 7).

    Reference/tests only; calculate_et0() uses the supplied pressure.
    """
    return 101.3 * ((293.0 - 0.0065 * elevation_m) / 293.0) ** 5.26


def extraterrestrial_radiation(latitude_deg, day):
    """Ra [MJ/m2/day] for latitude and date (Eq. 21-25).

    Ra = (24*60/pi) Gsc dr [ws sin(phi) sin(d) + cos(phi) cos(d) sin(ws)]
    dr = 1 + 0.033 cos(2 pi J / 365),  d = 0.409 sin(2 pi J / 365 - 1.39),
    ws = arccos(-tan(phi) tan(d)),  J = day of year.
    """
    phi = math.radians(latitude_deg)
    j = day.timetuple().tm_yday
    dr = 1.0 + 0.033 * math.cos(2.0 * math.pi * j / 365.0)
    declination = 0.409 * math.sin(2.0 * math.pi * j / 365.0 - 1.39)
    x = max(-1.0, min(1.0, -math.tan(phi) * math.tan(declination)))  # polar day/night
    sunset_angle = math.acos(x)
    return (24.0 * 60.0 / math.pi) * GSC_MJ_M2_MIN * dr * (
        sunset_angle * math.sin(phi) * math.sin(declination)
        + math.cos(phi) * math.cos(declination) * math.sin(sunset_angle)
    )


def clear_sky_radiation(ra_mj_m2_day, elevation_m):
    """Rso = (0.75 + 2e-5 z) Ra   [MJ/m2/day] (Eq. 37)."""
    return (0.75 + 2e-5 * elevation_m) * ra_mj_m2_day


def net_shortwave_radiation(rs_mj_m2_day, albedo=ALBEDO_GRASS):
    """Rns = (1 - a) Rs   [MJ/m2/day] (Eq. 38)."""
    return (1.0 - albedo) * rs_mj_m2_day


def net_longwave_radiation(tmax_c, tmin_c, ea_kpa, rs_mj_m2_day, rso_mj_m2_day):
    """Rnl [MJ/m2/day] (Eq. 39).

    Rnl = s [(Tmax,K^4 + Tmin,K^4)/2] (0.34 - 0.14 sqrt(ea)) (1.35 Rs/Rso - 0.35)
    Rs/Rso is capped at 1.0 per FAO-56.
    """
    tmax_k = tmax_c + KELVIN_RADIATION
    tmin_k = tmin_c + KELVIN_RADIATION
    relative_shortwave = min(rs_mj_m2_day / rso_mj_m2_day, 1.0)
    return (
        STEFAN_BOLTZMANN
        * (tmax_k ** 4 + tmin_k ** 4) / 2.0
        * (0.34 - 0.14 * math.sqrt(ea_kpa))
        * (1.35 * relative_shortwave - 0.35)
    )


def calculate_et0(
    *,
    tmax_c,
    tmin_c,
    rh_max_pct,
    rh_min_pct,
    pressure_kpa,
    wind_speed_m_s,
    solar_radiation_mj_m2_day,
    latitude_deg,
    elevation_m,
    day,
    wind_height_m=10.0,
):
    """FAO-56 Penman-Monteith ET0 for one day (Eq. 6, G = 0).

    Units are in the argument names. Rs is the daily SUM in MJ/m2/day, not
    W/m2. Wind is rescaled from `wind_height_m` to 2 m here. Raises
    MissingInputError / InvalidInputError on missing, non-finite or
    implausible values.

    Negative ET0 (only with strongly negative Rn) is clipped to 0 and flagged
    in `clipped_to_zero`.
    """
    tmax = require_number("Tmax", tmax_c, -50.0, 60.0, "deg C")
    tmin = require_number("Tmin", tmin_c, -50.0, 60.0, "deg C")
    if tmin > tmax:
        raise InvalidInputError(f"Tmin ({tmin}) is greater than Tmax ({tmax})")
    rh_max = require_number("RHmax", rh_max_pct, 0.0, 100.0, "%")
    rh_min = require_number("RHmin", rh_min_pct, 0.0, 100.0, "%")
    if rh_min > rh_max:
        raise InvalidInputError(f"RHmin ({rh_min}) is greater than RHmax ({rh_max})")
    pressure = require_number("pressure", pressure_kpa, 50.0, 110.0, "kPa")
    rs = require_number("solar radiation", solar_radiation_mj_m2_day, 0.0, 45.0, "MJ/m2/day")
    lat = require_number("latitude", latitude_deg, -89.0, 89.0, "deg")
    elevation = require_number("elevation", elevation_m, -430.0, 9000.0, "m")
    if not isinstance(day, date):
        raise InvalidInputError(f"day must be a date, got {day!r}")

    u2 = wind_speed_at_2m(wind_speed_m_s, wind_height_m)

    t_mean = (tmax + tmin) / 2.0
    delta = slope_saturation_vapour_pressure(t_mean)
    gamma = psychrometric_constant(pressure)
    es = mean_saturation_vapour_pressure(tmax, tmin)
    ea = actual_vapour_pressure(tmax, tmin, rh_max, rh_min)

    ra = extraterrestrial_radiation(lat, day)
    rso = clear_sky_radiation(ra, elevation)
    if rso <= 0.0:
        raise InvalidInputError("clear-sky radiation is zero (polar night); ET0 not defined here")
    if rs > ra:
        raise InvalidInputError(
            f"solar radiation {rs} MJ/m2/day exceeds extraterrestrial radiation {ra:.2f}"
        )
    rns = net_shortwave_radiation(rs)
    rnl = net_longwave_radiation(tmax, tmin, ea, rs, rso)
    rn = rns - rnl
    g_flux = 0.0  # daily time step (Eq. 42)

    numerator = (
        0.408 * delta * (rn - g_flux)
        + gamma * (900.0 / (t_mean + KELVIN_ET0)) * u2 * (es - ea)
    )
    denominator = delta + gamma * (1.0 + 0.34 * u2)
    et0 = numerator / denominator

    clipped = et0 < 0.0
    if clipped:
        et0 = 0.0
    if et0 > ET0_MAX_PLAUSIBLE_MM_DAY:
        raise InvalidInputError(f"computed ET0 {et0:.1f} mm/day is implausible; check inputs")

    return ET0Result(
        et0_mm_day=et0,
        t_mean_c=t_mean,
        u2_m_s=u2,
        delta_kpa_c=delta,
        gamma_kpa_c=gamma,
        es_kpa=es,
        ea_kpa=ea,
        ra_mj_m2_day=ra,
        rs_mj_m2_day=rs,
        rso_mj_m2_day=rso,
        rns_mj_m2_day=rns,
        rnl_mj_m2_day=rnl,
        rn_mj_m2_day=rn,
        clipped_to_zero=clipped,
    )
