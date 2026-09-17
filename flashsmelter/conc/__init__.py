"""精矿喷吹组件。

喷吹是整条链上最危险的动作：富氧没建立、燃烧器状态没落盘、余热锅炉闩锁未复位，
任何一条不满足都必须拒绝。每炉次的精矿总量受热料预算约束，喷吹意图先落盘再
开阀，避免「已经喷了但记录里没有」。
"""

from __future__ import annotations

from typing import Any, Mapping

from ..component import Component, ensure_actor
from ..errors import GuardViolation, LatchEngagedError, StateTransitionError
from ..machine import StateMachine
from ..ports import BurnerPort, OxygenPort, SettlerPort, WastePort
from ..runtime import RuntimeContext

STATES = ("blocked", "armed", "injecting", "paused", "stopped")

TRANSITIONS: Mapping[str, tuple[str, ...]] = {
    "blocked": ("armed", "stopped"),
    "armed": ("injecting", "paused", "stopped", "blocked"),
    "injecting": ("paused", "stopped", "injecting"),
    "paused": ("injecting", "stopped", "armed"),
    "stopped": ("blocked", "armed"),
}


class ConcentrateSystem(Component):
    name = "conc"

    def __init__(
        self,
        ctx: RuntimeContext,
        *,
        burner: BurnerPort,
        oxygen: OxygenPort,
        waste: WastePort,
        settler: SettlerPort,
    ) -> None:
        super().__init__(ctx)
        self._machine = StateMachine("conc", "blocked", TRANSITIONS, ctx.clock)
        self._burner = burner
        self._oxygen = oxygen
        self._waste = waste
        self._settler = settler
        self._heat_id: str | None = None
        self._fed_tons = 0.0
        self._last_rate_tph = 0.0
        self._feed_valve = "closed"
        self._injection_count = 0
        self._armed_at: str | None = None
        restored = self.restore()
        if restored is not None:
            self._machine.restore(restored)
            self._heat_id = restored.get("heat_id")
            self._fed_tons = float(restored.get("fed_tons", 0.0))
            self._last_rate_tph = float(restored.get("last_rate_tph", 0.0))
            self._feed_valve = str(restored.get("feed_valve", "closed"))
            self._injection_count = int(restored.get("injection_count", 0))
            self._armed_at = restored.get("armed_at")
        self._refresh_gauges()

    # ------------------------------------------------------------------ 动作
    def arm(
        self,
        actor: str,
        *,
        heat_id: str,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "arm",
            f"conc/{heat_id}",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            if not heat_id:
                raise GuardViolation("喷吹必须绑定炉次号")
            self._assert_gates()
            if self._machine.state == "injecting":
                raise StateTransitionError("喷吹进行中不能重新 armed", details={"state": self._machine.state})
            opening_new_heat = self._heat_id != heat_id
            intent = self.write_intent(
                "arm",
                {
                    "action": "arm",
                    "heat_id": heat_id,
                    "at": self.clock.timestamp_iso(),
                    "actor": actor,
                    "budget_tons": self.settings.heat_feed_budget_tons,
                    "previous_heat_id": self._heat_id,
                    "previous_fed_tons": round(self._fed_tons, 3),
                },
            )
            if opening_new_heat:
                self._fed_tons = 0.0
            self._heat_id = heat_id
            self._armed_at = self.clock.timestamp_iso()
            self._feed_valve = "closed"
            if self._machine.state != "armed":
                self._machine.to("armed", actor, f"炉次 {heat_id} 喷吹就绪")
            record = self._persist(reason="arm")
            trace.attach(record).note("intent_version", intent.version)
            return self.status()

    def inject(
        self,
        actor: str,
        *,
        rate_tph: float,
        tons: float,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "inject",
            f"conc/{self._heat_id or 'unassigned'}",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            if self._machine.state not in ("armed", "injecting", "paused"):
                raise StateTransitionError(
                    "喷吹前必须先 armed", details={"state": self._machine.state}
                )
            if rate_tph <= 0 or tons <= 0:
                raise GuardViolation("喷吹速率与吨位必须为正", details={"rate": rate_tph, "tons": tons})
            if rate_tph > self.settings.feed_rate_max_tph:
                raise GuardViolation(
                    "喷吹速率超过上限",
                    details={"rate_tph": rate_tph, "max": self.settings.feed_rate_max_tph},
                )
            projected = self._fed_tons + tons
            if projected > self.settings.heat_feed_budget_tons:
                raise GuardViolation(
                    "超出本炉次热料预算",
                    details={
                        "heat_id": self._heat_id,
                        "fed_tons": round(self._fed_tons, 3),
                        "requested_tons": tons,
                        "budget_tons": self.settings.heat_feed_budget_tons,
                    },
                )
            self._assert_gates()
            intent = self.write_intent(
                "inject",
                {
                    "action": "inject",
                    "heat_id": self._heat_id,
                    "rate_tph": rate_tph,
                    "tons": tons,
                    "projected_fed_tons": round(projected, 3),
                    "at": self.clock.timestamp_iso(),
                    "actor": actor,
                },
            )
            self._fed_tons = projected
            self._last_rate_tph = rate_tph
            self._injection_count += 1
            self.metrics.inc("conc.injections")
            self._feed_valve = "open"
            if self._machine.state != "injecting":
                self._machine.to("injecting", actor, "开始喷吹")
            record = self._persist(reason="inject")
            trace.attach(record).note("fed_tons", round(self._fed_tons, 3))
            trace.note("intent_version", intent.version)
            return self.status()

    def pause(
        self,
        actor: str,
        *,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "pause",
            "conc",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            if self._machine.state not in ("armed", "injecting"):
                raise StateTransitionError("当前状态无需暂停", details={"state": self._machine.state})
            self._feed_valve = "closed"
            self._machine.to("paused", actor, "暂停喷吹")
            record = self._persist(reason="pause")
            trace.attach(record)
            return self.status()

    def stop(
        self,
        actor: str,
        *,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "stop",
            "conc",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            if self._machine.state not in ("armed", "injecting", "paused"):
                raise StateTransitionError("当前状态没有可停的喷吹", details={"state": self._machine.state})
            intent = self.write_intent(
                "stop",
                {
                    "action": "stop",
                    "heat_id": self._heat_id,
                    "fed_tons": round(self._fed_tons, 3),
                    "at": self.clock.timestamp_iso(),
                    "actor": actor,
                },
            )
            self._feed_valve = "closed"
            self._machine.to("stopped", actor, "喷吹停止")
            record = self._persist(reason="stop")
            trace.attach(record).note("intent_version", intent.version)
            return self.status()

    def release_heat(
        self,
        actor: str,
        *,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "release_heat",
            "conc",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            if self._machine.state == "injecting":
                raise StateTransitionError("喷吹进行中不能释放炉次", details={"state": self._machine.state})
            completed_heat = self._heat_id
            self._heat_id = None
            self._fed_tons = 0.0
            self._feed_valve = "closed"
            self._machine.to("blocked", actor, f"释放炉次 {completed_heat}")
            record = self._persist(reason="release_heat")
            trace.attach(record).note("released_heat", completed_heat)
            return self.status()

    # ------------------------------------------------------------------ 查询
    @property
    def state(self) -> str:
        return self._machine.state

    @property
    def heat_id(self) -> str | None:
        return self._heat_id

    def is_flowing(self) -> bool:
        return self._machine.state == "injecting" and self._feed_valve == "open"

    def gates(self) -> Mapping[str, Any]:
        burner_ok, burner_detail = self._burner.stable_attestation()
        try:
            oxygen_detail = self._oxygen.ensure_established_for_feed()
            oxygen_ok = True
            oxygen_error = None
        except GuardViolation as exc:
            oxygen_ok = False
            oxygen_error = exc.code
            oxygen_detail = dict(exc.details)
        waste_latched = self._waste.is_latched()
        settler = self._settler.requirements()
        settler_ok = settler["bath_level_m"] >= settler["min_bath_level_m"]
        blockers: list[str] = []
        if not burner_ok:
            blockers.append(f"burner:{burner_detail.get('reason')}")
        if not oxygen_ok:
            blockers.append(f"oxygen:{oxygen_error}")
        if waste_latched:
            blockers.append("waste:latched")
        if not settler_ok:
            blockers.append("settler:bath-level-below-minimum")
        budget_remaining = round(self.settings.heat_feed_budget_tons - self._fed_tons, 3)
        if budget_remaining <= 0:
            blockers.append("conc:feed-budget-exhausted")
        return {
            "burner": {"ok": burner_ok, "detail": dict(burner_detail)},
            "oxygen": {"ok": oxygen_ok, "detail": dict(oxygen_detail)},
            "waste": {"ok": not waste_latched, "detail": dict(self._waste.status())},
            "settler": {"ok": settler_ok, "detail": dict(settler)},
            "budget": {"ok": budget_remaining > 0, "remaining_tons": budget_remaining},
            "ready": not blockers,
            "blockers": blockers,
        }

    def status(self) -> Mapping[str, Any]:
        return {
            "state": self._machine.state,
            "heat_id": self._heat_id,
            "fed_tons": round(self._fed_tons, 3),
            "budget_tons": self.settings.heat_feed_budget_tons,
            "budget_remaining_tons": round(self.settings.heat_feed_budget_tons - self._fed_tons, 3),
            "last_rate_tph": round(self._last_rate_tph, 3),
            "feed_valve": self._feed_valve,
            "injection_count": self._injection_count,
            "armed_at": self._armed_at,
            "flowing": self.is_flowing(),
            "history": list(self._machine.history),
        }

    # ------------------------------------------------------------------ 内部
    def _assert_gates(self) -> None:
        burner_ok, burner_detail = self._burner.stable_attestation()
        if not burner_ok:
            if self._burner.is_latched():
                raise LatchEngagedError(
                    "燃烧器处于故障闩锁，禁止精矿喷吹", details={"burner": dict(burner_detail)}
                )
            raise GuardViolation(
                "燃烧器未处于已落盘的稳定状态，禁止精矿喷吹",
                details={"burner": dict(burner_detail)},
            )
        self._oxygen.ensure_established_for_feed()
        if self._waste.is_latched():
            raise LatchEngagedError(
                "余热锅炉联锁未复位，禁止精矿喷吹",
                details={"waste": dict(self._waste.status())},
            )
        settler = self._settler.requirements()
        if settler["bath_level_m"] < settler["min_bath_level_m"]:
            raise GuardViolation(
                "沉淀池液位不足以接收熔体，禁止喷吹",
                details={"settler": dict(settler)},
            )
        if self._fed_tons >= self.settings.heat_feed_budget_tons:
            raise GuardViolation(
                "本炉次热料预算已用尽",
                details={"fed_tons": round(self._fed_tons, 3), "budget": self.settings.heat_feed_budget_tons},
            )

    def _persist(self, *, reason: str) -> Any:
        payload = {
            "state": self._machine.state,
            "reason": reason,
            "written_epoch": self.clock.timestamp(),
            "written_at": self.clock.timestamp_iso(),
            "heat_id": self._heat_id,
            "fed_tons": round(self._fed_tons, 3),
            "last_rate_tph": round(self._last_rate_tph, 3),
            "feed_valve": self._feed_valve,
            "injection_count": self._injection_count,
            "armed_at": self._armed_at,
            "history": list(self._machine.history),
        }
        record = self.persist_state(payload)
        self._refresh_gauges()
        return record

    def _refresh_gauges(self) -> None:
        self.metrics.observe("conc.fed_tons", round(self._fed_tons, 3))
        self.metrics.observe("conc.last_rate_tph", round(self._last_rate_tph, 3))


__all__ = ["ConcentrateSystem", "STATES", "TRANSITIONS"]
