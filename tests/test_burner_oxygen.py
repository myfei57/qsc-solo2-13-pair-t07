"""燃烧器与富氧系统：落盘凭证、基线时效、调档与回滚。"""

from __future__ import annotations

import unittest

from flashsmelter.errors import GuardViolation, LatchEngagedError, StaleBaselineError, StateTransitionError

from .helpers import make_app


class BurnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()
        self.burner = self.app.burner

    def test_ignite_requires_pressure_and_air(self) -> None:
        with self.assertRaises(GuardViolation) as low_pressure:
            self.burner.ignite("ops", fuel_pressure_kpa=50.0, air_flow_nm3h=6000.0)
        self.assertEqual("guard-violation", low_pressure.exception.code)
        with self.assertRaises(GuardViolation):
            self.burner.ignite("ops", fuel_pressure_kpa=200.0, air_flow_nm3h=100.0)
        self.assertEqual("off", self.burner.state)

    def test_ignite_flame_and_stabilize_are_ordered(self) -> None:
        with self.assertRaises(StateTransitionError):
            self.burner.stabilize("ops")
        self.burner.ignite("ops", fuel_pressure_kpa=200.0, air_flow_nm3h=6000.0)
        with self.assertRaises(StateTransitionError):
            self.burner.stabilize("ops")
        self.burner.confirm_flame("ops")
        with self.assertRaises(GuardViolation):
            self.burner.stabilize("ops", hold_seconds=1.0)
        status = self.burner.stabilize("ops")
        self.assertEqual("stable", status["state"])
        ok, detail = self.burner.stable_attestation()
        self.assertTrue(ok)
        self.assertEqual("stable", detail["state"])

    def test_attestation_expires_with_time(self) -> None:
        self.burner.ignite("ops", fuel_pressure_kpa=200.0, air_flow_nm3h=6000.0)
        self.burner.confirm_flame("ops")
        self.burner.stabilize("ops")
        self.app.clock.advance(self.app.settings.burner_record_max_age_seconds + 5)
        ok, detail = self.burner.stable_attestation()
        self.assertFalse(ok)
        self.assertEqual("record-stale", detail["reason"])

    def test_trip_latches_until_explicit_reset(self) -> None:
        self.burner.ignite("ops", fuel_pressure_kpa=200.0, air_flow_nm3h=6000.0)
        self.burner.confirm_flame("ops")
        self.burner.stabilize("ops")
        self.burner.trip("ops", reason="flame-out")
        self.assertTrue(self.burner.is_latched())
        with self.assertRaises(LatchEngagedError):
            self.burner.ignite("ops", fuel_pressure_kpa=200.0, air_flow_nm3h=6000.0)
        with self.assertRaises(GuardViolation):
            self.burner.reset("ops", note="")
        with self.assertRaises(GuardViolation):
            self.burner.reset("ops", note="done", fuel_pressure_kpa=10.0)
        status = self.burner.reset("ops", note="更换火焰探测器")
        self.assertEqual("off", status["state"])
        self.assertIsNone(status["latch_reason"])

    def test_cooling_flow(self) -> None:
        self.burner.ignite("ops", fuel_pressure_kpa=200.0, air_flow_nm3h=6000.0)
        self.burner.confirm_flame("ops")
        self.burner.stabilize("ops")
        self.assertEqual("cooling", self.burner.cool_down("ops")["state"])
        self.assertEqual("off", self.burner.finish_cooling("ops")["state"])
        with self.assertRaises(StateTransitionError):
            self.burner.finish_cooling("ops")

    def test_stale_command_generation_is_rejected(self) -> None:
        from flashsmelter.errors import ConflictError

        self.burner.ignite("ops", fuel_pressure_kpa=200.0, air_flow_nm3h=6000.0)
        self.burner.confirm_flame("ops")
        self.burner.stabilize("ops")
        stale = self.app.generation.value
        self.app.generation.bump("另一路操作改过状态")
        with self.assertRaises(ConflictError) as conflict:
            self.burner.cool_down("ops", expected_generation=stale)
        self.assertEqual(stale, conflict.exception.details["expected"])
        self.assertEqual(self.app.generation.value, conflict.exception.details["current"])


class OxygenTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()
        self.oxygen = self.app.oxygen

    def _baseline(self) -> None:
        self.oxygen.set_baseline("analyzer", value=0.6, source="analyzer-a")

    def test_establish_requires_fresh_baseline(self) -> None:
        with self.assertRaises(StaleBaselineError):
            self.oxygen.establish("ops", target_enrichment=0.6, flow_nm3h=9000.0)
        self._baseline()
        self.app.clock.advance(self.app.settings.oxygen_baseline_window_seconds + 1)
        with self.assertRaises(StaleBaselineError) as stale:
            self.oxygen.establish("ops", target_enrichment=0.6, flow_nm3h=9000.0)
        self.assertEqual("stale-baseline", stale.exception.code)
        self.assertFalse(self.oxygen.baseline_status()["fresh"])

    def test_establish_ramp_and_limits(self) -> None:
        self._baseline()
        with self.assertRaises(GuardViolation):
            self.oxygen.establish("ops", target_enrichment=0.9, flow_nm3h=9000.0)
        self.oxygen.establish("ops", target_enrichment=0.6, flow_nm3h=9000.0)
        with self.assertRaises(GuardViolation):
            self.oxygen.ramp("ops", target_enrichment=0.7)
        status = self.oxygen.ramp("ops", target_enrichment=0.62)
        self.assertAlmostEqual(0.62, status["setpoint"], places=4)
        feed = self.oxygen.ensure_established_for_feed()
        self.assertAlmostEqual(0.62, feed["enrichment"], places=4)

    def test_reading_older_than_baseline_is_rejected(self) -> None:
        self.oxygen.set_baseline("analyzer", value=0.6, source="analyzer-a")
        stale_at = self.app.clock.timestamp_iso()
        self.app.clock.advance(10)
        self.oxygen.set_baseline("analyzer", value=0.61, source="analyzer-b")
        with self.assertRaises(StaleBaselineError):
            self.oxygen.record_reading("analyzer", value=0.59, observed_at=stale_at)
        status = self.oxygen.record_reading("analyzer", value=0.62)
        self.assertAlmostEqual(0.62, status["enrichment"], places=4)

    def test_rollback_returns_to_last_good_setpoint(self) -> None:
        self._baseline()
        self.oxygen.establish("ops", target_enrichment=0.6, flow_nm3h=9000.0)
        with self.assertRaises(GuardViolation):
            self.oxygen.rollback("ops", reason="")
        status = self.oxygen.rollback("ops", reason="氧浓波动")
        self.assertEqual("rolled_back", status["state"])
        self.assertAlmostEqual(0.6, status["setpoint"], places=4)
        self.assertEqual(1, status["rollback_count"])

    def test_deviation_beyond_tolerance_degrades_supply(self) -> None:
        self._baseline()
        self.oxygen.establish("ops", target_enrichment=0.6, flow_nm3h=9000.0)
        within = self.oxygen.record_reading("analyzer", value=0.605)
        self.assertEqual("established", within["state"])
        beyond = self.oxygen.record_reading("analyzer", value=0.66)
        self.assertEqual("degraded", beyond["state"])

    def test_ramp_down_waits_for_feed_to_stop(self) -> None:
        from .helpers import feed_heat, start_furnace

        start_furnace(self.app)
        feed_heat(self.app)
        with self.assertRaises(GuardViolation) as blocked:
            self.oxygen.ramp_down("ops")
        self.assertEqual("guard-violation", blocked.exception.code)
        self.app.conc.stop("ops")
        self.assertEqual("degraded", self.oxygen.ramp_down("ops")["state"])

    def test_generation_conflict_is_reported(self) -> None:
        self._baseline()
        self.oxygen.establish("ops", target_enrichment=0.6, flow_nm3h=9000.0)
        stale_generation = self.app.generation.value
        self.oxygen.ramp("ops", target_enrichment=0.62)
        from flashsmelter.errors import ConflictError

        with self.assertRaises(ConflictError):
            self.oxygen.ramp("ops", target_enrichment=0.64, expected_generation=stale_generation)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
