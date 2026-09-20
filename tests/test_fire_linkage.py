"""消防联动：探测定级、分区确认、误报中止、联动执行与处置复位。"""

from __future__ import annotations

import unittest

from flashsmelter.application import Application
from flashsmelter.config import Settings
from flashsmelter.errors import ConflictError, GuardViolation, StateTransitionError, ValidationError
from flashsmelter.runtime import ManualClock

from .helpers import make_app, make_root

ZONE = "reaction-tower"
ZONE2 = "settler"


def _confirm_level_alarm(fire, zone: str = ZONE, prefix: str = "SD") -> None:
    """在同一分区报两只自动探测器，把分区推进到确认火警。"""

    fire.report("loop-1", detector_id=f"{prefix}-101", zone=zone)
    fire.report("loop-1", detector_id=f"{prefix}-102", zone=zone)


class FireReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()
        self.fire = self.app.fire

    def test_single_auto_detector_raises_prealarm_only(self) -> None:
        status = self.fire.report("loop-1", detector_id="SD-101", zone=ZONE)
        self.assertEqual("prealarm", status["zones"][ZONE]["state"])
        self.assertEqual("prealarm", status["zones"][ZONE]["level"])
        self.assertIsNone(status["zones"][ZONE]["pending_order"])
        self.assertEqual(0, status["pending_count"])

    def test_two_auto_detectors_confirm_and_create_order(self) -> None:
        self.fire.report("loop-1", detector_id="SD-101", zone=ZONE)
        status = self.fire.report("loop-1", detector_id="SD-102", zone=ZONE)
        self.assertEqual("confirmed", status["zones"][ZONE]["state"])
        self.assertIsNotNone(status["zones"][ZONE]["pending_order"])
        order = status["orders"][-1]
        self.assertEqual("pending", order["state"])
        self.assertEqual(ZONE, order["zone"])
        self.assertEqual(["SD-101", "SD-102"], sorted(order["triggers"]))

    def test_manual_call_point_confirms_immediately(self) -> None:
        status = self.fire.report("ops", detector_id="MCP-01", zone=ZONE, kind="manual")
        self.assertEqual("confirmed", status["zones"][ZONE]["state"])
        self.assertIsNotNone(status["zones"][ZONE]["pending_order"])

    def test_unknown_zone_and_kind_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.fire.report("loop-1", detector_id="SD-1", zone="nowhere")
        with self.assertRaises(ValidationError):
            self.fire.report("loop-1", detector_id="SD-1", zone=ZONE, kind="flame")

    def test_detector_cannot_jump_zone(self) -> None:
        self.fire.report("loop-1", detector_id="SD-101", zone=ZONE)
        with self.assertRaises(ValidationError) as conflict:
            self.fire.report("loop-1", detector_id="SD-101", zone=ZONE2)
        self.assertEqual(ZONE, conflict.exception.details["registered_zone"])

    def test_repeat_report_is_idempotent(self) -> None:
        self.fire.report("loop-1", detector_id="SD-101", zone=ZONE)
        status = self.fire.report("loop-1", detector_id="SD-101", zone=ZONE)
        self.assertEqual("prealarm", status["zones"][ZONE]["state"])
        self.assertEqual(["SD-101"], status["zones"][ZONE]["active_alarms"])
        self.assertEqual(0, status["pending_count"])


class FireConfirmTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()
        self.fire = self.app.fire
        _confirm_level_alarm(self.fire)

    def test_confirm_executes_linkage_steps_in_order(self) -> None:
        status = self.fire.confirm("operator-a", zone=ZONE)
        self.assertEqual("dispatched", status["zones"][ZONE]["state"])
        self.assertEqual("cut", status["devices"][ZONE]["non_fire_power"])
        self.assertEqual("open", status["devices"][ZONE]["deluge_valve"])
        self.assertEqual({"FP-01": "running", "FP-02": "running"}, status["pumps"])
        order = status["orders"][-1]
        self.assertEqual("executed", order["state"])
        self.assertEqual("operator-a", order["decided_by"])
        self.assertEqual("manual-confirm", order["via"])
        self.assertEqual(
            ["cut_non_fire_power", "open_deluge_valve", "start_fire_pumps"],
            [step["step"] for step in order["steps"]],
        )

    def test_confirm_writes_durable_intent_before_acting(self) -> None:
        self.fire.confirm("operator-a", zone=ZONE)
        order = self.fire.status()["orders"][-1]
        intent = self.app.store.get(self.fire.key("intent", f"linkage-{order['order_id']}"))
        self.assertIsNotNone(intent)
        self.assertEqual(ZONE, intent.payload["zone"])
        self.assertEqual(order["intent_key"], intent.key)

    def test_confirm_requires_confirmed_zone(self) -> None:
        with self.assertRaises(StateTransitionError):
            self.fire.confirm("operator-a", zone=ZONE2)

    def test_confirm_rejects_mismatched_order_id(self) -> None:
        with self.assertRaises(ValidationError):
            self.fire.confirm("operator-a", zone=ZONE, order_id="LD-9999")

    def test_confirm_with_stale_generation_is_rejected(self) -> None:
        stale = self.app.generation.value
        self.app.generation.bump("另一路操作改过状态")
        with self.assertRaises(ConflictError):
            self.fire.confirm("operator-a", zone=ZONE, expected_generation=stale)


class FireAbortTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()
        self.fire = self.app.fire
        _confirm_level_alarm(self.fire)

    def test_abort_cancels_pending_linkage_without_acting(self) -> None:
        status = self.fire.abort("operator-b", zone=ZONE, reason="现场核实为粉尘误报")
        self.assertEqual("normal", status["zones"][ZONE]["state"])
        self.assertEqual([], status["zones"][ZONE]["active_alarms"])
        order = status["orders"][-1]
        self.assertEqual("aborted", order["state"])
        self.assertEqual("现场核实为粉尘误报", order["abort_reason"])
        self.assertEqual("operator-b", order["decided_by"])
        # 误报中止后设备必须保持未动作。
        self.assertEqual("closed", status["devices"][ZONE]["deluge_valve"])
        self.assertEqual("normal", status["devices"][ZONE]["non_fire_power"])
        self.assertEqual("standby", status["pumps"]["FP-01"])

    def test_abort_requires_reason(self) -> None:
        with self.assertRaises(GuardViolation):
            self.fire.abort("operator-b", zone=ZONE, reason="")

    def test_abort_only_while_pending(self) -> None:
        self.fire.confirm("operator-a", zone=ZONE)
        with self.assertRaises(StateTransitionError):
            self.fire.abort("operator-b", zone=ZONE, reason="联动已执行")


class FireScanTest(unittest.TestCase):
    def test_auto_mode_executes_only_after_window(self) -> None:
        app = make_app()
        _confirm_level_alarm(app.fire)
        app.clock.advance(app.settings.fire_confirm_window_seconds - 5)
        result = app.fire.scan("control-system")
        self.assertEqual([], result["executed"])
        self.assertEqual("confirmed", app.fire.status()["zones"][ZONE]["state"])
        app.clock.advance(10)
        result = app.fire.scan("control-system")
        self.assertEqual(1, len(result["executed"]))
        status = app.fire.status()
        self.assertEqual("dispatched", status["zones"][ZONE]["state"])
        self.assertEqual("open", status["devices"][ZONE]["deluge_valve"])
        order = status["orders"][-1]
        self.assertEqual("auto-scan", order["via"])

    def test_manual_mode_waits_for_operator(self) -> None:
        app = make_app(fire_auto_execute=False)
        _confirm_level_alarm(app.fire)
        app.clock.advance(app.settings.fire_confirm_window_seconds + 1)
        result = app.fire.scan("control-system")
        self.assertEqual([], result["executed"])
        self.assertEqual(1, len(result["awaiting_manual"]))
        self.assertEqual("confirmed", app.fire.status()["zones"][ZONE]["state"])
        # 窗口过后人工确认仍然有效。
        status = app.fire.confirm("operator-a", zone=ZONE)
        self.assertEqual("dispatched", status["zones"][ZONE]["state"])

    def test_abort_inside_window_prevents_auto_execution(self) -> None:
        app = make_app()
        _confirm_level_alarm(app.fire)
        app.fire.abort("operator-b", zone=ZONE, reason="误报")
        app.clock.advance(app.settings.fire_confirm_window_seconds + 1)
        result = app.fire.scan("control-system")
        self.assertEqual([], result["executed"])
        self.assertEqual("normal", app.fire.status()["zones"][ZONE]["state"])


class FireResetTest(unittest.TestCase):
    def test_reset_after_dispatch_restores_devices(self) -> None:
        app = make_app()
        _confirm_level_alarm(app.fire)
        app.fire.confirm("operator-a", zone=ZONE)
        status = app.fire.reset("operator-a", zone=ZONE, note="火已扑灭，现场清理完毕")
        self.assertEqual("normal", status["zones"][ZONE]["state"])
        self.assertEqual([], status["zones"][ZONE]["active_alarms"])
        self.assertEqual("closed", status["devices"][ZONE]["deluge_valve"])
        self.assertEqual("normal", status["devices"][ZONE]["non_fire_power"])
        self.assertEqual("standby", status["pumps"]["FP-01"])

    def test_reset_prealarm_clears_alarm(self) -> None:
        app = make_app()
        app.fire.report("loop-1", detector_id="SD-101", zone=ZONE)
        status = app.fire.reset("operator-a", zone=ZONE, note="探测器吹扫后复归")
        self.assertEqual("normal", status["zones"][ZONE]["state"])
        self.assertEqual([], status["zones"][ZONE]["active_alarms"])

    def test_reset_requires_note_and_matching_state(self) -> None:
        app = make_app()
        with self.assertRaises(StateTransitionError):
            app.fire.reset("operator-a", zone=ZONE, note="无事可复位")
        _confirm_level_alarm(app.fire)
        with self.assertRaises(GuardViolation):
            app.fire.reset("operator-a", zone=ZONE, note="")
        with self.assertRaises(StateTransitionError):
            app.fire.reset("operator-a", zone=ZONE, note="待确认的联动单必须先确认或中止")

    def test_pumps_keep_running_while_other_zone_dispatched(self) -> None:
        app = make_app()
        _confirm_level_alarm(app.fire, zone=ZONE, prefix="SD")
        _confirm_level_alarm(app.fire, zone=ZONE2, prefix="SD2")
        app.fire.confirm("operator-a", zone=ZONE)
        app.fire.confirm("operator-a", zone=ZONE2)
        app.fire.reset("operator-a", zone=ZONE, note="反应塔区处置完毕")
        self.assertEqual("running", app.fire.status()["pumps"]["FP-01"])
        status = app.fire.reset("operator-a", zone=ZONE2, note="沉淀池区处置完毕")
        self.assertEqual("standby", status["pumps"]["FP-01"])


class FireAuditTest(unittest.TestCase):
    def test_full_flow_is_audited(self) -> None:
        app = make_app()
        _confirm_level_alarm(app.fire)
        app.fire.confirm("operator-a", zone=ZONE)
        app.fire.reset("operator-a", zone=ZONE, note="处置完毕")
        events = app.audit_events(target=f"fire:{ZONE}")
        self.assertEqual(["report", "report", "confirm", "reset"], [event["action"] for event in events])
        self.assertTrue(all(event["outcome"] == "ok" for event in events))
        confirm = events[2]
        self.assertEqual("operator-a", confirm["actor"])
        self.assertIn("order_id", confirm["details"])

    def test_rejected_operations_are_audited(self) -> None:
        app = make_app()
        with self.assertRaises(StateTransitionError):
            app.fire.confirm("operator-a", zone=ZONE)
        events = app.audit_events(outcome="rejected")
        self.assertEqual("confirm", events[-1]["action"])
        self.assertEqual(f"fire:{ZONE}", events[-1]["target"])


class FirePersistenceTest(unittest.TestCase):
    def test_state_survives_restart(self) -> None:
        clock = ManualClock()
        root = make_root()
        app = Application(Settings(root=root), clock=clock)
        _confirm_level_alarm(app.fire)
        app.fire.confirm("operator-a", zone=ZONE)
        reopened = Application(Settings(root=root), clock=clock)
        status = reopened.fire.status()
        self.assertEqual("dispatched", status["zones"][ZONE]["state"])
        self.assertEqual("open", status["devices"][ZONE]["deluge_valve"])
        self.assertEqual("cut", status["devices"][ZONE]["non_fire_power"])
        self.assertEqual("running", status["pumps"]["FP-01"])
        self.assertEqual("executed", status["orders"][-1]["state"])

    def test_pending_order_survives_restart_for_later_confirm(self) -> None:
        clock = ManualClock()
        root = make_root()
        app = Application(Settings(root=root), clock=clock)
        _confirm_level_alarm(app.fire)
        reopened = Application(Settings(root=root), clock=clock)
        status = reopened.fire.confirm("operator-a", zone=ZONE)
        self.assertEqual("dispatched", status["zones"][ZONE]["state"])


class FireSettingsTest(unittest.TestCase):
    def test_invalid_fire_settings_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            Settings(fire_zones=" ").validate()
        with self.assertRaises(ValidationError):
            Settings(fire_zones="a,a").validate()
        with self.assertRaises(ValidationError):
            Settings(fire_pumps="").validate()
        with self.assertRaises(ValidationError):
            Settings(fire_confirm_window_seconds=0).validate()

    def test_env_override_parses_bool(self) -> None:
        settings = Settings.from_env({"FLASHSMELTER_FIRE_AUTO_EXECUTE": "off"})
        self.assertFalse(settings.fire_auto_execute)
        settings = Settings.from_env({"FLASHSMELTER_FIRE_ZONES": "a,b"})
        self.assertEqual("a,b", settings.fire_zones)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
