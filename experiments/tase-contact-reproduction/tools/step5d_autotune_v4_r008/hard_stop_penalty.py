"""Hard-stop penalty sidecar for BO (not raw-path evidence).

When a live trial dies on stop-dominant / hard force revoke, append one row to
``r008-hard-stop-penalty.jsonl`` with ``penalty_mae_n = max(hist_max, FLOOR)``.
The r008 optimizer worker merges these into GP training; they never enter
``r006-observations`` / raw objectives.
"""

from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

PENALTY_SCHEMA = "step5d.autotune-v4/r008-hard-stop-penalty-v1"
PENALTY_JSONL_NAME = "r008-hard-stop-penalty.jsonl"
OBJECTIVES_SIDECAR_NAME = "r006-observations-r006-objectives.jsonl"
OBSERVATIONS_NAME = "r006-observations.jsonl"
STATE20_STOP_DOMINANT_NAME = "r008-state20-stop-dominant.json"
PENALTY_FLOOR_MAE_N = 10.0

# reason_code values raised by wire.py's stop-dominant packet check
# (build_wire_packet) that mean "this specific candidate's live force/torque
# response was bad" -- the same class of outcome the hard-stop penalty above
# already exists to punish. Recoverable: seal the penalty, do not revoke
# campaign authority, let the next run_one() HOME/reconcile and continue.
#   61 hard_abs_normal_60n / 62 hard_force_norm_100n / 63 hard_torque_norm_3nm
# Deliberately excluded (stay fatal via the normal _stop() path): 3
# sensor_stale, 4 external_stop, 41 structural_stop, 42 hold_mode violation,
# 50 baseline_qualification_missing -- those are session/sensor/protocol
# faults, not "this candidate was bad" outcomes, and auto-continuing past a
# stale sensor or an external stop request is a different, unreviewed risk.
RECOVERABLE_HARD_STOP_REASON_CODES = frozenset({61, 62, 63})

_REASON_CODE_RE = re.compile(r"reason_code=(\d+)")


def extract_reason_code(exc: BaseException | str) -> int | None:
    """Pull the ``reason_code=NN`` embedded by ``format_stop_dominant_error``.

    Reads the exception text itself (always present at raise time) rather
    than a dumped state20/state25 sidecar file -- the dump write is best
    effort (``except (OSError, TypeError, ValueError): pass`` in
    ``r006/live_adapter.py``) and may be missing even though the exception
    fired; the message text cannot be.
    """

    match = _REASON_CODE_RE.search(str(exc))
    if match is None:
        return None
    return int(match.group(1))


def exception_is_recoverable_hard_stop(exc: BaseException | str) -> bool:
    """True for candidate-specific live faults that should recycle, not halt.

    Includes stop-dominant force/torque trips (61/62/63) and contact-search
    ``five_newton_acquisition_timeout`` (overshoot / lose 4–6 N acquire band).
    """

    if extract_reason_code(exc) in RECOVERABLE_HARD_STOP_REASON_CODES:
        return True
    return "five_newton_acquisition_timeout" in str(exc)


def recoverable_streak_since_last_success(
    penalties: Sequence[Mapping[str, Any]],
    *,
    last_sealed_attempt_sequence: int,
) -> int:
    """Count recoverable-class penalty rows after the last real success.

    2026-08-06: the in-process ``self._recoverable_hard_stop_streak`` counter
    can never exceed 1 in real deployment -- every recoverable hard-stop
    tears the host process down and restarts it, and the counter is a plain
    instance attribute reset to 0 in ``__init__``. The circuit breaker it was
    meant to feed (``RECOVERABLE_HARD_STOP_LIMIT``) could structurally never
    trip. This derives the same streak from the durable penalty ledger
    instead, which survives process restarts: everything after the last
    sealed success (by ``attempt_sequence``) that looks like a candidate-
    specific fault (reason_code 61/62/63, or a
    ``five_newton_acquisition_timeout`` row -- those may lack a numeric
    reason_code, see ``read_stop_dominant_reason``) counts toward the streak.

    Rows with neither ``attempt_sequence`` nor ``dispatch_sequence`` set
    cannot be correlated against ``last_sealed_attempt_sequence`` at all and
    are still counted (biased toward tripping the breaker sooner rather than
    later -- under-tripping was the confirmed defect; an extra human review
    on a genuinely ambiguous row is the safe direction to err on an
    unattended overnight system).
    """

    count = 0
    for row in penalties:
        code = row.get("reason_code")
        reason = str(row.get("reason") or "")
        recoverable = (
            code in RECOVERABLE_HARD_STOP_REASON_CODES
            or "five_newton_acquisition_timeout" in reason
        )
        if not recoverable:
            continue
        seq = row.get("attempt_sequence")
        if seq is None:
            seq = row.get("dispatch_sequence")
        if seq is not None and int(seq) <= last_sealed_attempt_sequence:
            continue
        count += 1
    return count


def _safe_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def historical_max_mae_n(run_dir: Path | str) -> float | None:
    """Max sealed eligible MAE from ``r006-observations.jsonl``.

    Host-cheap proxy for trainable formal trials (eligible sealed rows carry
    the same ``mae_n`` the BO sidecar would expose after verification).
    """

    root = Path(run_dir)
    obs = root / OBSERVATIONS_NAME
    if not obs.is_file():
        return None
    best: float | None = None
    try:
        lines = obs.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, Mapping):
            continue
        if row.get("eligible") is not True:
            continue
        mae = _safe_float(row.get("mae_n"))
        if mae is None:
            continue
        best = mae if best is None else max(best, mae)
    return best


def penalty_mae_from_history(historical_max: float | None) -> float:
    """``max(historical_max, FLOOR)``; empty history → floor only."""

    if historical_max is None:
        return float(PENALTY_FLOOR_MAE_N)
    value = float(historical_max)
    if not math.isfinite(value) or value < 0.0:
        return float(PENALTY_FLOOR_MAE_N)
    return float(max(value, PENALTY_FLOOR_MAE_N))


def load_penalties(run_dir: Path | str) -> list[dict[str, Any]]:
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


def has_penalty_for_dispatch(run_dir: Path | str, dispatch_sequence: int) -> bool:
    want = int(dispatch_sequence)
    for row in load_penalties(run_dir):
        seq = row.get("dispatch_sequence")
        if isinstance(seq, int) and seq == want:
            return True
        if isinstance(seq, float) and int(seq) == want:
            return True
    return False


def build_penalty_row(
    *,
    dispatch_sequence: int,
    attempt_sequence: int | None,
    kind: str,
    point_key: Sequence[Any],
    candidate: Mapping[str, Any] | None = None,
    reason_code: int | None = None,
    reason: str | None = None,
    historical_max_mae_n: float | None = None,
    execution_id: str | None = None,
    issued_at_unix_s: float | None = None,
) -> dict[str, Any]:
    hist = (
        None
        if historical_max_mae_n is None
        else _safe_float(historical_max_mae_n)
    )
    penalty = penalty_mae_from_history(hist)
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
        "penalty_mae_n": float(penalty),
        "historical_max_mae_n": hist,
        "penalty_floor_mae_n": float(PENALTY_FLOOR_MAE_N),
        "reason_code": None if reason_code is None else int(reason_code),
        "reason": None if reason is None else str(reason),
        "execution_id": execution_id,
        "enters_gp_training": True,
        "enters_raw_ledger": False,
        "issued_at_unix_s": float(
            time.time() if issued_at_unix_s is None else issued_at_unix_s
        ),
    }
    if candidate:
        row["candidate"] = dict(candidate)
    return row


def append_penalty(run_dir: Path | str, row: Mapping[str, Any]) -> Path | None:
    """Append one penalty row. Idempotent on ``dispatch_sequence``. Returns path or None if skipped."""

    root = Path(run_dir)
    if row.get("schema") != PENALTY_SCHEMA:
        raise ValueError(f"hard-stop penalty schema differs: {row.get('schema')}")
    if row.get("enters_raw_ledger") is True:
        raise ValueError("hard-stop penalty must not enter raw ledger")
    dispatch = row.get("dispatch_sequence")
    if isinstance(dispatch, bool) or not isinstance(dispatch, int) or dispatch <= 0:
        raise ValueError("hard-stop penalty dispatch_sequence is invalid")
    if has_penalty_for_dispatch(root, dispatch):
        return None
    path = root / PENALTY_JSONL_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(dict(row), sort_keys=True, allow_nan=False) + "\n")
    return path


def read_stop_dominant_reason(run_dir: Path | str) -> tuple[int | None, str | None]:
    path = Path(run_dir) / STATE20_STOP_DOMINANT_NAME
    if not path.is_file():
        return None, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, None
    if not isinstance(payload, Mapping):
        return None, None
    code = payload.get("reason_code")
    reason = payload.get("reason")
    code_i = int(code) if isinstance(code, (int, float)) and not isinstance(code, bool) else None
    reason_s = str(reason) if reason is not None else None
    return code_i, reason_s


def exception_looks_like_hard_stop(exc: BaseException | str) -> bool:
    text = str(exc).lower()
    needles = (
        "stop-dominant",
        "stop_dominant",
        "hard_abs_",
        "reason_code=61",
        "authority_revoked",
        "hard_abs_normal",
    )
    return any(n in text for n in needles)


def candidate_fields_from_overlay(overlay: Mapping[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for key in (
        "force_p_gain",
        "force_i_gain",
        "force_damping",
        "normal_filter_tau_s",
        "orientation_ko",
        "motion_kp",
    ):
        value = _safe_float(overlay.get(key))
        if value is not None:
            out[key] = value
    return out


def merge_penalties_into_grouped(
    grouped: dict[tuple[Any, ...], list[float]],
    group_order: list[tuple[Any, ...]],
    penalties: Sequence[Mapping[str, Any]],
) -> int:
    """Merge penalty MAE values into the BO duplicate-grouping maps. Returns count added."""

    added = 0
    for row in penalties:
        key_raw = row.get("point_key")
        mae = _safe_float(row.get("penalty_mae_n"))
        if not isinstance(key_raw, Sequence) or isinstance(key_raw, (str, bytes)) or mae is None:
            continue
        key = tuple(key_raw)
        if key not in grouped:
            group_order.append(key)
            grouped[key] = []
        grouped[key].append(float(mae))
        added += 1
    return added


def record_hard_stop_penalty_for_run(
    run_dir: Path | str,
    *,
    dispatch_sequence: int,
    attempt_sequence: int | None,
    kind: str,
    point_key: Sequence[Any],
    candidate: Mapping[str, Any] | None = None,
    reason_code: int | None = None,
    reason: str | None = None,
    execution_id: str | None = None,
) -> dict[str, Any] | None:
    """Build+append a penalty for one hard stop. None if already recorded."""

    root = Path(run_dir)
    if has_penalty_for_dispatch(root, int(dispatch_sequence)):
        return None
    code, reason_s = read_stop_dominant_reason(root)
    hist = historical_max_mae_n(root)
    row = build_penalty_row(
        dispatch_sequence=int(dispatch_sequence),
        attempt_sequence=attempt_sequence,
        kind=kind,
        point_key=point_key,
        candidate=candidate,
        reason_code=reason_code if reason_code is not None else code,
        reason=reason if reason is not None else reason_s,
        historical_max_mae_n=hist,
        execution_id=execution_id,
    )
    appended = append_penalty(root, row)
    if appended is None:
        return None
    return row


__all__ = [
    "PENALTY_SCHEMA",
    "PENALTY_JSONL_NAME",
    "PENALTY_FLOOR_MAE_N",
    "OBJECTIVES_SIDECAR_NAME",
    "historical_max_mae_n",
    "penalty_mae_from_history",
    "load_penalties",
    "has_penalty_for_dispatch",
    "build_penalty_row",
    "append_penalty",
    "read_stop_dominant_reason",
    "exception_looks_like_hard_stop",
    "candidate_fields_from_overlay",
    "merge_penalties_into_grouped",
    "record_hard_stop_penalty_for_run",
]
