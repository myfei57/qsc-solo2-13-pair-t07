"""闪速炉总编排。

闪速炉把各子系统串成一条受控链：余热锅炉投运 → 吹扫 → 燃烧器点火稳定 →
富氧建立 → 精矿喷吹 → 沉淀分层 → 放渣放铜 → 停机。编排不替代子系统的门控，
只负责按工艺顺序调用：任何一步失败都会中止，绝不带着缺陷继续往下走。
"""

from __future__ import annotations

from typing import Any, Mapping

from ..component import Component, ensure_actor
from ..errors import GuardViolation, StateTransitionError
from ..machine import StateMachine
from ..ports import (
    BurnerPort,
    ConverterPort,
    FeedPort,
    MattePort,
    OxygenPort,
    SettlerPort,
    SlagPort,
    WastePort,
)
from ..runtime import RuntimeContext

STATES = ("cold", "purging", "oxygen_ready", "smelting", "tapping", "stopping", "stopped", "latched")

TRANSITIONS: Mapping[str, tuple[str, ...]] = {
    "cold": ("purging", "latched"),
    "purging": ("oxygen_ready", "stopping", "latched"),
    "oxygen_ready": ("smelting", "stopping", "latched"),
    "smelting": ("tapping", "stopping", "latched"),
    "tapping": ("smelting", "stopping", "latched"),
    "stopping": ("stopped", "latched"),
    "stopped": ("purging", "cold", "latched"),
    "latched": ("cold",),
}

HEAT_STREAM = "furnace/heats"


class FlashFurnace(Component):
    name = "furnace"

    def __init__(
        self,
        ctx: RuntimeContext,
        *,
        burner: BurnerPort,
        oxygen: OxygenPort,
        conc: FeedPort,
        settler: SettlerPort,
        slag: SlagPort,
        matte: MattePort,
        waste: WastePort,
        converter: ConverterPort,
    ) -> None:
        super().__init__(ctx)
        self._machine = StateMachine("furnace", "cold", TRANSITIONS, ctx.clock)
        self._burner = burner
        self._oxygen = oxygen
        self._conc = conc
        self._settler = settler
        self._slag = slag
        self._matte = matte
        self._waste = waste
        self._converter = converter
        self._heat_id: str | None = None
        self._started_at: str | None = None
        self._purge_started_at: float | None = None
        self._smelt_started_at: float | None = None
        self._last_tap_at: str | None = None
        self._latch_reason: str | None = None
        self._heats: list[dict[str, Any]] = []
        restored = self.restore()
        if restored is not None:
            self._machine.restore(restored)
            self._heat_id = restored.get("heat_id")
            self._started_at = restored.get("started_at")
            self._purge_started_at = restored.get("purge_started_at")
            self._smelt_started_at = restored.get("smelt_started_at")
            self._last_tap_at = restored.get("last_tap_at")
            self._latch_reason = restored.get("latch_reason")
            heats = restored.get("heats")
            if isinstance(heats, list):
                self._heats = [entry for entry in heats if isinstance(entry, dict)]
        self._refresh_gauges()

    # ------------------------------------------------------------------ 编排
    def start(
        self,
        actor: str,
        *,
        drum_level: float,
        fuel_pressure_kpa: float,
        air_flow_nm3h: float,
        oxygen_baseline: float,
        oxygen_baseline_source: str,
        oxygen_target: float,
        oxygen_flow_nm3h: float,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "start",
            "furnace",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
            bump_generation=True,
        ) as trace:
            self._machine.require_one_of(("cold", "stopped"), "启动闪速炉")
            self._machine.to("purging", actor, "启动吹扫")
            self._purge_started_at = self.clock.timestamp()
            self._persist(reason="purge")
            waste_state = self._waste.status()["state"]
            if waste_state == "latched":
                raise GuardViolation("余热锅炉处于闩锁，禁止启动闪速炉")
            if waste_state in ("idle", "cooling"):
                self._waste.start(actor, drum_level=drum_level)
            self._burner.ignite(
                actor, fuel_pressure_kpa=fuel_pressure_kpa, air_flow_nm3h=air_flow_nm3h
            )
            self._burner.confirm_flame(actor)
            self._burner.stabilize(actor)
            self._oxygen.set_baseline(actor, value=oxygen_baseline, source=oxygen_baseline_source)
            self._oxygen.establish(
                actor, target_enrichment=oxygen_target, flow_nm3h=oxygen_flow_nm3h
            )
            self._machine.to("oxygen_ready", actor, "富氧建立")
            self._started_at = self.clock.timestamp_iso()
            record = self._persist(reason="start")
            trace.attach(record).note("oxygen_target", oxygen_target)
            return self.status()

    def feed(
        self,
        actor: str,
        *,
        heat_id: str,
        rate_tph: float,
        tons: float,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "feed",
            f"furnace/{heat_id}",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
            bump_generation=True,
        ) as trace:
            self._machine.require_one_of(("oxygen_ready", "smelting"), "精矿喷吹")
            self._require_startup_window()
            if self._waste.is_latched():
                raise GuardViolation("余热锅炉闩锁未复位，禁止喷吹")
            conc_status = self._conc.status()
            if conc_status["state"] == "blocked" or conc_status["heat_id"] != heat_id:
                self._conc.arm(actor, heat_id=heat_id)
            self._conc.inject(actor, rate_tph=rate_tph, tons=tons)
            if self._machine.state != "smelting":
                self._machine.to("smelting", actor, f"炉次 {heat_id} 开始熔炼")
            if self._smelt_started_at is None:
                self._smelt_started_at = self.clock.timestamp()
            self._purge_started_at = None
            self._heat_id = heat_id
            record = self._persist(reason="feed")
            trace.attach(record).note("tons", tons).note("fed_tons", self._conc.status()["fed_tons"])
            return self.status()

    def tap(
        self,
        actor: str,
        *,
        heat_id: str,
        ladle_id: str,
        slag_tons: float,
        matte_tons: float,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "tap",
            f"furnace/{heat_id}",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
            bump_generation=True,
        ) as trace:
            self._machine.require("smelting", "放渣放铜")
            remaining = self.smelt_dwell_remaining()
            if remaining > 0:
                raise GuardViolation(
                    "熔炼静置时长不足，禁止放料",
                    details={
                        "remaining_seconds": round(remaining, 3),
                        "required_seconds": self.settings.furnace_min_smelt_dwell_seconds,
                    },
                )
            if self._waste.is_latched():
                raise GuardViolation("余热锅炉闩锁未复位，禁止放料")
            conc_state = self._conc.status()["state"]
            if conc_state in ("armed", "injecting", "paused"):
                self._conc.stop(actor)  # 放料前先停精矿喷吹
            self._settler.settle(actor, heat_id=heat_id)
            self._machine.to("tapping", actor, "转入放料")
            self._persist(reason="tap-begin")
            self._slag.tap(actor, heat_id=heat_id, target_tons=slag_tons)
            self._matte.tap(actor, heat_id=heat_id, ladle_id=ladle_id, target_tons=matte_tons)
            self._machine.to("smelting", actor, "放料结束")
            heat = {
                "heat_id": heat_id,
                "ladle_id": ladle_id,
                "slag_tons": round(slag_tons, 3),
                "matte_tons": round(matte_tons, 3),
                "tapped_at": self.clock.timestamp_iso(),
                "actor": actor,
            }
            self._heats.append(heat)
            self._heats = self._heats[-16:]
            self._last_tap_at = heat["tapped_at"]
            self.store.append(
                HEAT_STREAM,
                {
                    **heat,
                    "feed_tons": self._conc.status()["fed_tons"],
                    "oxygen_setpoint": self._oxygen.status()["setpoint"],
                    "converter_state": self._converter.status()["state"],
                },
            )
            record = self._persist(reason="tap")
            trace.attach(record).note("heat_id", heat_id)
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
            "furnace",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
            bump_generation=True,
        ) as trace:
            self._machine.require_one_of(("oxygen_ready", "smelting", "tapping", "purging"), "停机")
            self._machine.to("stopping", actor, "按顺序停机")
            self._persist(reason="stopping")
            conc_state = self._conc.status()["state"]
            if conc_state in ("armed", "injecting", "paused"):
                self._conc.stop(actor)  # 先停喷吹
            self._oxygen.ramp_down(actor)  # 再降富氧
            burner_state = self._burner.status()["state"]
            if burner_state in ("ignited", "stable"):
                self._burner.cool_down(actor)
            waste_state = self._waste.status()["state"]
            if waste_state in ("circulating", "heat_exchanging", "latched"):
                self._waste.cooldown(actor)
            self._machine.to("stopped", actor, "停机完成")
            self._smelt_started_at = None
            self._purge_started_at = None
            record = self._persist(reason="stop")
            trace.attach(record)
            return self.status()

    def latch(
        self,
        actor: str,
        *,
        reason: str,
        correlation_id: str | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "latch", "furnace", actor, correlation_id=correlation_id, bump_generation=True
        ) as trace:
            if not reason:
                raise GuardViolation("炉体联锁必须给出原因")
            if self._machine.state == "latched":
                raise StateTransitionError("闪速炉已处于联锁状态", details={"state": self._machine.state})
            self._machine.to("latched", actor, reason)
            self._latch_reason = reason
            record = self._persist(reason="latch")
            trace.attach(record).note("latch_reason", reason)
            return self.status()

    def reset(
        self,
        actor: str,
        *,
        note: str,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        actor = ensure_actor(actor)
        with self.action(
            "reset",
            "furnace",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
            bump_generation=True,
        ) as trace:
            self._machine.require("latched", "联锁复位")
            if not note:
                raise GuardViolation("复位必须填写处理说明")
            blockers: list[str] = []
            if self._waste.is_latched():
                blockers.append("waste-latched")
            if self._burner.is_latched():
                blockers.append("burner-latched")
            if self._conc.is_flowing():
                blockers.append("conc-flowing")
            if blockers:
                raise GuardViolation("子系统尚未就绪，禁止复位闪速炉", details={"blockers": blockers})
            self._machine.to("cold", actor, f"复位：{note}")
            self._latch_reason = None
            record = self._persist(reason="reset")
            trace.attach(record).note("note", note)
            return self.status()

    # ------------------------------------------------------------------ 查询
    @property
    def state(self) -> str:
        return self._machine.state

    @property
    def heat_id(self) -> str | None:
        return self._heat_id

    def smelt_dwell_remaining(self) -> float:
        if self._smelt_started_at is None:
            return 0.0
        elapsed = max(0.0, self.clock.timestamp() - float(self._smelt_started_at))
        return max(0.0, self.settings.furnace_min_smelt_dwell_seconds - elapsed)

    def purge_remaining(self) -> float:
        if self._purge_started_at is None:
            return 0.0
        elapsed = max(0.0, self.clock.timestamp() - float(self._purge_started_at))
        return max(0.0, self.settings.furnace_purge_seconds - elapsed)

    def heats(self, *, limit: int = 20) -> list[Mapping[str, Any]]:
        return [entry.payload for entry in self.store.read_stream(HEAT_STREAM, limit=limit)]

    def heat_report(self) -> Mapping[str, Any]:
        """当前炉次汇总视图，供控制台首页与班报使用。"""

        conc = self._conc.status()
        heat_id = self._heat_id
        return {
            "heat_id": heat_id,
            "state": self._machine.state,
            "feed_tons": conc["fed_tons"],
            "budget_remaining_tons": conc["budget_remaining_tons"],
            "slag_tapped_tons": 0.0 if heat_id is None else self._slag.heat_tapped_tons(heat_id),
            "matte_tapped_tons": 0.0 if heat_id is None else self._matte.heat_tapped_tons(heat_id),
            "heats_completed": len(self._heats),
        }

    def status(self) -> Mapping[str, Any]:
        return {
            "state": self._machine.state,
            "heat_id": self._heat_id,
            "started_at": self._started_at,
            "last_tap_at": self._last_tap_at,
            "latch_reason": self._latch_reason,
            "smelt_dwell_remaining_seconds": round(self.smelt_dwell_remaining(), 3),
            "purge_remaining_seconds": round(self.purge_remaining(), 3),
            "heats": list(self._heats[-4:]),
            "subsystems": {
                "burner": self._burner.status()["state"],
                "oxygen": self._oxygen.status()["state"],
                "conc": self._conc.status()["state"],
                "settler": self._settler.status()["state"],
                "slag": self._slag.status()["state"],
                "matte": self._matte.status()["state"],
                "waste": self._waste.status()["state"],
            },
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
            "started_at": self._started_at,
            "purge_started_at": self._purge_started_at,
            "smelt_started_at": self._smelt_started_at,
            "last_tap_at": self._last_tap_at,
            "latch_reason": self._latch_reason,
            "heats": list(self._heats[-8:]),
            "history": list(self._machine.history),
        }
        record = self.persist_state(payload)
        self._refresh_gauges()
        return record

    def _require_startup_window(self) -> None:
        """启动窗口看门狗：吹扫开始后必须在超时内转入喷吹。"""

        if self._purge_started_at is None:
            return
        elapsed = max(0.0, self.clock.timestamp() - float(self._purge_started_at))
        if elapsed > self.settings.furnace_transition_timeout_seconds:
            raise GuardViolation(
                "启动窗口超时，需要重新吹扫后再喷吹",
                details={
                    "elapsed_seconds": round(elapsed, 3),
                    "timeout_seconds": self.settings.furnace_transition_timeout_seconds,
                },
            )

    def _refresh_gauges(self) -> None:
        self.metrics.observe("furnace.state_code", float(STATES.index(self._machine.state)))
        self.metrics.observe("furnace.heats_completed", float(len(self._heats)))
        self.metrics.observe(
            "furnace.smelt_dwell_remaining_seconds", round(self.smelt_dwell_remaining(), 3)
        )


__all__ = ["FlashFurnace", "STATES", "TRANSITIONS", "HEAT_STREAM"]
