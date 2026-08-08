"""Per-attempt body reconcile sidecar (finance journal — not GP training).

Writes ``r008-body-reconcile.jsonl`` summarizing observer events for one sealed
attempt. Never appends to ``r006-observations`` / objectives.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Mapping

from .body_observer import OBSERVER_JSONL_NAME
from .cashier_finance import ROLE_FINANCE, SCHEMA as ROLES_SCHEMA

RECONCILE_SCHEMA = "step5d.autotune-v4/r008-body-reconcile-v1"
RECONCILE_JSONL_NAME = "r008-body-reconcile.jsonl"


def _safe_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def summarize_observer_events(
    events: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Aggregate diagnose-only observer events into one attempt reconcile row."""

    counts: dict[str, int] = {}
    search_moving = 0
    stalled_live = 0
    search_slow = 0
    mismatch_claim_vs_body = 0
    max_search_elapsed_s: float | None = None
    stall_packets: list[int] = []

    for ev in events:
        kind = str(ev.get("event") or "")
        counts[kind] = counts.get(kind, 0) + 1
        if kind == "search_moving":
            search_moving += 1
            detail = ev.get("detail") if isinstance(ev.get("detail"), Mapping) else {}
            elapsed = _safe_float(detail.get("search_elapsed_s"))
            if elapsed is not None:
                max_search_elapsed_s = (
                    elapsed
                    if max_search_elapsed_s is None
                    else max(max_search_elapsed_s, elapsed)
                )
        elif kind == "contacted_stalled_live":
            stalled_live += 1
            pkt = ev.get("packet_sequence")
            if isinstance(pkt, int):
                stall_packets.append(pkt)
        elif kind == "search_slow":
            search_slow += 1

        claim = str(ev.get("host_claim_phase") or "")
        verdict = str(ev.get("verdict") or "")
        # Cashier claimed search/path but teller said stalled/holding.
        claim_u = claim.upper()
        if (
            ("CONTACT_SEARCH" in claim_u or claim_u in {"ARM", "SEARCH", "RUN_60S", "PATH"})
            and verdict in {"contacted_stalled", "holding_ready"}
        ):
            mismatch_claim_vs_body += 1

    return {
        "event_counts": counts,
        "search_moving_ticks": search_moving,
        "contacted_stalled_live_ticks": stalled_live,
        "search_slow_events": search_slow,
        "claim_vs_body_mismatch_ticks": mismatch_claim_vs_body,
        "max_search_elapsed_s": max_search_elapsed_s,
        "stall_packet_sequences": stall_packets[-16:],
        "search_looked_moving": search_moving > 0,
        "had_live_stall": stalled_live > 0,
    }


def load_observer_events(run_dir: Path | str) -> list[dict[str, Any]]:
    path = Path(run_dir) / OBSERVER_JSONL_NAME
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def events_in_wall_window(
    events: list[Mapping[str, Any]],
    *,
    t0: float | None,
    t1: float | None,
) -> list[Mapping[str, Any]]:
    if t0 is None and t1 is None:
        return list(events)
    selected: list[Mapping[str, Any]] = []
    for ev in events:
        wall = _safe_float(ev.get("wall_time_s"))
        if wall is None:
            continue
        if t0 is not None and wall < t0:
            continue
        if t1 is not None and wall > t1:
            continue
        selected.append(ev)
    return selected


def build_reconcile_row(
    *,
    attempt_sequence: int,
    kind: str,
    execution_id: str | None = None,
    events: list[Mapping[str, Any]] | None = None,
    wall_t0: float | None = None,
    wall_t1: float | None = None,
    issued_at_unix_s: float | None = None,
) -> dict[str, Any]:
    summary = summarize_observer_events(list(events or []))
    return {
        "schema": RECONCILE_SCHEMA,
        "role": ROLE_FINANCE,
        "roles_schema": ROLES_SCHEMA,
        "trainable": False,
        "enters_gp_ledger": False,
        "attempt_sequence": int(attempt_sequence),
        "kind": str(kind),
        "execution_id": execution_id,
        "wall_window_s": {"t0": wall_t0, "t1": wall_t1},
        "issued_at_unix_s": float(
            time.time() if issued_at_unix_s is None else issued_at_unix_s
        ),
        **summary,
    }


def append_body_reconcile(
    run_dir: Path | str,
    row: Mapping[str, Any],
) -> Path:
    root = Path(run_dir)
    path = root / RECONCILE_JSONL_NAME
    if row.get("enters_gp_ledger") is True or row.get("trainable") is True:
        raise ValueError("body reconcile rows must not be trainable / GP ledger")
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(dict(row), sort_keys=True, allow_nan=False) + "\n")
    return path


def append_reconcile_for_attempt(
    run_dir: Path | str,
    *,
    attempt_sequence: int,
    kind: str,
    execution_id: str | None = None,
    wall_t0: float | None = None,
    wall_t1: float | None = None,
) -> dict[str, Any]:
    """Finance stamp for one sealed attempt (sidecar only)."""

    root = Path(run_dir)
    all_events = load_observer_events(root)
    windowed = events_in_wall_window(all_events, t0=wall_t0, t1=wall_t1)
    # If no wall window, use recent tail so empty windows still get a row.
    events = windowed if (wall_t0 is not None or wall_t1 is not None) else all_events[-200:]
    row = build_reconcile_row(
        attempt_sequence=attempt_sequence,
        kind=kind,
        execution_id=execution_id,
        events=events,
        wall_t0=wall_t0,
        wall_t1=wall_t1,
    )
    append_body_reconcile(root, row)
    return row


__all__ = [
    "RECONCILE_SCHEMA",
    "RECONCILE_JSONL_NAME",
    "summarize_observer_events",
    "load_observer_events",
    "events_in_wall_window",
    "build_reconcile_row",
    "append_body_reconcile",
    "append_reconcile_for_attempt",
]
