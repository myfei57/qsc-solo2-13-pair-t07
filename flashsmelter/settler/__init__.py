"""沉淀池组件。

反应塔出来的熔体在沉淀池里分层：上层炉渣、下层冰铜。放渣放铜都必须等到液位与
渣层达到工艺下限，并且静止分层时间足够，才允许进入 ``tap_ready``。
"""

from __future__ import annotations

from typing import Any, Mapping

from ..component import Component, ensure_actor
from ..errors import GuardViolation, StateTransitionError
from ..machine import StateMachine
from ..runtime import RuntimeContext

STATES = ("empty", "filling", "layering", "tap_ready", "tapping")

TRANSITIONS: Mapping[str, tuple[str, ...]] = {
    "empty": ("filling",),
    "filling": ("layering", "empty", "filling"),
    "layering": ("tap_ready", "filling", "empty", "layering"),
    "tap_ready": ("tapping", "filling", "empty", "layering"),
    "tapping": ("tap_ready", "filling", "empty"),
}

# 沉淀池有效沉降面积与熔体密度，用于把液位换算成可放出的吨位。
SETTLER_AREA_M2 = 45.0
SLAG_DENSITY_TPM3 = 2.6
MATTE_DENSITY_TPM3 = 4.8


class Settler(Component):
    name = "settler"

    def __init__(self, ctx: RuntimeContext) -> None:
        super().__init__(ctx)
        self._machine = StateMachine("settler", "empty", TRANSITIONS, ctx.clock)
        self._heat_id: str | None = None
        self._bath_level = 0.0
        self._slag_thickness = 0.0
        self._matte_level = 0.0
        self._layering_since: float | None = None
        self._taps: list[dict[str, Any]] = []
        self._active_tap: str | None = None
        restored = self.restore()
        if restored is not None:
            self._machine.restore(restored)
            self._heat_id = restored.get("heat_id")
            self._bath_level = float(restored.get("bath_level_m", 0.0))
            self._slag_thickness = float(restored.get("slag_thickness_m", 0.0))
            self._matte_level = float(restored.get("matte_level_m", 0.0))
            self._layering_since = restored.get("layering_since")
            self._active_tap = restored.get("active_tap")
            taps = restored.get("taps")
            if isinstance(taps, list):
                self._taps = [entry for entry in taps if isinstance(entry, dict)]
        self._refresh_gauges()

    # ------------------------------------------------------------------ 动作
    def update(
        self,
        actor: str,
        *,
        bath_level_m: float,
        slag_thickness_m: float,
        matte_level_m: float,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "update",
            "settler",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            for name, value in (
                ("bath_level_m", bath_level_m),
                ("slag_thickness_m", slag_thickness_m),
                ("matte_level_m", matte_level_m),
            ):
                if value < 0:
                    raise GuardViolation(f"{name} 不能为负", details={name: value})
            if slag_thickness_m + matte_level_m > bath_level_m + 1e-6:
                raise GuardViolation(
                    "分层数据不合法：渣层与冰铜层之和超过总液位",
                    details={
                        "bath_level_m": bath_level_m,
                        "slag_thickness_m": slag_thickness_m,
                        "matte_level_m": matte_level_m,
                    },
                )
            self._bath_level = bath_level_m
            self._slag_thickness = slag_thickness_m
            self._matte_level = matte_level_m
            self._reclassify(actor)
            record = self._persist(reason="update")
            trace.attach(record).note("state", self._machine.state)
            return self.status()

    def settle(
        self,
        actor: str,
        *,
        heat_id: str,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "settle",
            "settler",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            if not heat_id:
                raise GuardViolation("分层判定必须绑定炉次号")
            requirements = self.requirements()
            if not requirements["layer_ready_any"]:
                raise GuardViolation("沉淀池尚未满足放料条件", details=requirements)
            self._heat_id = heat_id
            self._machine.advance_to("tap_ready", actor, f"炉次 {heat_id} 分层完成")
            record = self._persist(reason="settle")
            trace.attach(record).note("heat_id", heat_id)
            return self.status()

    def begin_tap(
        self,
        actor: str,
        *,
        kind: str,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "begin_tap",
            f"settler/{kind}",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            self._validate_kind(kind)
            if self._machine.state != "tap_ready":
                raise StateTransitionError(
                    "沉淀池当前不可放料",
                    details={"state": self._machine.state, "requirements": self.requirements()},
                )
            if self._active_tap is not None:
                raise GuardViolation(
                    "已有放料动作在进行",
                    details={"active_tap": self._active_tap, "requested": kind},
                )
            self._active_tap = kind
            self._machine.to("tapping", actor, f"开始放{self._label(kind)}")
            record = self._persist(reason="begin_tap")
            trace.attach(record).note("kind", kind)
            return self.status()

    def end_tap(
        self,
        actor: str,
        *,
        kind: str,
        tons: float,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "end_tap",
            f"settler/{kind}",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            self._validate_kind(kind)
            if self._active_tap != kind:
                raise GuardViolation(
                    "放料动作与结束动作不匹配",
                    details={"active_tap": self._active_tap, "requested": kind},
                )
            self.consume(kind, tons)
            self._taps.append(
                {
                    "kind": kind,
                    "tons": round(tons, 3),
                    "heat_id": self._heat_id,
                    "at": self.clock.timestamp_iso(),
                    "actor": actor,
                }
            )
            self._taps = self._taps[-24:]
            self._active_tap = None
            self._machine.to("tap_ready", actor, f"结束放{self._label(kind)}")
            self._reclassify(actor)
            record = self._persist(reason="end_tap")
            trace.attach(record).note("tons", tons)
            return self.status()

    def consume(self, kind: str, tons: float) -> None:
        """按吨位扣减对应液位，放渣放铜共用。"""

        self._validate_kind(kind)
        if tons <= 0:
            raise GuardViolation("放料吨位必须为正", details={"tons": tons})
        if kind == "slag":
            available = self.available_slag_tons()
        else:
            available = self.available_matte_tons()
        if tons > available + 1e-6:
            raise GuardViolation(
                f"请求放出的{self._label(kind)}超过可用量",
                details={"requested_tons": tons, "available_tons": round(available, 3), "kind": kind},
            )
        if kind == "slag":
            self._slag_thickness = max(0.0, self._slag_thickness - tons / (SETTLER_AREA_M2 * SLAG_DENSITY_TPM3))
        else:
            self._matte_level = max(0.0, self._matte_level - tons / (SETTLER_AREA_M2 * MATTE_DENSITY_TPM3))
        self._refresh_gauges()

    # ------------------------------------------------------------------ 查询
    @property
    def state(self) -> str:
        return self._machine.state

    @property
    def heat_id(self) -> str | None:
        return self._heat_id

    def available_slag_tons(self) -> float:
        return round(self._slag_thickness * SETTLER_AREA_M2 * SLAG_DENSITY_TPM3, 3)

    def available_matte_tons(self) -> float:
        return round(self._matte_level * SETTLER_AREA_M2 * MATTE_DENSITY_TPM3, 3)

    def tap_ready(self) -> bool:
        """工艺就绪判定：只看液位、分层厚度与静置时长，不看状态机当前记到哪一步。"""

        requirements = self._conditions()
        return requirements["ready"]

    def requirements(self) -> Mapping[str, Any]:
        conditions = self._conditions()
        payload = {"state": self._machine.state, "heat_id": self._heat_id}
        payload.update(conditions)
        return payload

    def _conditions(self) -> dict[str, Any]:
        dwell = self._dwell_seconds()
        bath_ok = self._bath_level >= self.settings.settler_min_bath_level_m
        slag_ok = self._slag_thickness >= self.settings.slag_min_thickness_m
        matte_ok = self._matte_level >= self.settings.matte_min_level_m
        dwell_ok = dwell >= self.settings.settler_layering_dwell_seconds
        slag_blockers: list[str] = []
        if not bath_ok:
            slag_blockers.append("bath-level-below-minimum")
        if not slag_ok:
            slag_blockers.append("slag-layer-too-thin")
        if not dwell_ok:
            slag_blockers.append("layering-dwell-insufficient")
        matte_blockers: list[str] = []
        if not bath_ok:
            matte_blockers.append("bath-level-below-minimum")
        if not matte_ok:
            matte_blockers.append("matte-level-below-minimum")
        if not dwell_ok:
            matte_blockers.append("layering-dwell-insufficient")
        layer_ready_any = bath_ok and dwell_ok and (slag_ok or matte_ok)
        blockers = [] if layer_ready_any else sorted(set(slag_blockers) | set(matte_blockers))
        return {
            "bath_level_m": round(self._bath_level, 4),
            "min_bath_level_m": self.settings.settler_min_bath_level_m,
            "bath_ok": bath_ok,
            "slag_thickness_m": round(self._slag_thickness, 4),
            "min_slag_thickness_m": self.settings.slag_min_thickness_m,
            "slag_ready": bath_ok and slag_ok and dwell_ok,
            "slag_blockers": slag_blockers,
            "available_slag_tons": self.available_slag_tons(),
            "matte_level_m": round(self._matte_level, 4),
            "min_matte_level_m": self.settings.matte_min_level_m,
            "matte_ready": bath_ok and matte_ok and dwell_ok,
            "matte_blockers": matte_blockers,
            "available_matte_tons": self.available_matte_tons(),
            "layering_dwell_seconds": round(dwell, 3),
            "required_dwell_seconds": self.settings.settler_layering_dwell_seconds,
            "layer_ready_any": layer_ready_any,
            "ready": layer_ready_any,
            "blockers": blockers,
        }

    def status(self) -> Mapping[str, Any]:
        payload = dict(self.requirements())
        payload.update(
            {
                "active_tap": self._active_tap,
                "taps": list(self._taps[-6:]),
                "history": list(self._machine.history),
            }
        )
        return payload

    # ------------------------------------------------------------------ 内部
    def _validate_kind(self, kind: str) -> None:
        if kind not in ("slag", "matte"):
            raise GuardViolation("放料类型只能是 slag 或 matte", details={"kind": kind})

    def _label(self, kind: str) -> str:
        return "渣" if kind == "slag" else "铜"

    def _dwell_seconds(self) -> float:
        if self._layering_since is None:
            return 0.0
        return max(0.0, self.clock.timestamp() - float(self._layering_since))

    def _reclassify(self, actor: str) -> None:
        below_min = self._bath_level < self.settings.settler_min_bath_level_m
        slag_ok = self._slag_thickness >= self.settings.slag_min_thickness_m
        matte_ok = self._matte_level >= self.settings.matte_min_level_m
        layers_ready = slag_ok or matte_ok
        if below_min:
            self._layering_since = None
            self._machine.advance_to("empty", actor, "液位低于下限")
            return
        if not layers_ready:
            self._layering_since = None
            self._machine.advance_to("filling", actor, "分层厚度不足")
            return
        if self._layering_since is None:
            self._layering_since = self.clock.timestamp()
        if self._machine.state == "tapping" and self._active_tap is not None:
            return
        target = "tap_ready" if self._dwell_seconds() >= self.settings.settler_layering_dwell_seconds else "layering"
        self._machine.advance_to(target, actor, "分层状态刷新")

    def _persist(self, *, reason: str) -> Any:
        payload = {
            "state": self._machine.state,
            "reason": reason,
            "written_epoch": self.clock.timestamp(),
            "written_at": self.clock.timestamp_iso(),
            "heat_id": self._heat_id,
            "bath_level_m": round(self._bath_level, 4),
            "slag_thickness_m": round(self._slag_thickness, 4),
            "matte_level_m": round(self._matte_level, 4),
            "layering_since": self._layering_since,
            "active_tap": self._active_tap,
            "taps": list(self._taps[-12:]),
            "history": list(self._machine.history),
        }
        record = self.persist_state(payload)
        self._refresh_gauges()
        return record

    def _refresh_gauges(self) -> None:
        self.metrics.observe("settler.bath_level_m", round(self._bath_level, 4))
        self.metrics.observe("settler.slag_thickness_m", round(self._slag_thickness, 4))
        self.metrics.observe("settler.matte_level_m", round(self._matte_level, 4))
        self.metrics.observe("settler.available_slag_tons", self.available_slag_tons())
        self.metrics.observe("settler.available_matte_tons", self.available_matte_tons())


__all__ = [
    "Settler",
    "STATES",
    "TRANSITIONS",
    "SETTLER_AREA_M2",
    "SLAG_DENSITY_TPM3",
    "MATTE_DENSITY_TPM3",
]
