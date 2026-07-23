"""Side-effect-free framing for the optimizer process boundary."""

from __future__ import annotations

import json
from typing import Any


REQUEST_SCHEMA = "step5d.autotune-v3/optimizer-request-v2"
RESPONSE_SCHEMA = "step5d.autotune-v3/optimizer-response-v2"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MODE_SEEDS = {"rolling_batch_a": 9009, "rolling_batch_b": 9010}


class OptimizerWireError(ValueError):
    pass


def canonical_bytes(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise OptimizerWireError(
            f"optimizer payload is not canonical JSON: {exc}"
        ) from exc


def strict_json(encoded: bytes, role: str) -> Any:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise OptimizerWireError(f"{role} repeats key {key!r}")
            result[key] = value
        return result

    try:
        return json.loads(
            encoded.decode("ascii"),
            object_pairs_hook=unique,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"forbidden JSON constant {value!r}")
            ),
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise OptimizerWireError(f"{role} is not strict JSON: {exc}") from exc


__all__ = [
    "MAX_RESPONSE_BYTES",
    "MODE_SEEDS",
    "OptimizerWireError",
    "REQUEST_SCHEMA",
    "RESPONSE_SCHEMA",
    "canonical_bytes",
    "strict_json",
]
