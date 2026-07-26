"""Strict I-JSON loading and RFC 8785-style canonical SHA256 identities.

Identity inputs reject duplicate keys, non-finite values, lone surrogates,
integers outside the interoperable IEEE-754 range, and non-JSON values.  Object
keys are ordered by UTF-16 code units and finite floats use the ECMAScript
number thresholds required by JCS.  Strings are preserved byte-for-byte after
JSON decoding; this module never performs Unicode normalization.
"""

from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping


class StrictJSONError(ValueError):
    """Raised when input cannot be represented as unambiguous strict JSON."""


_IJSON_MAX_INTEGER = (1 << 53) - 1


def _reject_duplicate_keys(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StrictJSONError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _reject_nonfinite_constant(value: str) -> None:
    raise StrictJSONError(f"non-finite JSON number is forbidden: {value}")


def _parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise StrictJSONError(f"non-finite JSON number is forbidden: {value}")
    return parsed


def strict_json_loads(data: str | bytes | bytearray) -> Any:
    """Decode one strict JSON document without accepting duplicate keys."""

    try:
        value = json.loads(
            data,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite_constant,
            parse_float=_parse_finite_float,
        )
        _assert_json_value(value)
        return value
    except StrictJSONError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StrictJSONError(f"invalid JSON: {exc}") from exc


def load_strict_json(path: str | Path) -> Any:
    """Read a UTF-8 JSON file using :func:`strict_json_loads`."""

    source = Path(path)
    try:
        data = source.read_bytes()
    except OSError as exc:
        raise StrictJSONError(f"cannot read JSON file {source}: {exc}") from exc
    return strict_json_loads(data)


def _validate_string(value: str, path: str) -> None:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise StrictJSONError(f"lone Unicode surrogate at {path}") from exc


def _assert_json_value(value: Any, path: str = "$") -> None:
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, str):
        _validate_string(value, path)
        return
    if isinstance(value, int):
        if not -_IJSON_MAX_INTEGER <= value <= _IJSON_MAX_INTEGER:
            raise StrictJSONError(f"integer outside I-JSON range at {path}")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise StrictJSONError(f"non-finite number at {path}")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise StrictJSONError(f"non-string mapping key at {path}: {key!r}")
            _validate_string(key, f"{path}.<key>")
            _assert_json_value(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_json_value(item, f"{path}[{index}]")
        return
    raise StrictJSONError(
        f"value at {path} is outside the JSON data model: {type(value).__name__}"
    )


def _json_string(value: str) -> str:
    _validate_string(value, "$string")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _ecmascript_number(value: float) -> str:
    """Render one finite binary64 using the JCS/JSON.stringify thresholds."""

    if not math.isfinite(value):
        raise StrictJSONError("non-finite number cannot be canonicalized")
    if value == 0.0:
        return "0"

    negative = value < 0.0
    magnitude = -value if negative else value
    shortest = repr(magnitude).lower()
    decimal_value = Decimal(shortest)
    if 1e-6 <= magnitude < 1e21:
        rendered = format(decimal_value, "f")
        if "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".")
    else:
        mantissa, exponent = format(decimal_value, "e").split("e", 1)
        mantissa = mantissa.rstrip("0").rstrip(".")
        exponent_value = int(exponent)
        rendered = f"{mantissa}e{'+' if exponent_value >= 0 else ''}{exponent_value}"
    return f"-{rendered}" if negative else rendered


def _utf16_sort_key(value: str) -> bytes:
    try:
        return value.encode("utf-16-be", errors="strict")
    except UnicodeEncodeError as exc:
        raise StrictJSONError("object key contains a lone Unicode surrogate") from exc


def _canonical_text(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return _json_string(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _ecmascript_number(value)
    if isinstance(value, Mapping):
        return "{" + ",".join(
            f"{_json_string(key)}:{_canonical_text(value[key])}"
            for key in sorted(value, key=_utf16_sort_key)
        ) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_canonical_text(item) for item in value) + "]"
    raise StrictJSONError(
        f"value is outside the JSON data model: {type(value).__name__}"
    )


def canonical_json_bytes(value: Any) -> bytes:
    """Return canonical RFC 8785/JCS UTF-8 bytes for an I-JSON value."""

    _assert_json_value(value)
    return _canonical_text(value).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    """Return the lowercase SHA256 of :func:`canonical_json_bytes`."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()
