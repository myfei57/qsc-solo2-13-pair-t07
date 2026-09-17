"""燃烧器组件。

闪速炉反应塔靠燃烧器维持点火与炉温：点火前必须确认燃料压力与助燃风在量程内，
点火后要经过稳定保持才允许向炉内喷吹精矿。燃烧器状态必须先落盘并可回读，精矿
喷吹系统才承认它「稳定」——内存里的状态不作数。
"""

from __future__ import annotations

from typing import Any, Mapping

from ..component import Component, ensure_actor
from ..errors import GuardViolation, StateTransitionError
from ..machine import StateMachine
from ..runtime import RuntimeContext
from ..store import Record

STATES = ("off", "preheating", "ignited", "stable", "cooling", "fault_latched")

TRANSITIONS: Mapping[str, tuple[str, ...]] = {
    "off": ("preheating", "fault_latched"),
    "preheating": ("ignited", "off", "fault_latched"),
    "ignited": ("stable", "cooling", "fault_latched"),
    "stable": ("cooling", "fault_latched"),
    "cooling": ("off", "fault_latched"),
    "fault_latched": ("off",),
}


class Burner(Component):
    name = "burner"

    def __init__(self, ctx: RuntimeContext) -> None:
        super().__init__(ctx)
        self._machine = StateMachine("burner", "off", TRANSITIONS, ctx.clock)
        self._fuel_pressure_kpa = 0.0
        self._air_flow_nm3h = 0.0
        self._ignition_count = 0
        self._latch_reason: str | None = None
        self._stable_since: str | None = None
        self._hold_seconds = 0.0
        restored = self.restore()
        if restored is not None:
            self._machine.restore(restored)
            self._fuel_pressure_kpa = float(restored.get("fuel_pressure_kpa", 0.0))
            self._air_flow_nm3h = float(restored.get("air_flow_nm3h", 0.0))
            self._ignition_count = int(restored.get("ignition_count", 0))
            self._latch_reason = restored.get("latch_reason")
            self._stable_since = restored.get("stable_since")
            self._hold_seconds = float(restored.get("hold_seconds", 0.0))
        self._refresh_gauges()

    # ------------------------------------------------------------------ 动作
    def ignite(
        self,
        actor: str,
        *,
        fuel_pressure_kpa: float,
        air_flow_nm3h: float,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "ignite",
            "burner",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            self._require_not_latched("点火")
            if not self.settings.burner_fuel_pressure_min_kpa <= fuel_pressure_kpa <= self.settings.burner_fuel_pressure_max_kpa:
                raise GuardViolation(
                    "燃料压力不在点火量程内",
                    details={
                        "fuel_pressure_kpa": fuel_pressure_kpa,
                        "min": self.settings.burner_fuel_pressure_min_kpa,
                        "max": self.settings.burner_fuel_pressure_max_kpa,
                    },
                )
            if air_flow_nm3h < self.settings.burner_air_flow_min_nm3h:
                raise GuardViolation(
                    "助燃风不足，禁止点火",
                    details={
                        "air_flow_nm3h": air_flow_nm3h,
                        "min": self.settings.burner_air_flow_min_nm3h,
                    },
                )
            self._machine.to("preheating", actor, "点燃辅助烧嘴")
            record = self._persist(
                fuel_pressure_kpa=fuel_pressure_kpa,
                air_flow_nm3h=air_flow_nm3h,
                reason="ignite",
            )
            trace.attach(record).note("fuel_pressure_kpa", fuel_pressure_kpa)
            return self.status()

    def stabilize(
        self,
        actor: str,
        *,
        hold_seconds: float | None = None,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        required = self.settings.burner_min_stable_hold_seconds if hold_seconds is None else hold_seconds
        with self.action(
            "stabilize",
            "burner",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            self._machine.require("ignited", "稳定保持")
            if required < self.settings.burner_min_stable_hold_seconds:
                raise GuardViolation(
                    "稳定保持时长低于工艺下限",
                    details={"requested": required, "min": self.settings.burner_min_stable_hold_seconds},
                )
            self._hold_seconds += required
            self._stable_since = self.clock.timestamp_iso()
            self._machine.to("stable", actor, "燃烧稳定")
            record = self._persist(reason="stabilize")
            trace.attach(record).note("hold_seconds", required)
            return self.status()

    def confirm_flame(
        self,
        actor: str,
        *,
        flame_detected: bool = True,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        """火焰检测确认：预热结束、火焰建立之后才允许进入稳定保持。"""

        actor = ensure_actor(actor)
        with self.action(
            "confirm_flame",
            "burner",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            self._machine.require("preheating", "火焰确认")
            if not flame_detected:
                raise GuardViolation("火焰检测未确认，禁止进入稳定保持")
            self._machine.to("ignited", actor, "火焰建立")
            record = self._persist(reason="confirm_flame")
            trace.attach(record)
            return self.status()

    def cool_down(
        self,
        actor: str,
        *,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "cool_down",
            "burner",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            if self._machine.state not in ("ignited", "stable"):
                raise StateTransitionError(
                    "只有点火或稳定状态可以停火冷却",
                    details={"state": self._machine.state},
                )
            self._machine.to("cooling", actor, "停火冷却")
            self._fuel_pressure_kpa = 0.0
            self._air_flow_nm3h = 0.0
            self._stable_since = None
            record = self._persist(reason="cool_down")
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
            "burner",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            self._machine.require("cooling", "冷却结束")
            self._machine.to("off", actor, "冷却完成")
            record = self._persist(reason="finish_cooling")
            trace.attach(record)
            return self.status()

    def trip(
        self,
        actor: str,
        *,
        reason: str,
        correlation_id: str | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action("trip", "burner", actor, correlation_id=correlation_id) as trace:
            if not reason:
                raise GuardViolation("跳闸必须给出原因")
            self._machine.to("fault_latched", actor, reason)
            self._latch_reason = reason
            record = self._persist(reason="trip")
            trace.attach(record).note("latch_reason", reason)
            return self.status()

    def attest(
        self,
        actor: str,
        *,
        fuel_pressure_kpa: float | None = None,
        air_flow_nm3h: float | None = None,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        """周期性刷新落盘凭证。

        控制系统每一轮扫描都会重新确认燃烧器仍在稳定燃烧，并把状态重新落盘。
        没有这一步，稳定凭证会随时间过期，精矿喷吹会被正确地挡住。
        """

        actor = ensure_actor(actor)
        with self.action(
            "attest",
            "burner",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            if self._machine.state != "stable":
                raise StateTransitionError(
                    "只有稳定燃烧状态可以刷新落盘凭证",
                    details={"state": self._machine.state},
                )
            if fuel_pressure_kpa is not None:
                self._fuel_pressure_kpa = fuel_pressure_kpa
            if air_flow_nm3h is not None:
                self._air_flow_nm3h = air_flow_nm3h
            record = self._persist(reason="attest")
            trace.attach(record).note("refresh", True)
            return self.status()

    def reset(
        self,
        actor: str,
        *,
        note: str,
        fuel_pressure_kpa: float = 0.0,
        air_flow_nm3h: float = 0.0,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "reset",
            "burner",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            self._machine.require("fault_latched", "复位")
            if not note:
                raise GuardViolation("复位必须填写处理说明")
            if fuel_pressure_kpa != 0.0 or air_flow_nm3h != 0.0:
                raise GuardViolation(
                    "复位前燃料与助燃风必须回零",
                    details={"fuel_pressure_kpa": fuel_pressure_kpa, "air_flow_nm3h": air_flow_nm3h},
                )
            self._machine.to("off", actor, f"复位：{note}")
            self._latch_reason = None
            self._fuel_pressure_kpa = 0.0
            self._air_flow_nm3h = 0.0
            record = self._persist(reason="reset")
            trace.attach(record).note("note", note)
            return self.status()

    # ------------------------------------------------------------------ 查询
    def is_latched(self) -> bool:
        return self._machine.state == "fault_latched"

    @property
    def state(self) -> str:
        return self._machine.state

    def durable_stable_record(self) -> tuple[Mapping[str, Any] | None, float]:
        """返回最近一次落盘的稳定凭证及其年龄（秒）。

        精矿喷吹据此判断「燃烧器稳定」是否可信：记录缺失、状态不是 stable、
        或年龄超过时效窗口，都视为不可用。
        """

        record: Record | None = self.store.get(self.key(self.state_key))
        if record is None:
            return None, float("inf")
        payload = dict(record.payload)
        tried_at = payload.get("written_epoch")
        if tried_at is None:
            age = float("inf")
        else:
            age = max(0.0, self.clock.timestamp() - float(tried_at))
        return payload, age

    def stable_attestation(self) -> tuple[bool, Mapping[str, Any]]:
        payload, age = self.durable_stable_record()
        if payload is None:
            return False, {"reason": "no-record"}
        if payload.get("state") != "stable":
            return False, {"reason": "not-stable", "state": payload.get("state")}
        if age > self.settings.burner_record_max_age_seconds:
            return False, {
                "reason": "record-stale",
                "age_seconds": round(age, 3),
                "window_seconds": self.settings.burner_record_max_age_seconds,
            }
        return True, {
            "state": payload.get("state"),
            "since": payload.get("stable_since"),
            "age_seconds": round(age, 3),
            "fuel_pressure_kpa": payload.get("fuel_pressure_kpa"),
            "air_flow_nm3h": payload.get("air_flow_nm3h"),
        }

    def status(self) -> Mapping[str, Any]:
        payload, age = self.durable_stable_record()
        return {
            "state": self._machine.state,
            "fuel_pressure_kpa": round(self._fuel_pressure_kpa, 3),
            "air_flow_nm3h": round(self._air_flow_nm3h, 3),
            "ignition_count": self._ignition_count,
            "hold_seconds": round(self._hold_seconds, 3),
            "stable_since": self._stable_since,
            "latch_reason": self._latch_reason,
            "durable_state": None if payload is None else payload.get("state"),
            "durable_age_seconds": None if age == float("inf") else round(age, 3),
            "history": list(self._machine.history),
        }

    # ------------------------------------------------------------------ 内部
    def _persist(self, *, reason: str, fuel_pressure_kpa: float | None = None, air_flow_nm3h: float | None = None) -> Record:
        if fuel_pressure_kpa is not None:
            self._fuel_pressure_kpa = fuel_pressure_kpa
        if air_flow_nm3h is not None:
            self._air_flow_nm3h = air_flow_nm3h
        if reason == "ignite":
            self._ignition_count += 1
        if reason == "stabilize":
            self._stable_since = self.clock.timestamp_iso()
        payload = {
            "state": self._machine.state,
            "reason": reason,
            "written_epoch": self.clock.timestamp(),
            "written_at": self.clock.timestamp_iso(),
            "fuel_pressure_kpa": round(self._fuel_pressure_kpa, 3),
            "air_flow_nm3h": round(self._air_flow_nm3h, 3),
            "ignition_count": self._ignition_count,
            "hold_seconds": round(self._hold_seconds, 3),
            "stable_since": self._stable_since,
            "latch_reason": self._latch_reason,
            "history": list(self._machine.history),
        }
        record = self.persist_state(payload)
        self._refresh_gauges()
        return record

    def _require_not_latched(self, action: str) -> None:
        if self.is_latched():
            from ..errors import LatchEngagedError

            raise LatchEngagedError(
                "燃烧器处于故障闩锁，禁止动作",
                details={"action": action, "latch_reason": self._latch_reason},
            )

    def _refresh_gauges(self) -> None:
        self.metrics.observe("burner.state_code", float(STATES.index(self._machine.state)))
        self.metrics.observe("burner.fuel_pressure_kpa", round(self._fuel_pressure_kpa, 3))
        self.metrics.observe("burner.air_flow_nm3h", round(self._air_flow_nm3h, 3))


__all__ = ["Burner", "STATES", "TRANSITIONS"]
