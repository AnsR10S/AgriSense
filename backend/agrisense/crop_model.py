"""Winter wheat crop evapotranspiration: ETc = Kc x ET0 (FAO-56 Eq. 58).

Stage and Kc come from days since a configurable planting date and configurable
stage lengths. Calendar-day model assumptions, not observed crop development.

Kc follows the FAO-56 single-crop-coefficient curve (Fig. 34, Eq. 66):
    initial       Kc = Kc ini (constant)
    development   Kc rises linearly from Kc ini to Kc mid
    mid-season    Kc = Kc mid (constant)
    late-season   Kc falls linearly from Kc mid to Kc end

Coefficient values and sources: config.CropConfig.
"""

from datetime import date

from .validation import OutOfSeasonError, require_number

INITIAL = "initial"
DEVELOPMENT = "development"
MID_SEASON = "mid_season"
LATE_SEASON = "late_season"
STAGES = (INITIAL, DEVELOPMENT, MID_SEASON, LATE_SEASON)


def days_after_planting(planting_date, day):
    if not isinstance(planting_date, date) or not isinstance(day, date):
        raise TypeError("planting_date and day must be dates")
    return (day - planting_date).days


def growth_stage(crop, dap):
    if dap < 0:
        raise OutOfSeasonError(f"date is {-dap} day(s) before planting")
    if dap < crop.initial_days:
        return INITIAL
    if dap < crop.initial_days + crop.development_days:
        return DEVELOPMENT
    if dap < crop.initial_days + crop.development_days + crop.mid_days:
        return MID_SEASON
    if dap < crop.season_days:
        return LATE_SEASON
    raise OutOfSeasonError(
        f"day {dap} after planting is beyond the modelled {crop.season_days}-day season"
    )


def crop_coefficient(crop, dap):
    stage = growth_stage(crop, dap)
    if stage == INITIAL:
        return crop.kc_ini
    if stage == DEVELOPMENT:
        fraction = (dap - crop.initial_days) / crop.development_days
        return crop.kc_ini + fraction * (crop.kc_mid - crop.kc_ini)
    if stage == MID_SEASON:
        return crop.kc_mid
    late_start = crop.initial_days + crop.development_days + crop.mid_days
    fraction = (dap - late_start) / crop.late_days
    return crop.kc_mid + fraction * (crop.kc_end - crop.kc_mid)


def crop_evapotranspiration(kc, et0_mm_day):
    kc = require_number("Kc", kc, 0.0, 2.0)
    et0 = require_number("ET0", et0_mm_day, 0.0, 20.0, "mm/day")
    return kc * et0
