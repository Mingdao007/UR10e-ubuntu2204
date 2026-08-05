"""r008 host phase-timing instrumentation (Track A1).

Splits the non-contact residual of a physical attempt into durable marks:

* ``HOME`` — host Home reconciliation finished
* ``CONTACT_SEARCH_START`` — ARM complete; TP contact search about to begin
* ``STAGE25_START`` / ``SEARCH_DONE`` — first formal PATH/stage-25 sample
* ``RUN_60S`` — ``execute_attempt`` returned (path window + TP return home)
* ``SAFE_RETURN`` — host safe-return proof
* ``SEAL`` — observation sealed into the ledger

``CONTACT_LATCH`` is reserved but not emitted: the mature path-sample sink
only delivers state-25 PATH samples, so latch (state 20→21) is not visible
without editing the frozen r004 live writer.

Persists as:

1. ``metrics["phase_timings_s"]`` on the sealed observation (marks available
   at seal time; does not rewrite existing metric keys), and
2. ``r008-phase-timings.jsonl`` sidecar next to the run ledger (includes SEAL
   after the ledger append, so seal duration is measurable).

Historical canary / pre-instrumentation rows cannot be backfilled.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping, MutableMapping

PHASE_TIMINGS_SCHEMA = "step5d.autotune-v4/r008-phase-timings-v1"
PHASE_TIMINGS_SIDECAR_NAME = "r008-phase-timings.jsonl"

# Ordered marks used for duration diffs. Optional marks may be absent.
MARK_ORDER = (
    "HOME",
    "DISPATCH",
    "ARM",
    "CONTACT_SEARCH_START",
    "CONTACT_LATCH",
    "STAGE25_START",
    "SEARCH_DONE",
    "RUN_60S",
    "SAFE_RETURN",
    "SEAL",
    "TELL",
    "QUEUE_COMPLETE",
)

_EVENT_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9_]+):t\+(?P<elapsed>-?\d+(?:\.\d+)?):dt=(?P<delta>-?\d+(?:\.\d+)?)$"
)


def parse_phase_event(event: str) -> tuple[str, float, float] | None:
    """Parse ``NAME:t+<elapsed>:dt=<delta>`` host event lines."""

    if not isinstance(event, str):
        return None
    match = _EVENT_RE.match(event.strip())
    if match is None:
        return None
    return (
        match.group("name"),
        float(match.group("elapsed")),
        float(match.group("delta")),
    )


def marks_from_events(events: list[str] | tuple[str, ...]) -> dict[str, float]:
    """Collect first-seen elapsed marks from timed host events."""

    marks: dict[str, float] = {}
    for event in events:
        parsed = parse_phase_event(event)
        if parsed is None:
            continue
        name, elapsed, _delta = parsed
        if name not in marks:
            marks[name] = elapsed
    return marks


def _finite_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def _delta(marks: Mapping[str, float], start: str, end: str) -> float | None:
    left = _finite_or_none(marks.get(start))
    right = _finite_or_none(marks.get(end))
    if left is None or right is None:
        return None
    return max(0.0, right - left)


def compute_phase_durations_s(marks: Mapping[str, float]) -> dict[str, float | None]:
    """Derive home/search/path60/return/seal seconds from absolute marks.

    Definitions (elapsed from per-attempt clock origin):

    * ``home_s`` = HOME − 0 (origin)
    * ``contact_search_s`` = STAGE25_START − CONTACT_SEARCH_START
      (fallback: STAGE25_START − ARM; if STAGE25 missing, RUN_60S − start)
    * ``path_60_s`` = RUN_60S − STAGE25_START
      (includes formal 60 s PATH **and** TP return-home inside execute_attempt)
    * ``safe_return_s`` = SAFE_RETURN − RUN_60S (host proof; usually near-zero live)
    * ``seal_s`` = SEAL − SAFE_RETURN (None until SEAL mark exists)
    * ``seal_overlap_s`` / ``motion_blocked_on_seal_s`` are emitted by the
      r008 async-seal sidecar (PATH60 overlap only — not HOME→DISPATCH wait).
    * ``dispatch_s`` ≈ prior ``async_seal_wall_s`` is a fail signal (serial barrier).
    """

    search_start = "CONTACT_SEARCH_START" if "CONTACT_SEARCH_START" in marks else "ARM"
    if "STAGE25_START" in marks:
        contact_search_s = _delta(marks, search_start, "STAGE25_START")
        path_60_s = _delta(marks, "STAGE25_START", "RUN_60S")
    else:
        # Offline fakes / QUAL may never emit PATH samples.
        contact_search_s = _delta(marks, search_start, "RUN_60S")
        path_60_s = None

    return {
        "home_s": _finite_or_none(marks.get("HOME")),
        "dispatch_s": _delta(marks, "HOME", "DISPATCH"),
        "arm_s": _delta(marks, "DISPATCH", "ARM"),
        "contact_search_s": contact_search_s,
        "path_60_s": path_60_s,
        "safe_return_s": _delta(marks, "RUN_60S", "SAFE_RETURN"),
        "seal_s": _delta(marks, "SAFE_RETURN", "SEAL"),
        "tell_s": _delta(marks, "SEAL", "TELL"),
        "seal_overlap_s": None,
        "motion_blocked_on_seal_s": None,
    }


def build_phase_timings_s(
    marks: Mapping[str, float],
    *,
    attempt_sequence: int | None = None,
    kind: str | None = None,
    execution_id: str | None = None,
) -> dict[str, Any]:
    """Build the durable ``phase_timings_s`` payload (new field only)."""

    clean_marks = {
        str(name): float(elapsed)
        for name, elapsed in marks.items()
        if _finite_or_none(elapsed) is not None
    }
    payload: dict[str, Any] = {
        "schema": PHASE_TIMINGS_SCHEMA,
        "marks_elapsed_s": {name: clean_marks[name] for name in MARK_ORDER if name in clean_marks},
        "durations_s": compute_phase_durations_s(clean_marks),
        "notes": {
            "path_60_s_includes_tp_return_home": True,
            "contact_latch_unavailable_without_r004_writer_hook": True,
            "historical_rows_not_backfillable": True,
        },
    }
    # Preserve any unexpected marks (future overlays) without dropping them.
    extras = {name: value for name, value in clean_marks.items() if name not in payload["marks_elapsed_s"]}
    if extras:
        payload["marks_elapsed_s"].update(extras)
    if attempt_sequence is not None:
        payload["attempt_sequence"] = int(attempt_sequence)
    if kind is not None:
        payload["kind"] = str(kind)
    if execution_id is not None:
        payload["execution_id"] = str(execution_id)
    return payload


def attach_phase_timings_to_result(result: Any, marks: Mapping[str, float]) -> Any:
    """Return a copy of ``AttemptResult`` with ``metrics.phase_timings_s`` set."""

    metrics = dict(getattr(result, "metrics", {}) or {})
    execution_id: str | None = None
    raw_execution = metrics.get("execution_id") or getattr(result, "execution_id", None)
    if isinstance(raw_execution, str) and raw_execution:
        execution_id = raw_execution
    metrics["phase_timings_s"] = build_phase_timings_s(
        marks,
        attempt_sequence=int(getattr(result, "attempt_sequence", 0) or 0),
        kind=str(getattr(result, "kind", "") or ""),
        execution_id=execution_id,
    )
    return replace(result, metrics=metrics)


def append_phase_timings_sidecar(run_dir: Path, timings: Mapping[str, Any]) -> Path:
    """Append one JSONL row next to the run ledger for ops grepping."""

    path = Path(run_dir) / PHASE_TIMINGS_SIDECAR_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(timings), sort_keys=True, allow_nan=False) + "\n")
    return path


def wrap_path_sample_sink_for_stage25(
    sink: Callable[[Any], None],
    *,
    on_stage25_start: Callable[[], None],
) -> Callable[[Any], None]:
    """Invoke ``on_stage25_start`` on PATH/stage-25 samples.

    Debounce is owned by ``R008HostLoop.notify_stage25_start`` / per-attempt
    ``_stage25_marked`` (reset in ``_begin_phase_clock``). A campaign-global
    latch here hid STAGE25/SEARCH_DONE on every attempt after the first
    (see live_20260804_213105 path_60=None while PATH_XY still sealed).
    """

    def wrapped(sample: Any) -> None:
        state = getattr(sample, "state", None)
        path_time_s = getattr(sample, "path_time_s", None)
        # Live writer only sinks state==25 PATH samples with path_time_s.
        if state == 25 and path_time_s is not None:
            on_stage25_start()
        sink(sample)

    return wrapped


def merge_phase_marks(
    target: MutableMapping[str, float],
    source: Mapping[str, float],
) -> None:
    """Copy first-seen marks from ``source`` into ``target``."""

    for name, elapsed in source.items():
        value = _finite_or_none(elapsed)
        if value is None or name in target:
            continue
        target[name] = value


__all__ = [
    "MARK_ORDER",
    "PHASE_TIMINGS_SCHEMA",
    "PHASE_TIMINGS_SIDECAR_NAME",
    "append_phase_timings_sidecar",
    "attach_phase_timings_to_result",
    "build_phase_timings_s",
    "compute_phase_durations_s",
    "marks_from_events",
    "merge_phase_marks",
    "parse_phase_event",
    "wrap_path_sample_sink_for_stage25",
]
