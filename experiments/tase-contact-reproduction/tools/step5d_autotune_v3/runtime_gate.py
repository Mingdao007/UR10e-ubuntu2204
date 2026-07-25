"""Runtime campaign lease and the final machine gate in front of TP ARM.

The canonical shell invocation creates one lease for one immutable release and
campaign.  The bridge may start without the gate, but the mailbox runtime
cannot consume ARM until the supervisor publishes a fresh observation proving
the loaded controller path, TP runtime identity, process identity, and safety
state still agree.  The lease is orchestration authority only; it never
replaces the robot's fail-closed safety guards or physical Play/Stop.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import time
from typing import Any, Callable, Mapping

from .release_identity import (
    ReleaseIdentityError,
    load_runtime_release,
)

from .runtime_installation import (
    RuntimeInstallationError,
    load_runtime_pointer_identity,
    load_runtime_pointer_integrity,
    runtime_binding,
)
from .state import StateError, atomic_json, read_strict_json


LEASE_SCHEMA = "step5d.autotune-v3/campaign-lease-v1"
OBSERVATION_SCHEMA = "step5d.autotune-v3/arm-gate-observation-v3"
RUNTIME_IDENTITY_SCHEMA = "step5d.autotune-v3/tp-runtime-identity-v1"
ARM_GATE_MAX_AGE_S = 1.0
ARM_GRANT_MAX_AGE_S = 0.25
ARM_GATE_WATCHDOG_INTERVAL_S = 0.25
ARM_GATE_PENDING_INTERVAL_S = 0.025
TP_RUNTIME_IDENTITY_ESTABLISH_TIMEOUT_S = 1.0
CURRENT_POINTER_SCHEMA = "step5d.autotune-v3/current-release-pointer-v1"
CANONICAL_LAUNCHER = "scripts/step5d-autotune-v3.sh"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
_LOADED_PROGRAM_RE = re.compile(r"([^<>\s]+\.urp)(?=$|[>\s])", re.IGNORECASE)


class RuntimeGateError(RuntimeError):
    """A campaign lease or observed ARM predicate is missing or stale."""


RuntimeBindingLoader = Callable[[], Mapping[str, Any]]


def _load_runtime_environment_binding(
    loader: RuntimeBindingLoader,
    *,
    role: str,
) -> dict[str, Any]:
    try:
        value = loader()
    except RuntimeGateError:
        raise
    except RuntimeInstallationError as exc:
        raise RuntimeGateError(
            f"{exc.reason_code}: {role} failed: {exc.detail}"
        ) from exc
    except (OSError, UnicodeError, TypeError, ValueError, KeyError) as exc:
        raise RuntimeGateError(f"{role} failed: {exc}") from exc
    if not isinstance(value, Mapping) or value.get("schema") != (
        "step5d.autotune-v3/runtime-process-binding-v1"
    ):
        raise RuntimeGateError(f"{role} returned an invalid binding")
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        detached = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise RuntimeGateError(f"{role} is not canonical JSON") from exc
    if not isinstance(detached, dict):
        raise RuntimeGateError(f"{role} must be a JSON object")
    return detached


class RuntimeEnvironmentBindingGuard:
    """Revalidate one frozen managed-runtime binding before each new ARM."""

    def __init__(
        self,
        *,
        expected_binding: Mapping[str, Any],
        binding_loader: RuntimeBindingLoader,
    ) -> None:
        if not callable(binding_loader):
            raise RuntimeGateError("runtime environment binding loader is not callable")
        self._expected_binding = _load_runtime_environment_binding(
            lambda: expected_binding,
            role="expected runtime environment binding",
        )
        self._binding_loader = binding_loader
        self._last_command_seq: int | None = None

    @classmethod
    def full(
        cls,
        *,
        runtime_pointer: Mapping[str, Any],
    ) -> "RuntimeEnvironmentBindingGuard":
        """Use a verified startup pointer, then rehash package/host inputs per ARM."""

        expected = _load_runtime_environment_binding(
            lambda: runtime_binding(runtime_pointer=runtime_pointer),
            role="startup runtime environment binding",
        )
        return cls(
            expected_binding=expected,
            binding_loader=lambda: runtime_binding(
                runtime_pointer=load_runtime_pointer_integrity()
            ),
        )

    @classmethod
    def lightweight(cls) -> "RuntimeEnvironmentBindingGuard":
        """Recheck immutable pointer/attestation identity without package rehashing."""

        def load_identity_binding() -> Mapping[str, Any]:
            pointer = load_runtime_pointer_identity()
            return runtime_binding(runtime_pointer=pointer)

        expected = _load_runtime_environment_binding(
            load_identity_binding,
            role="startup lightweight runtime environment binding",
        )
        return cls(
            expected_binding=expected,
            binding_loader=load_identity_binding,
        )

    @classmethod
    def identity(
        cls,
        *,
        runtime_pointer: Mapping[str, Any],
    ) -> "RuntimeEnvironmentBindingGuard":
        """Bind a startup pointer and recheck immutable identity before each ARM."""

        expected = _load_runtime_environment_binding(
            lambda: runtime_binding(runtime_pointer=runtime_pointer),
            role="startup identity runtime environment binding",
        )

        def load_identity_binding() -> Mapping[str, Any]:
            return runtime_binding(runtime_pointer=load_runtime_pointer_identity())

        return cls(
            expected_binding=expected,
            binding_loader=load_identity_binding,
        )

    @property
    def binding_sha256(self) -> str:
        return _canonical_sha256(self._expected_binding)

    def recheck(self, command_seq: int) -> str:
        sequence = _positive_int(command_seq, "runtime recheck command_seq")
        if self._last_command_seq == sequence:
            return self.binding_sha256
        if self._last_command_seq is not None and sequence < self._last_command_seq:
            raise RuntimeGateError(
                "runtime recheck command sequence regressed"
            )
        observed = _load_runtime_environment_binding(
            self._binding_loader,
            role=f"runtime environment binding recheck for command {sequence}",
        )
        if observed != self._expected_binding:
            raise RuntimeGateError(
                "runtime environment binding changed before ARM"
            )
        self._last_command_seq = sequence
        return self.binding_sha256


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_file(path: Path, role: str) -> str:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise RuntimeGateError(f"{role} must be an absolute regular file")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256(value: Any, role: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise RuntimeGateError(f"{role} must be a lowercase SHA-256")
    return value


def _positive_int(value: Any, role: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RuntimeGateError(f"{role} must be a positive integer")
    return value


def _nonnegative_int(value: Any, role: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeGateError(f"{role} must be a non-negative integer")
    return value


def _timestamp(value: Any, role: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise RuntimeGateError(f"{role} must be a zoned timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeGateError(f"{role} is invalid") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise RuntimeGateError(f"{role} must include a timezone")
    return result


def _exact(value: Any, fields: set[str], role: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise RuntimeGateError(f"{role} fields differ")
    return value


@dataclass(frozen=True)
class ArmCommandBinding:
    mailbox_sha256: str
    campaign_epoch: int
    trial_id: int
    command: int
    candidate_token: int
    execution_profile_id: int
    command_seq: int
    logical_batch_sequence: int
    trial_uid: str

    def __post_init__(self) -> None:
        _sha256(self.mailbox_sha256, "ARM mailbox SHA-256")
        for name in (
            "campaign_epoch",
            "trial_id",
            "candidate_token",
            "execution_profile_id",
            "command_seq",
        ):
            _positive_int(getattr(self, name), f"ARM command {name}")
        if self.command != 1:
            raise RuntimeGateError("ARM command binding is not HostCommand.ARM")
        _nonnegative_int(
            self.logical_batch_sequence, "ARM command logical_batch_sequence"
        )
        if not isinstance(self.trial_uid, str) or not self.trial_uid:
            raise RuntimeGateError("ARM command trial_uid must be non-empty")

    @property
    def document(self) -> dict[str, Any]:
        return {
            "mailbox_sha256": self.mailbox_sha256,
            "campaign_epoch": self.campaign_epoch,
            "trial_id": self.trial_id,
            "command": self.command,
            "candidate_token": self.candidate_token,
            "execution_profile_id": self.execution_profile_id,
            "command_seq": self.command_seq,
            "logical_batch_sequence": self.logical_batch_sequence,
            "trial_uid": self.trial_uid,
        }

    @classmethod
    def from_document(cls, payload: Any) -> "ArmCommandBinding":
        row = _exact(
            payload,
            {
                "mailbox_sha256",
                "campaign_epoch",
                "trial_id",
                "command",
                "candidate_token",
                "execution_profile_id",
                "command_seq",
                "logical_batch_sequence",
                "trial_uid",
            },
            "ARM command binding",
        )
        return cls(**dict(row))


def process_starttime(pid: int) -> int:
    """Return Linux /proc starttime, protecting a lease from PID reuse."""

    _positive_int(pid, "process pid")
    try:
        stat_line = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        suffix = stat_line.rsplit(")", 1)[1].split()
        # suffix[0] is field 3 (state); starttime is field 22.
        if suffix[0].upper() in {"Z", "X"}:
            raise RuntimeGateError(f"process {pid} is not live (state={suffix[0]})")
        starttime = int(suffix[19])
    except (FileNotFoundError, IndexError, OSError, ValueError) as exc:
        raise RuntimeGateError(f"process {pid} is not alive with readable identity") from exc
    return _positive_int(starttime, "process starttime")


def _process_matches(value: Any, role: str) -> bool:
    row = _exact(value, {"pid", "starttime"}, role)
    pid = _positive_int(row["pid"], f"{role}.pid")
    expected = _positive_int(row["starttime"], f"{role}.starttime")
    try:
        return process_starttime(pid) == expected
    except RuntimeGateError:
        return False


@dataclass(frozen=True)
class CampaignLease:
    lease_id: str
    launch_id: str
    manifest_sha256: str
    release_stage_id: str
    program_id: str
    protocol_id: str
    campaign_id: str
    campaign_epoch: int
    campaign_fingerprint: str
    safety_envelope_sha256: str
    supervisor_pid: int
    supervisor_starttime: int
    authorization_source: str
    issued_at: str

    def __post_init__(self) -> None:
        for name in ("lease_id", "launch_id"):
            if _HEX32_RE.fullmatch(str(getattr(self, name))) is None:
                raise RuntimeGateError(f"{name} must be 32 lowercase hex characters")
        for name in (
            "manifest_sha256",
            "campaign_fingerprint",
            "safety_envelope_sha256",
        ):
            _sha256(getattr(self, name), name)
        for name in ("release_stage_id", "program_id", "protocol_id", "campaign_id"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise RuntimeGateError(f"{name} must be non-empty")
        _positive_int(self.campaign_epoch, "campaign_epoch")
        _positive_int(self.supervisor_pid, "supervisor_pid")
        _positive_int(self.supervisor_starttime, "supervisor_starttime")
        if not isinstance(self.authorization_source, str) or not self.authorization_source:
            raise RuntimeGateError("authorization_source must be non-empty")
        _timestamp(self.issued_at, "issued_at")

    @classmethod
    def issue(
        cls,
        *,
        lease_id: str,
        launch_id: str,
        manifest_sha256: str,
        release_stage_id: str,
        program_id: str,
        protocol_id: str,
        campaign_id: str,
        campaign_epoch: int,
        campaign_fingerprint: str,
        safety_envelope_sha256: str,
        supervisor_pid: int | None = None,
    ) -> "CampaignLease":
        pid = os.getpid() if supervisor_pid is None else supervisor_pid
        return cls(
            lease_id=lease_id,
            launch_id=launch_id,
            manifest_sha256=manifest_sha256,
            release_stage_id=release_stage_id,
            program_id=program_id,
            protocol_id=protocol_id,
            campaign_id=campaign_id,
            campaign_epoch=campaign_epoch,
            campaign_fingerprint=campaign_fingerprint,
            safety_envelope_sha256=safety_envelope_sha256,
            supervisor_pid=pid,
            supervisor_starttime=process_starttime(pid),
            authorization_source="canonical_shell_bridge_invocation",
            issued_at=datetime.now(timezone.utc).isoformat(),
        )

    @property
    def document(self) -> dict[str, Any]:
        return {
            "schema": LEASE_SCHEMA,
            "lease_id": self.lease_id,
            "launch_id": self.launch_id,
            "release": {
                "manifest_sha256": self.manifest_sha256,
                "release_stage_id": self.release_stage_id,
                "program_id": self.program_id,
                "protocol_id": self.protocol_id,
            },
            "campaign": {
                "campaign_id": self.campaign_id,
                "campaign_epoch": self.campaign_epoch,
                "campaign_fingerprint": self.campaign_fingerprint,
            },
            "safety_envelope_sha256": self.safety_envelope_sha256,
            "supervisor": {
                "pid": self.supervisor_pid,
                "starttime": self.supervisor_starttime,
            },
            "authorization_source": self.authorization_source,
            "issued_at": self.issued_at,
            "physical_play_stop_required": True,
            "safety_boundary": "lease_is_not_an_estop_or_motion_safety_boundary",
        }

    @property
    def sha256(self) -> str:
        return _canonical_sha256(self.document)

    @classmethod
    def from_document(cls, payload: Any) -> "CampaignLease":
        row = _exact(
            payload,
            {
                "schema",
                "lease_id",
                "launch_id",
                "release",
                "campaign",
                "safety_envelope_sha256",
                "supervisor",
                "authorization_source",
                "issued_at",
                "physical_play_stop_required",
                "safety_boundary",
            },
            "campaign lease",
        )
        if (
            row["schema"] != LEASE_SCHEMA
            or row["physical_play_stop_required"] is not True
            or row["safety_boundary"]
            != "lease_is_not_an_estop_or_motion_safety_boundary"
        ):
            raise RuntimeGateError("campaign lease escaped its authority boundary")
        release = _exact(
            row["release"],
            {"manifest_sha256", "release_stage_id", "program_id", "protocol_id"},
            "campaign lease release",
        )
        campaign = _exact(
            row["campaign"],
            {"campaign_id", "campaign_epoch", "campaign_fingerprint"},
            "campaign lease campaign",
        )
        supervisor = _exact(
            row["supervisor"], {"pid", "starttime"}, "campaign lease supervisor"
        )
        return cls(
            lease_id=row["lease_id"],
            launch_id=row["launch_id"],
            manifest_sha256=release["manifest_sha256"],
            release_stage_id=release["release_stage_id"],
            program_id=release["program_id"],
            protocol_id=release["protocol_id"],
            campaign_id=campaign["campaign_id"],
            campaign_epoch=campaign["campaign_epoch"],
            campaign_fingerprint=campaign["campaign_fingerprint"],
            safety_envelope_sha256=row["safety_envelope_sha256"],
            supervisor_pid=supervisor["pid"],
            supervisor_starttime=supervisor["starttime"],
            authorization_source=row["authorization_source"],
            issued_at=row["issued_at"],
        )


def write_campaign_lease(path: Path, lease: CampaignLease) -> str:
    if not path.is_absolute() or path.is_symlink():
        raise RuntimeGateError("campaign lease path must be absolute and non-symlink")
    atomic_json(path, lease.document)
    return lease.sha256


def load_campaign_lease(path: Path, *, expected_sha256: str | None = None) -> CampaignLease:
    try:
        payload = read_strict_json(path, role="campaign lease")
    except StateError as exc:
        raise RuntimeGateError(f"campaign lease is unavailable: {exc}") from exc
    lease = CampaignLease.from_document(payload)
    if expected_sha256 is not None and lease.sha256 != _sha256(
        expected_sha256, "campaign lease reference"
    ):
        raise RuntimeGateError("campaign lease canonical digest differs")
    return lease


def release_runtime_contract(root: Path, release: Any) -> dict[str, Any]:
    """Read the runtime-only contract from the already content-addressed manifest."""

    manifest_path = (root / str(release.manifest_path)).resolve()
    try:
        manifest_path.relative_to(root.resolve())
    except ValueError as exc:
        raise RuntimeGateError("release manifest escapes experiment root") from exc
    if _sha256_file(manifest_path, "release manifest") != release.manifest_sha256:
        raise RuntimeGateError("release manifest digest differs")
    manifest = read_strict_json(manifest_path, role="release manifest")
    runtime_identity = _exact(
        manifest.get("tp_runtime_identity"),
        {
            "schema",
            "program_id",
            "protocol_id",
            "protocol_version",
            "digest_hi",
            "digest_lo",
            "script_basis_sha256",
            "script_artifact_sha256",
            "registers",
        },
        "TP runtime identity",
    )
    if (
        runtime_identity["schema"] != RUNTIME_IDENTITY_SCHEMA
        or runtime_identity["program_id"] != release.program_id
        or runtime_identity["protocol_id"] != release.protocol_id
        or runtime_identity["script_artifact_sha256"]
        != release.artifacts[".script"]["sha256"]
    ):
        raise RuntimeGateError("TP runtime identity differs from release")
    registers = _exact(
        runtime_identity["registers"],
        {"protocol_version", "digest_hi", "digest_lo"},
        "TP runtime identity registers",
    )
    if registers != {"protocol_version": 35, "digest_hi": 36, "digest_lo": 37}:
        raise RuntimeGateError("TP runtime identity register allocation differs")
    for name in ("protocol_version", "digest_hi", "digest_lo"):
        value = runtime_identity[name]
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0x7FFFFFFF:
            raise RuntimeGateError(f"TP runtime identity {name} is outside int31")
    _sha256(runtime_identity["script_basis_sha256"], "TP script basis SHA-256")
    _sha256(runtime_identity["script_artifact_sha256"], "TP script artifact SHA-256")

    envelope = _exact(
        manifest.get("safety_envelope"), {"path", "sha256"}, "safety envelope"
    )
    envelope_path = (manifest_path.parent / str(envelope["path"])).resolve()
    try:
        envelope_path.relative_to(manifest_path.parent)
    except ValueError as exc:
        raise RuntimeGateError("safety envelope escapes immutable release") from exc
    envelope_sha = _sha256(envelope["sha256"], "safety envelope SHA-256")
    if _sha256_file(envelope_path, "safety envelope") != envelope_sha:
        raise RuntimeGateError("safety envelope digest differs")

    expected_program = release.controller_target
    if not isinstance(expected_program, str) or not expected_program.startswith("/"):
        raise RuntimeGateError("release lacks an absolute controller target")
    target = PurePosixPath(expected_program)
    if target.name.lower() != f"{release.program_id}.urp".lower():
        raise RuntimeGateError("controller target basename differs from release")
    return {
        "manifest_sha256": release.manifest_sha256,
        "safety_envelope_sha256": envelope_sha,
        "safety_envelope_path": envelope_path,
        "expected_loaded_program": target.as_posix(),
        "tp_runtime_identity": dict(runtime_identity),
    }


def loaded_program_paths(value: Any) -> set[str]:
    return {
        PurePosixPath(match.rstrip(".,")).as_posix().lower()
        for match in _LOADED_PROGRAM_RE.findall(str(value or ""))
    }


def loaded_program_matches(value: Any, expected: str) -> bool:
    return loaded_program_paths(value) == {PurePosixPath(expected).as_posix().lower()}


def _number(row: Mapping[str, Any], name: str) -> int:
    value = row.get(name)
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeGateError(f"runtime observation lacks numeric {name}") from exc
    if not math.isfinite(number) or not number.is_integer():
        raise RuntimeGateError(f"runtime observation {name} is not an integer")
    return int(number)


def _finite_number(row: Mapping[str, Any], name: str) -> float:
    value = row.get(name)
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeGateError(f"runtime observation lacks numeric {name}") from exc
    if not math.isfinite(number):
        raise RuntimeGateError(f"runtime observation {name} is not finite")
    return number


def validate_tp_runtime_identity(
    output: Mapping[str, Any], runtime_identity: Mapping[str, Any]
) -> None:
    expected = {
        int(runtime_identity["registers"][name]): int(runtime_identity[name])
        for name in ("protocol_version", "digest_hi", "digest_lo")
    }
    for register, value in expected.items():
        if _number(output, f"output_int_register_{register}") != value:
            raise RuntimeGateError(
                f"TP runtime identity mismatch at output_int_register_{register}"
            )


def _current_manifest_sha256(root: Path) -> str:
    try:
        pointer = read_strict_json(
            root / "config/step5d/current.json", role="current release pointer"
        )
    except StateError as exc:
        raise RuntimeGateError(f"current release pointer is unavailable: {exc}") from exc
    row = _exact(
        pointer,
        {"schema", "manifest_path", "manifest_sha256"},
        "current release pointer",
    )
    if row["schema"] != CURRENT_POINTER_SCHEMA:
        raise RuntimeGateError("current release pointer schema differs")
    return _sha256(row["manifest_sha256"], "current release manifest SHA-256")


@dataclass(frozen=True)
class _EffectiveReleaseAuthority:
    mode: str
    manifest_sha256: str
    environment: tuple[tuple[str, str], ...]
    authority_path: Path
    authority_sha256: str


def _effective_release_authority(
    root: Path, expected_manifest_sha256: str
) -> _EffectiveReleaseAuthority:
    """Derive release authority from validated process state, never a caller bypass."""

    try:
        pointer_path = (root / "config/step5d/current.json").resolve(strict=True)
    except OSError as exc:
        raise RuntimeGateError(f"current release pointer is unavailable: {exc}") from exc
    if _current_manifest_sha256(root) != expected_manifest_sha256:
        raise RuntimeGateError("current release differs from campaign release")
    return _EffectiveReleaseAuthority(
        mode="current",
        manifest_sha256=expected_manifest_sha256,
        environment=(),
        authority_path=pointer_path,
        authority_sha256=_sha256_file(pointer_path, "current release pointer"),
    )


def _fingerprint_map(value: Any, role: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or not value:
        raise RuntimeGateError(f"{role} fingerprint map is missing")
    result: dict[str, str] = {}
    for raw_path, raw_digest in value.items():
        if not isinstance(raw_path, str) or not raw_path:
            raise RuntimeGateError(f"{role} fingerprint path is invalid")
        relative = PurePosixPath(raw_path)
        if (
            relative.is_absolute()
            or relative.as_posix() != raw_path
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise RuntimeGateError(f"{role} fingerprint path is unsafe")
        result[raw_path] = _sha256(raw_digest, f"{role} fingerprint")
    return result


def _bound_file_sha256(root: Path, relative: str, role: str) -> str:
    path = root / relative
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise RuntimeGateError(f"{role} escapes its content root") from exc
    if path.is_symlink():
        raise RuntimeGateError(f"{role} must not be a symlink")
    return _sha256_file(resolved, role)


def _bound_release_source_sha256(
    root: Path,
    immutable_root: Path | None,
    relative: str,
    role: str,
) -> str:
    content_root = root
    if immutable_root is not None:
        immutable_path = immutable_root / relative
        if immutable_path.is_symlink():
            raise RuntimeGateError(f"{role} must not be a symlink")
        if immutable_path.exists():
            content_root = immutable_root
    return _bound_file_sha256(content_root, relative, role)


def publish_arm_observation(
    path: Path,
    *,
    lease: CampaignLease,
    lease_sha256: str,
    bridge_pid: int,
    dashboard: Mapping[str, Any],
    rtde_row: Mapping[str, Any],
    contract: Mapping[str, Any],
    csv_age_s: float,
    delivery_transaction_id: str,
    fresh_get_observed_at_unix_ns: int,
    arm_command: Mapping[str, Any] | None = None,
    command_observed_at_unix_ns: int | None = None,
    rtde_row_wall_ns: int | None = None,
    connection_epoch: int = 0,
) -> dict[str, Any]:
    """Atomically refresh the sole ARM gate from current machine observations."""

    command_binding = (
        None if arm_command is None else ArmCommandBinding.from_document(arm_command)
    )
    connection_epoch = _nonnegative_int(connection_epoch, "RTDE connection epoch")
    if not isinstance(delivery_transaction_id, str) or _HEX32_RE.fullmatch(
        delivery_transaction_id
    ) is None:
        raise RuntimeGateError("delivery transaction ID is invalid")
    fresh_get_at = _positive_int(
        fresh_get_observed_at_unix_ns,
        "controller fresh-GET observation timestamp",
    )
    if command_binding is None:
        if command_observed_at_unix_ns is not None:
            raise RuntimeGateError(
                "readiness observation must not carry an ARM request timestamp"
            )
        if rtde_row_wall_ns is not None:
            rtde_row_wall_ns = _positive_int(
                rtde_row_wall_ns, "readiness RTDE row wall timestamp"
            )
    else:
        if command_binding.campaign_epoch != lease.campaign_epoch:
            raise RuntimeGateError("ARM command campaign epoch differs from lease")
        command_observed_at_unix_ns = _positive_int(
            command_observed_at_unix_ns, "ARM command observation timestamp"
        )
        rtde_row_wall_ns = _positive_int(
            rtde_row_wall_ns, "ARM grant RTDE row wall timestamp"
        )
        if rtde_row_wall_ns < command_observed_at_unix_ns:
            raise RuntimeGateError("ARM grant RTDE row predates the command request")
    loaded = dashboard.get("get loaded program", dashboard.get("loaded_program", ""))
    program_state = str(
        dashboard.get("programState", dashboard.get("program_state", ""))
    )
    safety_text = str(dashboard.get("safetymode", dashboard.get("safety_mode", "")))
    runtime_state = _number(rtde_row, "ur_runtime_state")
    safety_mode = _number(rtde_row, "ur_safety_mode")
    controller_timestamp_s = _finite_number(rtde_row, "ur_timestamp")
    rtde_output = {
        name.removeprefix("ur_"): value
        for name, value in rtde_row.items()
        if name.startswith("ur_output_int_register_")
    }
    validate_tp_runtime_identity(rtde_output, contract["tp_runtime_identity"])
    predicates = {
        "campaign_lease_current": _process_matches(
            {"pid": lease.supervisor_pid, "starttime": lease.supervisor_starttime},
            "lease supervisor",
        ),
        "current_release_unchanged": contract["manifest_sha256"]
        == lease.manifest_sha256,
        "safety_envelope_unchanged": contract["safety_envelope_sha256"]
        == lease.safety_envelope_sha256,
        "dashboard_loaded_identity": loaded_program_matches(
            loaded, str(contract["expected_loaded_program"])
        ),
        "dashboard_playing": program_state.strip().upper().startswith("PLAYING"),
        "dashboard_safety_normal": safety_text.strip().upper().endswith("NORMAL"),
        "rtde_playing_normal": runtime_state == 2 and safety_mode == 1,
        "tp_runtime_identity": True,
        "bridge_process_alive": _process_matches(
            {"pid": bridge_pid, "starttime": process_starttime(bridge_pid)},
            "bridge process",
        ),
        "bridge_csv_fresh": 0.0 <= csv_age_s <= 0.75,
    }
    reason_codes = sorted(name for name, ok in predicates.items() if not ok)
    bridge = {"pid": bridge_pid, "starttime": process_starttime(bridge_pid)}
    observed_at = datetime.now(timezone.utc).isoformat()
    gate_kind = "readiness" if command_binding is None else "arm_grant"
    grant_id = (
        None
        if command_binding is None
        else _canonical_sha256(
            {
                "lease_sha256": _sha256(
                    lease_sha256, "campaign lease SHA-256"
                ),
                "manifest_sha256": lease.manifest_sha256,
                "bridge": bridge,
                "arm_command": command_binding.document,
                "command_observed_at_unix_ns": command_observed_at_unix_ns,
                "controller_timestamp_s": controller_timestamp_s,
                "rtde_row_wall_ns": rtde_row_wall_ns,
                "connection_epoch": connection_epoch,
                "delivery_transaction_id": delivery_transaction_id,
                "fresh_get_observed_at_unix_ns": fresh_get_at,
                "observed_at": observed_at,
            }
        )
    )
    payload = {
        "schema": OBSERVATION_SCHEMA,
        "lease_sha256": _sha256(lease_sha256, "campaign lease SHA-256"),
        "observed_at": observed_at,
        "manifest_sha256": lease.manifest_sha256,
        "safety_envelope_sha256": lease.safety_envelope_sha256,
        "gate_kind": gate_kind,
        "command_observed_at_unix_ns": command_observed_at_unix_ns,
        "arm_command": (
            None if command_binding is None else command_binding.document
        ),
        "grant_id": grant_id,
        "campaign": {
            "campaign_id": lease.campaign_id,
            "campaign_epoch": lease.campaign_epoch,
            "campaign_fingerprint": lease.campaign_fingerprint,
        },
        "supervisor": {
            "pid": lease.supervisor_pid,
            "starttime": lease.supervisor_starttime,
        },
        "bridge": bridge,
        "controller": {
            "loaded_program": str(loaded),
            "expected_loaded_program": str(contract["expected_loaded_program"]),
            "program_state": program_state,
            "safety_mode": safety_text,
            "delivery_transaction_id": delivery_transaction_id,
            "fresh_get_observed_at_unix_ns": fresh_get_at,
        },
        "rtde": {
            "runtime_state": runtime_state,
            "safety_mode": safety_mode,
            "tp_state": _number(rtde_row, "ur_output_int_register_26"),
            "protocol_version": _number(rtde_row, "ur_output_int_register_35"),
            "digest_hi": _number(rtde_row, "ur_output_int_register_36"),
            "digest_lo": _number(rtde_row, "ur_output_int_register_37"),
            "controller_timestamp_s": controller_timestamp_s,
            "row_wall_ns": rtde_row_wall_ns,
            "connection_epoch": connection_epoch,
        },
        "predicates": predicates,
        "reason_codes": reason_codes,
        "arm_permitted": not reason_codes,
        "revoked": False,
    }
    atomic_json(path, payload)
    return payload


def revoke_arm_observation(
    path: Path,
    *,
    lease: CampaignLease,
    lease_sha256: str,
    reason_code: str,
) -> None:
    if not isinstance(reason_code, str) or not reason_code:
        raise RuntimeGateError("revocation reason must be non-empty")
    atomic_json(
        path,
        {
            "schema": OBSERVATION_SCHEMA,
            "lease_sha256": _sha256(lease_sha256, "campaign lease SHA-256"),
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "manifest_sha256": lease.manifest_sha256,
            "safety_envelope_sha256": lease.safety_envelope_sha256,
            "gate_kind": "revoked",
            "command_observed_at_unix_ns": None,
            "arm_command": None,
            "grant_id": None,
            "campaign": {
                "campaign_id": lease.campaign_id,
                "campaign_epoch": lease.campaign_epoch,
                "campaign_fingerprint": lease.campaign_fingerprint,
            },
            "supervisor": {
                "pid": lease.supervisor_pid,
                "starttime": lease.supervisor_starttime,
            },
            "bridge": None,
            "controller": None,
            "rtde": None,
            "predicates": {},
            "reason_codes": [reason_code],
            "arm_permitted": False,
            "revoked": True,
        },
    )


@dataclass(frozen=True)
class ValidatedArmContext:
    lease_id: str
    lease_sha256: str
    manifest_sha256: str
    campaign_id: str
    campaign_epoch: int
    campaign_fingerprint: str
    safety_envelope_sha256: str
    observed_at: str
    gate_kind: str
    grant_id: str | None
    mailbox_sha256: str | None
    command_seq: int | None
    controller_timestamp_s: float
    connection_epoch: int


class ArmGateProvider:
    """Bridge-local verifier with a command-keyed monotonic watchdog cache."""

    def __init__(
        self,
        *,
        root: Path,
        gate_path: Path,
        lease_path: Path,
        lease_sha256: str,
        release: Any,
        runtime_environment_guard: RuntimeEnvironmentBindingGuard | None = None,
        identity_establish_timeout_s: float = (
            TP_RUNTIME_IDENTITY_ESTABLISH_TIMEOUT_S
        ),
    ) -> None:
        if not gate_path.is_absolute() or gate_path.is_symlink():
            raise RuntimeGateError("ARM gate path must be absolute and non-symlink")
        if not lease_path.is_absolute() or lease_path.is_symlink():
            raise RuntimeGateError("campaign lease path must be absolute and non-symlink")
        self.root = root.resolve(strict=True)
        self.gate_path = gate_path
        self.lease_path = lease_path
        self.lease = load_campaign_lease(lease_path, expected_sha256=lease_sha256)
        self.lease_sha256 = _sha256(lease_sha256, "campaign lease SHA-256")
        self.contract = release_runtime_contract(self.root, release)
        self.manifest_path = str(release.manifest_path)
        self.source_fingerprints = _fingerprint_map(
            getattr(release, "source_fingerprints", None), "release source"
        )
        self._release_authority = _effective_release_authority(
            self.root, release.manifest_sha256
        )
        self.immutable_source_root = None
        if CANONICAL_LAUNCHER not in self.source_fingerprints:
            raise RuntimeGateError("canonical launcher is absent from release sources")
        verification = getattr(release, "verification", {})
        if verification is None:
            verification = {}
        if not isinstance(verification, Mapping):
            raise RuntimeGateError("release verification metadata is invalid")
        self.repository_source_fingerprints = _fingerprint_map(
            verification.get("repository_source_fingerprints"), "repository source"
        )
        depth = verification.get("repository_source_root_depth")
        if isinstance(depth, bool) or not isinstance(depth, int) or not 0 <= depth <= 4:
            raise RuntimeGateError("repository source root depth is invalid")
        repository_root = self.root
        for _ in range(depth):
            repository_root = repository_root.parent
        self.repository_root = repository_root.resolve(strict=True)
        if (
            self.lease.manifest_sha256 != release.manifest_sha256
            or self.lease.release_stage_id != release.release_stage_id
            or self.lease.program_id != release.program_id
            or self.lease.protocol_id != release.protocol_id
            or self.lease.safety_envelope_sha256
            != self.contract["safety_envelope_sha256"]
        ):
            raise RuntimeGateError("campaign lease release binding differs")
        self._runtime_environment_guard = (
            RuntimeEnvironmentBindingGuard.lightweight()
            if runtime_environment_guard is None
            else runtime_environment_guard
        )
        if not isinstance(
            self._runtime_environment_guard, RuntimeEnvironmentBindingGuard
        ):
            raise RuntimeGateError("ARM gate runtime environment guard is invalid")
        try:
            identity_establish_timeout_s = float(identity_establish_timeout_s)
        except (TypeError, ValueError) as exc:
            raise RuntimeGateError(
                "TP runtime identity establishment timeout is invalid"
            ) from exc
        if (
            not math.isfinite(identity_establish_timeout_s)
            or identity_establish_timeout_s <= 0.0
            or identity_establish_timeout_s
            > TP_RUNTIME_IDENTITY_ESTABLISH_TIMEOUT_S
        ):
            raise RuntimeGateError(
                "TP runtime identity establishment timeout must be in (0, 1.0]"
            )
        self.identity_establish_timeout_s = identity_establish_timeout_s
        self._cache_initialized = False
        self._cached_context: ValidatedArmContext | None = None
        self._cache_valid_until = float("-inf")
        self._cache_key: tuple[str, str | None, int | None] | None = None
        self._last_monotonic: float | None = None
        self._last_rtde_controller_timestamp_s: float | None = None
        self._last_rtde_connection_epoch: int | None = None
        self._play_identity_pending_since_s: float | None = None
        self._play_identity_verified = False

    def _invalidate_cache(self) -> None:
        self._cache_initialized = False
        self._cached_context = None
        self._cache_valid_until = float("-inf")
        self._cache_key = None

    def _reset_play_identity(self) -> None:
        self._play_identity_pending_since_s = None
        self._play_identity_verified = False

    def _watchdog_release_bindings(self) -> None:
        lease = load_campaign_lease(
            self.lease_path, expected_sha256=self.lease_sha256
        )
        if lease != self.lease:
            raise RuntimeGateError("campaign lease changed during campaign")
        if not _process_matches(
            {"pid": lease.supervisor_pid, "starttime": lease.supervisor_starttime},
            "lease supervisor",
        ):
            raise RuntimeGateError("campaign lease supervisor process changed")
        try:
            authority = _effective_release_authority(
                self.root, self.lease.manifest_sha256
            )
        except (OSError, RuntimeGateError) as exc:
            raise RuntimeGateError("effective release changed during campaign") from exc
        if authority != self._release_authority:
            raise RuntimeGateError("effective release changed during campaign")
        if (
            _bound_file_sha256(self.root, self.manifest_path, "release manifest")
            != self.lease.manifest_sha256
        ):
            raise RuntimeGateError("release manifest changed during campaign")
        for relative, expected in sorted(self.source_fingerprints.items()):
            role = (
                "canonical launcher"
                if relative == CANONICAL_LAUNCHER
                else f"release source {relative}"
            )
            if (
                _bound_release_source_sha256(
                    self.root,
                    self.immutable_source_root,
                    relative,
                    role,
                )
                != expected
            ):
                raise RuntimeGateError(f"{role} changed during campaign")
        for relative, expected in sorted(
            self.repository_source_fingerprints.items()
        ):
            if (
                _bound_file_sha256(
                    self.repository_root, relative, f"repository source {relative}"
                )
                != expected
            ):
                raise RuntimeGateError(
                    f"repository source {relative} changed during campaign"
                )
        envelope_path = Path(self.contract["safety_envelope_path"])
        if (
            _sha256_file(envelope_path, "current safety envelope")
            != self.lease.safety_envelope_sha256
        ):
            raise RuntimeGateError("safety envelope changed during campaign")

    def observe_rtde(
        self,
        output: Mapping[str, Any] | None,
        *,
        connection_epoch: int = 0,
    ) -> bool:
        if output is None:
            return True
        try:
            connection_epoch = _nonnegative_int(
                connection_epoch, "bridge RTDE connection epoch"
            )
            connection_changed = (
                self._last_rtde_connection_epoch is not None
                and connection_epoch != self._last_rtde_connection_epoch
            )
            if connection_changed:
                self._invalidate_cache()
                self._last_rtde_controller_timestamp_s = None
                self._reset_play_identity()
            self._last_rtde_connection_epoch = connection_epoch
            runtime_state = _number(output, "runtime_state")
            if runtime_state != 2:
                if (
                    self._play_identity_pending_since_s is not None
                    or self._play_identity_verified
                ):
                    self._invalidate_cache()
                    self._reset_play_identity()
                return True
            if _number(output, "safety_mode") != 1:
                raise RuntimeGateError(
                    "UR safety left NORMAL while program is PLAYING"
                )
            controller_timestamp_s = _finite_number(output, "timestamp")
            if (
                self._last_rtde_controller_timestamp_s is not None
                and controller_timestamp_s
                < self._last_rtde_controller_timestamp_s
                and not connection_changed
            ):
                raise RuntimeGateError(
                    "RTDE controller timestamp regressed within one connection"
                )
            self._last_rtde_controller_timestamp_s = controller_timestamp_s
            try:
                validate_tp_runtime_identity(
                    output, self.contract["tp_runtime_identity"]
                )
            except RuntimeGateError as exc:
                if self._play_identity_verified:
                    raise
                self._invalidate_cache()
                if self._play_identity_pending_since_s is None:
                    self._play_identity_pending_since_s = controller_timestamp_s
                elapsed_s = (
                    controller_timestamp_s
                    - self._play_identity_pending_since_s
                )
                if elapsed_s >= self.identity_establish_timeout_s:
                    raise RuntimeGateError(
                        "TP runtime identity establishment timed out after "
                        f"{self.identity_establish_timeout_s:.3f} s: {exc}"
                    ) from exc
                return False
            self._play_identity_pending_since_s = None
            self._play_identity_verified = True
            return True
        except RuntimeGateError:
            self._invalidate_cache()
            raise

    def readiness_context(self) -> ValidatedArmContext | None:
        return self()

    def __call__(
        self,
        arm_command: Mapping[str, Any] | None = None,
        *,
        connection_epoch: int | None = None,
    ) -> ValidatedArmContext | None:
        expected_command = (
            None
            if arm_command is None
            else ArmCommandBinding.from_document(arm_command)
        )
        if expected_command is None:
            if connection_epoch is not None:
                raise RuntimeGateError(
                    "readiness lookup must not carry an RTDE connection epoch"
                )
            cache_key = ("readiness", None, None)
        else:
            if connection_epoch is None:
                raise RuntimeGateError("ARM lookup lacks its RTDE connection epoch")
            connection_epoch = _nonnegative_int(
                connection_epoch, "ARM lookup RTDE connection epoch"
            )
            cache_key = (
                "arm_grant",
                expected_command.mailbox_sha256,
                connection_epoch,
            )
            if self._last_rtde_connection_epoch != connection_epoch:
                self._invalidate_cache()
                return None
        now = time.monotonic()
        if not math.isfinite(now):
            self._invalidate_cache()
            raise RuntimeGateError("monotonic watchdog clock is invalid")
        if self._last_monotonic is not None and now < self._last_monotonic:
            self._invalidate_cache()
            raise RuntimeGateError("monotonic watchdog clock moved backwards")
        self._last_monotonic = now
        if cache_key != self._cache_key:
            self._invalidate_cache()
        if (
            self._cache_initialized
            and self._cache_key == cache_key
            and now < self._cache_valid_until
        ):
            return self._cached_context

        self._invalidate_cache()
        try:
            context, validity_s, _fresh_get_at = self._refresh(
                expected_command,
                expected_connection_epoch=connection_epoch,
            )
        except RuntimeGateError:
            raise
        except (OSError, StateError, ValueError, TypeError) as exc:
            raise RuntimeGateError(f"ARM gate watchdog failed: {exc}") from exc
        self._cached_context = context
        self._cache_key = cache_key
        self._cache_valid_until = now + min(
            ARM_GATE_WATCHDOG_INTERVAL_S, max(0.0, validity_s)
        )
        self._cache_initialized = self._cache_valid_until > now
        return context

    def _refresh(
        self,
        expected_command: ArmCommandBinding | None,
        *,
        expected_connection_epoch: int | None,
    ) -> tuple[ValidatedArmContext | None, float, int | None]:
        pending_validity_s = (
            ARM_GATE_WATCHDOG_INTERVAL_S
            if expected_command is None
            else ARM_GATE_PENDING_INTERVAL_S
        )
        self._watchdog_release_bindings()
        if not self.gate_path.exists():
            return None, pending_validity_s, None
        try:
            payload = read_strict_json(self.gate_path, role="ARM gate observation")
        except StateError as exc:
            raise RuntimeGateError(f"ARM gate observation is unavailable: {exc}") from exc
        row = _exact(
            payload,
            {
                "schema",
                "lease_sha256",
                "observed_at",
                "manifest_sha256",
                "safety_envelope_sha256",
                "gate_kind",
                "command_observed_at_unix_ns",
                "arm_command",
                "grant_id",
                "campaign",
                "supervisor",
                "bridge",
                "controller",
                "rtde",
                "predicates",
                "reason_codes",
                "arm_permitted",
                "revoked",
            },
            "ARM gate observation",
        )
        if row["schema"] != OBSERVATION_SCHEMA:
            raise RuntimeGateError("ARM gate observation schema differs")
        if row["revoked"] is True:
            return None, pending_validity_s, None
        if row["revoked"] is not False or row["arm_permitted"] is not True:
            raise RuntimeGateError("ARM gate is not permitted")
        gate_kind = row["gate_kind"]
        if expected_command is None:
            if gate_kind != "readiness":
                return None, ARM_GATE_PENDING_INTERVAL_S, None
            if any(
                value is not None
                for value in (
                    row["command_observed_at_unix_ns"],
                    row["arm_command"],
                    row["grant_id"],
                )
            ):
                raise RuntimeGateError("readiness gate carries ARM grant fields")
            max_age_s = ARM_GATE_MAX_AGE_S
        else:
            if gate_kind == "readiness" and row["arm_command"] is None:
                return None, ARM_GATE_PENDING_INTERVAL_S, None
            if gate_kind != "arm_grant":
                raise RuntimeGateError("ARM grant kind differs")
            actual_command = ArmCommandBinding.from_document(row["arm_command"])
            if actual_command.command_seq < expected_command.command_seq:
                return None, ARM_GATE_PENDING_INTERVAL_S, None
            if actual_command.command_seq > expected_command.command_seq:
                raise RuntimeGateError("ARM grant is newer than the pending command")
            if actual_command != expected_command:
                raise RuntimeGateError("ARM grant command binding differs")
            max_age_s = ARM_GRANT_MAX_AGE_S
        observed = _timestamp(row["observed_at"], "ARM gate observed_at")
        age_s = (datetime.now(timezone.utc) - observed).total_seconds()
        if not -0.1 <= age_s <= max_age_s:
            raise RuntimeGateError("ARM gate observation heartbeat is stale")
        campaign = _exact(
            row["campaign"],
            {"campaign_id", "campaign_epoch", "campaign_fingerprint"},
            "ARM gate campaign",
        )
        expected = (
            self.lease_sha256,
            self.lease.manifest_sha256,
            self.lease.safety_envelope_sha256,
            self.lease.campaign_id,
            self.lease.campaign_epoch,
            self.lease.campaign_fingerprint,
        )
        actual = (
            row["lease_sha256"],
            row["manifest_sha256"],
            row["safety_envelope_sha256"],
            campaign["campaign_id"],
            campaign["campaign_epoch"],
            campaign["campaign_fingerprint"],
        )
        if actual != expected:
            raise RuntimeGateError("ARM gate immutable binding differs")
        predicates = row["predicates"]
        if (
            not isinstance(predicates, Mapping)
            or not predicates
            or any(value is not True for value in predicates.values())
            or row["reason_codes"] != []
        ):
            raise RuntimeGateError("ARM gate predicates are incomplete")
        supervisor = _exact(
            row["supervisor"], {"pid", "starttime"}, "ARM gate supervisor"
        )
        if supervisor != {
            "pid": self.lease.supervisor_pid,
            "starttime": self.lease.supervisor_starttime,
        }:
            raise RuntimeGateError("ARM gate supervisor process changed")
        if not _process_matches(row["bridge"], "ARM gate bridge"):
            raise RuntimeGateError("ARM gate bridge process changed")
        controller = _exact(
            row["controller"],
            {
                "loaded_program",
                "expected_loaded_program",
                "program_state",
                "safety_mode",
                "delivery_transaction_id",
                "fresh_get_observed_at_unix_ns",
            },
            "ARM gate controller observation",
        )
        transaction_id = controller["delivery_transaction_id"]
        if (
            not isinstance(transaction_id, str)
            or _HEX32_RE.fullmatch(transaction_id) is None
        ):
            raise RuntimeGateError("ARM gate delivery transaction ID is invalid")
        fresh_get_at = _positive_int(
            controller["fresh_get_observed_at_unix_ns"],
            "ARM gate controller fresh-GET observation",
        )
        rtde = _exact(
            row["rtde"],
            {
                "runtime_state",
                "safety_mode",
                "tp_state",
                "protocol_version",
                "digest_hi",
                "digest_lo",
                "controller_timestamp_s",
                "row_wall_ns",
                "connection_epoch",
            },
            "ARM gate RTDE observation",
        )
        controller_timestamp_s = _finite_number(
            rtde, "controller_timestamp_s"
        )
        connection_epoch = _nonnegative_int(
            rtde["connection_epoch"], "ARM gate RTDE connection epoch"
        )
        grant_id: str | None = None
        mailbox_sha256: str | None = None
        command_seq: int | None = None
        if expected_command is not None:
            if connection_epoch != expected_connection_epoch:
                return None, ARM_GATE_PENDING_INTERVAL_S, None
            if self._last_rtde_controller_timestamp_s is None:
                return None, ARM_GATE_PENDING_INTERVAL_S, None
            if self._last_rtde_controller_timestamp_s < controller_timestamp_s:
                return None, ARM_GATE_PENDING_INTERVAL_S, None
            command_observed_at_unix_ns = _positive_int(
                row["command_observed_at_unix_ns"],
                "ARM grant command observation timestamp",
            )
            row_wall_ns = _positive_int(
                rtde["row_wall_ns"], "ARM grant RTDE row wall timestamp"
            )
            if row_wall_ns < command_observed_at_unix_ns:
                raise RuntimeGateError("ARM grant RTDE row predates the command request")
            observed_at_unix_ns = int(observed.timestamp() * 1_000_000_000)
            if observed_at_unix_ns < command_observed_at_unix_ns:
                raise RuntimeGateError("ARM grant predates the command request")
            grant_id = _sha256(row["grant_id"], "ARM grant id")
            mailbox_sha256 = expected_command.mailbox_sha256
            command_seq = expected_command.command_seq
            self._runtime_environment_guard.recheck(command_seq)
        elif rtde["row_wall_ns"] is not None:
            _positive_int(rtde["row_wall_ns"], "readiness RTDE row wall timestamp")
        return (
            ValidatedArmContext(
                lease_id=self.lease.lease_id,
                lease_sha256=self.lease_sha256,
                manifest_sha256=self.lease.manifest_sha256,
                campaign_id=self.lease.campaign_id,
                campaign_epoch=self.lease.campaign_epoch,
                campaign_fingerprint=self.lease.campaign_fingerprint,
                safety_envelope_sha256=self.lease.safety_envelope_sha256,
                observed_at=row["observed_at"],
                gate_kind=gate_kind,
                grant_id=grant_id,
                mailbox_sha256=mailbox_sha256,
                command_seq=command_seq,
                controller_timestamp_s=controller_timestamp_s,
                connection_epoch=connection_epoch,
            ),
            max_age_s - age_s,
            fresh_get_at,
        )


__all__ = [
    "ARM_GATE_MAX_AGE_S",
    "ARM_GATE_PENDING_INTERVAL_S",
    "ARM_GATE_WATCHDOG_INTERVAL_S",
    "ARM_GRANT_MAX_AGE_S",
    "ArmCommandBinding",
    "ArmGateProvider",
    "CampaignLease",
    "LEASE_SCHEMA",
    "OBSERVATION_SCHEMA",
    "RuntimeGateError",
    "RuntimeEnvironmentBindingGuard",
    "TP_RUNTIME_IDENTITY_ESTABLISH_TIMEOUT_S",
    "ValidatedArmContext",
    "load_campaign_lease",
    "loaded_program_matches",
    "loaded_program_paths",
    "process_starttime",
    "publish_arm_observation",
    "release_runtime_contract",
    "revoke_arm_observation",
    "validate_tp_runtime_identity",
    "write_campaign_lease",
]
