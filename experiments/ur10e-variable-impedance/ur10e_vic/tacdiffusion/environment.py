"""Cached immutable environment fingerprint; no runtime tree scan."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Sequence


def immutable_environment_fingerprint(paths: Sequence[str | Path], *, cache_path: str | Path) -> str:
    normalized = tuple(sorted(str(Path(path).resolve()) for path in paths))
    cache = Path(cache_path)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.sha256(json.dumps(normalized, separators=(",", ":")).encode()).hexdigest()
    if cache.exists():
        payload = json.loads(cache.read_text(encoding="utf-8"))
        if payload.get("cache_key") == cache_key and isinstance(payload.get("fingerprint"), str):
            return payload["fingerprint"]
    entries = []
    for value in normalized:
        path = Path(value)
        if not path.is_file():
            raise ValueError(f"environment fingerprint path must be an immutable file: {value}")
        entries.append({"path": value, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    fingerprint = hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    cache.write_text(json.dumps({"schema": "ur10e_environment_fingerprint/v1", "cache_key": cache_key, "fingerprint": fingerprint, "entries": entries}, sort_keys=True) + "\n", encoding="utf-8")
    return fingerprint
