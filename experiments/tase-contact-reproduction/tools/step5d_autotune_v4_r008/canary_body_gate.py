"""Canary-only internal-audit gate on sustained body stall.

Formal campaigns must keep ``mode='formal'`` (diagnose-only). Canary harnesses
use ``mode='canary'`` and abort when ``contacted_stalled_live`` (or advancing
packet ``contacted_stalled`` during search) persists for ``stall_s`` seconds.

Implementation delegates to ``body_observer.canary_stall_abort_tick``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping

from .body_observer import (
    CANARY_STALL_ABORT_ARTIFACT,
    DEFAULT_CANARY_STALL_ABORT_S,
    CanaryStallAbortState,
    canary_stall_abort_tick,
    watch_canary_stall_abort,
    write_canary_stall_abort_artifact,
)
from .cashier_finance import (
    CANARY_AUDIT_MAY_ABORT_ON_STALL,
    FORMAL_FINANCE_MAY_CLOSE_BOOKS,
    ROLE_INTERNAL_AUDIT,
)

GateMode = Literal["canary", "formal"]

DEFAULT_STALL_ABORT_S = DEFAULT_CANARY_STALL_ABORT_S
SCHEMA = "step5d.autotune-v4/r008-canary-body-gate-v1"

# Back-compat alias for callers expecting StallGateState on the gate module.
StallGateState = CanaryStallAbortState


@dataclass(frozen=True)
class StallGateDecision:
    abort: bool
    reason: str | None
    mode: GateMode
    stalled_for_s: float | None
    verdict: str | None
    detail: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "role": ROLE_INTERNAL_AUDIT,
            "abort": self.abort,
            "reason": self.reason,
            "mode": self.mode,
            "stalled_for_s": self.stalled_for_s,
            "verdict": self.verdict,
            "detail": dict(self.detail),
            "formal_may_close_books": FORMAL_FINANCE_MAY_CLOSE_BOOKS,
            "canary_may_abort_on_stall": CANARY_AUDIT_MAY_ABORT_ON_STALL,
        }


def canary_stall_abort_policy(
    run_dir: Path | str,
    state: StallGateState,
    *,
    mode: GateMode = "formal",
    stall_abort_s: float = DEFAULT_STALL_ABORT_S,
    now: float | None = None,
    host_live: bool | None = True,
    robot_host: str | None = None,
) -> StallGateDecision:
    """Evaluate one poll; formal never aborts."""

    canary_mode = mode == "canary" and CANARY_AUDIT_MAY_ABORT_ON_STALL
    events, abort_doc = canary_stall_abort_tick(
        run_dir,
        state,
        canary_mode=canary_mode,
        sustained_s=stall_abort_s,
        now=now,
        host_live=host_live,
        robot_host=robot_host,
    )
    stalled_for = None
    if state.stall_wall_t0 is not None:
        wall = float(time.time() if now is None else now)
        stalled_for = wall - state.stall_wall_t0

    verdict = abort_doc.get("verdict") if abort_doc else None
    if abort_doc is None and events:
        verdict = events[-1].get("verdict")

    detail: dict[str, Any] = {
        "observer_events": [e.get("event") for e in events],
        "packet_sequence": abort_doc.get("packet_sequence") if abort_doc else None,
        "host_claim_phase": abort_doc.get("host_claim_phase") if abort_doc else None,
    }
    if abort_doc:
        detail.update(abort_doc)

    if abort_doc is not None and canary_mode:
        return StallGateDecision(
            abort=True,
            reason=abort_doc.get("reason"),
            mode=mode,
            stalled_for_s=stalled_for,
            verdict=verdict,
            detail=detail,
        )

    return StallGateDecision(
        abort=False,
        reason=None,
        mode=mode,
        stalled_for_s=stalled_for,
        verdict=verdict,
        detail=detail,
    )


evaluate_stall_gate = canary_stall_abort_policy


def watch_canary_until_abort(
    run_dir: Path | str,
    *,
    interval_s: float = 1.0,
    stall_abort_s: float = DEFAULT_STALL_ABORT_S,
    count: int = 0,
    robot_host: str | None = None,
    write_jsonl: bool = True,
) -> StallGateDecision:
    """Poll until abort decision or ``count`` ticks (canary mode only)."""

    state = StallGateState()
    n = 0
    last = evaluate_stall_gate(
        run_dir, state, mode="canary", stall_abort_s=stall_abort_s, robot_host=robot_host
    )
    while True:
        for events, abort_doc in watch_canary_stall_abort(
            run_dir,
            canary_mode=True,
            sustained_s=stall_abort_s,
            interval_s=interval_s,
            robot_host=robot_host,
            write_jsonl=write_jsonl,
            count=1,
        ):
            _ = events
            last = evaluate_stall_gate(
                run_dir,
                state,
                mode="canary",
                stall_abort_s=stall_abort_s,
                robot_host=robot_host,
            )
            if abort_doc is not None:
                write_canary_stall_abort_artifact(run_dir, abort_doc)
                return last
        n += 1
        if count > 0 and n >= count:
            return last


__all__ = [
    "SCHEMA",
    "CANARY_STALL_ABORT_ARTIFACT",
    "DEFAULT_STALL_ABORT_S",
    "StallGateState",
    "StallGateDecision",
    "canary_stall_abort_policy",
    "evaluate_stall_gate",
    "watch_canary_until_abort",
    "write_canary_stall_abort_artifact",
]
