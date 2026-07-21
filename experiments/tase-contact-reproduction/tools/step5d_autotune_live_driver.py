#!/usr/bin/env python3
"""Fail-closed continuous host seam for Step5d-native autotune.

This module deliberately contains no controller connection or program-start
code.  ``AtomicCommandMailbox`` is the coordinator command sink, while
``BridgeMailboxRuntime`` is consumed by one already-owned persistent bridge
process.  Tests can therefore exercise the complete integer transaction and
host safe-closure logic without enabling robot I/O.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from ur10e_experiment_runtime.candidate_identity import (
    ControlCandidateUid,
    OccurrenceUid,
    TransportCandidateUid,
)

from step5d_autotune_contract import (
    CaptureArtifactPaths,
    CaptureManifest,
    ClosureEvidence,
    Evaluation,
    ExecutionProfile,
    ForceCandidate,
    SafeClosureEvidence,
    TypedSafeClosureEvidence,
    TrialSpec,
)
from step5d_autotune_state_machine import (
    FULL_HOME_ROLLING_PROTOCOL,
    HostCommand,
    HostPacket,
    TpLoopState,
    TpPacket,
)


MAILBOX_SCHEMA = "step5d_autotune_command_mailbox_v1"
INT32_MAX = 2_147_483_647
NORMAL_LEVELS = {
    1: 0.010,
    2: 0.015,
    3: 0.020,
    4: 0.030,
    5: 0.050,
    6: 0.100,
}
ACTUATOR_LEVELS = {1: 0.1, 2: 0.2, 3: 0.5}
HOST_TO_TP_NAMES = (
    "campaign_epoch",
    "trial_id",
    "command",
    "candidate_token",
    "execution_profile_id",
    "command_seq",
)
OPTIONAL_HOST_TO_TP_NAMES = ("logical_batch_sequence",)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class MailboxError(RuntimeError):
    """Strict mailbox or command-phase validation failed."""


class IncompleteTpSnapshot(MailboxError):
    """No complete TP feedback sample is available yet."""


class LegacyFloatStopRequired(MailboxError):
    """RUN cannot be interrupted with the outer integer STOP command."""


class ClosureNotReady(RuntimeError):
    """No current, exact WAIT_ACK safe segment can be finalized."""


def terminal_float_reason_crosscheck(
    samples: Iterable[tuple[TpPacket, float | None]],
    *,
    final_reason: int,
) -> bool:
    """Cross-check only rows where the integer terminal reason is authoritative."""

    reasons = {
        int(value)
        for packet, value in samples
        if packet.terminal_reason == final_reason
        and packet.state
        in {
            TpLoopState.TERMINAL,
            TpLoopState.RETRACT,
            TpLoopState.RETURN,
            TpLoopState.HOME_VERIFY,
            TpLoopState.WAIT_ACK,
            TpLoopState.READY_NEAR,
            TpLoopState.READY_HOME_CLOSED,
            TpLoopState.READY_HOME_NEXT,
        }
        and value is not None
        and math.isfinite(value)
        and value > 0.0
        and value.is_integer()
    }
    return reasons == {final_reason}


def _strict_int(name: str, value: Any, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MailboxError(f"{name} must be an integer")
    minimum = 1 if positive else 0
    if value < minimum or value > INT32_MAX:
        raise MailboxError(f"{name} is outside the permitted INT32 range")
    return value


def _finite(name: str, value: Any) -> float:
    if isinstance(value, bool):
        raise MailboxError(f"{name} must be numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise MailboxError(f"{name} must be numeric") from exc
    if not math.isfinite(parsed):
        raise MailboxError(f"{name} must be finite")
    return parsed


def _exact_level(name: str, value: Any, levels: Mapping[int, float]) -> tuple[int, float]:
    parsed = _finite(name, value)
    for index, level in levels.items():
        if math.isclose(parsed, level, rel_tol=0.0, abs_tol=1e-12):
            return index, level
    raise MailboxError(f"{name} is outside the frozen execution-profile lattice")


def decode_execution_profile_id(
    execution_profile_id: int,
    *,
    network_mode: bool,
) -> tuple[float, float, float]:
    """Decode and validate all three digits, not merely TP's ones digit."""

    value = _strict_int("execution_profile_id", execution_profile_id, positive=True)
    if value < 100 or value > 999:
        raise MailboxError("execution_profile_id must contain exactly three level digits")
    normal_level = value // 100
    host_level = (value // 10) % 10
    tp_level = value % 10
    if normal_level not in NORMAL_LEVELS:
        raise MailboxError("execution_profile_id has an invalid normal-rate digit")
    if host_level not in ACTUATOR_LEVELS:
        raise MailboxError("execution_profile_id has an invalid host-slew digit")
    if tp_level not in ACTUATOR_LEVELS:
        raise MailboxError("execution_profile_id has an invalid TP-accel digit")
    if network_mode and normal_level == 4:
        raise MailboxError("the .030 rad/s profile is offline_only and forbidden on network/live")
    return (
        NORMAL_LEVELS[normal_level],
        ACTUATOR_LEVELS[host_level],
        ACTUATOR_LEVELS[tp_level],
    )


def execution_profile_id_for(profile: ExecutionProfile, *, network_mode: bool) -> int:
    normal_digit, normal = _exact_level(
        "normal_max_rate_rad_s", profile.normal_max_rate_rad_s, NORMAL_LEVELS
    )
    host_digit, host = _exact_level(
        "host_qdot_slew_rad_s2", profile.host_qdot_slew_rad_s2, ACTUATOR_LEVELS
    )
    tp_digit, tp = _exact_level(
        "tp_speedj_accel_rad_s2", profile.tp_speedj_accel_rad_s2, ACTUATOR_LEVELS
    )
    if network_mode and (normal_digit == 4 or not profile.live_eligible):
        raise MailboxError("offline_only execution profile cannot enter the network mailbox")
    encoded = 100 * normal_digit + 10 * host_digit + tp_digit
    if decode_execution_profile_id(encoded, network_mode=network_mode) != (
        normal,
        host,
        tp,
    ):
        raise AssertionError("execution-profile codec is not self-consistent")
    return encoded


def validate_execution_profile_binding(
    profile: ExecutionProfile,
    execution_profile_id: int,
    *,
    network_mode: bool,
) -> None:
    encoded = execution_profile_id_for(profile, network_mode=network_mode)
    if execution_profile_id != encoded:
        raise MailboxError(
            "execution_profile_id does not exactly cross-check normal/host/TP levels"
        )


def integer_stop_transport(state: TpLoopState) -> str:
    """Return the only permitted stop carrier for the current TP phase."""

    if state is TpLoopState.RUN:
        return "legacy_float_stop_request"
    if state in {
        TpLoopState.READY_HOME,
        TpLoopState.WAIT_ACK,
        TpLoopState.READY_NEAR,
        TpLoopState.READY_HOME_CLOSED,
        TpLoopState.READY_HOME_NEXT,
        TpLoopState.WAIT_INFRA_READY,
        TpLoopState.FAULT,
    }:
        return "integer_stop"
    return "fail_closed"


@dataclass(frozen=True)
class RuntimeTrialBinding:
    trial_uid: str
    backend_id: str
    campaign_epoch: int
    trial_id: int
    candidate_token: int
    arm_command_seq: int
    execution_profile_id: int
    candidate: ForceCandidate
    profile: ExecutionProfile
    source_fingerprint: str
    config_fingerprint: str
    campaign_fingerprint: str
    trial_overlay: Mapping[str, Any] | None = None
    batch_row_index: int | None = None
    logical_batch_sequence: int | None = None
    occurrence_uid: OccurrenceUid | None = None
    transport_candidate_uid: TransportCandidateUid | None = None
    control_candidate_uid: ControlCandidateUid | None = None
    trial_overlay_sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.trial_uid, str) or not _SHA256_RE.fullmatch(self.trial_uid):
            raise MailboxError("trial_uid must be a lowercase SHA-256 identity")
        if not isinstance(self.backend_id, str) or not self.backend_id.strip():
            raise MailboxError("backend_id must be a non-empty string")
        for name in (
            "campaign_epoch",
            "trial_id",
            "candidate_token",
            "arm_command_seq",
            "execution_profile_id",
        ):
            _strict_int(name, getattr(self, name), positive=True)
        for name in (
            "source_fingerprint",
            "config_fingerprint",
            "campaign_fingerprint",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
                raise MailboxError(f"{name} must be a lowercase SHA-256 identity")
        if self.batch_row_index is not None and (
            isinstance(self.batch_row_index, bool)
            or not isinstance(self.batch_row_index, int)
            or not 1 <= self.batch_row_index <= 10
        ):
            raise MailboxError("batch_row_index must be in [1,10]")
        if self.logical_batch_sequence is not None:
            _strict_int(
                "logical_batch_sequence",
                self.logical_batch_sequence,
                positive=True,
            )
        identity_values = (
            self.occurrence_uid,
            self.transport_candidate_uid,
            self.control_candidate_uid,
            self.trial_overlay_sha256,
        )
        if self.logical_batch_sequence is not None and not all(
            value is not None for value in identity_values
        ):
            raise MailboxError("rolling runtime binding requires complete UID closure")
        if any(value is not None for value in identity_values):
            if not all(value is not None for value in identity_values):
                raise MailboxError("rolling runtime UID closure is incomplete")
            try:
                object.__setattr__(
                    self,
                    "occurrence_uid",
                    OccurrenceUid.parse(self.occurrence_uid),
                )
                object.__setattr__(
                    self,
                    "transport_candidate_uid",
                    TransportCandidateUid.parse(self.transport_candidate_uid),
                )
                object.__setattr__(
                    self,
                    "control_candidate_uid",
                    ControlCandidateUid.parse(self.control_candidate_uid),
                )
            except (TypeError, ValueError) as exc:
                raise MailboxError(f"rolling runtime UID closure is invalid: {exc}") from exc
            if not isinstance(self.trial_overlay_sha256, str) or not _SHA256_RE.fullmatch(
                self.trial_overlay_sha256
            ):
                raise MailboxError("rolling normalized overlay SHA is invalid")

    def payload(self) -> dict[str, Any]:
        payload = {
            "trial_uid": self.trial_uid,
            "backend_id": self.backend_id,
            "campaign_epoch": self.campaign_epoch,
            "trial_id": self.trial_id,
            "candidate_token": self.candidate_token,
            "arm_command_seq": self.arm_command_seq,
            "execution_profile_id": self.execution_profile_id,
            "candidate": {
                "target_force_n": self.candidate.target_force_n,
                "force_p_gain": self.candidate.force_p_gain,
                "force_i_gain": self.candidate.force_i_gain,
                "force_damping": self.candidate.force_damping,
            },
            "profile": self.profile.payload(),
            "source_fingerprint": self.source_fingerprint,
            "config_fingerprint": self.config_fingerprint,
            "campaign_fingerprint": self.campaign_fingerprint,
        }
        if self.trial_overlay is not None:
            payload["trial_overlay"] = dict(self.trial_overlay)
        if self.batch_row_index is not None:
            payload["batch_row_index"] = self.batch_row_index
        if self.logical_batch_sequence is not None:
            payload["logical_batch_sequence"] = self.logical_batch_sequence
        if self.occurrence_uid is not None:
            payload.update(
                occurrence_uid=self.occurrence_uid,
                transport_candidate_uid=self.transport_candidate_uid,
                control_candidate_uid=self.control_candidate_uid,
                trial_overlay_sha256=self.trial_overlay_sha256,
            )
        return payload


@dataclass(frozen=True)
class MailboxCommand:
    packet: HostPacket
    binding: RuntimeTrialBinding
    sha256: str

    @property
    def handshake(self) -> dict[str, int]:
        payload = {
            "campaign_epoch": self.packet.campaign_epoch,
            "trial_id": self.packet.trial_id,
            "command": int(self.packet.command),
            "candidate_token": self.packet.candidate_token,
            "execution_profile_id": self.packet.execution_profile_id,
            "command_seq": self.packet.command_seq,
        }
        payload["batch_row_index"] = self.binding.batch_row_index or 0
        payload["logical_batch_sequence"] = (
            self.binding.logical_batch_sequence or 0
        )
        return payload

    @property
    def arm_gate_binding(self) -> dict[str, Any]:
        if self.packet.command is not HostCommand.ARM:
            raise MailboxError("only ARM commands have an ARM gate binding")
        return {
            "mailbox_sha256": self.sha256,
            "campaign_epoch": self.packet.campaign_epoch,
            "trial_id": self.packet.trial_id,
            "command": int(self.packet.command),
            "candidate_token": self.packet.candidate_token,
            "execution_profile_id": self.packet.execution_profile_id,
            "command_seq": self.packet.command_seq,
            "logical_batch_sequence": self.packet.logical_batch_sequence,
            "trial_uid": self.binding.trial_uid,
        }


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key is forbidden: {key}")
        result[key] = value
    return result


def _strict_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("ascii")


def _packet_payload(packet: HostPacket) -> dict[str, int]:
    payload = {
        "campaign_epoch": packet.campaign_epoch,
        "trial_id": packet.trial_id,
        "command": int(packet.command),
        "candidate_token": packet.candidate_token,
        "execution_profile_id": packet.execution_profile_id,
        "command_seq": packet.command_seq,
    }
    if packet.logical_batch_sequence:
        payload["logical_batch_sequence"] = packet.logical_batch_sequence
    return payload


def _binding_from_prepared(
    packet: HostPacket,
    prepared_trial: Any,
    *,
    network_mode: bool,
    launch_profile: Any | None,
) -> RuntimeTrialBinding:
    trial = getattr(prepared_trial, "trial", None)
    if trial is None:
        raise MailboxError("prepared_trial lacks its durable TrialSpec")
    expected_identity = (
        trial.campaign.campaign_epoch,
        trial.trial_id,
        trial.candidate_token,
    )
    if (
        packet.campaign_epoch,
        packet.trial_id,
        packet.candidate_token,
    ) != expected_identity:
        raise MailboxError("HostPacket identity differs from prepared TrialSpec")
    if packet.command is HostCommand.ARM and packet.command_seq != trial.command_seq:
        raise MailboxError("ARM command_seq differs from prepared TrialSpec")
    if packet.command is HostCommand.ACK_BUNDLE and packet.command_seq <= trial.command_seq:
        raise MailboxError("ACK command_seq must be newer than the trial ARM")
    if packet.command not in {
        HostCommand.ARM,
        HostCommand.ACK_BUNDLE,
        HostCommand.STOP,
        HostCommand.COMPLETE_AT_HOME,
    }:
        raise MailboxError(
            "mailbox supports only ARM, ACK_BUNDLE, STOP, and COMPLETE_AT_HOME"
        )
    validate_execution_profile_binding(
        trial.execution_profile,
        packet.execution_profile_id,
        network_mode=network_mode,
    )
    frozen = getattr(prepared_trial, "frozen", None)
    if frozen is None:
        raise MailboxError("prepared_trial lacks its frozen fingerprint")
    binding = RuntimeTrialBinding(
        trial_uid=trial.trial_uid,
        backend_id=trial.backend_id,
        campaign_epoch=trial.campaign.campaign_epoch,
        trial_id=trial.trial_id,
        candidate_token=trial.candidate_token,
        arm_command_seq=trial.command_seq,
        execution_profile_id=packet.execution_profile_id,
        candidate=trial.candidate,
        profile=trial.execution_profile,
        source_fingerprint=trial.source_fingerprint,
        config_fingerprint=trial.config_fingerprint,
        campaign_fingerprint=trial.campaign.campaign_fingerprint,
        trial_overlay=getattr(prepared_trial, "trial_overlay", None),
        batch_row_index=getattr(prepared_trial, "batch_row_index", None),
        logical_batch_sequence=(
            packet.logical_batch_sequence or None
        ),
        occurrence_uid=getattr(prepared_trial, "occurrence_uid", None),
        transport_candidate_uid=getattr(
            prepared_trial, "transport_candidate_uid", None
        ),
        control_candidate_uid=getattr(prepared_trial, "control_candidate_uid", None),
        trial_overlay_sha256=getattr(prepared_trial, "trial_overlay_sha256", None),
    )
    if any(
        (
            getattr(frozen, "source_fingerprint", None) != binding.source_fingerprint,
            getattr(frozen, "config_fingerprint", None) != binding.config_fingerprint,
            getattr(frozen, "composite_fingerprint", None)
            != binding.campaign_fingerprint,
        )
    ):
        raise MailboxError("prepared frozen fingerprint differs from TrialSpec")
    environment = getattr(prepared_trial, "environment", None)
    if not isinstance(environment, Mapping):
        raise MailboxError("prepared_trial lacks its exact runtime environment")
    expected_numbers = {
        "STEP5D_AUTOTUNE_FORCE_P": binding.candidate.force_p_gain,
        "STEP5D_AUTOTUNE_FORCE_I": binding.candidate.force_i_gain,
        "STEP5D_AUTOTUNE_FORCE_DAMPING": binding.candidate.force_damping,
        "STEP5D_AUTOTUNE_NORMAL_RATE_RAD_S": binding.profile.normal_max_rate_rad_s,
        "STEP5D_AUTOTUNE_HOST_SLEW_RAD_S2": binding.profile.host_qdot_slew_rad_s2,
        "STEP5D_AUTOTUNE_SPEEDJ_ACCELERATION_RAD_S2": (
            binding.profile.tp_speedj_accel_rad_s2
        ),
    }
    for name, expected in expected_numbers.items():
        actual = _finite(name, environment.get(name))
        if not math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-12):
            raise MailboxError(f"prepared runtime differs from TrialSpec at {name}")
    if "BRIDGE_NORMAL_FILTER_ALPHA" in environment:
        raise MailboxError("normal_filter_alpha is forbidden in Step5d autotune")
    if binding.trial_overlay is not None:
        from step5d_autotune_v3.runtime_profile import (
            normalized_overlay_sha256,
            normalize_trial_overlay,
        )

        if launch_profile is None:
            raise MailboxError("V3 trial overlay lacks its immutable launch profile")
        try:
            normalized = normalize_trial_overlay(
                binding.trial_overlay,
                profile=launch_profile,
            )
        except (OSError, ValueError) as exc:
            raise MailboxError(f"V3 trial overlay is invalid: {exc}") from exc
        expected_overlay_identity = {
            "execution_profile_id": binding.profile.profile_id,
        }
        for name, expected in expected_overlay_identity.items():
            if normalized[name] != expected:
                raise MailboxError(f"V3 trial overlay differs from TrialSpec at {name}")
        expected_overlay_sha256 = getattr(
            prepared_trial,
            "trial_overlay_sha256",
            None,
        )
        if binding.logical_batch_sequence is not None and (
            expected_overlay_sha256 is None
            or normalized_overlay_sha256(
                launch_profile,
                normalized,
            )
            != expected_overlay_sha256
        ):
            raise MailboxError("V3 rolling overlay SHA differs from selected plan row")
        if binding.control_candidate_uid is not None and (
            normalized["control_candidate_uid"] != binding.control_candidate_uid
            or expected_overlay_sha256 != binding.trial_overlay_sha256
        ):
            raise MailboxError("V3 rolling overlay differs from UID closure")
        object.__setattr__(binding, "trial_overlay", normalized)
    return binding


class AtomicCommandMailbox:
    """Atomic+fsync command sink implementing ``ContinuousTpCommandSink``."""

    def __init__(
        self,
        path: Path,
        *,
        network_mode: bool = True,
        launch_profile: Any | None = None,
    ) -> None:
        if not isinstance(path, Path) or not path.is_absolute():
            raise MailboxError("command mailbox path must be absolute")
        self.path = path
        self.network_mode = bool(network_mode)
        self.launch_profile = launch_profile

    def send_command(self, packet: HostPacket, *, prepared_trial: Any) -> None:
        binding = _binding_from_prepared(
            packet,
            prepared_trial,
            network_mode=self.network_mode,
            launch_profile=self.launch_profile,
        )
        payload = {
            "schema": MAILBOX_SCHEMA,
            "packet": _packet_payload(packet),
            "runtime": binding.payload(),
        }
        encoded = _strict_json_bytes(payload)
        parent = self.path.parent
        if not parent.is_dir() or parent.is_symlink():
            raise MailboxError("command mailbox parent must be an existing real directory")
        if self.path.exists() and self.path.is_symlink():
            raise MailboxError("command mailbox must not replace a symlink")
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(
            os, "O_NOFOLLOW", 0
        )
        directory_fd = os.open(parent, directory_flags)
        temporary = parent / f".{self.path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        descriptor = -1
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise MailboxError("short mailbox write")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, self.path)
            os.fsync(directory_fd)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            os.close(directory_fd)

    def read_latest(self) -> MailboxCommand | None:
        try:
            descriptor = os.open(
                self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise MailboxError("command mailbox is unsafe or unreadable") from exc
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise MailboxError("command mailbox must be a singly-linked regular file")
            chunks: list[bytes] = []
            digest = hashlib.sha256()
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
                digest.update(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        try:
            current = self.path.stat(follow_symlinks=False)
        except OSError as exc:
            raise MailboxError("command mailbox changed while being read") from exc
        identities = {
            (row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns)
            for row in (before, after, current)
        }
        if len(identities) != 1:
            raise MailboxError("command mailbox changed while being read")
        encoded = b"".join(chunks)
        try:
            payload = json.loads(
                encoded.decode("ascii"),
                parse_constant=_reject_constant,
                object_pairs_hook=_unique_object,
            )
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise MailboxError(f"command mailbox is not strict finite JSON: {exc}") from exc
        return _mailbox_command_from_payload(
            payload,
            digest.hexdigest(),
            network_mode=self.network_mode,
            launch_profile=self.launch_profile,
        )


def _mailbox_command_from_payload(
    payload: Any,
    digest: str,
    *,
    network_mode: bool,
    launch_profile: Any | None,
) -> MailboxCommand:
    if not isinstance(payload, Mapping) or set(payload) != {"schema", "packet", "runtime"}:
        raise MailboxError("command mailbox has unknown or missing top-level fields")
    if payload["schema"] != MAILBOX_SCHEMA:
        raise MailboxError("command mailbox schema mismatch")
    raw_packet = payload["packet"]
    packet_fields = set(raw_packet) if isinstance(raw_packet, Mapping) else set()
    if not set(HOST_TO_TP_NAMES).issubset(packet_fields) or not packet_fields.issubset(
        set(HOST_TO_TP_NAMES) | set(OPTIONAL_HOST_TO_TP_NAMES)
    ):
        raise MailboxError("command mailbox packet fields differ from the register contract")
    try:
        command = HostCommand(_strict_int("command", raw_packet["command"]))
    except ValueError as exc:
        raise MailboxError("unknown host command") from exc
    packet = HostPacket(
        campaign_epoch=_strict_int(
            "campaign_epoch", raw_packet["campaign_epoch"], positive=True
        ),
        trial_id=_strict_int("trial_id", raw_packet["trial_id"], positive=True),
        command=command,
        candidate_token=_strict_int(
            "candidate_token", raw_packet["candidate_token"], positive=True
        ),
        execution_profile_id=_strict_int(
            "execution_profile_id", raw_packet["execution_profile_id"], positive=True
        ),
        command_seq=_strict_int(
            "command_seq", raw_packet["command_seq"], positive=True
        ),
        logical_batch_sequence=(
            0
            if raw_packet.get("logical_batch_sequence") is None
            else _strict_int(
                "logical_batch_sequence",
                raw_packet["logical_batch_sequence"],
                positive=True,
            )
        ),
    )
    if command not in {
        HostCommand.ARM,
        HostCommand.ACK_BUNDLE,
        HostCommand.STOP,
        HostCommand.COMPLETE_AT_HOME,
    }:
        raise MailboxError("mailbox command is outside ARM/ACK/STOP/COMPLETE")
    raw_runtime = payload["runtime"]
    expected_runtime = {
        "trial_uid",
        "backend_id",
        "campaign_epoch",
        "trial_id",
        "candidate_token",
        "arm_command_seq",
        "execution_profile_id",
        "candidate",
        "profile",
        "source_fingerprint",
        "config_fingerprint",
        "campaign_fingerprint",
    }
    runtime_fields = set(raw_runtime) if isinstance(raw_runtime, Mapping) else set()
    optional_runtime = {
        "trial_overlay",
        "batch_row_index",
        "logical_batch_sequence",
        "occurrence_uid",
        "transport_candidate_uid",
        "control_candidate_uid",
        "trial_overlay_sha256",
    }
    if not expected_runtime.issubset(runtime_fields) or not runtime_fields.issubset(
        expected_runtime | optional_runtime
    ):
        raise MailboxError("command mailbox runtime binding is incomplete")
    raw_candidate = raw_runtime["candidate"]
    if not isinstance(raw_candidate, Mapping) or set(raw_candidate) != {
        "target_force_n",
        "force_p_gain",
        "force_i_gain",
        "force_damping",
    }:
        raise MailboxError("command mailbox candidate is not canonical")
    try:
        candidate = ForceCandidate.from_payload(raw_candidate)
        profile = ExecutionProfile(**raw_runtime["profile"])
    except (TypeError, ValueError) as exc:
        raise MailboxError(f"command mailbox runtime is invalid: {exc}") from exc
    binding = RuntimeTrialBinding(
        trial_uid=raw_runtime["trial_uid"],
        backend_id=raw_runtime["backend_id"],
        campaign_epoch=_strict_int(
            "runtime campaign_epoch", raw_runtime["campaign_epoch"], positive=True
        ),
        trial_id=_strict_int(
            "runtime trial_id", raw_runtime["trial_id"], positive=True
        ),
        candidate_token=_strict_int(
            "runtime candidate_token", raw_runtime["candidate_token"], positive=True
        ),
        arm_command_seq=_strict_int(
            "runtime arm_command_seq", raw_runtime["arm_command_seq"], positive=True
        ),
        execution_profile_id=_strict_int(
            "runtime execution_profile_id",
            raw_runtime["execution_profile_id"],
            positive=True,
        ),
        candidate=candidate,
        profile=profile,
        source_fingerprint=raw_runtime["source_fingerprint"],
        config_fingerprint=raw_runtime["config_fingerprint"],
        campaign_fingerprint=raw_runtime["campaign_fingerprint"],
        trial_overlay=raw_runtime.get("trial_overlay"),
        batch_row_index=(
            None
            if raw_runtime.get("batch_row_index") is None
            else _strict_int(
                "runtime batch_row_index",
                raw_runtime["batch_row_index"],
                positive=True,
            )
        ),
        logical_batch_sequence=(
            None
            if raw_runtime.get("logical_batch_sequence") is None
            else _strict_int(
                "runtime logical_batch_sequence",
                raw_runtime["logical_batch_sequence"],
                positive=True,
            )
        ),
        occurrence_uid=raw_runtime.get("occurrence_uid"),
        transport_candidate_uid=raw_runtime.get("transport_candidate_uid"),
        control_candidate_uid=raw_runtime.get("control_candidate_uid"),
        trial_overlay_sha256=raw_runtime.get("trial_overlay_sha256"),
    )
    if binding.trial_overlay is not None:
        from step5d_autotune_v3.runtime_profile import (
            normalize_trial_overlay,
        )

        if launch_profile is None:
            raise MailboxError("V3 trial overlay lacks its immutable launch profile")
        try:
            normalized_overlay = normalize_trial_overlay(
                binding.trial_overlay,
                profile=launch_profile,
            )
        except (OSError, ValueError) as exc:
            raise MailboxError(f"V3 trial overlay is invalid: {exc}") from exc
        if normalized_overlay["execution_profile_id"] != binding.profile.profile_id:
            raise MailboxError("V3 trial overlay identity differs from runtime binding")
        if binding.control_candidate_uid is not None:
            from step5d_autotune_v3.runtime_profile import normalized_overlay_sha256

            if (
                normalized_overlay["control_candidate_uid"]
                != binding.control_candidate_uid
                or normalized_overlay_sha256(
                    launch_profile,
                    normalized_overlay,
                )
                != binding.trial_overlay_sha256
            ):
                raise MailboxError("V3 trial overlay differs from runtime UID closure")
        object.__setattr__(binding, "trial_overlay", normalized_overlay)
    validate_execution_profile_binding(
        binding.profile,
        packet.execution_profile_id,
        network_mode=network_mode,
    )
    if any(
        (
            binding.campaign_epoch != packet.campaign_epoch,
            binding.trial_id != packet.trial_id,
            binding.candidate_token != packet.candidate_token,
            binding.execution_profile_id != packet.execution_profile_id,
            binding.arm_command_seq > packet.command_seq,
            packet.command is HostCommand.ARM
            and binding.arm_command_seq != packet.command_seq,
            binding.logical_batch_sequence != (
                packet.logical_batch_sequence or None
            ),
        )
    ):
        raise MailboxError("command mailbox packet differs from durable runtime identity")
    return MailboxCommand(packet=packet, binding=binding, sha256=digest)


def tp_packet_from_rtde(output: Mapping[str, Any] | None) -> TpPacket:
    if not isinstance(output, Mapping):
        raise IncompleteTpSnapshot("TP feedback is unavailable")

    def register(index: int) -> int:
        name = f"output_int_register_{index}"
        if name not in output:
            raise IncompleteTpSnapshot(f"TP feedback lacks {name}")
        return _strict_int(name, output[name])

    try:
        state = TpLoopState(register(26))
    except ValueError as exc:
        raise MailboxError("TP reported an unknown loop state") from exc
    return TpPacket(
        campaign_epoch_echo=register(24),
        trial_id_echo=register(25),
        state=state,
        candidate_token_echo=register(27),
        terminal_reason=register(28),
        execution_profile_id_echo=register(29),
        consumed_command_seq=register(30),
        logical_batch_sequence_echo=(
            register(34) if "output_int_register_34" in output else 0
        ),
    )


def _tp_identity_matches(packet: HostPacket, snapshot: TpPacket) -> bool:
    return (
        snapshot.campaign_epoch_echo == packet.campaign_epoch
        and snapshot.trial_id_echo == packet.trial_id
        and snapshot.candidate_token_echo == packet.candidate_token
        and snapshot.execution_profile_id_echo == packet.execution_profile_id
        and (
            packet.logical_batch_sequence == 0
            or snapshot.logical_batch_sequence_echo
            == packet.logical_batch_sequence
        )
    )


class BridgeMailboxRuntime:
    """Apply fresh mailbox commands to one persistent bridge process."""

    DEFAULT_COMPLETION_PROTOCOL = "legacy_ack_bundle_v1"
    IDENTITY_COMMIT_TIMEOUT_S = 0.250

    def __init__(
        self,
        path: Path,
        *,
        campaign_home_reference_path: Path | None = None,
        arming_context_provider: Callable[..., Any | None] | None = None,
        completion_protocol: str | None = None,
        launch_profile: Any | None = None,
    ) -> None:
        self.mailbox = AtomicCommandMailbox(
            path,
            network_mode=True,
            launch_profile=launch_profile,
        )
        self.campaign_home_reference_path = (
            path.parent / "campaign_home_reference.json"
            if campaign_home_reference_path is None
            else campaign_home_reference_path
        )
        if not self.campaign_home_reference_path.is_absolute():
            raise MailboxError("campaign-home reference path must be absolute")
        self.campaign_home_reference: CampaignHomeReference | None = None
        self.arming_context_provider = arming_context_provider
        self.completion_protocol = (
            self.DEFAULT_COMPLETION_PROTOCOL
            if completion_protocol is None
            else completion_protocol
        )
        if self.completion_protocol not in {
            "legacy_ack_bundle_v1",
            "v3_direct_arm_v1",
            FULL_HOME_ROLLING_PROTOCOL,
        }:
            raise MailboxError("completion protocol is unsupported")
        if (
            self.completion_protocol == FULL_HOME_ROLLING_PROTOCOL
            and self.arming_context_provider is None
        ):
            raise MailboxError(
                "full-home rolling ARM requires a bridge-local arming gate provider"
            )
        self.active: MailboxCommand | None = None
        self.last_command: MailboxCommand | None = None
        self.last_command_seq = 0
        self.connection_epoch: int | None = None
        self._pending_arm_seq: int | None = None
        self._pending_arm_previous_seq: int | None = None
        self._pending_arm_started_s: float | None = None
        self._pending_arm_connection_epoch: int | None = None

    @property
    def identity_commit_pending(self) -> bool:
        return self._pending_arm_seq is not None

    def _begin_arm_identity_commit(
        self,
        command: MailboxCommand,
        snapshot: TpPacket,
        *,
        connection_epoch: int,
    ) -> None:
        sequence = command.binding.arm_command_seq
        if snapshot.consumed_command_seq > sequence:
            raise MailboxError("TP consumed a command newer than the pending ARM")
        if snapshot.consumed_command_seq == sequence:
            if not _tp_identity_matches(command.packet, snapshot):
                raise MailboxError("TP committed ARM sequence with a different identity")
            return
        self._pending_arm_seq = sequence
        self._pending_arm_previous_seq = snapshot.consumed_command_seq
        self._pending_arm_started_s = time.monotonic()
        self._pending_arm_connection_epoch = connection_epoch

    @staticmethod
    def _arm_from_binding(command: MailboxCommand) -> MailboxCommand:
        binding = command.binding
        packet = HostPacket(
            campaign_epoch=binding.campaign_epoch,
            trial_id=binding.trial_id,
            command=HostCommand.ARM,
            candidate_token=binding.candidate_token,
            execution_profile_id=binding.execution_profile_id,
            command_seq=binding.arm_command_seq,
            logical_batch_sequence=binding.logical_batch_sequence or 0,
        )
        return MailboxCommand(packet=packet, binding=binding, sha256=command.sha256)

    def _bootstrap_active(
        self,
        command: MailboxCommand,
        snapshot: TpPacket,
        args: Any,
        arming_context: Any | None = None,
    ) -> bool:
        """Reattach a fresh bridge to the durable mailbox/TP identity.

        Returns True when the latest mailbox command has already been consumed
        and therefore must not be treated as a fresh phase transition.
        """

        if self.active is not None:
            return False
        if (
            self.completion_protocol
            in {"v3_direct_arm_v1", FULL_HOME_ROLLING_PROTOCOL}
            and command.packet.command is HostCommand.ACK_BUNDLE
        ):
            raise MailboxError(
                "fresh legacy ACK is forbidden by v3_direct_arm_v1"
            )
        if snapshot.state in {
            TpLoopState.READY_HOME,
            TpLoopState.READY_NEAR,
            TpLoopState.READY_HOME_CLOSED,
            TpLoopState.READY_HOME_NEXT,
        }:
            if (
                command.packet.command is HostCommand.ACK_BUNDLE
                and snapshot.campaign_epoch_echo == 0
                and snapshot.trial_id_echo == 0
                and snapshot.candidate_token_echo == 0
                and snapshot.execution_profile_id_echo == 0
                and snapshot.consumed_command_seq == command.packet.command_seq
            ):
                self.last_command = command
                self.last_command_seq = command.packet.command_seq
                return True
            return False
        if (
            command.packet.command is HostCommand.ARM
            and snapshot.state is TpLoopState.WAIT_INFRA_READY
            and command.packet.command_seq > snapshot.consumed_command_seq
        ):
            # The durable next-candidate ARM has not reached the old
            # WAIT_INFRA identity yet; normal ARM phase validation sends it.
            return False
        arm = self._arm_from_binding(command)
        if not _tp_identity_matches(arm.packet, snapshot):
            raise MailboxError(
                "fresh bridge mailbox binding differs from the active TP identity"
            )
        if snapshot.consumed_command_seq < arm.packet.command_seq:
            raise MailboxError("TP active identity echoes an unconsumed ARM sequence")
        if snapshot.consumed_command_seq > command.packet.command_seq:
            raise MailboxError("TP consumed a command newer than the durable mailbox")
        if snapshot.consumed_command_seq not in {
            arm.packet.command_seq,
            command.packet.command_seq,
        }:
            raise MailboxError("TP consumed sequence is absent from the durable mailbox")
        self.active = arm
        self._apply_arm_runtime(args, arm.binding, arming_context)
        args.step5d_autotune_handshake = command.handshake
        if snapshot.consumed_command_seq == command.packet.command_seq:
            self.last_command = command
            self.last_command_seq = command.packet.command_seq
            return True
        self.last_command_seq = arm.packet.command_seq
        if command.packet.command is HostCommand.ARM:
            self.last_command = command
            return True
        return False

    def _reconcile_snapshot(
        self,
        snapshot: TpPacket,
        *,
        durable_command_seq: int,
        connection_epoch: int,
    ) -> None:
        if self.active is None:
            pending_retry_arm = (
                snapshot.state is TpLoopState.WAIT_INFRA_READY
                and durable_command_seq > snapshot.consumed_command_seq
            )
            if snapshot.state not in {
                TpLoopState.READY_HOME,
                TpLoopState.READY_NEAR,
                TpLoopState.READY_HOME_CLOSED,
                TpLoopState.READY_HOME_NEXT,
            } and not pending_retry_arm:
                raise MailboxError(
                    "fresh bridge runtime requires READY_HOME; non-home TP state "
                    "must be reconciled with an existing active binding"
                )
            return
        if snapshot.consumed_command_seq > durable_command_seq:
            raise MailboxError("TP consumed a command newer than this bridge mailbox")
        identity_states = {
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
            TpLoopState.FAULT,
        }
        if self._pending_arm_seq is not None:
            pending_seq = self._pending_arm_seq
            previous_seq = self._pending_arm_previous_seq
            started_s = self._pending_arm_started_s
            if connection_epoch != self._pending_arm_connection_epoch:
                raise MailboxError("RTDE reconnected during TP ARM identity commit")
            if previous_seq is None or started_s is None:
                raise MailboxError("pending TP ARM identity commit is incomplete")
            if snapshot.consumed_command_seq < previous_seq:
                raise MailboxError("TP consumed-command sequence regressed during ARM commit")
            if snapshot.consumed_command_seq > pending_seq:
                raise MailboxError("TP consumed-command sequence overshot pending ARM")
            if snapshot.consumed_command_seq < pending_seq:
                if time.monotonic() - started_s > self.IDENTITY_COMMIT_TIMEOUT_S:
                    raise MailboxError("TP ARM identity commit timed out")
                allowed_partial_states = {
                    TpLoopState.READY_HOME,
                    TpLoopState.READY_HOME_NEXT,
                    TpLoopState.ARMED,
                }
                if self.completion_protocol == "v3_direct_arm_v1":
                    allowed_partial_states.add(TpLoopState.READY_NEAR)
                if snapshot.state not in allowed_partial_states:
                    raise MailboxError("TP entered RUN before ARM identity commit")
                return
            if snapshot.state not in identity_states or not _tp_identity_matches(
                self.active.packet, snapshot
            ):
                raise MailboxError("TP committed ARM sequence with a different identity")
            self._pending_arm_seq = None
            self._pending_arm_previous_seq = None
            self._pending_arm_started_s = None
            self._pending_arm_connection_epoch = None
        if snapshot.state in identity_states and not _tp_identity_matches(
            self.active.packet, snapshot
        ):
            raise MailboxError("TP identity changed across bridge connection/runtime")
        if snapshot.state in identity_states and (
            snapshot.consumed_command_seq < self.active.binding.arm_command_seq
        ):
            raise MailboxError("TP active identity echoes an unconsumed ARM sequence")

    @staticmethod
    def _same_runtime(left: RuntimeTrialBinding, right: RuntimeTrialBinding) -> bool:
        return left == right

    def _validate_phase(self, command: MailboxCommand, snapshot: TpPacket) -> None:
        packet = command.packet
        if self.completion_protocol == FULL_HOME_ROLLING_PROTOCOL:
            if packet.logical_batch_sequence <= 0:
                raise MailboxError(
                    "full-home rolling command requires a positive logical batch sequence"
                )
            if command.binding.logical_batch_sequence != packet.logical_batch_sequence:
                raise MailboxError(
                    "full-home rolling packet differs from durable logical batch identity"
                )
        if packet.command is HostCommand.ARM:
            allowed = (
                {TpLoopState.READY_HOME, TpLoopState.READY_NEAR}
                if self.completion_protocol == "v3_direct_arm_v1"
                else {TpLoopState.READY_HOME, TpLoopState.READY_HOME_NEXT}
                if self.completion_protocol == FULL_HOME_ROLLING_PROTOCOL
                else {
                    TpLoopState.READY_HOME,
                    TpLoopState.READY_NEAR,
                    TpLoopState.READY_HOME_CLOSED,
                    TpLoopState.WAIT_INFRA_READY,
                }
            )
            if snapshot.state not in allowed:
                raise MailboxError("fresh ARM is invalid in the current TP state")
            if packet.command_seq <= snapshot.consumed_command_seq:
                raise MailboxError("ARM command sequence is stale at the TP")
            return
        if packet.command is HostCommand.ACK_BUNDLE:
            if self.completion_protocol in {
                "v3_direct_arm_v1",
                FULL_HOME_ROLLING_PROTOCOL,
            }:
                raise MailboxError(
                    "fresh legacy ACK is forbidden by v3_direct_arm_v1"
                )
            if snapshot.state is not TpLoopState.WAIT_ACK:
                raise MailboxError("ACK_BUNDLE is valid only at WAIT_ACK")
            if self.active is None or not self._same_runtime(
                self.active.binding, command.binding
            ):
                raise MailboxError("ACK_BUNDLE attempted to alter the active runtime binding")
            if not _tp_identity_matches(packet, snapshot):
                raise MailboxError("ACK_BUNDLE identity/profile differs from current TP echoes")
            if packet.command_seq <= snapshot.consumed_command_seq:
                raise MailboxError("ACK_BUNDLE command sequence is stale")
            return
        if packet.command is HostCommand.COMPLETE_AT_HOME:
            if self.completion_protocol != FULL_HOME_ROLLING_PROTOCOL:
                raise MailboxError("COMPLETE_AT_HOME requires full-home rolling protocol")
            if snapshot.state is not TpLoopState.READY_HOME_NEXT:
                raise MailboxError("COMPLETE_AT_HOME is valid only at READY_HOME_NEXT")
            if self.active is None or not self._same_runtime(
                self.active.binding, command.binding
            ):
                raise MailboxError("COMPLETE_AT_HOME altered the active runtime binding")
            if not _tp_identity_matches(packet, snapshot):
                raise MailboxError("COMPLETE_AT_HOME identity differs from TP echoes")
            if packet.command_seq <= snapshot.consumed_command_seq:
                raise MailboxError("COMPLETE_AT_HOME command sequence is stale")
            return
        if packet.command is HostCommand.STOP:
            transport = integer_stop_transport(snapshot.state)
            if transport == "legacy_float_stop_request":
                raise LegacyFloatStopRequired(
                    "RUN stop must use the legacy float stop_request; integer STOP is outer-loop only"
                )
            if transport != "integer_stop":
                raise MailboxError("integer STOP is invalid in the current TP state")
            if snapshot.state is not TpLoopState.READY_HOME and not _tp_identity_matches(
                packet, snapshot
            ):
                raise MailboxError("integer STOP identity differs from TP echoes")
            if packet.command_seq <= snapshot.consumed_command_seq:
                raise MailboxError("integer STOP command sequence is stale")
            return
        raise MailboxError("unsupported mailbox command")

    @staticmethod
    def _apply_arm_runtime(
        args: Any,
        binding: RuntimeTrialBinding,
        arming_context: Any | None = None,
    ) -> None:
        candidate = binding.candidate
        profile = binding.profile
        args.step5d_autotune_force_p = candidate.force_p_gain
        args.step5d_autotune_force_i = candidate.force_i_gain
        args.step5d_autotune_force_damping = candidate.force_damping
        args.step5d_autotune_force_terms = {
            "P": candidate.force_p_gain,
            "I": candidate.force_i_gain,
            "damping": candidate.force_damping,
            **candidate.native_mapping,
        }
        args.step5d_autotune_normal_rate_rad_s = profile.normal_max_rate_rad_s
        args.step5d_autotune_host_slew_rad_s2 = profile.host_qdot_slew_rad_s2
        args.step5d_autotune_speedj_acceleration_rad_s2 = (
            profile.tp_speedj_accel_rad_s2
        )
        args.bridge_normal_max_rate_rad_s = profile.normal_max_rate_rad_s
        args.step4e_normal_max_rate_rad_s = profile.normal_max_rate_rad_s
        args.step5d_autotune_profile_eligibility = "live_eligible"
        args.step5d_autotune_batch_row_index = binding.batch_row_index or 0
        args.step5d_autotune_logical_batch_sequence = (
            binding.logical_batch_sequence or 0
        )
        overlay = binding.trial_overlay
        if overlay is not None:
            for field in (
                "step5d_preload_filtered_min_n",
                "step5d_preload_filtered_max_n",
                "step5d_preload_raw_min_n",
                "step5d_preload_raw_max_n",
                "step5d_preload_force_norm_max_n",
                "step5d_preload_hold_s",
                "step5d_preload_timeout_s",
            ):
                setattr(args, field, float(overlay[field]))

    def poll(
        self,
        args: Any,
        output: Mapping[str, Any] | None,
        *,
        connection_epoch: int = 0,
    ) -> bool:
        command = self.mailbox.read_latest()
        if command is None or output is None:
            return False
        snapshot = tp_packet_from_rtde(output)
        arm_boundary = command.packet.command is HostCommand.ARM and (
            self.active is None
            or self.last_command is None
            or command.sha256 != self.last_command.sha256
        )
        arming_context = None
        if self.arming_context_provider is not None:
            if command.packet.command is HostCommand.ARM:
                arming_context = self.arming_context_provider(
                    command.arm_gate_binding,
                    connection_epoch=connection_epoch,
                )
            else:
                arming_context = self.arming_context_provider()
            if arming_context is None:
                return False
        if arm_boundary:
            confirmed = self.mailbox.read_latest()
            if confirmed is None or confirmed.sha256 != command.sha256:
                raise MailboxError("ARM mailbox changed after its command-bound grant")
        if (
            snapshot.state is TpLoopState.READY_HOME
            and command.packet.command is HostCommand.ARM
            and self._pending_arm_seq is None
        ):
            self.campaign_home_reference = (
                CampaignHomeReference.capture_or_verify(
                    self.campaign_home_reference_path,
                    binding=command.binding,
                    ready_output=output,
                    connection_epoch=connection_epoch,
                )
            )
        consumed_latest = self._bootstrap_active(
            command,
            snapshot,
            args,
            arming_context,
        )
        self._reconcile_snapshot(
            snapshot,
            durable_command_seq=max(self.last_command_seq, command.packet.command_seq),
            connection_epoch=connection_epoch,
        )
        self.connection_epoch = connection_epoch
        if consumed_latest:
            return False
        if self.last_command is not None and command.sha256 == self.last_command.sha256:
            return False
        if command.packet.command_seq <= self.last_command_seq:
            raise MailboxError("mailbox command sequence did not increase monotonically")
        self._validate_phase(command, snapshot)
        if command.packet.command is HostCommand.ARM:
            self._apply_arm_runtime(args, command.binding, arming_context)
            self.active = command
            self._begin_arm_identity_commit(
                command,
                snapshot,
                connection_epoch=connection_epoch,
            )
        args.step5d_autotune_handshake = command.handshake
        self.last_command = command
        self.last_command_seq = command.packet.command_seq
        return True


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_artifact(path: Path, encoded: bytes) -> None:
    if not path.is_absolute():
        raise MailboxError("capture artifact path must be absolute")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or (path.exists() and path.is_symlink()):
        raise MailboxError("capture artifact path must not traverse a terminal symlink")
    temporary = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise MailboxError("short capture artifact write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _sha256_regular(path: Path) -> str:
    if not path.is_absolute() or path.is_symlink():
        raise MailboxError("capture artifact must be an absolute regular file")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise MailboxError("capture artifact must be a singly-linked regular file")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        closed = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    current = path.stat(follow_symlinks=False)
    identities = {
        (row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns)
        for row in (opened, closed, current)
    }
    if len(identities) != 1:
        raise MailboxError("capture artifact changed while being hashed")
    return digest.hexdigest()


def _read_regular_bytes(path: Path) -> bytes:
    if not path.is_absolute() or path.is_symlink():
        raise MailboxError("evidence artifact must be an absolute regular file")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise MailboxError("evidence artifact must be a singly-linked regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        closed = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    current = path.stat(follow_symlinks=False)
    identities = {
        (row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns)
        for row in (opened, closed, current)
    }
    if len(identities) != 1:
        raise MailboxError("evidence artifact changed while being read")
    return b"".join(chunks)


CAMPAIGN_HOME_REFERENCE_SCHEMA = "step5d.autotune.campaign-home-reference/v1"


@dataclass(frozen=True)
class CampaignHomeReference:
    """Durable bridge-captured campaign home bound to frozen runtime identity."""

    path: Path
    sha256: str
    backend_id: str
    campaign_epoch: int
    campaign_fingerprint: str
    source_fingerprint: str
    config_fingerprint: str
    home_pose: tuple[float, ...]
    home_q: tuple[float, ...]
    ready_consumed_command_seq: int
    controller_timestamp_s: float
    connection_epoch: int

    @staticmethod
    def _ready_payload(
        output: Mapping[str, Any], *, connection_epoch: int
    ) -> dict[str, Any]:
        snapshot = tp_packet_from_rtde(output)
        if (
            snapshot.state is not TpLoopState.READY_HOME
            or snapshot.campaign_epoch_echo != 0
            or snapshot.trial_id_echo != 0
            or snapshot.candidate_token_echo != 0
            or snapshot.terminal_reason != 0
            or snapshot.execution_profile_id_echo != 0
        ):
            raise MailboxError(
                "campaign home must be captured from the zero-identity READY_HOME echo"
            )
        pose = _vector("actual_TCP_pose", output.get("actual_TCP_pose"), 6)
        joints = _vector("actual_q", output.get("actual_q"), 6)
        tcp_speed = _vector("actual_TCP_speed", output.get("actual_TCP_speed"), 6)
        qd = _vector("actual_qd", output.get("actual_qd"), 6)
        safety = _safety_mode(output.get("safety_mode"))
        linear_speed = _norm(tcp_speed[:3])
        angular_speed = _norm(tcp_speed[3:6])
        qd_max = max(abs(value) for value in qd)
        if (
            safety != "NORMAL"
            or linear_speed > 0.001
            or angular_speed > 0.01
            or qd_max > 0.01
        ):
            raise MailboxError("campaign home READY_HOME sample is not stationary and safe")
        return {
            "ready_handshake": {
                "campaign_epoch_echo": 0,
                "trial_id_echo": 0,
                "state": int(TpLoopState.READY_HOME),
                "candidate_token_echo": 0,
                "terminal_reason": 0,
                "execution_profile_id_echo": 0,
                "consumed_command_seq": snapshot.consumed_command_seq,
            },
            "home": {"tcp_pose": list(pose), "joint_positions": list(joints)},
            "bridge_provenance": {
                "controller_timestamp_s": _finite(
                    "controller timestamp", output.get("timestamp")
                ),
                "connection_epoch": _strict_int(
                    "connection_epoch", connection_epoch
                ),
                "safety_mode": safety,
                "tcp_linear_speed_m_s": linear_speed,
                "tcp_angular_speed_rad_s": angular_speed,
                "qd_max_rad_s": qd_max,
            },
        }

    @classmethod
    def _from_bytes(cls, path: Path, encoded: bytes) -> "CampaignHomeReference":
        try:
            payload = json.loads(
                encoded.decode("ascii"),
                parse_constant=_reject_constant,
                object_pairs_hook=_unique_object,
            )
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise MailboxError(f"campaign-home reference is malformed: {exc}") from exc
        if not isinstance(payload, Mapping) or set(payload) != {
            "schema",
            "binding",
            "ready_handshake",
            "home",
            "bridge_provenance",
        }:
            raise MailboxError("campaign-home reference fields are incomplete")
        if payload["schema"] != CAMPAIGN_HOME_REFERENCE_SCHEMA:
            raise MailboxError("campaign-home reference schema mismatch")
        if _strict_json_bytes(payload) != encoded:
            raise MailboxError("campaign-home reference is not canonical JSON")
        binding = payload["binding"]
        ready = payload["ready_handshake"]
        home = payload["home"]
        bridge = payload["bridge_provenance"]
        if not isinstance(binding, Mapping) or set(binding) != {
            "backend_id",
            "campaign_epoch",
            "campaign_fingerprint",
            "source_fingerprint",
            "config_fingerprint",
        }:
            raise MailboxError("campaign-home binding fields are incomplete")
        if not isinstance(ready, Mapping) or set(ready) != {
            "campaign_epoch_echo",
            "trial_id_echo",
            "state",
            "candidate_token_echo",
            "terminal_reason",
            "execution_profile_id_echo",
            "consumed_command_seq",
        }:
            raise MailboxError("campaign-home READY handshake fields are incomplete")
        if not isinstance(home, Mapping) or set(home) != {
            "tcp_pose",
            "joint_positions",
        }:
            raise MailboxError("campaign-home vectors are incomplete")
        if not isinstance(bridge, Mapping) or set(bridge) != {
            "controller_timestamp_s",
            "connection_epoch",
            "safety_mode",
            "tcp_linear_speed_m_s",
            "tcp_angular_speed_rad_s",
            "qd_max_rad_s",
        }:
            raise MailboxError("campaign-home bridge provenance is incomplete")
        if any(
            _strict_int(name, ready[name]) != expected
            for name, expected in (
                ("campaign_epoch_echo", 0),
                ("trial_id_echo", 0),
                ("state", int(TpLoopState.READY_HOME)),
                ("candidate_token_echo", 0),
                ("terminal_reason", 0),
                ("execution_profile_id_echo", 0),
            )
        ):
            raise MailboxError("campaign-home reference is not a zero-identity READY_HOME")
        if bridge["safety_mode"] != "NORMAL" or any(
            _finite(name, bridge[name]) > limit
            for name, limit in (
                ("tcp_linear_speed_m_s", 0.001),
                ("tcp_angular_speed_rad_s", 0.01),
                ("qd_max_rad_s", 0.01),
            )
        ):
            raise MailboxError("campaign-home bridge provenance is not stationary and safe")
        backend_id = binding["backend_id"]
        if not isinstance(backend_id, str) or not backend_id.strip():
            raise MailboxError("campaign-home backend_id is invalid")
        for name in (
            "campaign_fingerprint",
            "source_fingerprint",
            "config_fingerprint",
        ):
            if not isinstance(binding[name], str) or not _SHA256_RE.fullmatch(
                binding[name]
            ):
                raise MailboxError(f"campaign-home {name} is invalid")
        return cls(
            path=path,
            sha256=hashlib.sha256(encoded).hexdigest(),
            backend_id=backend_id,
            campaign_epoch=_strict_int(
                "campaign_epoch", binding["campaign_epoch"], positive=True
            ),
            campaign_fingerprint=binding["campaign_fingerprint"],
            source_fingerprint=binding["source_fingerprint"],
            config_fingerprint=binding["config_fingerprint"],
            home_pose=_vector("campaign home pose", home["tcp_pose"], 6),
            home_q=_vector("campaign home q", home["joint_positions"], 6),
            ready_consumed_command_seq=_strict_int(
                "ready consumed command_seq", ready["consumed_command_seq"]
            ),
            controller_timestamp_s=_finite(
                "controller timestamp", bridge["controller_timestamp_s"]
            ),
            connection_epoch=_strict_int(
                "connection_epoch", bridge["connection_epoch"]
            ),
        )

    @classmethod
    def load(cls, path: Path) -> "CampaignHomeReference":
        if not isinstance(path, Path) or not path.is_absolute():
            raise MailboxError("campaign-home reference path must be absolute")
        return cls._from_bytes(path, _read_regular_bytes(path))

    def verify_binding(self, binding: RuntimeTrialBinding) -> None:
        if any(
            (
                self.backend_id != binding.backend_id,
                self.campaign_epoch != binding.campaign_epoch,
                self.campaign_fingerprint != binding.campaign_fingerprint,
                self.source_fingerprint != binding.source_fingerprint,
                self.config_fingerprint != binding.config_fingerprint,
                _sha256_regular(self.path) != self.sha256,
            )
        ):
            raise MailboxError("campaign-home reference changed or binding differs")

    def verify_trial(self, trial: TrialSpec) -> None:
        if any(
            (
                self.backend_id != trial.backend_id,
                self.campaign_epoch != trial.campaign.campaign_epoch,
                self.campaign_fingerprint != trial.campaign.campaign_fingerprint,
                self.source_fingerprint != trial.source_fingerprint,
                self.config_fingerprint != trial.config_fingerprint,
                _sha256_regular(self.path) != self.sha256,
            )
        ):
            raise MailboxError("campaign-home reference does not bind this TrialSpec")

    def verify_ready_home_equivalent(
        self,
        *,
        pose: Sequence[float],
        joints: Sequence[float],
    ) -> None:
        """Accept normal RTDE sample jitter, while rejecting a changed home."""

        observed_pose = _vector("current READY_HOME pose", pose, 6)
        observed_joints = _vector("current READY_HOME q", joints, 6)
        if (
            _norm(
                tuple(
                    observed_pose[index] - self.home_pose[index]
                    for index in range(3)
                )
            )
            > 0.003
            or _orientation_error(self.home_pose[3:6], observed_pose[3:6]) > 0.05
            or max(
                abs(observed_joints[index] - self.home_q[index])
                for index in range(6)
            )
            > 0.01
        ):
            raise MailboxError("READY_HOME no longer matches durable campaign home")

    @classmethod
    def capture_or_verify(
        cls,
        path: Path,
        *,
        binding: RuntimeTrialBinding,
        ready_output: Mapping[str, Any],
        connection_epoch: int,
    ) -> "CampaignHomeReference":
        if not isinstance(path, Path) or not path.is_absolute():
            raise MailboxError("campaign-home reference path must be absolute")
        ready_payload = cls._ready_payload(
            ready_output, connection_epoch=connection_epoch
        )
        if path.exists():
            reference = cls.load(path)
            reference.verify_binding(binding)
            reference.verify_ready_home_equivalent(
                pose=ready_payload["home"]["tcp_pose"],
                joints=ready_payload["home"]["joint_positions"],
            )
            return reference
        payload = {
            "schema": CAMPAIGN_HOME_REFERENCE_SCHEMA,
            "binding": {
                "backend_id": binding.backend_id,
                "campaign_epoch": binding.campaign_epoch,
                "campaign_fingerprint": binding.campaign_fingerprint,
                "source_fingerprint": binding.source_fingerprint,
                "config_fingerprint": binding.config_fingerprint,
            },
            **ready_payload,
        }
        _atomic_artifact(path, _strict_json_bytes(payload))
        reference = cls.load(path)
        reference.verify_binding(binding)
        return reference


class BridgeTrialCsvRotator:
    """Write one exact TP identity and seal it at a terminal protocol state."""

    IDENTITY_COLUMNS = ("autotune_trial_uid", "autotune_backend_id")

    def __init__(self, root: Path, fieldnames: Sequence[str]) -> None:
        if not isinstance(root, Path) or not root.is_absolute():
            raise MailboxError("per-trial capture root must be absolute")
        fields = tuple(str(name) for name in fieldnames)
        if not fields or len(fields) != len(set(fields)):
            raise MailboxError("per-trial capture fieldnames must be non-empty and unique")
        self.root = root
        self.fieldnames = (*fields, *self.IDENTITY_COLUMNS)
        self._trial_uid: str | None = None
        self._handle: Any | None = None
        self._writer: csv.DictWriter | None = None
        self._partial_path: Path | None = None
        self._final_path: Path | None = None
        self._sealed = False
        self._rows_since_flush = 0

    @staticmethod
    def _matches(binding: RuntimeTrialBinding, snapshot: TpPacket) -> bool:
        return (
            snapshot.campaign_epoch_echo == binding.campaign_epoch
            and snapshot.trial_id_echo == binding.trial_id
            and snapshot.candidate_token_echo == binding.candidate_token
            and snapshot.execution_profile_id_echo == binding.execution_profile_id
            and snapshot.consumed_command_seq >= binding.arm_command_seq
            and (
                binding.logical_batch_sequence is None
                or snapshot.logical_batch_sequence_echo
                == binding.logical_batch_sequence
            )
        )

    def _close_partial(self, *, sync_bytes: bool) -> None:
        if self._handle is not None:
            self._handle.flush()
            if sync_bytes:
                os.fsync(self._handle.fileno())
            self._handle.close()
        self._handle = None
        self._writer = None
        self._rows_since_flush = 0

    def _open(self, binding: RuntimeTrialBinding) -> None:
        if self._trial_uid == binding.trial_uid:
            return
        self._close_partial(sync_bytes=True)
        trial_dir = self.root / binding.trial_uid
        trial_dir.mkdir(parents=True, exist_ok=True)
        if trial_dir.is_symlink():
            raise MailboxError("per-trial capture directory must not be a symlink")
        partial = trial_dir / "capture.csv.part"
        final = trial_dir / "capture.csv"
        self._trial_uid = binding.trial_uid
        self._partial_path = partial
        self._final_path = final
        self._sealed = final.exists()
        if self._sealed:
            if final.is_symlink():
                raise MailboxError("published per-trial capture must not be a symlink")
            return
        if partial.exists():
            if partial.is_symlink():
                raise MailboxError("partial per-trial capture must not be a symlink")
            with partial.open("r", newline="", encoding="utf-8") as existing:
                header = next(csv.reader(existing), None)
            if header != list(self.fieldnames):
                raise MailboxError("partial per-trial capture header changed across restart")
            handle = partial.open("a", newline="", encoding="utf-8")
        else:
            handle = partial.open("x", newline="", encoding="utf-8")
        self._handle = handle
        self._writer = csv.DictWriter(handle, fieldnames=self.fieldnames)
        if partial.stat().st_size == 0:
            self._writer.writeheader()
            handle.flush()
            os.fsync(handle.fileno())

    def observe(
        self,
        row: Mapping[str, Any],
        *,
        active: MailboxCommand | None,
        rtde_output: Mapping[str, Any] | None,
    ) -> bool:
        if active is None or rtde_output is None:
            return False
        snapshot = tp_packet_from_rtde(rtde_output)
        if snapshot.state is TpLoopState.READY_HOME or not self._matches(
            active.binding, snapshot
        ):
            return False
        self._open(active.binding)
        if self._sealed:
            return False
        if self._writer is None or self._handle is None:
            raise MailboxError("per-trial capture writer is not open")
        payload = {name: row.get(name, "") for name in self.fieldnames}
        payload["autotune_trial_uid"] = active.binding.trial_uid
        payload["autotune_backend_id"] = active.binding.backend_id
        self._writer.writerow(payload)
        self._rows_since_flush += 1
        if self._rows_since_flush >= 50 or snapshot.state.value >= TpLoopState.TERMINAL.value:
            self._handle.flush()
            self._rows_since_flush = 0
        if snapshot.state in {
            TpLoopState.WAIT_ACK,
            TpLoopState.READY_NEAR,
            TpLoopState.READY_HOME_CLOSED,
            TpLoopState.READY_HOME_NEXT,
            TpLoopState.WAIT_INFRA_READY,
            TpLoopState.FAULT,
        }:
            self._close_partial(sync_bytes=True)
            assert self._partial_path is not None and self._final_path is not None
            if self._final_path.exists():
                raise MailboxError("per-trial capture was already published")
            os.replace(self._partial_path, self._final_path)
            _fsync_directory(self._final_path.parent)
            self._sealed = True
        return True

    def close(self) -> None:
        self._close_partial(sync_bytes=True)


@dataclass(frozen=True)
class TrialCaptureAssessment:
    terminal_reason: int
    host_cause: str | None
    completion_marker: bool
    cadence_ok: bool
    feedback_fresh: bool
    rnn_oracle_aligned: bool
    stage25_complete_s: float
    evidence: Mapping[str, Any]

    def __post_init__(self) -> None:
        _strict_int("terminal_reason", self.terminal_reason, positive=True)
        if self.host_cause is not None and not isinstance(self.host_cause, str):
            raise MailboxError("host_cause must be a string or None")
        if self.terminal_reason == 4:
            if self.host_cause not in {
                None,
                "parameter_event",
                "operator_stop",
                "infra_stop",
            }:
                raise MailboxError("reason 4 host_cause is invalid")
        elif self.host_cause is not None:
            raise MailboxError("host_cause is valid only for terminal reason 4")
        for name in (
            "completion_marker",
            "cadence_ok",
            "feedback_fresh",
            "rnn_oracle_aligned",
        ):
            if type(getattr(self, name)) is not bool:
                raise MailboxError(f"{name} must be a boolean")
        if _finite("stage25_complete_s", self.stage25_complete_s) < 0.0:
            raise MailboxError("stage25_complete_s must be non-negative")
        if not isinstance(self.evidence, Mapping):
            raise MailboxError("capture assessment evidence must be a mapping")
        _strict_json_bytes(dict(self.evidence))


def classify_reason4_host_cause(bridge_cause: str) -> str:
    """Map explicit bridge provenance to the only accepted reason-4 causes."""

    if not isinstance(bridge_cause, str) or not bridge_cause.strip():
        raise MailboxError("reason 4 requires non-empty bridge cause provenance")
    value = bridge_cause.strip().lower()
    if value == "parameter_event" or value.startswith("parameter_event:"):
        return "parameter_event"
    if value.startswith(("signal_", "operator_stop", "keyboard_interrupt")):
        return "operator_stop"
    if value.startswith(
        (
            "rtde_feedback_stale_structural_stop",
            "rtde_transport_",
            "scheduler_unavailable_",
            "infrastructure_",
        )
    ):
        return "infra_stop"
    raise MailboxError("bridge cause cannot be typed as reason-4 parameter/operator/infra")


class TrialArtifactProducer:
    """Derive and publish evidence for one already-rotated exact trial CSV."""

    HANDSHAKE_COLUMNS = tuple(
        f"ur_output_int_register_{index}" for index in range(24, 31)
    )
    ROLLING_HANDSHAKE_COLUMNS = tuple(
        f"ur_output_int_register_{index}" for index in range(31, 35)
    )

    def __init__(self, root: Path, trial: TrialSpec) -> None:
        if not isinstance(root, Path) or not root.is_absolute():
            raise MailboxError("per-trial capture root must be absolute")
        if not isinstance(trial, TrialSpec):
            raise TypeError("trial must be TrialSpec")
        self.root = root
        self.trial = trial
        trial_dir = root / trial.trial_uid
        self.paths = CaptureArtifactPaths(
            csv_path=trial_dir / "capture.csv",
            metadata_path=trial_dir / "metadata.json",
            terminal_manifest_path=trial_dir / "terminal_manifest.json",
        )

    @staticmethod
    def _csv_finite(row: Mapping[str, Any], key: str) -> float | None:
        raw = row.get(key)
        if raw in (None, ""):
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    @classmethod
    def _csv_int(cls, row: Mapping[str, Any], key: str) -> int:
        value = cls._csv_finite(row, key)
        if value is None or not value.is_integer():
            raise MailboxError(f"per-trial capture {key} is not an integer")
        return _strict_int(key, int(value))

    def _capture_rows(
        self,
    ) -> tuple[list[dict[str, str]], tuple[str, ...], str]:
        path = self.paths.csv_path
        if not path.is_file() or path.is_symlink():
            raise MailboxError("exact per-trial capture.csv is not durably published")
        encoded = _read_regular_bytes(path)
        try:
            text = encoded.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MailboxError("per-trial capture.csv is not UTF-8") from exc
        try:
            reader = csv.DictReader(io.StringIO(text, newline=""))
            fields = tuple(reader.fieldnames or ())
            if not fields or len(fields) != len(set(fields)):
                raise MailboxError("per-trial capture header is missing or duplicated")
            required = {
                *BridgeTrialCsvRotator.IDENTITY_COLUMNS,
                *self.HANDSHAKE_COLUMNS,
            }
            if not required.issubset(fields):
                raise MailboxError("per-trial capture lacks exact handshake identity columns")
            rows = list(reader)
        except csv.Error as exc:
            raise MailboxError(f"per-trial capture.csv is malformed: {exc}") from exc
        if not rows:
            raise MailboxError("per-trial capture contains no exact trial rows")
        for row in rows:
            if None in row:
                raise MailboxError("per-trial capture row has extra unbound columns")
            if (
                row.get("autotune_trial_uid") != self.trial.trial_uid
                or row.get("autotune_backend_id") != self.trial.backend_id
            ):
                raise MailboxError("per-trial capture contains another trial identity")
        digest = hashlib.sha256(encoded).hexdigest()
        if _sha256_regular(path) != digest:
            raise MailboxError("per-trial capture changed after deterministic read")
        return rows, fields, digest

    def _fixture_row_count(self) -> int:
        encoded = _read_regular_bytes(self.paths.csv_path)
        reader = csv.DictReader(io.StringIO(encoded.decode("utf-8"), newline=""))
        fields = tuple(reader.fieldnames or ())
        if not set(BridgeTrialCsvRotator.IDENTITY_COLUMNS).issubset(fields):
            raise MailboxError("fixture capture lacks durable identity columns")
        rows = list(reader)
        if not rows or any(
            row.get("autotune_trial_uid") != self.trial.trial_uid
            or row.get("autotune_backend_id") != self.trial.backend_id
            for row in rows
        ):
            raise MailboxError("fixture capture identity is invalid")
        return len(rows)

    def derive_assessment(
        self,
        expected_arm: HostPacket,
        *,
        expected_terminal_reason: int,
    ) -> TrialCaptureAssessment:
        """Derive eligibility only from the fsync-sealed exact capture bytes."""

        if expected_arm.command is not HostCommand.ARM or any(
            (
                expected_arm.campaign_epoch != self.trial.campaign.campaign_epoch,
                expected_arm.trial_id != self.trial.trial_id,
                expected_arm.candidate_token != self.trial.candidate_token,
                expected_arm.command_seq != self.trial.command_seq,
            )
        ):
            raise MailboxError("assessment ARM identity differs from TrialSpec")
        validate_execution_profile_binding(
            self.trial.execution_profile,
            expected_arm.execution_profile_id,
            network_mode=True,
        )
        rows, fields, capture_sha = self._capture_rows()
        if expected_arm.logical_batch_sequence > 0 and not set(
            self.ROLLING_HANDSHAKE_COLUMNS
        ).issubset(fields):
            raise MailboxError(
                "rolling per-trial capture lacks batch/return identity columns"
            )
        packets: list[TpPacket] = []
        for row in rows:
            try:
                state = TpLoopState(self._csv_int(row, "ur_output_int_register_26"))
            except ValueError as exc:
                raise MailboxError("capture contains unknown TP state") from exc
            packet = TpPacket(
                campaign_epoch_echo=self._csv_int(
                    row, "ur_output_int_register_24"
                ),
                trial_id_echo=self._csv_int(row, "ur_output_int_register_25"),
                state=state,
                candidate_token_echo=self._csv_int(
                    row, "ur_output_int_register_27"
                ),
                terminal_reason=self._csv_int(
                    row, "ur_output_int_register_28"
                ),
                execution_profile_id_echo=self._csv_int(
                    row, "ur_output_int_register_29"
                ),
                consumed_command_seq=self._csv_int(
                    row, "ur_output_int_register_30"
                ),
                logical_batch_sequence_echo=(
                    self._csv_int(row, "ur_output_int_register_34")
                    if expected_arm.logical_batch_sequence > 0
                    else 0
                ),
            )
            if not _tp_identity_matches(expected_arm, packet) or (
                packet.consumed_command_seq != expected_arm.command_seq
            ):
                raise MailboxError("capture TP handshake differs from exact ARM identity")
            packets.append(packet)
        final = packets[-1]
        if final.state not in {
            TpLoopState.WAIT_ACK,
            TpLoopState.READY_NEAR,
            TpLoopState.READY_HOME_CLOSED,
            TpLoopState.READY_HOME_NEXT,
        } or final.terminal_reason <= 0:
            raise MailboxError("capture is not sealed by an exact terminal row")
        if final.terminal_reason != expected_terminal_reason:
            raise MailboxError("capture terminal reason differs from closure proof")
        terminal_reasons = {packet.terminal_reason for packet in packets if packet.terminal_reason}
        if terminal_reasons != {final.terminal_reason}:
            raise MailboxError("capture terminal reason changed before seal")

        # Reuse the established Step5d analyzer/replay thresholds rather than
        # accepting any caller-authored booleans.
        from analyze_step5d_bridge_run import (  # noqa: PLC0415
            STAGE25_JOINT_LAYOUT_TAG,
            STAGE25_MAX_ROW_GAP_S,
            STAGE25_SUCCESS_MIN_CONSUMPTION_RATIO,
            stage25_control_attribution,
        )
        from step5d_autotune_replay import (  # noqa: PLC0415
            CadenceEvidence,
            cadence_eligible,
        )
        from step5d_runtime_interface import (  # noqa: PLC0415
            STEP5D_AUTOTUNE_STAGE_ID,
            stage25_success_target_s,
        )

        stage_rows = [
            row
            for row in rows
            if (
                (stage := self._csv_finite(row, "ur_output_double_register_35"))
                is not None
                and abs(stage - 25.0) <= 0.005
            )
        ]
        stage_times = [self._csv_finite(row, "t_monotonic_s") for row in stage_rows]
        complete_stage_times = all(value is not None for value in stage_times)
        stage_duration_s = 0.0
        row_gap_over_20ms_count = 0
        if complete_stage_times and stage_times:
            finite_times = [float(value) for value in stage_times if value is not None]
            if any(later <= earlier for earlier, later in zip(finite_times, finite_times[1:])):
                complete_stage_times = False
            else:
                stage_duration_s = max(0.0, finite_times[-1] - finite_times[0])
                row_gap_over_20ms_count = sum(
                    1
                    for earlier, later in zip(finite_times, finite_times[1:])
                    if later - earlier > STAGE25_MAX_ROW_GAP_S + 1e-12
                )
        consumed_column_present = "_step5d_stage25_echo_consumed" in fields
        consumed_packets = (
            sum(
                1
                for row in stage_rows
                if (self._csv_finite(row, "_step5d_stage25_echo_consumed") or 0.0)
                >= 0.5
            )
            if consumed_column_present
            else None
        )
        feedback_ages = [
            self._csv_finite(row, "rtde_feedback_age_s") for row in stage_rows
        ]
        fresh_feedback_packets = sum(
            1 for value in feedback_ages if value is not None and value <= 0.010
        )
        cadence_ok, cadence_failures = cadence_eligible(
            CadenceEvidence(
                profile_id=self.trial.execution_profile.profile_id,
                sent_packets=len(stage_rows),
                consumed_packets=consumed_packets,
                fresh_feedback_packets=fresh_feedback_packets,
                row_gap_over_20ms_count=(
                    row_gap_over_20ms_count if complete_stage_times else 1
                ),
            )
        )
        attribution = stage25_control_attribution(stage_rows, {}) if stage_rows else {}
        feedback_p99 = attribution.get("feedback_age_p99_s")
        heartbeat_gap_max = attribution.get("sent_echo_heartbeat_gap_max")
        connected_values = [
            self._csv_finite(row, "rtde_connected") for row in stage_rows
        ]
        feedback_fresh = bool(
            stage_rows
            and len([value for value in feedback_ages if value is not None])
            == len(stage_rows)
            and isinstance(feedback_p99, (int, float))
            and math.isfinite(float(feedback_p99))
            and float(feedback_p99) <= 0.010
            and isinstance(heartbeat_gap_max, (int, float))
            and math.isfinite(float(heartbeat_gap_max))
            and float(heartbeat_gap_max) <= 5.0
            and all(value is not None and value >= 0.5 for value in connected_values)
        )
        accepted_rows = int(attribution.get("rnn_accepted_rows") or 0)
        oracle_rows = int(attribution.get("rnn_oracle_qdot_delta_rows") or 0)
        raw_rows = int(attribution.get("raw_rnn_residual_rows") or 0)
        post_rows = int(attribution.get("post_slew_command_residual_rows") or 0)
        oracle_max = attribution.get("rnn_oracle_qdot_delta_max")
        rnn_oracle_aligned = bool(
            stage_rows
            and accepted_rows / len(stage_rows)
            >= STAGE25_SUCCESS_MIN_CONSUMPTION_RATIO
            and oracle_rows == accepted_rows
            and raw_rows == accepted_rows
            and post_rows == accepted_rows
            and int(attribution.get("legacy_raw_residual_mismatch_rows") or 0) == 0
            and isinstance(oracle_max, (int, float))
            and math.isfinite(float(oracle_max))
            and float(oracle_max) <= 1e-6
            and int(
                (attribution.get("command_layout_tag_counts") or {}).get(
                    str(STAGE25_JOINT_LAYOUT_TAG), 0
                )
            )
            >= accepted_rows
        )
        float_terminal_crosscheck = terminal_float_reason_crosscheck(
            (
                (packet, self._csv_finite(row, "ur_output_double_register_30"))
                for row, packet in zip(rows, packets)
            ),
            final_reason=final.terminal_reason,
        )
        target_s = stage25_success_target_s(STEP5D_AUTOTUNE_STAGE_ID)
        if target_s is None:
            raise MailboxError("autotune Stage25 target is unavailable")
        completion_marker = bool(
            final.terminal_reason == 1
            and float_terminal_crosscheck
            and complete_stage_times
            and stage_duration_s >= target_s
        )
        bridge_causes = sorted(
            {
                str(row.get("guard_reason") or "").strip()
                for row in rows
                if str(row.get("guard_reason") or "").strip()
                and (self._csv_finite(row, "stop_request") or 0.0) > 0.5
            }
        )
        host_cause: str | None = None
        reason4_cause_status = "not_applicable"
        if final.terminal_reason == 4:
            if len(bridge_causes) == 1:
                try:
                    host_cause = classify_reason4_host_cause(bridge_causes[0])
                except MailboxError:
                    reason4_cause_status = "untyped_fail_closed"
                else:
                    reason4_cause_status = "typed_from_bridge_capture"
            else:
                reason4_cause_status = "missing_or_ambiguous_fail_closed"
        evidence = {
            "assessment_derivation": "exact_capture_wait_ack_bridge_v1",
            "capture_sha256": capture_sha,
            "capture_rows": len(rows),
            "wait_ack_handshake": {
                "campaign_epoch": final.campaign_epoch_echo,
                "trial_id": final.trial_id_echo,
                "candidate_token": final.candidate_token_echo,
                "execution_profile_id": final.execution_profile_id_echo,
                "consumed_command_seq": final.consumed_command_seq,
                "terminal_reason": final.terminal_reason,
            },
            "float_terminal_crosscheck": float_terminal_crosscheck,
            "stage25_rows": len(stage_rows),
            "stage25_target_s": target_s,
            "stage25_times_complete": complete_stage_times,
            "stage25_row_gap_over_20ms_count": row_gap_over_20ms_count,
            "cadence_failures": list(cadence_failures),
            "feedback_age_p99_s": feedback_p99,
            "heartbeat_gap_max": heartbeat_gap_max,
            "rnn_accepted_rows": accepted_rows,
            "rnn_oracle_qdot_delta_rows": oracle_rows,
            "rnn_oracle_qdot_delta_max": oracle_max,
            "raw_rnn_residual_rows": raw_rows,
            "post_slew_command_residual_rows": post_rows,
            "bridge_cause_rows": bridge_causes,
            "reason4_cause_status": reason4_cause_status,
        }
        return TrialCaptureAssessment(
            terminal_reason=final.terminal_reason,
            host_cause=host_cause,
            completion_marker=completion_marker,
            cadence_ok=cadence_ok,
            feedback_fresh=feedback_fresh,
            rnn_oracle_aligned=rnn_oracle_aligned,
            stage25_complete_s=stage_duration_s,
            evidence=evidence,
        )

    def _build_manifest(
        self,
        closure: ClosureEvidence,
        assessment: TrialCaptureAssessment,
        *,
        campaign_home_reference_sha256: str | None,
        production_derived: bool,
        source_fingerprint_post: str | None = None,
        config_fingerprint_post: str | None = None,
    ) -> CaptureManifest:
        if not isinstance(closure, (SafeClosureEvidence, TypedSafeClosureEvidence)):
            raise TypeError("closure has an unsupported evidence schema")
        row_count = (
            int(assessment.evidence.get("capture_rows", 0))
            if production_derived
            else self._fixture_row_count()
        )
        if row_count <= 0:
            raise MailboxError("capture row count is missing")
        source_post = source_fingerprint_post or self.trial.source_fingerprint
        config_post = config_fingerprint_post or self.trial.config_fingerprint
        metadata = {
            "schema_version": "step5d.autotune.capture-metadata/v1",
            "trial_uid": self.trial.trial_uid,
            "backend_id": self.trial.backend_id,
            "campaign_epoch": self.trial.campaign.campaign_epoch,
            "trial_id": self.trial.trial_id,
            "candidate_token": self.trial.candidate_token,
            "arm_command_seq": self.trial.command_seq,
            "candidate": self.trial.candidate.payload(),
            "execution_profile": self.trial.execution_profile.payload(),
            "source_fingerprint_pre": self.trial.source_fingerprint,
            "source_fingerprint_post": source_post,
            "config_fingerprint_pre": self.trial.config_fingerprint,
            "config_fingerprint_post": config_post,
            "capture_scope": "single_exact_trial",
            "capture_rows": row_count,
            "assessment_derivation": assessment.evidence.get(
                "assessment_derivation", "explicit_test_fixture"
            ),
            "campaign_home_reference_sha256": campaign_home_reference_sha256,
        }
        terminal = {
            "schema_version": "step5d.autotune.terminal-manifest/v1",
            "trial_uid": self.trial.trial_uid,
            "backend_id": self.trial.backend_id,
            "candidate_token": self.trial.candidate_token,
            "terminal_reason": assessment.terminal_reason,
            "host_cause": assessment.host_cause,
            "safe_closure_evidence": closure.payload(),
            "assessment": {
                "completion_marker": assessment.completion_marker,
                "cadence_ok": assessment.cadence_ok,
                "feedback_fresh": assessment.feedback_fresh,
                "rnn_oracle_aligned": assessment.rnn_oracle_aligned,
                "stage25_complete_s": assessment.stage25_complete_s,
                "evidence": dict(assessment.evidence),
            },
        }
        _atomic_artifact(self.paths.metadata_path, _strict_json_bytes(metadata))
        _atomic_artifact(
            self.paths.terminal_manifest_path,
            _strict_json_bytes(terminal),
        )
        csv_sha = _sha256_regular(self.paths.csv_path)
        if production_derived and assessment.evidence.get("capture_sha256") != csv_sha:
            raise MailboxError("capture.csv changed after deterministic assessment")
        metadata_sha = _sha256_regular(self.paths.metadata_path)
        terminal_sha = _sha256_regular(self.paths.terminal_manifest_path)
        if not closure.capture_hashes_complete or not closure.terminal_manifest_complete:
            raise MailboxError("closure cannot claim incomplete capture artifacts")
        return CaptureManifest(
            trial_uid=self.trial.trial_uid,
            backend_id=self.trial.backend_id,
            source_fingerprint_pre=self.trial.source_fingerprint,
            source_fingerprint_post=source_post,
            config_fingerprint_pre=self.trial.config_fingerprint,
            config_fingerprint_post=config_post,
            candidate_token=self.trial.candidate_token,
            terminal_reason=assessment.terminal_reason,
            host_cause=assessment.host_cause,
            csv_sha256=csv_sha,
            metadata_sha256=metadata_sha,
            terminal_manifest_sha256=terminal_sha,
            completion_marker=assessment.completion_marker,
            cadence_ok=assessment.cadence_ok,
            feedback_fresh=assessment.feedback_fresh,
            rnn_oracle_aligned=assessment.rnn_oracle_aligned,
            safety_normal=closure.host_safety_mode == "NORMAL",
            returned_safe=closure.returned_safe,
            immutable_bundle_written=True,
            stage25_complete_s=assessment.stage25_complete_s,
            safe_closure_evidence=closure,
            evidence={
                **dict(assessment.evidence),
                "capture_scope": "single_exact_trial",
                "capture_rows": row_count,
                "campaign_home_reference_sha256": campaign_home_reference_sha256,
                "immutable_bundle_status": "intent_pending_store_receipt",
            },
        )

    def build_manifest(
        self,
        closure: ClosureEvidence,
        *,
        expected_arm: HostPacket,
        expected_terminal_reason: int,
        home_reference: CampaignHomeReference,
        source_fingerprint_post: str | None = None,
        config_fingerprint_post: str | None = None,
    ) -> CaptureManifest:
        """Production API: assessment is derived internally, never accepted."""

        home_reference.verify_trial(self.trial)
        assessment = self.derive_assessment(
            expected_arm,
            expected_terminal_reason=expected_terminal_reason,
        )
        return self._build_manifest(
            closure,
            assessment,
            campaign_home_reference_sha256=home_reference.sha256,
            production_derived=True,
            source_fingerprint_post=source_fingerprint_post,
            config_fingerprint_post=config_fingerprint_post,
        )

    def build_manifest_for_test_fixture(
        self,
        closure: ClosureEvidence,
        assessment: TrialCaptureAssessment,
        *,
        source_fingerprint_post: str | None = None,
        config_fingerprint_post: str | None = None,
    ) -> CaptureManifest:
        """Explicit unit-test seam; production finalization never calls it."""

        return self._build_manifest(
            closure,
            assessment,
            campaign_home_reference_sha256=None,
            production_derived=False,
            source_fingerprint_post=source_fingerprint_post,
            config_fingerprint_post=config_fingerprint_post,
        )


def _vector(name: str, value: Any, length: int) -> tuple[float, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise MailboxError(f"{name} must be a numeric sequence")
    if len(value) < length:
        raise MailboxError(f"{name} must contain at least {length} values")
    return tuple(_finite(f"{name}[{index}]", value[index]) for index in range(length))


def _norm(values: Sequence[float]) -> float:
    return math.sqrt(sum(value * value for value in values))


def _rotvec_matrix(vector: Sequence[float]) -> tuple[tuple[float, float, float], ...]:
    angle = _norm(vector)
    if angle <= 1e-15:
        return ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    x, y, z = (value / angle for value in vector)
    c = math.cos(angle)
    s = math.sin(angle)
    one_minus_c = 1.0 - c
    return (
        (
            c + x * x * one_minus_c,
            x * y * one_minus_c - z * s,
            x * z * one_minus_c + y * s,
        ),
        (
            y * x * one_minus_c + z * s,
            c + y * y * one_minus_c,
            y * z * one_minus_c - x * s,
        ),
        (
            z * x * one_minus_c - y * s,
            z * y * one_minus_c + x * s,
            c + z * z * one_minus_c,
        ),
    )


def _orientation_error(home_rotvec: Sequence[float], actual_rotvec: Sequence[float]) -> float:
    home = _rotvec_matrix(home_rotvec)
    actual = _rotvec_matrix(actual_rotvec)
    # trace(R_home^T R_actual) is the full Frobenius inner product.
    trace_relative = sum(
        home[row][column] * actual[row][column]
        for row in range(3)
        for column in range(3)
    )
    cosine = max(-1.0, min(1.0, (trace_relative - 1.0) / 2.0))
    return math.acos(cosine)


def _safety_mode(value: Any) -> str:
    if isinstance(value, str):
        return value.strip().upper()
    if isinstance(value, bool):
        return "UNKNOWN"
    if isinstance(value, (int, float)) and float(value).is_integer():
        # UR RTDE safety_mode enumeration: 1 is NORMAL.
        return "NORMAL" if int(value) == 1 else f"RTDE_{int(value)}"
    return "UNKNOWN"


@dataclass(frozen=True)
class _ClosureMetrics:
    tp_position_error_m: float
    tp_orientation_error_rad: float
    tp_joint_error_max_rad: float
    host_position_error_m: float
    host_orientation_error_rad: float
    host_joint_error_max_rad: float
    host_tcp_linear_speed_m_s: float
    host_tcp_angular_speed_rad_s: float
    host_qd_max_rad_s: float


class HostClosureCollector:
    """Collect a continuous exact-identity 0.5 s host+TP safe-home proof."""

    def __init__(
        self,
        *,
        expected_arm: HostPacket,
        home_reference: CampaignHomeReference,
        required_dwell_s: float = 0.5,
        max_sample_gap_s: float = 0.05,
    ) -> None:
        if not isinstance(home_reference, CampaignHomeReference):
            raise TypeError("production collector requires CampaignHomeReference")
        self._initialize(
            expected_arm=expected_arm,
            campaign_home_pose=home_reference.home_pose,
            campaign_home_q=home_reference.home_q,
            home_reference=home_reference,
            required_dwell_s=required_dwell_s,
            max_sample_gap_s=max_sample_gap_s,
        )

    @classmethod
    def for_test_fixture(
        cls,
        *,
        expected_arm: HostPacket,
        campaign_home_pose: Sequence[float],
        campaign_home_q: Sequence[float],
        required_dwell_s: float = 0.5,
        max_sample_gap_s: float = 0.05,
    ) -> "HostClosureCollector":
        """Explicit non-production seam for fake-RTDE unit tests only."""

        instance = object.__new__(cls)
        instance._initialize(
            expected_arm=expected_arm,
            campaign_home_pose=campaign_home_pose,
            campaign_home_q=campaign_home_q,
            home_reference=None,
            required_dwell_s=required_dwell_s,
            max_sample_gap_s=max_sample_gap_s,
        )
        return instance

    def _initialize(
        self,
        *,
        expected_arm: HostPacket,
        campaign_home_pose: Sequence[float],
        campaign_home_q: Sequence[float],
        home_reference: CampaignHomeReference | None,
        required_dwell_s: float,
        max_sample_gap_s: float,
    ) -> None:
        if expected_arm.command is not HostCommand.ARM:
            raise ValueError("expected_arm must be the consumed ARM HostPacket")
        self.expected_arm = expected_arm
        self.home_reference = home_reference
        self.home_pose = _vector("campaign_home_pose", campaign_home_pose, 6)
        self.home_q = _vector("campaign_home_q", campaign_home_q, 6)
        self.required_dwell_s = _finite("required_dwell_s", required_dwell_s)
        self.max_sample_gap_s = _finite("max_sample_gap_s", max_sample_gap_s)
        if self.required_dwell_s < 0.5:
            raise ValueError("safe closure dwell cannot be shorter than 0.5 s")
        if self.max_sample_gap_s <= 0.0:
            raise ValueError("max_sample_gap_s must be positive")
        self.reset()

    def reset(self) -> None:
        self._start_s: float | None = None
        self._last_s: float | None = None
        self._identity: tuple[int, ...] | None = None
        self._metrics: _ClosureMetrics | None = None

    @property
    def dwell_s(self) -> float:
        if self._start_s is None or self._last_s is None:
            return 0.0
        return max(0.0, self._last_s - self._start_s)

    @property
    def ready(self) -> bool:
        return self._metrics is not None and self.dwell_s >= self.required_dwell_s

    @property
    def terminal_reason(self) -> int | None:
        return None if self._identity is None else self._identity[-1]

    def require_production_home(self, trial: TrialSpec) -> CampaignHomeReference:
        if self.home_reference is None:
            raise ClosureNotReady(
                "test-fixture campaign home is forbidden in production finalization"
            )
        self.home_reference.verify_trial(trial)
        return self.home_reference

    def _sample_metrics(self, sample: Mapping[str, Any]) -> tuple[TpPacket, _ClosureMetrics, str]:
        tp = tp_packet_from_rtde(sample)
        pose = _vector("actual_TCP_pose", sample.get("actual_TCP_pose"), 6)
        joints = _vector("actual_q", sample.get("actual_q"), 6)
        tcp_speed = _vector("actual_TCP_speed", sample.get("actual_TCP_speed"), 6)
        qd = _vector("actual_qd", sample.get("actual_qd"), 6)
        safety = _safety_mode(sample.get("safety_mode"))
        metrics = _ClosureMetrics(
            tp_position_error_m=_finite(
                "output_double_register_36", sample.get("output_double_register_36")
            ),
            tp_orientation_error_rad=_finite(
                "output_double_register_37", sample.get("output_double_register_37")
            ),
            tp_joint_error_max_rad=_finite(
                "output_double_register_38", sample.get("output_double_register_38")
            ),
            host_position_error_m=_norm(
                tuple(pose[index] - self.home_pose[index] for index in range(3))
            ),
            host_orientation_error_rad=_orientation_error(
                self.home_pose[3:6], pose[3:6]
            ),
            host_joint_error_max_rad=max(
                abs(joints[index] - self.home_q[index]) for index in range(6)
            ),
            host_tcp_linear_speed_m_s=_norm(tcp_speed[:3]),
            host_tcp_angular_speed_rad_s=_norm(tcp_speed[3:6]),
            host_qd_max_rad_s=max(abs(value) for value in qd),
        )
        return tp, metrics, safety

    def observe(self, sample: Mapping[str, Any], *, monotonic_s: float) -> bool:
        timestamp = _finite("monotonic_s", monotonic_s)
        try:
            tp, metrics, safety = self._sample_metrics(sample)
        except (MailboxError, IncompleteTpSnapshot):
            self.reset()
            return False
        identity = (
            tp.campaign_epoch_echo,
            tp.trial_id_echo,
            tp.candidate_token_echo,
            tp.execution_profile_id_echo,
            tp.consumed_command_seq,
            tp.terminal_reason,
        )
        exact = (
            tp.state is TpLoopState.WAIT_ACK
            and tp.terminal_reason > 0
            and _tp_identity_matches(self.expected_arm, tp)
            and tp.consumed_command_seq == self.expected_arm.command_seq
        )
        limits_ok = (
            metrics.tp_position_error_m <= 0.003
            and metrics.tp_orientation_error_rad <= 0.05
            and metrics.tp_joint_error_max_rad <= 0.01
            and metrics.host_position_error_m <= 0.003
            and metrics.host_orientation_error_rad <= 0.05
            and metrics.host_joint_error_max_rad <= 0.01
            and metrics.host_tcp_linear_speed_m_s <= 0.001
            and metrics.host_tcp_angular_speed_rad_s <= 0.01
            and metrics.host_qd_max_rad_s <= 0.01
            and safety == "NORMAL"
        )
        discontinuous = (
            self._last_s is not None
            and (timestamp <= self._last_s or timestamp - self._last_s > self.max_sample_gap_s)
        )
        if not exact or not limits_ok or discontinuous or (
            self._identity is not None and self._identity != identity
        ):
            self.reset()
            if not exact or not limits_ok:
                return False
        if self._start_s is None:
            self._start_s = timestamp
            self._identity = identity
            self._metrics = metrics
        else:
            assert self._metrics is not None
            self._metrics = _ClosureMetrics(
                **{
                    name: max(getattr(self._metrics, name), getattr(metrics, name))
                    for name in self._metrics.__dataclass_fields__
                }
            )
        self._last_s = timestamp
        return self.ready

    def finalize(
        self,
        *,
        capture_hashes_complete: bool,
        terminal_manifest_complete: bool,
        fingerprint_closed: bool,
    ) -> SafeClosureEvidence:
        if self._metrics is None or self._identity is None:
            raise ClosureNotReady("no current exact WAIT_ACK closure segment exists")
        metrics = self._metrics
        return SafeClosureEvidence(
            **{
                name: getattr(metrics, name) for name in metrics.__dataclass_fields__
            },
            host_safety_mode="NORMAL",
            host_dwell_s=self.dwell_s,
            trial_token_match=True,
            capture_hashes_complete=capture_hashes_complete,
            terminal_manifest_complete=terminal_manifest_complete,
            fingerprint_closed=fingerprint_closed,
        )


@dataclass(frozen=True)
class ImmutableBundleStoreReceipt:
    """Receipt derived from real bundle bytes plus verified store history."""

    trial_uid: str
    bundle_path: Path
    bundle_sha256: str
    history_identity: str

    def __post_init__(self) -> None:
        if not _SHA256_RE.fullmatch(self.trial_uid):
            raise MailboxError("store receipt trial_uid is invalid")
        if not self.bundle_path.is_absolute() or not self.bundle_path.is_file():
            raise MailboxError("store receipt requires absolute bundle bytes")
        if not _SHA256_RE.fullmatch(self.bundle_sha256):
            raise MailboxError("store receipt bundle SHA-256 is invalid")
        if not _SHA256_RE.fullmatch(self.history_identity):
            raise MailboxError("store receipt history identity is invalid")


@dataclass(frozen=True)
class ClosureAckResult:
    """Durable result of closure, bundle publication, and ACK dispatch."""

    closure: ClosureEvidence
    manifest: CaptureManifest
    evaluation: Evaluation
    immutable_bundle_path: Path
    store_receipt: ImmutableBundleStoreReceipt
    close_decision: Any
    ack_packet: HostPacket | None


@dataclass(frozen=True)
class DirectBundleResult:
    """r006 result through immutable bundle and fresh-store cold-read."""

    closure: ClosureEvidence
    manifest: CaptureManifest
    evaluation: Evaluation
    immutable_bundle_path: Path
    store_receipt: ImmutableBundleStoreReceipt
    close_decision: Any
    verified_resume_history: tuple[Mapping[str, Any], ...]


def finalize_produced_bundle_direct(
    *,
    closure: ClosureEvidence,
    terminal_reason: int,
    producer: TrialArtifactProducer,
    expected_arm: HostPacket,
    home_reference: CampaignHomeReference,
    backend: Any,
    store: Any,
    coordinator: Any,
    source_fingerprint_post: str | None = None,
    config_fingerprint_post: str | None = None,
    bundle_committed: Callable[[ImmutableBundleStoreReceipt], None] | None = None,
    control_candidate_uid: str | None = None,
) -> DirectBundleResult:
    """Production r006 seam: sealed CSV -> bundle -> fresh cold-read, no ACK."""

    trial = producer.trial
    home_reference.verify_trial(trial)
    if expected_arm.command is not HostCommand.ARM or any(
        (
            expected_arm.campaign_epoch != trial.campaign.campaign_epoch,
            expected_arm.trial_id != trial.trial_id,
            expected_arm.candidate_token != trial.candidate_token,
            expected_arm.command_seq != trial.command_seq,
        )
    ):
        raise MailboxError("direct bundle ARM identity differs from TrialSpec")
    source_post = source_fingerprint_post or trial.source_fingerprint
    config_post = config_fingerprint_post or trial.config_fingerprint
    manifest = producer.build_manifest(
        closure,
        expected_arm=expected_arm,
        expected_terminal_reason=terminal_reason,
        home_reference=home_reference,
        source_fingerprint_post=source_post,
        config_fingerprint_post=config_post,
    )
    evaluation = backend.evaluate_trial(trial, manifest, producer.paths.csv_path)
    if not isinstance(evaluation, Evaluation):
        raise TypeError("backend.evaluate_trial must return Evaluation")
    bundle_path = store.write_trial_bundle(
        trial,
        manifest,
        evaluation,
        artifact_paths=producer.paths,
    )
    if not isinstance(bundle_path, Path) or not bundle_path.is_absolute():
        raise MailboxError("campaign store must return an absolute bundle path")
    decision = coordinator.close_trial(
        manifest=manifest,
        evaluation=evaluation,
        safe_closure=closure,
        bundle_path=bundle_path,
        control_candidate_uid=control_candidate_uid,
    )
    from step5d_autotune_store import cold_read_resume_history_subprocess

    verified_history = tuple(
        cold_read_resume_history_subprocess(store.root.resolve())
    )
    matching_rows = [
        row
        for row in verified_history
        if isinstance(row, Mapping) and row.get("trial_uid") == trial.trial_uid
    ]
    if len(matching_rows) != 1 or not isinstance(
        matching_rows[0].get("history_identity"), str
    ):
        raise MailboxError("direct bundle requires one fresh cold-read history row")
    store_receipt = ImmutableBundleStoreReceipt(
        trial_uid=trial.trial_uid,
        bundle_path=bundle_path,
        bundle_sha256=_sha256_regular(bundle_path),
        history_identity=matching_rows[0]["history_identity"],
    )
    if bundle_committed is not None:
        bundle_committed(store_receipt)
    coordinator.persist_direct_advance(
        bundle_path,
        verified_resume_history=verified_history,
    )
    return DirectBundleResult(
        closure=closure,
        manifest=manifest,
        evaluation=evaluation,
        immutable_bundle_path=bundle_path,
        store_receipt=store_receipt,
        close_decision=decision,
        verified_resume_history=verified_history,
    )


def _finalize_bundle_and_dispatch_ack(
    *,
    collector: Any,
    trial: TrialSpec,
    manifest_factory: Callable[[ClosureEvidence], CaptureManifest],
    backend: Any,
    store: Any,
    coordinator: Any,
    artifact_paths: CaptureArtifactPaths,
    csv_path: Path,
    prepared_trial: Any,
    command_sink: Any,
    capture_hashes_complete: bool,
    terminal_manifest_complete: bool,
    fingerprint_closed: bool,
    bundle_committed: Callable[[ImmutableBundleStoreReceipt], None] | None = None,
) -> ClosureAckResult:
    """Internal ordering primitive shared by production and the fixture seam.

    This is the executable host seam between RTDE closure evidence and the
    recovery-owned coordinator.  It performs no controller connection itself:
    ``command_sink`` is normally ``AtomicCommandMailbox``.  Ordering is fixed as
    evaluate -> immutable bundle -> supervisor close -> verified history ->
    durable issue_ack -> mailbox dispatch.
    """

    expected = collector.expected_arm
    if any(
        (
            expected.campaign_epoch != trial.campaign.campaign_epoch,
            expected.trial_id != trial.trial_id,
            expected.candidate_token != trial.candidate_token,
            expected.command_seq != trial.command_seq,
        )
    ):
        raise MailboxError("closure collector identity differs from TrialSpec")
    closure = collector.finalize(
        capture_hashes_complete=capture_hashes_complete,
        terminal_manifest_complete=terminal_manifest_complete,
        fingerprint_closed=fingerprint_closed,
    )
    manifest = manifest_factory(closure)
    if not isinstance(manifest, CaptureManifest):
        raise TypeError("manifest_factory must return CaptureManifest")
    if any(
        (
            manifest.trial_uid != trial.trial_uid,
            manifest.candidate_token != trial.candidate_token,
            manifest.safe_closure_evidence != closure,
            not manifest.immutable_bundle_written,
        )
    ):
        raise MailboxError(
            "capture manifest does not bind closure/trial or immutable-bundle intent"
        )
    evaluation = backend.evaluate_trial(trial, manifest, csv_path)
    if not isinstance(evaluation, Evaluation):
        raise TypeError("backend.evaluate_trial must return Evaluation")
    bundle_path = store.write_trial_bundle(
        trial,
        manifest,
        evaluation,
        artifact_paths=artifact_paths,
    )
    if not isinstance(bundle_path, Path) or not bundle_path.is_absolute():
        raise MailboxError("campaign store must return an absolute immutable bundle path")
    decision = coordinator.close_trial(
        manifest=manifest,
        evaluation=evaluation,
        safe_closure=closure,
        bundle_path=bundle_path,
    )
    ack_packet: HostPacket | None = None
    verified_history = store.read_resume_history()
    matching_rows = [
        row
        for row in verified_history
        if isinstance(row, Mapping) and row.get("trial_uid") == trial.trial_uid
    ]
    if len(matching_rows) != 1 or not isinstance(
        matching_rows[0].get("history_identity"), str
    ):
        raise MailboxError("store receipt requires one verified exact history row")
    store_receipt = ImmutableBundleStoreReceipt(
        trial_uid=trial.trial_uid,
        bundle_path=bundle_path,
        bundle_sha256=_sha256_regular(bundle_path),
        history_identity=matching_rows[0]["history_identity"],
    )
    if bundle_committed is not None:
        bundle_committed(store_receipt)
    if bool(getattr(decision, "ack_permitted", False)):
        ack_packet = coordinator.issue_ack(
            bundle_path,
            verified_resume_history=verified_history,
        )
        if not isinstance(ack_packet, HostPacket) or (
            ack_packet.command is not HostCommand.ACK_BUNDLE
        ):
            raise MailboxError("coordinator issue_ack did not return ACK_BUNDLE")
        coordinator.dispatch(
            ack_packet,
            prepared_trial=prepared_trial,
            sink=command_sink,
        )
    return ClosureAckResult(
        closure=closure,
        manifest=manifest,
        evaluation=evaluation,
        immutable_bundle_path=bundle_path,
        store_receipt=store_receipt,
        close_decision=decision,
        ack_packet=ack_packet,
    )


def finalize_bundle_and_dispatch_ack_for_test_fixture(**kwargs: Any) -> ClosureAckResult:
    """Explicit test-only seam permitting a fixture-authored manifest factory."""

    return _finalize_bundle_and_dispatch_ack(**kwargs)


def finalize_produced_bundle_and_dispatch_ack(
    *,
    collector: Any,
    producer: TrialArtifactProducer,
    backend: Any,
    store: Any,
    coordinator: Any,
    prepared_trial: Any,
    command_sink: Any,
    source_fingerprint_post: str | None = None,
    config_fingerprint_post: str | None = None,
    bundle_committed: Callable[[ImmutableBundleStoreReceipt], None] | None = None,
) -> ClosureAckResult:
    """Production full-chain seam from rotated CSV through verified ACK.

    ``immutable_bundle_written`` in the current CaptureManifest schema is
    treated only as an intent at manifest construction.  ACK additionally
    requires ``ImmutableBundleStoreReceipt`` from real bundle bytes plus one
    verified history identity, followed by a durable coordinator ACK journal.
    """

    trial = producer.trial
    home_reference = collector.require_production_home(trial)
    terminal_reason = collector.terminal_reason
    if terminal_reason is None:
        raise ClosureNotReady("production assessment requires an exact WAIT_ACK reason")
    source_post = source_fingerprint_post or trial.source_fingerprint
    config_post = config_fingerprint_post or trial.config_fingerprint

    def manifest_factory(closure: ClosureEvidence) -> CaptureManifest:
        return producer.build_manifest(
            closure,
            expected_arm=collector.expected_arm,
            expected_terminal_reason=terminal_reason,
            home_reference=home_reference,
            source_fingerprint_post=source_post,
            config_fingerprint_post=config_post,
        )

    return _finalize_bundle_and_dispatch_ack(
        collector=collector,
        trial=trial,
        manifest_factory=manifest_factory,
        backend=backend,
        store=store,
        coordinator=coordinator,
        artifact_paths=producer.paths,
        csv_path=producer.paths.csv_path,
        prepared_trial=prepared_trial,
        command_sink=command_sink,
        capture_hashes_complete=True,
        terminal_manifest_complete=True,
        fingerprint_closed=(
            source_post == trial.source_fingerprint
            and config_post == trial.config_fingerprint
        ),
        bundle_committed=bundle_committed,
    )
