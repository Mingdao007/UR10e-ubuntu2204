"""Shared process-lifetime lock for direct UR controller mutation entrypoints."""
from __future__ import annotations
import fcntl, os
from pathlib import Path
from typing import IO

_HANDLES: list[IO[str]] = []

def acquire_controller_mutation_locks() -> list[IO[str]]:
    root = Path(os.environ.get("UR10E_LOCK_ROOT", "/tmp/ur10e-resource-locks"))
    root.mkdir(parents=True, exist_ok=True)
    names = ("live-writer-throughput.lock", "throughput.lock",
             "tp-deploy-readback-sha-promotion.lock")
    for name in names:
        handle = (root / name).open("a+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        _HANDLES.append(handle)
    return list(_HANDLES)

def release_controller_mutation_locks(handles: list[IO[str]]) -> None:
    for handle in reversed(handles):
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
        if handle in _HANDLES:
            _HANDLES.remove(handle)
