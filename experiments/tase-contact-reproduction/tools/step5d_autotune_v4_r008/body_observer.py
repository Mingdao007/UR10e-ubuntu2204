"""r008 body observer — finance journal on top of robot_body_truth (teller).

Cashier/finance roles: see ``cashier_finance.py``. Polls a live run-dir (never
opens a second RTDE recipe). Emits structured events to
``r008-body-observer.jsonl``. Does **not** kill the formal host, change
schedules, or fail-closed gate formal campaigns (not internal audit).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .robot_body_truth import (
    BodyVerdict,
    MotionDiff,
    RobotBodyTruth,
    classify_run_dir,
    observe_dashboard,
)

SCHEMA = "step5d.autotune-v4/r008-body-observer-v1"
OBSERVER_JSONL_NAME = "r008-body-observer.jsonl"
PHASE_TIMINGS_NAME = "r008-phase-timings.jsonl"

# Wave4 FAR=0.005 planner estimate (eff=0.8, FAR 11mm @ 0.005, NEAR 2.5mm @ 0.0005).
DEFAULT_PLANNED_SEARCH_S = 8.89
DEFAULT_SEARCH_SLOW_FACTOR = 1.5
DEFAULT_IDLE_BETWEEN_CYCLES_S = 15.0
DEFAULT_CANARY_STALL_ABORT_S = 8.0
CANARY_STALL_ABORT_ARTIFACT = "r008-canary-stall-abort.json"


class ObserverEventKind(str, Enum):
    SEARCH_MOVING = "search_moving"
    SEARCH_SLOW = "search_slow"
    CONTACT_LATCH_OK = "contact_latch_ok"
    IDLE_BETWEEN_CYCLES = "idle_between_cycles"
    HOST_DEAD_ROBOT_PLAYING = "host_dead_robot_playing"
    CONTACTED_STALLED_LIVE = "contacted_stalled_live"
    TICK = "tick"  # optional heartbeat when --emit-ticks


def _phase_is_search(claim: str | None) -> bool:
    if not claim:
        return False
    u = claim.upper()
    return "CONTACT_SEARCH" in u or u in {"ARM", "SEARCH"}


def _phase_is_between_cycles(claim: str | None) -> bool:
    if not claim:
        return False
    u = claim.upper()
    return any(tok in u for tok in ("HOME", "SAFE_RETURN", "SEAL", "QUEUE_COMPLETE"))


def _host_live_for_run_dir(run_dir: Path) -> bool:
    """True if a supervise/host process still references this run-dir."""

    needle = str(run_dir.resolve())
    proc = Path("/proc")
    if not proc.is_dir():
        return True  # fail-open on exotic hosts
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", "ignore"
            )
        except OSError:
            continue
        if needle not in cmdline:
            continue
        if "run_step5d_autotune_v4_r008" in cmdline or "supervise_step5d_autotune_v4_r008" in cmdline:
            return True
    return False


def _dashboard_playing(robot_host: str) -> bool | None:
    try:
        dash = observe_dashboard(robot_host, timeout_s=2.0)
    except Exception:
        return None
    if dash.get("program_running") is True:
        return True
    if dash.get("program_running") is False:
        return False
    state = str(dash.get("program_state") or "").upper()
    if "PLAYING" in state:
        return True
    if state.startswith("STOPPED"):
        return False
    return None


def latest_contact_search_s(run_dir: Path) -> float | None:
    """Newest phase-timings row with contact_search_s."""

    path = run_dir / PHASE_TIMINGS_NAME
    if not path.is_file():
        return None
    last: float | None = None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines[-80:]:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        d = row.get("durations_s") or {}
        val = d.get("contact_search_s")
        if isinstance(val, (int, float)):
            last = float(val)
    return last


@dataclass
class BodyObserverState:
    last_packet_sequence: int | None = None
    search_wall_t0: float | None = None
    idle_wall_t0: float | None = None
    saw_contact_force_in_search: bool = False
    prior_tp: int | None = None
    prior_verdict: BodyVerdict | None = None
    emitted: set[str] = field(default_factory=set)

    def clear_ephemeral_keys(self, *prefixes: str) -> None:
        drop = [k for k in self.emitted if any(k.startswith(p) for p in prefixes)]
        for k in drop:
            self.emitted.discard(k)


def observe_once(
    run_dir: Path | str,
    state: BodyObserverState,
    *,
    planned_search_s: float = DEFAULT_PLANNED_SEARCH_S,
    search_slow_factor: float = DEFAULT_SEARCH_SLOW_FACTOR,
    idle_between_cycles_s: float = DEFAULT_IDLE_BETWEEN_CYCLES_S,
    robot_host: str | None = None,
    now: float | None = None,
    host_live: bool | None = None,
    dashboard_playing: bool | None = None,
) -> list[dict[str, Any]]:
    """Classify one sample and return zero or more observer events."""

    root = Path(run_dir)
    wall = float(time.time() if now is None else now)
    snap, v, motion = classify_run_dir(root)
    events: list[dict[str, Any]] = []

    pkt = snap.packet_sequence
    pkt_advanced = (
        pkt is not None
        and (
            state.last_packet_sequence is None
            or int(pkt) != int(state.last_packet_sequence)
        )
    )

    claim = snap.host_claim_phase
    # Body-primary for active descent; claim-primary for stalled (avoids SEAL-era
    # stale tp=20 contact rows). Moving-down always counts as search.
    claim_search = _phase_is_search(claim)
    body_descending = v == BodyVerdict.MOVING_DOWN
    in_search = claim_search or body_descending

    # --- host dead / robot playing (independent of packet advance) ---
    live = _host_live_for_run_dir(root) if host_live is None else bool(host_live)
    playing = dashboard_playing
    if playing is None and robot_host:
        playing = _dashboard_playing(robot_host)
    if playing is True and not live:
        key = "host_dead_robot_playing"
        if key not in state.emitted:
            state.emitted.add(key)
            events.append(
                _event(
                    ObserverEventKind.HOST_DEAD_ROBOT_PLAYING,
                    wall,
                    snap,
                    v,
                    motion,
                    detail={"host_live": False, "dashboard_playing": True},
                )
            )
    elif live:
        state.emitted.discard("host_dead_robot_playing")

    # Frozen snapshot: do not accumulate stall / search_slow on the same packet.
    if pkt is not None and not pkt_advanced and state.last_packet_sequence is not None:
        return events

    if pkt is not None:
        state.last_packet_sequence = int(pkt)

    # --- search window tracking ---
    if in_search and v == BodyVerdict.MOVING_DOWN:
        if state.search_wall_t0 is None:
            state.search_wall_t0 = wall
        events.append(
            _event(
                ObserverEventKind.SEARCH_MOVING,
                wall,
                snap,
                v,
                motion,
                detail={"search_elapsed_s": wall - state.search_wall_t0},
            )
        )
        slow_after = float(planned_search_s) * float(search_slow_factor)
        elapsed = wall - state.search_wall_t0
        if elapsed > slow_after:
            key = f"search_slow:{int(state.search_wall_t0)}"
            if key not in state.emitted:
                state.emitted.add(key)
                events.append(
                    _event(
                        ObserverEventKind.SEARCH_SLOW,
                        wall,
                        snap,
                        v,
                        motion,
                        detail={
                            "search_elapsed_s": elapsed,
                            "planned_search_s": planned_search_s,
                            "threshold_s": slow_after,
                            "latest_contact_search_s": latest_contact_search_s(root),
                        },
                    )
                )
    elif not in_search:
        state.search_wall_t0 = None
        state.clear_ephemeral_keys("search_slow:")

    if in_search and snap.force_norm_n is not None and snap.force_norm_n >= 1.0:
        state.saw_contact_force_in_search = True

    # contact latch OK: had contact force in search, then left tp=20
    if (
        state.saw_contact_force_in_search
        and state.prior_tp == 20
        and snap.tp_state is not None
        and int(snap.tp_state) != 20
    ):
        key = f"contact_latch_ok:{wall:.0f}"
        if key not in state.emitted:
            state.emitted.add(key)
            events.append(
                _event(
                    ObserverEventKind.CONTACT_LATCH_OK,
                    wall,
                    snap,
                    v,
                    motion,
                    detail={"prior_tp": state.prior_tp, "tp_state": snap.tp_state},
                )
            )
        state.saw_contact_force_in_search = False

    # idle between cycles
    between = _phase_is_between_cycles(claim)
    idle_body = v in {BodyVerdict.HOLDING_READY, BodyVerdict.UNKNOWN} and (
        snap.stationary is True
        or (motion is not None and motion.speed_proxy_m_s < 0.0005)
    )
    if between and idle_body:
        if state.idle_wall_t0 is None:
            state.idle_wall_t0 = wall
        elif wall - state.idle_wall_t0 >= float(idle_between_cycles_s):
            key = f"idle_between:{int(state.idle_wall_t0)}"
            if key not in state.emitted:
                state.emitted.add(key)
                events.append(
                    _event(
                        ObserverEventKind.IDLE_BETWEEN_CYCLES,
                        wall,
                        snap,
                        v,
                        motion,
                        detail={
                            "idle_s": wall - state.idle_wall_t0,
                            "threshold_s": idle_between_cycles_s,
                            "host_claim_phase": claim,
                        },
                    )
                )
    else:
        state.idle_wall_t0 = None
        state.clear_ephemeral_keys("idle_between:")

    # True contact stall: advancing packets + search claim (not SEAL/HOME idle).
    if claim_search and v == BodyVerdict.CONTACTED_STALLED and pkt_advanced:
        key = f"stalled_live:{pkt}"
        if key not in state.emitted:
            state.emitted.add(key)
            events.append(
                _event(
                    ObserverEventKind.CONTACTED_STALLED_LIVE,
                    wall,
                    snap,
                    v,
                    motion,
                    detail={"packet_sequence": pkt},
                )
            )

    if snap.tp_state is not None:
        state.prior_tp = int(snap.tp_state)
    state.prior_verdict = v
    return events


def _event(
    kind: ObserverEventKind,
    wall: float,
    snap: RobotBodyTruth,
    v: BodyVerdict,
    motion: MotionDiff | None,
    *,
    detail: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    pose = snap.tcp_pose_m_rad
    out: dict[str, Any] = {
        "schema": SCHEMA,
        "event": kind.value,
        "wall_time_s": wall,
        "verdict": v.value,
        "host_claim_phase": snap.host_claim_phase,
        "tp_state": snap.tp_state,
        "packet_sequence": snap.packet_sequence,
        "force_norm_n": snap.force_norm_n,
        "tcp_z_m": pose[2] if pose else None,
        "source": snap.source,
    }
    if motion is not None:
        out["motion"] = motion.as_dict()
    if detail:
        out["detail"] = dict(detail)
    return out


def append_events(run_dir: Path | str, events: Sequence[Mapping[str, Any]]) -> Path:
    root = Path(run_dir)
    path = root / OBSERVER_JSONL_NAME
    if not events:
        return path
    with path.open("a", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(dict(ev), sort_keys=True, allow_nan=False) + "\n")
    return path


def watch_run_dir(
    run_dir: Path | str,
    *,
    interval_s: float = 1.0,
    planned_search_s: float = DEFAULT_PLANNED_SEARCH_S,
    search_slow_factor: float = DEFAULT_SEARCH_SLOW_FACTOR,
    idle_between_cycles_s: float = DEFAULT_IDLE_BETWEEN_CYCLES_S,
    robot_host: str | None = None,
    write_jsonl: bool = True,
    count: int = 0,
) -> Iterator[list[dict[str, Any]]]:
    """Poll forever (or ``count`` ticks). Yields event batches per tick."""

    root = Path(run_dir)
    state = BodyObserverState()
    n = 0
    while True:
        batch = observe_once(
            root,
            state,
            planned_search_s=planned_search_s,
            search_slow_factor=search_slow_factor,
            idle_between_cycles_s=idle_between_cycles_s,
            robot_host=robot_host,
        )
        if write_jsonl and batch:
            append_events(root, batch)
        yield batch
        n += 1
        if count > 0 and n >= count:
            return
        time.sleep(max(0.1, float(interval_s)))


@dataclass
class CanaryStallAbortState:
    """Stateful tracker for sustained contacted_stalled / contacted_stalled_live."""

    observer: BodyObserverState = field(default_factory=BodyObserverState)
    stall_wall_t0: float | None = None

    @property
    def stall_elapsed_s(self) -> float | None:
        if self.stall_wall_t0 is None:
            return None
        return max(0.0, time.time() - self.stall_wall_t0)


def _stall_live_this_tick(
    events: Sequence[Mapping[str, Any]],
    *,
    claim_search: bool,
    verdict: BodyVerdict,
    pkt_advanced: bool,
) -> bool:
    if pkt_advanced and any(
        e.get("event") == ObserverEventKind.CONTACTED_STALLED_LIVE.value for e in events
    ):
        return True
    return claim_search and verdict == BodyVerdict.CONTACTED_STALLED and pkt_advanced


def _stall_sustained_active(*, claim_search: bool, verdict: BodyVerdict) -> bool:
    return claim_search and verdict == BodyVerdict.CONTACTED_STALLED


def canary_stall_abort_tick(
    run_dir: Path | str,
    state: CanaryStallAbortState,
    *,
    canary_mode: bool = False,
    sustained_s: float = DEFAULT_CANARY_STALL_ABORT_S,
    now: float | None = None,
    robot_host: str | None = None,
    host_live: bool | None = None,
    dashboard_playing: bool | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Poll run-dir once; return (observer_events, abort_reason|None).

    Formal/default (``canary_mode=False``) never returns an abort dict — diagnose only.
    """

    root = Path(run_dir)
    wall = float(time.time() if now is None else now)
    prior_pkt = state.observer.last_packet_sequence
    events = observe_once(
        root,
        state.observer,
        robot_host=robot_host,
        now=wall,
        host_live=host_live,
        dashboard_playing=dashboard_playing,
    )
    snap, verdict, _motion = classify_run_dir(root)
    pkt = snap.packet_sequence
    pkt_advanced = (
        prior_pkt is not None
        and pkt is not None
        and int(pkt) != int(prior_pkt)
    )
    claim_search = _phase_is_search(snap.host_claim_phase)

    stall_live = _stall_live_this_tick(
        events,
        claim_search=claim_search,
        verdict=verdict,
        pkt_advanced=pkt_advanced,
    )
    sustained = _stall_sustained_active(claim_search=claim_search, verdict=verdict)

    if stall_live and state.stall_wall_t0 is None:
        state.stall_wall_t0 = wall
    elif not sustained:
        state.stall_wall_t0 = None

    if state.stall_wall_t0 is None:
        return events, None

    elapsed = wall - state.stall_wall_t0
    if elapsed < float(sustained_s):
        return events, None

    abort_doc: dict[str, Any] = {
        "schema": SCHEMA + "/canary-stall-abort-v1",
        "reason": "contacted_stalled_sustained",
        "sustained_s": round(elapsed, 3),
        "threshold_s": float(sustained_s),
        "verdict": verdict.value,
        "host_claim_phase": snap.host_claim_phase,
        "packet_sequence": pkt,
        "canary_mode": bool(canary_mode),
        "wall_time_s": wall,
        "run_dir": str(root.resolve()),
    }
    if not canary_mode:
        return events, None
    return events, abort_doc


def write_canary_stall_abort_artifact(run_dir: Path | str, abort: Mapping[str, Any]) -> Path:
    root = Path(run_dir)
    path = root / CANARY_STALL_ABORT_ARTIFACT
    path.write_text(
        json.dumps(dict(abort), indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return path


def watch_canary_stall_abort(
    run_dir: Path | str,
    *,
    canary_mode: bool = False,
    sustained_s: float = DEFAULT_CANARY_STALL_ABORT_S,
    interval_s: float = 1.0,
    robot_host: str | None = None,
    write_jsonl: bool = True,
    count: int = 0,
) -> Iterator[tuple[list[dict[str, Any]], dict[str, Any] | None]]:
    """Poll run-dir; yield (observer_events, abort_dict|None) each tick."""

    root = Path(run_dir)
    state = CanaryStallAbortState()
    n = 0
    while True:
        events, abort = canary_stall_abort_tick(
            root,
            state,
            canary_mode=canary_mode,
            sustained_s=sustained_s,
            robot_host=robot_host,
        )
        if write_jsonl and events:
            append_events(root, events)
        yield events, abort
        n += 1
        if count > 0 and n >= count:
            return
        time.sleep(max(0.1, float(interval_s)))


# Policy entrypoint name used by plan tier C / canary harness wiring.
canary_stall_abort_policy = canary_stall_abort_tick


def search_duration_report(run_dir: Path | str) -> dict[str, Any]:
    """Summarize recent contact_search_s vs planner (for handoff)."""

    root = Path(run_dir)
    planned = DEFAULT_PLANNED_SEARCH_S
    samples: list[float] = []
    path = root / PHASE_TIMINGS_NAME
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            d = row.get("durations_s") or {}
            val = d.get("contact_search_s")
            if isinstance(val, (int, float)):
                samples.append(float(val))
    recent = samples[-8:]
    return {
        "schema": SCHEMA + "/search-report",
        "planned_search_s": planned,
        "n_samples": len(samples),
        "recent_contact_search_s": recent,
        "recent_mean_s": (sum(recent) / len(recent)) if recent else None,
        "vs_plan_delta_s": (
            (sum(recent) / len(recent) - planned) if recent else None
        ),
        "home_z_red_line": (
            "Shortening search toward ~10s needs lower Home Z or raising "
            "PLANNED_V_FAR/NEAR max (fingerprint/safety) — not observer scope."
        ),
        "run_dir": str(root.resolve()),
    }


__all__ = [
    "SCHEMA",
    "OBSERVER_JSONL_NAME",
    "CANARY_STALL_ABORT_ARTIFACT",
    "DEFAULT_PLANNED_SEARCH_S",
    "DEFAULT_CANARY_STALL_ABORT_S",
    "BodyObserverState",
    "CanaryStallAbortState",
    "ObserverEventKind",
    "append_events",
    "canary_stall_abort_policy",
    "canary_stall_abort_tick",
    "latest_contact_search_s",
    "observe_once",
    "search_duration_report",
    "watch_canary_stall_abort",
    "watch_run_dir",
    "write_canary_stall_abort_artifact",
]
