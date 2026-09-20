"""冶炼命名空间。

一条产线上的每台闪速炉、每套转炉都挂在 ``site/unit`` 两级命名空间下；所有
落盘键、审计事件与控制台请求都带命名空间，多炉并行运行时状态互不串扰。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterator, Mapping

from ..errors import ValidationError

_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
_MAX_KEY_LENGTH = 96

# 组件 → 工位分区：控制台按分区聚合成一个页面，便于值班人员按工段查看。
ZONE_CATALOG: Mapping[str, str] = {
    "furnace": "reactor",
    "burner": "reactor",
    "conc": "reactor",
    "oxygen": "gas",
    "settler": "settler",
    "slag": "settler",
    "matte": "settler",
    "conv": "converter",
    "waste": "offgas",
    "fire": "control",
    "audit": "control",
    "store": "control",
    "console": "control",
}


@dataclass(frozen=True, slots=True)
class Namespace:
    """``site/unit`` 两级命名空间。"""

    site: str
    unit: str

    @classmethod
    def parse(cls, text: str) -> "Namespace":
        if not isinstance(text, str):
            raise ValidationError("命名空间必须是字符串", details={"value": repr(text)})
        parts = [part for part in text.strip().split("/") if part]
        if len(parts) != 2:
            raise ValidationError(
                "命名空间必须形如 site/unit",
                details={"value": text, "expected": "site/unit"},
            )
        site, unit = parts
        for label, token in (("site", site), ("unit", unit)):
            if not _TOKEN_PATTERN.match(token):
                raise ValidationError(
                    f"命名空间 {label} 段不合法",
                    details={"label": label, "value": token},
                )
        return cls(site=site, unit=unit)

    def __str__(self) -> str:
        return f"{self.site}/{self.unit}"

    @property
    def prefix(self) -> str:
        return f"{self.site}/{self.unit}"

    def key(self, *parts: str) -> str:
        """拼出带命名空间的落盘键：``site/unit/part/part``。"""

        if not parts:
            raise ValidationError("落盘键至少需要一个段")
        for part in parts:
            if not isinstance(part, str) or not part:
                raise ValidationError("落盘键段必须是非空字符串", details={"part": repr(part)})
        key = "/".join((self.site, self.unit, *parts))
        if len(key) > _MAX_KEY_LENGTH:
            raise ValidationError("落盘键过长", details={"key": key, "length": len(key)})
        return key

    def matches(self, key: str) -> bool:
        return key == self.prefix or key.startswith(self.prefix + "/")

    def strip(self, key: str) -> str:
        if not self.matches(key):
            raise ValidationError("键不属于当前命名空间", details={"key": key, "namespace": self.prefix})
        return key[len(self.prefix) :].lstrip("/")

    def zone(self, component: str) -> str:
        try:
            return ZONE_CATALOG[component]
        except KeyError as exc:  # pragma: no cover - 仅防御内部错拼
            raise ValidationError("未知组件", details={"component": component}) from exc

    def iter_zones(self) -> Iterator[tuple[str, str]]:
        for component, zone in sorted(ZONE_CATALOG.items()):
            yield component, zone

    def as_dict(self) -> dict[str, Any]:
        return {"site": self.site, "unit": self.unit, "prefix": self.prefix}


def validate_component(component: str) -> str:
    """控制台/CLI 传入的组件名白名单校验。"""

    if component not in ZONE_CATALOG:
        raise ValidationError("未知组件", details={"component": component})
    return component


__all__ = ["Namespace", "ZONE_CATALOG", "validate_component"]
