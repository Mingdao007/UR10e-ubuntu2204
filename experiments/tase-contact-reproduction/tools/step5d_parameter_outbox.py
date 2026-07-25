"""Durable post-processing tasks for the parameter receiver hot path."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from step5d_autotune_v3.state import atomic_json


OUTBOX_SCHEMA = "step5d.parameter-receiver/postprocess-task-v1"


def enqueue_postprocess_task(
    root: Path,
    *,
    dispatch_sequence: int,
    trial_uid: str,
    capture_path: Path,
    result_path: Path,
) -> Path:
    """Atomically enqueue SHA/analysis/PNG work without doing it inline."""

    tasks = root / "tasks"
    tasks.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = tasks / f"{dispatch_sequence:012d}.json"
    payload: dict[str, Any] = {
        "schema": OUTBOX_SCHEMA,
        "dispatch_sequence": int(dispatch_sequence),
        "trial_uid": str(trial_uid),
        "capture_path": str(capture_path),
        "result_path": str(result_path),
        "operations": ["sha256", "analysis", "png"],
        "optimizer_required": False,
        "status": "PENDING",
    }
    if path.exists() or path.is_symlink():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("immutable postprocess task is unreadable") from exc
        if path.is_symlink() or existing != payload:
            raise RuntimeError("immutable postprocess task differs")
    else:
        atomic_json(path, payload)
    return path
