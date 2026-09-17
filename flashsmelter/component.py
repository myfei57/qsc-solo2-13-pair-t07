"""工艺组件基类。

所有组件共享同一套动作包装：校验指令代际 → 执行 → 记录审计与指标。这样门控
失败（顺序错位、闩锁未复位、基线过期）既会抛给调用方，也会在审计流里留痕。
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, ClassVar, Iterator, Mapping

from .errors import ConflictError, FlashSmelterError
from .runtime import RuntimeContext
from .store import Record


@dataclass(slots=True)
class ActionTrace:
    """一次动作的可变跟踪信息，供调用方补充上下文。"""

    action: str
    target: str
    actor: str
    correlation_id: str
    details: dict[str, Any] = field(default_factory=dict)
    record: Record | None = None

    def note(self, key: str, value: Any) -> "ActionTrace":
        self.details[key] = value
        return self

    def attach(self, record: Record | None) -> "ActionTrace":
        self.record = record
        if record is not None:
            self.details.setdefault("key", record.key)
            self.details.setdefault("version", record.version)
        return self


class Component:
    """组件基类：统一动作包装、落盘与状态装载。"""

    name: ClassVar[str] = "component"
    state_key: ClassVar[str] = "state"

    def __init__(self, ctx: RuntimeContext) -> None:
        self.ctx = ctx
        self.settings = ctx.settings
        self.namespace = ctx.namespace
        self.store = ctx.store
        self.clock = ctx.clock
        self.metrics = ctx.metrics
        self.generation = ctx.generation
        self.audit = ctx.audit

    # ------------------------------------------------------------- 关键工具
    def key(self, *parts: str) -> str:
        return self.ctx.key(self.name, *parts)

    def load_state(self) -> Mapping[str, Any] | None:
        record = self.store.get(self.key(self.state_key))
        return None if record is None else record.payload

    def state_record(self) -> Record | None:
        return self.store.get(self.key(self.state_key))

    def persist_state(self, payload: Mapping[str, Any]) -> Record:
        """先落盘、后动作：状态快照必须回读一致才返回。"""

        return self.store.commit_intent(self.key(self.state_key), payload)

    def write_intent(self, action: str, payload: Mapping[str, Any]) -> Record:
        """把「即将执行的动作」单独落盘。

        执行机构动作（开阀、开炮口、复归闩锁）之前写一份意图记录并回读校验，
        失败就绝不动作；事后审计可以把意图与结果对上。
        """

        return self.store.commit_intent(self.key("intent", action), payload)

    @contextmanager
    def action(
        self,
        action: str,
        target: str,
        actor: str,
        *,
        correlation_id: str | None = None,
        expected_generation: int | None = None,
        bump_generation: bool = False,
    ) -> Iterator[ActionTrace]:
        """包装一次工艺动作，统一处理代际校验、审计与指标。"""

        if not actor:
            actor = "anonymous"
        trace = ActionTrace(
            action=action,
            target=target,
            actor=actor,
            correlation_id=correlation_id or uuid.uuid4().hex,
        )
        qualified = f"{self.name}.{action}"
        try:
            self.generation.check(expected_generation, action=qualified)
            yield trace
        except FlashSmelterError as exc:
            outcome = "rejected" if exc.status < 500 else "failed"
            self._record(trace, outcome, exc.code)
            self.metrics.inc(f"{qualified}.{outcome}")
            self.metrics.inc(f"actions.{outcome}")
            raise
        except Exception:
            self._record(trace, "failed", "unhandled-error")
            self.metrics.inc(f"{qualified}.failed")
            self.metrics.inc("actions.failed")
            raise
        else:
            self._record(trace, "ok", "")
            self.metrics.inc(f"{qualified}.ok")
            self.metrics.inc("actions.ok")
            if bump_generation:
                self.generation.bump(qualified)

    def _record(self, trace: ActionTrace, outcome: str, reason: str) -> None:
        details = dict(trace.details)
        if reason:
            details["reason"] = reason
        if self.audit is not None:
            self.audit.record(
                actor=trace.actor,
                action=trace.action,
                target=trace.target,
                outcome=outcome,
                correlation_id=trace.correlation_id,
                details=details,
            )

    # ------------------------------------------------------------- 状态装载
    def restore(self) -> Mapping[str, Any] | None:
        """子类在 ``__init__`` 中调用，把上次落盘状态装回内存。"""

        payload = self.load_state()
        if payload is not None:
            self.metrics.inc(f"{self.name}.restore")
        return payload

    def status(self) -> Mapping[str, Any]:
        raise NotImplementedError

    def snapshot(self) -> dict[str, Any]:
        record = self.state_record()
        return {
            "component": self.name,
            "zone": self.namespace.zone(self.name),
            "state": dict(self.status()),
            "persisted_version": None if record is None else record.version,
            "persisted_at": None if record is None else record.written_at,
        }


def ensure_actor(actor: str | None) -> str:
    """操作者缺省时补一个可审计的占位值。"""

    return (actor or "anonymous").strip() or "anonymous"


def require_generation_match(actual: int, expected: int | None, *, action: str) -> None:
    if expected is not None and expected != actual:
        raise ConflictError(
            "指令代际与当前状态不一致",
            details={"action": action, "expected": expected, "current": actual},
        )


__all__ = ["Component", "ActionTrace", "ensure_actor", "require_generation_match"]
