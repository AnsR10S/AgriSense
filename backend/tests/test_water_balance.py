"""Tests for agrisense.water_balance (FAO-56 root-zone depletion method).

Published reference values come from FAO-56 (Allen et al., 1998), Chapter 8,
https://www.fao.org/4/X0490E/x0490e0e.htm :
  - Example 36: TAW and RAW for several crops and soils (Eq. 82, 83, Tables 19, 22)
  - Example 37: 10-day depletion table with the stress coefficient Ks (Eq. 81, 84, 85)
"""

import unittest
from dataclasses import replace
from datetime import date, timedelta

from agrisense import water_balance as wb
from agrisense.config import AppConfig, CropConfig, SoilConfig, StudyConfig
from agrisense.validation import ConfigError, InvalidInputError, MissingInputError

START = date(2026, 11, 13)


class FAO56Example36Tests(unittest.TestCase):

    def test_taw_and_raw_for_published_crop_soil_combinations(self):
        # (crop, soil, FC, WP, Zr [m], p, published TAW [mm], published RAW [mm])
        cases = [
            ("onion", "loamy sand", 0.15, 0.06, 0.4, 0.30, 36, 11),
            ("onion", "silt", 0.32, 0.15, 0.4, 0.30, 68, 20),
            ("onion", "silty clay", 0.35, 0.23, 0.4, 0.30, 48, 14),
            ("tomato", "loamy sand", 0.15, 0.06, 0.8, 0.40, 72, 29),
            ("tomato", "silt", 0.32, 0.15, 0.8, 0.40, 136, 54),
            ("maize", "loamy sand", 0.15, 0.06, 1.2, 0.55, 108, 59),
            ("maize", "silt", 0.32, 0.15, 1.2, 0.55, 204, 112),
            ("maize", "silty clay", 0.35, 0.23, 1.2, 0.55, 144, 79),
        ]
        for crop, soil, fc, wp, zr, p, taw_pub, raw_pub in cases:
            with self.subTest(crop=crop, soil=soil):
                taw = wb.total_available_water(fc, wp, zr)
                raw = wb.readily_available_water(p, taw)
                self.assertAlmostEqual(taw, taw_pub, delta=0.5)
                self.assertAlmostEqual(raw, raw_pub, delta=0.5)


class FAO56Example37Tests(unittest.TestCase):

    def test_taw_and_raw(self):
        taw = wb.total_available_water(0.32, 0.12, 0.8)
        self.assertAlmostEqual(taw, 160.0)
        self.assertAlmostEqual(wb.readily_available_water(0.40, taw), 64.0)

    def test_ten_day_table_with_stress_coefficient(self):
        published_ks = [1.00, 1.00, 0.97, 0.91, 0.85, 0.80, 0.75, 0.70, 0.66, 0.62]
        published_etc_adj = [6.0, 6.0, 5.8, 5.4, 5.1, 4.8, 4.5, 4.2, 3.9, 3.7]
        published_dr_end = [61.0, 67.0, 72.8, 78.3, 83.4, 88.2, 92.6, 96.9, 100.8, 104.5]
        etc = 5.0 * 1.2
        depletion = 55.0
        for day in range(10):
            step = wb.update_depletion(depletion, etc, 0.0, 0.0, 160.0, 0.40)
            with self.subTest(day=day + 1):
                self.assertAlmostEqual(step.ks, published_ks[day], delta=0.006)
                self.assertAlmostEqual(step.etc_adjusted_mm, published_etc_adj[day], delta=0.06)
                self.assertAlmostEqual(step.depletion_mm, published_dr_end[day], delta=0.1)
            depletion = step.depletion_mm


class TawRawTests(unittest.TestCase):
    def test_default_soil_taw_per_metre(self):
        soil = SoilConfig()
        self.assertAlmostEqual(wb.total_available_water(soil.field_capacity_m3_m3,
                                                        soil.wilting_point_m3_m3, 1.0), 120.0)

    def test_raw_is_p_times_taw(self):
        self.assertAlmostEqual(wb.readily_available_water(0.55, 120.0), 66.0)

    def test_invalid_inputs(self):
        with self.assertRaises(InvalidInputError):
            wb.total_available_water(0.10, 0.20, 1.0)       # WP above FC
        with self.assertRaises(InvalidInputError):
            wb.total_available_water(0.25, 0.10, -1.0)
        with self.assertRaises(InvalidInputError):
            wb.total_available_water(float("nan"), 0.10, 1.0)
        with self.assertRaises(MissingInputError):
            wb.readily_available_water(None, 100.0)
        with self.assertRaises(InvalidInputError):
            wb.readily_available_water(1.5, 100.0)

    def test_zero_root_depth_gives_zero_taw(self):
        self.assertEqual(wb.total_available_water(0.25, 0.10, 0.0), 0.0)


class RootDepthTests(unittest.TestCase):
    def test_linear_growth_to_end_of_development(self):
        crop = CropConfig()  # 0.25 -> 1.5 m over 170 days
        self.assertAlmostEqual(wb.root_depth(crop, 0), 0.25)
        self.assertAlmostEqual(wb.root_depth(crop, 85), (0.25 + 1.5) / 2)
        self.assertAlmostEqual(wb.root_depth(crop, 170), 1.5)
        self.assertAlmostEqual(wb.root_depth(crop, 230), 1.5)

    def test_maximum_root_depth_is_configurable(self):
        crop = replace(CropConfig(), root_depth_max_m=0.8)
        self.assertAlmostEqual(wb.root_depth(crop, 200), 0.8)


class StressCoefficientTests(unittest.TestCase):
    def test_ks_is_one_up_to_raw(self):
        self.assertEqual(wb.stress_coefficient(0.0, 100.0, 0.5), 1.0)
        self.assertEqual(wb.stress_coefficient(50.0, 100.0, 0.5), 1.0)

    def test_ks_declines_to_zero_at_taw(self):
        self.assertAlmostEqual(wb.stress_coefficient(75.0, 100.0, 0.5), 0.5)
        self.assertEqual(wb.stress_coefficient(100.0, 100.0, 0.5), 0.0)


class DepletionUpdateTests(unittest.TestCase):
    def update(self, dr=20.0, etc=4.0, rain=0.0, irrigation=0.0, taw=100.0, p=0.5):
        return wb.update_depletion(dr, etc, rain, irrigation, taw, p)

    def test_etc_increases_depletion(self):
        self.assertAlmostEqual(self.update(dr=20.0, etc=4.0).depletion_mm, 24.0)

    def test_rainfall_reduces_depletion(self):
        self.assertAlmostEqual(self.update(dr=20.0, etc=4.0, rain=10.0).depletion_mm, 14.0)

    def test_explicit_irrigation_reduces_depletion(self):
        self.assertAlmostEqual(self.update(dr=30.0, etc=4.0, irrigation=20.0).depletion_mm, 14.0)

    def test_rain_and_irrigation_combine(self):
        self.assertAlmostEqual(self.update(dr=30.0, etc=4.0, rain=6.0, irrigation=10.0).depletion_mm, 18.0)

    def test_rainfall_cannot_reduce_depletion_below_zero(self):
        step = self.update(dr=5.0, etc=3.0, rain=40.0)
        self.assertEqual(step.depletion_mm, 0.0)
        self.assertAlmostEqual(step.excess_water_mm, 32.0)   # 5 + 3 - 40 = -32 -> lost, not stored

    def test_excess_water_is_not_stored_as_extra_soil_water(self):
        wet = self.update(dr=5.0, etc=3.0, rain=40.0)
        next_day = wb.update_depletion(wet.depletion_mm, 4.0, 0.0, 0.0, 100.0, 0.5)
        self.assertAlmostEqual(next_day.depletion_mm, 4.0)   # starts from field capacity, not from surplus

    def test_over_irrigation_is_also_capped_at_field_capacity(self):
        step = self.update(dr=10.0, etc=2.0, irrigation=80.0)
        self.assertEqual(step.depletion_mm, 0.0)
        self.assertAlmostEqual(step.excess_water_mm, 68.0)

    def test_depletion_cannot_exceed_taw(self):
        step = wb.update_depletion(99.0, 30.0, 0.0, 0.0, 100.0, 0.5)
        self.assertLessEqual(step.depletion_mm, 100.0)
        self.assertFalse(wb.update_depletion(20.0, 1.0, 0.0, 0.0, 100.0, 0.5).capped_at_taw)

    def test_depletion_is_capped_at_taw_when_stress_adjustment_is_overwhelmed(self):
        step = wb.update_depletion(100.0, 5.0, 0.0, 0.0, 100.0, 0.5)   # already at wilting point, Ks = 0
        self.assertEqual(step.depletion_mm, 100.0)
        self.assertEqual(step.etc_adjusted_mm, 0.0)

    def test_zero_etc_and_zero_rain_leave_depletion_unchanged(self):
        self.assertEqual(self.update(dr=17.5, etc=0.0).depletion_mm, 17.5)

    def test_stress_reduces_actual_evapotranspiration(self):
        step = wb.update_depletion(75.0, 4.0, 0.0, 0.0, 100.0, 0.5)   # beyond RAW (50): Ks = 0.5
        self.assertAlmostEqual(step.ks, 0.5)
        self.assertAlmostEqual(step.depletion_mm, 77.0)

    def test_invalid_inputs(self):
        for kwargs in (dict(etc=-1.0), dict(rain=-2.0), dict(irrigation=-1.0), dict(dr=-1.0),
                       dict(etc=float("nan")), dict(rain=float("inf")), dict(taw=0.0), dict(rain="3")):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(InvalidInputError):
                    self.update(**kwargs)

    def test_missing_precipitation_is_not_replaced_by_zero(self):
        with self.assertRaises(MissingInputError):
            self.update(rain=None)


def forcing(offset, et0=4.0, rain=0.0):
    return wb.DailyForcing(START + timedelta(days=offset), et0, rain)


class RunWaterBalanceTests(unittest.TestCase):
    config = AppConfig()

    def test_first_day_hand_calculation(self):
        states = wb.run_water_balance(self.config, [forcing(0, et0=4.0)])
        s = states[0]
        self.assertEqual(s.day, START)
        self.assertEqual(s.days_after_planting, 0)
        self.assertEqual(s.stage, "initial")
        self.assertAlmostEqual(s.kc, 0.7)
        self.assertAlmostEqual(s.etc_mm, 2.8)
        self.assertAlmostEqual(s.root_depth_m, 0.25)
        self.assertAlmostEqual(s.taw_mm, 30.0)       # 1000 x (0.23 - 0.11) x 0.25
        self.assertAlmostEqual(s.raw_mm, 16.5)       # 0.55 x 30
        self.assertAlmostEqual(s.depletion_start_mm, 0.0)
        self.assertAlmostEqual(s.depletion_end_mm, 2.8)

    def test_depletion_accumulates_day_to_day_without_rain(self):
        states = wb.run_water_balance(self.config, [forcing(i) for i in range(5)])
        ends = [s.depletion_end_mm for s in states]
        self.assertEqual(ends, sorted(ends))
        for previous, current in zip(states, states[1:]):
            self.assertAlmostEqual(current.depletion_start_mm, previous.depletion_end_mm)

    def test_configured_initial_depletion_is_used(self):
        config = replace(self.config, soil=SoilConfig(initial_depletion_mm=10.0))
        s = wb.run_water_balance(config, [forcing(0)])[0]
        self.assertAlmostEqual(s.depletion_start_mm, 10.0)
        self.assertAlmostEqual(s.depletion_end_mm, 12.8)

    def test_initial_depletion_above_taw_is_a_configuration_error(self):
        config = replace(self.config, soil=SoilConfig(initial_depletion_mm=50.0))  # TAW is 30 mm on day 0
        with self.assertRaises(ConfigError):
            wb.run_water_balance(config, [forcing(0)])

    def test_rainfall_event_in_a_run(self):
        states = wb.run_water_balance(self.config, [forcing(0), forcing(1), forcing(2, rain=25.0), forcing(3)])
        self.assertEqual(states[2].depletion_end_mm, 0.0)
        self.assertGreater(states[2].excess_water_mm, 0.0)
        self.assertGreater(states[3].depletion_end_mm, 0.0)

    def test_explicit_irrigation_is_applied_on_the_given_day_only(self):
        forcings = [forcing(i) for i in range(4)]
        base = wb.run_water_balance(self.config, forcings)
        irrigated = wb.run_water_balance(self.config, forcings, {START + timedelta(days=2): 5.0})
        self.assertEqual(irrigated[1].depletion_end_mm, base[1].depletion_end_mm)
        self.assertEqual(irrigated[2].irrigation_mm, 5.0)
        self.assertLess(irrigated[2].depletion_end_mm, base[2].depletion_end_mm)

    def test_no_irrigation_is_assumed_unless_supplied(self):
        states = wb.run_water_balance(self.config, [forcing(i) for i in range(3)])
        self.assertTrue(all(s.irrigation_mm == 0.0 for s in states))

    def test_taw_grows_with_root_depth(self):
        states = wb.run_water_balance(self.config, [forcing(i) for i in range(30)])
        self.assertGreater(states[-1].taw_mm, states[0].taw_mm)

    def test_gaps_in_the_weather_series_are_rejected(self):
        with self.assertRaisesRegex(InvalidInputError, "consecutive"):
            wb.run_water_balance(self.config, [forcing(0), forcing(2)])

    def test_series_must_start_on_the_simulation_start_date(self):
        with self.assertRaisesRegex(InvalidInputError, "consecutive"):
            wb.run_water_balance(self.config, [forcing(1), forcing(2)])

    def test_empty_series_is_rejected(self):
        with self.assertRaises(InvalidInputError):
            wb.run_water_balance(self.config, [])

    def test_water_balance_has_no_soil_moisture_sensor_input(self):
        # The simulated potentiometer value is not a forcing variable.
        self.assertEqual({f for f in wb.DailyForcing.__dataclass_fields__}, {"day", "et0_mm", "precipitation_mm"})

    def test_soil_parameters_are_configurable(self):
        drier = replace(self.config, soil=SoilConfig(field_capacity_m3_m3=0.20, wilting_point_m3_m3=0.15))
        s = wb.run_water_balance(drier, [forcing(0)])[0]
        self.assertAlmostEqual(s.taw_mm, 12.5)


class StudyDefaultsTests(unittest.TestCase):
    def test_simulation_start_is_explicit_and_not_inferred(self):
        study = StudyConfig()
        self.assertEqual(study.simulation_start_date, START)
        later = replace(study, simulation_start_date=date(2026, 11, 20))
        self.assertEqual(later.simulation_start_date, date(2026, 11, 20))


if __name__ == "__main__":
    unittest.main()
