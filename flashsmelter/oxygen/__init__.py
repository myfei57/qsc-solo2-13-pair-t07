"""富氧系统组件。

富氧是闪速熔炼的先决条件：氧浓度没建立到位，精矿喷吹一律拒绝。系统同时维护
分析仪基线——基线过期（或读数比基线还旧）时，任何基于该基线的指令都要作废，
回滚也必须回到最近一次被确认过的富氧设定值。
"""

from __future__ import annotations

from typing import Any, Mapping

from ..component import Component, ensure_actor
from ..errors import GuardViolation, StaleBaselineError, StateTransitionError
from ..machine import StateMachine
from ..ports import FeedPort
from ..runtime import RuntimeContext, epoch_from_iso

STATES = ("idle", "ramping", "established", "degraded", "rolled_back")

TRANSITIONS: Mapping[str, tuple[str, ...]] = {
    "idle": ("ramping", "established"),
    "ramping": ("established", "degraded"),
    "established": ("ramping", "degraded", "rolled_back"),
    "degraded": ("ramping", "established", "rolled_back"),
    "rolled_back": ("ramping", "established", "idle"),
}


class OxygenSystem(Component):
    name = "oxygen"

    def __init__(self, ctx: RuntimeContext) -> None:
        super().__init__(ctx)
        self._machine = StateMachine("oxygen", "idle", TRANSITIONS, ctx.clock)
        self._enrichment = 0.0
        self._setpoint = 0.0
        self._last_good_setpoint: float | None = None
        self._flow_nm3h = 0.0
        self._baseline_value: float | None = None
        self._baseline_at: str | None = None
        self._baseline_epoch: float | None = None
        self._baseline_source: str | None = None
        self._rollback_count = 0
        self._readings: list[dict[str, Any]] = []
        self._feed_port: FeedPort | None = None
        restored = self.restore()
        if restored is not None:
            self._machine.restore(restored)
            self._enrichment = float(restored.get("enrichment", 0.0))
            self._setpoint = float(restored.get("setpoint", 0.0))
            last_good = restored.get("last_good_setpoint")
            self._last_good_setpoint = None if last_good is None else float(last_good)
            self._flow_nm3h = float(restored.get("flow_nm3h", 0.0))
            baseline = restored.get("baseline") or {}
            if baseline:
                self._baseline_value = float(baseline.get("value", 0.0))
                self._baseline_at = baseline.get("at")
                self._baseline_epoch = baseline.get("epoch")
                self._baseline_source = baseline.get("source")
            self._rollback_count = int(restored.get("rollback_count", 0))
            readings = restored.get("readings")
            if isinstance(readings, list):
                self._readings = [entry for entry in readings if isinstance(entry, dict)]
        self._refresh_gauges()

    def bind_feed_port(self, port: FeedPort) -> None:
        """注入精矿喷吹端口：富氧降档前必须确认喷吹已经停止。"""

        self._feed_port = port

    # ------------------------------------------------------------------ 动作
    def set_baseline(
        self,
        actor: str,
        *,
        value: float,
        source: str,
        observed_at: str | None = None,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "set_baseline",
            "oxygen",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            self._validate_enrichment(value, field="基线")
            if not source:
                raise GuardViolation("基线必须标注来源")
            epoch = self.clock.timestamp()
            at = observed_at or self.clock.timestamp_iso()
            if observed_at is not None:
                epoch = epoch_from_iso(observed_at)
                if epoch > self.clock.timestamp():
                    raise GuardViolation("基线观测时间晚于当前时刻")
            self._baseline_value = value
            self._baseline_at = at
            self._baseline_epoch = epoch
            self._baseline_source = source
            record = self._persist(reason="set_baseline")
            trace.attach(record).note("value", value).note("source", source)
            return self.status()

    def record_reading(
        self,
        actor: str,
        *,
        value: float,
        observed_at: str | None = None,
        correlation_id: str | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action("record_reading", "oxygen", actor, correlation_id=correlation_id) as trace:
            self._validate_enrichment(value, field="读数")
            epoch = self.clock.timestamp() if observed_at is None else epoch_from_iso(observed_at)
            if epoch > self.clock.timestamp():
                raise GuardViolation("读数观测时间晚于当前时刻")
            if self._baseline_epoch is not None and epoch < self._baseline_epoch:
                raise StaleBaselineError(
                    "读数早于最近一次基线，属于滞后样本，已拒绝入库",
                    details={
                        "reading_at": observed_at or self.clock.timestamp_iso(),
                        "baseline_at": self._baseline_at,
                    },
                )
            self._readings.append({"value": round(value, 4), "at": self.clock.timestamp_iso(), "epoch": epoch})
            self._readings = self._readings[-32:]
            deviation = round(value - self._setpoint, 4)
            self._enrichment = value
            if (
                self._machine.state == "established"
                and abs(deviation) > self.settings.oxygen_analyzer_tolerance
            ):
                self._machine.to("degraded", actor, f"氧浓度偏离设定值 {deviation:+.4f}")
            record = self._persist(reason="record_reading")
            trace.attach(record).note("value", value).note("deviation", deviation)
            return self.status()

    def establish(
        self,
        actor: str,
        *,
        target_enrichment: float,
        flow_nm3h: float,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "establish",
            "oxygen",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
            bump_generation=True,
        ) as trace:
            self._validate_enrichment(target_enrichment, field="目标富氧浓度")
            self._require_baseline_fresh()
            if flow_nm3h < self.settings.oxygen_flow_min_nm3h:
                raise GuardViolation(
                    "富氧流量不足，无法建立",
                    details={"flow_nm3h": flow_nm3h, "min": self.settings.oxygen_flow_min_nm3h},
                )
            if self._machine.state == "established" and abs(self._setpoint - target_enrichment) < 1e-9:
                raise StateTransitionError("富氧已经建立在同一设定值", details={"setpoint": self._setpoint})
            if self._machine.state in ("idle", "rolled_back", "degraded"):
                self._machine.to("ramping", actor, "建立富氧")
            elif self._machine.state == "ramping":
                pass
            else:
                self._machine.to("ramping", actor, "重新建立富氧")
            self._setpoint = target_enrichment
            self._enrichment = target_enrichment
            self._flow_nm3h = flow_nm3h
            self._last_good_setpoint = target_enrichment
            self._machine.to("established", actor, "富氧到位")
            record = self._persist(reason="establish")
            trace.attach(record).note("target_enrichment", target_enrichment)
            return self.status()

    def ramp(
        self,
        actor: str,
        *,
        target_enrichment: float,
        step: float | None = None,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "ramp",
            "oxygen",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
            bump_generation=True,
        ) as trace:
            self._machine.require("established", "富氧调档")
            self._validate_enrichment(target_enrichment, field="目标富氧浓度")
            self._require_baseline_fresh()
            increment = self.settings.oxygen_ramp_step if step is None else step
            if increment <= 0:
                raise GuardViolation("爬坡步长必须为正", details={"step": increment})
            delta = target_enrichment - self._setpoint
            if abs(delta) < 1e-9:
                raise GuardViolation("目标值与当前设定值一致", details={"setpoint": self._setpoint})
            if abs(delta) - increment > 1e-9:
                raise GuardViolation(
                    "单次调档不得超过步长，请分步执行",
                    details={"delta": round(delta, 4), "step": increment},
                )
            self._setpoint = round(self._setpoint + delta, 4)
            self._enrichment = self._setpoint
            self._last_good_setpoint = self._setpoint
            record = self._persist(reason="ramp")
            trace.attach(record).note("setpoint", self._setpoint)
            return self.status()

    def rollback(
        self,
        actor: str,
        *,
        reason: str,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "rollback",
            "oxygen",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
            bump_generation=True,
        ) as trace:
            if not reason:
                raise GuardViolation("回滚必须给出原因")
            if self._last_good_setpoint is None:
                raise GuardViolation("没有可回滚的富氧设定值", details={"state": self._machine.state})
            self._machine.to("rolled_back", actor, reason)
            self._setpoint = self._last_good_setpoint
            self._enrichment = self._last_good_setpoint
            self._rollback_count += 1
            record = self._persist(reason="rollback")
            trace.attach(record).note("reason", reason).note("setpoint", self._setpoint)
            return self.status()

    def ramp_down(
        self,
        actor: str,
        *,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        """停机降档：必须先确认精矿喷吹已经停止。"""

        actor = ensure_actor(actor)
        with self.action(
            "ramp_down",
            "oxygen",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
            bump_generation=True,
        ) as trace:
            if self._feed_port is not None and self._feed_port.is_flowing():
                raise GuardViolation(
                    "精矿喷吹仍在进行，禁止富氧降档",
                    details={"state": self._machine.state, "feed": self._feed_port.status().get("state")},
                )
            if self._machine.state not in ("established", "degraded"):
                raise StateTransitionError(
                    "只有已建立/降级的富氧系统可以降档",
                    details={"state": self._machine.state},
                )
            self._setpoint = self.settings.oxygen_enrichment_min
            self._enrichment = self._setpoint
            self._flow_nm3h = 0.0
            self._machine.to("degraded", actor, "停机降档")
            record = self._persist(reason="ramp_down")
            trace.attach(record).note("setpoint", self._setpoint)
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
            "oxygen",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            self._machine.to("idle", actor, "复位富氧系统")
            self._enrichment = 0.0
            self._setpoint = 0.0
            self._flow_nm3h = 0.0
            record = self._persist(reason="reset")
            trace.attach(record)
            return self.status()

    # ------------------------------------------------------------------ 查询
    @property
    def state(self) -> str:
        return self._machine.state

    def baseline_status(self) -> Mapping[str, Any]:
        if self._baseline_value is None or self._baseline_epoch is None:
            return {
                "value": None,
                "at": None,
                "age_seconds": None,
                "fresh": False,
                "window_seconds": self.settings.oxygen_baseline_window_seconds,
                "source": None,
            }
        age = max(0.0, self.clock.timestamp() - float(self._baseline_epoch))
        return {
            "value": round(float(self._baseline_value), 4),
            "at": self._baseline_at,
            "age_seconds": round(age, 3),
            "fresh": age <= self.settings.oxygen_baseline_window_seconds,
            "window_seconds": self.settings.oxygen_baseline_window_seconds,
            "source": self._baseline_source,
        }

    def ensure_established_for_feed(self) -> Mapping[str, Any]:
        """精矿喷吹的前置校验：富氧已建立且基线新鲜。"""

        self._require_baseline_fresh()
        if self._machine.state != "established":
            raise GuardViolation(
                "富氧未建立到位，禁止精矿喷吹",
                details={"state": self._machine.state, "setpoint": self._setpoint},
            )
        if self._enrichment < self.settings.oxygen_enrichment_min:
            raise GuardViolation(
                "富氧浓度低于喷吹下限",
                details={"enrichment": self._enrichment, "min": self.settings.oxygen_enrichment_min},
            )
        return {
            "enrichment": round(self._enrichment, 4),
            "setpoint": round(self._setpoint, 4),
            "flow_nm3h": round(self._flow_nm3h, 3),
            "baseline": self.baseline_status(),
        }

    def status(self) -> Mapping[str, Any]:
        return {
            "state": self._machine.state,
            "enrichment": round(self._enrichment, 4),
            "setpoint": round(self._setpoint, 4),
            "last_good_setpoint": self._last_good_setpoint,
            "flow_nm3h": round(self._flow_nm3h, 3),
            "rollback_count": self._rollback_count,
            "baseline": dict(self.baseline_status()),
            "readings": list(self._readings[-5:]),
            "history": list(self._machine.history),
        }

    # ------------------------------------------------------------------ 内部
    def _validate_enrichment(self, value: float, *, field: str) -> None:
        if not self.settings.oxygen_enrichment_min <= value <= self.settings.oxygen_enrichment_max:
            raise GuardViolation(
                f"{field}超出富氧量程",
                details={
                    "value": value,
                    "min": self.settings.oxygen_enrichment_min,
                    "max": self.settings.oxygen_enrichment_max,
                },
            )

    def _require_baseline_fresh(self) -> None:
        baseline = self.baseline_status()
        if baseline["value"] is None:
            raise StaleBaselineError("缺少分析仪基线，必须先标定基线")
        if not baseline["fresh"]:
            raise StaleBaselineError(
                "分析仪基线已过期，指令作废",
                details={
                    "age_seconds": baseline["age_seconds"],
                    "window_seconds": baseline["window_seconds"],
                    "value": baseline["value"],
                },
            )

    def _persist(self, *, reason: str) -> Any:
        payload = {
            "state": self._machine.state,
            "reason": reason,
            "written_epoch": self.clock.timestamp(),
            "written_at": self.clock.timestamp_iso(),
            "enrichment": round(self._enrichment, 4),
            "setpoint": round(self._setpoint, 4),
            "last_good_setpoint": self._last_good_setpoint,
            "flow_nm3h": round(self._flow_nm3h, 3),
            "rollback_count": self._rollback_count,
            "baseline": {
                "value": self._baseline_value,
                "at": self._baseline_at,
                "epoch": self._baseline_epoch,
                "source": self._baseline_source,
            },
            "readings": list(self._readings[-8:]),
            "history": list(self._machine.history),
        }
        record = self.persist_state(payload)
        self._refresh_gauges()
        return record

    def _refresh_gauges(self) -> None:
        self.metrics.observe("oxygen.enrichment", round(self._enrichment, 4))
        self.metrics.observe("oxygen.setpoint", round(self._setpoint, 4))
        baseline = self.baseline_status()
        if baseline["age_seconds"] is not None:
            self.metrics.observe("oxygen.baseline_age_seconds", baseline["age_seconds"])


__all__ = ["OxygenSystem", "STATES", "TRANSITIONS"]
