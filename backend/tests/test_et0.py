"""Tests for agrisense.et0 (FAO-56 Penman-Monteith).

Published reference values come from:
    Allen, R.G., Pereira, L.S., Raes, D., Smith, M. (1998). Crop evapotranspiration -
    Guidelines for computing crop water requirements. FAO Irrigation and Drainage
    Paper 56, Rome. https://www.fao.org/4/X0490E/x0490e00.htm
      - Chapter 3, Examples 2, 3, 8, 11, 12 (https://www.fao.org/4/X0490E/x0490e07.htm)
      - Chapter 4, Example 18 (https://www.fao.org/4/X0490E/x0490e08.htm)

Run from the backend folder:
    python -m unittest discover -s tests -t . -v
"""

import math
import unittest
from datetime import date

from agrisense import et0
from agrisense.validation import InvalidInputError, MissingInputError


class FAO56Example18Tests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.result = et0.calculate_et0(
            tmax_c=21.5, tmin_c=12.3, rh_max_pct=84.0, rh_min_pct=63.0,
            pressure_kpa=100.1,                      # published, from Eq. 7 at 100 m
            wind_speed_m_s=10.0 / 3.6,               # 10 km/h = 2.78 m/s at 10 m
            solar_radiation_mj_m2_day=22.07,         # published Rs
            latitude_deg=50.80, elevation_m=100.0,
            day=date(2026, 7, 6),                    # J = 187
            wind_height_m=10.0,
        )

    def test_final_et0_matches_published_value(self):
        self.assertAlmostEqual(self.result.et0_mm_day, 3.88, delta=0.01)

    def test_published_intermediate_values(self):
        r = self.result
        self.assertAlmostEqual(r.t_mean_c, 16.9, places=6)
        self.assertAlmostEqual(r.u2_m_s, 2.078, delta=0.003)       # wind at 2 m
        self.assertAlmostEqual(r.delta_kpa_c, 0.122, delta=0.0005)
        self.assertAlmostEqual(r.gamma_kpa_c, 0.0666, delta=0.0001)
        self.assertAlmostEqual(r.es_kpa, 1.997, delta=0.002)
        self.assertAlmostEqual(r.ea_kpa, 1.409, delta=0.001)
        self.assertAlmostEqual(r.es_kpa - r.ea_kpa, 0.589, delta=0.002)
        self.assertAlmostEqual(r.ra_mj_m2_day, 41.09, delta=0.02)
        self.assertAlmostEqual(r.rso_mj_m2_day, 30.90, delta=0.02)
        self.assertAlmostEqual(r.rns_mj_m2_day, 17.00, delta=0.02)
        self.assertAlmostEqual(r.rnl_mj_m2_day, 3.71, delta=0.01)
        self.assertAlmostEqual(r.rn_mj_m2_day, 13.28, delta=0.02)

    def test_radiation_and_aerodynamic_terms_match_published_split(self):
        r = self.result
        denominator = r.delta_kpa_c + r.gamma_kpa_c * (1 + 0.34 * r.u2_m_s)
        radiation_term = 0.408 * r.delta_kpa_c * r.rn_mj_m2_day / denominator
        aerodynamic_term = (r.gamma_kpa_c * 900.0 / (r.t_mean_c + 273.0) * r.u2_m_s
                            * (r.es_kpa - r.ea_kpa) / denominator)
        self.assertAlmostEqual(radiation_term, 2.81, delta=0.01)    # published
        self.assertAlmostEqual(aerodynamic_term, 1.07, delta=0.01)  # published

    def test_day_of_year_matches_published_j(self):
        self.assertEqual(date(2026, 7, 6).timetuple().tm_yday, 187)

    def test_pressure_from_elevation_matches_published_value(self):
        self.assertAlmostEqual(et0.pressure_from_elevation(100.0), 100.1, delta=0.05)

    def test_not_clipped(self):
        self.assertFalse(self.result.clipped_to_zero)


class FAO56ComponentExampleTests(unittest.TestCase):

    def test_example_2_pressure_and_psychrometric_constant_at_1800_m(self):
        pressure = et0.pressure_from_elevation(1800.0)
        self.assertAlmostEqual(pressure, 81.8, delta=0.05)
        self.assertAlmostEqual(et0.psychrometric_constant(pressure), 0.054, delta=0.0005)

    def test_example_3_saturation_vapour_pressure(self):
        self.assertAlmostEqual(et0.saturation_vapour_pressure(24.5), 3.075, delta=0.001)
        self.assertAlmostEqual(et0.saturation_vapour_pressure(15.0), 1.705, delta=0.001)
        self.assertAlmostEqual(et0.mean_saturation_vapour_pressure(24.5, 15.0), 2.39, delta=0.005)

    def test_example_8_extraterrestrial_radiation_20_south_3_september(self):
        ra = et0.extraterrestrial_radiation(-20.0, date(2026, 9, 3))  # J = 246
        self.assertAlmostEqual(ra, 32.2, delta=0.1)

    def test_example_11_net_longwave_radiation_rio_de_janeiro(self):
        # Published: Rs = 14.5, Rso = 18.8, Tmax 25.1, Tmin 19.1, ea = 2.1 kPa -> Rnl = 3.5
        rnl = et0.net_longwave_radiation(25.1, 19.1, 2.1, 14.5, 18.8)
        self.assertAlmostEqual(rnl, 3.5, delta=0.1)

    def test_example_12_net_shortwave_radiation(self):
        # Published as 11.1 (exact: 0.77 x 14.5 = 11.165; the text truncates).
        self.assertAlmostEqual(et0.net_shortwave_radiation(14.5), 11.1, delta=0.1)

    def test_relative_shortwave_is_capped_at_one(self):
        capped = et0.net_longwave_radiation(25.0, 15.0, 1.5, 40.0, 20.0)
        at_one = et0.net_longwave_radiation(25.0, 15.0, 1.5, 20.0, 20.0)
        self.assertAlmostEqual(capped, at_one)


BASE = dict(
    tmax_c=26.0, tmin_c=14.5, rh_max_pct=95.0, rh_min_pct=42.0,
    pressure_kpa=99.9, wind_speed_m_s=2.5, solar_radiation_mj_m2_day=15.5,
    latitude_deg=29.97, elevation_m=193.0, day=date(2026, 11, 14), wind_height_m=10.0,
)


def run(**overrides):
    return et0.calculate_et0(**{**BASE, **overrides})


class WindHeightTests(unittest.TestCase):
    def test_fao_eq_47_factor_for_10_m(self):
        # FAO-56 Example 18 uses the factor 0.748 for a 10 m measurement.
        self.assertAlmostEqual(et0.wind_speed_at_2m(1.0, 10.0), 0.748, delta=0.001)

    def test_published_example_conversion(self):
        self.assertAlmostEqual(et0.wind_speed_at_2m(2.78, 10.0), 2.078, delta=0.003)

    def test_measurement_at_2_m_is_unchanged(self):
        self.assertAlmostEqual(et0.wind_speed_at_2m(3.0, 2.0), 3.0, delta=0.002)

    def test_zero_wind_stays_zero(self):
        self.assertEqual(et0.wind_speed_at_2m(0.0, 10.0), 0.0)

    def test_higher_measurement_height_gives_lower_2m_speed(self):
        self.assertLess(et0.wind_speed_at_2m(3.0, 10.0), et0.wind_speed_at_2m(3.0, 5.0))

    def test_invalid_wind_inputs(self):
        for bad in (-1.0, float("nan"), float("inf"), "3", True, 60.0):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidInputError):
                    et0.wind_speed_at_2m(bad, 10.0)
        with self.assertRaises(MissingInputError):
            et0.wind_speed_at_2m(None, 10.0)
        with self.assertRaises(InvalidInputError):
            et0.wind_speed_at_2m(2.0, 0.0)

    def test_calculation_uses_the_converted_wind_speed(self):
        self.assertAlmostEqual(run(wind_speed_m_s=3.0).u2_m_s,
                               3.0 * 4.87 / math.log(67.8 * 10 - 5.42), places=9)


class UnitConversionTests(unittest.TestCase):
    def test_hpa_to_kpa(self):
        self.assertAlmostEqual(et0.hpa_to_kpa(1013.25), 101.325)
        self.assertAlmostEqual(et0.hpa_to_kpa(999.2), 99.92)

    def test_hpa_to_kpa_rejects_bad_values(self):
        for bad in (0.0, -5.0, float("nan"), "999", True, 5000.0):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidInputError):
                    et0.hpa_to_kpa(bad)
        with self.assertRaises(MissingInputError):
            et0.hpa_to_kpa(None)

    def test_pressure_in_hpa_is_rejected_where_kpa_is_required(self):
        with self.assertRaisesRegex(InvalidInputError, "kPa"):
            run(pressure_kpa=999.2)  # forgot to divide by 10

    def test_radiation_in_watts_per_m2_is_rejected_where_mj_is_required(self):
        with self.assertRaisesRegex(InvalidInputError, "MJ/m2/day"):
            run(solar_radiation_mj_m2_day=250.0)  # a W/m2 value, not a daily MJ/m2 sum

    def test_radiation_term_scales_with_mj_input(self):
        low = run(solar_radiation_mj_m2_day=8.0).et0_mm_day
        high = run(solar_radiation_mj_m2_day=18.0).et0_mm_day
        self.assertGreater(high, low)

    def test_equivalent_evaporation_factor(self):
        # FAO-56 Eq. 20: 1 MJ/m2/day = 0.408 mm/day (published, e.g. 32.2 -> 13.1)
        self.assertAlmostEqual(0.408 * 32.2, 13.1, delta=0.05)


class MissingInputTests(unittest.TestCase):
    def test_every_required_input_is_required(self):
        required = ["tmax_c", "tmin_c", "rh_max_pct", "rh_min_pct", "pressure_kpa",
                    "wind_speed_m_s", "solar_radiation_mj_m2_day", "latitude_deg", "elevation_m"]
        for name in required:
            with self.subTest(missing=name):
                with self.assertRaises(MissingInputError):
                    run(**{name: None})

    def test_day_must_be_a_date(self):
        with self.assertRaises(InvalidInputError):
            run(day="2026-11-14")

    def test_missing_values_are_never_defaulted(self):
        with self.assertRaises(MissingInputError):
            run(rh_min_pct=None)  # must not fall back to e.g. mean humidity


class InvalidTemperatureTests(unittest.TestCase):
    def test_tmin_greater_than_tmax(self):
        with self.assertRaisesRegex(InvalidInputError, "Tmin"):
            run(tmax_c=10.0, tmin_c=12.0)

    def test_implausible_temperatures(self):
        for kwargs in (dict(tmax_c=75.0), dict(tmin_c=-80.0, tmax_c=0.0), dict(tmax_c=-60.0, tmin_c=-70.0)):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(InvalidInputError):
                    run(**kwargs)

    def test_equal_tmax_tmin_is_allowed(self):
        self.assertGreaterEqual(run(tmax_c=20.0, tmin_c=20.0, rh_max_pct=70.0, rh_min_pct=70.0).et0_mm_day, 0.0)

    def test_negative_temperatures_are_valid(self):
        result = run(tmax_c=2.0, tmin_c=-6.0, solar_radiation_mj_m2_day=6.0, day=date(2026, 1, 10))
        self.assertGreaterEqual(result.et0_mm_day, 0.0)


class InvalidHumidityTests(unittest.TestCase):
    def test_out_of_range_humidity(self):
        for kwargs in (dict(rh_max_pct=101.0), dict(rh_min_pct=-1.0), dict(rh_max_pct=-5.0, rh_min_pct=-6.0)):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(InvalidInputError):
                    run(**kwargs)

    def test_rh_min_greater_than_rh_max(self):
        with self.assertRaisesRegex(InvalidInputError, "RHmin"):
            run(rh_max_pct=50.0, rh_min_pct=60.0)

    def test_zero_and_hundred_percent_are_valid(self):
        self.assertGreaterEqual(run(rh_max_pct=100.0, rh_min_pct=0.0).et0_mm_day, 0.0)
        self.assertGreaterEqual(run(rh_max_pct=0.0, rh_min_pct=0.0).et0_mm_day, 0.0)

    def test_drier_air_gives_higher_et0(self):
        humid = run(rh_max_pct=100.0, rh_min_pct=90.0).et0_mm_day
        dry = run(rh_max_pct=60.0, rh_min_pct=15.0).et0_mm_day
        self.assertGreater(dry, humid)


class ZeroValueTests(unittest.TestCase):
    def test_zero_wind_is_valid_and_removes_the_aerodynamic_term(self):
        result = run(wind_speed_m_s=0.0)
        self.assertEqual(result.u2_m_s, 0.0)
        expected = 0.408 * result.delta_kpa_c * result.rn_mj_m2_day / (result.delta_kpa_c + result.gamma_kpa_c)
        self.assertAlmostEqual(result.et0_mm_day, expected, places=9)

    def test_zero_wind_is_not_treated_as_missing(self):
        self.assertIsNotNone(run(wind_speed_m_s=0).et0_mm_day)

    def test_zero_solar_radiation_is_valid(self):
        result = run(solar_radiation_mj_m2_day=0.0)
        self.assertGreaterEqual(result.et0_mm_day, 0.0)
        self.assertEqual(result.rns_mj_m2_day, 0.0)

    def test_negative_wind_is_rejected(self):
        with self.assertRaises(InvalidInputError):
            run(wind_speed_m_s=-0.1)


class NonFiniteTests(unittest.TestCase):
    def test_nan_and_infinity_rejected_for_every_numeric_input(self):
        numeric = ["tmax_c", "tmin_c", "rh_max_pct", "rh_min_pct", "pressure_kpa",
                   "wind_speed_m_s", "solar_radiation_mj_m2_day", "latitude_deg", "elevation_m"]
        for name in numeric:
            for bad in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(name=name, bad=bad):
                    with self.assertRaisesRegex(InvalidInputError, "not finite"):
                        run(**{name: bad})

    def test_wrong_types_rejected(self):
        for bad in ("12.0", True, [1.0], {}):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidInputError):
                    run(tmax_c=bad)


class OutputBoundsTests(unittest.TestCase):
    def test_summer_and_winter_values_are_reasonable_for_the_cairo_region(self):
        summer = run(tmax_c=38.0, tmin_c=23.0, rh_max_pct=70.0, rh_min_pct=18.0, wind_speed_m_s=3.5,
                     solar_radiation_mj_m2_day=29.0, day=date(2026, 6, 21), pressure_kpa=99.0).et0_mm_day
        winter = run(tmax_c=19.0, tmin_c=8.0, rh_max_pct=92.0, rh_min_pct=40.0, wind_speed_m_s=2.0,
                     solar_radiation_mj_m2_day=11.0, day=date(2026, 1, 15), pressure_kpa=100.0).et0_mm_day
        self.assertTrue(5.0 <= summer <= 12.0, summer)
        self.assertTrue(0.5 <= winter <= 3.0, winter)
        self.assertGreater(summer, winter)

    def test_range_of_plausible_conditions_stays_within_bounds(self):
        for month in range(1, 13):
            for wind in (0.0, 1.0, 4.0, 10.0):
                for rs_fraction in (0.2, 0.6, 0.9):
                    day = date(2026, month, 15)
                    ra = et0.extraterrestrial_radiation(29.97, day)
                    result = run(day=day, wind_speed_m_s=wind, solar_radiation_mj_m2_day=rs_fraction * ra,
                                 tmax_c=15.0 + month, tmin_c=5.0 + month)
                    with self.subTest(month=month, wind=wind, rs=rs_fraction):
                        self.assertTrue(0.0 <= result.et0_mm_day <= 15.0, result.et0_mm_day)

    def test_implausible_result_is_rejected_not_returned(self):
        with self.assertRaisesRegex(InvalidInputError, "implausible"):
            run(tmax_c=60.0, tmin_c=45.0, rh_max_pct=5.0, rh_min_pct=0.0, wind_speed_m_s=50.0,
                solar_radiation_mj_m2_day=40.0, day=date(2026, 6, 21))

    def test_solar_radiation_cannot_exceed_extraterrestrial_radiation(self):
        with self.assertRaisesRegex(InvalidInputError, "extraterrestrial"):
            run(solar_radiation_mj_m2_day=40.0, day=date(2026, 12, 21))

    def test_negative_et0_is_clipped_and_flagged(self):
        # Clear, very cold, calm winter day at 60 N: net radiation is strongly negative.
        result = run(tmax_c=-20.0, tmin_c=-30.0, rh_max_pct=100.0, rh_min_pct=100.0, wind_speed_m_s=0.0,
                     solar_radiation_mj_m2_day=1.0, day=date(2026, 12, 21), latitude_deg=60.0,
                     elevation_m=0.0, pressure_kpa=101.3)
        self.assertLess(result.rn_mj_m2_day, 0.0)
        self.assertEqual(result.et0_mm_day, 0.0)
        self.assertTrue(result.clipped_to_zero)

    def test_does_not_use_openmeteo_et0(self):
        # calculate_et0 has no parameter for Open-Meteo's ET0, by design.
        import inspect
        self.assertFalse([p for p in inspect.signature(et0.calculate_et0).parameters if "openmeteo" in p])


if __name__ == "__main__":
    unittest.main()
