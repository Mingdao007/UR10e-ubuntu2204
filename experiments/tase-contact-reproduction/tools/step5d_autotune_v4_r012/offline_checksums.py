"""Passive checksum helpers for offline calibration and replay artifacts only."""

from __future__ import annotations

import hashlib
from typing import Any

from .common import canonical_bytes


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def require_digest(value: Any, role: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{role} must be a lowercase SHA-256")
    return value


__all__ = ["digest", "require_digest", "sha256_bytes"]
