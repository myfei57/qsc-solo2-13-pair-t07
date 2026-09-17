"""放铜（冰铜）组件。

冰铜从沉淀池放到包子后送转炉吹炼。约束有两条：本炉次必须先放渣，以及转炉必须
有空余批次容量。每次放铜都会生成一条可追溯的冰铜批记录，供转炉入炉时校验。
"""

from __future__ import annotations

from typing import Any, Mapping

from ..component import Component, ensure_actor
from ..errors import GuardViolation, LatchEngagedError, StateTransitionError
from ..machine import StateMachine
from ..ports import ConverterPort, SettlerPort, SlagPort, WastePort
from ..runtime import RuntimeContext

STATES = ("closed", "opening", "flowing", "closing", "sealed")

TRANSITIONS: Mapping[str, tuple[str, ...]] = {
    "closed": ("opening",),
    "opening": ("flowing", "closed"),
    "flowing": ("closing", "sealed"),
    "closing": ("sealed",),
    "sealed": ("closed", "opening"),
}

CHARGE_STREAM = "matte/charges"


class MatteTap(Component):
    name = "matte"

    def __init__(
        self,
        ctx: RuntimeContext,
        *,
        settler: SettlerPort,
        waste: WastePort,
        slag: SlagPort,
        converter: ConverterPort | None = None,
    ) -> None:
        super().__init__(ctx)
        self._machine = StateMachine("matte", "closed", TRANSITIONS, ctx.clock)
        self._settler = settler
        self._waste = waste
        self._converter = converter
        self._slag = slag
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

    def bind_converter(self, converter: ConverterPort) -> None:
        self._converter = converter

    # ------------------------------------------------------------------ 动作
    def tap(
        self,
        actor: str,
        *,
        heat_id: str,
        ladle_id: str,
        target_tons: float,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "tap",
            f"matte/{ladle_id}",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            if not heat_id or not ladle_id:
                raise GuardViolation("放铜必须给出炉次号与包子号")
            if target_tons <= 0:
                raise GuardViolation("放铜吨位必须为正", details={"target_tons": target_tons})
            self._slag.require_heat_slagged(heat_id)
            if self._waste.is_latched():
                raise LatchEngagedError(
                    "余热锅炉闩锁未复位，禁止放铜",
                    details={"waste": dict(self._waste.status())},
                )
            requirements = self._settler.requirements()
            if not requirements["matte_ready"]:
                raise GuardViolation("沉淀池尚未具备放铜条件", details=dict(requirements))
            if target_tons > float(requirements["available_matte_tons"]) + 1e-6:
                raise GuardViolation(
                    "放铜吨位超过沉淀池可放出的冰铜量",
                    details={
                        "target_tons": target_tons,
                        "available_tons": requirements["available_matte_tons"],
                    },
                )
            converter = self._require_converter()
            if not converter.can_accept(target_tons):
                raise GuardViolation(
                    "转炉当前无法接收本批冰铜",
                    details={"converter": dict(converter.status()), "target_tons": target_tons},
                )
            intent = self.write_intent(
                "tap",
                {
                    "action": "tap",
                    "heat_id": heat_id,
                    "ladle_id": ladle_id,
                    "target_tons": target_tons,
                    "at": self.clock.timestamp_iso(),
                    "actor": actor,
                },
            )
            self._settler.begin_tap(actor, kind="matte")
            self._machine.to("opening", actor, f"炉次 {heat_id} 开放铜口")
            self._machine.to("flowing", actor, "冰铜流入包子")
            self._settler.consume("matte", target_tons)
            self._settler.end_tap(actor, kind="matte", tons=target_tons)
            self._machine.to("closing", actor, "关放铜口")
            self._machine.to("sealed", actor, "放铜口封堵")
            self._heat_id = heat_id
            self._tapped_tons = round(self._heats.get(heat_id, 0.0) + target_tons, 3)
            self._heats[heat_id] = self._tapped_tons
            self._last_tap_at = self.clock.timestamp_iso()
            self._last_tap_seconds = round(
                target_tons / self.settings.matte_tap_rate_tph * 3600.0, 3
            )
            charge = self.store.append(
                CHARGE_STREAM,
                {
                    "heat_id": heat_id,
                    "ladle_id": ladle_id,
                    "tons": round(target_tons, 3),
                    "status": "available",
                    "tapped_at": self._last_tap_at,
                    "actor": actor,
                    "intent_version": intent.version,
                },
            )
            record = self._persist(reason="tap")
            trace.attach(record).note("charge_seq", charge.seq)
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
            "matte",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            if self._machine.state == "flowing":
                raise StateTransitionError("冰铜流未关闭，禁止复位", details={"state": self._machine.state})
            self._machine.to("closed", actor, "放铜口复位")
            record = self._persist(reason="reset")
            trace.attach(record)
            return self.status()

    def mark_charged(self, ladle_id: str, batch_id: str, tons: float, actor: str) -> None:
        """转炉入炉后登记，避免同一包冰铜被重复吹炼。"""

        self.store.append(
            CHARGE_STREAM,
            {
                "ladle_id": ladle_id,
                "batch_id": batch_id,
                "tons": round(tons, 3),
                "status": "charged",
                "charged_at": self.clock.timestamp_iso(),
                "actor": actor,
            },
        )

    # ------------------------------------------------------------------ 查询
    @property
    def state(self) -> str:
        return self._machine.state

    def is_tapping(self) -> bool:
        return self._machine.state in ("opening", "flowing", "closing")

    def heat_tapped_tons(self, heat_id: str) -> float:
        return round(self._heats.get(heat_id, 0.0), 3)

    def charges(self, *, limit: int = 50) -> list[Mapping[str, Any]]:
        entries = self.store.read_stream(CHARGE_STREAM, limit=limit)
        by_ladle: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for entry in entries:
            ladle = str(entry.payload.get("ladle_id", ""))
            if not ladle:
                continue
            if ladle not in by_ladle:
                by_ladle[ladle] = {"ladle_id": ladle, "status": "available"}
                order.append(ladle)
            if entry.payload.get("status") == "charged":
                by_ladle[ladle].update(
                    {
                        "status": "charged",
                        "batch_id": entry.payload.get("batch_id"),
                        "charged_at": entry.payload.get("charged_at"),
                    }
                )
            else:
                by_ladle[ladle].update(
                    {
                        "heat_id": entry.payload.get("heat_id"),
                        "tons": entry.payload.get("tons"),
                        "tapped_at": entry.payload.get("tapped_at"),
                    }
                )
        return [by_ladle[ladle] for ladle in order]

    def available_charge(self, ladle_id: str) -> Mapping[str, Any] | None:
        for charge in self.charges(limit=200):
            if charge["ladle_id"] == ladle_id and charge["status"] == "available":
                return charge
        return None

    def status(self) -> Mapping[str, Any]:
        return {
            "state": self._machine.state,
            "heat_id": self._heat_id,
            "tapped_tons": round(self._tapped_tons, 3),
            "last_tap_at": self._last_tap_at,
            "last_tap_seconds": self._last_tap_seconds,
            "heats": dict(sorted(self._heats.items())),
            "charges": self.charges(limit=8),
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
        self.metrics.observe("matte.tapped_tons", round(self._tapped_tons, 3))
        self.metrics.observe("matte.heats_recorded", float(len(self._heats)))

    def _require_converter(self) -> ConverterPort:
        if self._converter is None:
            from ..errors import ConfigurationError

            raise ConfigurationError("放铜组件未绑定转炉端口")
        return self._converter


__all__ = ["MatteTap", "STATES", "TRANSITIONS", "CHARGE_STREAM"]
