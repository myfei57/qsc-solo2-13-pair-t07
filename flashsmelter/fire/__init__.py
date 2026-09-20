"""消防联动组件。

就地报警器只能「响了才知道」，雨淋阀、消防泵还得人跑过去开。这个组件把
「探测点报警 → 分区确认 → 联动执行 → 处置复位」串成闭环：

1. 探测点（感烟/感温等自动探测器、手动报警按钮）按防火分区登记、上报火警；
2. 同一分区内两只自动探测器、或任意一只手动按钮报警，构成确认火警，
   生成联动单并进入确认窗口——执行联动前必须有人明确「是哪个区」；
3. 确认窗口内可以按误报人工中止；窗口到期且处于自动模式时，由扫描动作
   自动执行；执行顺序固定为 切非消防电源 → 开雨淋阀 → 起消防泵；
4. 执行机构动作之前先写联动意图并回读校验，落盘失败绝不动作；
5. 处置完毕（火灭或确认误报）后复位分区：报警清除、阀关、泵停、电源恢复。

每一次报警、确认、中止、执行与复位都进审计流水，联动单随状态落盘，事后
可以逐条对上「谁、什么时候、对哪个区、做了什么」。
"""

from __future__ import annotations

from typing import Any, Mapping

from ..component import Component, ensure_actor
from ..errors import GuardViolation, ValidationError
from ..machine import StateMachine
from ..runtime import RuntimeContext
from ..store import Record

DETECTOR_KINDS = ("auto", "manual")
ZONE_STATES = ("normal", "prealarm", "confirmed", "dispatched")

TRANSITIONS: Mapping[str, tuple[str, ...]] = {
    "normal": ("prealarm", "confirmed"),
    "prealarm": ("confirmed", "normal"),
    "confirmed": ("dispatched", "normal"),
    "dispatched": ("normal",),
}

ORDER_PENDING = "pending"
ORDER_EXECUTED = "executed"
ORDER_ABORTED = "aborted"

# 联动执行顺序固定：先切非消防电源，再开雨淋阀，最后起消防泵。
EXECUTION_STEPS = ("cut_non_fire_power", "open_deluge_valve", "start_fire_pumps")

ORDER_HISTORY_LIMIT = 50


class FireLinkage(Component):
    """按防火分区组织的消防联动：探测定级、分区确认、联动执行、处置复位。"""

    name = "fire"

    def __init__(self, ctx: RuntimeContext) -> None:
        super().__init__(ctx)
        self._zones: tuple[str, ...] = tuple(
            zone.strip() for zone in self.settings.fire_zones.split(",") if zone.strip()
        )
        self._pump_ids: tuple[str, ...] = tuple(
            pump.strip() for pump in self.settings.fire_pumps.split(",") if pump.strip()
        )
        self._machines = {
            zone: StateMachine(f"fire:{zone}", "normal", TRANSITIONS, ctx.clock) for zone in self._zones
        }
        self._detectors: dict[str, dict[str, Any]] = {}
        self._devices: dict[str, dict[str, str]] = {
            zone: {"deluge_valve": "closed", "non_fire_power": "normal"} for zone in self._zones
        }
        self._pumps: dict[str, str] = {pump: "standby" for pump in self._pump_ids}
        self._orders: list[dict[str, Any]] = []
        self._order_seq = 0
        restored = self.restore()
        if restored is not None:
            machines = restored.get("machines", {})
            if isinstance(machines, Mapping):
                for zone, payload in machines.items():
                    if zone in self._machines and isinstance(payload, Mapping):
                        self._machines[zone].restore(payload)
            detectors = restored.get("detectors", {})
            if isinstance(detectors, Mapping):
                self._detectors = {
                    str(detector_id): dict(payload)
                    for detector_id, payload in detectors.items()
                    if isinstance(payload, Mapping)
                }
            devices = restored.get("devices", {})
            if isinstance(devices, Mapping):
                for zone, payload in devices.items():
                    if zone in self._devices and isinstance(payload, Mapping):
                        self._devices[zone].update({str(key): str(value) for key, value in payload.items()})
            pumps = restored.get("pumps", {})
            if isinstance(pumps, Mapping):
                for pump, state in pumps.items():
                    if pump in self._pumps:
                        self._pumps[pump] = str(state)
            orders = restored.get("orders", [])
            if isinstance(orders, list):
                self._orders = [dict(order) for order in orders if isinstance(order, Mapping)]
            self._order_seq = int(restored.get("order_seq", len(self._orders)))
        self._refresh_gauges()

    # ------------------------------------------------------------------ 报警
    def report(
        self,
        actor: str,
        *,
        detector_id: str,
        zone: str,
        kind: str = "auto",
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        """探测点上报火警；达到确认条件时生成联动单并进入确认窗口。"""

        actor = ensure_actor(actor)
        with self.action(
            "report",
            f"fire:{zone}",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            self._require_zone(zone)
            if kind not in DETECTOR_KINDS:
                raise ValidationError(
                    "探测器类型不合法", details={"kind": kind, "allowed": list(DETECTOR_KINDS)}
                )
            existing = self._detectors.get(detector_id)
            if existing is not None and existing["zone"] != zone:
                raise ValidationError(
                    "探测器登记分区与上报分区不一致",
                    details={
                        "detector_id": detector_id,
                        "registered_zone": existing["zone"],
                        "reported_zone": zone,
                    },
                )
            if existing is not None and existing.get("active"):
                # 探测器复报不改变状态，只留一条审计，避免重复报警刷出多张联动单。
                trace.note("already_active", True).note("zone", zone)
                return self.status()
            self._detectors[detector_id] = {
                "zone": zone,
                "kind": kind,
                "active": True,
                "reported_at": self.clock.timestamp_iso(),
            }
            level = self._zone_level(zone)
            machine = self._machines[zone]
            if machine.state == "normal":
                machine.to(
                    "prealarm" if level == "prealarm" else "confirmed",
                    actor,
                    f"探测点 {detector_id} 报警",
                )
            elif machine.state == "prealarm" and level == "confirmed":
                machine.to("confirmed", actor, f"探测点 {detector_id} 报警，构成确认火警")
            order_id = None
            if machine.state == "confirmed":
                order = self._pending_order(zone)
                if order is None:
                    order = self._create_order(zone)
                if detector_id not in order["triggers"]:
                    order["triggers"].append(detector_id)
                order_id = order["order_id"]
            record = self._persist(reason="report")
            trace.attach(record).note("zone", zone).note("level", level).note("detector_id", detector_id)
            if order_id is not None:
                trace.note("order_id", order_id)
            return self.status()

    # ------------------------------------------------------------------ 确认
    def confirm(
        self,
        actor: str,
        *,
        zone: str,
        order_id: str | None = None,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        """人工确认分区并立即执行联动；分区必须与待确认联动单一致。"""

        actor = ensure_actor(actor)
        with self.action(
            "confirm",
            f"fire:{zone}",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            self._require_zone(zone)
            machine = self._machines[zone]
            machine.require("confirmed", "联动确认")
            order = self._pending_order(zone)
            if order is None:
                raise GuardViolation("分区没有待确认的联动单", details={"zone": zone})
            if order_id is not None and order_id != order["order_id"]:
                raise ValidationError(
                    "联动单号与分区待确认单不一致",
                    details={"zone": zone, "expected": order["order_id"], "given": order_id},
                )
            self._execute(order, decided_by=actor, via="manual-confirm")
            machine.to("dispatched", actor, "人工确认分区，执行联动")
            record = self._persist(reason="confirm")
            trace.attach(record).note("zone", zone).note("order_id", order["order_id"]).note(
                "steps", [step["step"] for step in order["steps"]]
            )
            return self.status()

    # ------------------------------------------------------------------ 中止
    def abort(
        self,
        actor: str,
        *,
        zone: str,
        reason: str,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        """误报中止：撤销待确认联动单、清除分区报警，设备不动作。"""

        actor = ensure_actor(actor)
        with self.action(
            "abort",
            f"fire:{zone}",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            self._require_zone(zone)
            if not reason:
                raise GuardViolation("误报中止必须填写原因")
            machine = self._machines[zone]
            machine.require("confirmed", "误报中止")
            order = self._pending_order(zone)
            if order is None:
                raise GuardViolation("分区没有待确认的联动单", details={"zone": zone})
            order["state"] = ORDER_ABORTED
            order["abort_reason"] = reason
            order["decided_by"] = actor
            order["decided_at"] = self.clock.timestamp_iso()
            machine.to("normal", actor, f"误报中止：{reason}")
            cleared = self._clear_zone_alarms(zone)
            record = self._persist(reason="abort")
            trace.attach(record).note("zone", zone).note("order_id", order["order_id"]).note(
                "cleared_alarms", cleared
            )
            return self.status()

    # ------------------------------------------------------------------ 扫描
    def scan(self, actor: str, *, correlation_id: str | None = None) -> Mapping[str, Any]:
        """控制系统周期扫描：确认窗口到期的联动单按模式自动执行或挂起催办。"""

        actor = ensure_actor(actor)
        with self.action("scan", "fire", actor, correlation_id=correlation_id) as trace:
            now = self.clock.timestamp()
            executed: list[str] = []
            awaiting: list[str] = []
            changed = False
            for zone in self._zones:
                order = self._pending_order(zone)
                if order is None or now < float(order["deadline_epoch"]):
                    continue
                if self.settings.fire_auto_execute:
                    self._execute(order, decided_by=actor, via="auto-scan")
                    self._machines[zone].to("dispatched", actor, "确认窗口到期，自动执行联动")
                    executed.append(order["order_id"])
                    changed = True
                else:
                    if order.get("note") != "awaiting-manual-confirm":
                        order["note"] = "awaiting-manual-confirm"
                        changed = True
                    awaiting.append(order["order_id"])
            record = self._persist(reason="scan") if changed else None
            trace.attach(record).note("executed", executed).note("awaiting_manual", awaiting)
            return {
                "executed": executed,
                "awaiting_manual": awaiting,
                "pending": [order["order_id"] for order in self._orders if order["state"] == ORDER_PENDING],
                "zones": {zone: self._machines[zone].state for zone in self._zones},
            }

    # ------------------------------------------------------------------ 复位
    def reset(
        self,
        actor: str,
        *,
        zone: str,
        note: str,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
    ) -> Mapping[str, Any]:
        """处置完成后复位分区：清报警、关阀、恢复供电；无区联动时停泵。"""

        actor = ensure_actor(actor)
        with self.action(
            "reset",
            f"fire:{zone}",
            actor,
            correlation_id=correlation_id,
            expected_generation=expected_generation,
        ) as trace:
            self._require_zone(zone)
            if not note:
                raise GuardViolation("复位必须填写处置说明")
            machine = self._machines[zone]
            machine.require_one_of(("prealarm", "dispatched"), "复位")
            was_dispatched = machine.state == "dispatched"
            cleared = self._clear_zone_alarms(zone)
            if was_dispatched:
                self._devices[zone]["deluge_valve"] = "closed"
                self._devices[zone]["non_fire_power"] = "normal"
            machine.to("normal", actor, f"处置复位：{note}")
            if was_dispatched and not self._any_zone_dispatched():
                for pump in self._pumps:
                    self._pumps[pump] = "standby"
            record = self._persist(reason="reset")
            trace.attach(record).note("zone", zone).note("note", note).note("cleared_alarms", cleared)
            return self.status()

    # ------------------------------------------------------------------ 查询
    def status(self) -> Mapping[str, Any]:
        zones: dict[str, Any] = {}
        for zone in self._zones:
            order = self._pending_order(zone)
            zones[zone] = {
                "state": self._machines[zone].state,
                "level": self._zone_level(zone),
                "active_alarms": self._active_alarms(zone),
                "pending_order": None if order is None else order["order_id"],
            }
        return {
            "state": self._aggregate_state(),
            "mode": "auto" if self.settings.fire_auto_execute else "manual",
            "confirm_window_seconds": self.settings.fire_confirm_window_seconds,
            "zones": zones,
            "devices": {zone: dict(device) for zone, device in self._devices.items()},
            "pumps": dict(self._pumps),
            "detectors": {detector_id: dict(payload) for detector_id, payload in self._detectors.items()},
            "orders": [dict(order) for order in self._orders],
            "pending_count": sum(1 for order in self._orders if order["state"] == ORDER_PENDING),
        }

    # ------------------------------------------------------------------ 执行
    def _execute(self, order: dict[str, Any], *, decided_by: str, via: str) -> None:
        """按固定顺序执行联动：切非消防电源 → 开雨淋阀 → 起消防泵。

        意图先落盘并回读校验，失败就绝不动作。现场侧这里对应联动控制器
        输出回路（PLC/Modbus 线圈），本组件把每一步结果做成可审计的落盘
        状态，接上真实输出时只需替换状态翻转这一段。
        """

        zone = order["zone"]
        intent = self.write_intent(
            f"linkage-{order['order_id']}",
            {
                "order_id": order["order_id"],
                "zone": zone,
                "via": via,
                "steps": list(EXECUTION_STEPS),
                "pumps": list(self._pump_ids),
                "at": self.clock.timestamp_iso(),
            },
        )
        steps: list[dict[str, Any]] = order["steps"]
        self._devices[zone]["non_fire_power"] = "cut"
        steps.append(self._step_record("cut_non_fire_power", zone))
        self._devices[zone]["deluge_valve"] = "open"
        steps.append(self._step_record("open_deluge_valve", zone))
        for pump in self._pump_ids:
            self._pumps[pump] = "running"
        steps.append(self._step_record("start_fire_pumps", ",".join(self._pump_ids)))
        order["state"] = ORDER_EXECUTED
        order["decided_by"] = decided_by
        order["decided_at"] = self.clock.timestamp_iso()
        order["via"] = via
        order["intent_key"] = intent.key

    def _step_record(self, step: str, target: str) -> dict[str, Any]:
        return {"step": step, "target": target, "result": "done", "at": self.clock.timestamp_iso()}

    # ------------------------------------------------------------------ 内部
    def _require_zone(self, zone: str) -> None:
        if zone not in self._zones:
            raise ValidationError("未知防火分区", details={"zone": zone, "known": list(self._zones)})

    def _zone_level(self, zone: str) -> str:
        auto = 0
        for detector in self._detectors.values():
            if detector["zone"] != zone or not detector.get("active"):
                continue
            if detector["kind"] == "manual":
                return "confirmed"
            auto += 1
        if auto >= 2:
            return "confirmed"
        if auto == 1:
            return "prealarm"
        return "none"

    def _active_alarms(self, zone: str) -> list[str]:
        return sorted(
            detector_id
            for detector_id, detector in self._detectors.items()
            if detector["zone"] == zone and detector.get("active")
        )

    def _pending_order(self, zone: str) -> dict[str, Any] | None:
        for order in reversed(self._orders):
            if order["zone"] == zone and order["state"] == ORDER_PENDING:
                return order
        return None

    def _create_order(self, zone: str) -> dict[str, Any]:
        self._order_seq += 1
        order = {
            "order_id": f"LD-{self._order_seq:04d}",
            "zone": zone,
            "level": "confirmed",
            "state": ORDER_PENDING,
            "triggers": self._active_alarms(zone),
            "created_at": self.clock.timestamp_iso(),
            "deadline_epoch": round(self.clock.timestamp() + self.settings.fire_confirm_window_seconds, 3),
            "decided_by": None,
            "decided_at": None,
            "via": None,
            "steps": [],
            "abort_reason": None,
            "note": None,
        }
        self._orders.append(order)
        del self._orders[:-ORDER_HISTORY_LIMIT]
        return order

    def _clear_zone_alarms(self, zone: str) -> list[str]:
        cleared = []
        for detector_id, detector in self._detectors.items():
            if detector["zone"] == zone and detector.get("active"):
                detector["active"] = False
                cleared.append(detector_id)
        return sorted(cleared)

    def _any_zone_dispatched(self) -> bool:
        return any(machine.state == "dispatched" for machine in self._machines.values())

    def _aggregate_state(self) -> str:
        """全厂消防总态：取各分区中最紧急的一档，供控制台与自检汇总。"""

        states = {machine.state for machine in self._machines.values()}
        for candidate in ("dispatched", "confirmed", "prealarm"):
            if candidate in states:
                return candidate
        return "normal"

    def _persist(self, *, reason: str) -> Record:
        payload = {
            "reason": reason,
            "written_epoch": self.clock.timestamp(),
            "written_at": self.clock.timestamp_iso(),
            "machines": {zone: machine.to_dict() for zone, machine in self._machines.items()},
            "detectors": {detector_id: dict(detector) for detector_id, detector in self._detectors.items()},
            "devices": {zone: dict(device) for zone, device in self._devices.items()},
            "pumps": dict(self._pumps),
            "orders": [dict(order) for order in self._orders],
            "order_seq": self._order_seq,
        }
        record = self.persist_state(payload)
        self._refresh_gauges()
        return record

    def _refresh_gauges(self) -> None:
        for zone, machine in self._machines.items():
            self.metrics.observe(f"fire.zone.{zone}.state_code", float(ZONE_STATES.index(machine.state)))
        pending = sum(1 for order in self._orders if order["state"] == ORDER_PENDING)
        self.metrics.observe("fire.pending_orders", float(pending))


__all__ = [
    "FireLinkage",
    "DETECTOR_KINDS",
    "ZONE_STATES",
    "TRANSITIONS",
    "EXECUTION_STEPS",
    "ORDER_PENDING",
    "ORDER_EXECUTED",
    "ORDER_ABORTED",
]
