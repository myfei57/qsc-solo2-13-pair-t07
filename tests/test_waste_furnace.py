"""余热锅炉联锁与闪速炉编排、停机顺序、状态恢复。"""

from __future__ import annotations

import unittest

from flashsmelter.application import Application
from flashsmelter.errors import ConflictError, GuardViolation, StateTransitionError

from .helpers import feed_heat, make_app, make_root, run_heat, settle_pool, start_furnace


class WasteLatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()

    def test_drum_level_low_latches_and_holds(self) -> None:
        self.app.waste.start("ops", drum_level=0.6)
        status = self.app.waste.update("ops", drum_level=0.3, exhaust_temp_c=320.0)
        self.assertEqual("latched", status["state"])
        self.assertEqual("drum-level-low", status["latch_reason"])
        with self.assertRaises(GuardViolation) as early:
            self.app.waste.reset("ops", note="补水完成", drum_level=0.6, exhaust_temp_c=320.0)
        self.assertIn("remaining_seconds", early.exception.details)
        self.app.clock.advance(self.app.settings.waste_latch_min_hold_seconds + 1)
        with self.assertRaises(GuardViolation):
            self.app.waste.reset("ops", note="补水完成", drum_level=0.6, exhaust_temp_c=999.0)
        with self.assertRaises(GuardViolation):
            self.app.waste.reset("ops", note="补水完成", drum_level=0.6, exhaust_temp_c=320.0, tube_leak=True)
        status = self.app.waste.reset("ops", note="补水完成", drum_level=0.6, exhaust_temp_c=320.0)
        self.assertEqual("circulating", status["state"])
        self.assertEqual(1, status["latch_count"])

    def test_over_temperature_and_leak_latch(self) -> None:
        self.app.waste.start("ops", drum_level=0.6)
        hot = self.app.waste.update(
            "ops", drum_level=0.6, exhaust_temp_c=self.app.settings.waste_exhaust_temp_max_c + 20
        )
        self.assertEqual("exhaust-over-temperature", hot["latch_reason"])
        self.app.clock.advance(self.app.settings.waste_latch_min_hold_seconds + 1)
        leak = self.app.waste.update("ops", drum_level=0.6, exhaust_temp_c=300.0, tube_leak=True)
        self.assertEqual("tube-leak", leak["latch_reason"])

    def test_cooldown_flow(self) -> None:
        self.app.waste.start("ops", drum_level=0.6)
        self.app.waste.update("ops", drum_level=0.6, exhaust_temp_c=300.0, steam_flow_tph=12.0)
        self.assertEqual("heat_exchanging", self.app.waste.state)
        self.assertEqual("cooling", self.app.waste.cooldown("ops")["state"])
        self.assertEqual("idle", self.app.waste.finish_cooling("ops")["state"])


class FurnaceOrchestrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()

    def test_start_sequence_and_latch_reset(self) -> None:
        start_furnace(self.app)
        status = self.app.furnace.status()
        self.assertEqual("oxygen_ready", status["state"])
        self.assertEqual("stable", status["subsystems"]["burner"])
        self.assertEqual("established", status["subsystems"]["oxygen"])
        self.assertEqual("circulating", status["subsystems"]["waste"])
        self.app.furnace.latch("ops", reason="settler-thermocouple")
        self.assertEqual("latched", self.app.furnace.state)
        self.app.waste.update("ops", drum_level=0.2, exhaust_temp_c=300.0)
        with self.assertRaises(GuardViolation) as blocked:
            self.app.furnace.reset("ops", note="处理完成")
        self.assertIn("waste-latched", blocked.exception.details["blockers"])

    def test_tap_requires_smelt_dwell(self) -> None:
        start_furnace(self.app)
        settle_pool(self.app)
        self.app.furnace.feed("ops", heat_id="H-1", rate_tph=150.0, tons=500.0)
        with self.assertRaises(GuardViolation) as too_early:
            self.app.furnace.tap("ops", heat_id="H-1", ladle_id="L-1", slag_tons=5.0, matte_tons=20.0)
        self.assertGreater(too_early.exception.details["remaining_seconds"], 0.0)
        self.app.clock.advance(self.app.settings.furnace_min_smelt_dwell_seconds + 1)
        self.assertEqual(
            "smelting",
            self.app.furnace.tap(
                "ops", heat_id="H-1", ladle_id="L-1", slag_tons=5.0, matte_tons=20.0
            )["state"],
        )

    def test_tap_before_feed_is_rejected(self) -> None:
        start_furnace(self.app)
        with self.assertRaises(StateTransitionError):
            self.app.furnace.tap("ops", heat_id="H-1", ladle_id="L-1", slag_tons=1.0, matte_tons=1.0)

    def test_startup_window_watchdog_blocks_late_feed(self) -> None:
        start_furnace(self.app)
        settle_pool(self.app)
        self.app.clock.advance(self.app.settings.furnace_transition_timeout_seconds + 1)
        with self.assertRaises(GuardViolation) as expired:
            self.app.furnace.feed("ops", heat_id="H-1", rate_tph=150.0, tons=100.0)
        self.assertIn("timeout_seconds", expired.exception.details)

    def test_stop_order_stops_feed_then_oxygen(self) -> None:
        start_furnace(self.app)
        feed_heat(self.app, "H-1")
        self.assertTrue(self.app.conc.is_flowing())
        status = self.app.furnace.stop("ops")
        self.assertEqual("stopped", status["state"])
        self.assertEqual("stopped", self.app.conc.state)
        self.assertEqual("degraded", self.app.oxygen.state)
        self.assertEqual("cooling", self.app.waste.state)
        self.assertEqual("cooling", self.app.burner.state)
        with self.assertRaises(StateTransitionError):
            self.app.furnace.stop("ops")

    def test_generation_conflict_blocks_stale_command(self) -> None:
        start_furnace(self.app)
        stale = self.app.generation.value
        settle_pool(self.app)
        self.app.furnace.feed("ops", heat_id="H-1", rate_tph=150.0, tons=300.0)
        with self.assertRaises(ConflictError) as conflict:
            self.app.furnace.feed(
                "ops", heat_id="H-1", rate_tph=150.0, tons=100.0, expected_generation=stale
            )
        self.assertEqual(stale, conflict.exception.details["expected"])

    def test_state_survives_restart(self) -> None:
        root = make_root()
        app = make_app(root=root)
        start_furnace(app)
        feed_heat(app, "H-9")
        app.conc.stop("ops")
        restarted = Application(app.settings, clock=app.clock)
        self.assertEqual("smelting", restarted.furnace.state)
        self.assertEqual("H-9", restarted.conc.status()["heat_id"])
        self.assertEqual("stopped", restarted.conc.state)
        self.assertAlmostEqual(
            app.conc.status()["fed_tons"], restarted.conc.status()["fed_tons"], places=3
        )
        self.assertTrue(restarted.store.verify().ok)

    def test_heat_report_tracks_totals(self) -> None:
        start_furnace(self.app)
        run_heat(self.app, "H-8", "L-8")
        report = self.app.furnace.heat_report()
        self.assertEqual("H-8", report["heat_id"])
        self.assertAlmostEqual(500.0, report["feed_tons"], places=3)
        self.assertAlmostEqual(8.0, report["slag_tapped_tons"], places=3)
        self.assertAlmostEqual(40.0, report["matte_tapped_tons"], places=3)
        self.assertEqual(1, report["heats_completed"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
