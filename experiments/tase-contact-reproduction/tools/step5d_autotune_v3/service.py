"""Offline-only systemd service shell for Step5d autotune v3.

The first v3 delivery deliberately owns no live bridge, TP, controller, or
motion process.  It verifies the frozen control contract, holds single-service
ownership, exposes the durable stop latch, and drains only derived postprocess
jobs.  A later live promotion must replace the explicit blocker under the
UR10e owner gates.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from .postprocess import DerivedPostprocessQueue
from .state import (
    CampaignPaths,
    StateError,
    control_lock,
    orchestration_fingerprint,
    physical_status,
    publish_service_state,
    read_stop_latch,
    set_stop_latch,
)


OFFLINE_BLOCKER = "offline_only_live_start_disabled"


class ServiceError(RuntimeError):
    """The offline v3 service cannot establish a fail-closed owner state."""


CheckProvider = Callable[[], Mapping[str, Any]]
PhaseProvider = Callable[[CampaignPaths], Mapping[str, Any]]


class _DerivedWorker:
    """Run non-safety analysis off the service lifecycle thread."""

    def __init__(self, queue: DerivedPostprocessQueue) -> None:
        self.queue = queue
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.details: dict[str, Any] = {
            "processed": 0,
            "complete": 0,
            "analysis_failed": 0,
            "worker_error": None,
        }
        self.thread = threading.Thread(
            target=self._run,
            name="step5d-v3-derived-postprocess",
            daemon=True,
        )

    def _run(self) -> None:
        while not self.stop.is_set():
            try:
                result = self.queue.run_pending(limit=8)
                with self.lock:
                    self.details["processed"] += result.total
                    self.details["complete"] += result.succeeded
                    self.details["analysis_failed"] += result.failed
                    self.details["worker_error"] = None
            except Exception as exc:
                with self.lock:
                    self.details["worker_error"] = f"{type(exc).__name__}:{exc}"
            self.stop.wait(0.05)

    @contextlib.contextmanager
    def running(self) -> Iterator["_DerivedWorker"]:
        self.thread.start()
        try:
            yield self
        finally:
            self.stop.set()
            self.thread.join(timeout=1.0)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return dict(self.details)


def _check_contract() -> Mapping[str, Any]:
    from .launcher import check_effective_config

    return check_effective_config()


class OfflineService:
    def __init__(
        self,
        *,
        experiment_root: Path,
        campaign_root: Path,
        check_provider: CheckProvider = _check_contract,
        phase_provider: PhaseProvider = physical_status,
        poll_interval_s: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        owned_child_commands: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        self.experiment_root = experiment_root.expanduser().absolute()
        self.paths = CampaignPaths(campaign_root)
        self.check_provider = check_provider
        self.phase_provider = phase_provider
        self.poll_interval_s = float(poll_interval_s)
        self.sleep = sleep
        self.owned_child_commands: dict[str, tuple[str, ...]] = {}
        self._shutdown_requested = False
        if self.poll_interval_s <= 0.0:
            raise ServiceError("poll interval must be positive")
        for name, command in (owned_child_commands or {}).items():
            if name not in {"runner", "bridge", "watchdog"}:
                raise ServiceError(f"unsupported owned child role: {name}")
            if (
                not isinstance(command, Sequence)
                or isinstance(command, (str, bytes))
                or not command
                or any(not isinstance(token, str) or not token for token in command)
            ):
                raise ServiceError(f"owned {name} command must be non-empty string tokens")
            self.owned_child_commands[name] = tuple(command)

    @contextlib.contextmanager
    def _owned_children(self) -> Iterator[dict[str, subprocess.Popen[bytes]]]:
        children: dict[str, subprocess.Popen[bytes]] = {}
        try:
            for name in sorted(self.owned_child_commands):
                process = subprocess.Popen(
                    self.owned_child_commands[name],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                )
                children[name] = process
                if process.poll() is not None:
                    raise ServiceError(f"owned {name} child exited during startup")
            yield children
        finally:
            for process in children.values():
                if process.poll() is None:
                    process.terminate()
            for process in children.values():
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2.0)

    @staticmethod
    def _child_details(
        children: Mapping[str, subprocess.Popen[bytes]],
    ) -> dict[str, Any]:
        pids: dict[str, int] = {}
        for name, process in children.items():
            returncode = process.poll()
            if returncode is not None:
                raise ServiceError(f"owned {name} child exited unexpectedly rc={returncode}")
            pids[name] = process.pid
        runner = children.get("runner")
        return {
            "runner_pid": os.getpid() if runner is None else runner.pid,
            "child_pids": pids,
            "child_start_count": {name: 1 for name in pids},
            "bridge_started": "bridge" in children,
            "watchdog_started": "watchdog" in children,
        }

    def _queue_details(self) -> dict[str, Any]:
        if not self.paths.candidate_plan.is_file():
            return {"revision": 0, "candidate_count": 0, "closed": False}
        from step5d_autotune_batch_plan import load_plan

        plan = load_plan(self.paths.candidate_plan)
        details = {
            "revision": plan.revision,
            "candidate_count": len(plan.candidates),
            "closed": plan.closed,
        }
        if self.paths.trial_overlays.is_file():
            from .state import read_strict_json

            overlays = read_strict_json(
                self.paths.trial_overlays, role="v3 trial overlay plan"
            )
            if (
                not isinstance(overlays, dict)
                or overlays.get("schema")
                != "step5d.autotune-v3/trial-overlay-plan-v1"
                or overlays.get("revision") != plan.revision
                or overlays.get("candidate_count") != len(plan.candidates)
            ):
                raise ServiceError("candidate and V3 trial-overlay plans are not coherent")
            details["trial_overlay_plan_fingerprint"] = overlays.get("fingerprint")
        elif plan.revision:
            raise ServiceError("candidate plan lacks its V3 trial-overlay plan")
        return details

    def _publish(
        self,
        *,
        instance_id: str,
        phase: str,
        control_fingerprint: str,
        orchestration_fingerprint_value: str,
        primary_blocker: str | None,
        details: Mapping[str, Any],
    ) -> None:
        publish_service_state(
            self.paths,
            instance_id=instance_id,
            phase=phase,
            control_fingerprint=control_fingerprint,
            orchestration_fingerprint=orchestration_fingerprint_value,
            primary_blocker=primary_blocker,
            details={
                "mode": "offline_control_plane",
                "bridge_started": False,
                "controller_touched": False,
                "tp_started": False,
                "motion_allowed": False,
                **dict(details),
            },
        )

    def _install_signal_handlers(self) -> dict[int, Any]:
        previous: dict[int, Any] = {}

        def request_shutdown(_signum: int, _frame: Any) -> None:
            self._shutdown_requested = True

        try:
            for signum in (signal.SIGTERM, signal.SIGINT):
                previous[signum] = signal.getsignal(signum)
                signal.signal(signum, request_shutdown)
        except ValueError:
            return {}
        return previous

    @staticmethod
    def _restore_signal_handlers(previous: Mapping[int, Any]) -> None:
        for signum, handler in previous.items():
            signal.signal(signum, handler)

    def run(self, *, resume: bool = False, max_cycles: int | None = None) -> int:
        if max_cycles is not None and (type(max_cycles) is not int or max_cycles < 1):
            raise ServiceError("max_cycles must be a positive integer")
        report = dict(self.check_provider())
        if report.get("ok") is not True:
            raise ServiceError("effective control contract check did not pass")
        control_fingerprint = report.get("control_fingerprint")
        if not isinstance(control_fingerprint, str) or len(control_fingerprint) != 64:
            raise ServiceError("control contract report lacks a fingerprint")
        orchestration_fp = orchestration_fingerprint(self.experiment_root)
        postprocess = DerivedPostprocessQueue(
            self.paths.postprocess,
            allowed_capture_root=self.paths.root,
        )
        postprocess_worker = _DerivedWorker(postprocess)
        previous_handlers = self._install_signal_handlers()
        try:
            with (
                control_lock(self.paths, owner=True) as instance_id,
                self._owned_children() as children,
                postprocess_worker.running() as derived_worker,
            ):
                if resume and read_stop_latch(self.paths)["armed"]:
                    set_stop_latch(self.paths, armed=False)
                child_details = self._child_details(children)
                self._publish(
                    instance_id=instance_id,
                    phase="starting",
                    control_fingerprint=control_fingerprint,
                    orchestration_fingerprint_value=orchestration_fp,
                    primary_blocker=OFFLINE_BLOCKER,
                    details={
                        "resume_requested": resume,
                        "queue": self._queue_details(),
                        **child_details,
                    },
                )
                cycles = 0
                while True:
                    child_details = self._child_details(children)
                    physical = dict(self.phase_provider(self.paths))
                    latch = read_stop_latch(self.paths)
                    if physical.get("integrity_error"):
                        self._publish(
                            instance_id=instance_id,
                            phase="failed_closed",
                            control_fingerprint=control_fingerprint,
                            orchestration_fingerprint_value=orchestration_fp,
                            primary_blocker="physical_state_unavailable",
                            details={"physical": physical, **child_details},
                        )
                        return 78
                    if latch["armed"] and physical.get("safe_to_stop") is True:
                        self._publish(
                            instance_id=instance_id,
                            phase="stopped_after_current",
                            control_fingerprint=control_fingerprint,
                            orchestration_fingerprint_value=orchestration_fp,
                            primary_blocker="stop_after_current_complete",
                            details={
                                "physical": physical,
                                "stop_latch": latch,
                                **child_details,
                            },
                        )
                        return 0

                    postprocess_details = derived_worker.snapshot()
                    phase = "ready_home"
                    if physical.get("trial_active") is True:
                        phase = (
                            "stop_pending_active_trial"
                            if latch["armed"]
                            else "trial_active"
                        )
                    self._publish(
                        instance_id=instance_id,
                        phase=phase,
                        control_fingerprint=control_fingerprint,
                        orchestration_fingerprint_value=orchestration_fp,
                        primary_blocker=OFFLINE_BLOCKER,
                        details={
                            "physical": physical,
                            "queue": self._queue_details(),
                            "stop_latch": latch,
                            "derived_postprocess": postprocess_details,
                            **child_details,
                        },
                    )
                    cycles += 1
                    if self._shutdown_requested or (
                        max_cycles is not None and cycles >= max_cycles
                    ):
                        self._publish(
                            instance_id=instance_id,
                            phase="stopped",
                            control_fingerprint=control_fingerprint,
                            orchestration_fingerprint_value=orchestration_fp,
                            primary_blocker=OFFLINE_BLOCKER,
                            details={
                                "shutdown_requested": self._shutdown_requested,
                                "queue": self._queue_details(),
                                "last_cycle": {
                                    "physical": physical,
                                    "queue": self._queue_details(),
                                    "stop_latch": latch,
                                    "derived_postprocess": postprocess_details,
                                },
                                **child_details,
                            },
                        )
                        return 0
                    self.sleep(self.poll_interval_s)
        except StateError as exc:
            raise ServiceError(str(exc)) from exc
        finally:
            self._restore_signal_handlers(previous_handlers)


def run_service(
    experiment_root: Path,
    campaign_root: Path,
    *,
    resume: bool = False,
) -> int:
    return OfflineService(
        experiment_root=experiment_root,
        campaign_root=campaign_root,
    ).run(resume=resume)
