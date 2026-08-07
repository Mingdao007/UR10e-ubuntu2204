"""Early-abort penalty sidecar for BO (not raw-path evidence).

When Phase5 early-abort fires, append one row to
``r008-early-abort-penalty.jsonl`` with
``penalty_mae_n = max(partial_at_trigger, kappa_at_trigger * best_so_far)``.
No independent floor — high trigger rates would flatten GP labels to one
constant. Shadow rows default ``enters_gp_training=False``; only True rows
are merged into the r008 optimizer worker. Never enters raw ledger.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from step5d_autotune_v4_r008.hard_stop_penalty import merge_penalties_into_grouped

PENALTY_SCHEMA = "step5d.autotune-v4/r008-early-abort-penalty-v1"
PENALTY_JSONL_NAME = "r008-early-abort-penalty.jsonl"


def _safe_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def load_early_abort_penalties(run_dir: Path | str) -> list[dict[str, Any]]:
    path = Path(run_dir) / PENALTY_JSONL_NAME
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("schema") == PENALTY_SCHEMA:
            out.append(row)
    return out


def has_early_abort_penalty_for_dispatch(
    run_dir: Path | str, dispatch_sequence: int
) -> bool:
    want = int(dispatch_sequence)
    for row in load_early_abort_penalties(run_dir):
        seq = row.get("dispatch_sequence")
        if isinstance(seq, int) and seq == want:
            return True
        if isinstance(seq, float) and int(seq) == want:
            return True
    return False


def build_early_abort_penalty_row(
    *,
    dispatch_sequence: int,
    attempt_sequence: int | None,
    kind: str,
    point_key: Sequence[Any],
    candidate: Mapping[str, Any] | None = None,
    partial_mae_n_at_trigger: float,
    best_so_far_mae_n: float,
    kappa_at_trigger: float,
    trigger_progress_fraction: float,
    enters_gp_training: bool = False,
    execution_id: str | None = None,
    issued_at_unix_s: float | None = None,
) -> dict[str, Any]:
    partial = float(partial_mae_n_at_trigger)
    best = float(best_so_far_mae_n)
    kappa = float(kappa_at_trigger)
    progress = float(trigger_progress_fraction)
    for name, value in (
        ("partial_mae_n_at_trigger", partial),
        ("best_so_far_mae_n", best),
        ("kappa_at_trigger", kappa),
        ("trigger_progress_fraction", progress),
    ):
        if not math.isfinite(value):
            raise ValueError(f"early-abort {name} must be finite")
    penalty_mae_n = max(partial, kappa * best)
    # Normalize lattice ints that arrived as floats.
    norm_key: list[Any] = []
    for item in point_key:
        if isinstance(item, bool):
            norm_key.append(item)
        elif isinstance(item, float) and item.is_integer():
            norm_key.append(int(item))
        else:
            norm_key.append(item)
    row: dict[str, Any] = {
        "schema": PENALTY_SCHEMA,
        "dispatch_sequence": int(dispatch_sequence),
        "attempt_sequence": None if attempt_sequence is None else int(attempt_sequence),
        "kind": str(kind),
        "point_key": norm_key,
        "penalty_mae_n": float(penalty_mae_n),
        "partial_mae_n_at_trigger": float(partial),
        "best_so_far_mae_n": float(best),
        "kappa_at_trigger": float(kappa),
        "trigger_progress_fraction": float(progress),
        "execution_id": execution_id,
        "enters_gp_training": bool(enters_gp_training),
        "enters_raw_ledger": False,
        "issued_at_unix_s": float(
            time.time() if issued_at_unix_s is None else issued_at_unix_s
        ),
    }
    if candidate:
        row["candidate"] = dict(candidate)
    return row


def append_early_abort_penalty(
    run_dir: Path | str, row: Mapping[str, Any]
) -> Path | None:
    """Append one early-abort row. Idempotent on ``dispatch_sequence``.

    Returns path or None if skipped.
    """

    root = Path(run_dir)
    if row.get("schema") != PENALTY_SCHEMA:
        raise ValueError(f"early-abort penalty schema differs: {row.get('schema')}")
    if row.get("enters_raw_ledger") is True:
        raise ValueError("early-abort penalty must not enter raw ledger")
    dispatch = row.get("dispatch_sequence")
    if isinstance(dispatch, bool) or not isinstance(dispatch, int) or dispatch <= 0:
        raise ValueError("early-abort penalty dispatch_sequence is invalid")
    if has_early_abort_penalty_for_dispatch(root, dispatch):
        return None
    path = root / PENALTY_JSONL_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(dict(row), sort_keys=True, allow_nan=False) + "\n")
    return path


def record_early_abort_for_run(
    run_dir: Path | str,
    *,
    dispatch_sequence: int,
    attempt_sequence: int | None,
    kind: str,
    point_key: Sequence[Any],
    candidate: Mapping[str, Any] | None = None,
    partial_mae_n_at_trigger: float,
    best_so_far_mae_n: float,
    kappa_at_trigger: float,
    trigger_progress_fraction: float,
    enters_gp_training: bool = False,
    execution_id: str | None = None,
    issued_at_unix_s: float | None = None,
) -> dict[str, Any] | None:
    """Build+append an early-abort penalty. None if already recorded."""

    root = Path(run_dir)
    if has_early_abort_penalty_for_dispatch(root, int(dispatch_sequence)):
        return None
    row = build_early_abort_penalty_row(
        dispatch_sequence=int(dispatch_sequence),
        attempt_sequence=attempt_sequence,
        kind=kind,
        point_key=point_key,
        candidate=candidate,
        partial_mae_n_at_trigger=partial_mae_n_at_trigger,
        best_so_far_mae_n=best_so_far_mae_n,
        kappa_at_trigger=kappa_at_trigger,
        trigger_progress_fraction=trigger_progress_fraction,
        enters_gp_training=enters_gp_training,
        execution_id=execution_id,
        issued_at_unix_s=issued_at_unix_s,
    )
    appended = append_early_abort_penalty(root, row)
    if appended is None:
        return None
    return row


__all__ = [
    "PENALTY_SCHEMA",
    "PENALTY_JSONL_NAME",
    "load_early_abort_penalties",
    "has_early_abort_penalty_for_dispatch",
    "build_early_abort_penalty_row",
    "append_early_abort_penalty",
    "record_early_abort_for_run",
    "merge_penalties_into_grouped",
]
