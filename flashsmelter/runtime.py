"""运行期基础设施：时钟、指标、代际与上下文。"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from .config import Settings
from .errors import ConflictError, ValidationError
from .ns import Namespace
from .store import DurableStore

_ISO_FORMAT = "%Y-%m-%dT%H:%M:%S.%f%z"


def iso_from_epoch(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def epoch_from_iso(text: str) -> float:
    if not isinstance(text, str) or not text:
        raise ValidationError("时间戳必须是非空字符串", details={"value": repr(text)})
    normalized = text.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+0000"
    try:
        parsed = datetime.strptime(normalized, _ISO_FORMAT)
    except ValueError as exc:
        raise ValidationError("时间戳格式不合法", details={"value": text, "expected": "ISO-8601"}) from exc
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()


class Clock:
    """系统时钟；测试用 :class:`ManualClock` 替换即可复现基线过期等时间敏感场景。"""

    def timestamp(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    def timestamp_iso(self) -> str:
        return iso_from_epoch(self.timestamp())


class ManualClock(Clock):
    """可手动推进的时钟，供测试与离线回放使用。"""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self._epoch = float(start)

    def advance(self, seconds: float) -> float:
        if seconds < 0:
            raise ValidationError("时钟只能向前推进", details={"seconds": seconds})
        self._epoch += float(seconds)
        return self._epoch

    def set(self, epoch: float) -> float:
        self._epoch = float(epoch)
        return self._epoch

    def timestamp(self) -> float:
        return self._epoch

    def monotonic(self) -> float:
        return self._epoch


class Metrics:
    """轻量计数与瞬时量，供 ``/api/metrics`` 与自检输出。"""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._counters: dict[str, int] = {}
        self._gauges: dict[str, float] = {}
        self._lock = threading.Lock()
        self._started = clock.monotonic()

    def inc(self, name: str, value: int = 1) -> int:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + value
            return self._counters[name]

    def observe(self, name: str, value: float) -> float:
        with self._lock:
            self._gauges[name] = float(value)
            return self._gauges[name]

    def counter(self, name: str) -> int:
        with self._lock:
            return self._counters.get(name, 0)

    def gauge(self, name: str) -> float | None:
        with self._lock:
            return self._gauges.get(name)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counters = dict(sorted(self._counters.items()))
            gauges = dict(sorted(self._gauges.items()))
        return {
            "uptime_seconds": round(self._clock.monotonic() - self._started, 3),
            "counters": counters,
            "gauges": gauges,
        }


class Generation:
    """全局代际。

    控制台的每条控制指令都可以带上它读到的代际；代际不一致说明另一路操作已经
    改过状态，指令必须作废而不是按旧假设执行——这正是「命令冲突」的判定口径。
    """

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._value = 0
        self._reason = "init"
        self._updated_at = clock.timestamp_iso()

    @property
    def value(self) -> int:
        return self._value

    def bump(self, reason: str) -> int:
        if not reason:
            raise ValidationError("代际推进必须给出原因")
        self._value += 1
        self._reason = reason
        self._updated_at = self._clock.timestamp_iso()
        return self._value

    def check(self, expected: int | None, *, action: str) -> None:
        if expected is None:
            return
        if expected != self._value:
            raise ConflictError(
                "指令代际与当前状态不一致，请重新读取状态后再下发",
                details={"action": action, "expected": expected, "current": self._value},
            )

    def to_dict(self) -> dict[str, Any]:
        return {"value": self._value, "reason": self._reason, "updated_at": self._updated_at}


@dataclass(slots=True)
class RuntimeContext:
    """组件共享的运行上下文，由控制台在启动时组装。"""

    settings: Settings
    namespace: Namespace
    store: DurableStore
    clock: Clock
    metrics: Metrics
    generation: Generation
    audit: Any = None
    extras: dict[str, Any] = field(default_factory=dict)

    def key(self, *parts: str) -> str:
        return self.namespace.key(*parts)

    def describe(self) -> Mapping[str, Any]:
        return {
            "namespace": self.namespace.as_dict(),
            "generation": self.generation.to_dict(),
            "root": str(self.settings.root),
        }


__all__ = [
    "Clock",
    "ManualClock",
    "Metrics",
    "Generation",
    "RuntimeContext",
    "iso_from_epoch",
    "epoch_from_iso",
]
