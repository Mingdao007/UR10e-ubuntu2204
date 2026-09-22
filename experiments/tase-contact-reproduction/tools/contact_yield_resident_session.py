"""Resident TASE lifecycle around the mature native writer.

This module owns session boundaries only.  Control, force, freshness, and
wire safety remain in the existing mature writer/provider path.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
import copy
import gc
import hashlib
import json
import math
import multiprocessing as mp
import os
from pathlib import Path
import tempfile
import time
import traceback
from typing import Any, Callable, Mapping

from contact_yield_live_writer import (
    YieldLivePrerequisites,
    native_attempt,
    stop_and_confirm,
)
from step5d_autotune_v4_r004.wire import CommandMode, SessionCommand
from step5d_autotune_v4_r004.contracts import (
    HOME_ORIENTATION_TOLERANCE_RAD,
    HOME_POSITION_TOLERANCE_M,
    HOME_Q_TOLERANCE_RAD,
)
from step5d_autotune_v4_r004.transport import FRESH_FRAME_WAIT_MAX_S


RESIDENT_SESSION_SCHEMA = "yield-live-entry/resident-session-v2"
RESIDENT_SEAL_SCHEMA = "yield-live-entry/attempt-seal-v1"
RESIDENT_REFRESH_SCHEMA = "yield-live-entry/home-refresh-v1"
READY_HOME_NEXT = 78
STOPPED = 90


class ResidentSessionError(RuntimeError):
    """A resident lifecycle transition failed closed."""


def _resident_process_task(task: Callable[[], Any], sender: Any) -> None:
    """Run a read-only evidence task in a forked child.

    The live writer is deliberately not reconstructed in this child.  On the
    supported Linux host the forked snapshot contains the already collected
    collector/buffer objects, while the parent remains the only process that
    owns and polls the real-time transports.
    """

    try:
        # fork inherits the live owner's FIFO policy. Evidence work must
        # never compete with its parent at the same realtime priority.
        os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0))
        scheduler = {"policy": os.sched_getscheduler(0),
                     "priority": os.sched_getparam(0).sched_priority,
                     "affinity": sorted(os.sched_getaffinity(0))}
        if scheduler["policy"] != os.SCHED_OTHER or scheduler["priority"] != 0:
            raise ResidentSessionError("evidence worker retained realtime scheduling")
        result = task()
        sender.send(("ok", result, scheduler))
    except BaseException as exc:  # pragma: no cover - exercised through parent
        try:
            sender.send(
                (
                    "error",
                    type(exc).__name__,
                    str(exc),
                    traceback.format_exc(),
                )
            )
        except BaseException:
            pass
    finally:
        sender.close()


def _resident_service_task(
    run_dir: str,
    sequence: int,
    context: Mapping[str, Any],
    buffers: Mapping[str, list[Any]],
    frozen_service: Mapping[str, list[Any]],
) -> dict[str, Any]:
    """Serialize one immutable attempt snapshot outside the RTDE owner."""

    attempt_dir = Path(run_dir) / "attempts" / f"{int(sequence):04d}"
    segments = {
        name: _atomic_jsonl(attempt_dir / f"{name}.jsonl", list(rows))
        for name, rows in buffers.items()
    }
    service_rows: list[dict[str, Any]] = []
    for label, rows in frozen_service.items():
        for row in rows:
            service_rows.append(
                {
                    "attempt_sequence": int(sequence),
                    "stage": str(context.get("stage", "seal")),
                    "label": str(label),
                    "row": row,
                }
            )
    service_segment = _atomic_jsonl(
        attempt_dir / "service_observations.jsonl", service_rows
    )
    stages = {}
    for frame in buffers.get("robot_frames", ()):
        state = str(frame.integer_echoes.get(26))
        stamp = frame.received_monotonic_s
        stage = stages.setdefault(state, {"first_observed_monotonic_s": stamp, "samples": 0})
        stage["last_observed_monotonic_s"] = stamp
        stage["samples"] += 1
    return {
        "segments": segments,
        "service_segment": service_segment,
        "service_context": dict(context),
        "tp_stage_observations": stages,
    }


def _resident_service_tail_task(
    run_dir: str,
    context: Mapping[str, Any],
    frozen_service: Mapping[str, list[Any]],
) -> dict[str, Any]:
    """Persist service observations left after the final attempt seal."""

    rows: list[dict[str, Any]] = []
    for label, values in frozen_service.items():
        for row in values:
            rows.append(
                {
                    "attempt_sequence": context.get("attempt_sequence"),
                    "stage": str(context.get("stage", "session_finish")),
                    "label": str(label),
                    "row": row,
                }
            )
    return {
        "service_segment": _atomic_jsonl(
            Path(run_dir) / "session-service-observations.jsonl", rows
        ),
        "service_context": dict(context),
    }


def _json_default(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, (tuple, list)):
        return list(value)
    raise TypeError(f"unsupported resident evidence type: {type(value).__name__}")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(
                dict(payload),
                stream,
                indent=2,
                sort_keys=True,
                allow_nan=False,
                default=_json_default,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _atomic_jsonl(path: Path, rows: list[Any]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    count = 0
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            for row in rows:
                stream.write(
                    json.dumps(
                        row,
                        sort_keys=True,
                        allow_nan=False,
                        default=_json_default,
                    )
                    + "\n"
                )
                count += 1
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return {
        "path": str(path),
        "count": count,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _event(ctx: "ResidentSession", stage: str, event: str, **fields: Any) -> None:
    row = {
        "stage": str(stage),
        "event": str(event),
        "monotonic_s": float(ctx.mono_clock()),
        "wall_s": float(ctx.wall_clock()),
    }
    row.update(fields)
    ctx.lifecycle_events.append(row)


def _finite(value: Any, field: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ResidentSessionError(f"{field} is not finite")
    return result


def _home_proof(writer: Any, output: Any, *, fresh: bool) -> dict[str, Any]:
    home = getattr(writer, "_home", None)
    if output is None or home is None or home.q is None:
        return {"fresh": bool(fresh), "stationary": False, "home_verified": False}
    pose = tuple(float(value) for value in output.tcp_pose_m_rad)
    q = tuple(float(value) for value in output.q_rad)
    home_pose = tuple(float(value) for value in home.pose)
    home_q = tuple(float(value) for value in home.q)
    position_error = math.dist(pose[:3], home_pose[:3])
    q_error = max(abs(actual - expected) for actual, expected in zip(q, home_q, strict=True))
    try:
        import numpy as np
        from contact_yield_math import so3_exp, so3_log

        orientation_error = float(
            np.linalg.norm(so3_log(so3_exp(pose[3:]) @ so3_exp(home_pose[3:]).T))
        )
    except Exception:
        orientation_error = math.dist(pose[3:], home_pose[3:])
    safety_normal = bool(getattr(output, "safety_normal", False))
    stationary = bool(getattr(output, "stationary", False))
    identity = True
    identity_check = getattr(writer, "_identity_matches", None)
    if callable(identity_check):
        identity = bool(identity_check(output))
    state = (getattr(output, "integer_echoes", {}) or {}).get(26)
    verified = bool(
        fresh
        and safety_normal
        and stationary
        and identity
        and state in {READY_HOME_NEXT, STOPPED}
        and position_error <= HOME_POSITION_TOLERANCE_M
        and orientation_error <= HOME_ORIENTATION_TOLERANCE_RAD
        and q_error <= HOME_Q_TOLERANCE_RAD
    )
    return {
        "fresh": bool(fresh),
        "stationary": stationary,
        "safety_normal": safety_normal,
        "resident_identity": identity,
        "state": state,
        "home_pose_error_m": position_error,
        "home_orientation_error_rad": orientation_error,
        "home_q_error_rad": q_error,
        "home_verified": verified,
        "fixed_home_route": verified,
        "controller_timestamp": getattr(output, "timestamp", None),
        "received_monotonic_s": getattr(output, "received_monotonic_s", None),
    }


@dataclass
class ResidentSession:
    mature: Any
    runtime: Any
    provider: Any
    prerequisites: YieldLivePrerequisites
    run_dir: Path
    mono_clock: Callable[[], float] = time.monotonic
    wall_clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    injected_endpoints: bool = False
    refresh_readback: Callable[..., Any] | None = None
    dashboard_stop_and_verify: Callable[..., Any] | None = None

    def __post_init__(self) -> None:
        self.run_dir = Path(self.run_dir).expanduser().resolve()
        self.seed_state: Any = None
        self.prepared = False
        self.closed = False
        self.next_sequence = 1
        self.last_home: dict[str, Any] | None = None
        self.last_stop: dict[str, Any] | None = None
        self.lifecycle_events: list[dict[str, Any]] = []
        self.transport_trace: list[dict[str, Any]] = []
        self.refreshes: list[dict[str, Any]] = []
        self._last_rtde_timestamp: float | None = None
        self._last_consumed_packet: int | None = None
        self._last_session_sequence: int | None = None
        self._service_error: BaseException | None = None
        self._pending_service_batches = []
        self._gc_was_enabled = gc.isenabled()
        self.last_completed_evidence = None
        self.last_completed_sequence = None
        self._service_context: dict[str, Any] = {
            "attempt_sequence": 0,
            "stage": "session",
        }
        self._service_capacity = 4096

    @property
    def writer(self) -> Any:
        return self.mature.writer

    def prepare_session(self, *, live_ack: str) -> None:
        if self.prepared:
            raise ResidentSessionError("resident session was prepared twice")
        _event(self, "SESSION", "open_start")
        gc.collect()
        gc.disable()
        self.mature.open(live_ack=live_ack)
        self.writer._terminal_finalize_service = self._run_terminal_finalize
        self.writer._terminal_settle_service = self._wait_home_settle
        self.seed_state = copy.deepcopy(self.provider.snapshot())
        self.prepared = True
        self.transport_trace.append(
            {
                "event": "open",
                "monotonic_s": float(self.mono_clock()),
                "wall_s": float(self.wall_clock()),
                "controller_opened": bool(
                    getattr(getattr(self.writer, "_controller_transport", None), "opened", True)
                ),
                "kunwei_opened": bool(
                    getattr(getattr(self.writer, "_kunwei_transport", None), "opened", True)
                ),
            }
        )
        _event(self, "SESSION", "open_complete", resident_state="READY_HOME_NEXT")

    def _service_tick(self, *, settling=False) -> tuple[Any, Any] | None:
        writer = self.writer
        wait_policy = getattr(writer, "fresh_frame_wait_policy", None)
        configured_wait = float(getattr(wait_policy, "wait_s", 0.0))
        wait_s = max(configured_wait, FRESH_FRAME_WAIT_MAX_S)
        try:
            output = writer._poll_checked(
                # A TP READY_HOME_NEXT frame can have residual sub-limit
                # velocity after its state change. Keep servicing transport;
                # the separate Home dwell gates the next ARM.
                require_stationary=False,
                allow_prearm_epoch=True,
                wait_s=wait_s,
            )
        except BaseException as exc:
            self.transport_trace.append(
                {
                    "event": "recv_error",
                    "stage": "service",
                    "monotonic_s": float(self.mono_clock()),
                    "wall_s": float(self.wall_clock()),
                    "cause": f"{type(exc).__name__}: {exc}",
                }
            )
            raise
        if not getattr(writer, "_last_poll_was_fresh", False):
            self.sleep(0.002)
            return None
        # NativeYieldLiveWriter records every frame and packet in the durable
        # service segments; avoid a second campaign-sized copy in the receipt.
        state = (getattr(output, "integer_echoes", {}) or {}).get(26)
        if state in {READY_HOME_NEXT, 80}:
            from contact_home_motion_profile import (
                HOME_JOINT_SPEED_GUARD_RAD_S, HOME_TCP_SPEED_GUARD_M_S,
                HOME_ANGULAR_SPEED_GUARD_RAD_S,
            )
        if settling:
            if state not in {READY_HOME_NEXT, 80}:
                raise ResidentSessionError('Home settle lost terminal return state')
        if state in {READY_HOME_NEXT, 80}:
            if (max(abs(v) for v in output.qd_rad_s) > HOME_JOINT_SPEED_GUARD_RAD_S
                    or math.hypot(*output.tcp_speed_m_s_rad_s[:3]) > HOME_TCP_SPEED_GUARD_M_S
                    or math.hypot(*output.tcp_speed_m_s_rad_s[3:]) > HOME_ANGULAR_SPEED_GUARD_RAD_S):
                raise ResidentSessionError('Home settle exceeds existing return velocity guard')
            if not _home_proof(writer, output, fresh=True).get('home_verified'):
                self._home_settle_dirty = True
        elif not settling and not output.stationary:
            raise ResidentSessionError('resident service left stationary Home state')
        sensor = None
        if state in {READY_HOME_NEXT, 80}:
            sensor = writer._read_sensor()
            writer._session_command = SessionCommand.HOLD
            writer._send_packet(sensor, command_mode=CommandMode.HOLD)
        self.sleep(0.002)
        return output, sensor

    def _wait_home_settle(self, terminal):
        """Keep RETURNING guards until joint Home stays still for 0.5 s."""
        previous_mode = getattr(self.writer, '_service_mode', False)
        self.writer._service_mode = True
        deadline = float(self.mono_clock()) + 5.0
        stable_since = None
        _event(self, 'HOME_SETTLE', 'start', sequence=self.next_sequence)
        try:
            while float(self.mono_clock()) < deadline:
                pair = self._service_tick(settling=True)
                if pair is None:
                    continue
                output, _ = pair
                proof = _home_proof(self.writer, output, fresh=True)
                if proof['home_verified']:
                    now = float(output.timestamp)
                    stable_since = now if stable_since is None else stable_since
                    if now - stable_since >= 0.5:
                        self._home_settle_dirty = False
                        _event(self, 'HOME_SETTLE', 'verified', sequence=self.next_sequence,
                               stationary_duration_s=now-stable_since)
                        return output
                else:
                    stable_since = None
            raise ResidentSessionError('continuous joint Home stationary dwell timed out')
        finally:
            self.writer._service_mode = previous_mode

    def _set_service_context(self, *, attempt_sequence: int, stage: str) -> None:
        self._service_context = {
            "attempt_sequence": int(attempt_sequence),
            "stage": str(stage),
        }

    def _rotate_service_observations(self) -> tuple[dict[str, list[Any]], dict[str, Any]]:
        """Rotate the bounded in-memory service view before a child snapshot."""

        writer = self.writer
        current = getattr(writer, "_service_observations", {})
        frozen = {
            str(label): list(rows)
            for label, rows in current.items()
            if rows
        }
        labels = tuple(str(label) for label in current)
        writer._service_observations = {label: [] for label in labels}
        context = dict(self._service_context)
        self._pending_service_batches.append((frozen, context))
        return frozen, context

    def _run_process_work(
        self,
        *,
        reason: str,
        task: Callable[[], Any],
        on_started: Callable[[], None] | None = None,
    ) -> Any:
        """Run slow evidence work in a fork while this process services RTDE."""

        if os.name != "posix":
            raise ResidentSessionError("resident process service requires POSIX fork")
        ctx = mp.get_context("fork")
        receiver, sender = ctx.Pipe(duplex=False)
        writer = self.writer
        writer._service_mode = True
        process = ctx.Process(
            target=_resident_process_task,
            args=(task, sender),
            name=f"tase-resident-{reason}",
        )
        _event(self, "SERVICE", "start", reason=reason, worker="fork")
        result: Any = None
        received = False
        try:
            process.start()
            sender.close()
            if on_started is not None:
                on_started()
            while True:
                if receiver.poll(0.0):
                    message = receiver.recv()
                    received = True
                    if message[0] == "ok":
                        result = message[1]
                        _event(self, "SERVICE", "worker_scheduler", reason=reason,
                               scheduler=message[2])
                    else:
                        raise ResidentSessionError(
                            f"resident {reason} worker failed: {message[1]}: {message[2]}"
                        )
                    # Keep the owner alive until the child has really exited;
                    # the result pipe may become readable before fsync/exit.
                    while process.is_alive():
                        self._service_tick()
                    process.join()
                    return result
                if not process.is_alive():
                    if receiver.poll(0.0):
                        continue
                    raise ResidentSessionError(
                        f"resident {reason} worker exited without a result"
                    )
                self._service_tick()
        except BaseException as exc:
            self._service_error = exc
            # Stop publication before waiting for the failed evidence worker.
            stop = getattr(writer, "stop", None)
            if callable(stop):
                try:
                    stop("resident_evidence_fault")
                except Exception:
                    pass  # Preserve the first failure; finish_session verifies stopping.
            if process.pid is not None:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=0.2)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=0.2)
            raise
        finally:
            sender.close()
            receiver.close()
            writer._service_mode = False
            _event(
                self,
                "SERVICE",
                "stop",
                reason=reason,
                worker="fork",
                result_received=received,
                exitcode=process.exitcode,
            )

    def _run_terminal_finalize(self, finalizer):
        """Finalize evidence in a fork while this owner services RTDE."""

        result = self._run_process_work(reason="terminal_finalize", task=finalizer)
        self.last_completed_evidence = result
        self.last_completed_sequence = self.next_sequence
        # Collector destruction also costs CPU: release large lists in chunks
        # while the sole owner keeps servicing the real endpoints.
        from step5d_autotune_v4_r004.evidence import (
            PathEvidenceCollector, QualificationEvidenceCollector,
        )
        from step5d_autotune_v4_r004.timing import TimingEvidenceCollector
        collectors = (PathEvidenceCollector, QualificationEvidenceCollector,
                      TimingEvidenceCollector)

        def retire(value):
            if isinstance(value, collectors):
                for buffer in vars(value).values():
                    retire(buffer)
            elif isinstance(value, dict):
                for buffer in value.values():
                    retire(buffer)
            elif isinstance(value, (list, set)):
                self._clear_serviced(value)

        for cell in getattr(finalizer, '__closure__', ()) or ():
            value = cell.cell_contents
            if isinstance(value, collectors):
                retire(value)
        return result

    def _clear_serviced(self, buffer):
        previous_mode = getattr(self.writer, '_service_mode', False)
        self.writer._service_mode = True
        self._retirement_last_tick_s = -math.inf
        try:
            while buffer:
                if isinstance(buffer, set):
                    for _ in range(min(len(buffer), 1024)):
                        buffer.pop()
                else:
                    del buffer[-1024:]
                if not self.closed and (
                        float(self.mono_clock()) - getattr(self, '_retirement_last_tick_s', -math.inf) >= .002):
                    self._service_tick()
                    self._retirement_last_tick_s = float(self.mono_clock())
        finally:
            self.writer._service_mode = previous_mode

    def verify_ready_for_next(self, *, reason: str) -> dict[str, Any]:
        if not self.prepared or self.closed:
            raise ResidentSessionError("resident session is not open")
        poll = getattr(self.writer, "_poll_checked", None)
        if not callable(poll):
            raise ResidentSessionError("mature writer lacks fresh Home verification seam")
        deadline = float(self.mono_clock()) + 0.250
        output = None
        fresh = False
        wait_policy = getattr(self.writer, "fresh_frame_wait_policy", None)
        # The mature hot loop may intentionally use a zero-wait policy.  A
        # Home boundary is a freshness rendezvous, so use the existing
        # bounded RTDE wait seam instead of repeatedly inspecting a cache.
        wait_s = max(float(getattr(wait_policy, "wait_s", 0.0)), FRESH_FRAME_WAIT_MAX_S)
        while float(self.mono_clock()) < deadline:
            output = poll(
                require_stationary=False,
                allow_prearm_epoch=True,
                wait_s=max(0.0, min(wait_s, deadline - float(self.mono_clock()))),
            )
            fresh = bool(getattr(self.writer, "_last_poll_was_fresh", False))
            if fresh:
                break
            self.sleep(0.002)
        if not fresh or output is None:
            raise ResidentSessionError("READY_HOME_NEXT fresh RTDE frame timeout")
        if not output.stationary or getattr(self, '_home_settle_dirty', False):
            output = self._wait_home_settle(output)
            fresh = True
        echoes = getattr(output, "integer_echoes", {}) or {}
        state = echoes.get(26)
        if state != READY_HOME_NEXT:
            raise ResidentSessionError(
                f"resident next-arm state is {state!r}, expected READY_HOME_NEXT"
            )
        assert_home = getattr(self.writer, "_assert_prearm_home_boundary", None)
        if callable(assert_home):
            assert_home(output)
        timestamp = _finite(getattr(output, "timestamp", float("nan")), "RTDE timestamp")
        consumed = int(getattr(output, "consumed_packet_sequence", -1))
        session_sequence = int(echoes.get(29, -1))
        if self._last_rtde_timestamp is not None and timestamp <= self._last_rtde_timestamp:
            raise ResidentSessionError("RTDE timestamp did not advance at Home")
        if self._last_consumed_packet is not None and consumed < self._last_consumed_packet:
            raise ResidentSessionError("consumed packet sequence regressed at Home")
        if self._last_session_sequence is not None and session_sequence < self._last_session_sequence:
            raise ResidentSessionError("session command sequence regressed at Home")
        self._last_rtde_timestamp = timestamp
        self._last_consumed_packet = consumed
        self._last_session_sequence = session_sequence
        self.transport_trace.append(
            {
                "event": "recv",
                "stage": "ready_home_next",
                "reason": str(reason),
                "monotonic_s": float(self.mono_clock()),
                "wall_s": float(self.wall_clock()),
                "fresh": True,
                "controller_timestamp": timestamp,
                "consumed_packet_sequence": consumed,
                "session_command_sequence": session_sequence,
                "state": state,
            }
        )
        controller = getattr(self.writer, "_r013_path_early_end_controller", None)
        typed_ack: dict[str, Any] = {"kind": "resident_ready_home_next", "state": state}
        if controller is not None and bool(getattr(controller, "requested", False)):
            requested = int(getattr(controller, "requested_sequence", -1))
            completion = controller.read_completion_registers()
            if tuple(completion) != (requested, 0, 0):
                raise ResidentSessionError(
                    "R013 PATH_END acknowledgement is stale or incomplete: "
                    f"requested={requested}, observed={completion}"
                )
            typed_ack = {
                "kind": "r013_path_end",
                "requested_sequence": requested,
                "ack_sequence": completion[0],
                "terminal_reason": completion[1],
                "reason43_subtype": completion[2],
            }
        home = _home_proof(self.writer, output, fresh=True)
        if not home.get("home_verified"):
            raise ResidentSessionError("fresh READY_HOME_NEXT sample is not exact stationary Home")
        result = {
            "state": state,
            "runtime_playing": bool(
                getattr(output, "program_running", False)
                or getattr(output, "runtime_state", None) in {2, "PLAYING", "RUNNING"}
            ),
            "safety_normal": bool(getattr(output, "safety_normal", False)),
            "stationary": bool(getattr(output, "stationary", False)),
            "home_verified": True,
            "typed_complete_ack": typed_ack,
            "home_proof": home,
            "controller_timestamp": timestamp,
            "received_monotonic_s": getattr(output, "received_monotonic_s", None),
        }
        if result["runtime_playing"] is not True:
            raise ResidentSessionError("resident TP is not PLAYING at READY_HOME_NEXT")
        self.last_home = result
        _event(self, "HOME_CHECK", "verified", reason=reason, state=state)
        return result

    def _refresh_needed(self) -> bool:
        if not isinstance(self.prerequisites, YieldLivePrerequisites):
            return False
        now = float(self.wall_clock())
        stamps = (
            self.prerequisites.controller.observed_at_s,
            self.prerequisites.runtime.observed_at_s,
            self.prerequisites.script1.observed_at_s,
            self.prerequisites.baseline_observed_at_s,
        )
        oldest = min(float(value) for value in stamps)
        return now - oldest >= 300.0

    def refresh_at_home(self) -> dict[str, Any] | None:
        """Refresh session-owned admission evidence before the 300 s seam.

        The current open RTDE/Kunwei pair is reused at verified Home.  No
        neutral HOLD, reconnect, or competing endpoint is introduced.
        """
        if not self._refresh_needed():
            return None
        home = self.verify_ready_for_next(reason="pre_expiry_refresh")
        if self.refresh_readback is None:
            raise ResidentSessionError(
                "admission evidence reached 300 s without an injected fresh readback/baseline fetch"
            )
        now = float(self.wall_clock())
        refreshed = self.refresh_readback(
            session=self,
            home=self.last_home,
            now_s=now,
        )
        if not isinstance(refreshed, Mapping):
            raise ResidentSessionError("fresh readback callback did not return a mapping")
        new_prerequisites = refreshed.get("prerequisites")
        if not isinstance(new_prerequisites, YieldLivePrerequisites):
            raise ResidentSessionError("fresh readback callback did not return typed prerequisites")
        now = float(self.wall_clock())
        new_prerequisites.validate(now_s=now)
        old = self.prerequisites
        for field in ("controller", "runtime", "script1", "baseline_observed_at_s"):
            previous = getattr(old, field)
            current = getattr(new_prerequisites, field)
            previous_stamp = previous if isinstance(previous, (int, float)) else previous.observed_at_s
            current_stamp = current if isinstance(current, (int, float)) else current.observed_at_s
            if float(current_stamp) <= float(previous_stamp):
                raise ResidentSessionError(f"fresh {field} timestamp did not advance")
        baseline_meta = refreshed.get("baseline")
        if not isinstance(baseline_meta, Mapping):
            raise ResidentSessionError("fresh baseline callback metadata is missing")
        required_baseline = {
            "acquisition_duration_s",
            "initial_exclusion_s",
            "sample_count",
            "variance_n2",
            "stationary",
            "no_contact",
            "eoat_identity_sha256",
        }
        if not required_baseline.issubset(baseline_meta):
            raise ResidentSessionError("fresh baseline metadata is incomplete")
        if (
            float(baseline_meta["acquisition_duration_s"]) < 10.0
            or float(baseline_meta["initial_exclusion_s"]) != 0.5
            or int(baseline_meta["sample_count"]) < 100
            or baseline_meta["stationary"] is not True
            or baseline_meta["no_contact"] is not True
            or baseline_meta["eoat_identity_sha256"] != old.contract.eoat_sha256
        ):
            raise ResidentSessionError("fresh baseline failed the existing 10 s/0.5 s context gate")
        refresh_index = len(self.refreshes) + 1
        refresh_dir = self.run_dir / "session-refresh" / f"{refresh_index:04d}"
        result = {
            "schema": RESIDENT_REFRESH_SCHEMA,
            "refresh_index": refresh_index,
            "refreshed_at_s": now,
            "previous_observed_at_s": min(
                old.controller.observed_at_s,
                old.runtime.observed_at_s,
                old.script1.observed_at_s,
                old.baseline_observed_at_s,
            ),
            "controller_receipt_sha256": new_prerequisites.controller.receipt_sha256,
            "runtime_observed_at_s": new_prerequisites.runtime.observed_at_s,
            "baseline": dict(baseline_meta),
            "home_verified": home["home_verified"],
            "readback_validation": refreshed.get("readback_validation"),
        }
        _atomic_json(refresh_dir / "refresh-receipt.json", result)
        self.prerequisites = new_prerequisites
        self.writer.prerequisites = new_prerequisites
        self.writer.software_baseline_n = new_prerequisites.software_baseline_n
        self.writer.session.identity._expected = new_prerequisites.controller
        self.refreshes.append(result)
        _event(self, "SESSION_REFRESH", "sealed", refresh_index=refresh_index)
        return result

    def reset_at_home(
        self,
        *,
        parameter_binding: Mapping[str, Any] | None = None,
        parameter_file: Path | None = None,
    ) -> None:
        if self.seed_state is None:
            raise ResidentSessionError("resident seed state is unavailable")
        if callable(getattr(self.writer, "_poll_checked", None)):
            self.verify_ready_for_next(reason="attempt_reset")
        else:
            _event(self, "HOME_CHECK", "seam_unavailable_for_fault_fixture")
        refresh = self.refresh_at_home()
        del refresh
        self.provider.restore(copy.deepcopy(self.seed_state))
        if parameter_file is not None:
            apply = getattr(self.provider, 'apply_outer_parameters_at_home', None)
            if not callable(apply):
                raise ResidentSessionError(
                    'resident provider has no validated Home parameter loader'
                )
            apply(parameter_file)
        if parameter_binding is not None:
            current = getattr(self.provider, "parameter_binding", None)
            if not isinstance(current, Mapping):
                raise ResidentSessionError(
                    "resident parameter reset requires an existing validated binding"
                )
            requested = dict(parameter_binding)
            actual = dict(current)
            # A Home boundary may reset provider state, but it is not a
            # parameter loader.  Until a validated config is loaded through
            # the public owner, labels and runtime values must remain bound to
            # the object that constructed the provider.
            if requested != actual:
                differing = sorted(
                    key
                    for key in set(requested) | set(actual)
                    if requested.get(key) != actual.get(key)
                )
                raise ResidentSessionError(
                    "resident parameter changes require a validated Home config load; "
                    f"differing_fields={differing}"
                )
        _event(self, "ATTEMPT", "reset_at_home")

    def run_attempt(
        self,
        *,
        phase: str,
        sequence: int,
        control_cpu: int | None,
        parameter_binding: Mapping[str, Any] | None = None,
        parameter_file: Path | None = None,
    ) -> dict[str, Any]:
        started_mono = float(self.mono_clock())
        started_wall = float(self.wall_clock())
        refresh_count = len(self.refreshes)
        if sequence != self.next_sequence:
            raise ResidentSessionError(
                f"attempt sequence {sequence} is not the next resident sequence {self.next_sequence}"
            )
        self._set_service_context(attempt_sequence=sequence, stage="attempt")
        self.reset_at_home(
            parameter_binding=parameter_binding,
            parameter_file=parameter_file,
        )
        if hasattr(self.writer, "_host_path_publish_count"):
            self.writer._host_path_publish_count = 0
        end = getattr(self.writer, "_r013_path_early_end_controller", None)
        if end is not None:
            end.arm(sequence)
        attempt = native_attempt(
            command=phase,
            epoch=self.prerequisites.session_epoch,
            sequence=sequence,
        )
        self.mature.dispatch(attempt, object())
        scheduler = None
        try:
            if control_cpu is not None and not self.injected_endpoints:
                from step5d_autotune_v4_r013.timing_scheduler import (
                    FormalTimingSchedulerLeaseV2,
                    TimingSchedulerProfileV1,
                )

                lease = FormalTimingSchedulerLeaseV2(
                    TimingSchedulerProfileV1(control_cpu_affinity=(int(control_cpu),))
                )
                self.writer.install_timing_scheduler_lease(lease)
                self.writer.prepare_timing_scheduler_lease(collect_gc=False)
            self.mature.arm(attempt)
            _event(self, "ATTEMPT", "arm", sequence=sequence, phase=phase)
            evidence = self.mature.run_60s(attempt)
            _event(self, "ATTEMPT", "path_returned", sequence=sequence, state=READY_HOME_NEXT)
        finally:
            release = getattr(self.writer, "release_timing_scheduler_lease", None)
            if callable(release):
                scheduler = release()
        evidence_payload = asdict(evidence) if is_dataclass(evidence) else dict(evidence)
        ready = self.verify_ready_for_next(reason="attempt_complete")
        metrics = evidence_payload.get("metrics") or {}
        proof = evidence_payload.get("home_proof") or {}
        item = {
            "timing": {
                "started_monotonic_s": started_mono,
                "started_at_s": started_wall,
                "home_verified_monotonic_s": float(self.mono_clock()),
                "path_started_monotonic_s": getattr(self.writer, "_path_command_started_mono_s", None),
                "preparation_refreshed": len(self.refreshes) != refresh_count,
                "cycle_kind": ("cold" if sequence == 1 else
                               "refresh" if len(self.refreshes) != refresh_count else "reuse"),
            },
            "sequence": sequence,
            "phase": phase,
            "host_path_publishes": getattr(self.writer, "_host_path_publish_count", None),
            "scheduler": scheduler,
            "evidence": evidence_payload,
            "state": self.provider.snapshot(),
            "parameter_binding": dict(getattr(self.provider, "parameter_binding", {})),
            "applied_runtime_parameters": {
                "Md_scalar": getattr(getattr(self.provider.runtime, "outer_loop_config", None), "Md_scalar", None),
                "Bd_scalar": getattr(getattr(self.provider.runtime, "outer_loop_config", None), "Bd_scalar", None),
                "force_integral_limit_n_s": getattr(self.provider.runtime, "force_integral_limit_n_s", None),
                "force_integral_policy": getattr(self.provider.runtime, "force_integral_policy", None),
                "force_integral_authority_error_n": getattr(self.provider.runtime, "force_integral_authority_error_n", None),
            } if hasattr(self.provider, "runtime") else None,
            "evidence_eligible": bool(getattr(evidence, "eligible", False)),
            "lifecycle": {
                "path_complete": bool(
                    metrics.get("complete") is True
                    or metrics.get("complete_bins") == metrics.get("required_bins") == 550
                    or evidence_payload.get("complete") is True
                ),
                "home_verified": bool(
                    proof.get("stationary") is True
                    and proof.get("fixed_home_route") is True
                ),
                "ready_for_next": True,
                "program_stopped": False,
                "sealed": False,
                "resident_playing": ready["runtime_playing"],
                "typed_complete_ack": ready["typed_complete_ack"],
            },
            "ready_for_next": ready,
        }
        self.next_sequence += 1
        return item

    def seal_attempt(self, item: dict[str, Any], *, service: bool = True) -> dict[str, Any]:
        sequence = int(item["sequence"])
        writer = self.writer
        buffers = {
            "raw_sensor": list(getattr(writer, "raw_observations", ())),
            "robot_frames": list(getattr(writer, "robot_observations", ())),
            "admission_robot_frames": list(getattr(writer, "admission_robot_observations", ())),
            "rejected_robot_frames": list(getattr(writer, "rejected_robot_observations", ())),
            "published_packets": list(getattr(writer, "command_observations", ())),
            "command_timeline": list(getattr(self.provider, "command_timeline", ())),
        }
        frozen_service, service_context = self._rotate_service_observations()
        # Once this immutable snapshot is forked, service observations belong
        # to the next Home boundary. They cannot be mixed into this attempt's
        # raw segments while the child is hashing/encoding them.
        next_context = {
            "attempt_sequence": sequence + 1,
            "stage": "prearm",
        }
        work = self._run_process_work if service else self._closed_work
        snapshot = work(
            reason="attempt_seal",
            task=lambda: _resident_service_task(
                str(self.run_dir), sequence, service_context, buffers, frozen_service
            ),
            on_started=lambda: self._set_service_context(
                attempt_sequence=next_context["attempt_sequence"],
                stage=next_context["stage"],
            ),
        )
        segments = dict(snapshot["segments"])
        service_segment = dict(snapshot["service_segment"])
        service_counts = {name: len(rows) for name, rows in frozen_service.items()}
        seal = {
            "schema": RESIDENT_SEAL_SCHEMA,
            "session_schema": RESIDENT_SESSION_SCHEMA,
            "attempt_sequence": sequence,
            "sealed_at_s": float(self.wall_clock()),
            "segments": segments,
            "service_observation_counts": service_counts,
            "service_observations": service_segment,
            "service_context": dict(service_context),
            "service_buffer_rotated": True,
            "lifecycle": dict(item.get("lifecycle") or {}),
        }
        seal["lifecycle"]["sealed"] = True
        work(
            reason="attempt_seal_metadata",
            task=lambda: (
                _atomic_json(self.run_dir / "attempts" / f"{sequence:04d}" / "seal.json", seal),
                _atomic_json(self.run_dir / "attempts" / f"{sequence:04d}" / "attempt-result.json",
                             {**item, 'sealed_evidence': seal}),
            ),
        )
        item["sealed_evidence"] = seal
        self._pending_service_batches = [
            batch for batch in self._pending_service_batches if batch[0] is not frozen_service
        ]
        item.setdefault("lifecycle", {})["sealed"] = True
        timing = item.setdefault("timing", {})
        timing["tp_stage_observations"] = snapshot["tp_stage_observations"]
        buffer_names = {
            "raw_sensor": "raw_observations",
            "robot_frames": "robot_observations",
            "admission_robot_frames": "admission_robot_observations",
            "rejected_robot_frames": "rejected_robot_observations",
            "published_packets": "command_observations",
        }
        for name, attribute in buffer_names.items():
            buffer = getattr(writer, attribute, None)
            if isinstance(buffer, list):
                self._clear_serviced(buffer)
        timeline = getattr(self.provider, "command_timeline", None)
        if isinstance(timeline, list):
            self._clear_serviced(timeline)
        timing["sealed_monotonic_s"] = float(self.mono_clock())
        if "started_monotonic_s" in timing:
            timing["through_seal_s"] = timing["sealed_monotonic_s"] - timing["started_monotonic_s"]
        _event(self, "SEAL", "complete", sequence=sequence, segment_count=len(segments))
        return item

    def _closed_work(self, *, reason, task, on_started=None):
        if not self.closed:
            raise ResidentSessionError("unserviced evidence work requires closed transports")
        if on_started is not None:
            on_started()
        return task()

    def seal_service_tail(self):
        if not self.closed:
            raise ResidentSessionError("service tail must follow transport closure")
        self._rotate_service_observations()
        pending = list(self._pending_service_batches)
        combined = {}
        for batch, context in pending:
            for label, rows in batch.items():
                combined.setdefault(label, []).extend(rows)
        result = _resident_service_tail_task(
            str(self.run_dir), self._service_context, combined)
        self._pending_service_batches.clear()
        return result

    def finish_session(self, *, reason: str) -> dict[str, Any]:
        if self.closed:
            return dict(self.last_stop or {})
        _event(self, "STOP", "requested", reason=reason)
        self.transport_trace.append(
            {
                "event": "stop_request",
                "reason": reason,
                "monotonic_s": float(self.mono_clock()),
                "wall_s": float(self.wall_clock()),
            }
        )
        errors: list[str] = []
        # Include the final STOP packets and observations in the durable tail.
        self.writer._service_mode = True
        try:
            stop = stop_and_confirm(self.writer)
        except BaseException as exc:
            stop = {"stopped": False, "tp_ack": False, "error": str(exc)}
            errors.append(f"stop: {type(exc).__name__}: {exc}")
        self.last_stop = dict(stop)
        output = getattr(self.writer, "_last_output", None)
        fresh = bool(
            stop.get("stopped") is True
            and stop.get("received_monotonic_s") is not None
            and stop.get("state") == STOPPED
        )
        proof = _home_proof(self.writer, output, fresh=fresh)
        stop["home_verified"] = bool(proof.get("home_verified"))
        stop["home_proof"] = proof
        self.last_stop = stop
        _event(
            self,
            "STOP",
            "observed",
            stopped=bool(stop.get("stopped")),
            tp_ack=bool(stop.get("tp_ack")),
            home_verified=bool(stop.get("home_verified")),
            reason=stop.get("reason"),
        )
        dashboard_result: Mapping[str, Any] | None = None
        if callable(self.dashboard_stop_and_verify):
            try:
                dashboard_result = self.dashboard_stop_and_verify(
                    reason=reason,
                    protocol_stop=dict(stop),
                    home_proof=dict(proof),
                )
                if not isinstance(dashboard_result, Mapping):
                    raise ResidentSessionError(
                        "Dashboard stop verifier did not return a mapping"
                    )
                stop["dashboard_stop"] = dict(dashboard_result)
                stop["program_stopped"] = bool(
                    dashboard_result.get("program_stopped") is True
                    or dashboard_result.get("stopped") is True
                )
                if stop["program_stopped"] is not True:
                    errors.append("Dashboard STOPPED/runtime_state=1/Home verification failed")
            except BaseException as exc:
                stop["program_stopped"] = False
                errors.append(f"dashboard stop: {type(exc).__name__}: {exc}")
        else:
            # A direct writer-only caller has no Dashboard owner. Keep the
            # protocol acknowledgement distinct and never infer termination.
            stop["dashboard_stop"] = {"available": False}
            stop["program_stopped"] = False
        try:
            self.mature.close()
        except BaseException as exc:
            errors.append(f"mature: {type(exc).__name__}: {exc}")
        try:
            self.runtime.close()
        except BaseException as exc:
            errors.append(f"runtime: {type(exc).__name__}: {exc}")
        self.transport_trace.append(
            {
                "event": "close",
                "monotonic_s": float(self.mono_clock()),
                "wall_s": float(self.wall_clock()),
                "errors": list(errors),
            }
        )
        self.closed = True
        if self._gc_was_enabled:
            gc.enable()
        stop["cleanup_errors"] = errors
        # TP state 90 is a protocol acknowledgement from the resident
        # while-True program.  Dashboard termination is owned by the outer
        # supervisor and is intentionally not inferred here.
        stop["protocol_stop_ack"] = bool(stop.get("stopped") is True and stop.get("tp_ack") is True)
        stop["session_home_verified"] = bool(stop.get("home_verified") is True)
        return stop


def prepare_session(**kwargs: Any) -> ResidentSession:
    session = ResidentSession(**kwargs)
    return session


def run_attempt(session: ResidentSession, **kwargs: Any) -> dict[str, Any]:
    return session.run_attempt(**kwargs)


def finish_session(session: ResidentSession, **kwargs: Any) -> dict[str, Any]:
    return session.finish_session(**kwargs)


__all__ = [
    "READY_HOME_NEXT",
    "RESIDENT_REFRESH_SCHEMA",
    "RESIDENT_SEAL_SCHEMA",
    "RESIDENT_SESSION_SCHEMA",
    "ResidentSession",
    "ResidentSessionError",
    "finish_session",
    "prepare_session",
    "run_attempt",
]


def refresh_live_preparation(*, session, home, now_s):
    """Fetch installed files and capture the unchanged baseline via the sole owner."""
    del home, now_s
    import numpy as np
    from contact_recovery_readback import fetch_recovery_readback
    from contact_yield_live_contract import CONTACT_PROGRAM, HOME_PROGRAM
    from contact_yield_live_writer import load_run_dir_receipts
    from contact_yield_supervisor import _write_receipts
    from prepare_figure8 import validate_sample
    from step5d_eoat_profiles import load_new_eoat_profile

    old = session.prerequisites
    contract = old.contract
    refresh_dir = session.run_dir / 'session-refresh' / f'{len(session.refreshes)+1:04d}'
    refresh_dir.mkdir(parents=True, exist_ok=False)
    package_dir = refresh_dir / 'package'
    session._run_process_work(
        reason='readback_refresh',
        task=lambda: str(fetch_recovery_readback(package_dir,
                            basenames=(CONTACT_PROGRAM, HOME_PROGRAM))),
    )
    proof = json.loads((package_dir / 'readback-results.json').read_text())
    if proof.get('pass') is not True:
        raise ResidentSessionError('fresh package readback validation failed')
    profile = load_new_eoat_profile()
    rows = []
    start = session.mono_clock()
    previous_sensor = None
    writer = session.writer
    writer._service_mode = True
    try:
        while session.mono_clock() - start < 10.0:
            pair = session._service_tick()
            if pair is None or session.mono_clock() - start < .5:
                continue
            output, sensor = pair
            if sensor is None or sensor.raw_wrench is None:
                raise ResidentSessionError('resident baseline requires raw Kunwei wrench')
            validate_sample(output, sensor.raw_wrench, sensor.observed_at_s,
                            session.mono_clock(), contract, profile, resident=True)
            if sensor.observed_at_s == previous_sensor:
                continue
            previous_sensor = sensor.observed_at_s
            rows.append({'robot': asdict(output), 'wrench_n_nm': list(sensor.raw_wrench),
                         'sensor_observed_monotonic_s': sensor.observed_at_s,
                         'poll_monotonic_s': session.mono_clock()})
    finally:
        writer._service_mode = False
    duration = session.mono_clock() - start
    if len(rows) < 100:
        raise ResidentSessionError('resident baseline has fewer than 100 distinct samples')
    session.verify_ready_for_next(reason='refresh_complete')
    last = writer._last_output
    row = {'payload': last.payload_kg, 'payload_cog': list(last.payload_cog_m),
           'tcp_offset': list(last.tcp_offset_m_rad),
           'actual_TCP_speed': list(last.tcp_speed_m_s_rad_s),
           'actual_TCP_pose': list(last.tcp_pose_m_rad), 'actual_q': list(last.q_rad),
           'actual_qd': list(last.qd_rad_s), 'observed_at_s': last.observed_at_s,
           'runtime_state': 2 if last.program_running else 1}

    def persist():
        capture = refresh_dir / 'baseline-frames.json'
        capture.write_text(json.dumps(rows, allow_nan=False) + '\n')
        values = np.asarray([r['wrench_n_nm'] for r in rows])
        baseline = {'schema': 'yield-software-baseline-v1',
                    'observed_at_s': rows[-1]['robot']['observed_at_s'],
                    'mean_wrench_n_nm': values.mean(axis=0).tolist(),
                    'std_wrench_n_nm': values.std(axis=0).tolist(),
                    'stationary': True, 'no_contact': True,
                    'no_contact_basis': 'same approved joint Home and uninterrupted bench observation',
                    'eoat_identity_sha256': profile.profile_sha256,
                    'capture_file': capture.name,
                    'capture_sha256': hashlib.sha256(capture.read_bytes()).hexdigest(),
                    'steady_samples': len(rows), 'claim_scope': 'software subtraction; no hardware tare'}
        _atomic_json(refresh_dir / 'software_baseline_receipt.json', baseline)
        _write_receipts(refresh_dir, row, proof, contract,
                        session_epoch=old.session_epoch,
                        resident_session_id=old.resident_session_id)
        return {'acquisition_duration_s': duration, 'initial_exclusion_s': .5,
                'sample_count': len(rows), 'variance_n2': values.var(axis=0).tolist(),
                'stationary': True, 'no_contact': True,
                'eoat_identity_sha256': profile.profile_sha256}
    baseline_meta = session._run_process_work(reason='baseline_refresh_seal', task=persist)
    renewed, _ = load_run_dir_receipts(refresh_dir, contract=contract,
                        route_id=old.route_id, attempt_id='resident-refresh',
                        now_s=session.wall_clock())
    return {'prerequisites': renewed, 'baseline': baseline_meta,
            'readback_validation': {'path': str(package_dir), **proof}}


def infrastructure_report(receipt, *, session_started_s, finished_s, live):
    """Assess six real units with full turnaround accounting; no Home-to-Home proxy."""
    import statistics
    attempts = receipt.get('attempts', [])
    spans = []
    units = []
    for attempt in attempts:
        timing = attempt.get('timing', {})
        elapsed = timing.get('turnaround_s', timing.get('through_seal_s'))
        units.append({'sequence': attempt.get('sequence'), **timing,
                      'full_turnaround_s': elapsed,
                      'evidence_eligible': attempt.get('evidence_eligible') is True})
        if timing.get('cycle_kind') == 'reuse' and elapsed is not None:
            spans.append(float(elapsed))
    median = statistics.median(spans) if spans else None
    lifecycle_ok = len(attempts) == 6 and all(
        a.get('evidence_eligible') is True and
        all(a.get('lifecycle', {}).get(k) is True
            for k in ('path_complete', 'home_verified', 'ready_for_next', 'sealed'))
        for a in attempts)
    fixed_parameters = all(
        a.get('parameter_binding', {}).get('Md_scalar') == 9.565272137974492 and
        a.get('parameter_binding', {}).get('Bd_scalar') == 693.6559295653944 and
        a.get('parameter_binding', {}).get('force_integral_limit_n_s') == .1 and
        a.get('parameter_binding', {}).get('force_integral_policy') == 'legacy-clamp-v1'
        for a in attempts)
    refresh_count = len(receipt.get('session', {}).get('refreshes', []))
    stopped = receipt.get('stop', {}).get('program_stopped') is True
    clean = not receipt.get('error') and not receipt.get('cleanup_errors')
    software_ok = lifecycle_ok and fixed_parameters and refresh_count >= 1 and stopped and clean
    return {'schema': 'tase.resident-infrastructure-acceptance-v1',
            'evidence_scope': 'live' if live else 'offline_synthetic_endpoints',
            'units': units, 'ordinary_reuse_median_s': median,
            'ordinary_reuse_samples': len(spans), 'target_median_s': 85.0,
            'campaign_total_wall_s': float(finished_s) - float(session_started_s),
            'cold_refresh_recovery_in_total': True, 'refresh_count': refresh_count,
            'lifecycle_passed': software_ok,
            'live_acceptance_passed': bool(live and software_ok and median is not None and median <= 85.0)}
