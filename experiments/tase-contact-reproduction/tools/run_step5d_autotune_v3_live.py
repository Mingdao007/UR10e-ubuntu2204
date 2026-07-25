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
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping


# Refuse direct execution before importing the runtime graph.  Besides keeping
# the internal worker private, this preserves a cheap fail-closed boundary when
# the host has not installed the experiment runtime PYTHONPATH.
_EARLY_ROOT = Path(__file__).resolve().parents[1]
_EARLY_CANONICAL_ENV = "STEP5D_V3_CANONICAL_LAUNCHER"
if __name__ == "__main__" and os.environ.get(_EARLY_CANONICAL_ENV) != str(
    (_EARLY_ROOT / "scripts/step5d-autotune-v3.sh").resolve()
):
    print(
        json.dumps(
            {
                "schema": "step5d.autotune-v3/live-campaign-launch-result-v1",
                "ok": False,
                "blocker": (
                    "internal live worker is not a public entrypoint; use "
                    f"{_EARLY_ROOT / 'scripts/step5d-autotune-v3.sh'} bridge-live"
                ),
            },
            sort_keys=True,
        )
    )
    raise SystemExit(2)

from step5d_autotune_state_machine import TpLoopState
from step5d_autotune_v3.dashboard import DashboardObservationError, dashboard_exchange
from step5d_autotune_v3.delivery_observation import (
    DeliveryObservationError,
    fresh_get_provenance,
    load_delivery_observation,
    validate_delivery_observation,
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
from step5d_bridge_authority import AuthorityFence
from step5d_autotune_v3.runtime_observation import (
    RuntimeObservationError,
    RuntimeObservationPublisher,
)
from step5d_autotune_v3.state import (
    CampaignPaths,
    atomic_json,
    read_strict_json,
)
from step5d_autotune_live_driver import AtomicCommandMailbox
from ur10e_parallel import ResourceProfile, writer_lease, writer_lease_owner
from step5d_bridge_status import (
    readiness_claim,
    resolve_status as resolve_bridge_status,
    verify_readiness_claim,
)


class BatchProducerError(RuntimeError):
    """Compatibility error surface; the producer implementation loads lazily."""


class ExactOptimizerClient:
    """Lazy proxy preserving the testable owner surface without eager imports."""

    def __new__(cls, *args: Any, **kwargs: Any) -> Any:
        from step5d_autotune_v3.optimizer_protocol import ExactOptimizerClient as implementation

        return implementation(*args, **kwargs)


def deployment_certificate(*args: Any, **kwargs: Any) -> Any:
    from step5d_autotune_v3.optimizer_protocol import deployment_certificate as implementation

    return implementation(*args, **kwargs)


def load_gpu_functional_attestation(*args: Any, **kwargs: Any) -> Any:
    from step5d_autotune_v3.optimizer_deployment import load_gpu_functional_attestation as implementation

    return implementation(*args, **kwargs)


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "tools/run_step5d_autotune_v3_bridge.py"
RUNNER = ROOT / "tools/run_step5d_autotune_campaign.py"
RESULT_SCHEMA = "step5d.autotune-v3/live-campaign-launch-result-v1"
LIVE_PREFLIGHT_SCHEMA = "step5d.autotune-v3/live-preflight-snapshot-v3"
CANONICAL_LAUNCH_ENV = "STEP5D_V3_CANONICAL_LAUNCHER"
ARM_GATE_REFRESH_INTERVAL_S = ARM_GRANT_MAX_AGE_S * 0.4
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


_NO_PRODUCER_RESULT = object()
_ACTIVE_AUTHORITY_FENCE: AuthorityFence | None = None


def _assert_active_authority() -> None:
    if _ACTIVE_AUTHORITY_FENCE is not None:
        _ACTIVE_AUTHORITY_FENCE.assert_active()


def _publish_canonical_readiness_claim(
    output_root: Path, required_state: str
) -> dict[str, Any]:
    _assert_active_authority()
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


class _AsyncProducerPoller:
    def __init__(
        self,
        producer: RollingBatchProducer,
        proposal_provider: ProductionProposalProvider,
        *,
        interval_s: float = 0.2,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if interval_s <= 0.0:
            raise ValueError("producer polling interval must be positive")
        self._producer = producer
        self._proposal_provider = proposal_provider
        self._interval_s = interval_s
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._in_flight = False
        self._closed = False
        self._next_due = 0.0
        self._result: Any = _NO_PRODUCER_RESULT
        self._error: Exception | None = None

    def _run_once(self) -> None:
        try:
            result = self._producer.poll_once(
                proposal_provider=self._proposal_provider
            )
            error = None
        except Exception as exc:
            result = _NO_PRODUCER_RESULT
            error = exc
        with self._lock:
            self._result = result
            self._error = error
            self._next_due = self._monotonic() + self._interval_s
            self._in_flight = False

    def poll(self, *, now: float | None = None) -> Any | None:
        current = self._monotonic() if now is None else now
        start_thread = False
        result: Any = _NO_PRODUCER_RESULT
        with self._lock:
            if self._error is not None:
                error = self._error
                raise LiveLaunchError(
                    "rolling batch producer failed closed: "
                    f"{type(error).__name__}: {error}"
                ) from error
            if self._result is not _NO_PRODUCER_RESULT:
                result = self._result
                self._result = _NO_PRODUCER_RESULT
            elif (
                not self._closed
                and not self._in_flight
                and current >= self._next_due
            ):
                self._in_flight = True
                start_thread = True
        if start_thread:
            thread = threading.Thread(
                target=self._run_once,
                name="step5d-rolling-batch-producer",
                daemon=True,
            )
            with self._lock:
                self._thread = thread
            thread.start()
        return None if result is _NO_PRODUCER_RESULT else result

    def close(self, *, timeout_s: float = 0.5) -> tuple[bool, Exception | None]:
        if timeout_s < 0.0:
            raise ValueError("producer close timeout must be non-negative")
        with self._lock:
            self._closed = True
            thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout_s)
        with self._lock:
            return not self._in_flight, self._error


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


def _authority_fence_for_basis(
    basis: Mapping[str, Any], args: argparse.Namespace
) -> AuthorityFence:
    attempt_id = os.environ.get("STEP5D_V3_LAUNCH_ATTEMPT_ID")
    if attempt_id is None:
        attempt_id = str(basis["launch_nonce"])
    if attempt_id != basis["launch_nonce"]:
        raise LiveLaunchError("launch attempt identity differs from launch basis")
    root = Path(
        os.environ.get(
            "STEP5D_V3_AUTHORITY_ROOT",
            str(ROOT / "runs/step5d_bridge_authority"),
        )
    )
    return AuthorityFence(
        root,
        attempt_id=attempt_id,
        sequence=int(basis["authority_epoch"]),
        owner_pid=args.canonical_owner_pid,
        owner_starttime_ticks=args.canonical_owner_starttime,
    )


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
    _assert_active_authority()
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
    _assert_active_authority()
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
    _assert_active_authority()
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
    _assert_active_authority()
    try:
        for _attempt in range(3):
            # The caller's worker fence is rechecked before every ARM gate
            # observation/publication boundary.
            authority_root = Path(
                os.environ.get(
                    "STEP5D_V3_AUTHORITY_ROOT",
                    str(ROOT / "runs/step5d_bridge_authority"),
                )
            )
            attempt_id = os.environ.get("STEP5D_V3_LAUNCH_ATTEMPT_ID", "")
            if attempt_id:
                from step5d_bridge_authority import require_active

                require_active(
                    authority_root,
                    attempt_id=attempt_id,
                    sequence=int(os.environ.get("STEP5D_V3_AUTHORITY_EPOCH", "0")),
                )
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
            dashboard = dashboard_exchange(
                robot_host,
                ["programState", "safetymode", "get loaded program"],
                timeout=2.0,
            )
            if arm_command is None:
                row = csv_follower.poll()
                if row is None:
                    raise LiveLaunchError("ARM gate lacks a complete bridge RTDE row")
            else:
                deadline = time.monotonic() + 0.5
                row = None
                while time.monotonic() < deadline:
                    if bridge.poll() is not None:
                        raise LiveLaunchError(
                            "bridge exited while awaiting a post-request RTDE row"
                        )
                    candidate = csv_follower.poll()
                    if candidate is not None:
                        try:
                            row_wall_ns = int(candidate["t_wall_ns"])
                        except (KeyError, TypeError, ValueError) as exc:
                            raise LiveLaunchError(
                                "ARM gate RTDE row lacks an integer wall timestamp"
                            ) from exc
                        if row_wall_ns >= command_observed_at_unix_ns:
                            row = candidate
                            break
                    time.sleep(0.005)
                if row is None:
                    raise LiveLaunchError(
                        "ARM gate lacks a post-request bridge RTDE row"
                    )
            command_after = mailbox_reader.read_latest()
            command_sha_after = (
                None if command_after is None else command_after.sha256
            )
            if command_sha_after != command_sha_before:
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
            if attempt_id:
                require_active(
                    authority_root,
                    attempt_id=attempt_id,
                    sequence=int(os.environ.get("STEP5D_V3_AUTHORITY_EPOCH", "0")),
                )
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
        else:
            raise LiveLaunchError(
                "ARM mailbox changed across three fresh observation attempts"
            )
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


def _announce_stop_if_playing(robot_host: str) -> bool:
    try:
        state = dashboard_exchange(robot_host, ["programState"], timeout=2.0)
    except Exception:
        state = {}
    if str(state.get("programState", "")).startswith("PLAYING"):
        print("ACTION_REQUIRED_PRESS_TP_STOP_NOW", flush=True)
        return True
    return False


def _wait_file(path: Path, process: subprocess.Popen[Any], timeout_s: float, role: str) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise LiveLaunchError(f"{role} process exited before readiness rc={process.returncode}")
        if path.is_file():
            return
        time.sleep(0.05)
    raise LiveLaunchError(f"{role} readiness timeout")


def _terminate(process: subprocess.Popen[Any] | None) -> int | None:
    if process is None:
        return None
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=8.0)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5.0)
    return process.returncode


def _run_bridge_command_and_wait_for_readiness(
    command: list[str],
    *,
    bridge_ready_path: Path,
    timeout_s: float,
    role: str,
    **popen_kwargs: Any,
) -> tuple[subprocess.Popen[Any], int, int]:
    """Start a subprocess and wait for the readiness artifact."""
    launch_started_ns = time.perf_counter_ns()
    process = subprocess.Popen(command, **popen_kwargs)
    _wait_file(bridge_ready_path, process, timeout_s, role)
    ready_observed_ns = time.perf_counter_ns()
    return process, launch_started_ns, ready_observed_ns


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


def _stop_v3_program(
    robot_host: str,
    *,
    timeout_s: float = 5.0,
    poll_interval_s: float = 0.1,
    exchange: Callable[..., Mapping[str, Any]] = dashboard_exchange,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Observe operator-owned TP Stop; never send Dashboard stop."""

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


def _build_bridge_launch_command(
    *,
    control_python: str,
    runtime_root: Path,
    launch_profile: Any,
    contract: Mapping[str, Any] | None = None,
) -> list[str]:
    return [
        control_python,
        str(WRAPPER),
        *build_bridge_argv(
            runtime_root,
            contract=contract,
            launch_profile=launch_profile,
            trial_overlay=DEFAULT_OVERLAY,
        )[2:],
    ]


def run(args: argparse.Namespace) -> Mapping[str, Any]:
    runtime_pointer = getattr(args, "_runtime_pointer", None)
    if not isinstance(runtime_pointer, Mapping):
        runtime_pointer = require_runtime_profile("control")
    _gpu_attestation, gpu_reference = load_gpu_functional_attestation(
        runtime_pointer=runtime_pointer
    )
    optimizer_deployment = deployment_certificate(
        runtime_pointer=runtime_pointer,
        gpu_attestation_digest=gpu_reference["sha256"],
    )
    optimizer_client = ExactOptimizerClient(
        deployment=optimizer_deployment,
        runtime_pointer=runtime_pointer,
    )
    try:
        return _run_live(args, runtime_pointer, optimizer_client)
    finally:
        optimizer_client.close()


def _run_live(
    args: argparse.Namespace,
    runtime_pointer: Mapping[str, Any],
    optimizer_client: ExactOptimizerClient,
) -> Mapping[str, Any]:
    from prepare_step5d_autotune_launch import (
        write_machine_campaign_binding,
    )
    from run_step5d_autotune_v3_bridge import TICKET_SCHEMA, TICKET_SCOPE
    from step5d_autotune_batch_plan import load_plan
    from step5d_autotune_v3.batch_producer import (
        BatchProducerError,
        ProductionProposalProvider,
        RollingBatchProducer,
        production_candidate_catalog,
    )
    from step5d_autotune_v3.launch_basis import (
        read_and_validate_launch_basis,
        validate_delivery_observation_binding,
        validate_strict_bridge_ready,
    )

    control_python = runtime_pointer["profiles"]["control"]["python_executable"]
    optimizer_python = runtime_pointer["profiles"]["optimizer"]["python_executable"]
    launch_basis = read_and_validate_launch_basis(
        args.launch_basis,
        owner_pid=args.canonical_owner_pid,
        owner_starttime=args.canonical_owner_starttime,
        expected_basis_sha256=args.launch_basis_sha256,
    )
    if launch_basis["authority_epoch"] != args.authority_epoch:
        raise LiveLaunchError("launch basis authority epoch differs from the active authority")
    authority_fence = _authority_fence_for_basis(launch_basis, args)
    os.environ["STEP5D_V3_AUTHORITY_ROOT"] = str(authority_fence.root)
    os.environ["STEP5D_V3_AUTHORITY_EPOCH"] = str(authority_fence.sequence)
    try:
        authority_fence.assert_active()
    except Exception as exc:
        raise LiveLaunchError(f"launch authority is not active: {exc}") from exc
    global _ACTIVE_AUTHORITY_FENCE
    _ACTIVE_AUTHORITY_FENCE = authority_fence
    if not args.admission.is_file() or args.admission.is_symlink():
        raise LiveLaunchError("validated admission artifact is missing or unsafe")
    admission = read_strict_json(args.admission, role="bridge admission")
    campaign_preparation = read_strict_json(
        args.campaign_prepare, role="campaign preparation result"
    )
    try:
        validate_delivery_observation_binding(
            args.delivery_observation,
            basis=launch_basis,
            admission=admission,
            experiment_root=ROOT,
        )
    except Exception as exc:
        raise LiveLaunchError(f"delivery observation is not basis-bound: {exc}") from exc
    if (
        campaign_preparation.get("ok") is not True
        or campaign_preparation.get("campaign_fingerprint")
        != launch_basis["campaign_fingerprint"]
    ):
        raise LiveLaunchError("campaign preparation is not bound to the launch basis")
    release = load_runtime_release(ROOT)
    delivery_observation = load_delivery_observation(
        ROOT, args.delivery_observation, release=release
    )
    legacy_preflight = None
    runtime_root = args.output_root.expanduser().absolute() / "runtime"
    runtime_root.mkdir(parents=True, exist_ok=False, mode=0o700)
    bridge_run = runtime_root / "bridge"
    bridge_runtime = bridge_run / "runtime"
    bridge_runtime.mkdir(parents=True, exist_ok=False, mode=0o700)
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
    command = _build_bridge_launch_command(
        control_python=control_python,
        runtime_root=runtime_root,
        contract=contract,
        launch_profile=launch_profile,
    )
    robot_host = str(check["effective_config"]["robot_host"])
    preflight = _validate_preflight(
        args.preflight,
        release,
        launch_profile,
    )
    campaign_binding = bridge_runtime / "campaign_binding.json"
    launch_plan_path = bridge_runtime / "campaign_launch_plan.json"
    prepared = campaign_preparation
    paths = CampaignPaths(args.campaign_root)
    plan = load_plan(paths.candidate_plan, campaign_id=str(prepared["campaign_id"]))
    overlay_plan = read_strict_json(paths.trial_overlays, role="V3 trial-overlay plan")
    producer_binding = _sha256_json(
        {
            "schema": "step5d.autotune-v3/producer-binding-v1",
            "manifest_sha256": release.manifest_sha256,
            "campaign_id": prepared["campaign_id"],
            "campaign_fingerprint": prepared["campaign_fingerprint"],
            "safety_envelope_sha256": release_runtime_contract(ROOT, release)[
                "safety_envelope_sha256"
            ],
            "launch_profile_fingerprint": launch_profile.fingerprint,
        }
    )
    producer = RollingBatchProducer(
        campaign_root=args.campaign_root,
        campaign_id=str(prepared["campaign_id"]),
        binding_fingerprint=producer_binding,
        launch_profile=launch_profile,
        authority_guard=authority_fence.assert_active,
    )
    proposal_provider = ProductionProposalProvider(
        campaign_root=args.campaign_root,
        campaign_id=str(prepared["campaign_id"]),
        catalog=production_candidate_catalog(),
        optimizer_client=optimizer_client,
    )
    producer_snapshot: Any = None
    producer_poller = _AsyncProducerPoller(producer, proposal_provider)
    inherited_launch_id = os.environ.get("STEP5D_V3_LAUNCH_ATTEMPT_ID")
    launch_id = str(launch_basis["launch_nonce"])
    if inherited_launch_id is not None and inherited_launch_id != launch_id:
        raise LiveLaunchError("canonical launch-attempt ID differs from launch basis")
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
    lease_sha256 = lease.sha256
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
        "campaign_binding": {
            "campaign_id": prepared["campaign_id"],
            "campaign_epoch": prepared["campaign_epoch"],
            "campaign_fingerprint": prepared["campaign_fingerprint"],
            "candidate_plan_revision": plan.revision,
            "candidate_plan_sha256": _sha256_path(paths.candidate_plan),
            "trial_overlay_plan_sha256": _sha256_path(paths.trial_overlays),
            "machine_binding_sha256": _sha256_json(
                {
                    "campaign_id": prepared["campaign_id"],
                    "campaign_epoch": prepared["campaign_epoch"],
                    "campaign_fingerprint": prepared["campaign_fingerprint"],
                }
            ),
        },
        "campaign_lease": {
            "path": str(lease_path),
            "sha256": lease_sha256,
            "document": lease.document,
        },
        "arm_gate_path": str(arm_gate_path),
        "launch_basis": {
            "path": str(args.launch_basis),
            "sha256": launch_basis["basis_sha256"],
        },
        "delivery_observation": {
            "path": str(args.delivery_observation),
            "sha256": launch_basis["delivery_observation_sha256"],
        },
        "authority": {
            "root": str(authority_fence.root),
            "attempt_id": authority_fence.attempt_id,
            "sequence": authority_fence.sequence,
        },
    }
    ticket_json = json.dumps(ticket, sort_keys=True, separators=(",", ":"))
    bridge_environment = production_runtime_environment(
        os.environ,
        profile="control",
        additions={
            "STEP5D_V3_RUNTIME_TICKET_JSON": ticket_json,
            "STEP5D_BRIDGE_LAUNCH_NONCE": launch_id,
        },
        runtime_pointer=runtime_pointer,
    )
    runner_environment = production_runtime_environment(
        os.environ,
        profile="optimizer",
        additions={
            "STEP5D_V3_SUPERVISOR_PID": str(os.getpid()),
            "STEP5D_V3_RUNTIME_TICKET_JSON": ticket_json,
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
    producer_stopped = True
    producer_error: str | None = None
    mailbox_reader = AtomicCommandMailbox(
        runtime_root / "command.json",
        network_mode=True,
        launch_profile=launch_profile,
    )
    mailbox_tracker: dict[str, Any] = {"seen": {}, "duplicate": False}
    preexisting_bundles = _immutable_trial_bundles(args.campaign_root)
    supervisor_pid = os.getpid()
    supervisor_starttime = process_starttime(supervisor_pid)
    writer_guard: Any | None = None
    try:
        with bridge_log_path.open("wb") as bridge_log:
            bridge, _bridge_launch_started_ns, _bridge_ready_observed_ns = _run_bridge_command_and_wait_for_readiness(
                command,
                cwd=ROOT,
                env=bridge_environment,
                stdin=subprocess.DEVNULL,
                stdout=bridge_log,
                stderr=subprocess.STDOUT,
                close_fds=True,
                preexec_fn=lambda expected_pid=supervisor_pid, expected_start=supervisor_starttime: _parent_death_guard(
                    expected_pid,
                    expected_start,
                ),
                bridge_ready_path=bridge_run / "bridge_ready.json",
                timeout_s=args.ready_timeout_s,
                role="bridge",
            )
            bridge_ready = read_strict_json(
                bridge_run / "bridge_ready.json", role="bridge readiness"
            )
            try:
                validate_strict_bridge_ready(
                    bridge_ready,
                    bridge_pid=bridge.pid,
                    bridge_starttime_ticks=process_starttime(bridge.pid),
                    launch_nonce=launch_id,
                    expected_profile=release.control_profile_id,
                    ticket=ticket,
                    basis=launch_basis,
                    release=release,
                    admission=admission,
                )
                authority_fence.assert_active()
            except Exception as exc:
                raise LiveLaunchError(f"strict bridge readiness rejected: {exc}") from exc
            print("V3_BRIDGE_READY_NO_ARM", flush=True)
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
            # No campaign/lease/producer artifacts are published until the
            # bridge has emitted a strict, health-bound ready observation.
            authority_fence.assert_active()
            write_campaign_lease(lease_path, lease)
            authority_fence.assert_active()
            try:
                producer_snapshot = producer.poll_once(proposal_provider=proposal_provider)
            except BatchProducerError as exc:
                raise LiveLaunchError(f"rolling batch producer failed closed: {exc}") from exc
            authority_fence.assert_active()
            write_machine_campaign_binding(
                campaign_binding,
                campaign_id=str(prepared["campaign_id"]),
                campaign_epoch=int(prepared["campaign_epoch"]),
                campaign_fingerprint=str(prepared["campaign_fingerprint"]),
                candidate_plan_path=paths.candidate_plan,
                trial_overlay_plan_path=paths.trial_overlays,
                binding_source="canonical_v3_live_entrypoint",
            )
            authority_fence.assert_active()
            prepared = {
                **prepared,
                "machine_binding_status": "finalized_exact_candidate_and_overlay_plans",
                "machine_binding_sha256": _sha256_path(campaign_binding),
            }
            authority_fence.assert_active()
            atomic_json(launch_plan_path, prepared)
            csv_path = bridge_run / "bridge_rtde_500hz.csv"
            csv_follower = _LatestCsvFollower(csv_path)
            runner_command = [
                optimizer_python,
                str(RUNNER),
                "--experiment-root",
                str(ROOT),
                "--bridge-run",
                str(bridge_run),
                "--campaign-root",
                str(args.campaign_root),
                "--mailbox",
                str(runtime_root / "command.json"),
                "--runner-ready-file",
                str(runner_ready),
                "--campaign-binding",
                str(campaign_binding),
                "--campaign-lease",
                str(lease_path),
                "--launch-basis",
                str(args.launch_basis),
                "--launch-basis-sha256",
                str(args.launch_basis_sha256),
                "--owner-pid",
                str(args.canonical_owner_pid),
                "--owner-starttime",
                str(args.canonical_owner_starttime),
                "--delivery-observation",
                str(args.delivery_observation),
                "--campaign-epoch",
                str(prepared["campaign_epoch"]),
                "--selection-policy",
                "codex_batches",
                "--candidate-plan",
                str(paths.candidate_plan),
                "--wait-for-home",
                "--home-timeout-s",
                str(args.play_timeout_s + 5.0),
                "--recover-infra-aborted-active",
                "--v3-stop-latch",
                str(paths.stop_latch),
                "--v3-derived-postprocess-root",
                str(paths.postprocess),
                "--v3-trial-overlays",
                str(paths.trial_overlays),
                "--v3-launch-profile",
                str(launch_profile_path),
                "--v3-program-id",
                release.program_id,
                "--v3-runtime-root",
                str(runtime_root),
                "--wait-for-first-arm-gate",
                "--first-arm-gate-timeout-s",
                str(args.play_timeout_s + 5.0),
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
                _wait_file(runner_ready, runner, args.ready_timeout_s, "campaign runner")
                atomic_json(
                    runtime_root / "campaign_ready.json",
                    {
                        "schema": "step5d.autotune-v3/campaign-ready-v1",
                        "launch_basis_sha256": launch_basis["basis_sha256"],
                        "bridge_ready": bridge_ready.get("ok") is True,
                        "bridge_ready_sha256": _sha256_path(bridge_run / "bridge_ready.json"),
                        "runner_ready": True,
                    },
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
            play_barrier_deadline = time.monotonic() + (args.play_timeout_s + 5.0)
            while True:
                if time.monotonic() > play_barrier_deadline:
                    raise LiveLaunchError(
                        "TP did not reach PLAYING state before the first ARM gate timeout"
                    )
                play_row = csv_follower.poll()
                if _runtime_playing_normal(play_row):
                    break
                if bridge.poll() is not None:
                    raise LiveLaunchError(
                        f"bridge exited before the first ARM gate rc={bridge.returncode}"
                    )
                time.sleep(0.05)
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
            play_observed = True
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
                completed_snapshot = producer_poller.poll(now=now)
                if completed_snapshot is not None:
                    producer_snapshot = completed_snapshot
                time.sleep(0.025)
            if bridge.poll() is not None and runner.poll() is None:
                deadline = time.monotonic() + 3.0
                while runner.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                if runner.poll() is None or runner.returncode != 0:
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
                print("V3_CAMPAIGN_RUNNER_STOPPED_BRIDGE_STILL_ALIVE", flush=True)
    finally:
        try:
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
                producer_stopped, producer_exception = producer_poller.close()
            except Exception as exc:
                producer_stopped = False
                producer_error = f"producer_close:{type(exc).__name__}:{exc}"
            else:
                if producer_exception is not None:
                    producer_error = (
                        f"{type(producer_exception).__name__}:{producer_exception}"
                    )
            try:
                runner_rc = _terminate(runner)
            except Exception as exc:
                authority_revocation_errors.append(
                    f"runner_terminate:{type(exc).__name__}:{exc}"
                )
            try:
                bridge_rc = _terminate(bridge)
            except Exception as exc:
                authority_revocation_errors.append(
                    f"bridge_terminate:{type(exc).__name__}:{exc}"
                )
            if bridge is not None:
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
                        "producer_stopped": producer_stopped,
                        "producer_error": producer_error,
                    },
                )
            except Exception as exc:
                print(
                    f"cleanup evidence unavailable: {type(exc).__name__}:{exc}",
                    file=sys.stderr,
                )
        finally:
            if writer_guard is not None:
                writer_guard.__exit__(None, None, None)
    result = {
        "schema": RESULT_SCHEMA,
        "ok": (
            runner is not None
            and runner.returncode == 0
            and bridge_alive_at_campaign_outcome
            and not authority_revocation_errors
            and producer_stopped
            and producer_error is None
        ),
        "launch_id": launch_id,
        "campaign_id": prepared["campaign_id"],
        "campaign_epoch": prepared["campaign_epoch"],
        "campaign_prepare_fingerprint": prepared["campaign_fingerprint"],
        "legacy_preflight": legacy_preflight,
        "candidate_plan_revision": plan.revision,
        "producer_plan_revision": producer_snapshot.plan_revision,
        "producer_phase": producer_snapshot.phase,
        "bridge_run": str(bridge_run),
        "preflight_controller_identity_sha256": preflight["controller_identity_sha256"],
        "release_manifest_sha256": release.manifest_sha256,
        "campaign_lease_sha256": lease_sha256,
        "bridge_alive_at_campaign_outcome": bridge_alive_at_campaign_outcome,
        "authority_revocation_errors": authority_revocation_errors,
        "producer_stopped": producer_stopped,
        "producer_error": producer_error,
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
    parser.add_argument("--ready-timeout-s", type=float, default=45.0)
    parser.add_argument("--play-timeout-s", type=float, default=120.0)
    parser.add_argument("--canonical-owner-pid", type=int, required=True)
    parser.add_argument("--canonical-owner-starttime", type=int, required=True)
    parser.add_argument("--authority-epoch", type=int, required=True)
    parser.add_argument("--admission", type=Path, required=True)
    parser.add_argument("--launch-basis", type=Path, required=True)
    parser.add_argument("--launch-basis-sha256", required=True)
    parser.add_argument("--campaign-prepare", type=Path, required=True)
    return parser.parse_args(argv)


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
        if (
            args.output_root is None
            or args.preflight is None
            or args.delivery_observation is None
        ):
            raise LiveLaunchError(
                "live worker requires --output-root, --preflight, and "
                "--delivery-observation"
            )
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
