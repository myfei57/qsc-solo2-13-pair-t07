"""组件之间的协作接口。

组件不直接互相实例化，而是依赖这里的结构化协议；控制台在组装时把真实组件注入
进去。这样「谁在哪个动作前读谁的落盘记录」在类型上就是显式的。
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable


@runtime_checkable
class BurnerPort(Protocol):
    def durable_stable_record(self) -> tuple[Mapping[str, Any] | None, float]: ...

    def is_latched(self) -> bool: ...

    def status(self) -> Mapping[str, Any]: ...


@runtime_checkable
class FeedPort(Protocol):
    def is_flowing(self) -> bool: ...

    def status(self) -> Mapping[str, Any]: ...


@runtime_checkable
class OxygenPort(Protocol):
    def ensure_established_for_feed(self) -> Mapping[str, Any]: ...

    def status(self) -> Mapping[str, Any]: ...


@runtime_checkable
class WastePort(Protocol):
    def is_latched(self) -> bool: ...

    def status(self) -> Mapping[str, Any]: ...


@runtime_checkable
class SettlerPort(Protocol):
    def settle(self, actor: str, *, heat_id: str, **kwargs: Any) -> Mapping[str, Any]: ...

    def requirements(self) -> Mapping[str, Any]: ...

    def status(self) -> Mapping[str, Any]: ...


@runtime_checkable
class MattePort(Protocol):
    def is_tapping(self) -> bool: ...

    def available_charge(self, ladle_id: str) -> Mapping[str, Any] | None: ...

    def mark_charged(self, ladle_id: str, batch_id: str, tons: float, actor: str) -> None: ...

    def status(self) -> Mapping[str, Any]: ...


@runtime_checkable
class ConverterPort(Protocol):
    def can_accept(self, tons: float) -> bool: ...

    def status(self) -> Mapping[str, Any]: ...


@runtime_checkable
class SlagPort(Protocol):
    def heat_tapped_tons(self, heat_id: str) -> float: ...

    def require_heat_slagged(self, heat_id: str) -> float: ...

    def status(self) -> Mapping[str, Any]: ...


__all__ = [
    "BurnerPort",
    "FeedPort",
    "OxygenPort",
    "WastePort",
    "SettlerPort",
    "MattePort",
    "ConverterPort",
    "SlagPort",
]
