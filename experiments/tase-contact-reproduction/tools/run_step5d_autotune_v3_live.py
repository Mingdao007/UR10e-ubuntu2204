#!/usr/bin/python3.10
"""Own one continuous V3 bridge + real-motion campaign after deterministic release gates."""

from __future__ import annotations

import argparse
import ctypes
import csv
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping

from step5d_autotune_state_machine import TpLoopState
from step5d_autotune_v3.dashboard import DashboardObservationError, dashboard_exchange
from step5d_autotune_v3.delivery_observation import (
    DeliveryObservationError,
    fresh_get_provenance,
    load_delivery_observation,
    validate_delivery_observation,
)
from step5d_autotune_v3.bridge_admission import validate_bridge_admission
from step5d_autotune_v3.launch_basis import (
    read_and_validate_launch_basis,
    validate_delivery_observation_binding,
    validate_strict_bridge_ready,
)
from step5d_autotune_v3.governance import (
    RUNTIME_OBSERVATION_INTERVAL_S,
    load_current_release_snapshot,
    resolve_governed_status,
)
from step5d_autotune_v3.launcher import build_bridge_argv, check_effective_config
from step5d_autotune_v3.profile import load_contract
from step5d_autotune_v3.release_contract import (
    release_contract_scope_for_release,
)
from step5d_autotune_v3.release_certificate import (
    certificate_path,
    load_release_certificate,
)
from step5d_autotune_v3.release_identity import (
    LAUNCH_PROFILE_PATH,
    SAFETY_ENVELOPE_PATH,
    ReleaseIdentity,
    load_current_release,
    load_runtime_release,
    release_payload_path,
)
from step5d_autotune_v3.runtime_environment import production_runtime_environment
from step5d_autotune_v3.runtime_installation import require_runtime_profile
from step5d_autotune_v3.runtime_profile import (
    CONTROL_PROFILE_ID,
    DEFAULT_OVERLAY,
    RELEASE_STAGE_ID,
    load_launch_profile,
    overlay_fingerprint,
)
from step5d_autotune_v3.runtime_gate import (
    ARM_GRANT_MAX_AGE_S,
    CampaignLease,
    RuntimeGateError,
    loaded_program_paths,
    process_starttime,
    publish_arm_observation,
    release_runtime_contract,
    revoke_arm_observation,
    write_campaign_lease,
)
from step5d_autotune_v3.runtime_observation import (
    RuntimeObservationError,
    RuntimeObservationPublisher,
)
from step5d_autotune_v3.state import atomic_json, read_strict_json
from step5d_parameter_queue import status as parameter_queue_status
from step5d_autotune_live_driver import AtomicCommandMailbox
from ur10e_parallel import ResourceProfile, writer_lease, writer_lease_owner
from step5d_bridge_status import (
    readiness_claim,
    resolve_status as resolve_bridge_status,
    verify_readiness_claim,
)


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "tools/run_step5d_autotune_v3_bridge.py"
RUNNER = ROOT / "tools/run_step5d_parameter_campaign.py"
RESULT_SCHEMA = "step5d.autotune-v3/live-campaign-launch-result-v1"
LIVE_PREFLIGHT_SCHEMA = "step5d.autotune-v3/live-preflight-snapshot-v3"
CANONICAL_LAUNCH_ENV = "STEP5D_V3_CANONICAL_LAUNCHER"
ARM_GATE_REFRESH_INTERVAL_S = ARM_GRANT_MAX_AGE_S * 0.4
RECOVERY_BACKOFF_S = 1.0
TERMINAL_STOP_OBSERVATION_BUDGET_S = 1.0
ARM_ACKNOWLEDGED_STATES = frozenset(
    {
        TpLoopState.ARMED,
        TpLoopState.RUN,
        TpLoopState.TERMINAL,
        TpLoopState.RETRACT,
        TpLoopState.RETURN,
        TpLoopState.HOME_VERIFY,
        TpLoopState.WAIT_ACK,
        TpLoopState.WAIT_INFRA_READY,
        TpLoopState.READY_NEAR,
        TpLoopState.READY_HOME_CLOSED,
        TpLoopState.READY_HOME_NEXT,
    }
)
class LiveLaunchError(RuntimeError):
    pass


LIVE_REQUIRED_IDENTITY_ARGS = (
    "output_root",
    "preflight",
    "delivery_observation",
    "admission",
    "authority_epoch",
    "launch_basis",
    "launch_basis_sha256",
    "campaign_prepare",
)


class LiveSessionState(str, Enum):
    RUNNING = "RUNNING"
    WAITING_FOR_PARAMETERS = "WAITING_FOR_PARAMETERS"
    WAITING_FOR_HOME = "WAITING_FOR_HOME"
    WAITING_FOR_HARDWARE = "WAITING_FOR_HARDWARE"
    RECOVERING = "RECOVERING"
    SHUTDOWN = "SHUTDOWN"


@dataclass
class LiveSessionLifecycle:
    """Own the receiver independently from any one bridge/runner session."""

    state: LiveSessionState = LiveSessionState.WAITING_FOR_PARAMETERS
    session_status: str = "WAITING_FOR_PARAMETERS"
    receiver_accepting: bool = True
    parameters_available: bool = False
    authoritative_home: bool = False
    safety_normal: bool = False
    bridge: Any | None = None
    runner: Any | None = None
    last_error: str | None = None
    transitions: list[str] = field(default_factory=list)
    explicit_shutdown: bool = False

    def _set_state(self, state: LiveSessionState, *, status: str | None = None) -> None:
        self.state = state
        self.session_status = status or state.value
        self.transitions.append(state.value)

    def _derive_ready_state(self) -> None:
        if not self.receiver_accepting:
            self._set_state(LiveSessionState.SHUTDOWN)
        elif not self.safety_normal:
            self._set_state(LiveSessionState.WAITING_FOR_HARDWARE)
        elif not self.authoritative_home:
            self._set_state(LiveSessionState.WAITING_FOR_HOME)
        elif not self.parameters_available:
            self._set_state(LiveSessionState.WAITING_FOR_PARAMETERS)
        else:
            self._set_state(LiveSessionState.RUNNING)

    def start_session(
        self,
        *,
        bridge: Any | None,
        runner: Any | None,
    ) -> None:
        self.bridge = bridge
        self.runner = runner
        self.last_error = None
        self.parameters_available = True
        self._set_state(LiveSessionState.WAITING_FOR_HOME)

    def observe(
        self,
        *,
        parameters_available: bool | None = None,
        authoritative_home: bool | None = None,
        safety_normal: bool | None = None,
        runner_exit: int | None = None,
        bridge_exit: int | None = None,
        error: BaseException | str | None = None,
    ) -> LiveSessionState:
        """Reduce one observation without stopping the persistent receiver."""

        if parameters_available is not None:
            self.parameters_available = parameters_available
        if authoritative_home is not None:
            self.authoritative_home = authoritative_home
        if safety_normal is not None:
            self.safety_normal = safety_normal
        runner_failed = runner_exit is not None and runner_exit != 0
        if error is not None or runner_failed or bridge_exit is not None:
            detail = error if error is not None else (
                f"runner exited rc={runner_exit}"
                if runner_exit is not None
                else f"bridge exited rc={bridge_exit}"
            )
            self.last_error = str(detail)
            if not self.safety_normal:
                self._set_state(LiveSessionState.WAITING_FOR_HARDWARE, status="DEGRADED")
            else:
                self._set_state(LiveSessionState.RECOVERING, status="DEGRADED")
            return self.state
        self._derive_ready_state()
        return self.state

    def request_shutdown(self) -> None:
        self.explicit_shutdown = True
        self.receiver_accepting = False
        self._set_state(LiveSessionState.SHUTDOWN)

    def cleanup_targets(
        self,
        *,
        explicit_shutdown: bool = False,
        failed_session: bool = False,
    ) -> tuple[Any | None, Any | None]:
        """Return this supervisor's session children, never the receiver."""

        if explicit_shutdown:
            self.request_shutdown()
        if not (self.explicit_shutdown or failed_session):
            return None, None
        return self.runner, self.bridge


def _run_recoverable_sessions(
    session_callable: Callable[[], Mapping[str, Any] | None],
    cleanup: Callable[[], None],
    backoff: Callable[[], None],
    *,
    lifecycle: LiveSessionLifecycle | None = None,
) -> Mapping[str, Any] | None:
    """Run sessions while keeping the durable receiver alive across failures.

    A normal return of ``None`` means that the durable queue is currently
    empty; it is therefore a wait condition, not supervisor shutdown.  A
    failed session is retried only after its owned children are cleaned up and
    a bounded backoff.  The next isolated session performs its own canonical
    preflight and fresh bridge/RTDE Home + safety gates before ARM.  The
    injectable callbacks keep this control flow directly testable without
    starting live children.
    """

    lifecycle = lifecycle or LiveSessionLifecycle()
    previous_handlers: dict[int, Any] = {}

    def request_signal_shutdown(signum: int, frame: Any) -> None:
        del signum, frame
        raise KeyboardInterrupt

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, request_signal_shutdown)
        except (ValueError, OSError):
            # A non-main-thread unit test cannot install Python signal
            # handlers; KeyboardInterrupt remains the shutdown path there.
            continue

    try:
        while True:
            try:
                result = session_callable()
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                cleanup()
                lifecycle.observe(error=exc)
                if lifecycle.state is not LiveSessionState.WAITING_FOR_HARDWARE:
                    lifecycle._set_state(LiveSessionState.RECOVERING, status="DEGRADED")
                lifecycle._set_state(LiveSessionState.WAITING_FOR_HARDWARE)
                backoff()
                continue

            if result is None:
                # The queue's durable records remain accepting/pending/inflight;
                # an empty queue must not turn into a receiver shutdown.
                lifecycle.parameters_available = False
                lifecycle._set_state(LiveSessionState.WAITING_FOR_PARAMETERS)
                backoff()
                continue
            return result
    except KeyboardInterrupt:
        lifecycle.request_shutdown()
        cleanup()
        raise
    finally:
        for signum, handler in previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                pass


def dispatch_single_session(
    session_callable: Callable[[], Mapping[str, Any] | None],
    cleanup: Callable[[], None],
) -> Mapping[str, Any] | None:
    """Run exactly one owned session for deterministic offline tests.

    This seam deliberately has no retry loop and creates no child process of
    its own.  The session callback owns any children it starts; on failure the
    supplied cleanup callback is invoked before the exception is propagated.
    """
    try:
        return session_callable()
    except BaseException:
        cleanup()
        raise


def _write_session_lifecycle(
    output_root: Path,
    lifecycle: LiveSessionLifecycle,
    *,
    reason: str,
) -> None:
    atomic_json(
        output_root / "session_lifecycle.json",
        {
            "schema": "step5d.autotune-v3/live-session-lifecycle-v1",
            "state": lifecycle.state.value,
            "session_status": lifecycle.session_status,
            "receiver_accepting": lifecycle.receiver_accepting,
            "reason": reason,
            "last_error": lifecycle.last_error,
            "transitions": list(lifecycle.transitions),
        },
    )


def _publish_canonical_readiness_claim(
    output_root: Path, required_state: str
) -> dict[str, Any]:
    try:
        status = resolve_bridge_status(ROOT)
        claim = readiness_claim(status, required_state)
        verify_readiness_claim(status, claim)
    except Exception as exc:
        raise LiveLaunchError(
            f"canonical readiness claim was not admitted: {type(exc).__name__}:{exc}"
        ) from exc
    atomic_json(output_root / "readiness-claim.json", claim)
    return claim


class _LatestCsvFollower:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().absolute()
        self.identity: tuple[int, int] | None = None
        self.offset = 0
        self.pending = b""
        self.fields: list[str] | None = None
        self.latest_row: dict[str, str] | None = None
        self.bytes_read = 0

    def _reset(self, identity: tuple[int, int]) -> None:
        self.identity = identity
        self.offset = 0
        self.pending = b""
        self.fields = None
        self.latest_row = None

    def poll(self) -> Mapping[str, str] | None:
        if self.path.is_symlink() or not self.path.is_file():
            return None
        try:
            stat = self.path.stat()
            identity = (stat.st_dev, stat.st_ino)
            if self.identity != identity or stat.st_size < self.offset:
                self._reset(identity)
            with self.path.open("rb") as handle:
                handle.seek(self.offset)
                appended = handle.read()
                self.offset = handle.tell()
        except OSError:
            return None
        self.bytes_read += len(appended)
        if not appended:
            return self.latest_row
        chunks = (self.pending + appended).split(b"\n")
        self.pending = chunks.pop()
        for encoded_line in chunks:
            if encoded_line.endswith(b"\r"):
                encoded_line = encoded_line[:-1]
            if not encoded_line:
                continue
            try:
                values = next(csv.reader([encoded_line.decode("utf-8")]))
            except (UnicodeError, csv.Error, StopIteration) as exc:
                raise LiveLaunchError(
                    f"bridge CSV contains an invalid complete row: {exc}"
                ) from exc
            if self.fields is None:
                if len(values) != len(set(values)) or any(not value for value in values):
                    raise LiveLaunchError("bridge CSV header is invalid")
                self.fields = values
                continue
            if len(values) != len(self.fields):
                raise LiveLaunchError("bridge CSV row cardinality differs")
            self.latest_row = dict(zip(self.fields, values, strict=True))
        return self.latest_row


def _require_canonical_launcher(environment: Mapping[str, str] | None = None) -> Path:
    values = os.environ if environment is None else environment
    expected = (ROOT / "scripts/step5d-autotune-v3.sh").resolve()
    observed = values.get(CANONICAL_LAUNCH_ENV, "")
    if not observed or Path(observed).expanduser().resolve() != expected:
        raise LiveLaunchError(
            "internal live worker is not a public entrypoint; use "
            f"{expected} bridge"
        )
    return expected


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _missing_live_identity_args(args: argparse.Namespace) -> list[str]:
    missing = [
        name
        for name in LIVE_REQUIRED_IDENTITY_ARGS
        if getattr(args, name, None) is None
    ]
    if getattr(args, "launch_basis_sha256", None) == "":
        missing.append("launch_basis_sha256")
    return sorted(set(missing))


def _require_live_identity_args(args: argparse.Namespace) -> None:
    missing = _missing_live_identity_args(args)
    if missing:
        raise LiveLaunchError(
            "live worker requires "
            + ", ".join(f"--{name.replace('_', '-')}" for name in missing)
        )


def _validate_coordinator_runtime_root(args: argparse.Namespace) -> Path:
    from run_step5d_autotune_v3_coordinator import validate_coordinator_runtime_root

    try:
        return validate_coordinator_runtime_root(args)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise LiveLaunchError(f"coordinator runtime root validation failed: {exc}") from exc


def _create_bridge_runtime(runtime_root: Path) -> tuple[Path, Path]:
    """Create the still-empty bridge subtree after identity validation."""

    bridge_run = runtime_root / "bridge"
    bridge_runtime = bridge_run / "runtime"
    if bridge_run.exists() or bridge_run.is_symlink():
        raise LiveLaunchError("bridge session state already exists")
    try:
        bridge_run.mkdir(mode=0o700, exist_ok=False)
        bridge_runtime.mkdir(mode=0o700, exist_ok=False)
    except OSError as exc:
        raise LiveLaunchError(f"bridge runtime could not be created: {exc}") from exc
    if (
        bridge_run.is_symlink()
        or not bridge_run.is_dir()
        or bridge_runtime.is_symlink()
        or not bridge_runtime.is_dir()
    ):
        raise LiveLaunchError("bridge runtime is not a real directory")
    return bridge_run, bridge_runtime


def _integer_row(row: Mapping[str, Any] | None, name: str) -> int:
    if row is None:
        return 0
    try:
        value = float(row[name])
    except (KeyError, TypeError, ValueError):
        return 0
    return int(value) if value.is_integer() else 0


def _play_identity_recheck_observation(
    gate_observation: Mapping[str, Any],
    delivery_observation: Mapping[str, Any],
) -> dict[str, Any]:
    if gate_observation.get("arm_permitted") is not True:
        raise LiveLaunchError("Play identity evidence does not permit ARM")
    controller = gate_observation.get("controller")
    rtde = gate_observation.get("rtde")
    if not isinstance(controller, Mapping) or not isinstance(rtde, Mapping):
        raise LiveLaunchError("Play identity evidence is incomplete")
    paths = loaded_program_paths(controller.get("loaded_program"))
    if len(paths) != 1:
        raise LiveLaunchError("Play identity evidence has no unique loaded program")
    triplet = delivery_observation.get("triplet_sha256")
    if not isinstance(triplet, Mapping) or set(triplet) != {".script", ".txt", ".urp"}:
        raise LiveLaunchError("Play identity evidence lacks the readback triplet")
    identity: dict[str, int] = {}
    for name in ("protocol_version", "digest_hi", "digest_lo"):
        value = rtde.get(name)
        if isinstance(value, bool) or not isinstance(value, int):
            raise LiveLaunchError(f"Play identity evidence lacks integer {name}")
        identity[name] = value
    return {
        "readback_triplet_sha256": dict(triplet),
        "loaded_program": next(iter(paths)),
        "tp_runtime_identity": identity,
    }


def _revoke_campaign_authority(
    gate_path: Path,
    *,
    lease: CampaignLease,
    lease_sha256: str,
    publisher: RuntimeObservationPublisher | None,
    reason: str,
) -> list[str]:
    if reason not in {"campaign_terminal", "supervisor_exit"}:
        raise ValueError("campaign authority revocation reason differs")
    errors: list[str] = []
    if publisher is not None:
        try:
            publisher.revoke_lease(
                observed_at_unix_ns=time.time_ns(),
                reason=reason,
            )
        except Exception as exc:
            errors.append(f"observation_lease:{type(exc).__name__}:{exc}")
    try:
        revoke_arm_observation(
            gate_path,
            lease=lease,
            lease_sha256=lease_sha256,
            reason_code=reason,
        )
    except Exception as exc:
        errors.append(f"arm_gate:{type(exc).__name__}:{exc}")
    return errors


def _release_triplet(release: ReleaseIdentity) -> dict[str, str]:
    return {
        suffix: str(release.artifacts[suffix]["sha256"])
        for suffix in (".script", ".txt", ".urp")
    }


def _release_contract_evidence_reference(
    experiment_root: Path,
    release: ReleaseIdentity,
) -> dict[str, str]:
    certificate_root = experiment_root / "runs/step5d_autotune_v3"
    scope = release_contract_scope_for_release(experiment_root, release)
    _certificate, evidence, _contract = load_release_certificate(
        certificate_root,
        certificate_path(certificate_root, scope),
        expected_scope=scope,
    )
    return {
        "path": str(evidence),
        "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
    }


def _immutable_trial_bundles(campaign_root: Path) -> set[Path]:
    root = campaign_root.resolve(strict=True)
    bundles: set[Path] = set()
    for path in root.rglob("immutable_trial_bundle.json"):
        if path.is_symlink() or not path.is_file():
            raise LiveLaunchError("trial evidence must be a real file")
        resolved = path.resolve(strict=True)
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise LiveLaunchError("trial evidence escapes campaign root") from exc
        bundles.add(resolved)
    return bundles


def _dashboard_external_blocker(
    campaign_root: Path,
    error: BaseException,
) -> dict[str, Any]:
    observed_at = time.time_ns()
    evidence = (
        campaign_root
        / "governance"
        / "runtime_blockers"
        / f"dashboard-{observed_at}.json"
    )
    atomic_json(
        evidence,
        {
            "schema": "step5d.autotune-v3/external-blocker-evidence-v1",
            "reason_code": "DASHBOARD_UNREACHABLE",
            "observed_at_unix_ns": observed_at,
            "error_type": type(error).__name__,
            "detail": str(error),
        },
    )
    return {
        "reason_code": "DASHBOARD_UNREACHABLE",
        "observed_at_unix_ns": observed_at,
        "evidence": evidence,
    }


def _mailbox_observation(
    reader: AtomicCommandMailbox,
    row: Mapping[str, Any] | None,
    tracker: dict[str, Any],
) -> tuple[dict[str, Any], Any | None]:
    command = reader.read_latest()
    pending: int | None = None
    if command is not None:
        sequence = int(command.packet.command_seq)
        prior = tracker["seen"].get(sequence)
        if prior not in (None, command.sha256) or (
            tracker["seen"] and sequence < max(tracker["seen"])
        ):
            tracker["duplicate"] = True
        tracker["seen"][sequence] = command.sha256
        if (
            command.packet.command.name == "ARM"
            and _integer_row(row, "ur_output_int_register_30") < sequence
        ):
            pending = sequence
    return (
        {
            "observed_at_unix_ns": time.time_ns(),
            "pending_arm_sequence": pending,
            "duplicate_arm_detected": bool(tracker["duplicate"]),
        },
        command,
    )


def _update_observed_lifecycle(
    publisher: RuntimeObservationPublisher,
    *,
    row: Mapping[str, Any] | None,
    command: Any | None,
    campaign_root: Path,
    preexisting_bundles: set[Path],
) -> None:
    if command is not None and command.packet.command.name == "ARM":
        sequence = int(command.packet.command_seq)
        acknowledged = (
            _integer_row(row, "ur_output_int_register_26")
            in ARM_ACKNOWLEDGED_STATES
            and _integer_row(row, "ur_output_int_register_30") == sequence
        )
        if acknowledged and publisher.events["first_arm_ack"] is None:
            publisher.update_lifecycle(
                "first_arm_ack", observed_at_unix_ns=time.time_ns(), sequence=sequence
            )

    new_bundles = sorted(_immutable_trial_bundles(campaign_root) - preexisting_bundles)
    if new_bundles and publisher.events["trial_completion"] is None:
        bundle = new_bundles[0]
        payload = read_strict_json(bundle, role="completed trial bundle")
        trial = payload.get("trial") if isinstance(payload, Mapping) else None
        trial_id = trial.get("trial_uid") if isinstance(trial, Mapping) else None
        if not isinstance(trial_id, str) or not trial_id:
            raise LiveLaunchError("completed trial bundle lacks trial identity")
        publisher.update_lifecycle(
            "trial_completion",
            observed_at_unix_ns=time.time_ns(),
            trial_id=trial_id,
            evidence=bundle,
        )

    if (
        command is not None
        and command.packet.command.name == "ARM"
        and publisher.events["trial_completion"] is not None
        and publisher.events["next_arm_ack"] is None
    ):
        sequence = int(command.packet.command_seq)
        first = publisher.events["first_arm_ack"]
        if (
            first is not None
            and sequence != first["sequence"]
            and _integer_row(row, "ur_output_int_register_26")
            in ARM_ACKNOWLEDGED_STATES
            and _integer_row(row, "ur_output_int_register_30") == sequence
        ):
            publisher.update_lifecycle(
                "next_arm_ack", observed_at_unix_ns=time.time_ns(), sequence=sequence
            )


def _publish_runtime_observation(
    publisher: RuntimeObservationPublisher,
    *,
    release: ReleaseIdentity,
    bridge: subprocess.Popen[Any],
    runner: subprocess.Popen[Any],
    bridge_csv: _LatestCsvFollower,
    bridge_ready: Mapping[str, Any],
    robot_host: str,
    mailbox_reader: AtomicCommandMailbox,
    mailbox_tracker: dict[str, Any],
    campaign_root: Path,
    preexisting_bundles: set[Path],
    dashboard: Mapping[str, Any] | None = None,
    delivery_observation: Mapping[str, Any],
    runner_exit_code: int | None = None,
) -> dict[str, Any]:
    shell_pid_text = os.environ.get("STEP5D_V3_SHELL_PID")
    try:
        canonical_shell_pid = int(shell_pid_text or "")
    except ValueError as exc:
        raise LiveLaunchError("canonical shell PID binding is missing") from exc
    if canonical_shell_pid <= 0 or canonical_shell_pid != os.getppid():
        raise LiveLaunchError("canonical shell is not the live supervisor parent")
    if bridge.poll() is not None:
        raise LiveLaunchError("bridge exited before runtime observation")
    observed_runner_exit = runner.poll()
    if runner_exit_code is None:
        if observed_runner_exit is not None:
            raise LiveLaunchError("campaign runner exited before runtime observation")
        exited_processes = None
    else:
        if runner_exit_code != 0 or observed_runner_exit != runner_exit_code:
            raise LiveLaunchError("terminal runner exit observation differs")
        exited_processes = {"campaign_runner": runner_exit_code}
    row = bridge_csv.poll()
    mailbox, command = _mailbox_observation(mailbox_reader, row, mailbox_tracker)
    _update_observed_lifecycle(
        publisher,
        row=row,
        command=command,
        campaign_root=campaign_root,
        preexisting_bundles=preexisting_bundles,
    )
    external_blocker = None
    if dashboard is not None:
        observed_dashboard = dashboard
    else:
        try:
            observed_dashboard = dashboard_exchange(
                robot_host,
                ["programState", "safetymode", "get loaded program"],
                timeout=2.0,
            )
        except (DashboardObservationError, OSError) as exc:
            observed_dashboard = None
            external_blocker = _dashboard_external_blocker(campaign_root, exc)
    fields = bridge_ready.get("rtde_output_fields")
    types = bridge_ready.get("rtde_output_types")
    if not isinstance(fields, list) or not isinstance(types, list):
        raise LiveLaunchError("bridge readiness lacks the negotiated RTDE recipe")
    bridge_starttime = process_starttime(bridge.pid)
    if bridge_starttime <= 0:
        raise LiveLaunchError("bridge process starttime is unavailable")
    lease_owner = writer_lease_owner(ResourceProfile.from_env())
    if (
        not isinstance(lease_owner, Mapping)
        or lease_owner.get("pid") != os.getpid()
        or lease_owner.get("starttime_ticks") != process_starttime(os.getpid())
        or lease_owner.get("task") != "step5d-autotune-v3-production-bridge"
    ):
        raise LiveLaunchError("exclusive production writer lease ownership was lost")
    try:
        publisher.publish(
            bridge_pid=bridge.pid,
            bridge_starttime_ticks=bridge_starttime,
            bridge_csv=bridge_csv.path,
            process_pids={
                "canonical_launcher": canonical_shell_pid,
                "launcher_supervisor": os.getpid(),
                "bridge_wrapper": bridge.pid,
                "campaign_runner": runner.pid,
            },
            writer_pids=[bridge.pid],
            uploaded_triplet_sha256=delivery_observation["triplet_sha256"],
            readback_triplet_sha256=delivery_observation["triplet_sha256"],
            delivery_observation=delivery_observation,
            dashboard_result=observed_dashboard,
            controller_observed_at_unix_ns=(
                None if external_blocker is not None else time.time_ns()
            ),
            rtde_output_fields=fields,
            rtde_output_types=types,
            mailbox=mailbox,
            external_blocker=external_blocker,
            exited_processes=exited_processes,
        )
    except RuntimeObservationError as exc:
        raise LiveLaunchError(f"runtime observation failed closed: {exc}") from exc
    return resolve_governed_status(ROOT, campaign_root)


def _runtime_playing_normal(row: Mapping[str, str] | None) -> bool:
    if row is None:
        return False
    try:
        return (
            int(float(row["ur_runtime_state"])) == 2
            and int(float(row["ur_safety_mode"])) == 1
        )
    except (KeyError, TypeError, ValueError):
        return False


def _refresh_arm_gate(
    gate_path: Path,
    *,
    lease: CampaignLease,
    lease_sha256: str,
    bridge: subprocess.Popen[Any],
    csv_follower: _LatestCsvFollower,
    robot_host: str,
    runtime_contract: Mapping[str, Any],
    mailbox_reader: AtomicCommandMailbox,
    delivery_observation: Mapping[str, Any],
    release: ReleaseIdentity,
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    try:
        while True:
            validated_delivery = validate_delivery_observation(
                ROOT, delivery_observation, release=release
            )
            fresh_get = fresh_get_provenance(validated_delivery)
            if bridge.poll() is not None:
                raise LiveLaunchError(
                    f"bridge exited before ARM gate refresh rc={bridge.returncode}"
                )
            command_before = mailbox_reader.read_latest()
            command_sha_before = (
                None if command_before is None else command_before.sha256
            )
            arm_command = (
                command_before.arm_gate_binding
                if command_before is not None
                and command_before.packet.command.name == "ARM"
                else None
            )
            command_observed_at_unix_ns = (
                time.time_ns() if arm_command is not None else None
            )
            dashboard = _wait_dashboard_observation(
                bridge,
                robot_host=robot_host,
            )
            row = _wait_arm_rtde_row(
                bridge,
                csv_follower=csv_follower,
                not_before_unix_ns=command_observed_at_unix_ns,
            )
            command_after = mailbox_reader.read_latest()
            command_sha_after = (
                None if command_after is None else command_after.sha256
            )
            if command_sha_after != command_sha_before:
                time.sleep(0.005)
                continue
            try:
                connection_epoch = int(row.get("rtde_reconnects", 0))
            except (TypeError, ValueError) as exc:
                raise LiveLaunchError(
                    "ARM gate RTDE connection epoch is invalid"
                ) from exc
            if connection_epoch < 0:
                raise LiveLaunchError("ARM gate RTDE connection epoch is negative")
            rtde_row_wall_ns = None
            if row.get("t_wall_ns") not in (None, ""):
                try:
                    rtde_row_wall_ns = int(row["t_wall_ns"])
                except (TypeError, ValueError) as exc:
                    raise LiveLaunchError(
                        "ARM gate RTDE row wall timestamp is invalid"
                    ) from exc
            csv_age_s = max(0.0, time.time() - csv_follower.path.stat().st_mtime)
            observed = publish_arm_observation(
                gate_path,
                lease=lease,
                lease_sha256=lease_sha256,
                bridge_pid=bridge.pid,
                dashboard=dashboard,
                rtde_row=row,
                contract=runtime_contract,
                csv_age_s=csv_age_s,
                delivery_transaction_id=fresh_get["transaction_id"],
                fresh_get_observed_at_unix_ns=fresh_get[
                    "observed_at_unix_ns"
                ],
                arm_command=arm_command,
                command_observed_at_unix_ns=command_observed_at_unix_ns,
                rtde_row_wall_ns=rtde_row_wall_ns,
                connection_epoch=connection_epoch,
            )
            break
    except (
        DeliveryObservationError,
        OSError,
        RuntimeError,
        RuntimeGateError,
    ) as exc:
        raise LiveLaunchError(f"ARM gate observation failed: {exc}") from exc
    if observed.get("arm_permitted") is not True:
        raise LiveLaunchError(
            "ARM gate predicates failed: "
            + ",".join(map(str, observed.get("reason_codes") or ["unknown"]))
        )
    return observed, dashboard


def _wait_dashboard_observation(
    bridge: subprocess.Popen[Any],
    *,
    robot_host: str,
    poll_interval_s: float = 0.05,
    exchange: Callable[..., Mapping[str, Any]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Mapping[str, Any]:
    """Retry bounded socket attempts until evidence or process exit."""

    observe = dashboard_exchange if exchange is None else exchange
    while True:
        if bridge.poll() is not None:
            raise LiveLaunchError(
                f"bridge exited before Dashboard observation rc={bridge.returncode}"
            )
        try:
            return observe(
                robot_host,
                ["programState", "safetymode", "get loaded program"],
                timeout=2.0,
            )
        except (DashboardObservationError, OSError):
            sleep(poll_interval_s)


def _wait_arm_rtde_row(
    bridge: subprocess.Popen[Any],
    *,
    csv_follower: _LatestCsvFollower,
    not_before_unix_ns: int | None,
    poll_interval_s: float = 0.005,
    sleep: Callable[[float], None] = time.sleep,
) -> Mapping[str, str]:
    """Wait for explicit fresh RTDE evidence; elapsed time is never an outcome."""

    while True:
        if bridge.poll() is not None:
            raise LiveLaunchError(
                "bridge exited while awaiting an ARM-gate RTDE row"
            )
        candidate = csv_follower.poll()
        if candidate is not None:
            if not_before_unix_ns is None:
                return candidate
            try:
                row_wall_ns = int(candidate["t_wall_ns"])
            except (KeyError, TypeError, ValueError) as exc:
                raise LiveLaunchError(
                    "ARM gate RTDE row lacks an integer wall timestamp"
                ) from exc
            if row_wall_ns >= not_before_unix_ns:
                return candidate
        sleep(poll_interval_s)


def _announce_stop_if_playing(robot_host: str) -> bool:
    try:
        state = dashboard_exchange(robot_host, ["programState"], timeout=2.0)
    except Exception:
        state = {}
    if str(state.get("programState", "")).startswith("PLAYING"):
        print("ACTION_REQUIRED_PRESS_TP_STOP_NOW", flush=True)
        return True
    return False


def _wait_file(
    path: Path,
    process: subprocess.Popen[Any],
    role: str,
    *,
    poll_interval_s: float = 0.05,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Wait for readiness or explicit process exit, without a wall-clock deadline."""

    while True:
        if process.poll() is not None:
            raise LiveLaunchError(f"{role} process exited before readiness rc={process.returncode}")
        if path.is_file():
            return
        sleep(poll_interval_s)


def _terminate(process: subprocess.Popen[Any] | None) -> int | None:
    if process is None:
        return None
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=0.25)
    return process.returncode


def _run_bridge_command_and_wait_for_readiness(
    command: list[str],
    *,
    bridge_ready_path: Path,
    timeout_s: float,
    role: str,
    **popen_kwargs: Any,
) -> tuple[subprocess.Popen[Any], int, int]:
    """Start one owned bridge child and require a bounded readiness artifact."""
    launch_started_ns = time.perf_counter_ns()
    process = subprocess.Popen(command, **popen_kwargs)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise LiveLaunchError(
                f"{role} process exited before readiness rc={process.returncode}"
            )
        if bridge_ready_path.is_file():
            return process, launch_started_ns, time.perf_counter_ns()
        time.sleep(0.05)
    _terminate(process)
    raise LiveLaunchError(f"{role} readiness timeout")


def _parent_death_guard(
    expected_parent_pid: int,
    expected_parent_starttime: int,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, int(signal.SIGTERM), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG) failed")
    if (
        os.getppid() != expected_parent_pid
        or process_starttime(expected_parent_pid) != expected_parent_starttime
    ):
        os.kill(os.getpid(), signal.SIGTERM)


def _program_stopped(result: Mapping[str, Any]) -> bool:
    state = str(result.get("programState", result.get("program_state", ""))).upper()
    return "STOPPED" in state


def _should_request_program_stop(
    *, campaign_completed: bool, explicit_shutdown: bool
) -> bool:
    """Allow TP Stop only for terminal completion or explicit user shutdown."""

    return campaign_completed or explicit_shutdown


def _stop_v3_program(
    robot_host: str,
    *,
    timeout_s: float = TERMINAL_STOP_OBSERVATION_BUDGET_S,
    poll_interval_s: float = 0.1,
    exchange: Callable[..., Mapping[str, Any]] = dashboard_exchange,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Observe terminal operator-owned TP Stop; never affect an active campaign."""

    if timeout_s <= 0.0 or poll_interval_s <= 0.0:
        raise ValueError("program-stop timeout and poll interval must be positive")
    observations: list[dict[str, Any]] = []
    query_errors: list[str] = []

    def observe() -> Mapping[str, Any]:
        result = exchange(robot_host, ["programState"], timeout=2.0)
        observations.append(dict(result))
        return result

    try:
        initial = observe()
    except Exception as exc:
        initial = {}
        query_errors.append(f"{type(exc).__name__}:{exc}")
    if _program_stopped(initial):
        return {
            "ok": True,
            "method": "observed_stopped_after_bridge_shutdown",
            "stop_request": None,
            "observations": observations,
            "query_errors": query_errors,
        }
    deadline = monotonic() + timeout_s
    while monotonic() < deadline:
        sleep(min(poll_interval_s, max(0.0, deadline - monotonic())))
        try:
            observed = observe()
        except Exception as exc:
            query_errors.append(f"{type(exc).__name__}:{exc}")
            continue
        if _program_stopped(observed):
            return {
                "ok": True,
                "method": "observed_stopped_after_operator_stop",
                "stop_request": None,
                "observations": observations,
                "query_errors": query_errors,
            }
    return {
        "ok": False,
        "method": "tp_stop_required",
        "stop_request": None,
        "observations": observations,
        "query_errors": query_errors,
        "required_operator_action": "PRESS_TP_STOP",
    }


def _validate_preflight(
    path: Path,
    release_identity: ReleaseIdentity,
    launch_profile: Any | None = None,
) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise LiveLaunchError("fresh V3 live preflight snapshot is required")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LiveLaunchError(f"V3 live preflight is invalid: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != LIVE_PREFLIGHT_SCHEMA:
        raise LiveLaunchError("V3 live preflight schema differs")
    if payload.get("ok") is not True or payload.get("fresh") is not True:
        raise LiveLaunchError("V3 live preflight did not pass freshly")
    expected: dict[str, Any] = {
        "candidate_stage_id": release_identity.release_stage_id,
        "control_profile_id": release_identity.control_profile_id,
        "tp_program_id": release_identity.program_id,
    }
    runtime_contract = release_runtime_contract(ROOT, release_identity)
    expected.update(
        {
            "release_manifest_sha256": release_identity.manifest_sha256,
            "expected_loaded_program": runtime_contract[
                "expected_loaded_program"
            ],
            "tp_runtime_identity": runtime_contract["tp_runtime_identity"],
            "launch_profile": {
                "path": LAUNCH_PROFILE_PATH,
                "sha256": release_identity.generated_files[
                    LAUNCH_PROFILE_PATH
                ],
            },
        }
    )
    if launch_profile is not None:
        expected["launch_profile_fingerprint"] = launch_profile.fingerprint
    for key, value in expected.items():
        if payload.get(key) != value:
            raise LiveLaunchError(f"V3 live preflight {key} differs")
    predicates = payload.get("predicates") or {}
    required = {
        "safety_normal",
        "program_safe_for_bridge",
        "robot_stationary",
        "prealign_start_clearance",
        "no_existing_writer",
        "mailbox_initial_zero",
        "runtime_dependencies",
        "controller_delivery",
    }
    if set(predicates) != required or not all(
        isinstance(predicates[name], dict) and predicates[name].get("ok") is True
        for name in required
    ):
        raise LiveLaunchError("V3 live preflight predicates are incomplete")
    return payload


def _validate_active_launch_identity(
    args: argparse.Namespace,
    release: ReleaseIdentity,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Consume the coordinator artifacts before any live child is spawned."""
    try:
        _require_live_identity_args(args)
        _validate_coordinator_runtime_root(args)
        if args.campaign_prepare is None:
            raise LiveLaunchError("coordinator campaign preparation artifact is required")
        basis = read_and_validate_launch_basis(
            args.launch_basis,
            owner_pid=args.canonical_owner_pid,
            owner_starttime=args.canonical_owner_starttime,
            expected_basis_sha256=args.launch_basis_sha256,
        )
        if basis["authority_epoch"] != args.authority_epoch:
            raise LiveLaunchError("launch basis authority epoch differs")
        admission = validate_bridge_admission(
            ROOT,
            read_strict_json(args.admission, role="bridge admission"),
            release=release,
        )
        if admission.get("campaign_fingerprint") != basis["campaign_fingerprint"]:
            raise LiveLaunchError("bridge admission campaign identity differs")
        validate_delivery_observation_binding(
            args.delivery_observation,
            basis=basis,
            admission=admission,
            experiment_root=ROOT,
        )
        from run_step5d_autotune_v3_coordinator import _validate_campaign_prepare

        campaign = _validate_campaign_prepare(
            read_strict_json(args.campaign_prepare, role="campaign preparation"),
            basis,
        )
        return basis, admission, campaign
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        if isinstance(exc, LiveLaunchError):
            raise
        raise LiveLaunchError(f"active launch identity validation failed: {exc}") from exc


def run(args: argparse.Namespace) -> Mapping[str, Any]:
    runtime_pointer = getattr(args, "_runtime_pointer", None)
    if not isinstance(runtime_pointer, Mapping):
        runtime_pointer = require_runtime_profile("control")
    return _run_live(args, runtime_pointer)


def _run_live(
    args: argparse.Namespace,
    runtime_pointer: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Own the durable receiver and re-enter isolated live session attempts."""

    _require_live_identity_args(args)
    output_root = args.output_root.expanduser().absolute()
    output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lifecycle = LiveSessionLifecycle()
    attempt_number = 0

    def session_callable() -> Mapping[str, Any] | None:
        nonlocal attempt_number
        attempt_number += 1
        attempt_root = output_root / f"attempt-{attempt_number:04d}"
        attempt_root.mkdir(parents=False, exist_ok=False, mode=0o700)
        attempt_args = argparse.Namespace(**vars(args))
        attempt_args._coordinator_output_root = output_root
        attempt_args.output_root = attempt_root
        atomic_json(
            output_root / "recoverable_session_status.json",
            {
                "schema": "step5d.autotune-v3/recoverable-session-status-v1",
                "attempt": attempt_number,
                "state": "RUNNING",
                "attempt_root": str(attempt_root),
            },
        )
        try:
            result = _run_live_session(attempt_args, runtime_pointer)
        except BaseException as exc:
            atomic_json(
                output_root / "recoverable_session_status.json",
                {
                    "schema": "step5d.autotune-v3/recoverable-session-status-v1",
                    "attempt": attempt_number,
                    "state": (
                        "SHUTDOWN"
                        if isinstance(exc, KeyboardInterrupt)
                        else "RECOVERING"
                    ),
                    "attempt_root": str(attempt_root),
                    "error": f"{type(exc).__name__}:{exc}",
                },
            )
            raise
        atomic_json(
            output_root / "recoverable_session_status.json",
            {
                "schema": "step5d.autotune-v3/recoverable-session-status-v1",
                "attempt": attempt_number,
                "state": "COMPLETED",
                "attempt_root": str(attempt_root),
            },
        )
        return result

    def cleanup() -> None:
        # _run_live_session owns and terminates its runner + bridge in its
        # finally block.  This injectable hook records that the durable
        # receiver remains alive while the wrapper backs off before creating
        # the next isolated session.  Readiness belongs to that new session.
        atomic_json(
            output_root / "recoverable_session_status.json",
            {
                "schema": "step5d.autotune-v3/recoverable-session-status-v1",
                "attempt": attempt_number,
                "state": (
                    "SHUTDOWN"
                    if lifecycle.explicit_shutdown
                    else "WAITING_FOR_HARDWARE"
                ),
                "receiver_accepting": lifecycle.receiver_accepting,
            },
        )

    if getattr(args, "single_session", False):
        return dispatch_single_session(session_callable, cleanup)
    return _run_recoverable_sessions(
        session_callable,
        cleanup=cleanup,
        backoff=lambda: time.sleep(RECOVERY_BACKOFF_S),
        lifecycle=lifecycle,
    )


def _run_live_session(
    args: argparse.Namespace,
    runtime_pointer: Mapping[str, Any],
) -> Mapping[str, Any]:
    from prepare_step5d_autotune_launch import write_machine_campaign_binding
    from run_step5d_autotune_v3_bridge import TICKET_SCHEMA, TICKET_SCOPE

    control_python = runtime_pointer["profiles"]["control"]["python_executable"]
    release = load_runtime_release(ROOT)
    delivery_observation = load_delivery_observation(
        ROOT, args.delivery_observation, release=release
    )
    legacy_preflight = None
    runtime_root = _validate_coordinator_runtime_root(args)
    basis, admission, campaign_prepare = _validate_active_launch_identity(args, release)
    bridge_run, bridge_runtime = _create_bridge_runtime(runtime_root)
    contract_path = release_payload_path(ROOT, release, SAFETY_ENVELOPE_PATH)
    launch_profile_path = release_payload_path(ROOT, release, LAUNCH_PROFILE_PATH)
    contract = load_contract(contract_path)
    launch_profile = load_launch_profile(
        launch_profile_path,
        contract=contract,
        expected_tp_program_id=release.program_id,
    )
    check = check_effective_config(
        runtime_root=runtime_root,
        contract_path=contract_path,
        launch_profile_path=launch_profile_path,
        expected_tp_program_id=release.program_id,
        trial_overlay=DEFAULT_OVERLAY,
    )
    command = [
        control_python,
        str(WRAPPER),
        *build_bridge_argv(
            runtime_root,
            contract=contract,
            launch_profile=launch_profile,
            trial_overlay=DEFAULT_OVERLAY,
        )[2:],
    ]
    robot_host = str(check["effective_config"]["robot_host"])
    preflight = _validate_preflight(
        args.preflight,
        release,
        launch_profile,
    )
    campaign_binding = bridge_runtime / "campaign_binding.json"
    launch_plan_path = bridge_runtime / "campaign_launch_plan.json"
    prepared = dict(campaign_prepare["result"])
    plan_path = Path(str(prepared["candidate_plan"]))
    source_path = Path(str(prepared["trial_overlay_plan"]))
    plan = read_strict_json(plan_path, role="parameter receiver plan")
    plan_revision = plan.get("revision") if isinstance(plan, Mapping) else None
    if (
        isinstance(plan_revision, bool)
        or not isinstance(plan_revision, int)
        or plan_revision < 1
    ):
        raise LiveLaunchError("parameter receiver plan revision is invalid")
    machine_binding = write_machine_campaign_binding(
        campaign_binding,
        campaign_id=str(prepared["campaign_id"]),
        campaign_epoch=int(prepared["campaign_epoch"]),
        campaign_fingerprint=str(prepared["campaign_fingerprint"]),
        candidate_plan_path=plan_path,
        trial_overlay_plan_path=source_path,
        binding_source="canonical_v3_live_entrypoint",
    )
    prepared = {
        **prepared,
        "machine_binding_status": "finalized_parameter_receiver",
        "machine_binding_sha256": _sha256_path(campaign_binding),
    }
    atomic_json(launch_plan_path, prepared)
    receiver_root = Path(str(prepared["receiver_root"]))
    inherited_launch_id = os.environ.get("STEP5D_V3_LAUNCH_ATTEMPT_ID")
    if inherited_launch_id is not None and inherited_launch_id != basis["launch_nonce"]:
        raise LiveLaunchError("canonical launch-attempt ID differs from launch basis")
    launch_id = str(basis["launch_nonce"])
    runtime_contract = release_runtime_contract(ROOT, release)
    lease = CampaignLease.issue(
        lease_id=uuid.uuid4().hex,
        launch_id=launch_id,
        manifest_sha256=release.manifest_sha256,
        release_stage_id=release.release_stage_id,
        program_id=release.program_id,
        protocol_id=release.protocol_id,
        campaign_id=str(prepared["campaign_id"]),
        campaign_epoch=int(prepared["campaign_epoch"]),
        campaign_fingerprint=str(prepared["campaign_fingerprint"]),
        safety_envelope_sha256=runtime_contract["safety_envelope_sha256"],
    )
    lease_path = bridge_runtime / "campaign_lease.json"
    lease_sha256 = write_campaign_lease(lease_path, lease)
    arm_gate_path = bridge_runtime / "arm_gate.json"
    ticket = {
        "schema": TICKET_SCHEMA,
        "parent_pid": os.getpid(),
        "argv_sha256": _sha256_json(command[2:]),
        "launch_id": launch_id,
        "scope": TICKET_SCOPE,
        "launch_profile": {
            "path": str(launch_profile_path),
            "sha256": release.generated_files[LAUNCH_PROFILE_PATH],
        },
        "launch_profile_fingerprint": launch_profile.fingerprint,
        "trial_overlay_fingerprint": overlay_fingerprint(launch_profile, DEFAULT_OVERLAY),
        "release_stage_id": release.release_stage_id,
        "control_profile_id": release.control_profile_id,
        "tp_program_id": release.program_id,
        "manifest_sha256": release.manifest_sha256,
        "safety_envelope_sha256": runtime_contract["safety_envelope_sha256"],
        "launch_basis": {
            "path": str(args.launch_basis),
            "sha256": basis["basis_sha256"],
        },
        "delivery_observation": {
            "path": str(args.delivery_observation),
            "sha256": basis["delivery_observation_sha256"],
        },
        "authority_epoch": basis["authority_epoch"],
        "campaign_prepare": {
            "path": str(args.campaign_prepare),
            "sha256": _sha256_path(args.campaign_prepare),
        },
        "campaign_binding": {
            "campaign_id": prepared["campaign_id"],
            "campaign_epoch": prepared["campaign_epoch"],
            "campaign_fingerprint": prepared["campaign_fingerprint"],
            "candidate_plan_revision": plan_revision,
            "candidate_plan_sha256": _sha256_path(plan_path),
            "trial_overlay_plan_sha256": _sha256_path(source_path),
            "machine_binding_sha256": _sha256_path(campaign_binding),
        },
        "campaign_lease": {
            "path": str(lease_path),
            "sha256": lease_sha256,
        },
        "arm_gate_path": str(arm_gate_path),
    }
    ticket_path = runtime_root / "runtime_ticket.json"
    atomic_json(ticket_path, ticket)
    bridge_environment = production_runtime_environment(
        os.environ,
        profile="control",
        additions={
            "STEP5D_V3_RUNTIME_TICKET": str(ticket_path),
            "STEP5D_BRIDGE_LAUNCH_NONCE": launch_id,
        },
        runtime_pointer=runtime_pointer,
    )
    runner_environment = production_runtime_environment(
        os.environ,
        profile="control",
        additions={
            "STEP5D_V3_SUPERVISOR_PID": str(os.getpid()),
        },
        runtime_pointer=runtime_pointer,
    )
    runner_ready = bridge_runtime / "campaign_runner_ready.json"
    bridge_log_path = args.output_root / "bridge.log"
    runner_log_path = args.output_root / "campaign_runner.log"
    bridge: subprocess.Popen[Any] | None = None
    runner: subprocess.Popen[Any] | None = None
    bridge_rc: int | None = None
    runner_rc: int | None = None
    cleanup: Mapping[str, Any] | None = None
    play_observed = False
    bridge_alive_at_campaign_outcome = False
    publisher: RuntimeObservationPublisher | None = None
    governed_status: Mapping[str, Any] | None = None
    authority_revocation_errors: list[str] = []
    campaign_completed = False
    publisher_terminalization_started = False
    lifecycle = LiveSessionLifecycle()
    mailbox_reader = AtomicCommandMailbox(
        runtime_root / "command.json",
        network_mode=True,
        launch_profile=launch_profile,
    )
    mailbox_tracker: dict[str, Any] = {"seen": {}, "duplicate": False}
    preexisting_bundles = _immutable_trial_bundles(args.campaign_root)
    supervisor_pid = os.getpid()
    supervisor_starttime = process_starttime(supervisor_pid)
    try:
        writer_guard = writer_lease(
            ResourceProfile.from_env(),
            "step5d-autotune-v3-production-bridge",
            blocking=False,
        )
        writer_guard.__enter__()
    except (OSError, TimeoutError) as exc:
        raise LiveLaunchError(
            f"exclusive production writer lease is unavailable: {exc}"
        ) from exc
    try:
        with bridge_log_path.open("wb") as bridge_log:
            # The production session remains continuous after this barrier;
            # only startup readiness is bounded and fail-fast.
            bridge, _bridge_launch_started_ns, _bridge_ready_observed_ns = (
                _run_bridge_command_and_wait_for_readiness(
                    command,
                    bridge_ready_path=bridge_run / "bridge_ready.json",
                    timeout_s=float(getattr(args, "ready_timeout_s", 30.0)),
                    role="bridge",
                    cwd=ROOT,
                    env=bridge_environment,
                    stdin=subprocess.DEVNULL,
                    stdout=bridge_log,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                    preexec_fn=lambda expected_pid=supervisor_pid, expected_start=supervisor_starttime: _parent_death_guard(
                        expected_pid, expected_start
                    ),
                )
            )
            lifecycle.bridge = bridge
            bridge_ready = read_strict_json(
                bridge_run / "bridge_ready.json", role="bridge readiness"
            )
            validate_strict_bridge_ready(
                bridge_ready,
                bridge_pid=bridge.pid,
                bridge_starttime_ticks=process_starttime(bridge.pid),
                launch_nonce=launch_id,
                expected_profile=release.control_profile_id,
                ticket=ticket,
                basis=basis,
                release=release,
                admission=admission,
            )
            csv_path = bridge_run / "bridge_rtde_500hz.csv"
            csv_follower = _LatestCsvFollower(csv_path)
            print("V3_BRIDGE_READY_NO_ARM", flush=True)
            runner_command = [
                control_python,
                str(RUNNER),
                "--experiment-root",
                str(ROOT),
                "--bridge-run",
                str(bridge_run),
                "--campaign-root",
                str(args.campaign_root),
                "--receiver-root",
                str(receiver_root),
                "--mailbox",
                str(runtime_root / "command.json"),
                "--runner-ready-file",
                str(runner_ready),
                "--campaign-binding",
                str(campaign_binding),
                "--campaign-lease",
                str(lease_path),
                "--release-manifest-sha256",
                release.manifest_sha256,
                "--v3-launch-profile",
                str(launch_profile_path),
                "--v3-program-id",
                release.program_id,
                "--launch-basis",
                str(args.launch_basis),
                "--launch-basis-sha256",
                basis["basis_sha256"],
                "--delivery-observation",
                str(args.delivery_observation),
                "--campaign-prepare",
                str(args.campaign_prepare),
                "--admission",
                str(args.admission),
                "--canonical-owner-pid",
                str(args.canonical_owner_pid),
                "--canonical-owner-starttime",
                str(args.canonical_owner_starttime),
                "--authority-epoch",
                str(args.authority_epoch),
            ]
            with runner_log_path.open("wb") as runner_log:
                runner = subprocess.Popen(
                    runner_command,
                    cwd=ROOT,
                    env=runner_environment,
                    stdin=subprocess.DEVNULL,
                    stdout=runner_log,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                    preexec_fn=lambda expected_pid=supervisor_pid, expected_start=supervisor_starttime: _parent_death_guard(
                        expected_pid,
                    expected_start,
                ),
            )
                lifecycle.runner = runner
                _wait_file(runner_ready, runner, "campaign runner")
                lifecycle.start_session(
                    bridge=bridge,
                    runner=runner,
                )
                _write_session_lifecycle(
                    args.output_root,
                    lifecycle,
                    reason="session_started_waiting_for_home",
                )
                release_snapshot = load_current_release_snapshot(ROOT)
                if not release_snapshot.valid:
                    raise LiveLaunchError(
                        f"current governance release is invalid: {release_snapshot.error}"
                    )
                publisher = RuntimeObservationPublisher.start(
                    experiment_root=ROOT,
                    campaign_root=args.campaign_root,
                    run_id=launch_id,
                    release=release_snapshot,
                    release_contract_evidence=_release_contract_evidence_reference(
                        ROOT,
                        release,
                    ),
                    lease=lease,
                    lease_expires_at_unix_ns=time.time_ns()
                    + 12 * 60 * 60 * 1_000_000_000,
                )
                publisher.update_lifecycle(
                    "waiting_for_play", observed_at_unix_ns=time.time_ns()
                )
                governed_status = _publish_runtime_observation(
                    publisher,
                    release=release,
                    bridge=bridge,
                    runner=runner,
                    bridge_csv=csv_follower,
                    bridge_ready=bridge_ready,
                    robot_host=robot_host,
                    mailbox_reader=mailbox_reader,
                    mailbox_tracker=mailbox_tracker,
                    campaign_root=args.campaign_root,
                    preexisting_bundles=preexisting_bundles,
                    delivery_observation=delivery_observation,
                )
                if governed_status["state"] not in {
                    "WAITING_FOR_IDENTITY_PLAY",
                    "WAITING_FOR_PLAY",
                }:
                    raise LiveLaunchError(
                        "machine state did not reach a governed Play barrier: "
                        + ",".join(governed_status["blocker"]["reason_codes"])
                    )
                _publish_canonical_readiness_claim(
                    args.output_root,
                    governed_status["state"],
                )
                print("V3_CAMPAIGN_READY_FOR_TP_PLAY", flush=True)
                print("READY_FOR_ONE_PLAY_TO_MOVE", flush=True)
                next_observation = (
                    time.monotonic() + RUNTIME_OBSERVATION_INTERVAL_S
                )
                while True:
                    if bridge.poll() is not None:
                        lifecycle.observe(
                            bridge_exit=bridge.returncode,
                            error="bridge exited while waiting for TP Play",
                        )
                        _write_session_lifecycle(
                            args.output_root,
                            lifecycle,
                            reason="bridge_session_ended",
                        )
                        raise LiveLaunchError("bridge exited while waiting for TP Play")
                    if runner.poll() is not None:
                        lifecycle.observe(
                            runner_exit=runner.returncode,
                            error="campaign runner exited while waiting for TP Play",
                        )
                        _write_session_lifecycle(
                            args.output_root,
                            lifecycle,
                            reason="runner_session_ended",
                        )
                        raise LiveLaunchError(
                            "campaign runner exited while waiting for TP Play"
                        )
                    if _runtime_playing_normal(csv_follower.poll()):
                        play_observed = True
                        lifecycle.observe(
                            parameters_available=True,
                            authoritative_home=True,
                            safety_normal=True,
                        )
                        _write_session_lifecycle(
                            args.output_root,
                            lifecycle,
                            reason="hardware_authority_observed",
                        )
                        break
                    if publisher is not None and time.monotonic() >= next_observation:
                        governed_status = _publish_runtime_observation(
                            publisher,
                            release=release,
                            bridge=bridge,
                            runner=runner,
                            bridge_csv=csv_follower,
                            bridge_ready=bridge_ready,
                            robot_host=robot_host,
                            mailbox_reader=mailbox_reader,
                            mailbox_tracker=mailbox_tracker,
                            campaign_root=args.campaign_root,
                            preexisting_bundles=preexisting_bundles,
                            delivery_observation=delivery_observation,
                        )
                        if governed_status["state"] not in {
                            "WAITING_FOR_IDENTITY_PLAY",
                            "WAITING_FOR_PLAY",
                        }:
                            raise LiveLaunchError(
                                "governed Play barrier invalidated: "
                                + ",".join(
                                    governed_status["blocker"]["reason_codes"]
                                )
                            )
                        _publish_canonical_readiness_claim(
                            args.output_root,
                            governed_status["state"],
                        )
                        next_observation = (
                            time.monotonic() + RUNTIME_OBSERVATION_INTERVAL_S
                        )
                    time.sleep(0.025)
                try:
                    play_gate_observation, play_dashboard = _refresh_arm_gate(
                        arm_gate_path,
                        lease=lease,
                        lease_sha256=lease_sha256,
                        bridge=bridge,
                        csv_follower=csv_follower,
                        robot_host=robot_host,
                        runtime_contract=runtime_contract,
                        mailbox_reader=mailbox_reader,
                        delivery_observation=delivery_observation,
                        release=release,
                    )
                except LiveLaunchError:
                    if publisher is not None:
                        _publish_runtime_observation(
                            publisher,
                            release=release,
                            bridge=bridge,
                            runner=runner,
                            bridge_csv=csv_follower,
                            bridge_ready=bridge_ready,
                            robot_host=robot_host,
                            mailbox_reader=mailbox_reader,
                            mailbox_tracker=mailbox_tracker,
                            campaign_root=args.campaign_root,
                            preexisting_bundles=preexisting_bundles,
                            delivery_observation=delivery_observation,
                        )
                    raise
                if publisher is not None:
                    play_at = time.time_ns()
                    play_recheck = _play_identity_recheck_observation(
                        play_gate_observation,
                        delivery_observation,
                    )
                    publisher.update_lifecycle(
                        "play_observed", observed_at_unix_ns=play_at
                    )
                    publisher.update_lifecycle(
                        "play_identity_recheck",
                        observed_at_unix_ns=max(time.time_ns(), play_at + 1),
                        **play_recheck,
                    )
                    governed_status = _publish_runtime_observation(
                        publisher,
                        release=release,
                        bridge=bridge,
                        runner=runner,
                        bridge_csv=csv_follower,
                        bridge_ready=bridge_ready,
                        robot_host=robot_host,
                        mailbox_reader=mailbox_reader,
                        mailbox_tracker=mailbox_tracker,
                        campaign_root=args.campaign_root,
                        preexisting_bundles=preexisting_bundles,
                        dashboard=play_dashboard,
                        delivery_observation=delivery_observation,
                    )
                print("V3_POST_PLAY_IDENTITY_REVERIFIED_ARM_GATE_OPEN", flush=True)
                print("V3_CAMPAIGN_RUNNING_ONE_PLAY_CONTINUOUS", flush=True)
                next_gate_refresh = time.monotonic()
                next_observation = time.monotonic()
                while bridge.poll() is None and runner.poll() is None:
                    now = time.monotonic()
                    dashboard_observation: Mapping[str, Any] | None = None
                    if now >= next_gate_refresh:
                        try:
                            _, dashboard_observation = _refresh_arm_gate(
                                arm_gate_path,
                                lease=lease,
                                lease_sha256=lease_sha256,
                                bridge=bridge,
                                csv_follower=csv_follower,
                                robot_host=robot_host,
                                runtime_contract=runtime_contract,
                                mailbox_reader=mailbox_reader,
                                delivery_observation=delivery_observation,
                                release=release,
                            )
                        except LiveLaunchError:
                            if publisher is not None:
                                _publish_runtime_observation(
                                    publisher,
                                    release=release,
                                    bridge=bridge,
                                    runner=runner,
                                    bridge_csv=csv_follower,
                                    bridge_ready=bridge_ready,
                                    robot_host=robot_host,
                                    mailbox_reader=mailbox_reader,
                                    mailbox_tracker=mailbox_tracker,
                                    campaign_root=args.campaign_root,
                                    preexisting_bundles=preexisting_bundles,
                                    delivery_observation=delivery_observation,
                                )
                            raise
                        next_gate_refresh = now + ARM_GATE_REFRESH_INTERVAL_S
                    if publisher is not None and now >= next_observation:
                        governed_status = _publish_runtime_observation(
                            publisher,
                            release=release,
                            bridge=bridge,
                            runner=runner,
                            bridge_csv=csv_follower,
                            bridge_ready=bridge_ready,
                            robot_host=robot_host,
                            mailbox_reader=mailbox_reader,
                            mailbox_tracker=mailbox_tracker,
                            campaign_root=args.campaign_root,
                            preexisting_bundles=preexisting_bundles,
                            dashboard=dashboard_observation,
                            delivery_observation=delivery_observation,
                        )
                        next_observation = now + RUNTIME_OBSERVATION_INTERVAL_S
                    time.sleep(0.025)
                if bridge.poll() is not None or runner.poll() is not None:
                    session_error = (
                        "live session process exited"
                        if bridge.poll() is not None
                        or (runner.poll() is not None and runner.returncode != 0)
                        else None
                    )
                    lifecycle.observe(
                        bridge_exit=bridge.returncode
                        if bridge.poll() is not None
                        else None,
                        runner_exit=runner.returncode
                        if runner.poll() is not None
                        else None,
                        error=session_error,
                    )
                    _write_session_lifecycle(
                        args.output_root,
                        lifecycle,
                        reason="live_session_degraded",
                    )
                if bridge.poll() is not None and runner.poll() is None:
                    raise LiveLaunchError(
                        f"bridge exited before campaign completion rc={bridge.returncode}"
                    )
                if runner.poll() is not None and runner.returncode == 0:
                    bridge_alive_at_campaign_outcome = bridge.poll() is None
                    if publisher is not None:
                        governed_status = _publish_runtime_observation(
                            publisher,
                            release=release,
                            bridge=bridge,
                            runner=runner,
                            bridge_csv=csv_follower,
                            bridge_ready=bridge_ready,
                            robot_host=robot_host,
                            mailbox_reader=mailbox_reader,
                            mailbox_tracker=mailbox_tracker,
                            campaign_root=args.campaign_root,
                            preexisting_bundles=preexisting_bundles,
                            delivery_observation=delivery_observation,
                            runner_exit_code=0,
                        )
                        if not bool(
                            governed_status.get("outcome", {}).get("live_proven")
                        ):
                            queue_state = parameter_queue_status(receiver_root)
                            if (
                                queue_state.get("pending_count") == 0
                                and queue_state.get("inflight") is None
                            ):
                                lifecycle.observe(
                                    parameters_available=False,
                                    authoritative_home=True,
                                    safety_normal=True,
                                )
                                _write_session_lifecycle(
                                    args.output_root,
                                    lifecycle,
                                    reason="durable_queue_empty_waiting",
                                )
                                return None
                            raise LiveLaunchError(
                                "campaign ended before LIVE_PROVEN machine evidence"
                            )
                        terminal_revoked_at = time.time_ns()
                        publisher.revoke_lease(
                            observed_at_unix_ns=terminal_revoked_at,
                            reason="campaign_terminal",
                        )
                        publisher_terminalization_started = True
                        publisher.update_lifecycle(
                            "campaign_terminal",
                            observed_at_unix_ns=terminal_revoked_at + 1,
                            reason="campaign_complete",
                            runner_exit_code=0,
                        )
                        governed_status = _publish_runtime_observation(
                            publisher,
                            release=release,
                            bridge=bridge,
                            runner=runner,
                            bridge_csv=csv_follower,
                            bridge_ready=bridge_ready,
                            robot_host=robot_host,
                            mailbox_reader=mailbox_reader,
                            mailbox_tracker=mailbox_tracker,
                            campaign_root=args.campaign_root,
                            preexisting_bundles=preexisting_bundles,
                            delivery_observation=delivery_observation,
                            runner_exit_code=0,
                        )
                        if (
                            governed_status.get("terminal", {}).get("completed")
                            is not True
                            or governed_status.get("next_action")
                            != "campaign_complete"
                        ):
                            raise LiveLaunchError(
                                "campaign terminal attestation did not close"
                            )
                    campaign_completed = True
                    lifecycle.observe(
                        parameters_available=False,
                        authoritative_home=True,
                        safety_normal=True,
                    )
                    print("V3_CAMPAIGN_RUNNER_STOPPED_BRIDGE_STILL_ALIVE", flush=True)
    except KeyboardInterrupt:
        lifecycle.request_shutdown()
        _write_session_lifecycle(
            args.output_root,
            lifecycle,
            reason="explicit_user_shutdown",
        )
        raise
    except Exception as exc:
        lifecycle.observe(error=exc)
        try:
            _write_session_lifecycle(
                args.output_root,
                lifecycle,
                reason="session_degraded",
            )
        except Exception:
            pass
        raise
    finally:
        try:
            request_program_stop = _should_request_program_stop(
                campaign_completed=campaign_completed,
                explicit_shutdown=lifecycle.explicit_shutdown,
            )
            if campaign_completed:
                # Campaign completion is an authorized safe cleanup, but it is
                # not a lifecycle SHUTDOWN transition.
                cleanup_targets = lifecycle.cleanup_targets(
                    failed_session=True
                )
            else:
                # A failed runner/bridge session must leave the durable
                # receiver queue accepting while both session children stop.
                cleanup_targets = lifecycle.cleanup_targets(
                    explicit_shutdown=lifecycle.explicit_shutdown,
                    failed_session=True,
                )
            try:
                authority_revocation_errors.extend(
                    _revoke_campaign_authority(
                        arm_gate_path,
                        lease=lease,
                        lease_sha256=lease_sha256,
                        publisher=(
                            None if publisher_terminalization_started else publisher
                        ),
                        reason=(
                            "campaign_terminal"
                            if campaign_completed
                            else "supervisor_exit"
                        ),
                    )
                )
            except Exception as exc:
                authority_revocation_errors.append(
                    f"authority_revoke:{type(exc).__name__}:{exc}"
                )
            try:
                runner_target, bridge_target = cleanup_targets
                runner_rc = (
                    _terminate(runner_target)
                    if runner_target is not None
                    else (None if runner is None else runner.poll())
                )
            except Exception as exc:
                authority_revocation_errors.append(
                    f"runner_terminate:{type(exc).__name__}:{exc}"
                )
            try:
                bridge_rc = (
                    _terminate(bridge_target)
                    if bridge_target is not None
                    else (None if bridge is None else bridge.poll())
                )
            except Exception as exc:
                authority_revocation_errors.append(
                    f"bridge_terminate:{type(exc).__name__}:{exc}"
                )
            if (
                bridge is not None
                and cleanup_targets != (None, None)
                and request_program_stop
            ):
                if play_observed:
                    try:
                        _announce_stop_if_playing(robot_host)
                    except Exception as exc:
                        authority_revocation_errors.append(
                            f"stop_announcement:{type(exc).__name__}:{exc}"
                        )
                try:
                    cleanup = _stop_v3_program(robot_host)
                except Exception as exc:
                    authority_revocation_errors.append(
                        f"program_stop:{type(exc).__name__}:{exc}"
                    )
            try:
                atomic_json(
                    args.output_root / "cleanup.json",
                    {
                        "runner_exit_code": runner_rc,
                        "bridge_exit_code": bridge_rc,
                        "program_stop": cleanup,
                        "authority_revocation_errors": authority_revocation_errors,
                        "parameter_receiver_stopped": not lifecycle.receiver_accepting,
                        "session_state": lifecycle.state.value,
                    },
                )
            except Exception as exc:
                print(
                    f"cleanup evidence unavailable: {type(exc).__name__}:{exc}",
                    file=sys.stderr,
                )
        finally:
            writer_guard.__exit__(None, None, None)
    result = {
        "schema": RESULT_SCHEMA,
        "ok": (
            runner is not None
            and runner.returncode == 0
            and bridge_alive_at_campaign_outcome
            and not authority_revocation_errors
        ),
        "launch_id": launch_id,
        "campaign_id": prepared["campaign_id"],
        "campaign_epoch": prepared["campaign_epoch"],
        "campaign_prepare_fingerprint": prepared["campaign_fingerprint"],
        "legacy_preflight": legacy_preflight,
        "candidate_plan_revision": plan_revision,
        "parameter_receiver": str(receiver_root),
        "optimizer_required": False,
        "bridge_run": str(bridge_run),
        "preflight_controller_identity_sha256": preflight["controller_identity_sha256"],
        "release_manifest_sha256": release.manifest_sha256,
        "campaign_lease_sha256": lease_sha256,
        "bridge_alive_at_campaign_outcome": bridge_alive_at_campaign_outcome,
        "authority_revocation_errors": authority_revocation_errors,
        "program_stop": cleanup,
    }
    atomic_json(args.output_root / "live_campaign_result.json", result)
    if result["ok"] is not True:
        raise LiveLaunchError(f"campaign runner exited rc={None if runner is None else runner.returncode}")
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--preflight", type=Path)
    parser.add_argument("--delivery-observation", type=Path)
    parser.add_argument("--admission", type=Path)
    parser.add_argument("--authority-epoch", type=int)
    parser.add_argument("--launch-basis", type=Path)
    parser.add_argument("--launch-basis-sha256")
    parser.add_argument("--campaign-prepare", type=Path)
    parser.add_argument("--ready-timeout-s", type=float, default=30.0)
    parser.add_argument(
        "--campaign-root",
        type=Path,
        default=ROOT / "runs/step5d_autotune_v3",
    )
    parser.add_argument(
        "--launch-profile",
        type=Path,
        default=ROOT / "config/step5/step5d_autotune_v3_launch_profile.json",
    )
    parser.add_argument("--canonical-owner-pid", type=int, required=True)
    parser.add_argument("--canonical-owner-starttime", type=int, required=True)
    parser.add_argument("--prepare-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--single-session", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not args.prepare_only:
        missing = _missing_live_identity_args(args)
        if missing:
            parser.error(
                "live mode requires "
                + ", ".join(f"--{name.replace('_', '-')}" for name in missing)
            )
    return args


def main(argv: list[str] | None = None) -> int:
    try:
        _require_canonical_launcher()
    except Exception as exc:
        print(
            json.dumps(
                {"schema": RESULT_SCHEMA, "ok": False, "blocker": str(exc)},
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    args = parse_args(argv)
    try:
        _parent_death_guard(
            args.canonical_owner_pid,
            args.canonical_owner_starttime,
        )
        args._runtime_pointer = require_runtime_profile("control")
        if args.prepare_only:
            from prepare_step5d_autotune_launch import LaunchPreparationRequest, prepare

            release = load_current_release(ROOT)
            contract_path = release_payload_path(
                ROOT, release, SAFETY_ENVELOPE_PATH
            )
            launch_profile_path = release_payload_path(
                ROOT, release, LAUNCH_PROFILE_PATH
            )
            load_launch_profile(
                launch_profile_path,
                contract=load_contract(contract_path),
                expected_tp_program_id=release.program_id,
            )
            result = prepare(
                LaunchPreparationRequest(
                    experiment_root=ROOT,
                    campaign_root=args.campaign_root,
                    binding_file=(
                        args.campaign_root
                        / "control"
                        / "prepare_only_campaign_binding.json"
                    ),
                    binding_source="canonical_v3_prepare_only",
                    launch_profile_path=launch_profile_path,
                    candidate_batch_size=5,
                    rolling_plan=True,
                )
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        if (
            args.output_root is None
            or args.preflight is None
            or args.delivery_observation is None
        ):
            raise LiveLaunchError(
                "live worker requires --output-root, --preflight, and "
                "--delivery-observation"
            )
        _require_live_identity_args(args)
        result = run(args)
    except Exception as exc:
        result = {"schema": RESULT_SCHEMA, "ok": False, "blocker": str(exc)}
        if args.output_root is not None:
            try:
                atomic_json(args.output_root / "live_campaign_result.json", result)
            except Exception as evidence_exc:
                result["evidence_write_blocker"] = str(evidence_exc)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
