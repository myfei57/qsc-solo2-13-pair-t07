"""落盘编码：规范化 JSON、校验和与键名校验。"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

from ..errors import ValidationError

_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
MAX_SEGMENTS = 8


def canonical_json(payload: Any) -> str:
    """稳定序列化：排序键、压缩空白、禁止 NaN。"""

    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def checksum_of(version: int, written_at: str, payload: Any) -> str:
    digest = hashlib.sha256()
    digest.update(str(version).encode("utf-8"))
    digest.update(b"\x1f")
    digest.update(written_at.encode("utf-8"))
    digest.update(b"\x1f")
    digest.update(canonical_json(payload).encode("utf-8"))
    return digest.hexdigest()


def validate_key(key: str) -> list[str]:
    """把 ``a/b/c`` 形式的键切成安全路径段。"""

    if not isinstance(key, str) or not key:
        raise ValidationError("落盘键必须是非空字符串", details={"key": repr(key)})
    segments = key.split("/")
    if len(segments) > MAX_SEGMENTS:
        raise ValidationError("落盘键层级过深", details={"key": key, "segments": len(segments)})
    for segment in segments:
        if not _SEGMENT_PATTERN.match(segment):
            raise ValidationError("落盘键段不合法", details={"key": key, "segment": segment})
    return segments


def stringify_mapping(payload: Mapping[str, Any]) -> dict[str, Any]:
    """浅拷贝成普通 dict，拒绝非映射输入。"""

    if not isinstance(payload, Mapping):
        raise ValidationError("落盘内容必须是映射", details={"type": type(payload).__name__})
    return {str(key): value for key, value in payload.items()}


__all__ = ["canonical_json", "checksum_of", "validate_key", "stringify_mapping", "MAX_SEGMENTS"]
