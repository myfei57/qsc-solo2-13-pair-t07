"""余热锅炉组件。

反应塔与沉淀池的高温烟气进余热锅炉换热降温。汽包水位、排烟温度与管束泄漏任何
一项越限都会触发闩锁；闩锁是保持型的：工况恢复后仍锁住，必须由人工在最短保持
时长之后显式复位，且复位前先把处理说明落盘。
"""

from __future__ import annotations

from typing import Any, Mapping

from ..component import Component, ensure_actor
from ..errors import GuardViolation, StateTransitionError
from ..machine import StateMachine
from ..runtime import RuntimeContext

STATES = ("idle", "circulating", "heat_exchanging", "latched", "cooling")

TRANSITIONS: Mapping[str, tuple[str, ...]] = {
    "idle": ("circulating", "latched"),
    "circulating": ("heat_exchanging", "latched", "cooling"),
    "heat_exchanging": ("latched", "cooling"),
    "latched": ("circulating", "cooling"),
    "cooling": ("idle", "latched"),
}


class WasteHeatBoiler(Component):
    name = "waste"

    def __init__(self, ctx: RuntimeContext) -> None:
        super().__init__(ctx)
        self._machine = StateMachine("waste", "idle", TRANSITIONS, ctx.clock)
        self._drum_level = 0.0
        self._exhaust_temp_c = 0.0
        self._tube_leak = False
        self._steam_flow_tph = 0.0
        self._latch_reason: str | None = None
        self._latched_at: float | None = None
        self._latch_count = 0
        self._last_update_at: str | None = None
        restored = self.restore()
        if restored is not None:
            self._machine.restore(restored)
            self._drum_level = float(restored.get("drum_level", 0.0))
            self._exhaust_temp_c = float(restored.get("exhaust_temp_c", 0.0))
            self._tube_leak = bool(restored.get("tube_leak", False))
            self._steam_flow_tph = float(restored.get("steam_flow_tph", 0.0))
            self._latch_reason = restored.get("latch_reason")
            self._latched_at = restored.get("latched_at")
            self._latch_count = int(restored.get("latch_count", 0))
            self._last_update_at = restored.get("last_update_at")
        self._refresh_gauges()

    # ------------------------------------------------------------------ 动作
    def start(
        self,
        actor: str,
        *,
        drum_level: float,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "start",
            "waste",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            if self._machine.state not in ("idle", "cooling"):
                raise StateTransitionError(
                    "余热锅炉当前不能启动", details={"state": self._machine.state}
                )
            if drum_level < self.settings.waste_drum_level_min:
                raise GuardViolation(
                    "汽包水位低于启动下限",
                    details={"drum_level": drum_level, "min": self.settings.waste_drum_level_min},
                )
            self._drum_level = drum_level
            self._machine.to("circulating", actor, "循环泵投运")
            record = self._persist(reason="start")
            trace.attach(record).note("drum_level", drum_level)
            return self.status()

    def update(
        self,
        actor: str,
        *,
        drum_level: float,
        exhaust_temp_c: float,
        tube_leak: bool = False,
        steam_flow_tph: float = 0.0,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "update",
            "waste",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            if drum_level < 0 or exhaust_temp_c < 0 or steam_flow_tph < 0:
                raise GuardViolation(
                    "锅炉测点不能为负",
                    details={
                        "drum_level": drum_level,
                        "exhaust_temp_c": exhaust_temp_c,
                        "steam_flow_tph": steam_flow_tph,
                    },
                )
            self._drum_level = drum_level
            self._exhaust_temp_c = exhaust_temp_c
            self._tube_leak = tube_leak
            self._steam_flow_tph = steam_flow_tph
            self._last_update_at = self.clock.timestamp_iso()
            trigger = self._latch_trigger()
            if trigger is not None:
                reason, detail = trigger
                intent = self.write_intent(
                    "latch",
                    {
                        "action": "latch",
                        "reason": reason,
                        "detail": detail,
                        "at": self.clock.timestamp_iso(),
                        "actor": actor,
                    },
                )
                if self._machine.state != "latched":
                    self._machine.to("latched", actor, reason)
                self._latch_reason = reason
                self._latched_at = self.clock.timestamp()
                self._latch_count += 1
                record = self._persist(reason="latch")
                trace.attach(record).note("latch_reason", reason).note("intent_version", intent.version)
                return self.status()
            if self._machine.state == "circulating" and (steam_flow_tph > 0 or exhaust_temp_c > 0):
                self._machine.to("heat_exchanging", actor, "烟气进入换热")
            record = self._persist(reason="update")
            trace.attach(record).note("latch_trigger", None)
            return self.status()

    def cooldown(
        self,
        actor: str,
        *,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "cooldown",
            "waste",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            if self._machine.state not in ("circulating", "heat_exchanging", "latched"):
                raise StateTransitionError("余热锅炉当前无需冷却", details={"state": self._machine.state})
            self._machine.to("cooling", actor, "转入冷却")
            self._steam_flow_tph = 0.0
            self._exhaust_temp_c = 0.0
            record = self._persist(reason="cooldown")
            trace.attach(record)
            return self.status()

    def finish_cooling(
        self,
        actor: str,
        *,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "finish_cooling",
            "waste",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            self._machine.require("cooling", "冷却结束")
            self._machine.to("idle", actor, "冷却完成")
            record = self._persist(reason="finish_cooling")
            trace.attach(record)
            return self.status()

    def reset(
        self,
        actor: str,
        *,
        note: str,
        drum_level: float,
        exhaust_temp_c: float,
        tube_leak: bool = False,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "reset",
            "waste",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            self._machine.require("latched", "联锁复位")
            if not note:
                raise GuardViolation("复位必须填写处理说明")
            remaining = self._hold_remaining()
            if remaining > 0:
                raise GuardViolation(
                    "闩锁最短保持时长未到，禁止复位",
                    details={
                        "remaining_seconds": round(remaining, 3),
                        "min_hold_seconds": self.settings.waste_latch_min_hold_seconds,
                    },
                )
            if tube_leak:
                raise GuardViolation("管束泄漏未消除，禁止复位")
            if drum_level < self.settings.waste_drum_level_min:
                raise GuardViolation(
                    "汽包水位未恢复，禁止复位",
                    details={"drum_level": drum_level, "min": self.settings.waste_drum_level_min},
                )
            if exhaust_temp_c > self.settings.waste_exhaust_temp_max_c:
                raise GuardViolation(
                    "排烟温度仍然超限，禁止复位",
                    details={
                        "exhaust_temp_c": exhaust_temp_c,
                        "max": self.settings.waste_exhaust_temp_max_c,
                    },
                )
            intent = self.write_intent(
                "reset",
                {
                    "action": "reset",
                    "note": note,
                    "latch_reason": self._latch_reason,
                    "at": self.clock.timestamp_iso(),
                    "actor": actor,
                },
            )
            self._drum_level = drum_level
            self._exhaust_temp_c = exhaust_temp_c
            self._tube_leak = False
            self._machine.to("circulating", actor, f"闩锁复位：{note}")
            self._latch_reason = None
            self._latched_at = None
            record = self._persist(reason="reset")
            trace.attach(record).note("note", note).note("intent_version", intent.version)
            return self.status()

    # ------------------------------------------------------------------ 查询
    @property
    def state(self) -> str:
        return self._machine.state

    def is_latched(self) -> bool:
        return self._machine.state == "latched"

    def hold_remaining(self) -> float:
        return self._hold_remaining()

    def status(self) -> Mapping[str, Any]:
        return {
            "state": self._machine.state,
            "drum_level": round(self._drum_level, 4),
            "drum_level_min": self.settings.waste_drum_level_min,
            "exhaust_temp_c": round(self._exhaust_temp_c, 3),
            "exhaust_temp_max_c": self.settings.waste_exhaust_temp_max_c,
            "tube_leak": self._tube_leak,
            "steam_flow_tph": round(self._steam_flow_tph, 3),
            "latch_reason": self._latch_reason,
            "latched_at": self._latched_at,
            "latch_count": self._latch_count,
            "hold_remaining_seconds": round(self.hold_remaining(), 3),
            "last_update_at": self._last_update_at,
            "history": list(self._machine.history),
        }

    # ------------------------------------------------------------------ 内部
    def _latch_trigger(self) -> tuple[str, dict[str, Any]] | None:
        if self._tube_leak:
            return "tube-leak", {"tube_leak": True}
        if self._drum_level < self.settings.waste_drum_level_min:
            return "drum-level-low", {
                "drum_level": self._drum_level,
                "min": self.settings.waste_drum_level_min,
            }
        if self._exhaust_temp_c > self.settings.waste_exhaust_temp_max_c:
            return "exhaust-over-temperature", {
                "exhaust_temp_c": self._exhaust_temp_c,
                "max": self.settings.waste_exhaust_temp_max_c,
            }
        return None

    def _hold_remaining(self) -> float:
        if self._latched_at is None:
            return 0.0
        elapsed = max(0.0, self.clock.timestamp() - float(self._latched_at))
        return max(0.0, self.settings.waste_latch_min_hold_seconds - elapsed)

    def _persist(self, *, reason: str) -> Any:
        payload = {
            "state": self._machine.state,
            "reason": reason,
            "written_epoch": self.clock.timestamp(),
            "written_at": self.clock.timestamp_iso(),
            "drum_level": round(self._drum_level, 4),
            "exhaust_temp_c": round(self._exhaust_temp_c, 3),
            "tube_leak": self._tube_leak,
            "steam_flow_tph": round(self._steam_flow_tph, 3),
            "latch_reason": self._latch_reason,
            "latched_at": self._latched_at,
            "latch_count": self._latch_count,
            "last_update_at": self._last_update_at,
            "history": list(self._machine.history),
        }
        record = self.persist_state(payload)
        self._refresh_gauges()
        return record

    def _refresh_gauges(self) -> None:
        self.metrics.observe("waste.drum_level", round(self._drum_level, 4))
        self.metrics.observe("waste.exhaust_temp_c", round(self._exhaust_temp_c, 3))
        self.metrics.observe("waste.latch_count", float(self._latch_count))


__all__ = ["WasteHeatBoiler", "STATES", "TRANSITIONS"]
