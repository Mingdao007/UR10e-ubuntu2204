"""r008 overlay: raise the frozen 15s fresh-subprocess verify timeout.

Every non-QUAL seal calls ``R006HostLoop._sidecar_rows`` →
``R006ObservationLedger.fresh_process_verify`` → a fresh-process *ledger*
replay of every raw PATH artifact.  With ~20 sealed ~18 MB artifacts that
replay already exceeds the frozen r005 ``timeout=15.0``, which surfaces as
``unexpected_code_fault:fresh-process ledger raw-evidence verification failed``
and revokes authority mid-SPACEFILL.

r008 keeps the verifier body byte-identical and only lengthens the wall-clock
budget for the live host process.  Prefer also using the r008 ledger/host
overrides that skip full ledger replay on the hot seal path; this scope is the
fail-closed backstop for cold resume and remaining binding calls.

Phase 3 (2026-08-07): ``mode=artifact`` prefers in-process hydrate +
ForceObjective columnar verify (slim JSON + ``.r008raw``). Legacy full-JSON
artifacts still verify. Falls back to the stock subprocess on failure.
"""

from __future__ import annotations

import json
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from step5d_autotune_v4_r005.observations import ObservationError

# Empirically ~16s for 21 formal rows; BO/RETEST will grow further.
R008_FRESH_VERIFY_TIMEOUT_S = 600.0


def run_fresh_r008(mode: str, path: Path, *, timeout_s: float = R008_FRESH_VERIFY_TIMEOUT_S) -> dict[str, Any]:
    """Same contract as r005 ``_run_fresh``, with an r008-owned timeout.

    For ``mode=artifact``, try Phase-3 in-process verify first (hydrate
    ``.r008raw`` + columnar ForceObjective). Fail closed to stock subprocess.
    """

    if mode == "artifact":
        try:
            from step5d_autotune_v4_r008.ledger_raw_artifact import (
                verify_ledger_artifact_fresh,
            )

            return verify_ledger_artifact_fresh(Path(path))
        except Exception:
            # Fail closed to stock subprocess (legacy full-JSON / odd fixtures).
            pass

    from step5d_autotune_v4_r005 import observations as obs

    tools_root = str(Path(obs.__file__).resolve().parents[1])
    try:
        result = subprocess.run(
            [sys.executable, "-c", obs._FRESH_CHILD_CODE, tools_root, mode, str(Path(path).resolve())],
            check=True,
            capture_output=True,
            text=True,
            timeout=float(timeout_s),
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip().splitlines()[-1:] or ["child exited nonzero"]
        raise ObservationError(
            f"fresh-process {mode} raw-evidence verification failed: {detail[0]}"
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise ObservationError(f"fresh-process {mode} raw-evidence verification failed") from exc
    try:
        value = json.loads(result.stdout)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ObservationError(f"fresh-process {mode} returned invalid receipt") from exc
    if not isinstance(value, dict):
        raise ObservationError(f"fresh-process {mode} returned a non-object receipt")
    return value


@contextmanager
def r008_fresh_verify_scope(*, timeout_s: float = R008_FRESH_VERIFY_TIMEOUT_S) -> Iterator[None]:
    """Process-local patch: r005 ``_run_fresh`` uses the r008 timeout budget."""

    from step5d_autotune_v4_r005 import observations as obs

    original = obs._run_fresh

    def _patched(mode: str, path: Path) -> dict[str, Any]:
        return run_fresh_r008(mode, path, timeout_s=timeout_s)

    try:
        obs._run_fresh = _patched  # type: ignore[assignment]
        yield
    finally:
        obs._run_fresh = original  # type: ignore[assignment]


__all__ = [
    "R008_FRESH_VERIFY_TIMEOUT_S",
    "r008_fresh_verify_scope",
    "run_fresh_r008",
]
