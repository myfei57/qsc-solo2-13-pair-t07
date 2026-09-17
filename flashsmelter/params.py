"""控制台与 CLI 共用的参数解析。"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .errors import ValidationError

_TRUE_LITERALS = {"true", "1", "yes", "on", "y"}
_FALSE_LITERALS = {"false", "0", "no", "off", "n"}


class Params:
    """把原始请求参数收敛成有类型的取值。"""

    def __init__(self, raw: Mapping[str, Any] | None = None, *, source: str = "request") -> None:
        if raw is None:
            raw = {}
        if not isinstance(raw, Mapping):
            raise ValidationError("参数必须是映射", details={"source": source, "type": type(raw).__name__})
        self._raw = {str(key): value for key, value in raw.items()}
        self._source = source
        self._consumed: set[str] = set()

    @property
    def raw(self) -> Mapping[str, Any]:
        return dict(self._raw)

    def keys(self) -> Iterable[str]:
        return tuple(self._raw.keys())

    def has(self, name: str) -> bool:
        return name in self._raw and self._raw[name] not in (None, "")

    def text(
        self,
        name: str,
        *,
        required: bool = True,
        default: str | None = None,
        max_length: int = 160,
    ) -> str:
        self._consumed.add(name)
        value = self._raw.get(name, default)
        if value is None or value == "":
            if required:
                raise ValidationError(f"缺少参数 {name}", details={"source": self._source, "param": name})
            return "" if default is None else default
        text = str(value).strip()
        if not text and required:
            raise ValidationError(f"参数 {name} 不能为空", details={"source": self._source, "param": name})
        if len(text) > max_length:
            raise ValidationError(
                f"参数 {name} 超长",
                details={"source": self._source, "param": name, "length": len(text), "max": max_length},
            )
        return text

    def number(
        self,
        name: str,
        *,
        required: bool = True,
        default: float | None = None,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> float:
        self._consumed.add(name)
        value = self._raw.get(name, default)
        if value is None or value == "":
            if required:
                raise ValidationError(f"缺少参数 {name}", details={"source": self._source, "param": name})
            if default is None:
                raise ValidationError(
                    f"参数 {name} 既非必填也无默认值", details={"source": self._source, "param": name}
                )
            value = default
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                f"参数 {name} 必须是数值",
                details={"source": self._source, "param": name, "value": repr(value)},
            ) from exc
        if number != number:  # NaN
            raise ValidationError(f"参数 {name} 不能是 NaN", details={"param": name})
        if minimum is not None and number < minimum:
            raise ValidationError(
                f"参数 {name} 低于下限",
                details={"param": name, "value": number, "minimum": minimum},
            )
        if maximum is not None and number > maximum:
            raise ValidationError(
                f"参数 {name} 超过上限",
                details={"param": name, "value": number, "maximum": maximum},
            )
        return number

    def integer(
        self,
        name: str,
        *,
        required: bool = True,
        default: int | None = None,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int:
        raw = self.number(
            name,
            required=required,
            default=None if default is None else float(default),
            minimum=None if minimum is None else float(minimum),
            maximum=None if maximum is None else float(maximum),
        )
        if abs(raw - round(raw)) > 1e-9:
            raise ValidationError(f"参数 {name} 必须是整数", details={"param": name, "value": raw})
        return int(round(raw))

    def boolean(self, name: str, *, required: bool = True, default: bool | None = None) -> bool:
        self._consumed.add(name)
        value = self._raw.get(name, default)
        if isinstance(value, bool):
            return value
        if value is None or value == "":
            if required:
                raise ValidationError(f"缺少参数 {name}", details={"source": self._source, "param": name})
            assert default is not None
            return default
        text = str(value).strip().lower()
        if text in _TRUE_LITERALS:
            return True
        if text in _FALSE_LITERALS:
            return False
        raise ValidationError(
            f"参数 {name} 不是布尔取值",
            details={"source": self._source, "param": name, "value": str(value)},
        )

    def optional_number(self, name: str, **kwargs: Any) -> float | None:
        if not self.has(name):
            self._consumed.add(name)
            return None
        return self.number(name, **kwargs)

    def optional_text(self, name: str, **kwargs: Any) -> str | None:
        if not self.has(name):
            self._consumed.add(name)
            return None
        return self.text(name, **kwargs)

    def mapping(self, name: str, *, required: bool = False) -> dict[str, Any]:
        self._consumed.add(name)
        value = self._raw.get(name)
        if value is None:
            if required:
                raise ValidationError(f"缺少参数 {name}", details={"param": name})
            return {}
        if not isinstance(value, Mapping):
            raise ValidationError(
                f"参数 {name} 必须是对象",
                details={"param": name, "type": type(value).__name__},
            )
        return {str(key): item for key, item in value.items()}

    def reject_unknown(self, allowed: Iterable[str]) -> None:
        permitted = set(allowed)
        unknown = sorted(set(self._raw) - permitted)
        if unknown:
            raise ValidationError(
                "存在未支持的参数",
                details={"source": self._source, "unknown": unknown, "allowed": sorted(permitted)},
            )

    def as_dict(self) -> dict[str, Any]:
        return dict(self._raw)


__all__ = ["Params"]
