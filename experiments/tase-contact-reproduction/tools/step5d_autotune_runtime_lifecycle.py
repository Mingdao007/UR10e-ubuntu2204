#!/usr/bin/env python3
"""Production adapter from Step5d V3 trials to the shared batch runtime."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path
import stat
import sys
from typing import Any, Callable, Mapping


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SOURCE = ROOT.parents[1] / "src" / "ur10e_experiment_runtime"
if str(RUNTIME_SOURCE) not in sys.path:
    sys.path.insert(0, str(RUNTIME_SOURCE))

from ur10e_experiment_runtime import (  # noqa: E402
    BatchFate,
    BatchIdentity,
    BatchJournal,
    BatchRow,
    DirectReadyReceipt,
    EvidenceSink,
    ExactAckReceipt,
    ReturnReferenceKind,
    SafeClosureReceipt,
    build_trial_brief,
    build_direct_trial_brief,
    canonical_sha256,
    return_reference,
)
from ur10e_experiment_runtime.identity import (  # noqa: E402
    canonical_json_bytes,
    strict_json_loads,
)
from ur10e_experiment_runtime.failure_to_guard import (  # noqa: E402
    MetricRole,
    ObserverStatus,
    OracleStatus,
    TrialOutcomeClass,
)
from ur10e_experiment_runtime.physical_prior import (  # noqa: E402
    STEP5D_V3_PHYSICAL_PRIOR,
)
from ur10e_experiment_runtime.return_route import (  # noqa: E402
    RETURN_ANGULAR_ACCELERATION_GUARD_RAD_S2,
    RETURN_ANGULAR_SPEED_GUARD_RAD_S,
    RETURN_CONTROLLER_MAX_SAMPLE_GAP_S,
    return_policy_fingerprint,
)
from ur10e_experiment_runtime.stage_adapters import (  # noqa: E402
    moving_sphere_safety_envelope_fingerprint,
    stage_autotune_adapter_fingerprint,
)

from step5d_autotune_batch_plan import CandidateBatchPlan  # noqa: E402
from step5d_autotune_contract import (  # noqa: E402
    CaptureManifest,
    DirectReadyClosureEvidence,
    Evaluation,
    ExecutionProfile,
    ForceCandidate,
    TypedSafeClosureEvidence,
    TrialDisposition,
    TrialSpec,
)
from step5d_autotune_live_driver import (  # noqa: E402
    CampaignHomeReference,
    ClosureNotReady,
    ImmutableBundleStoreReceipt,
)
from step5d_autotune_state_machine import HostPacket  # noqa: E402


_SHA256_CHARACTERS = frozenset("0123456789abcdef")
_RETURN_GUARDS = frozenset(
    {
        "force",
        "torque",
        "joints",
        "sensor_freshness",
        "heartbeat",
        "contact_loss",
        "route_workspace",
    }
)
_RETURN_GUARD_BITS = {
    "force": 1 << 0,
    "torque": 1 << 1,
    "joints": 1 << 2,
    "sensor_freshness": 1 << 3,
    "heartbeat": 1 << 4,
    "contact_loss": 1 << 5,
    "route_workspace": 1 << 6,
}
RETURN_PHASE_MISMATCH = "return_phase_mismatch"


def _sha256(name: str, value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _SHA256_CHARACTERS for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA256")
    return value


@dataclass(frozen=True)
class PostAckControllerReadback:
    """Exact controller proof after ACK and typed return-target closure."""

    batch_uid: str
    row_index: int
    trial_uid: str
    return_reference_uid: str
    return_reference: ReturnReferenceKind
    ack_command_seq: int
    consumed_command_seq: int
    tp_state: str
    batch_row_echo: int
    return_kind_echo: str
    return_guard_mask: int
    position_error_m: float
    orientation_error_rad: float
    tcp_linear_speed_m_s: float
    tcp_angular_speed_rad_s: float
    qd_max_rad_s: float
    return_phase_echo: float
    return_segment_id: int
    return_current_angular_speed_rad_s: float
    return_current_angular_acceleration_rad_s2: float
    return_max_angular_speed_rad_s: float
    return_max_angular_acceleration_rad_s2: float
    return_max_sample_gap_s: float
    dwell_s: float
    safety_mode: str
    safety_guards: Mapping[str, bool]
    transcript_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "batch_uid",
            "trial_uid",
            "return_reference_uid",
            "transcript_sha256",
        ):
            _sha256(name, getattr(self, name))
        if self.return_reference is not (
            ReturnReferenceKind.CAMPAIGN_HOME
            if self.row_index == 10
            else ReturnReferenceKind.NEAR_READY
        ):
            raise ValueError("post-ACK return reference differs from exact batch row")
        expected_state = (
            "READY_HOME_CLOSED"
            if self.return_reference is ReturnReferenceKind.CAMPAIGN_HOME
            else "READY_NEAR"
        )
        if self.tp_state != expected_state:
            raise ValueError("post-ACK TP state differs from typed return reference")
        if self.batch_row_echo != self.row_index or self.return_kind_echo != self.return_reference.value:
            raise ValueError("post-ACK TP return identity echo differs")
        if self.return_guard_mask != 0x7F:
            raise ValueError("post-ACK TP return guard mask is incomplete")
        if self.consumed_command_seq != self.ack_command_seq:
            raise ValueError("post-ACK readback does not prove exact ACK consumption")
        if self.dwell_s < 0.5:
            raise ValueError("post-ACK safe closure dwell is shorter than 0.5 s")
        if self.safety_mode != "NORMAL":
            raise ValueError("post-ACK controller safety mode is not NORMAL")
        limits = {
            "position_error_m": 0.003,
            "orientation_error_rad": 0.05,
            "tcp_linear_speed_m_s": 0.001,
            "tcp_angular_speed_rad_s": 0.01,
            "qd_max_rad_s": 0.01,
            "return_current_angular_speed_rad_s": RETURN_ANGULAR_SPEED_GUARD_RAD_S,
            "return_current_angular_acceleration_rad_s2": RETURN_ANGULAR_ACCELERATION_GUARD_RAD_S2,
            "return_max_angular_speed_rad_s": RETURN_ANGULAR_SPEED_GUARD_RAD_S,
            "return_max_angular_acceleration_rad_s2": RETURN_ANGULAR_ACCELERATION_GUARD_RAD_S2,
            "return_max_sample_gap_s": RETURN_CONTROLLER_MAX_SAMPLE_GAP_S,
        }
        for name, maximum in limits.items():
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0.0 or value > maximum:
                raise ValueError(f"post-ACK {name} exceeds the frozen limit")
        if not math.isclose(self.return_phase_echo, 40.3, abs_tol=1e-9):
            raise ValueError("post-ACK readback lacks completed return phase")
        if type(self.return_segment_id) is not int or self.return_segment_id != 3:
            raise ValueError("post-ACK readback lacks exact return segment identity")
        if self.return_max_sample_gap_s <= 0.0:
            raise ValueError("post-ACK readback lacks a positive return sample gap")
        if set(self.safety_guards) != _RETURN_GUARDS or not all(
            type(value) is bool and value for value in self.safety_guards.values()
        ):
            raise ValueError("post-ACK return guards are incomplete or failed")

    def document(self) -> dict[str, Any]:
        return {
            "schema": "step5d.autotune-v3/post-ack-controller-readback-v2",
            "batch_uid": self.batch_uid,
            "row_index": self.row_index,
            "trial_uid": self.trial_uid,
            "return_reference_uid": self.return_reference_uid,
            "return_reference": self.return_reference.value,
            "ack_command_seq": self.ack_command_seq,
            "consumed_command_seq": self.consumed_command_seq,
            "tp_state": self.tp_state,
            "batch_row_echo": self.batch_row_echo,
            "return_kind_echo": self.return_kind_echo,
            "return_guard_mask": self.return_guard_mask,
            "position_error_m": self.position_error_m,
            "orientation_error_rad": self.orientation_error_rad,
            "tcp_linear_speed_m_s": self.tcp_linear_speed_m_s,
            "tcp_angular_speed_rad_s": self.tcp_angular_speed_rad_s,
            "qd_max_rad_s": self.qd_max_rad_s,
            "return_phase_echo": self.return_phase_echo,
            "return_segment_id": self.return_segment_id,
            "return_current_angular_speed_rad_s": self.return_current_angular_speed_rad_s,
            "return_current_angular_acceleration_rad_s2": self.return_current_angular_acceleration_rad_s2,
            "return_max_angular_speed_rad_s": self.return_max_angular_speed_rad_s,
            "return_max_angular_acceleration_rad_s2": self.return_max_angular_acceleration_rad_s2,
            "return_max_sample_gap_s": self.return_max_sample_gap_s,
            "dwell_s": self.dwell_s,
            "safety_mode": self.safety_mode,
            "safety_guards": dict(sorted(self.safety_guards.items())),
            "transcript_sha256": self.transcript_sha256,
        }

    @property
    def controller_readback_sha256(self) -> str:
        return canonical_sha256(self.document())


@dataclass(frozen=True)
class TerminalReadyControllerReadback:
    """Exact r006 terminal-ready proof from the sealed production capture."""

    batch_uid: str
    row_index: int
    trial_uid: str
    return_reference_uid: str
    return_reference: ReturnReferenceKind
    arm_command_seq: int
    consumed_command_seq: int
    tp_state: str
    batch_row_echo: int
    return_kind_echo: str
    return_guard_mask: int
    position_error_m: float
    orientation_error_rad: float
    tcp_linear_speed_m_s: float
    tcp_angular_speed_rad_s: float
    qd_max_rad_s: float
    return_phase_echo: float
    return_segment_id: int
    return_current_angular_speed_rad_s: float
    return_current_angular_acceleration_rad_s2: float
    return_max_angular_speed_rad_s: float
    return_max_angular_acceleration_rad_s2: float
    return_max_sample_gap_s: float
    safety_mode: str
    safety_guards: Mapping[str, bool]
    transcript_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "batch_uid",
            "trial_uid",
            "return_reference_uid",
            "transcript_sha256",
        ):
            _sha256(name, getattr(self, name))
        expected_reference = (
            ReturnReferenceKind.CAMPAIGN_HOME
            if self.row_index == 10
            else ReturnReferenceKind.NEAR_READY
        )
        expected_state = (
            "READY_HOME_CLOSED"
            if expected_reference is ReturnReferenceKind.CAMPAIGN_HOME
            else "READY_NEAR"
        )
        if self.return_reference is not expected_reference or self.tp_state != expected_state:
            raise ValueError("terminal-ready reference/state differs from batch row")
        if self.batch_row_echo != self.row_index or self.return_kind_echo != self.return_reference.value:
            raise ValueError("terminal-ready return identity echo differs")
        if self.return_guard_mask != 0x7F:
            raise ValueError("terminal-ready guard mask is incomplete")
        if self.consumed_command_seq != self.arm_command_seq:
            raise ValueError("terminal-ready readback does not retain exact ARM sequence")
        if self.safety_mode != "NORMAL":
            raise ValueError("terminal-ready controller safety mode is not NORMAL")
        limits = {
            "position_error_m": 0.003,
            "orientation_error_rad": 0.05,
            "tcp_linear_speed_m_s": 0.001,
            "tcp_angular_speed_rad_s": 0.01,
            "qd_max_rad_s": 0.01,
            "return_current_angular_speed_rad_s": RETURN_ANGULAR_SPEED_GUARD_RAD_S,
            "return_current_angular_acceleration_rad_s2": RETURN_ANGULAR_ACCELERATION_GUARD_RAD_S2,
            "return_max_angular_speed_rad_s": RETURN_ANGULAR_SPEED_GUARD_RAD_S,
            "return_max_angular_acceleration_rad_s2": RETURN_ANGULAR_ACCELERATION_GUARD_RAD_S2,
            "return_max_sample_gap_s": RETURN_CONTROLLER_MAX_SAMPLE_GAP_S,
        }
        for name, maximum in limits.items():
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0.0 or value > maximum:
                raise ValueError(f"terminal-ready {name} exceeds the frozen limit")
        if not math.isclose(self.return_phase_echo, 40.3, abs_tol=1e-9):
            raise ValueError("terminal-ready readback lacks completed return phase")
        if self.return_segment_id != 3 or self.return_max_sample_gap_s <= 0.0:
            raise ValueError("terminal-ready return envelope is incomplete")
        if set(self.safety_guards) != _RETURN_GUARDS or not all(
            type(value) is bool and value for value in self.safety_guards.values()
        ):
            raise ValueError("terminal-ready safety guards are incomplete or failed")

    def document(self) -> dict[str, Any]:
        return {
            "schema": "step5d.autotune-v3/terminal-ready-controller-readback-v1",
            "protocol": "v3_direct_arm_v1",
            "batch_uid": self.batch_uid,
            "row_index": self.row_index,
            "trial_uid": self.trial_uid,
            "return_reference_uid": self.return_reference_uid,
            "return_reference": self.return_reference.value,
            "arm_command_seq": self.arm_command_seq,
            "consumed_command_seq": self.consumed_command_seq,
            "tp_state": self.tp_state,
            "batch_row_echo": self.batch_row_echo,
            "return_kind_echo": self.return_kind_echo,
            "return_guard_mask": self.return_guard_mask,
            "position_error_m": self.position_error_m,
            "orientation_error_rad": self.orientation_error_rad,
            "tcp_linear_speed_m_s": self.tcp_linear_speed_m_s,
            "tcp_angular_speed_rad_s": self.tcp_angular_speed_rad_s,
            "qd_max_rad_s": self.qd_max_rad_s,
            "return_phase_echo": self.return_phase_echo,
            "return_segment_id": self.return_segment_id,
            "return_current_angular_speed_rad_s": self.return_current_angular_speed_rad_s,
            "return_current_angular_acceleration_rad_s2": self.return_current_angular_acceleration_rad_s2,
            "return_max_angular_speed_rad_s": self.return_max_angular_speed_rad_s,
            "return_max_angular_acceleration_rad_s2": self.return_max_angular_acceleration_rad_s2,
            "return_max_sample_gap_s": self.return_max_sample_gap_s,
            "safety_mode": self.safety_mode,
            "safety_guards": dict(sorted(self.safety_guards.items())),
            "transcript_sha256": self.transcript_sha256,
        }

    @property
    def controller_readback_sha256(self) -> str:
        return canonical_sha256(self.document())


def _post_ack_readback_relative_path(
    *, row_index: int, trial_uid: str, controller_readback_sha256: str
) -> Path:
    _sha256("trial_uid", trial_uid)
    _sha256("controller_readback_sha256", controller_readback_sha256)
    if type(row_index) is not int or not 1 <= row_index <= 10:
        raise ValueError("post-ACK readback row index must be in [1,10]")
    return Path("post_ack_controller_readbacks") / (
        f"row-{row_index:02d}-{trial_uid}-{controller_readback_sha256}.json"
    )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _persist_post_ack_readback(
    *, campaign_root: Path, readback: PostAckControllerReadback
) -> tuple[str, Path]:
    root = campaign_root.resolve()
    if root.is_symlink() or not root.is_dir():
        raise ValueError("campaign root is not a safe directory")
    relative = _post_ack_readback_relative_path(
        row_index=readback.row_index,
        trial_uid=readback.trial_uid,
        controller_readback_sha256=readback.controller_readback_sha256,
    )
    artifact_root = root / relative.parent
    artifact_root.mkdir(mode=0o700, exist_ok=True)
    if artifact_root.is_symlink() or not artifact_root.is_dir():
        raise ValueError("post-ACK readback root is not a safe directory")
    path = root / relative
    payload = canonical_json_bytes(readback.document()) + b"\n"
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o444,
        )
    except FileExistsError:
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise FileExistsError("post-ACK controller readback identity collision")
    else:
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(descriptor)
        _fsync_directory(artifact_root)
    return relative.as_posix(), path.resolve()


def _load_post_ack_readback(
    *, campaign_root: Path, ack: ExactAckReceipt
) -> PostAckControllerReadback:
    if ack.controller_readback_path is None:
        raise ValueError("runtime batch exact ACK lacks controller readback path")
    expected_relative = _post_ack_readback_relative_path(
        row_index=ack.row_index,
        trial_uid=ack.trial_uid,
        controller_readback_sha256=ack.controller_readback_sha256,
    )
    if ack.controller_readback_path != expected_relative.as_posix():
        raise ValueError("runtime batch controller readback path differs")
    root = campaign_root.resolve()
    path = root / expected_relative
    artifact_root = root / expected_relative.parent
    if artifact_root.is_symlink() or not artifact_root.is_dir():
        raise ValueError("runtime batch controller readback root is unsafe")
    if path.is_symlink() or not path.is_file():
        raise ValueError("runtime batch controller readback artifact is missing")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("runtime batch controller readback is not a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            encoded = stream.read()
    finally:
        os.close(descriptor)
    document = strict_json_loads(encoded)
    if not isinstance(document, Mapping):
        raise ValueError("runtime batch controller readback is not an object")
    payload = dict(document)
    if payload.pop("schema", None) != (
        "step5d.autotune-v3/post-ack-controller-readback-v2"
    ):
        raise ValueError("runtime batch controller readback schema differs")
    try:
        payload["return_reference"] = ReturnReferenceKind(payload["return_reference"])
        readback = PostAckControllerReadback(**payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("runtime batch controller readback fields differ") from exc
    if encoded != canonical_json_bytes(readback.document()) + b"\n":
        raise ValueError("runtime batch controller readback encoding differs")
    if any(
        (
            readback.controller_readback_sha256 != ack.controller_readback_sha256,
            readback.batch_uid != ack.batch_uid,
            readback.row_index != ack.row_index,
            readback.trial_uid != ack.trial_uid,
            readback.return_reference_uid != ack.return_reference_uid,
            readback.ack_command_seq != ack.ack_command_seq,
            readback.consumed_command_seq != ack.consumed_command_seq,
        )
    ):
        raise ValueError("runtime batch controller readback identity differs")
    return readback


def _terminal_ready_readback_relative_path(
    *, row_index: int, trial_uid: str, controller_readback_sha256: str
) -> Path:
    _sha256("trial_uid", trial_uid)
    _sha256("controller_readback_sha256", controller_readback_sha256)
    if type(row_index) is not int or not 1 <= row_index <= 10:
        raise ValueError("terminal-ready row index must be in [1,10]")
    return Path("terminal_ready_controller_readbacks") / (
        f"row-{row_index:02d}-{trial_uid}-{controller_readback_sha256}.json"
    )


def _persist_terminal_ready_readback(
    *, campaign_root: Path, readback: TerminalReadyControllerReadback
) -> tuple[str, Path]:
    root = campaign_root.resolve()
    if root.is_symlink() or not root.is_dir():
        raise ValueError("campaign root is not a safe directory")
    relative = _terminal_ready_readback_relative_path(
        row_index=readback.row_index,
        trial_uid=readback.trial_uid,
        controller_readback_sha256=readback.controller_readback_sha256,
    )
    artifact_root = root / relative.parent
    artifact_root.mkdir(mode=0o700, exist_ok=True)
    if artifact_root.is_symlink() or not artifact_root.is_dir():
        raise ValueError("terminal-ready readback root is not a safe directory")
    path = root / relative
    payload = canonical_json_bytes(readback.document()) + b"\n"
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o444,
        )
    except FileExistsError:
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise FileExistsError("terminal-ready readback identity collision")
    else:
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(descriptor)
        _fsync_directory(artifact_root)
    return relative.as_posix(), path.resolve()


def _load_terminal_ready_readback(
    *, campaign_root: Path, completion: DirectReadyReceipt
) -> TerminalReadyControllerReadback:
    expected_relative = _terminal_ready_readback_relative_path(
        row_index=completion.row_index,
        trial_uid=completion.trial_uid,
        controller_readback_sha256=completion.controller_readback_sha256,
    )
    if completion.controller_readback_path != expected_relative.as_posix():
        raise ValueError("terminal-ready readback path differs")
    path = campaign_root.resolve() / expected_relative
    if path.is_symlink() or not path.is_file():
        raise ValueError("terminal-ready readback artifact is missing")
    encoded = path.read_bytes()
    document = strict_json_loads(encoded)
    if not isinstance(document, Mapping):
        raise ValueError("terminal-ready readback is not an object")
    payload = dict(document)
    if payload.pop("schema", None) != (
        "step5d.autotune-v3/terminal-ready-controller-readback-v1"
    ) or payload.pop("protocol", None) != "v3_direct_arm_v1":
        raise ValueError("terminal-ready readback schema differs")
    try:
        payload["return_reference"] = ReturnReferenceKind(payload["return_reference"])
        readback = TerminalReadyControllerReadback(**payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("terminal-ready readback fields differ") from exc
    if encoded != canonical_json_bytes(readback.document()) + b"\n":
        raise ValueError("terminal-ready readback encoding differs")
    if any(
        (
            readback.controller_readback_sha256
            != completion.controller_readback_sha256,
            readback.batch_uid != completion.batch_uid,
            readback.row_index != completion.row_index,
            readback.trial_uid != completion.trial_uid,
            readback.return_reference_uid != completion.return_reference_uid,
            readback.arm_command_seq != completion.arm_command_seq,
            readback.consumed_command_seq != completion.consumed_command_seq,
            readback.tp_state != completion.tp_state,
        )
    ):
        raise ValueError("terminal-ready readback identity differs")
    return readback


@dataclass(frozen=True)
class TrialBriefAdmissionReceipt:
    trial_uid: str
    ack_command_seq: int
    publication_uid: str
    document_sha256: str
    path: Path
    file_sha256: str
    optimizer_eligible: bool

    def __post_init__(self) -> None:
        _sha256("trial_uid", self.trial_uid)
        _sha256("publication_uid", self.publication_uid)
        _sha256("document_sha256", self.document_sha256)
        _sha256("file_sha256", self.file_sha256)
        if not self.path.is_absolute() or not self.path.is_file() or self.path.is_symlink():
            raise ValueError("admission receipt requires absolute regular TrialBrief bytes")
        if self.ack_command_seq < 1:
            raise ValueError("admission ACK sequence must be positive")
        if type(self.optimizer_eligible) is not bool:
            raise ValueError("admission optimizer_eligible must be boolean")


@dataclass(frozen=True)
class DirectTrialBriefAdmissionReceipt:
    trial_uid: str
    arm_command_seq: int
    publication_uid: str
    document_sha256: str
    path: Path
    file_sha256: str
    optimizer_eligible: bool

    def __post_init__(self) -> None:
        for name in ("trial_uid", "publication_uid", "document_sha256", "file_sha256"):
            _sha256(name, getattr(self, name))
        if not self.path.is_absolute() or not self.path.is_file() or self.path.is_symlink():
            raise ValueError("direct admission requires absolute regular TrialBrief bytes")
        if self.arm_command_seq < 1:
            raise ValueError("direct admission ARM sequence must be positive")
        if type(self.optimizer_eligible) is not bool:
            raise ValueError("direct admission optimizer_eligible must be boolean")


class PreAckTypedClosureCollector:
    """Continuous exact-identity WAIT_ACK proof at the row's sealed target."""

    def __init__(
        self,
        *,
        context: "BatchAttemptContext",
        trial: TrialSpec,
        expected_arm: HostPacket,
        campaign_home_reference: CampaignHomeReference,
        required_dwell_s: float = 0.5,
        max_sample_gap_s: float = 0.05,
    ) -> None:
        self.context = context
        self.trial = trial
        self.expected_arm = expected_arm
        self.home_reference = campaign_home_reference
        self.required_dwell_s = float(required_dwell_s)
        self.max_sample_gap_s = float(max_sample_gap_s)
        if self.required_dwell_s < 0.5 or self.max_sample_gap_s <= 0.0:
            raise ValueError("pre-ACK typed closure dwell/gap contract is invalid")
        self.home_reference.verify_trial(trial)
        self.reset()

    def reset(self) -> None:
        self._reset_closure()
        self._mismatch_start_s: float | None = None
        self._mismatch_last_s: float | None = None
        self._mismatch_terminal_reason: int | None = None
        self._failure_reason: str | None = None

    def _reset_closure(self) -> None:
        self._start_s: float | None = None
        self._last_s: float | None = None
        self._last_heartbeat: float | None = None
        self._terminal_reason: int | None = None
        self._return_phase_echo: float | None = None
        self._return_segment_id: int | None = None
        self._maxima = {
            "tp_position_error_m": 0.0,
            "tp_orientation_error_rad": 0.0,
            "tp_qd_max_rad_s": 0.0,
            "host_position_error_m": 0.0,
            "host_orientation_error_rad": 0.0,
            "host_tcp_linear_speed_m_s": 0.0,
            "host_tcp_angular_speed_rad_s": 0.0,
            "host_qd_max_rad_s": 0.0,
            "return_current_angular_speed_rad_s": 0.0,
            "return_current_angular_acceleration_rad_s2": 0.0,
            "return_max_angular_speed_rad_s": 0.0,
            "return_max_angular_acceleration_rad_s2": 0.0,
            "return_max_sample_gap_s": 0.0,
        }

    def _reset_return_phase_mismatch(self) -> None:
        self._mismatch_start_s = None
        self._mismatch_last_s = None
        self._mismatch_terminal_reason = None

    def _observe_return_phase_mismatch(
        self, *, timestamp: float, terminal_reason: int
    ) -> None:
        discontinuous = self._mismatch_last_s is not None and (
            timestamp <= self._mismatch_last_s
            or timestamp - self._mismatch_last_s > self.max_sample_gap_s
        )
        reason_changed = (
            self._mismatch_terminal_reason is not None
            and terminal_reason != self._mismatch_terminal_reason
        )
        if discontinuous or reason_changed:
            self._reset_return_phase_mismatch()
        if self._mismatch_start_s is None:
            self._mismatch_start_s = timestamp
            self._mismatch_terminal_reason = terminal_reason
        self._mismatch_last_s = timestamp
        if timestamp - self._mismatch_start_s >= self.required_dwell_s:
            self._failure_reason = RETURN_PHASE_MISMATCH

    @property
    def dwell_s(self) -> float:
        if self._start_s is None or self._last_s is None:
            return 0.0
        return max(0.0, self._last_s - self._start_s)

    @property
    def ready(self) -> bool:
        return self._terminal_reason is not None and self.dwell_s >= self.required_dwell_s

    @property
    def terminal_reason(self) -> int | None:
        return self._terminal_reason

    @property
    def failure_reason(self) -> str | None:
        return self._failure_reason

    def require_production_home(self, trial: TrialSpec) -> CampaignHomeReference:
        self.home_reference.verify_trial(trial)
        return self.home_reference

    def observe(self, row: Mapping[str, Any], *, monotonic_s: float) -> bool:
        timestamp = PostAckClosureCollector._number(
            {"monotonic_s": monotonic_s}, "monotonic_s"
        )
        expected_kind = (
            2
            if self.context.reference.kind is ReturnReferenceKind.CAMPAIGN_HOME
            else 1
        )
        try:
            pose = PostAckClosureCollector._vector(row, "ur_actual_TCP_pose")
            speed = PostAckClosureCollector._vector(row, "ur_actual_TCP_speed")
            joints = PostAckClosureCollector._vector(row, "ur_actual_q")
            qd = PostAckClosureCollector._vector(row, "ur_actual_qd")
            heartbeat = PostAckClosureCollector._number(row, "heartbeat")
            terminal_reason = PostAckClosureCollector._integer(
                row, "ur_output_int_register_28"
            )
            identity_ok = all(
                (
                    PostAckClosureCollector._integer(row, "ur_output_int_register_24")
                    == self.trial.campaign.campaign_epoch,
                    PostAckClosureCollector._integer(row, "ur_output_int_register_25")
                    == self.trial.trial_id,
                    PostAckClosureCollector._integer(row, "ur_output_int_register_26")
                    == 70,
                    PostAckClosureCollector._integer(row, "ur_output_int_register_27")
                    == self.trial.candidate_token,
                    terminal_reason > 0,
                    PostAckClosureCollector._integer(row, "ur_output_int_register_29")
                    == self.expected_arm.execution_profile_id,
                    PostAckClosureCollector._integer(row, "ur_output_int_register_30")
                    == self.expected_arm.command_seq,
                    PostAckClosureCollector._integer(row, "ur_output_int_register_31")
                    == self.context.row_index,
                    PostAckClosureCollector._integer(row, "ur_output_int_register_32")
                    == expected_kind,
                    PostAckClosureCollector._integer(row, "ur_output_int_register_33")
                    == 0x7F,
                )
            )
            host_position_error = PostAckClosureCollector._norm(
                tuple(
                    pose[index] - self.context.reference.pose_xyz_m[index]
                    for index in range(3)
                )
            )
            host_orientation_error = PostAckClosureCollector._orientation_error(
                self.context.reference.pose_rotvec_rad,
                pose[3:],
            )
            host_linear_speed = PostAckClosureCollector._norm(speed[:3])
            host_angular_speed = PostAckClosureCollector._norm(speed[3:])
            host_qd_max = max(abs(value) for value in qd)
            tp_position_error = PostAckClosureCollector._number(
                row, "ur_output_double_register_36"
            )
            tp_orientation_error = PostAckClosureCollector._number(
                row, "ur_output_double_register_37"
            )
            tp_qd_max = PostAckClosureCollector._number(
                row, "ur_output_double_register_38"
            )
            return_phase_echo = PostAckClosureCollector._number(
                row, "ur_output_double_register_35"
            )
            return_segment_value = PostAckClosureCollector._number(
                row, "ur_output_double_register_39"
            )
            return_segment_id = int(round(return_segment_value))
            return_current_angular_speed = PostAckClosureCollector._number(
                row, "ur_output_double_register_40"
            )
            return_current_angular_acceleration = PostAckClosureCollector._number(
                row, "ur_output_double_register_41"
            )
            return_max_angular_speed = PostAckClosureCollector._number(
                row, "ur_output_double_register_42"
            )
            return_max_angular_acceleration = PostAckClosureCollector._number(
                row, "ur_output_double_register_43"
            )
            return_max_sample_gap = PostAckClosureCollector._number(
                row, "ur_output_double_register_44"
            )
            heartbeat_fresh = (
                self._last_heartbeat is None or heartbeat != self._last_heartbeat
            )
            guards = {
                "force": abs(PostAckClosureCollector._number(row, "normal_force_n"))
                <= 60.0
                and PostAckClosureCollector._number(row, "force_norm_n") <= 100.0,
                "torque": PostAckClosureCollector._number(row, "torque_norm_nm")
                <= 3.0,
                "joints": all(abs(value) <= 6.283185307 for value in joints),
                "sensor_freshness": PostAckClosureCollector._number(
                    row, "sensor_age_s"
                )
                <= 0.1
                and PostAckClosureCollector._number(row, "rtde_feedback_age_s")
                <= 0.05,
                "heartbeat": heartbeat_fresh,
                "contact_loss": terminal_reason > 0,
                "route_workspace": 0.350 <= pose[0] <= 0.650
                and -0.050 <= pose[1] <= 0.250
                and 0.000 <= pose[2] <= 0.350,
            }
            return_telemetry_ok = all(
                (
                    math.isclose(return_phase_echo, 40.3, abs_tol=1e-9),
                    math.isclose(return_segment_value, 3.0, abs_tol=1e-9),
                    return_current_angular_speed
                    <= RETURN_ANGULAR_SPEED_GUARD_RAD_S,
                    return_current_angular_acceleration
                    <= RETURN_ANGULAR_ACCELERATION_GUARD_RAD_S2,
                    return_max_angular_speed <= RETURN_ANGULAR_SPEED_GUARD_RAD_S,
                    return_max_angular_acceleration
                    <= RETURN_ANGULAR_ACCELERATION_GUARD_RAD_S2,
                    0.0 < return_max_sample_gap
                    <= RETURN_CONTROLLER_MAX_SAMPLE_GAP_S,
                )
            )
            other_limits_ok = all(
                (
                    tp_position_error <= self.context.reference.position_tolerance_m,
                    tp_orientation_error
                    <= self.context.reference.orientation_tolerance_rad,
                    tp_qd_max <= self.context.reference.qd_tolerance_rad_s,
                    host_position_error
                    <= self.context.reference.position_tolerance_m,
                    host_orientation_error
                    <= self.context.reference.orientation_tolerance_rad,
                    host_linear_speed
                    <= self.context.reference.linear_speed_tolerance_m_s,
                    host_angular_speed
                    <= self.context.reference.angular_speed_tolerance_rad_s,
                    host_qd_max <= self.context.reference.qd_tolerance_rad_s,
                    PostAckClosureCollector._integer(row, "ur_safety_mode") == 1,
                    all(guards.values()),
                )
            )
        except ValueError:
            self.reset()
            return False
        discontinuous = self._last_s is not None and (
            timestamp <= self._last_s
            or timestamp - self._last_s > self.max_sample_gap_s
        )
        reason_changed = (
            self._terminal_reason is not None
            and terminal_reason != self._terminal_reason
        )
        if identity_ok and not return_telemetry_ok:
            self._reset_closure()
            self._observe_return_phase_mismatch(
                timestamp=timestamp,
                terminal_reason=terminal_reason,
            )
            return False
        self._reset_return_phase_mismatch()
        if not identity_ok or not other_limits_ok or discontinuous or reason_changed:
            self._reset_closure()
            return False
        self._failure_reason = None
        if self._start_s is None:
            self._start_s = timestamp
            self._terminal_reason = terminal_reason
            self._return_phase_echo = return_phase_echo
            self._return_segment_id = return_segment_id
        self._last_s = timestamp
        self._last_heartbeat = heartbeat
        maxima = {
            "tp_position_error_m": tp_position_error,
            "tp_orientation_error_rad": tp_orientation_error,
            "tp_qd_max_rad_s": tp_qd_max,
            "host_position_error_m": host_position_error,
            "host_orientation_error_rad": host_orientation_error,
            "host_tcp_linear_speed_m_s": host_linear_speed,
            "host_tcp_angular_speed_rad_s": host_angular_speed,
            "host_qd_max_rad_s": host_qd_max,
            "return_current_angular_speed_rad_s": return_current_angular_speed,
            "return_current_angular_acceleration_rad_s2": return_current_angular_acceleration,
            "return_max_angular_speed_rad_s": return_max_angular_speed,
            "return_max_angular_acceleration_rad_s2": return_max_angular_acceleration,
            "return_max_sample_gap_s": return_max_sample_gap,
        }
        for name, value in maxima.items():
            self._maxima[name] = max(self._maxima[name], value)
        return self.ready

    def finalize(
        self,
        *,
        capture_hashes_complete: bool,
        terminal_manifest_complete: bool,
        fingerprint_closed: bool,
    ) -> TypedSafeClosureEvidence:
        if not self.ready:
            raise ClosureNotReady("no complete exact WAIT_ACK typed closure exists")
        if self._return_phase_echo is None or self._return_segment_id is None:
            raise ClosureNotReady("return angular-envelope identity was not observed")
        return TypedSafeClosureEvidence(
            return_reference_uid=self.context.reference.reference_uid,
            return_reference_kind=self.context.reference.kind.value,
            batch_row_index=self.context.row_index,
            return_phase_echo=self._return_phase_echo,
            return_segment_id=self._return_segment_id,
            **self._maxima,
            return_guard_mask=0x7F,
            safety_guards={name: True for name in _RETURN_GUARDS},
            host_safety_mode="NORMAL",
            host_dwell_s=self.dwell_s,
            trial_token_match=True,
            capture_hashes_complete=capture_hashes_complete,
            terminal_manifest_complete=terminal_manifest_complete,
            fingerprint_closed=fingerprint_closed,
        )


class PostAckClosureCollector:
    """Bounded 0.5 s typed return/readback collector over new bridge rows."""

    def __init__(
        self,
        *,
        context: "BatchAttemptContext",
        trial: TrialSpec,
        ack_packet: HostPacket,
        required_dwell_s: float = 0.5,
        max_sample_gap_s: float = 0.05,
    ) -> None:
        self.context = context
        self.trial = trial
        self.ack_packet = ack_packet
        self.required_dwell_s = float(required_dwell_s)
        self.max_sample_gap_s = float(max_sample_gap_s)
        if self.required_dwell_s < 0.5 or self.max_sample_gap_s <= 0.0:
            raise ValueError("post-ACK dwell/gap contract is invalid")
        self.reset()

    def reset(self) -> None:
        self._start_s: float | None = None
        self._last_s: float | None = None
        self._last_heartbeat: float | None = None
        self._return_phase_echo: float | None = None
        self._return_segment_id: int | None = None
        self._maxima = {
            "position_error_m": 0.0,
            "orientation_error_rad": 0.0,
            "tcp_linear_speed_m_s": 0.0,
            "tcp_angular_speed_rad_s": 0.0,
            "qd_max_rad_s": 0.0,
            "return_current_angular_speed_rad_s": 0.0,
            "return_current_angular_acceleration_rad_s2": 0.0,
            "return_max_angular_speed_rad_s": 0.0,
            "return_max_angular_acceleration_rad_s2": 0.0,
            "return_max_sample_gap_s": 0.0,
        }
        self._transcript = hashlib.sha256()

    @property
    def dwell_s(self) -> float:
        if self._start_s is None or self._last_s is None:
            return 0.0
        return max(0.0, self._last_s - self._start_s)

    @property
    def ready(self) -> bool:
        return self.dwell_s >= self.required_dwell_s

    @staticmethod
    def _number(row: Mapping[str, Any], name: str) -> float:
        try:
            value = float(row[name])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"post-ACK bridge row lacks {name}") from exc
        if not math.isfinite(value):
            raise ValueError(f"post-ACK bridge row has non-finite {name}")
        return value

    @classmethod
    def _integer(cls, row: Mapping[str, Any], name: str) -> int:
        value = cls._number(row, name)
        if not value.is_integer():
            raise ValueError(f"post-ACK bridge row {name} is not integral")
        return int(value)

    @classmethod
    def _vector(cls, row: Mapping[str, Any], prefix: str) -> tuple[float, ...]:
        return tuple(cls._number(row, f"{prefix}_{index}") for index in range(6))

    @staticmethod
    def _norm(values: tuple[float, ...]) -> float:
        return math.sqrt(sum(value * value for value in values))

    @staticmethod
    def _orientation_error(
        expected: tuple[float, float, float],
        actual: tuple[float, float, float],
    ) -> float:
        from step5d_autotune_live_driver import _orientation_error

        return _orientation_error(expected, actual)

    def observe(self, row: Mapping[str, Any], *, monotonic_s: float) -> bool:
        timestamp = float(monotonic_s)
        expected_state = (
            77
            if self.context.reference.kind is ReturnReferenceKind.CAMPAIGN_HOME
            else 76
        )
        expected_kind = (
            2
            if self.context.reference.kind is ReturnReferenceKind.CAMPAIGN_HOME
            else 1
        )
        try:
            pose = self._vector(row, "ur_actual_TCP_pose")
            speed = self._vector(row, "ur_actual_TCP_speed")
            joints = self._vector(row, "ur_actual_q")
            qd = self._vector(row, "ur_actual_qd")
            heartbeat = self._number(row, "heartbeat")
            identity_ok = all(
                (
                    self._integer(row, "ur_output_int_register_24")
                    == self.trial.campaign.campaign_epoch,
                    self._integer(row, "ur_output_int_register_25")
                    == self.trial.trial_id,
                    self._integer(row, "ur_output_int_register_26") == expected_state,
                    self._integer(row, "ur_output_int_register_27")
                    == self.trial.candidate_token,
                    self._integer(row, "ur_output_int_register_28") == 1,
                    self._integer(row, "ur_output_int_register_29")
                    == self.ack_packet.execution_profile_id,
                    self._integer(row, "ur_output_int_register_30")
                    == self.ack_packet.command_seq,
                    self._integer(row, "ur_output_int_register_31")
                    == self.context.row_index,
                    self._integer(row, "ur_output_int_register_32") == expected_kind,
                    self._integer(row, "ur_output_int_register_33") == 0x7F,
                )
            )
            position_error = self._norm(
                tuple(
                    pose[index] - self.context.reference.pose_xyz_m[index]
                    for index in range(3)
                )
            )
            orientation_error = self._orientation_error(
                self.context.reference.pose_rotvec_rad,
                pose[3:],
            )
            linear_speed = self._norm(speed[:3])
            angular_speed = self._norm(speed[3:])
            qd_max = max(abs(value) for value in qd)
            return_phase_echo = self._number(
                row, "output_double_register_35"
            )
            return_segment_id = self._integer(
                row, "output_double_register_39"
            )
            return_current_angular_speed = self._number(
                row, "output_double_register_40"
            )
            return_current_angular_acceleration = self._number(
                row, "output_double_register_41"
            )
            return_max_angular_speed = self._number(
                row, "output_double_register_42"
            )
            return_max_angular_acceleration = self._number(
                row, "output_double_register_43"
            )
            return_max_sample_gap = self._number(
                row, "output_double_register_44"
            )
            heartbeat_fresh = (
                self._last_heartbeat is None or heartbeat != self._last_heartbeat
            )
            limits_ok = all(
                (
                    position_error <= self.context.reference.position_tolerance_m,
                    orientation_error
                    <= self.context.reference.orientation_tolerance_rad,
                    linear_speed
                    <= self.context.reference.linear_speed_tolerance_m_s,
                    angular_speed
                    <= self.context.reference.angular_speed_tolerance_rad_s,
                    qd_max <= self.context.reference.qd_tolerance_rad_s,
                    math.isclose(return_phase_echo, 40.3, abs_tol=1e-9),
                    return_segment_id == 3,
                    return_current_angular_speed
                    <= RETURN_ANGULAR_SPEED_GUARD_RAD_S,
                    return_current_angular_acceleration
                    <= RETURN_ANGULAR_ACCELERATION_GUARD_RAD_S2,
                    return_max_angular_speed <= RETURN_ANGULAR_SPEED_GUARD_RAD_S,
                    return_max_angular_acceleration
                    <= RETURN_ANGULAR_ACCELERATION_GUARD_RAD_S2,
                    0.0 < return_max_sample_gap
                    <= RETURN_CONTROLLER_MAX_SAMPLE_GAP_S,
                    self._integer(row, "ur_safety_mode") == 1,
                    abs(self._number(row, "normal_force_n")) <= 60.0,
                    self._number(row, "force_norm_n") <= 100.0,
                    self._number(row, "torque_norm_nm") <= 3.0,
                    self._number(row, "sensor_age_s") <= 0.1,
                    self._number(row, "rtde_feedback_age_s") <= 0.05,
                    heartbeat_fresh,
                    all(abs(value) <= 6.283185307 for value in joints),
                    0.350 <= pose[0] <= 0.650,
                    -0.050 <= pose[1] <= 0.250,
                    0.000 <= pose[2] <= 0.350,
                )
            )
        except ValueError:
            self.reset()
            return False
        discontinuous = self._last_s is not None and (
            timestamp <= self._last_s
            or timestamp - self._last_s > self.max_sample_gap_s
        )
        if not identity_ok or not limits_ok or discontinuous:
            self.reset()
            return False
        if self._start_s is None:
            self._start_s = timestamp
            self._return_phase_echo = return_phase_echo
            self._return_segment_id = return_segment_id
        self._last_s = timestamp
        self._last_heartbeat = heartbeat
        maxima = {
            "position_error_m": position_error,
            "orientation_error_rad": orientation_error,
            "tcp_linear_speed_m_s": linear_speed,
            "tcp_angular_speed_rad_s": angular_speed,
            "qd_max_rad_s": qd_max,
            "return_current_angular_speed_rad_s": return_current_angular_speed,
            "return_current_angular_acceleration_rad_s2": return_current_angular_acceleration,
            "return_max_angular_speed_rad_s": return_max_angular_speed,
            "return_max_angular_acceleration_rad_s2": return_max_angular_acceleration,
            "return_max_sample_gap_s": return_max_sample_gap,
        }
        for name, value in maxima.items():
            self._maxima[name] = max(self._maxima[name], value)
        transcript_row = {
            "timestamp": timestamp,
            "controller_timestamp": self._number(row, "ur_timestamp"),
            "heartbeat": heartbeat,
            "pose": list(pose),
            "speed": list(speed),
            "qd": list(qd),
            "return_angular_envelope": {
                "phase_echo": return_phase_echo,
                "segment_id": return_segment_id,
                "current_speed_rad_s": return_current_angular_speed,
                "current_acceleration_rad_s2": return_current_angular_acceleration,
                "max_speed_rad_s": return_max_angular_speed,
                "max_acceleration_rad_s2": return_max_angular_acceleration,
                "max_sample_gap_s": return_max_sample_gap,
            },
            "force": {
                "normal_n": self._number(row, "normal_force_n"),
                "norm_n": self._number(row, "force_norm_n"),
                "torque_norm_nm": self._number(row, "torque_norm_nm"),
            },
        }
        self._transcript.update(canonical_sha256(transcript_row).encode("ascii"))
        return self.ready

    def finalize(self) -> PostAckControllerReadback:
        if not self.ready:
            raise ValueError("post-ACK closure is not ready")
        if self._return_phase_echo is None or self._return_segment_id is None:
            raise ValueError("post-ACK return angular-envelope identity is missing")
        return PostAckControllerReadback(
            batch_uid=self.context.identity.batch_uid,
            row_index=self.context.row_index,
            trial_uid=self.trial.trial_uid,
            return_reference_uid=self.context.reference.reference_uid,
            return_reference=self.context.reference.kind,
            ack_command_seq=self.ack_packet.command_seq,
            consumed_command_seq=self.ack_packet.command_seq,
            tp_state=(
                "READY_HOME_CLOSED"
                if self.context.reference.kind is ReturnReferenceKind.CAMPAIGN_HOME
                else "READY_NEAR"
            ),
            batch_row_echo=self.context.row_index,
            return_kind_echo=self.context.reference.kind.value,
            return_guard_mask=0x7F,
            return_phase_echo=self._return_phase_echo,
            return_segment_id=self._return_segment_id,
            **self._maxima,
            dwell_s=self.dwell_s,
            safety_mode="NORMAL",
            safety_guards={name: True for name in _RETURN_GUARDS},
            transcript_sha256=self._transcript.hexdigest(),
        )


def direct_ready_closure_from_sealed_capture(
    *,
    context: "BatchAttemptContext",
    trial: TrialSpec,
    expected_arm: HostPacket,
    campaign_home_reference: CampaignHomeReference,
    producer: Any,
) -> tuple[DirectReadyClosureEvidence, TerminalReadyControllerReadback, int]:
    """Read the fsync-sealed production CSV and derive r006 lifecycle proof."""

    campaign_home_reference.verify_trial(trial)
    rows, _, _ = producer._capture_rows()
    row = rows[-1]
    number = PostAckClosureCollector._number
    integer = PostAckClosureCollector._integer
    vector = PostAckClosureCollector._vector
    pose = vector(row, "ur_actual_TCP_pose")
    speed = vector(row, "ur_actual_TCP_speed")
    joints = vector(row, "ur_actual_q")
    qd = vector(row, "ur_actual_qd")
    expected_state = 77 if context.row_index == 10 else 76
    expected_kind = 2 if context.row_index == 10 else 1
    terminal_reason = integer(row, "ur_output_int_register_28")
    identity_ok = all(
        (
            integer(row, "ur_output_int_register_24")
            == trial.campaign.campaign_epoch,
            integer(row, "ur_output_int_register_25") == trial.trial_id,
            integer(row, "ur_output_int_register_26") == expected_state,
            integer(row, "ur_output_int_register_27") == trial.candidate_token,
            terminal_reason == 1,
            integer(row, "ur_output_int_register_29")
            == expected_arm.execution_profile_id,
            integer(row, "ur_output_int_register_30") == expected_arm.command_seq,
            integer(row, "ur_output_int_register_31") == context.row_index,
            integer(row, "ur_output_int_register_32") == expected_kind,
            integer(row, "ur_output_int_register_33") == 0x7F,
            integer(row, "ur_safety_mode") == 1,
        )
    )
    if not identity_ok:
        raise ClosureNotReady("sealed capture lacks exact r006 terminal-ready identity")
    host_position_error = PostAckClosureCollector._norm(
        tuple(
            pose[index] - context.reference.pose_xyz_m[index]
            for index in range(3)
        )
    )
    host_orientation_error = PostAckClosureCollector._orientation_error(
        context.reference.pose_rotvec_rad,
        pose[3:],
    )
    host_linear_speed = PostAckClosureCollector._norm(speed[:3])
    host_angular_speed = PostAckClosureCollector._norm(speed[3:])
    host_qd_max = max(abs(value) for value in qd)
    guards = {
        "force": abs(number(row, "normal_force_n")) <= 60.0
        and number(row, "force_norm_n") <= 100.0,
        "torque": number(row, "torque_norm_nm") <= 3.0,
        "joints": all(abs(value) <= 6.283185307 for value in joints),
        "sensor_freshness": number(row, "sensor_age_s") <= 0.1
        and number(row, "rtde_feedback_age_s") <= 0.05,
        "heartbeat": True,
        "contact_loss": terminal_reason > 0,
        "route_workspace": 0.350 <= pose[0] <= 0.650
        and -0.050 <= pose[1] <= 0.250
        and 0.000 <= pose[2] <= 0.350,
    }
    transcript_sha256 = canonical_sha256(
        {
            "t_monotonic_s": number(row, "t_monotonic_s"),
            "ur_timestamp": number(row, "ur_timestamp"),
            "identity": [
                integer(row, f"ur_output_int_register_{index}")
                for index in range(24, 34)
            ],
            "pose": list(pose),
            "speed": list(speed),
            "qd": list(qd),
        }
    )
    readback = TerminalReadyControllerReadback(
        batch_uid=context.identity.batch_uid,
        row_index=context.row_index,
        trial_uid=trial.trial_uid,
        return_reference_uid=context.reference.reference_uid,
        return_reference=context.reference.kind,
        arm_command_seq=expected_arm.command_seq,
        consumed_command_seq=expected_arm.command_seq,
        tp_state="READY_HOME_CLOSED" if context.row_index == 10 else "READY_NEAR",
        batch_row_echo=context.row_index,
        return_kind_echo=context.reference.kind.value,
        return_guard_mask=0x7F,
        position_error_m=host_position_error,
        orientation_error_rad=host_orientation_error,
        tcp_linear_speed_m_s=host_linear_speed,
        tcp_angular_speed_rad_s=host_angular_speed,
        qd_max_rad_s=host_qd_max,
        return_phase_echo=number(row, "ur_output_double_register_35"),
        return_segment_id=int(round(number(row, "ur_output_double_register_39"))),
        return_current_angular_speed_rad_s=number(
            row, "ur_output_double_register_40"
        ),
        return_current_angular_acceleration_rad_s2=number(
            row, "ur_output_double_register_41"
        ),
        return_max_angular_speed_rad_s=number(
            row, "ur_output_double_register_42"
        ),
        return_max_angular_acceleration_rad_s2=number(
            row, "ur_output_double_register_43"
        ),
        return_max_sample_gap_s=number(row, "ur_output_double_register_44"),
        safety_mode="NORMAL",
        safety_guards=guards,
        transcript_sha256=transcript_sha256,
    )
    closure = DirectReadyClosureEvidence(
        return_reference_uid=context.reference.reference_uid,
        return_reference_kind=context.reference.kind.value,
        batch_row_index=context.row_index,
        tp_position_error_m=number(row, "ur_output_double_register_36"),
        tp_orientation_error_rad=number(row, "ur_output_double_register_37"),
        tp_qd_max_rad_s=number(row, "ur_output_double_register_38"),
        return_phase_echo=readback.return_phase_echo,
        return_segment_id=readback.return_segment_id,
        return_current_angular_speed_rad_s=(
            readback.return_current_angular_speed_rad_s
        ),
        return_current_angular_acceleration_rad_s2=(
            readback.return_current_angular_acceleration_rad_s2
        ),
        return_max_angular_speed_rad_s=readback.return_max_angular_speed_rad_s,
        return_max_angular_acceleration_rad_s2=(
            readback.return_max_angular_acceleration_rad_s2
        ),
        return_max_sample_gap_s=readback.return_max_sample_gap_s,
        host_position_error_m=host_position_error,
        host_orientation_error_rad=host_orientation_error,
        host_tcp_linear_speed_m_s=host_linear_speed,
        host_tcp_angular_speed_rad_s=host_angular_speed,
        host_qd_max_rad_s=host_qd_max,
        return_guard_mask=0x7F,
        safety_guards=guards,
        host_safety_mode="NORMAL",
        host_dwell_s=0.0,
        trial_token_match=True,
        capture_hashes_complete=True,
        terminal_manifest_complete=True,
        fingerprint_closed=True,
    )
    if not closure.returned_safe:
        raise ClosureNotReady(
            "sealed direct-ready closure failed: " + ",".join(closure.failures())
        )
    return closure, readback, terminal_reason


@dataclass(frozen=True)
class BatchAttemptContext:
    identity: BatchIdentity
    journal: BatchJournal
    row_index: int
    reference: Any
    trial_brief_root: Path
    retrying_incomplete: bool = False

    @property
    def expected_row(self) -> BatchRow:
        return self.identity.rows[self.row_index - 1]

    def start_attempt(
        self,
        trial: TrialSpec,
        actual_trial_overlay: Mapping[str, Any],
    ) -> None:
        if (
            actual_trial_overlay.get("control_candidate_uid")
            != self.expected_row.control_candidate_uid
            or dict(actual_trial_overlay) != dict(self.expected_row.trial_overlay)
        ):
            raise ValueError("runtime batch attempt differs from the actual trial overlay")
        self.journal.start_attempt(self.row_index, trial.trial_uid)

    def record_bundle(self, receipt: ImmutableBundleStoreReceipt) -> None:
        self.journal.record_bundle(
            self.row_index,
            receipt.trial_uid,
            receipt.bundle_sha256,
        )

    def complete_post_ack(
        self,
        *,
        trial: TrialSpec,
        arm_packet: HostPacket,
        ack_packet: HostPacket,
        store_receipt: ImmutableBundleStoreReceipt,
        manifest: CaptureManifest,
        evaluation: Evaluation,
        readback: PostAckControllerReadback,
    ) -> TrialBriefAdmissionReceipt:
        if any(
            (
                readback.batch_uid != self.identity.batch_uid,
                readback.row_index != self.row_index,
                readback.trial_uid != trial.trial_uid,
                readback.return_reference_uid != self.reference.reference_uid,
                readback.ack_command_seq != ack_packet.command_seq,
                store_receipt.trial_uid != trial.trial_uid,
            )
        ):
            raise ValueError("post-ACK evidence differs from the active batch attempt")
        campaign_root = self.trial_brief_root.parent.resolve()
        readback_relative_path, _ = _persist_post_ack_readback(
            campaign_root=campaign_root,
            readback=readback,
        )
        ack = ExactAckReceipt(
            batch_uid=self.identity.batch_uid,
            row_index=self.row_index,
            trial_uid=trial.trial_uid,
            control_candidate_uid=self.expected_row.control_candidate_uid,
            immutable_bundle_sha256=store_receipt.bundle_sha256,
            return_reference_uid=self.reference.reference_uid,
            controller_readback_sha256=readback.controller_readback_sha256,
            arm_command_seq=arm_packet.command_seq,
            ack_command_seq=ack_packet.command_seq,
            consumed_command_seq=readback.consumed_command_seq,
            controller_readback_path=readback_relative_path,
        )
        verified_readback = _load_post_ack_readback(
            campaign_root=campaign_root,
            ack=ack,
        )
        self.journal.record_ack_consumed(ack)
        closure = SafeClosureReceipt(
            batch_uid=self.identity.batch_uid,
            row_index=self.row_index,
            trial_uid=trial.trial_uid,
            ack_uid=ack.ack_uid,
            return_reference_uid=self.reference.reference_uid,
            controller_readback_sha256=verified_readback.controller_readback_sha256,
            return_reference=verified_readback.return_reference,
        )
        self.journal.record_safe_closure(closure)
        verified_readback = _load_post_ack_readback(
            campaign_root=campaign_root,
            ack=ack,
        )
        outcome_class, metric_role = _classify(evaluation)
        artifact_digests = {
            "bundle": store_receipt.bundle_sha256,
            "capture_csv": manifest.csv_sha256,
            "capture_metadata": manifest.metadata_sha256,
            "terminal_manifest": manifest.terminal_manifest_sha256,
            "post_ack_controller_readback": verified_readback.controller_readback_sha256,
        }
        fingerprint_verified = bool(
            manifest.fingerprint_closed
            and manifest.source_fingerprint_pre == trial.source_fingerprint
            and manifest.source_fingerprint_post == trial.source_fingerprint
            and manifest.config_fingerprint_pre == trial.config_fingerprint
            and manifest.config_fingerprint_post == trial.config_fingerprint
        )
        brief = build_trial_brief(
            batch=self.identity,
            row_index=self.row_index,
            trial_uid=trial.trial_uid,
            immutable_bundle_sha256=store_receipt.bundle_sha256,
            ack=ack,
            closure=closure,
            outcome_class=outcome_class,
            metric_role=metric_role,
            objective=evaluation.objective_mae_n,
            oracle_status=(
                OracleStatus.CREDIBLE
                if manifest.rnn_oracle_aligned
                else OracleStatus.FAILED
            ),
            observer_status=(
                ObserverStatus.COMPLETE
                if manifest.cadence_ok and manifest.feedback_fresh
                else ObserverStatus.INCOMPLETE
            ),
            artifact_digests=artifact_digests,
            fingerprint_verified=fingerprint_verified,
        )
        path = EvidenceSink(self.trial_brief_root, self.journal).publish_trial_brief(
            brief
        )
        published = strict_json_loads(path.read_bytes())
        return TrialBriefAdmissionReceipt(
            trial_uid=trial.trial_uid,
            ack_command_seq=ack_packet.command_seq,
            publication_uid=brief.publication_uid,
            document_sha256=canonical_sha256(published),
            path=path,
            file_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            optimizer_eligible=published["optimizer_eligible"],
        )

    def complete_terminal_ready(
        self,
        *,
        trial: TrialSpec,
        arm_packet: HostPacket,
        store_receipt: ImmutableBundleStoreReceipt,
        manifest: CaptureManifest,
        evaluation: Evaluation,
        readback: TerminalReadyControllerReadback,
    ) -> DirectTrialBriefAdmissionReceipt:
        """Commit one r006 row without creating or consuming an ACK packet."""

        if any(
            (
                readback.batch_uid != self.identity.batch_uid,
                readback.row_index != self.row_index,
                readback.trial_uid != trial.trial_uid,
                readback.return_reference_uid != self.reference.reference_uid,
                readback.arm_command_seq != arm_packet.command_seq,
                readback.consumed_command_seq != arm_packet.command_seq,
                store_receipt.trial_uid != trial.trial_uid,
            )
        ):
            raise ValueError("terminal-ready evidence differs from active batch attempt")
        campaign_root = self.trial_brief_root.parent.resolve()
        readback_relative_path, _ = _persist_terminal_ready_readback(
            campaign_root=campaign_root,
            readback=readback,
        )
        completion = DirectReadyReceipt(
            batch_uid=self.identity.batch_uid,
            row_index=self.row_index,
            trial_uid=trial.trial_uid,
            control_candidate_uid=self.expected_row.control_candidate_uid,
            immutable_bundle_sha256=store_receipt.bundle_sha256,
            return_reference_uid=self.reference.reference_uid,
            controller_readback_sha256=readback.controller_readback_sha256,
            arm_command_seq=arm_packet.command_seq,
            consumed_command_seq=readback.consumed_command_seq,
            tp_state=readback.tp_state,
            controller_readback_path=readback_relative_path,
        )
        verified_readback = _load_terminal_ready_readback(
            campaign_root=campaign_root,
            completion=completion,
        )
        self.journal.record_direct_ready(completion)
        outcome_class, metric_role = _classify(evaluation)
        artifact_digests = {
            "bundle": store_receipt.bundle_sha256,
            "capture_csv": manifest.csv_sha256,
            "capture_metadata": manifest.metadata_sha256,
            "terminal_manifest": manifest.terminal_manifest_sha256,
            "terminal_ready_controller_readback": (
                verified_readback.controller_readback_sha256
            ),
        }
        fingerprint_verified = bool(
            manifest.fingerprint_closed
            and manifest.source_fingerprint_pre == trial.source_fingerprint
            and manifest.source_fingerprint_post == trial.source_fingerprint
            and manifest.config_fingerprint_pre == trial.config_fingerprint
            and manifest.config_fingerprint_post == trial.config_fingerprint
        )
        brief = build_direct_trial_brief(
            batch=self.identity,
            row_index=self.row_index,
            trial_uid=trial.trial_uid,
            immutable_bundle_sha256=store_receipt.bundle_sha256,
            completion=completion,
            outcome_class=outcome_class,
            metric_role=metric_role,
            objective=(
                evaluation.objective_mae_n
                if metric_role is MetricRole.TRAINABLE_OBJECTIVE
                else None
            ),
            oracle_status=(
                OracleStatus.CREDIBLE
                if manifest.rnn_oracle_aligned
                else OracleStatus.FAILED
            ),
            observer_status=(
                ObserverStatus.COMPLETE
                if manifest.cadence_ok and manifest.feedback_fresh
                else ObserverStatus.INCOMPLETE
            ),
            artifact_digests=artifact_digests,
            fingerprint_verified=fingerprint_verified,
        )
        path = EvidenceSink(self.trial_brief_root, self.journal).publish_trial_brief(
            brief
        )
        published = strict_json_loads(path.read_bytes())
        return DirectTrialBriefAdmissionReceipt(
            trial_uid=trial.trial_uid,
            arm_command_seq=arm_packet.command_seq,
            publication_uid=brief.publication_uid,
            document_sha256=canonical_sha256(published),
            path=path,
            file_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            optimizer_eligible=published["optimizer_eligible"],
        )


def _classify(
    evaluation: Evaluation,
) -> tuple[TrialOutcomeClass, MetricRole]:
    disposition = evaluation.disposition
    if disposition is TrialDisposition.OBJECTIVE and evaluation.eligible:
        return (
            TrialOutcomeClass.VALID_PARAMETER_OBSERVATION,
            MetricRole.TRAINABLE_OBJECTIVE,
        )
    outcome = {
        TrialDisposition.WAIT_INFRA_READY: TrialOutcomeClass.INFRASTRUCTURE_FAILURE,
        TrialDisposition.PARAMETER_EVENT: TrialOutcomeClass.MODEL_MISMATCH,
        TrialDisposition.OPERATOR_STOP: TrialOutcomeClass.OPERATOR_STOP,
        TrialDisposition.CODE_CONTRACT_BUG: TrialOutcomeClass.CODE_CONTRACT_FAILURE,
        TrialDisposition.SAFETY_STOP: TrialOutcomeClass.SAFETY_STOP,
        TrialDisposition.MANUAL_RECOVERY: TrialOutcomeClass.SAFETY_STOP,
        TrialDisposition.FAIL_CLOSED: (
            TrialOutcomeClass.OBSERVER_GAP
            if any(
                "cadence" in failure or "feedback" in failure
                for failure in evaluation.structural_failures
            )
            else TrialOutcomeClass.CODE_CONTRACT_FAILURE
        ),
    }.get(disposition, TrialOutcomeClass.CODE_CONTRACT_FAILURE)
    return outcome, MetricRole.UNAVAILABLE


def _evaluation_from_bundle(payload: Any) -> Evaluation:
    if not isinstance(payload, Mapping):
        raise ValueError("runtime batch evaluation is not an object")
    try:
        return Evaluation(
            trial_uid=payload["trial_uid"],
            backend_id=payload["backend_id"],
            eligible=payload["eligible"],
            disposition=TrialDisposition(payload["disposition"]),
            objective_mae_n=payload["objective_mae_n"],
            force_bias_n=payload["force_bias_n"],
            force_std_n=payload["force_std_n"],
            coverage_12_plus_minus_1_ratio=payload[
                "coverage_12_plus_minus_1_ratio"
            ],
            complete_bins=payload["complete_bins"],
            safe_closure=payload["safe_closure"],
            structural_failures=tuple(payload["structural_failures"]),
            metrics=payload["metrics"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("runtime batch evaluation is invalid") from exc


def _ack_from_document(document: Mapping[str, Any] | None) -> ExactAckReceipt:
    if not isinstance(document, Mapping):
        raise ValueError("runtime batch lacks a recoverable exact ACK receipt")
    payload = dict(document)
    if payload.pop("schema", None) != "ur10e.exact_ack_receipt/v2":
        raise ValueError("runtime batch exact ACK receipt schema differs")
    return ExactAckReceipt(**payload)


def _direct_ready_from_document(
    document: Mapping[str, Any] | None,
) -> DirectReadyReceipt:
    if not isinstance(document, Mapping):
        raise ValueError("runtime batch lacks a direct-ready receipt")
    payload = dict(document)
    if payload.pop("schema", None) != "ur10e.direct_ready_receipt/v1":
        raise ValueError("runtime batch direct-ready receipt schema differs")
    return DirectReadyReceipt(**payload)


def _closure_from_document(
    document: Mapping[str, Any] | None,
) -> SafeClosureReceipt:
    if not isinstance(document, Mapping):
        raise ValueError("runtime batch lacks a recoverable closure receipt")
    payload = dict(document)
    if payload.pop("schema", None) != "ur10e.safe_closure_receipt/v1":
        raise ValueError("runtime batch closure receipt schema differs")
    payload["return_reference"] = ReturnReferenceKind(payload["return_reference"])
    return SafeClosureReceipt(**payload)


def recover_runtime_batch_trial_briefs(
    *,
    campaign_root: Path,
) -> dict[
    str, TrialBriefAdmissionReceipt | DirectTrialBriefAdmissionReceipt
]:
    """Idempotently close the safe-closure -> TrialBrief crash cut."""

    batches_root = campaign_root.resolve() / "runtime_batches"
    if not batches_root.exists():
        return {}
    if batches_root.is_symlink() or not batches_root.is_dir():
        raise ValueError("runtime batch root is not a safe directory")
    roots = tuple(
        path
        for path in batches_root.iterdir()
        if path.is_dir() and not path.is_symlink()
    )
    if len(roots) != 1:
        raise ValueError("campaign must contain exactly one runtime BatchIdentity")
    journal = BatchJournal.open(roots[0])
    identity = journal.identity()
    brief_root = campaign_root.resolve() / "trial_briefs"
    state = journal.state()
    for row_index in state.unpublished_trial_brief_row_indices:
        row = state.rows[row_index - 1]
        if row.fate is BatchFate.DIRECT_COMPLETED:
            completion = _direct_ready_from_document(
                row.direct_ready_receipt_document
            )
            readback = _load_terminal_ready_readback(
                campaign_root=campaign_root,
                completion=completion,
            )
            if row.trial_uid is None or row.immutable_bundle_sha256 is None:
                raise ValueError("direct-completed row lacks bundle identity")
            bundle_path = (
                campaign_root.resolve()
                / "store"
                / "trials"
                / row.trial_uid
                / "immutable_trial_bundle.json"
            )
            if (
                bundle_path.is_symlink()
                or not bundle_path.is_file()
                or hashlib.sha256(bundle_path.read_bytes()).hexdigest()
                != row.immutable_bundle_sha256
            ):
                raise ValueError("runtime batch recovery bundle differs")
            bundle = strict_json_loads(bundle_path.read_bytes())
            if not isinstance(bundle, Mapping):
                raise ValueError("runtime batch recovery bundle is not an object")
            trial_payload = bundle.get("trial")
            capture_payload = bundle.get("capture")
            if not isinstance(trial_payload, Mapping) or not isinstance(
                capture_payload, Mapping
            ):
                raise ValueError("runtime batch recovery bundle fields differ")
            manifest = CaptureManifest(**dict(capture_payload))
            evaluation = _evaluation_from_bundle(bundle.get("evaluation"))
            outcome_class, metric_role = _classify(evaluation)
            brief = build_direct_trial_brief(
                batch=identity,
                row_index=row_index,
                trial_uid=row.trial_uid,
                immutable_bundle_sha256=row.immutable_bundle_sha256,
                completion=completion,
                outcome_class=outcome_class,
                metric_role=metric_role,
                objective=(
                    evaluation.objective_mae_n
                    if metric_role is MetricRole.TRAINABLE_OBJECTIVE
                    else None
                ),
                oracle_status=(
                    OracleStatus.CREDIBLE
                    if manifest.rnn_oracle_aligned
                    else OracleStatus.FAILED
                ),
                observer_status=(
                    ObserverStatus.COMPLETE
                    if manifest.cadence_ok and manifest.feedback_fresh
                    else ObserverStatus.INCOMPLETE
                ),
                artifact_digests={
                    "bundle": row.immutable_bundle_sha256,
                    "capture_csv": manifest.csv_sha256,
                    "capture_metadata": manifest.metadata_sha256,
                    "terminal_manifest": manifest.terminal_manifest_sha256,
                    "terminal_ready_controller_readback": (
                        readback.controller_readback_sha256
                    ),
                },
                fingerprint_verified=bool(
                    manifest.fingerprint_closed
                    and manifest.source_fingerprint_pre
                    == trial_payload.get("source_fingerprint")
                    and manifest.source_fingerprint_post
                    == trial_payload.get("source_fingerprint")
                    and manifest.config_fingerprint_pre
                    == trial_payload.get("config_fingerprint")
                    and manifest.config_fingerprint_post
                    == trial_payload.get("config_fingerprint")
                ),
            )
            EvidenceSink(brief_root, journal).publish_trial_brief(brief)
            state = journal.state()
            continue
        ack = _ack_from_document(row.ack_receipt_document)
        closure = _closure_from_document(row.closure_receipt_document)
        readback = _load_post_ack_readback(campaign_root=campaign_root, ack=ack)
        if row.trial_uid is None or row.immutable_bundle_sha256 is None:
            raise ValueError("ACK-completed runtime row lacks bundle identity")
        bundle_path = (
            campaign_root.resolve()
            / "store"
            / "trials"
            / row.trial_uid
            / "immutable_trial_bundle.json"
        )
        if (
            bundle_path.is_symlink()
            or not bundle_path.is_file()
            or hashlib.sha256(bundle_path.read_bytes()).hexdigest()
            != row.immutable_bundle_sha256
        ):
            raise ValueError("runtime batch recovery bundle differs")
        bundle = strict_json_loads(bundle_path.read_bytes())
        if not isinstance(bundle, Mapping):
            raise ValueError("runtime batch recovery bundle is not an object")
        trial_payload = bundle.get("trial")
        capture_payload = bundle.get("capture")
        if not isinstance(trial_payload, Mapping) or not isinstance(
            capture_payload, Mapping
        ):
            raise ValueError("runtime batch recovery bundle fields differ")
        manifest = CaptureManifest(**dict(capture_payload))
        evaluation = _evaluation_from_bundle(bundle.get("evaluation"))
        if any(
            (
                trial_payload.get("trial_uid") != row.trial_uid,
                manifest.trial_uid != row.trial_uid,
                evaluation.trial_uid != row.trial_uid,
            )
        ):
            raise ValueError("runtime batch recovery trial identity differs")
        outcome_class, metric_role = _classify(evaluation)
        brief = build_trial_brief(
            batch=identity,
            row_index=row_index,
            trial_uid=row.trial_uid,
            immutable_bundle_sha256=row.immutable_bundle_sha256,
            ack=ack,
            closure=closure,
            outcome_class=outcome_class,
            metric_role=metric_role,
            objective=evaluation.objective_mae_n,
            oracle_status=(
                OracleStatus.CREDIBLE
                if manifest.rnn_oracle_aligned
                else OracleStatus.FAILED
            ),
            observer_status=(
                ObserverStatus.COMPLETE
                if manifest.cadence_ok and manifest.feedback_fresh
                else ObserverStatus.INCOMPLETE
            ),
            artifact_digests={
                "bundle": row.immutable_bundle_sha256,
                "capture_csv": manifest.csv_sha256,
                "capture_metadata": manifest.metadata_sha256,
                "terminal_manifest": manifest.terminal_manifest_sha256,
                "post_ack_controller_readback": readback.controller_readback_sha256,
            },
            fingerprint_verified=bool(
                manifest.fingerprint_closed
                and manifest.source_fingerprint_pre
                == trial_payload.get("source_fingerprint")
                and manifest.source_fingerprint_post
                == trial_payload.get("source_fingerprint")
                and manifest.config_fingerprint_pre
                == trial_payload.get("config_fingerprint")
                and manifest.config_fingerprint_post
                == trial_payload.get("config_fingerprint")
            ),
        )
        EvidenceSink(brief_root, journal).publish_trial_brief(brief)
        state = journal.state()

    receipts: dict[
        str, TrialBriefAdmissionReceipt | DirectTrialBriefAdmissionReceipt
    ] = {}
    for row in state.rows:
        if row.fate not in {BatchFate.ACK_COMPLETED, BatchFate.DIRECT_COMPLETED}:
            continue
        if row.fate is BatchFate.DIRECT_COMPLETED:
            completion = _direct_ready_from_document(
                row.direct_ready_receipt_document
            )
            if any(
                value is None
                for value in (
                    row.trial_uid,
                    row.trial_brief_publication_uid,
                    row.trial_brief_document_sha256,
                    row.optimizer_eligible,
                )
            ):
                raise ValueError("direct-completed row lacks TrialBrief identity")
            path = brief_root / f"{row.trial_brief_publication_uid}.trial-brief.json"
            encoded = path.read_bytes()
            published = strict_json_loads(encoded)
            if (
                not isinstance(published, Mapping)
                or published.get("completion_uid") != completion.completion_uid
            ):
                raise ValueError("direct TrialBrief completion digest differs")
            receipt = DirectTrialBriefAdmissionReceipt(
                trial_uid=str(row.trial_uid),
                arm_command_seq=completion.arm_command_seq,
                publication_uid=str(row.trial_brief_publication_uid),
                document_sha256=canonical_sha256(published),
                path=path.resolve(),
                file_sha256=hashlib.sha256(encoded).hexdigest(),
                optimizer_eligible=bool(row.optimizer_eligible),
            )
            if receipt.document_sha256 != row.trial_brief_document_sha256:
                raise ValueError("direct TrialBrief document digest differs")
            receipts[receipt.trial_uid] = receipt
            continue
        ack = _ack_from_document(row.ack_receipt_document)
        readback = _load_post_ack_readback(campaign_root=campaign_root, ack=ack)
        if any(
            value is None
            for value in (
                row.trial_uid,
                row.trial_brief_publication_uid,
                row.trial_brief_document_sha256,
                row.optimizer_eligible,
            )
        ):
            raise ValueError("ACK-completed runtime row lacks TrialBrief identity")
        path = brief_root / f"{row.trial_brief_publication_uid}.trial-brief.json"
        encoded = path.read_bytes()
        published = strict_json_loads(encoded)
        if (
            not isinstance(published, Mapping)
            or published.get("artifact_digests", {}).get(
                "post_ack_controller_readback"
            )
            != readback.controller_readback_sha256
        ):
            raise ValueError("runtime batch TrialBrief readback digest differs")
        receipt = TrialBriefAdmissionReceipt(
            trial_uid=str(row.trial_uid),
            ack_command_seq=ack.ack_command_seq,
            publication_uid=str(row.trial_brief_publication_uid),
            document_sha256=canonical_sha256(published),
            path=path.resolve(),
            file_sha256=hashlib.sha256(encoded).hexdigest(),
            optimizer_eligible=bool(row.optimizer_eligible),
        )
        if receipt.document_sha256 != row.trial_brief_document_sha256:
            raise ValueError("runtime batch TrialBrief document digest differs")
        receipts[receipt.trial_uid] = receipt
    return receipts


def prepare_batch_attempt_context(
    *,
    plan: CandidateBatchPlan,
    selected_candidate: ForceCandidate,
    profile: ExecutionProfile,
    overlay_resolver: Callable[[ForceCandidate], Mapping[str, Any]],
    campaign_uid: str,
    experiment_fingerprint: str,
    launch_fingerprint: str,
    controller_readback_fingerprint: str,
    authorization_ref_sha256: str,
    stopping_bound_fingerprint: str | None,
    plant_epoch: int,
    campaign_root: Path,
    campaign_home_pose: tuple[float, ...],
) -> BatchAttemptContext:
    _sha256("experiment_fingerprint", experiment_fingerprint)
    _sha256("launch_fingerprint", launch_fingerprint)
    _sha256("controller_readback_fingerprint", controller_readback_fingerprint)
    _sha256("authorization_ref_sha256", authorization_ref_sha256)
    if stopping_bound_fingerprint is not None:
        _sha256("stopping_bound_fingerprint", stopping_bound_fingerprint)
    matches = [
        (batch_index, row_index)
        for batch_index, batch in enumerate(plan.batches, start=1)
        for row_index, candidate in enumerate(batch, start=1)
        if candidate.candidate_uid == selected_candidate.candidate_uid
    ]
    if len(matches) != 1:
        raise ValueError("selected candidate must occur once in the exact batch plan")
    batch_index, row_index = matches[0]
    candidates = plan.batches[batch_index - 1]
    if len(candidates) != 10:
        raise ValueError("runtime BatchIdentity requires an exact 10-row batch")
    rows = []
    for index, candidate in enumerate(candidates, start=1):
        overlay = dict(overlay_resolver(candidate))
        control_candidate = {
            name: overlay[name]
            for name in (
                "force_p_gain",
                "force_i_gain",
                "force_damping",
                "orientation_ko",
            )
        }
        rows.append(BatchRow(index, control_candidate, overlay))
    near_pose = (
        *STEP5D_V3_PHYSICAL_PRIOR.precontact_xyz_m,
        *STEP5D_V3_PHYSICAL_PRIOR.precontact_rotvec_rad,
    )
    identity = BatchIdentity(
        campaign_uid=campaign_uid,
        experiment_fingerprint=experiment_fingerprint,
        launch_fingerprint=launch_fingerprint,
        adapter_fingerprint=stage_autotune_adapter_fingerprint(),
        physical_prior_fingerprint=STEP5D_V3_PHYSICAL_PRIOR.fingerprint,
        safety_envelope_fingerprint=moving_sphere_safety_envelope_fingerprint(
            STEP5D_V3_PHYSICAL_PRIOR.fingerprint,
            stopping_bound_fingerprint=stopping_bound_fingerprint,
        ),
        return_policy_fingerprint=return_policy_fingerprint(
            near_ready_pose=near_pose,
            campaign_home_pose=campaign_home_pose,
            prior=STEP5D_V3_PHYSICAL_PRIOR,
        ),
        controller_readback_fingerprint=controller_readback_fingerprint,
        authorization_ref_sha256=authorization_ref_sha256,
        plant_epoch=plant_epoch,
        rows=tuple(rows),
    )
    batch_root = (
        campaign_root.resolve() / "runtime_batches" / identity.batch_uid
    )
    if batch_root.exists():
        journal = BatchJournal.open(batch_root)
        if journal.identity().document() != identity.document():
            raise ValueError("existing runtime batch identity differs")
    else:
        journal = BatchJournal.create(batch_root, identity)
    state = journal.state()
    if state.next_row_index != row_index:
        raise ValueError("selected candidate is not the exact next incomplete batch row")
    selected_state = state.rows[row_index - 1]
    if (
        selected_state.fate is BatchFate.ATTEMPTED_INCOMPLETE
        and (
            selected_state.immutable_bundle_sha256 is not None
            or selected_state.ack_receipt_sha256 is not None
        )
    ):
        raise ValueError(
            "durable post-execution batch row requires phase-specific reconciliation"
        )
    reference = return_reference(
        identity,
        row_index,
        near_ready_pose=near_pose,
        campaign_home_pose=campaign_home_pose,
    )
    return BatchAttemptContext(
        identity=identity,
        journal=journal,
        row_index=row_index,
        reference=reference,
        trial_brief_root=(campaign_root.resolve() / "trial_briefs"),
        retrying_incomplete=(
            selected_state.fate is BatchFate.ATTEMPTED_INCOMPLETE
            and selected_state.immutable_bundle_sha256 is None
            and selected_state.ack_receipt_sha256 is None
        ),
    )


def next_runtime_batch_candidate(
    *,
    plan: CandidateBatchPlan,
    campaign_root: Path,
) -> ForceCandidate | None:
    """Select only the exact next non-ACK-completed row on resume."""

    if len(plan.batches) != 1 or len(plan.batches[0]) != 10:
        raise ValueError("production runtime requires one exact ten-row batch plan")
    candidates = plan.batches[0]
    batches_root = campaign_root.resolve() / "runtime_batches"
    if not batches_root.exists():
        return candidates[0]
    if batches_root.is_symlink() or not batches_root.is_dir():
        raise ValueError("runtime batch root is not a safe directory")
    roots = tuple(
        path
        for path in batches_root.iterdir()
        if path.is_dir() and not path.is_symlink()
    )
    if len(roots) != 1:
        raise ValueError("campaign must contain exactly one runtime BatchIdentity")
    journal = BatchJournal.open(roots[0])
    identity = journal.identity()
    planned_values = tuple(
        (
            candidate.force_p_gain,
            candidate.force_i_gain,
            candidate.force_damping,
        )
        for candidate in candidates
    )
    identity_values = tuple(
        (
            row.control_candidate["force_p_gain"],
            row.control_candidate["force_i_gain"],
            row.control_candidate["force_damping"],
        )
        for row in identity.rows
    )
    if planned_values != identity_values:
        raise ValueError("runtime BatchIdentity candidates differ from the durable plan")
    state = journal.state()
    if state.unpublished_trial_brief_row_indices:
        raise ValueError(
            "ACK-completed batch row requires TrialBrief recovery before selection"
        )
    if state.next_row_index is None:
        journal.finalize()
        if journal.verified_exit_code() != 0:
            raise ValueError("durable runtime BatchResult exit code differs")
        return None
    return candidates[state.next_row_index - 1]


def runtime_batch_verified_complete(*, campaign_root: Path) -> bool:
    batches_root = campaign_root.resolve() / "runtime_batches"
    if not batches_root.is_dir() or batches_root.is_symlink():
        return False
    roots = tuple(
        path
        for path in batches_root.iterdir()
        if path.is_dir() and not path.is_symlink()
    )
    if len(roots) != 1:
        return False
    journal = BatchJournal.open(roots[0])
    return bool(
        journal.state().complete
        and journal.state().result_published
        and journal.verified_exit_code() == 0
    )
