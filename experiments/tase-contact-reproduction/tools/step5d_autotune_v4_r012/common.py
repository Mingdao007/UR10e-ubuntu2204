"""Small deterministic helpers shared by the offline R011 primitives."""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


class R012ValueError(ValueError):
    """A value is outside the R011 typed contract."""


def json_tree(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): json_tree(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_tree(item) for item in value]
    return copy.deepcopy(value)


def freeze_tree(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): freeze_tree(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze_tree(item) for item in value)
    return value

def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            json_tree(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise R012ValueError(f"value is not canonical JSON: {exc}") from exc


def finite(value: Any, role: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise R012ValueError(f"{role} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise R012ValueError(f"{role} must be finite")
    return number


def positive(value: Any, role: str) -> float:
    number = finite(value, role)
    if number <= 0.0:
        raise R012ValueError(f"{role} must be positive")
    return number


def strict_json_object(path: Path, role: str) -> dict[str, Any]:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise R012ValueError(f"{role} must be a regular file: {path}")

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise R012ValueError(f"{role} repeats key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=unique_pairs,
            parse_constant=lambda token: (_ for _ in ()).throw(
                R012ValueError(f"{role} contains {token}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise R012ValueError(f"{role} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise R012ValueError(f"{role} must be an object")
    return value
