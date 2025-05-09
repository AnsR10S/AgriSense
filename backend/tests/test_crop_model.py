"""Tests for agrisense.crop_model and the crop/study configuration.

Coefficient values under test come from FAO-56 (Allen et al., 1998):
Table 12 (winter wheat, non-frozen soils: Kc ini 0.7, Kc mid 1.15, Kc end 0.25-0.4)
and Table 11 (winter wheat, November planting, Mediterranean: 30/140/40/30 days).
"""

import math
import unittest
from dataclasses import replace
from datetime import date

from agrisense import crop_model
from agrisense.config import CropConfig, SoilConfig, StudyConfig
from agrisense.validation import ConfigError, InvalidInputError, MissingInputError, OutOfSeasonError

CROP = CropConfig()  # 30 / 140 / 40 / 30 days; Kc 0.7 / 1.15 / 0.25


class DefaultsDocumentedFromFAO56Tests(unittest.TestCase):
    def test_default_coefficients_match_fao56_table_12(self):
        self.assertEqual(CROP.kc_ini, 0.70)
        self.assertEqual(CROP.kc_mid, 1.15)
        self.assertTrue(0.25 <= CROP.kc_end <= 0.40)

    def test_default_stage_lengths_match_fao56_table_11(self):
        self.assertEqual((CROP.initial_days, CROP.development_days, CROP.mid_days, CROP.late_days),
                         (30, 140, 40, 30))
        self.assertEqual(CROP.season_days, 240)

    def test_default_planting_date_is_labelled_as_assumption(self):
        study = StudyConfig()
        self.assertTrue(study.parameters_are_assumptions)
        self.assertEqual(study.planting_date, date(2026, 11, 13))


class DaysAfterPlantingTests(unittest.TestCase):
    def test_counts_from_the_configured_planting_date(self):
        planting = date(2026, 11, 15)
        self.assertEqual(crop_model.days_after_planting(planting, date(2026, 11, 15)), 0)
        self.assertEqual(crop_model.days_after_planting(planting, date(2026, 12, 15)), 30)
        self.assertEqual(crop_model.days_after_planting(planting, date(2026, 11, 14)), -1)

    def test_changing_the_planting_date_changes_the_stage(self):
        day = date(2026, 12, 20)
        early = crop_model.days_after_planting(date(2026, 11, 1), day)   # 49 days
        late = crop_model.days_after_planting(date(2026, 12, 10), day)   # 10 days
        self.assertEqual(crop_model.growth_stage(CROP, early), crop_model.DEVELOPMENT)
        self.assertEqual(crop_model.growth_stage(CROP, late), crop_model.INITIAL)

    def test_requires_dates(self):
        with self.assertRaises(TypeError):
            crop_model.days_after_planting("2026-11-15", date(2026, 11, 16))


class GrowthStageTests(unittest.TestCase):
    def test_stage_boundaries(self):
        cases = {
            0: crop_model.INITIAL, 29: crop_model.INITIAL,
            30: crop_model.DEVELOPMENT, 169: crop_model.DEVELOPMENT,
            170: crop_model.MID_SEASON, 209: crop_model.MID_SEASON,
            210: crop_model.LATE_SEASON, 239: crop_model.LATE_SEASON,
        }
        for dap, expected in cases.items():
            with self.subTest(dap=dap):
                self.assertEqual(crop_model.growth_stage(CROP, dap), expected)

    def test_outside_the_season_raises(self):
        for dap in (-1, 240, 500):
            with self.subTest(dap=dap):
                with self.assertRaises(OutOfSeasonError):
                    crop_model.growth_stage(CROP, dap)

    def test_stage_durations_are_configurable(self):
        short = replace(CROP, initial_days=10, development_days=20, mid_days=30, late_days=10)
        self.assertEqual(crop_model.growth_stage(short, 9), crop_model.INITIAL)
        self.assertEqual(crop_model.growth_stage(short, 10), crop_model.DEVELOPMENT)
        self.assertEqual(crop_model.growth_stage(short, 30), crop_model.MID_SEASON)
        self.assertEqual(crop_model.growth_stage(short, 60), crop_model.LATE_SEASON)
        with self.assertRaises(OutOfSeasonError):
            crop_model.growth_stage(short, 70)


class CropCoefficientTests(unittest.TestCase):
    def test_initial_stage_is_constant_kc_ini(self):
        for dap in (0, 10, 29):
            self.assertEqual(crop_model.crop_coefficient(CROP, dap), 0.70)

    def test_development_stage_interpolates_linearly(self):
        self.assertAlmostEqual(crop_model.crop_coefficient(CROP, 30), 0.70)
        self.assertAlmostEqual(crop_model.crop_coefficient(CROP, 100), (0.70 + 1.15) / 2)
        self.assertAlmostEqual(crop_model.crop_coefficient(CROP, 169), 0.70 + 139 / 140 * 0.45)

    def test_mid_season_is_constant_kc_mid(self):
        for dap in (170, 190, 209):
            self.assertEqual(crop_model.crop_coefficient(CROP, dap), 1.15)

    def test_late_season_decreases_towards_kc_end(self):
        self.assertAlmostEqual(crop_model.crop_coefficient(CROP, 210), 1.15)
        self.assertAlmostEqual(crop_model.crop_coefficient(CROP, 225), (1.15 + 0.25) / 2)
        self.assertAlmostEqual(crop_model.crop_coefficient(CROP, 239), 1.15 + 29 / 30 * (0.25 - 1.15))

    def test_kc_has_no_jumps_between_stages(self):
        for dap in range(1, CROP.season_days):
            step = abs(crop_model.crop_coefficient(CROP, dap) - crop_model.crop_coefficient(CROP, dap - 1))
            self.assertLess(step, 0.1, dap)

    def test_kc_values_are_configurable_without_code_changes(self):
        custom = replace(CROP, kc_ini=0.3, kc_mid=1.2, kc_end=0.4)
        self.assertEqual(crop_model.crop_coefficient(custom, 0), 0.3)
        self.assertEqual(crop_model.crop_coefficient(custom, 180), 1.2)

    def test_outside_season_raises(self):
        with self.assertRaises(OutOfSeasonError):
            crop_model.crop_coefficient(CROP, 240)


class CropEvapotranspirationTests(unittest.TestCase):
    def test_etc_is_kc_times_et0(self):
        self.assertAlmostEqual(crop_model.crop_evapotranspiration(0.7, 4.0), 2.8)
        self.assertAlmostEqual(crop_model.crop_evapotranspiration(1.15, 5.2), 5.98)

    def test_zero_et0_gives_zero_etc(self):
        self.assertEqual(crop_model.crop_evapotranspiration(1.15, 0.0), 0.0)

    def test_invalid_inputs(self):
        for kc, et0 in ((-0.1, 4.0), (0.7, -1.0), (float("nan"), 4.0), (0.7, float("inf")),
                        (0.7, "4"), (3.0, 4.0), (0.7, 99.0)):
            with self.subTest(kc=kc, et0=et0):
                with self.assertRaises(InvalidInputError):
                    crop_model.crop_evapotranspiration(kc, et0)
        with self.assertRaises(MissingInputError):
            crop_model.crop_evapotranspiration(0.7, None)


class ConfigValidationTests(unittest.TestCase):
    def test_invalid_crop_parameters(self):
        for kwargs in (dict(kc_ini=0.0), dict(kc_mid=-1.0), dict(initial_days=0), dict(late_days=2.5),
                       dict(depletion_fraction_p=1.0), dict(depletion_fraction_p=0.0),
                       dict(root_depth_initial_m=2.0, root_depth_max_m=1.0), dict(root_depth_max_m=9.0)):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ConfigError):
                    CropConfig(**kwargs)

    def test_invalid_study_parameters(self):
        for kwargs in (dict(field_id=" "), dict(latitude=95.0), dict(longitude=200.0), dict(elevation_m=-1000.0),
                       dict(simulation_start_date=date(2026, 11, 1))):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ConfigError):
                    StudyConfig(**kwargs)

    def test_invalid_soil_parameters(self):
        for kwargs in (dict(field_capacity_m3_m3=0.1, wilting_point_m3_m3=0.2),
                       dict(wilting_point_m3_m3=0.0), dict(initial_depletion_mm=-1.0),
                       dict(field_capacity_m3_m3=0.9)):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ConfigError):
                    SoilConfig(**kwargs)


if __name__ == "__main__":
    unittest.main()
