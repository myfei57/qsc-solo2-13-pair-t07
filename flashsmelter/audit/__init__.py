"""冶炼审计。

审计流是追加型 JSONL，一条记录一次动作尝试：动作名、目标、操作者、结果、原因与
代际。拒绝类动作同样入库，便于事后还原「谁在什么时候被哪条联锁挡住」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..errors import ValidationError
from ..ns import Namespace
from ..runtime import Clock
from ..store import DurableStore, JournalEntry

AUDIT_STREAM = "audit/events"

OUTCOMES = ("ok", "rejected", "failed")


@dataclass(frozen=True, slots=True)
class AuditEvent:
    seq: int
    at: str
    namespace: str
    actor: str
    action: str
    target: str
    outcome: str
    correlation_id: str
    details: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "at": self.at,
            "namespace": self.namespace,
            "actor": self.actor,
            "action": self.action,
            "target": self.target,
            "outcome": self.outcome,
            "correlation_id": self.correlation_id,
            "details": dict(self.details),
        }


class AuditLog:
    """审计流水读写。"""

    def __init__(self, store: DurableStore, namespace: Namespace, clock: Clock) -> None:
        self._store = store
        self._namespace = namespace
        self._clock = clock

    def record(
        self,
        *,
        actor: str,
        action: str,
        target: str,
        outcome: str,
        correlation_id: str,
        details: Mapping[str, Any] | None = None,
    ) -> AuditEvent:
        if outcome not in OUTCOMES:
            raise ValidationError("非法的审计结果", details={"outcome": outcome, "allowed": list(OUTCOMES)})
        payload = {
            "at": self._clock.timestamp_iso(),
            "namespace": self._namespace.prefix,
            "actor": actor or "unknown",
            "action": action,
            "target": target,
            "outcome": outcome,
            "correlation_id": correlation_id,
            "details": dict(details or {}),
        }
        entry = self._store.append(AUDIT_STREAM, payload)
        return self._to_event(entry)

    def query(
        self,
        *,
        limit: int = 50,
        since_seq: int = 0,
        action: str | None = None,
        target: str | None = None,
        outcome: str | None = None,
        actor: str | None = None,
    ) -> list[AuditEvent]:
        if limit < 1:
            raise ValidationError("limit 必须为正", details={"limit": limit})
        entries = self._store.read_stream(AUDIT_STREAM, limit=max(limit * 8, 200), since_seq=since_seq)
        events = [self._to_event(entry) for entry in entries]
        filtered = [
            event
            for event in events
            if (action is None or event.action == action)
            and (target is None or event.target == target)
            and (outcome is None or event.outcome == outcome)
            and (actor is None or event.actor == actor)
        ]
        return filtered[-limit:]

    def length(self) -> int:
        return self._store.stream_length(AUDIT_STREAM)

    def stats(self) -> dict[str, Any]:
        events = self.query(limit=1000)
        by_outcome: dict[str, int] = {outcome: 0 for outcome in OUTCOMES}
        by_actor: dict[str, int] = {}
        for event in events:
            by_outcome[event.outcome] = by_outcome.get(event.outcome, 0) + 1
            by_actor[event.actor] = by_actor.get(event.actor, 0) + 1
        return {
            "total": self.length(),
            "sampled": len(events),
            "by_outcome": by_outcome,
            "by_actor": dict(sorted(by_actor.items())),
            "last_event_at": events[-1].at if events else None,
        }

    def _to_event(self, entry: JournalEntry) -> AuditEvent:
        payload = entry.payload
        return AuditEvent(
            seq=entry.seq,
            at=str(payload.get("at", entry.written_at)),
            namespace=str(payload.get("namespace", self._namespace.prefix)),
            actor=str(payload.get("actor", "unknown")),
            action=str(payload.get("action", "")),
            target=str(payload.get("target", "")),
            outcome=str(payload.get("outcome", "ok")),
            correlation_id=str(payload.get("correlation_id", "")),
            details=payload.get("details", {}) or {},
        )


__all__ = ["AuditLog", "AuditEvent", "AUDIT_STREAM", "OUTCOMES"]
