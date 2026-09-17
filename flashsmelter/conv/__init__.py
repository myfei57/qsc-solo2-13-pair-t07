"""转炉组件。

转炉按「入炉 → 吹炼 → 扒渣 → 出铜」的顺序推进，每个动作都要求前一步已经完成；
入炉的冰铜必须是本炉次已放渣之后产生的可追溯批记录，且单批不超过容量上限。
"""

from __future__ import annotations

import uuid
from typing import Any, Mapping

from ..component import Component, ensure_actor
from ..errors import GuardViolation, NotFoundError, StateTransitionError
from ..machine import StateMachine
from ..ports import MattePort
from ..runtime import RuntimeContext

STATES = ("idle", "charging", "blowing", "skimming", "discharging")

TRANSITIONS: Mapping[str, tuple[str, ...]] = {
    "idle": ("charging",),
    "charging": ("blowing",),
    "blowing": ("skimming",),
    "skimming": ("discharging",),
    "discharging": ("discharging", "idle"),
}

BATCH_STREAM = "conv/batches"


class Converter(Component):
    name = "conv"

    def __init__(self, ctx: RuntimeContext, *, matte: MattePort) -> None:
        super().__init__(ctx)
        self._machine = StateMachine("conv", "idle", TRANSITIONS, ctx.clock)
        self._matte = matte
        self._batch_id: str | None = None
        self._ladle_id: str | None = None
        self._heat_id: str | None = None
        self._charged_tons = 0.0
        self._blown_seconds = 0.0
        self._skimmed_tons = 0.0
        self._discharged_tons = 0.0
        self._batches_completed = 0
        restored = self.restore()
        if restored is not None:
            self._machine.restore(restored)
            self._batch_id = restored.get("batch_id")
            self._ladle_id = restored.get("ladle_id")
            self._heat_id = restored.get("heat_id")
            self._charged_tons = float(restored.get("charged_tons", 0.0))
            self._blown_seconds = float(restored.get("blown_seconds", 0.0))
            self._skimmed_tons = float(restored.get("skimmed_tons", 0.0))
            self._discharged_tons = float(restored.get("discharged_tons", 0.0))
            self._batches_completed = int(restored.get("batches_completed", 0))
        self._refresh_gauges()

    # ------------------------------------------------------------------ 动作
    def charge(
        self,
        actor: str,
        *,
        ladle_id: str,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "charge",
            f"conv/{ladle_id}",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            if not ladle_id:
                raise GuardViolation("入炉必须给出包子号")
            if self._machine.state != "idle":
                raise StateTransitionError(
                    "转炉正在处理上一批，禁止入炉", details={"state": self._machine.state}
                )
            charge = self._matte.available_charge(ladle_id)
            if charge is None:
                raise NotFoundError(
                    "找不到可入炉的冰铜批记录（可能尚未放铜或已被吹炼）",
                    details={"ladle_id": ladle_id},
                )
            tons = float(charge["tons"])
            if tons > self.settings.converter_batch_max_tons:
                raise GuardViolation(
                    "冰铜批次超过转炉容量上限",
                    details={"tons": tons, "max": self.settings.converter_batch_max_tons},
                )
            batch_id = f"batch-{uuid.uuid4().hex[:12]}"
            intent = self.write_intent(
                "charge",
                {
                    "action": "charge",
                    "batch_id": batch_id,
                    "ladle_id": ladle_id,
                    "tons": tons,
                    "at": self.clock.timestamp_iso(),
                    "actor": actor,
                },
            )
            self._matte.mark_charged(ladle_id, batch_id, tons, actor)
            self._batch_id = batch_id
            self._ladle_id = ladle_id
            self._heat_id = charge.get("heat_id")
            self._charged_tons = tons
            self._blown_seconds = 0.0
            self._skimmed_tons = 0.0
            self._discharged_tons = 0.0
            self._machine.to("charging", actor, f"第 {self._batches_completed + 1} 批入炉")
            record = self._persist(reason="charge")
            trace.attach(record).note("batch_id", batch_id).note("intent_version", intent.version)
            return self.status()

    def blow(
        self,
        actor: str,
        *,
        seconds: float,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "blow",
            f"conv/{self._batch_id or 'unassigned'}",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            self._machine.require("charging", "吹炼")
            if seconds <= 0:
                raise GuardViolation("吹炼时长必须为正", details={"seconds": seconds})
            self._blown_seconds = round(self._blown_seconds + seconds, 3)
            self._machine.to("blowing", actor, "开始吹炼")
            record = self._persist(reason="blow")
            trace.attach(record).note("blown_seconds", self._blown_seconds)
            return self.status()

    def skim(
        self,
        actor: str,
        *,
        tons: float,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "skim",
            f"conv/{self._batch_id or 'unassigned'}",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            self._machine.require("blowing", "扒渣")
            if tons <= 0:
                raise GuardViolation("扒渣吨位必须为正", details={"tons": tons})
            if self._skimmed_tons + tons > self._charged_tons:
                raise GuardViolation(
                    "扒渣量超过入炉总量",
                    details={
                        "charged_tons": round(self._charged_tons, 3),
                        "skimmed_tons": round(self._skimmed_tons, 3),
                        "requested_tons": tons,
                    },
                )
            self._skimmed_tons = round(self._skimmed_tons + tons, 3)
            self._machine.to("skimming", actor, "扒除转炉渣")
            record = self._persist(reason="skim")
            trace.attach(record).note("skimmed_tons", self._skimmed_tons)
            return self.status()

    def discharge(
        self,
        actor: str,
        *,
        tons: float,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "discharge",
            f"conv/{self._batch_id or 'unassigned'}",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            self._machine.require_one_of(("skimming", "discharging"), "出铜")
            if tons <= 0:
                raise GuardViolation("出铜吨位必须为正", details={"tons": tons})
            if self._discharged_tons + tons > self._charged_tons:
                raise GuardViolation(
                    "出铜量超过入炉总量",
                    details={
                        "charged_tons": round(self._charged_tons, 3),
                        "discharged_tons": round(self._discharged_tons, 3),
                        "requested_tons": tons,
                    },
                )
            self._discharged_tons = round(self._discharged_tons + tons, 3)
            if self._machine.state != "discharging":
                self._machine.to("discharging", actor, "出粗铜")
            record = self._persist(reason="discharge")
            trace.attach(record).note("discharged_tons", self._discharged_tons)
            return self.status()

    def finish_batch(
        self,
        actor: str,
        *,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "finish_batch",
            f"conv/{self._batch_id or 'unassigned'}",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            self._machine.require("discharging", "结束批次")
            if self._discharged_tons <= 0:
                raise GuardViolation("尚未出铜，不能结束批次", details={"discharged_tons": self._discharged_tons})
            completed = self._batches_completed + 1
            entry = self.store.append(
                BATCH_STREAM,
                {
                    "batch_id": self._batch_id,
                    "heat_id": self._heat_id,
                    "ladle_id": self._ladle_id,
                    "charged_tons": round(self._charged_tons, 3),
                    "skimmed_tons": round(self._skimmed_tons, 3),
                    "discharged_tons": round(self._discharged_tons, 3),
                    "blown_seconds": round(self._blown_seconds, 3),
                    "completed_at": self.clock.timestamp_iso(),
                    "actor": actor,
                },
            )
            self._batches_completed = completed
            self._machine.to("idle", actor, f"第 {completed} 批结束")
            self._batch_id = None
            self._ladle_id = None
            self._heat_id = None
            self._charged_tons = 0.0
            self._blown_seconds = 0.0
            self._skimmed_tons = 0.0
            self._discharged_tons = 0.0
            record = self._persist(reason="finish_batch")
            trace.attach(record).note("batch_seq", entry.seq).note("batches_completed", completed)
            return self.status()

    # ------------------------------------------------------------------ 查询
    @property
    def state(self) -> str:
        return self._machine.state

    def can_accept(self, tons: float) -> bool:
        return self._machine.state == "idle" and 0 < tons <= self.settings.converter_batch_max_tons

    def batches(self, *, limit: int = 20) -> list[Mapping[str, Any]]:
        return [entry.payload for entry in self.store.read_stream(BATCH_STREAM, limit=limit)]

    def status(self) -> Mapping[str, Any]:
        return {
            "state": self._machine.state,
            "batch_id": self._batch_id,
            "ladle_id": self._ladle_id,
            "heat_id": self._heat_id,
            "charged_tons": round(self._charged_tons, 3),
            "blown_seconds": round(self._blown_seconds, 3),
            "skimmed_tons": round(self._skimmed_tons, 3),
            "discharged_tons": round(self._discharged_tons, 3),
            "batches_completed": self._batches_completed,
            "max_batch_tons": self.settings.converter_batch_max_tons,
            "history": list(self._machine.history),
        }

    # ------------------------------------------------------------------ 内部
    def _persist(self, *, reason: str) -> Any:
        payload = {
            "state": self._machine.state,
            "reason": reason,
            "written_epoch": self.clock.timestamp(),
            "written_at": self.clock.timestamp_iso(),
            "batch_id": self._batch_id,
            "ladle_id": self._ladle_id,
            "heat_id": self._heat_id,
            "charged_tons": round(self._charged_tons, 3),
            "blown_seconds": round(self._blown_seconds, 3),
            "skimmed_tons": round(self._skimmed_tons, 3),
            "discharged_tons": round(self._discharged_tons, 3),
            "batches_completed": self._batches_completed,
            "history": list(self._machine.history),
        }
        record = self.persist_state(payload)
        self._refresh_gauges()
        return record

    def _refresh_gauges(self) -> None:
        self.metrics.observe("conv.charged_tons", round(self._charged_tons, 3))
        self.metrics.observe("conv.batches_completed", float(self._batches_completed))


__all__ = ["Converter", "STATES", "TRANSITIONS", "BATCH_STREAM"]
