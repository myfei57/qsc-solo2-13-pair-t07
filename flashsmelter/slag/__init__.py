"""放渣组件。

放渣先于放铜：只有本炉次的炉渣已经从沉淀池排出并登记，才允许打开放铜口。放渣
本身还要满足沉淀池分层就绪、渣层厚度足够、余热锅炉未闩锁、没有正在进行的放铜。
"""

from __future__ import annotations

from typing import Any, Mapping

from ..component import Component, ensure_actor
from ..errors import GuardViolation, LatchEngagedError, NotFoundError, StateTransitionError
from ..machine import StateMachine
from ..ports import MattePort, SettlerPort, WastePort
from ..runtime import RuntimeContext

STATES = ("closed", "opening", "flowing", "closing", "sealed")

TRANSITIONS: Mapping[str, tuple[str, ...]] = {
    "closed": ("opening",),
    "opening": ("flowing", "closed"),
    "flowing": ("closing", "sealed"),
    "closing": ("sealed",),
    "sealed": ("closed", "opening"),
}


class SlagTap(Component):
    name = "slag"

    def __init__(
        self,
        ctx: RuntimeContext,
        *,
        settler: SettlerPort,
        waste: WastePort,
        matte: MattePort | None = None,
    ) -> None:
        super().__init__(ctx)
        self._machine = StateMachine("slag", "closed", TRANSITIONS, ctx.clock)
        self._settler = settler
        self._matte = matte
        self._waste = waste
        self._heat_id: str | None = None
        self._tapped_tons = 0.0
        self._last_tap_at: str | None = None
        self._last_tap_seconds = 0.0
        self._heats: dict[str, float] = {}
        restored = self.restore()
        if restored is not None:
            self._machine.restore(restored)
            self._heat_id = restored.get("heat_id")
            self._tapped_tons = float(restored.get("tapped_tons", 0.0))
            self._last_tap_at = restored.get("last_tap_at")
            self._last_tap_seconds = float(restored.get("last_tap_seconds", 0.0))
            heats = restored.get("heats")
            if isinstance(heats, dict):
                self._heats = {str(key): float(value) for key, value in heats.items()}
        self._refresh_gauges()

    def bind_matte(self, matte: MattePort) -> None:
        """放铜组件与放渣组件互相引用，用延迟绑定打破构造期循环。"""

        self._matte = matte

    # ------------------------------------------------------------------ 动作
    def tap(
        self,
        actor: str,
        *,
        heat_id: str,
        target_tons: float,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "tap",
            f"slag/{heat_id}",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            if not heat_id:
                raise GuardViolation("放渣必须绑定炉次号")
            if target_tons <= 0:
                raise GuardViolation("放渣吨位必须为正", details={"target_tons": target_tons})
            if self._waste.is_latched():
                raise LatchEngagedError(
                    "余热锅炉闩锁未复位，禁止放渣",
                    details={"waste": dict(self._waste.status())},
                )
            matte = self._require_matte()
            if matte.is_tapping():
                raise GuardViolation(
                    "放铜正在进行，禁止同时放渣",
                    details={"matte": dict(matte.status())},
                )
            requirements = self._settler.requirements()
            if self._settler.heat_id is not None and self._settler.heat_id != heat_id:
                raise GuardViolation(
                    "沉淀池绑定的炉次与请求不一致",
                    details={"settler_heat_id": self._settler.heat_id, "requested": heat_id},
                )
            if not requirements["slag_ready"]:
                raise GuardViolation("沉淀池尚未具备放渣条件", details=dict(requirements))
            if target_tons > float(requirements["available_slag_tons"]) + 1e-6:
                raise GuardViolation(
                    "放渣吨位超过沉淀池可放出的渣量",
                    details={
                        "target_tons": target_tons,
                        "available_tons": requirements["available_slag_tons"],
                    },
                )
            intent = self.write_intent(
                "tap",
                {
                    "action": "tap",
                    "heat_id": heat_id,
                    "target_tons": target_tons,
                    "at": self.clock.timestamp_iso(),
                    "actor": actor,
                },
            )
            self._settler.begin_tap(actor, kind="slag")
            self._machine.to("opening", actor, f"炉次 {heat_id} 开渣口")
            self._machine.to("flowing", actor, "渣流建立")
            self._settler.consume("slag", target_tons)
            self._settler.end_tap(actor, kind="slag", tons=target_tons)
            self._machine.to("closing", actor, "关渣口")
            self._machine.to("sealed", actor, "渣口封堵")
            self._heat_id = heat_id
            self._tapped_tons = round(self._heats.get(heat_id, 0.0) + target_tons, 3)
            self._heats[heat_id] = self._tapped_tons
            self._last_tap_at = self.clock.timestamp_iso()
            self._last_tap_seconds = round(target_tons / self.settings.slag_tap_rate_tph * 3600.0, 3)
            record = self._persist(reason="tap")
            trace.attach(record).note("tapped_tons", target_tons).note("intent_version", intent.version)
            trace.note("tap_seconds", self._last_tap_seconds)
            return self.status()

    def reset(
        self,
        actor: str,
        *,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "reset",
            "slag",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            if self._machine.state == "flowing":
                raise StateTransitionError("渣流未关闭，禁止复位", details={"state": self._machine.state})
            self._machine.to("closed", actor, "渣口复位")
            record = self._persist(reason="reset")
            trace.attach(record)
            return self.status()

    # ------------------------------------------------------------------ 查询
    @property
    def state(self) -> str:
        return self._machine.state

    def is_heat_slagged(self, heat_id: str) -> bool:
        return self._heats.get(heat_id, 0.0) > 0.0

    def heat_tapped_tons(self, heat_id: str) -> float:
        return round(self._heats.get(heat_id, 0.0), 3)

    def require_heat_slagged(self, heat_id: str) -> float:
        if not self.is_heat_slagged(heat_id):
            raise NotFoundError(
                "本炉次尚未放渣，放铜被拒绝",
                details={"heat_id": heat_id, "slag_tapped_tons": 0.0},
            )
        return self.heat_tapped_tons(heat_id)

    def status(self) -> Mapping[str, Any]:
        return {
            "state": self._machine.state,
            "heat_id": self._heat_id,
            "tapped_tons": round(self._tapped_tons, 3),
            "last_tap_at": self._last_tap_at,
            "last_tap_seconds": self._last_tap_seconds,
            "heats": dict(sorted(self._heats.items())),
            "history": list(self._machine.history),
        }

    # ------------------------------------------------------------------ 内部
    def _persist(self, *, reason: str) -> Any:
        payload = {
            "state": self._machine.state,
            "reason": reason,
            "written_epoch": self.clock.timestamp(),
            "written_at": self.clock.timestamp_iso(),
            "heat_id": self._heat_id,
            "tapped_tons": round(self._tapped_tons, 3),
            "last_tap_at": self._last_tap_at,
            "last_tap_seconds": self._last_tap_seconds,
            "heats": dict(sorted(self._heats.items())),
            "history": list(self._machine.history),
        }
        record = self.persist_state(payload)
        self._refresh_gauges()
        return record

    def _refresh_gauges(self) -> None:
        self.metrics.observe("slag.tapped_tons", round(self._tapped_tons, 3))
        self.metrics.observe("slag.heats_recorded", float(len(self._heats)))

    def _require_matte(self) -> MattePort:
        if self._matte is None:
            from ..errors import ConfigurationError

            raise ConfigurationError("放渣组件未绑定放铜端口")
        return self._matte


__all__ = ["SlagTap", "STATES", "TRANSITIONS"]
