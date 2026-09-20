"""组装根。

控制台与 CLI 都从这里拿到同一套组件与动作注册表：状态只装配一次，动作只在
一处定义，避免出现「控制台能调、CLI 调不了」这类不一致。
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from .audit import AuditLog
from .burner import Burner
from .component import Component, ensure_actor
from .conc import ConcentrateSystem
from .config import Settings
from .conv import Converter
from .errors import ValidationError
from .fire import FireLinkage
from .furnace import FlashFurnace
from .matte import MatteTap
from .ns import Namespace
from .oxygen import OxygenSystem
from .params import Params
from .runtime import Clock, Generation, Metrics, RuntimeContext
from .settler import Settler
from .slag import SlagTap
from .store import DurableStore
from .waste import WasteHeatBoiler

ActionHandler = Callable[[Params], Mapping[str, Any]]


class Application:
    """把配置、持久化、组件与控制台动作装配成一个可运行实例。"""

    def __init__(self, settings: Settings, *, clock: Clock | None = None) -> None:
        settings.validate()
        settings.ensure_directories()
        self.settings = settings
        self.clock = clock or Clock()
        self.namespace = Namespace.parse(settings.namespace)
        self.metrics = Metrics(self.clock)
        self.generation = Generation(self.clock)
        self.store = DurableStore(settings.root, clock=self.clock)
        self.audit = AuditLog(self.store, self.namespace, self.clock)
        self.ctx = RuntimeContext(
            settings=settings,
            namespace=self.namespace,
            store=self.store,
            clock=self.clock,
            metrics=self.metrics,
            generation=self.generation,
            audit=self.audit,
        )
        self._build_components()
        self._actions: dict[str, ActionHandler] = self._build_actions()

    # ------------------------------------------------------------- 组件装配
    def _build_components(self) -> None:
        ctx = self.ctx
        self.burner = Burner(ctx)
        self.waste = WasteHeatBoiler(ctx)
        self.settler = Settler(ctx)
        self.oxygen = OxygenSystem(ctx)
        self.slag = SlagTap(ctx, settler=self.settler, waste=self.waste)
        self.matte = MatteTap(ctx, settler=self.settler, waste=self.waste, slag=self.slag)
        self.conv = Converter(ctx, matte=self.matte)
        self.conc = ConcentrateSystem(
            ctx, burner=self.burner, oxygen=self.oxygen, waste=self.waste, settler=self.settler
        )
        self.furnace = FlashFurnace(
            ctx,
            burner=self.burner,
            oxygen=self.oxygen,
            conc=self.conc,
            settler=self.settler,
            slag=self.slag,
            matte=self.matte,
            waste=self.waste,
            converter=self.conv,
        )
        self.slag.bind_matte(self.matte)
        self.matte.bind_converter(self.conv)
        self.oxygen.bind_feed_port(self.conc)
        self.fire = FireLinkage(ctx)
        self.components: tuple[Component, ...] = (
            self.furnace,
            self.burner,
            self.oxygen,
            self.conc,
            self.settler,
            self.slag,
            self.matte,
            self.conv,
            self.waste,
            self.fire,
        )
        self._by_name: dict[str, Component] = {component.name: component for component in self.components}

    # ------------------------------------------------------------- 动作注册
    def _build_actions(self) -> dict[str, ActionHandler]:
        actions: dict[str, ActionHandler] = {}

        def register(name: str) -> Callable[[ActionHandler], ActionHandler]:
            def decorate(handler: ActionHandler) -> ActionHandler:
                if name in actions:
                    raise ValidationError("动作重复注册", details={"action": name})
                actions[name] = handler
                return handler

            return decorate

        @register("furnace.start")
        def _furnace_start(params: Params) -> Mapping[str, Any]:
            return self.furnace.start(
                params.text("actor", required=False, default="control-room"),
                drum_level=params.number("drum_level", minimum=0.0, maximum=1.0),
                fuel_pressure_kpa=params.number("fuel_pressure_kpa", minimum=0.0),
                air_flow_nm3h=params.number("air_flow_nm3h", minimum=0.0),
                oxygen_baseline=params.number("oxygen_baseline", minimum=0.0, maximum=1.0),
                oxygen_baseline_source=params.text("oxygen_baseline_source"),
                oxygen_target=params.number("oxygen_target", minimum=0.0, maximum=1.0),
                oxygen_flow_nm3h=params.number("oxygen_flow_nm3h", minimum=0.0),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("furnace.feed")
        def _furnace_feed(params: Params) -> Mapping[str, Any]:
            return self.furnace.feed(
                params.text("actor", required=False, default="control-room"),
                heat_id=params.text("heat_id"),
                rate_tph=params.number("rate_tph", minimum=0.0),
                tons=params.number("tons", minimum=0.0),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("furnace.tap")
        def _furnace_tap(params: Params) -> Mapping[str, Any]:
            return self.furnace.tap(
                params.text("actor", required=False, default="control-room"),
                heat_id=params.text("heat_id"),
                ladle_id=params.text("ladle_id"),
                slag_tons=params.number("slag_tons", minimum=0.0),
                matte_tons=params.number("matte_tons", minimum=0.0),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("furnace.stop")
        def _furnace_stop(params: Params) -> Mapping[str, Any]:
            return self.furnace.stop(
                params.text("actor", required=False, default="control-room"),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("furnace.latch")
        def _furnace_latch(params: Params) -> Mapping[str, Any]:
            return self.furnace.latch(
                params.text("actor", required=False, default="control-room"),
                reason=params.text("reason"),
                correlation_id=params.optional_text("correlation_id"),
            )

        @register("furnace.reset")
        def _furnace_reset(params: Params) -> Mapping[str, Any]:
            return self.furnace.reset(
                params.text("actor", required=False, default="control-room"),
                note=params.text("note"),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("burner.ignite")
        def _burner_ignite(params: Params) -> Mapping[str, Any]:
            return self.burner.ignite(
                params.text("actor", required=False, default="control-room"),
                fuel_pressure_kpa=params.number("fuel_pressure_kpa", minimum=0.0),
                air_flow_nm3h=params.number("air_flow_nm3h", minimum=0.0),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("burner.stabilize")
        def _burner_stabilize(params: Params) -> Mapping[str, Any]:
            return self.burner.stabilize(
                params.text("actor", required=False, default="control-room"),
                hold_seconds=params.optional_number("hold_seconds", minimum=0.0),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("burner.confirm_flame")
        def _burner_confirm_flame(params: Params) -> Mapping[str, Any]:
            return self.burner.confirm_flame(
                params.text("actor", required=False, default="control-room"),
                flame_detected=params.boolean("flame_detected", required=False, default=True),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("burner.cool_down")
        def _burner_cool_down(params: Params) -> Mapping[str, Any]:
            return self.burner.cool_down(
                params.text("actor", required=False, default="control-room"),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("burner.finish_cooling")
        def _burner_finish_cooling(params: Params) -> Mapping[str, Any]:
            return self.burner.finish_cooling(
                params.text("actor", required=False, default="control-room"),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("burner.trip")
        def _burner_trip(params: Params) -> Mapping[str, Any]:
            return self.burner.trip(
                params.text("actor", required=False, default="control-room"),
                reason=params.text("reason"),
                correlation_id=params.optional_text("correlation_id"),
            )

        @register("burner.attest")
        def _burner_attest(params: Params) -> Mapping[str, Any]:
            return self.burner.attest(
                params.text("actor", required=False, default="control-system"),
                fuel_pressure_kpa=params.optional_number("fuel_pressure_kpa", minimum=0.0),
                air_flow_nm3h=params.optional_number("air_flow_nm3h", minimum=0.0),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("burner.reset")
        def _burner_reset(params: Params) -> Mapping[str, Any]:
            return self.burner.reset(
                params.text("actor", required=False, default="control-room"),
                note=params.text("note"),
                fuel_pressure_kpa=params.optional_number("fuel_pressure_kpa", minimum=0.0) or 0.0,
                air_flow_nm3h=params.optional_number("air_flow_nm3h", minimum=0.0) or 0.0,
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("oxygen.set_baseline")
        def _oxygen_baseline(params: Params) -> Mapping[str, Any]:
            return self.oxygen.set_baseline(
                params.text("actor", required=False, default="control-room"),
                value=params.number("value", minimum=0.0, maximum=1.0),
                source=params.text("source"),
                observed_at=params.optional_text("observed_at"),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("oxygen.record_reading")
        def _oxygen_reading(params: Params) -> Mapping[str, Any]:
            return self.oxygen.record_reading(
                params.text("actor", required=False, default="analyzer"),
                value=params.number("value", minimum=0.0, maximum=1.0),
                observed_at=params.optional_text("observed_at"),
                correlation_id=params.optional_text("correlation_id"),
            )

        @register("oxygen.establish")
        def _oxygen_establish(params: Params) -> Mapping[str, Any]:
            return self.oxygen.establish(
                params.text("actor", required=False, default="control-room"),
                target_enrichment=params.number("target_enrichment", minimum=0.0, maximum=1.0),
                flow_nm3h=params.number("flow_nm3h", minimum=0.0),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("oxygen.ramp")
        def _oxygen_ramp(params: Params) -> Mapping[str, Any]:
            return self.oxygen.ramp(
                params.text("actor", required=False, default="control-room"),
                target_enrichment=params.number("target_enrichment", minimum=0.0, maximum=1.0),
                step=params.optional_number("step", minimum=0.0),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("oxygen.rollback")
        def _oxygen_rollback(params: Params) -> Mapping[str, Any]:
            return self.oxygen.rollback(
                params.text("actor", required=False, default="control-room"),
                reason=params.text("reason"),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("oxygen.ramp_down")
        def _oxygen_ramp_down(params: Params) -> Mapping[str, Any]:
            return self.oxygen.ramp_down(
                params.text("actor", required=False, default="control-room"),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("oxygen.reset")
        def _oxygen_reset(params: Params) -> Mapping[str, Any]:
            return self.oxygen.reset(
                params.text("actor", required=False, default="control-room"),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("conc.arm")
        def _conc_arm(params: Params) -> Mapping[str, Any]:
            return self.conc.arm(
                params.text("actor", required=False, default="control-room"),
                heat_id=params.text("heat_id"),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("conc.inject")
        def _conc_inject(params: Params) -> Mapping[str, Any]:
            return self.conc.inject(
                params.text("actor", required=False, default="control-room"),
                rate_tph=params.number("rate_tph", minimum=0.0),
                tons=params.number("tons", minimum=0.0),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("conc.pause")
        def _conc_pause(params: Params) -> Mapping[str, Any]:
            return self.conc.pause(
                params.text("actor", required=False, default="control-room"),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("conc.stop")
        def _conc_stop(params: Params) -> Mapping[str, Any]:
            return self.conc.stop(
                params.text("actor", required=False, default="control-room"),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("conc.release_heat")
        def _conc_release(params: Params) -> Mapping[str, Any]:
            return self.conc.release_heat(
                params.text("actor", required=False, default="control-room"),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("settler.update")
        def _settler_update(params: Params) -> Mapping[str, Any]:
            return self.settler.update(
                params.text("actor", required=False, default="control-room"),
                bath_level_m=params.number("bath_level_m", minimum=0.0),
                slag_thickness_m=params.number("slag_thickness_m", minimum=0.0),
                matte_level_m=params.number("matte_level_m", minimum=0.0),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("settler.settle")
        def _settler_settle(params: Params) -> Mapping[str, Any]:
            return self.settler.settle(
                params.text("actor", required=False, default="control-room"),
                heat_id=params.text("heat_id"),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("settler.begin_tap")
        def _settler_begin_tap(params: Params) -> Mapping[str, Any]:
            return self.settler.begin_tap(
                params.text("actor", required=False, default="control-room"),
                kind=params.text("kind"),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("settler.end_tap")
        def _settler_end_tap(params: Params) -> Mapping[str, Any]:
            return self.settler.end_tap(
                params.text("actor", required=False, default="control-room"),
                kind=params.text("kind"),
                tons=params.number("tons", minimum=0.0),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("slag.tap")
        def _slag_tap(params: Params) -> Mapping[str, Any]:
            return self.slag.tap(
                params.text("actor", required=False, default="control-room"),
                heat_id=params.text("heat_id"),
                target_tons=params.number("target_tons", minimum=0.0),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("slag.reset")
        def _slag_reset(params: Params) -> Mapping[str, Any]:
            return self.slag.reset(
                params.text("actor", required=False, default="control-room"),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("matte.tap")
        def _matte_tap(params: Params) -> Mapping[str, Any]:
            return self.matte.tap(
                params.text("actor", required=False, default="control-room"),
                heat_id=params.text("heat_id"),
                ladle_id=params.text("ladle_id"),
                target_tons=params.number("target_tons", minimum=0.0),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("matte.reset")
        def _matte_reset(params: Params) -> Mapping[str, Any]:
            return self.matte.reset(
                params.text("actor", required=False, default="control-room"),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("conv.charge")
        def _conv_charge(params: Params) -> Mapping[str, Any]:
            return self.conv.charge(
                params.text("actor", required=False, default="control-room"),
                ladle_id=params.text("ladle_id"),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("conv.blow")
        def _conv_blow(params: Params) -> Mapping[str, Any]:
            return self.conv.blow(
                params.text("actor", required=False, default="control-room"),
                seconds=params.number("seconds", minimum=0.0),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("conv.skim")
        def _conv_skim(params: Params) -> Mapping[str, Any]:
            return self.conv.skim(
                params.text("actor", required=False, default="control-room"),
                tons=params.number("tons", minimum=0.0),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("conv.discharge")
        def _conv_discharge(params: Params) -> Mapping[str, Any]:
            return self.conv.discharge(
                params.text("actor", required=False, default="control-room"),
                tons=params.number("tons", minimum=0.0),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("conv.finish_batch")
        def _conv_finish(params: Params) -> Mapping[str, Any]:
            return self.conv.finish_batch(
                params.text("actor", required=False, default="control-room"),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("waste.start")
        def _waste_start(params: Params) -> Mapping[str, Any]:
            return self.waste.start(
                params.text("actor", required=False, default="control-room"),
                drum_level=params.number("drum_level", minimum=0.0, maximum=1.0),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("waste.update")
        def _waste_update(params: Params) -> Mapping[str, Any]:
            return self.waste.update(
                params.text("actor", required=False, default="control-room"),
                drum_level=params.number("drum_level", minimum=0.0, maximum=1.0),
                exhaust_temp_c=params.number("exhaust_temp_c", minimum=0.0),
                tube_leak=params.boolean("tube_leak", required=False, default=False),
                steam_flow_tph=params.number("steam_flow_tph", required=False, default=0.0, minimum=0.0),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("waste.cooldown")
        def _waste_cooldown(params: Params) -> Mapping[str, Any]:
            return self.waste.cooldown(
                params.text("actor", required=False, default="control-room"),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("waste.finish_cooling")
        def _waste_finish_cooling(params: Params) -> Mapping[str, Any]:
            return self.waste.finish_cooling(
                params.text("actor", required=False, default="control-room"),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("waste.reset")
        def _waste_reset(params: Params) -> Mapping[str, Any]:
            return self.waste.reset(
                params.text("actor", required=False, default="control-room"),
                note=params.text("note"),
                drum_level=params.number("drum_level", minimum=0.0, maximum=1.0),
                exhaust_temp_c=params.number("exhaust_temp_c", minimum=0.0),
                tube_leak=params.boolean("tube_leak", required=False, default=False),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("fire.report")
        def _fire_report(params: Params) -> Mapping[str, Any]:
            return self.fire.report(
                params.text("actor", required=False, default="detector-loop"),
                detector_id=params.text("detector_id"),
                zone=params.text("zone"),
                kind=params.text("kind", required=False, default="auto"),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("fire.confirm")
        def _fire_confirm(params: Params) -> Mapping[str, Any]:
            return self.fire.confirm(
                params.text("actor", required=False, default="control-room"),
                zone=params.text("zone"),
                order_id=params.optional_text("order_id"),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("fire.abort")
        def _fire_abort(params: Params) -> Mapping[str, Any]:
            return self.fire.abort(
                params.text("actor", required=False, default="control-room"),
                zone=params.text("zone"),
                reason=params.text("reason"),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        @register("fire.scan")
        def _fire_scan(params: Params) -> Mapping[str, Any]:
            return self.fire.scan(
                params.text("actor", required=False, default="control-system"),
                correlation_id=params.optional_text("correlation_id"),
            )

        @register("fire.reset")
        def _fire_reset(params: Params) -> Mapping[str, Any]:
            return self.fire.reset(
                params.text("actor", required=False, default="control-room"),
                zone=params.text("zone"),
                note=params.text("note"),
                correlation_id=params.optional_text("correlation_id"),
                expected_generation=params.optional_number("expected_generation"),
            )

        return actions

    # ------------------------------------------------------------- 对外接口
    @property
    def actions(self) -> Mapping[str, ActionHandler]:
        return dict(self._actions)

    def component(self, name: str) -> Component:
        try:
            return self._by_name[name]
        except KeyError as exc:
            raise ValidationError("未知组件", details={"component": name}) from exc

    def invoke(self, action: str, params: Mapping[str, Any] | None = None, *, source: str = "api") -> Mapping[str, Any]:
        try:
            handler = self._actions[action]
        except KeyError as exc:
            raise ValidationError("未知动作", details={"action": action, "known": sorted(self._actions)}) from exc
        parsed = params if isinstance(params, Params) else Params(params, source=source)
        return handler(parsed)

    def state(self) -> Mapping[str, Any]:
        service = dict(self.ctx.describe())
        service["version"] = _version()
        return {
            "service": service,
            "heat": self.furnace.heat_report(),
            "components": {component.name: dict(component.snapshot()) for component in self.components},
            "metrics": self.metrics.snapshot(),
        }

    def audit_events(
        self,
        *,
        limit: int = 50,
        since_seq: int = 0,
        action: str | None = None,
        target: str | None = None,
        outcome: str | None = None,
        actor: str | None = None,
    ) -> list[Mapping[str, Any]]:
        events = self.audit.query(
            limit=limit,
            since_seq=since_seq,
            action=action,
            target=target,
            outcome=outcome,
            actor=actor,
        )
        return [event.to_dict() for event in events]

    def verify(self) -> Mapping[str, Any]:
        report = self.store.verify()
        payload = dict(report.to_dict())
        payload["components"] = {component.name: component.status()["state"] for component in self.components}
        payload["audit_length"] = self.audit.length()
        return payload

    def describe_actions(self) -> list[Mapping[str, Any]]:
        return [
            {
                "action": name,
                "component": name.split(".", 1)[0],
                "verb": name.split(".", 1)[1],
                "endpoint": "/api/" + name.replace(".", "/"),
            }
            for name in sorted(self._actions)
        ]


def _version() -> str:
    from . import __version__

    return __version__


def build_application(settings: Settings, *, clock: Clock | None = None) -> Application:
    return Application(settings, clock=clock)


__all__ = ["Application", "build_application", "ActionHandler"]
