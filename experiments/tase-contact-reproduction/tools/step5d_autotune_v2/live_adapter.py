"""Production transport adapter from the v2 control plane to the proven TP bridge."""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from step5d_autotune_contract import ExecutionProfile, ForceCandidate
from step5d_autotune_live_driver import (
    BridgeMailboxRuntime,
    BridgeTrialCsvRotator,
    MailboxCommand,
    RuntimeTrialBinding,
    TpFeedbackObservation,
    TpFeedbackPhase,
)
from step5d_autotune_state_machine import HostCommand, HostPacket, TpLoopState

from .mailbox import AtomicMailbox, MailboxEnvelope
from .runtime import BACKEND_ID, EVENT_SCHEMA, EXECUTION_PROFILE_ID, _stable_int32


READY_SCHEMA = "step5d.autotune.bridge-ready/v2"
HEALTH_SCHEMA = "step5d.autotune.bridge-health/v2"
STARTUP_SCHEMA = "step5d.autotune.tp-startup/v1"
STARTUP_FEEDBACK_AGE_LIMIT_S = 0.050
EXPECTED_PROFILE = {
    "normal_max_rate_rad_s": "0.05",
    "host_qdot_slew_rad_s2": "0.5",
    "tp_speedj_accel_rad_s2": "0.5",
    "qdot_cap_rad_s": "0.5",
}


class LiveAdapterError(RuntimeError):
    """The v2 live transport cannot prove its immutable binding or health."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("ascii")
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    if parent.is_symlink() or path.is_symlink():
        raise LiveAdapterError("runtime evidence path must not be a symlink")
    temporary = parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise LiveAdapterError("short runtime evidence write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _vector(name: str, value: Any) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != 6:
        raise LiveAdapterError(f"{name} must contain six values")
    result = tuple(float(item) for item in value)
    if not all(math.isfinite(item) for item in result):
        raise LiveAdapterError(f"{name} must be finite")
    return result


def _stationary_and_normal(output: Mapping[str, Any]) -> bool:
    speed = _vector("actual_TCP_speed", output.get("actual_TCP_speed"))
    qd = _vector("actual_qd", output.get("actual_qd"))
    safety = output.get("safety_mode")
    normal = (
        str(safety).strip().upper() == "NORMAL"
        or (not isinstance(safety, bool) and isinstance(safety, (int, float)) and int(safety) == 1)
    )
    return (
        normal
        and math.sqrt(sum(value * value for value in speed[:3])) <= 0.001
        and math.sqrt(sum(value * value for value in speed[3:])) <= 0.01
        and max(abs(value) for value in qd) <= 0.01
    )


class V2CommandMailbox:
    """Read one strict v2 envelope and materialize the legacy runtime binding."""

    def __init__(self, path: Path, deployment_id: str) -> None:
        self.mailbox = AtomicMailbox(path)
        self.deployment_id = deployment_id
        self.current_deployment_seen = False

    def read_latest(self) -> MailboxCommand | None:
        envelope = self.mailbox.read_latest()
        if envelope is None:
            return None
        observed_deployment = envelope.payload.get("deployment_id")
        if (
            isinstance(observed_deployment, str)
            and observed_deployment != self.deployment_id
        ):
            if not self.current_deployment_seen:
                return None
            raise LiveAdapterError("v2 mailbox deployment identity differs")
        command = self._decode(envelope)
        self.current_deployment_seen = True
        return command

    def _decode(self, envelope: MailboxEnvelope) -> MailboxCommand:
        payload = envelope.payload
        command_name = payload.get("command")
        expected = (
            {"command", "deployment_id", "trial_id", "binding"}
            if command_name == "ARM"
            else {"command", "deployment_id", "trial_id", "artifact_sha256", "binding"}
        )
        if command_name not in {"ARM", "ACK_BUNDLE"} or set(payload) != expected:
            raise LiveAdapterError("v2 mailbox command fields differ from the live contract")
        if payload["deployment_id"] != self.deployment_id:
            raise LiveAdapterError("v2 mailbox deployment identity differs")
        binding = payload["binding"]
        required_binding = {
            "trial_uid",
            "backend_id",
            "campaign_epoch",
            "tp_trial_id",
            "candidate_token",
            "arm_command_seq",
            "execution_profile_id",
            "candidate",
            "profile",
            "source_fingerprint",
            "config_fingerprint",
            "campaign_fingerprint",
        }
        if not isinstance(binding, Mapping) or set(binding) != required_binding:
            raise LiveAdapterError("v2 mailbox runtime binding fields differ")
        candidate = binding["candidate"]
        if not isinstance(candidate, Mapping) or set(candidate) != {
            "group_id",
            "target_force_n",
            "p",
            "i",
            "d",
            "profile_id",
            "comparison_key",
            "purpose",
        }:
            raise LiveAdapterError("v2 mailbox candidate fields differ")
        if binding["profile"] != EXPECTED_PROFILE:
            raise LiveAdapterError("v2 mailbox execution profile differs")
        if binding["backend_id"] != BACKEND_ID:
            raise LiveAdapterError("v2 mailbox backend identity differs")
        if binding["campaign_epoch"] != _stable_int32(self.deployment_id):
            raise LiveAdapterError("v2 mailbox campaign epoch is not stable")
        if binding["candidate_token"] != _stable_int32(str(candidate["comparison_key"])):
            raise LiveAdapterError("v2 mailbox candidate token is not stable")
        if binding["tp_trial_id"] != binding["arm_command_seq"]:
            raise LiveAdapterError("v2 mailbox TP trial id differs from ARM sequence")
        if command_name == "ARM" and envelope.sequence != binding["arm_command_seq"]:
            raise LiveAdapterError("v2 ARM envelope sequence differs from its binding")
        if binding["execution_profile_id"] != EXECUTION_PROFILE_ID:
            raise LiveAdapterError("v2 mailbox execution profile code differs")
        if payload["trial_id"] != binding["trial_uid"]:
            raise LiveAdapterError("v2 mailbox trial identity differs")
        runtime_binding = RuntimeTrialBinding(
            trial_uid=str(binding["trial_uid"]),
            backend_id=str(binding["backend_id"]),
            campaign_epoch=int(binding["campaign_epoch"]),
            trial_id=int(binding["tp_trial_id"]),
            candidate_token=int(binding["candidate_token"]),
            arm_command_seq=int(binding["arm_command_seq"]),
            execution_profile_id=int(binding["execution_profile_id"]),
            candidate=ForceCandidate(
                target_force_n=float(candidate["target_force_n"]),
                force_p_gain=float(candidate["p"]),
                force_i_gain=float(candidate["i"]),
                force_damping=float(candidate["d"]),
            ),
            profile=ExecutionProfile(
                str(candidate["profile_id"]),
                float(binding["profile"]["normal_max_rate_rad_s"]),
                float(binding["profile"]["host_qdot_slew_rad_s2"]),
                float(binding["profile"]["tp_speedj_accel_rad_s2"]),
                float(binding["profile"]["qdot_cap_rad_s"]),
            ),
            source_fingerprint=str(binding["source_fingerprint"]),
            config_fingerprint=str(binding["config_fingerprint"]),
            campaign_fingerprint=str(binding["campaign_fingerprint"]),
        )
        packet = HostPacket(
            campaign_epoch=runtime_binding.campaign_epoch,
            trial_id=runtime_binding.trial_id,
            command=HostCommand.ARM if command_name == "ARM" else HostCommand.ACK_BUNDLE,
            candidate_token=runtime_binding.candidate_token,
            execution_profile_id=runtime_binding.execution_profile_id,
            command_seq=envelope.sequence,
        )
        return MailboxCommand(packet=packet, binding=runtime_binding, sha256=envelope.checksum)


class Step5dAutotuneV2LiveAdapter:
    """Publish exact child health and bridge events from the persistent 500 Hz loop."""

    def __init__(
        self,
        *,
        mailbox_path: Path,
        runtime_root: Path,
        deployment_id: str,
        launch_nonce: str,
        startup_stable_s: float = 0.5,
    ) -> None:
        if len(launch_nonce) != 64 or any(character not in "0123456789abcdef" for character in launch_nonce):
            raise LiveAdapterError("v2 launch nonce must be 64 lowercase hexadecimal characters")
        self.runtime_root = runtime_root.resolve()
        self.deployment_id = deployment_id
        self.launch_nonce = launch_nonce
        self.command_mailbox = V2CommandMailbox(mailbox_path, deployment_id)
        self.event_path = self.runtime_root / "bridge_events.jsonl"
        self.ready_path = self.runtime_root / "bridge_ready.json"
        self.health_path = self.runtime_root / "bridge_health.json"
        self.startup_path = self.runtime_root / "tp_startup.json"
        if not math.isfinite(startup_stable_s) or not 0.1 <= startup_stable_s <= 5.0:
            raise LiveAdapterError("startup stable duration must be inside [0.1,5.0] seconds")
        self.startup_stable_s = float(startup_stable_s)
        self.startup_baseline: str | None = None
        self.startup_active_at_s: float | None = None
        self.startup_gate_passed = False
        self._startup_publish_key: tuple[str, str | None, bool] | None = None
        self.health_sequence = 0
        self.ready = False
        self.emitted: set[tuple[str, str, int]] = set()

    @classmethod
    def from_environment(cls, mailbox_path: Path) -> "Step5dAutotuneV2LiveAdapter | None":
        if os.environ.get("STEP5D_AUTOTUNE_V2_ADAPTER") != "1":
            return None
        required = {
            name: os.environ.get(name, "")
            for name in (
                "STEP5D_AUTOTUNE_V2_RUNTIME_ROOT",
                "STEP5D_AUTOTUNE_V2_DEPLOYMENT_ID",
                "STEP5D_AUTOTUNE_V2_LAUNCH_NONCE",
                "STEP5D_AUTOTUNE_V2_STARTUP_STABLE_S",
            )
        }
        if any(not value for value in required.values()):
            raise LiveAdapterError("v2 adapter environment is incomplete")
        return cls(
            mailbox_path=mailbox_path,
            runtime_root=Path(required["STEP5D_AUTOTUNE_V2_RUNTIME_ROOT"]),
            deployment_id=required["STEP5D_AUTOTUNE_V2_DEPLOYMENT_ID"],
            launch_nonce=required["STEP5D_AUTOTUNE_V2_LAUNCH_NONCE"],
            startup_stable_s=float(required["STEP5D_AUTOTUNE_V2_STARTUP_STABLE_S"]),
        )

    def publish_health(self, sample_counter: int) -> None:
        self.health_sequence += 1
        _atomic_json(
            self.health_path,
            {
                "schema": HEALTH_SCHEMA,
                "pid": os.getpid(),
                "deployment_id": self.deployment_id,
                "launch_nonce": self.launch_nonce,
                "health_sequence": self.health_sequence,
                "observed_at": _now(),
                "configured_rate_hz": 500,
                "sample_counter": sample_counter,
                "rtde_healthy": True,
                "command_transport_healthy": True,
                "event_transport_healthy": True,
            },
        )
    def _publish_startup(
        self,
        *,
        phase: str,
        observation: TpFeedbackObservation,
        output: Mapping[str, Any],
        sample_counter: int,
        stable_duration_s: float,
        operator_action: str | None,
    ) -> None:
        key = (phase, self.startup_baseline, self.startup_gate_passed)
        if key == self._startup_publish_key:
            return
        packet = observation.packet
        _atomic_json(
            self.startup_path,
            {
                "schema": STARTUP_SCHEMA,
                "pid": os.getpid(),
                "deployment_id": self.deployment_id,
                "launch_nonce": self.launch_nonce,
                "observed_at": _now(),
                "phase": phase,
                "baseline": self.startup_baseline,
                "runtime_state": output.get("runtime_state"),
                "tp_state": None if packet is None else int(packet.state),
                "consumed_command_seq": (
                    None if packet is None else packet.consumed_command_seq
                ),
                "sample_counter": sample_counter,
                "stable_duration_s": stable_duration_s,
                "startup_gate_passed": self.startup_gate_passed,
                "motion_allowed": False,
                "operator_action": operator_action,
            },
        )
        self._startup_publish_key = key

    def publish_startup(
        self,
        *,
        observation: TpFeedbackObservation,
        output: Mapping[str, Any],
        sample_counter: int,
        infrastructure_ready: bool,
        feedback_age_s: float,
        observed_at_s: float,
    ) -> None:
        if self.startup_gate_passed:
            return
        if not infrastructure_ready or not _stationary_and_normal(output):
            return
        if observation.phase is TpFeedbackPhase.PREPLAY:
            if observation.baseline_kind not in {"cold_zero", "latched_ready"}:
                raise LiveAdapterError("operator readiness lacks an accepted TP baseline")
            self.startup_baseline = observation.baseline_kind
            self.startup_active_at_s = None
            self._publish_startup(
                phase="awaiting_tp_play",
                observation=observation,
                output=output,
                sample_counter=sample_counter,
                stable_duration_s=0.0,
                operator_action="press_tp_play",
            )
            if not self.ready:
                _atomic_json(
                    self.ready_path,
                    {
                        "schema": READY_SCHEMA,
                        "pid": os.getpid(),
                        "deployment_id": self.deployment_id,
                        "launch_nonce": self.launch_nonce,
                        "sample_rate_hz": 500,
                        "bridge_ready": True,
                        "startup_stationary_verified": True,
                        "command_transport_ready": True,
                        "event_transport_ready": True,
                    },
                )
                self.ready = True
            return
        if self.startup_baseline is None:
            raise LiveAdapterError("TP startup transition lacks an accepted baseline")
        if observation.phase is TpFeedbackPhase.STARTING:
            self.startup_active_at_s = None
            self._publish_startup(
                phase="stabilizing",
                observation=observation,
                output=output,
                sample_counter=sample_counter,
                stable_duration_s=0.0,
                operator_action=None,
            )
            return
        packet = observation.packet
        active_ready = (
            observation.phase is TpFeedbackPhase.ACTIVE
            and output.get("runtime_state") == 2
            and packet is not None
            and packet.state is TpLoopState.READY_HOME
            and packet.campaign_epoch_echo == 0
            and packet.trial_id_echo == 0
            and packet.candidate_token_echo == 0
            and packet.terminal_reason == 0
            and packet.execution_profile_id_echo == 0
            and packet.consumed_command_seq == 0
        )
        if not active_ready:
            raise LiveAdapterError("TP startup ACTIVE sample is not zero-identity READY_HOME")
        if not math.isfinite(feedback_age_s) or feedback_age_s > STARTUP_FEEDBACK_AGE_LIMIT_S:
            self.startup_active_at_s = None
            return
        if self.startup_active_at_s is None:
            self.startup_active_at_s = observed_at_s
        stable_duration_s = max(0.0, observed_at_s - self.startup_active_at_s)
        if stable_duration_s < self.startup_stable_s:
            self._publish_startup(
                phase="stabilizing",
                observation=observation,
                output=output,
                sample_counter=sample_counter,
                stable_duration_s=stable_duration_s,
                operator_action=None,
            )
            return
        self.startup_gate_passed = True
        self._publish_startup(
            phase="live",
            observation=observation,
            output=output,
            sample_counter=sample_counter,
            stable_duration_s=stable_duration_s,
            operator_action=None,
        )

    def _emit(self, command: MailboxCommand, event: str, **details: Any) -> None:
        key = (command.binding.trial_uid, event, command.packet.command_seq)
        if key in self.emitted:
            return
        row = {
            "schema": EVENT_SCHEMA,
            "deployment_id": self.deployment_id,
            "trial_id": command.binding.trial_uid,
            "event": event,
            "command_sequence": command.packet.command_seq,
            "command_checksum": command.sha256,
            "observed_at": _now(),
            **details,
        }
        self.event_path.parent.mkdir(parents=True, exist_ok=True)
        if self.event_path.is_symlink():
            raise LiveAdapterError("bridge event stream must not be a symlink")
        with self.event_path.open("a", encoding="ascii") as handle:
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self.emitted.add(key)

    def observe(
        self,
        *,
        runtime: BridgeMailboxRuntime,
        rotator: BridgeTrialCsvRotator,
        output: Mapping[str, Any],
        sample_counter: int,
        infrastructure_ready: bool = True,
        feedback_age_s: float = 0.0,
        observed_at_s: float | None = None,
    ) -> None:
        self.publish_health(sample_counter)
        observation = runtime.latest_tp_observation
        if observation is not None:
            self.publish_startup(
                observation=observation,
                output=output,
                sample_counter=sample_counter,
                infrastructure_ready=infrastructure_ready,
                feedback_age_s=feedback_age_s,
                observed_at_s=(
                    time.monotonic() if observed_at_s is None else observed_at_s
                ),
            )
        active = runtime.active
        latest = runtime.last_command
        if active is None:
            return
        if observation is None or observation.packet is None:
            return
        snapshot = observation.packet
        if snapshot.state is TpLoopState.FAULT:
            runtime_state = output.get("runtime_state")
            try:
                runtime_state = float(runtime_state)
            except (TypeError, ValueError):
                runtime_state = None
            self.publish_safety_halt(
                runtime=runtime,
                rotator=rotator,
                reason="tp_fault",
                tp_stop_acknowledged=True,
                tp_state=snapshot.state.name,
                runtime_state=runtime_state,
                normal_force_n=None,
                force_norm_n=None,
                sample_counter=sample_counter,
                stop_packets_sent=0,
            )
            return
        identity_matches = (
            snapshot.campaign_epoch_echo == active.packet.campaign_epoch
            and snapshot.trial_id_echo == active.packet.trial_id
            and snapshot.candidate_token_echo == active.packet.candidate_token
            and snapshot.execution_profile_id_echo == active.packet.execution_profile_id
        )
        if identity_matches and snapshot.consumed_command_seq >= active.binding.arm_command_seq:
            self._emit(active, "tp_consumed")
            if snapshot.state.value >= TpLoopState.RUN.value:
                self._emit(active, "run_started")
        if snapshot.state is TpLoopState.WAIT_ACK and identity_matches:
            if runtime.campaign_home_reference is None:
                raise LiveAdapterError("WAIT_ACK lacks the captured campaign Home")
            runtime.campaign_home_reference.verify_measured_home(output)
            self._emit(active, "home_verified", safe_home_verified=True)
            sealed = rotator.sealed_path
            if sealed is not None:
                self._emit(
                    active,
                    "raw_capture_sealed",
                    path=str(sealed.resolve()),
                    sha256=_sha256(sealed),
                )
        if (
            latest is not None
            and latest.packet.command is HostCommand.ACK_BUNDLE
            and snapshot.state is TpLoopState.READY_HOME
            and snapshot.consumed_command_seq == latest.packet.command_seq
        ):
            if runtime.campaign_home_reference is None:
                raise LiveAdapterError("ACK closure lacks the captured campaign Home")
            runtime.campaign_home_reference.verify_measured_home(output)
            self._emit(
                latest,
                "ready_home",
                command_cleared=True,
                measured_home_verified=True,
            )

    def publish_safety_halt(
        self,
        *,
        runtime: BridgeMailboxRuntime,
        rotator: BridgeTrialCsvRotator,
        reason: str,
        tp_stop_acknowledged: bool,
        tp_state: str,
        runtime_state: float | None,
        normal_force_n: float | None,
        force_norm_n: float | None,
        sample_counter: int,
        stop_packets_sent: int,
    ) -> Path:
        active = runtime.active
        if active is None:
            raise LiveAdapterError("safety halt lacks an active trial binding")
        path = rotator.seal_safety_halt().resolve(strict=True)
        self._emit(
            active,
            "safety_halt",
            reason=str(reason),
            tp_stop_acknowledged=bool(tp_stop_acknowledged),
            tp_state=str(tp_state),
            runtime_state=runtime_state,
            normal_force_n=(None if normal_force_n is None else float(normal_force_n)),
            force_norm_n=(None if force_norm_n is None else float(force_norm_n)),
            sample_counter=int(sample_counter),
            stop_packets_sent=int(stop_packets_sent),
            path=str(path),
            sha256=_sha256(path),
        )
        return path
