"""放渣、放铜与转炉批次链。"""

from __future__ import annotations

import unittest

from flashsmelter.errors import GuardViolation, NotFoundError, StateTransitionError

from .helpers import feed_heat, make_app, run_heat, start_furnace


class TapChainTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()

    def test_matte_requires_slag_of_same_heat(self) -> None:
        start_furnace(self.app)
        feed_heat(self.app, "H-1")
        with self.assertRaises(NotFoundError) as blocked:
            self.app.matte.tap("ops", heat_id="H-1", ladle_id="L-1", target_tons=10.0)
        self.assertEqual("not-found", blocked.exception.code)

    def test_slag_requires_settler_readiness(self) -> None:
        start_furnace(self.app)
        feed_heat(self.app, "H-1")
        self.app.settler.update("ops", bath_level_m=0.7, slag_thickness_m=0.01, matte_level_m=0.5)
        with self.assertRaises(GuardViolation):
            self.app.slag.tap("ops", heat_id="H-1", target_tons=1.0)

    def test_tap_tons_cannot_exceed_available(self) -> None:
        start_furnace(self.app)
        feed_heat(self.app, "H-1")
        with self.assertRaises(GuardViolation) as blocked:
            self.app.slag.tap("ops", heat_id="H-1", target_tons=999.0)
        self.assertLessEqual(
            blocked.exception.details["available_tons"], self.app.settler.available_slag_tons()
        )
        self.app.settler.settle("ops", heat_id="H-1")
        self.app.slag.tap("ops", heat_id="H-1", target_tons=8.0)
        with self.assertRaises(GuardViolation):
            self.app.matte.tap("ops", heat_id="H-1", ladle_id="L-1", target_tons=999.0)

    def test_full_heat_produces_traceable_batch(self) -> None:
        start_furnace(self.app)
        status = run_heat(self.app, "H-2", "L-2")
        self.assertEqual("idle", status["state"])
        self.assertEqual(1, status["batches_completed"])
        batches = self.app.conv.batches()
        self.assertEqual(1, len(batches))
        self.assertEqual("L-2", batches[0]["ladle_id"])
        self.assertGreater(self.app.slag.status()["last_tap_seconds"], 0.0)
        self.assertGreater(self.app.matte.status()["last_tap_seconds"], 0.0)
        heat = self.app.furnace.heats()
        self.assertEqual(8.0, heat[0]["slag_tons"])
        charges = self.app.matte.charges()
        self.assertEqual("charged", charges[0]["status"])

    def test_charge_cannot_reuse_ladle(self) -> None:
        start_furnace(self.app)
        run_heat(self.app, "H-3", "L-3")
        with self.assertRaises(NotFoundError):
            self.app.conv.charge("ops", ladle_id="L-3")

    def test_converter_sequence_is_enforced(self) -> None:
        start_furnace(self.app)
        feed_heat(self.app, "H-4")
        self.app.furnace.tap("ops", heat_id="H-4", ladle_id="L-4", slag_tons=6.0, matte_tons=30.0)
        with self.assertRaises(StateTransitionError):
            self.app.conv.blow("ops", seconds=60.0)
        self.app.conv.charge("ops", ladle_id="L-4")
        with self.assertRaises(StateTransitionError):
            self.app.conv.charge("ops", ladle_id="L-4")
        with self.assertRaises(GuardViolation):
            self.app.conv.blow("ops", seconds=0.0)
        self.app.conv.blow("ops", seconds=120.0)
        with self.assertRaises(GuardViolation):
            self.app.conv.skim("ops", tons=999.0)
        self.app.conv.skim("ops", tons=2.0)
        with self.assertRaises(GuardViolation):
            self.app.conv.discharge("ops", tons=999.0)
        with self.assertRaises(StateTransitionError):
            self.app.conv.finish_batch("ops")
        self.app.conv.discharge("ops", tons=25.0)
        self.app.conv.discharge("ops", tons=5.0)
        self.assertEqual("idle", self.app.conv.finish_batch("ops")["state"])

    def test_converter_rejects_when_busy(self) -> None:
        start_furnace(self.app)
        run_heat(self.app, "H-5", "L-5")
        run_heat(self.app, "H-6", "L-6", charge_only=True)
        self.assertFalse(self.app.conv.can_accept(20.0))
        feed_heat(self.app, "H-7")
        self.app.clock.advance(self.app.settings.furnace_min_smelt_dwell_seconds + 1)
        self.app.settler.update("ops", bath_level_m=0.75, slag_thickness_m=0.16, matte_level_m=0.5)
        self.app.clock.advance(self.app.settings.settler_layering_dwell_seconds + 1)
        self.app.settler.settle("ops", heat_id="H-7")
        self.app.slag.tap("ops", heat_id="H-7", target_tons=6.0)
        with self.assertRaises(GuardViolation) as blocked:
            self.app.matte.tap("ops", heat_id="H-7", ladle_id="L-7", target_tons=20.0)
        self.assertEqual("guard-violation", blocked.exception.code)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
