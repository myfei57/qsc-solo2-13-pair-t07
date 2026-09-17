"""精矿喷吹门控与沉淀池分层。"""

from __future__ import annotations

import unittest

from flashsmelter.errors import GuardViolation, LatchEngagedError, StaleBaselineError, StateTransitionError

from .helpers import make_app, settle_pool, start_furnace


class ConcentrateGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()

    def test_arm_is_rejected_before_burner_record_is_durable(self) -> None:
        with self.assertRaises(GuardViolation) as blocked:
            self.app.conc.arm("ops", heat_id="H-1")
        self.assertIn("burner", blocked.exception.details)

    def test_arm_requires_fresh_oxygen_baseline(self) -> None:
        from flashsmelter.application import Application
        from flashsmelter.config import Settings
        from flashsmelter.runtime import ManualClock

        from .helpers import make_root

        self.app = Application(
            Settings(
                root=make_root(),
                burner_record_max_age_seconds=600.0,
                oxygen_baseline_window_seconds=30.0,
            ),
            clock=ManualClock(),
        )
        start_furnace(self.app)
        self.app.clock.advance(31)
        with self.assertRaises(StaleBaselineError):
            self.app.conc.arm("ops", heat_id="H-1")

    def test_arm_requires_waste_latch_cleared(self) -> None:
        start_furnace(self.app)
        self.app.waste.update("ops", drum_level=0.1, exhaust_temp_c=300.0)
        self.assertTrue(self.app.waste.is_latched())
        with self.assertRaises(LatchEngagedError):
            self.app.conc.arm("ops", heat_id="H-1")
        self.assertIn("waste", self.app.conc.gates()["blockers"][0])

    def test_inject_updates_budget_and_valve_state(self) -> None:
        start_furnace(self.app)
        settle_pool(self.app)
        self.app.conc.arm("ops", heat_id="H-1")
        status = self.app.conc.inject("ops", rate_tph=140.0, tons=600.0)
        self.assertEqual("injecting", status["state"])
        self.assertTrue(status["flowing"])
        self.assertAlmostEqual(600.0, status["fed_tons"], places=3)
        self.assertAlmostEqual(
            self.app.settings.heat_feed_budget_tons - 600.0, status["budget_remaining_tons"], places=3
        )
        with self.assertRaises(GuardViolation) as over_rate:
            self.app.conc.inject("ops", rate_tph=self.app.settings.feed_rate_max_tph + 10, tons=10.0)
        self.assertEqual("guard-violation", over_rate.exception.code)
        with self.assertRaises(GuardViolation):
            self.app.conc.inject("ops", rate_tph=100.0, tons=1000.0)
        paused = self.app.conc.pause("ops")
        self.assertFalse(paused["flowing"])
        stopped = self.app.conc.stop("ops")
        self.assertEqual("stopped", stopped["state"])
        released = self.app.conc.release_heat("ops")
        self.assertEqual("blocked", released["state"])
        self.assertAlmostEqual(0.0, released["fed_tons"], places=3)

    def test_inject_before_arm_is_rejected(self) -> None:
        start_furnace(self.app)
        with self.assertRaises(StateTransitionError):
            self.app.conc.inject("ops", rate_tph=100.0, tons=10.0)

    def test_arming_new_heat_resets_budget(self) -> None:
        start_furnace(self.app)
        settle_pool(self.app)
        self.app.furnace.feed("ops", heat_id="H-1", rate_tph=150.0, tons=500.0)
        self.app.conc.stop("ops")
        self.app.furnace.feed("ops", heat_id="H-2", rate_tph=150.0, tons=200.0)
        status = self.app.conc.status()
        self.assertEqual("H-2", status["heat_id"])
        self.assertAlmostEqual(200.0, status["fed_tons"], places=3)


class SettlerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()

    def test_inconsistent_layer_data_is_rejected(self) -> None:
        with self.assertRaises(GuardViolation):
            self.app.settler.update("ops", bath_level_m=0.3, slag_thickness_m=0.2, matte_level_m=0.2)
        with self.assertRaises(GuardViolation):
            self.app.settler.update("ops", bath_level_m=0.5, slag_thickness_m=-0.1, matte_level_m=0.2)

    def test_ready_requires_bath_layers_and_dwell(self) -> None:
        self.app.settler.update("ops", bath_level_m=0.2, slag_thickness_m=0.05, matte_level_m=0.1)
        self.assertFalse(self.app.settler.requirements()["ready"])
        self.assertIn("bath-level-below-minimum", self.app.settler.requirements()["blockers"])
        self.app.settler.update("ops", bath_level_m=0.7, slag_thickness_m=0.02, matte_level_m=0.5)
        self.assertIn("slag-layer-too-thin", self.app.settler.requirements()["slag_blockers"])
        self.app.settler.update("ops", bath_level_m=0.7, slag_thickness_m=0.15, matte_level_m=0.5)
        requirements = self.app.settler.requirements()
        self.assertTrue(requirements["bath_ok"])
        self.assertIn("layering-dwell-insufficient", requirements["blockers"])
        self.app.clock.advance(self.app.settings.settler_layering_dwell_seconds + 1)
        self.assertTrue(self.app.settler.requirements()["ready"])
        status = self.app.settler.settle("ops", heat_id="H-7")
        self.assertEqual("tap_ready", status["state"])
        self.assertEqual("H-7", status["heat_id"])

    def test_consume_limits_and_tap_pairing(self) -> None:
        self.app.settler.update("ops", bath_level_m=0.6, slag_thickness_m=0.05, matte_level_m=0.4)
        self.app.clock.advance(self.app.settings.settler_layering_dwell_seconds + 1)
        with self.assertRaises(GuardViolation):
            self.app.settler.consume("slag", 999.0)
        with self.assertRaises(GuardViolation):
            self.app.settler.begin_tap("ops", kind="metal")
        self.app.settler.settle("ops", heat_id="H-1")
        self.app.settler.begin_tap("ops", kind="matte")
        with self.assertRaises(GuardViolation):
            self.app.settler.end_tap("ops", kind="slag", tons=1.0)
        status = self.app.settler.end_tap("ops", kind="matte", tons=10.0)
        self.assertIsNone(status["active_tap"])
        self.assertAlmostEqual(10.0, status["taps"][-1]["tons"], places=3)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
