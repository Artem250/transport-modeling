import unittest

from models import Link
from odm_service import (
    DESIGN_HOUR_PEAK_SHARE_DEFAULT,
    KRCH_30TH_DEFAULT,
    DESIGN_HOUR_DAILY_SPLIT_TOTAL,
    KG_DEFAULT,
    KN_DEFAULT,
    KT_DEFAULT,
    analyze_odm_city_link,
    derive_average_hourly_intensity,
    derive_design_hourly_intensity,
    derive_design_hourly_intensity_from_daily_split,
    derive_design_hourly_intensity_from_peak_share,
    get_pmax_lookup,
    speed_limit_factor,
)


class OdmServiceTests(unittest.TestCase):
    def test_average_hourly_intensity_formula(self):
        aadt = 10000.0
        expected = aadt * 365.0 * KT_DEFAULT * KN_DEFAULT * KG_DEFAULT / 4.0
        self.assertAlmostEqual(derive_average_hourly_intensity(aadt), expected, places=6)

    def test_design_hour_intensity_formula(self):
        aadt = 10000.0
        expected = derive_average_hourly_intensity(aadt) * KRCH_30TH_DEFAULT
        self.assertAlmostEqual(derive_design_hourly_intensity(aadt), expected, places=6)

    def test_design_hour_daily_split_formula(self):
        aadt = 10000.0
        expected = aadt * DESIGN_HOUR_DAILY_SPLIT_TOTAL
        self.assertAlmostEqual(derive_design_hourly_intensity_from_daily_split(aadt), expected, places=6)

    def test_design_hour_peak_share_formula(self):
        aadt = 10000.0
        link = Link(
            id="L_peak",
            name="Peak",
            start_node_id="N1",
            end_node_id="N2",
        )
        expected = aadt * DESIGN_HOUR_PEAK_SHARE_DEFAULT
        self.assertAlmostEqual(derive_design_hourly_intensity_from_peak_share(link, aadt), expected, places=6)

    def test_speed_limit_factor_lookup(self):
        link = Link(
            id="L1",
            name="Test",
            start_node_id="N1",
            end_node_id="N2",
            parameters={"speed_limit": 40},
        )
        factor, used_default = speed_limit_factor(link)
        self.assertEqual(factor, 0.96)
        self.assertFalse(used_default)

    def test_pmax_lookup_for_four_lane_road(self):
        link = Link(
            id="L2",
            name="Four lane",
            start_node_id="N1",
            end_node_id="N2",
            parameters={"lanes_total": 4},
        )
        pmax_base, n_effective, defaults = get_pmax_lookup(link)
        self.assertEqual(pmax_base, 3600.0)
        self.assertEqual(n_effective, 1)

    def test_analyze_odm_city_link_returns_hourly_variants(self):
        link = Link(
            id="L3",
            name="SKDF link",
            start_node_id="N1",
            end_node_id="N2",
            length_km=1.2,
            parameters={"lanes_total": 4, "lane_width_m": 3.5, "speed_limit_skdf": 60},
            metadata={
                "skdf": {
                    "traffic_aadt": 12000,
                    "capacity_values": [8800, 13800],
                    "speed_limit_values": [60],
                }
            },
        )
        results = analyze_odm_city_link(link, {"car": 1.0, "truck": 2.0})
        self.assertIsNotNone(results)
        self.assertIn("N_hour_avg", results)
        self.assertIn("N_hour_design", results)
        self.assertIn("P_odm", results)
        self.assertIn("LOS_avg", results)
        self.assertIn("LOS_design", results)
        self.assertEqual(results["capacity_skdf_reference"], [8800, 13800])


if __name__ == "__main__":
    unittest.main()
