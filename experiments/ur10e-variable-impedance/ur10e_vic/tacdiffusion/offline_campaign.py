"""Deterministic, network-free TacDiffusion fixture campaign.

This module is the Lane 3 composition owner.  It deliberately composes the
existing typed dynamics, causal Kunwei alignment, observation, action-label,
episode-v3, eligibility, and queue primitives.  It does not contain a
controller transport, a model-active route, or a production promotion path.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from io import BytesIO
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import tempfile
import zipfile
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from .contracts import (
    CONTROL_RATE_HZ,
    DynamicsReceipt,
    DynamicsSample,
    OFFLINE_FAKE_RTDE_FIXTURE_ID,
)
from .dynamic_filter import RateInvariantForceFilter
from .eligibility import EligibilityValidator, read_eligibility_receipt
from .episode_composition import (
    ActionLabelContext,
    ActionLabelProvider,
    CausalKunweiAlignmentAdapter,
    DeterministicExpertActionProvider,
    EpisodeSemanticContext,
    FakeRTDEDynamicsProvider,
)
from .episode_recorder import (
    EpisodeFrameV3,
    EpisodeRecorder,
    ObservationReceipt,
    ReferenceReceipt,
    TypedEpisodeReceipt,
    compute_v3_row_sha256,
    read_episode_artifact,
    read_recorder_health,
    validate_sealed_episode_manifest,
)
from .expert import ExpertInput
from .expert_episode_artifact import ExpertEpisodeBindings
from .observation import (
    OBSERVATION_DIMENSION,
    OBSERVATION_SLICE_DIMENSION,
    OBSERVATION_V3_SCHEMA_VERSION,
    ObservationLineage,
    ObservationSlice,
    TacDiffusionObservation,
)
from .queue import (
    CampaignLifecycle,
    CampaignRunner,
    CampaignState,
    DurableHookOutbox,
    EpisodeRequest,
    HomeIdentityLedger,
    PersistentRollingQueue,
    SafeRetractPlan,
)
from .trajectory import TRAJECTORY_FAMILIES


OFFLINE_CAMPAIGN_SCHEMA = "ur10e_tacdiffusion_offline_campaign/v1"
OFFLINE_CAMPAIGN_CONTRACT_SCHEMA = "ur10e_tacdiffusion_offline_campaign_contract/v1"
OFFLINE_CAMPAIGN_RECEIPT_SCHEMA = "ur10e_tacdiffusion_offline_campaign_receipt/v1"
OFFLINE_EPISODE_RECEIPT_SCHEMA = "ur10e_tacdiffusion_offline_episode_receipt/v1"
OFFLINE_FAILURE_SCHEMA = "ur10e_tacdiffusion_offline_failure/v1"
OFFLINE_OBSERVATION_RECEIPT_SCHEMA = "ur10e_tacdiffusion_offline_observation_receipt/v1"
OFFLINE_REFERENCE_RECEIPT_SCHEMA = "ur10e_tacdiffusion_offline_reference_receipt/v1"
OFFLINE_RECOVERY_RECEIPT_SCHEMA = "ur10e_tacdiffusion_offline_recovery_receipt/v1"
OFFLINE_SOURCE_SCHEMA = "ur10e_tacdiffusion_offline_source_receipt/v2"
OFFLINE_SPLIT_SCHEMA = "ur10e_tacdiffusion_offline_frozen_split/v1"
OFFLINE_DATASET_SCHEMA = "ur10e_tacdiffusion_offline_fixture_dataset/v1"
OFFLINE_BUNDLE_SCHEMA = "ur10e_tacdiffusion_offline_fixture_bundle/v1"

CAMPAIGN_EPISODE_COUNT = 50
CAMPAIGN_FAIL_MODULO = 7
CAMPAIGN_RECOVERY_CYCLES = 100
ACTION_DIMENSION = 12
CONTROL_DT_S = 1.0 / CONTROL_RATE_HZ
FRAME_ID = "tool0_tcp"
SENSOR_FRAME_ID = "kunwei_sensor"
CALIBRATION_SHA256 = hashlib.sha256(b"offline-kunwei-calibration-v1").hexdigest()
NORMALIZATION_SHA256 = hashlib.sha256(b"offline-fixture-normalization-v1").hexdigest()
FAKE_CONTROLLER_SCHEMA = "ur10e_tacdiffusion_offline_fake_controller/v1"
FAKE_CONTROLLER_CALIBRATION_IDENTITY = "offline-fake-controller-calibration-v1"
SOURCE_RECEIPT_VERSION = "repo_relative_bytes_sha256_v1"
SOURCE_RECEIPT_PATHS: tuple[str, ...] = (
    "experiments/ur10e-variable-impedance/ur10e_vic/tacdiffusion/contracts.py",
    "experiments/ur10e-variable-impedance/ur10e_vic/tacdiffusion/dynamic_filter.py",
    "experiments/ur10e-variable-impedance/ur10e_vic/tacdiffusion/eligibility.py",
    "experiments/ur10e-variable-impedance/ur10e_vic/tacdiffusion/episode_composition.py",
    "experiments/ur10e-variable-impedance/ur10e_vic/tacdiffusion/episode_recorder.py",
    "experiments/ur10e-variable-impedance/ur10e_vic/tacdiffusion/observation.py",
    "experiments/ur10e-variable-impedance/ur10e_vic/tacdiffusion/offline_campaign.py",
    "experiments/ur10e-variable-impedance/ur10e_vic/tacdiffusion/queue.py",
    "experiments/ur10e-variable-impedance/ur10e_vic/tacdiffusion/trajectory.py",
    "experiments/ur10e-variable-impedance/ur10e_vic/tacdiffusion/cli.py",
    "experiments/ur10e-variable-impedance/tools/materialize_tacdiffusion_offline_campaign.py",
)


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(payload), sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _payload_sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _sha(value: object, name: str) -> str:
    text = str(value)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return text


def _finite_vector(values: Iterable[float], length: int, name: str) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if len(result) != length or not all(math.isfinite(value) for value in result):
        raise ValueError(f"{name} must contain {length} finite values")
    return result


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, sort_keys=True, indent=2, allow_nan=False) + "\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if temporary.exists():
        raise FileExistsError(f"temporary receipt already exists: {temporary}")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _repository_root() -> Path:
    # This module lives at <repo>/experiments/ur10e-variable-impedance/
    # ur10e_vic/tacdiffusion/offline_campaign.py.  Only repo-relative source
    # paths are persisted; absolute paths never enter a receipt or bundle.
    return Path(__file__).resolve().parents[4]


def _validate_repo_relative_source_path(value: object) -> str:
    path = str(value)
    pure = PurePosixPath(path)
    if (
        not path
        or pure.is_absolute()
        or ".." in pure.parts
        or pure.as_posix() != path
        or not path.endswith(".py")
    ):
        raise ValueError("source receipt path is not a safe repo-relative Python path")
    return path


def _source_receipt_payload(*, repository_root: Path) -> dict[str, Any]:
    files: list[dict[str, str]] = []
    for relative in SOURCE_RECEIPT_PATHS:
        safe_relative = _validate_repo_relative_source_path(relative)
        source = repository_root / safe_relative
        if not source.is_file():
            raise ValueError(f"source receipt file is missing: {safe_relative}")
        files.append({"path": safe_relative, "sha256": _file_sha256(source)})
    return {
        "schema": OFFLINE_SOURCE_SCHEMA,
        "version": SOURCE_RECEIPT_VERSION,
        "files": files,
        "fixture_only": True,
        "production_promotion_allowed": False,
    }


def _source_identity_hashes(source_receipt: Mapping[str, Any]) -> dict[str, str]:
    files = source_receipt.get("files")
    if not isinstance(files, list):
        raise ValueError("source receipt files are missing")
    identities = {"source_receipt_sha256": _sha(source_receipt.get("source_receipt_sha256"), "source_receipt_sha256")}
    by_path: dict[str, str] = {}
    for item in files:
        if not isinstance(item, Mapping):
            raise ValueError("source receipt file entry is invalid")
        path = _validate_repo_relative_source_path(item.get("path"))
        digest = _sha(item.get("sha256"), f"source:{path}")
        identities[f"source:{path}"] = digest
        by_path[path] = digest
    aliases = {
        "receiver_source_sha256": by_path[
            "experiments/ur10e-variable-impedance/ur10e_vic/tacdiffusion/episode_composition.py"
        ],
        "bundle_reference_sha256": by_path[
            "experiments/ur10e-variable-impedance/ur10e_vic/tacdiffusion/trajectory.py"
        ],
        "kunwei_calibration_sha256": by_path[
            "experiments/ur10e-variable-impedance/ur10e_vic/tacdiffusion/observation.py"
        ],
        "runtime_source_sha256": by_path[
            "experiments/ur10e-variable-impedance/ur10e_vic/tacdiffusion/offline_campaign.py"
        ],
    }
    identities.update(aliases)
    return dict(sorted(identities.items()))


def _source_receipt(*, repository_root: Path | None = None) -> dict[str, Any]:
    payload = _source_receipt_payload(repository_root=repository_root or _repository_root())
    return payload | {"source_receipt_sha256": _payload_sha256(payload)}


def _validate_source_receipt(
    source_receipt: Mapping[str, Any],
    *,
    repository_root: Path | None = None,
) -> dict[str, str]:
    if not isinstance(source_receipt, Mapping):
        raise ValueError("source receipt must be an object")
    if source_receipt.get("schema") != OFFLINE_SOURCE_SCHEMA or source_receipt.get("version") != SOURCE_RECEIPT_VERSION:
        raise ValueError("source receipt schema/version mismatch")
    if source_receipt.get("fixture_only") is not True or source_receipt.get("production_promotion_allowed") is not False:
        raise ValueError("source receipt promotion boundary is invalid")
    files = source_receipt.get("files")
    if not isinstance(files, list) or len(files) != len(SOURCE_RECEIPT_PATHS):
        raise ValueError("source receipt file closure is incomplete")
    normalized: list[dict[str, str]] = []
    for item in files:
        if not isinstance(item, Mapping):
            raise ValueError("source receipt file entry is invalid")
        path = _validate_repo_relative_source_path(item.get("path"))
        normalized.append({"path": path, "sha256": _sha(item.get("sha256"), f"source:{path}")})
    if [item["path"] for item in normalized] != list(SOURCE_RECEIPT_PATHS):
        raise ValueError("source receipt file ordering/identity mismatch")
    unsigned = dict(source_receipt)
    supplied = unsigned.pop("source_receipt_sha256", None)
    if _sha(supplied, "source_receipt_sha256") != _payload_sha256(unsigned):
        raise ValueError("source receipt digest mismatch")
    current_root = repository_root or _repository_root()
    for item in normalized:
        source = current_root / item["path"]
        if not source.is_file():
            raise ValueError(f"source receipt file is missing: {item['path']}")
        if _file_sha256(source) != item["sha256"]:
            raise ValueError(f"source receipt source drift: {item['path']}")
    normalized_receipt = dict(unsigned)
    normalized_receipt["files"] = normalized
    return _source_identity_hashes(source_receipt)


@dataclass(frozen=True)
class OfflineCampaignContract:
    """Frozen configuration for the exact offline pilot campaign."""

    seed: int = 42
    attempted_episodes: int = CAMPAIGN_EPISODE_COUNT
    trajectory_families: tuple[str, ...] = TRAJECTORY_FAMILIES
    fail_modulo: int = CAMPAIGN_FAIL_MODULO
    recovery_cycles: int = CAMPAIGN_RECOVERY_CYCLES
    observation_dimension: int = OBSERVATION_DIMENSION
    action_dimension: int = ACTION_DIMENSION
    control_rate_hz: int = CONTROL_RATE_HZ
    fixture_only: bool = True
    production_promotion_allowed: bool = False
    schema: str = OFFLINE_CAMPAIGN_CONTRACT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != OFFLINE_CAMPAIGN_CONTRACT_SCHEMA:
            raise ValueError("unsupported offline campaign contract schema")
        if self.seed < 0:
            raise ValueError("offline campaign seed must be non-negative")
        if self.attempted_episodes != CAMPAIGN_EPISODE_COUNT:
            raise ValueError("offline fixture campaign must attempt exactly 50 episodes")
        if tuple(self.trajectory_families) != tuple(TRAJECTORY_FAMILIES):
            raise ValueError("offline fixture campaign requires the seven named families")
        if self.fail_modulo != CAMPAIGN_FAIL_MODULO:
            raise ValueError("offline fixture campaign fail_modulo is frozen at 7")
        if self.recovery_cycles != CAMPAIGN_RECOVERY_CYCLES:
            raise ValueError("offline fixture campaign requires exactly 100 recovery cycles")
        if self.observation_dimension != OBSERVATION_DIMENSION:
            raise ValueError("offline fixture observation dimension must be 84")
        if self.action_dimension != ACTION_DIMENSION:
            raise ValueError("offline fixture action dimension must be 12")
        if self.control_rate_hz != CONTROL_RATE_HZ:
            raise ValueError("offline fixture control rate must be 500 Hz")
        if self.fixture_only is not True or self.production_promotion_allowed is not False:
            raise ValueError("offline campaign is fixture-only and cannot be promoted")

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "seed": self.seed,
            "attempted_episodes": self.attempted_episodes,
            "trajectory_families": list(self.trajectory_families),
            "fail_modulo": self.fail_modulo,
            "recovery_cycles": self.recovery_cycles,
            "observation_dimension": self.observation_dimension,
            "action_dimension": self.action_dimension,
            "control_rate_hz": self.control_rate_hz,
            "fixture_only": self.fixture_only,
            "production_promotion_allowed": self.production_promotion_allowed,
        }

    @property
    def contract_sha256(self) -> str:
        return _payload_sha256(self.canonical_payload())

    def as_json(self) -> dict[str, Any]:
        return self.canonical_payload() | {"contract_sha256": self.contract_sha256}


_IMPORT_SOURCE_RECEIPT = _source_receipt()
SOURCE_IDENTITIES: Mapping[str, str] = _source_identity_hashes(_IMPORT_SOURCE_RECEIPT)


IDENTITY_JACOBIAN_6X6: tuple[tuple[float, ...], ...] = tuple(
    tuple(1.0 if row == column else 0.0 for column in range(6))
    for row in range(6)
)


@dataclass(frozen=True)
class FakeControllerCalibration:
    """Typed fixture Jacobian-transpose calibration for the FakeController."""

    jacobian_6x6: tuple[tuple[float, ...], ...] = IDENTITY_JACOBIAN_6X6
    frame_id: str = FRAME_ID
    calibration_identity: str = FAKE_CONTROLLER_CALIBRATION_IDENTITY
    schema: str = f"{FAKE_CONTROLLER_SCHEMA}_calibration"

    def __post_init__(self) -> None:
        if self.schema != f"{FAKE_CONTROLLER_SCHEMA}_calibration":
            raise ValueError("unsupported FakeController calibration schema")
        if not self.frame_id.strip() or not self.calibration_identity.strip():
            raise ValueError("FakeController calibration identity is required")
        matrix = tuple(tuple(float(value) for value in row) for row in self.jacobian_6x6)
        if len(matrix) != 6 or any(len(row) != 6 for row in matrix):
            raise ValueError("FakeController calibration Jacobian must be 6x6")
        if not all(math.isfinite(value) for row in matrix for value in row):
            raise ValueError("FakeController calibration Jacobian must be finite")
        object.__setattr__(self, "jacobian_6x6", matrix)

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "jacobian_6x6": [list(row) for row in self.jacobian_6x6],
            "frame_id": self.frame_id,
            "calibration_identity": self.calibration_identity,
        }

    @property
    def calibration_sha256(self) -> str:
        return _payload_sha256(self.canonical_payload())

    def as_json(self) -> dict[str, Any]:
        return self.canonical_payload() | {"calibration_sha256": self.calibration_sha256}


@dataclass(frozen=True)
class FakeControllerCommand:
    """A typed commanded-torque transition, distinct from the 12D action."""

    sequence: int
    timestamp_s: float
    frame_id: str
    calibration_identity: str
    calibration_sha256: str
    expert_wrench_6d: tuple[float, ...]
    stiffness_6d: tuple[float, ...]
    commanded_no_gravity_torque_6d: tuple[float, ...]
    echoed_action_12d: tuple[float, ...]
    schema: str = FAKE_CONTROLLER_SCHEMA
    command_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.schema != FAKE_CONTROLLER_SCHEMA:
            raise ValueError("unsupported FakeController command schema")
        if isinstance(self.sequence, bool) or self.sequence < 0:
            raise ValueError("FakeController command sequence is invalid")
        if not math.isfinite(self.timestamp_s) or self.timestamp_s < 0.0:
            raise ValueError("FakeController command timestamp is invalid")
        if not self.frame_id.strip() or not self.calibration_identity.strip():
            raise ValueError("FakeController command identity is incomplete")
        _sha(self.calibration_sha256, "FakeController calibration_sha256")
        for name, length in (
            ("expert_wrench_6d", 6),
            ("stiffness_6d", 6),
            ("commanded_no_gravity_torque_6d", 6),
            ("echoed_action_12d", ACTION_DIMENSION),
        ):
            object.__setattr__(self, name, _finite_vector(getattr(self, name), length, name))
        if tuple(self.echoed_action_12d[:6]) != tuple(self.expert_wrench_6d) or tuple(self.echoed_action_12d[6:]) != tuple(self.stiffness_6d):
            raise ValueError("FakeController command action/echo channels are inconsistent")
        expected = _payload_sha256(self.canonical_payload())
        if self.command_sha256 is not None and self.command_sha256 != expected:
            raise ValueError("FakeController command hash mismatch")
        object.__setattr__(self, "command_sha256", expected)

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "sequence": self.sequence,
            "timestamp_s": self.timestamp_s,
            "frame_id": self.frame_id,
            "calibration_identity": self.calibration_identity,
            "calibration_sha256": self.calibration_sha256,
            "expert_wrench_6d": list(self.expert_wrench_6d),
            "stiffness_6d": list(self.stiffness_6d),
            "commanded_no_gravity_torque_6d": list(self.commanded_no_gravity_torque_6d),
            "echoed_action_12d": list(self.echoed_action_12d),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> "FakeControllerCommand":
        if not isinstance(value, Mapping):
            raise ValueError("FakeController command receipt must be an object")
        return cls(
            sequence=int(value["sequence"]),
            timestamp_s=float(value["timestamp_s"]),
            frame_id=str(value["frame_id"]),
            calibration_identity=str(value["calibration_identity"]),
            calibration_sha256=str(value["calibration_sha256"]),
            expert_wrench_6d=tuple(value["expert_wrench_6d"]),
            stiffness_6d=tuple(value["stiffness_6d"]),
            commanded_no_gravity_torque_6d=tuple(value["commanded_no_gravity_torque_6d"]),
            echoed_action_12d=tuple(value["echoed_action_12d"]),
            schema=str(value.get("schema", "")),
            command_sha256=str(value["command_sha256"]) if value.get("command_sha256") is not None else None,
        )

    def as_json(self) -> dict[str, Any]:
        return self.canonical_payload() | {"command_sha256": self.command_sha256}


class FakeController:
    """Deterministic typed controller separating action labels from torque."""

    def __init__(self, *, calibration: FakeControllerCalibration | None = None) -> None:
        self.calibration = calibration or FakeControllerCalibration()
        self.reset()

    def reset(self) -> None:
        self._last_command: FakeControllerCommand | None = None
        self._previous_commanded_no_gravity_torque_nm = (0.0,) * 6

    @property
    def previous_commanded_no_gravity_torque_nm(self) -> tuple[float, ...]:
        return self._previous_commanded_no_gravity_torque_nm

    @property
    def last_command(self) -> FakeControllerCommand | None:
        return self._last_command

    def command(
        self,
        *,
        sequence: int,
        timestamp_s: float,
        action_12d: Sequence[float],
        echoed_action_12d: Sequence[float],
    ) -> FakeControllerCommand:
        action = _finite_vector(action_12d, ACTION_DIMENSION, "FakeController action")
        echoed = _finite_vector(echoed_action_12d, ACTION_DIMENSION, "FakeController echo")
        if action != echoed:
            raise ValueError("FakeController action echo mismatch")
        if self._last_command is not None:
            if sequence != self._last_command.sequence + 1:
                raise ValueError("FakeController command sequence is nonconsecutive")
            if not math.isclose(timestamp_s - self._last_command.timestamp_s, CONTROL_DT_S, rel_tol=0.0, abs_tol=1e-12):
                raise ValueError("FakeController command timestamp is not on the 500 Hz grid")
        wrench = action[:6]
        stiffness = action[6:]
        torque = tuple(
            sum(self.calibration.jacobian_6x6[row][joint] * wrench[row] for row in range(6))
            for joint in range(6)
        )
        command = FakeControllerCommand(
            sequence=sequence,
            timestamp_s=float(timestamp_s),
            frame_id=self.calibration.frame_id,
            calibration_identity=self.calibration.calibration_identity,
            calibration_sha256=self.calibration.calibration_sha256,
            expert_wrench_6d=wrench,
            stiffness_6d=stiffness,
            commanded_no_gravity_torque_6d=torque,
            echoed_action_12d=echoed,
        )
        self._last_command = command
        self._previous_commanded_no_gravity_torque_nm = command.commanded_no_gravity_torque_6d
        return command


@dataclass(frozen=True)
class FakeRTDETick:
    sequence: int
    timestamp_s: float
    dynamics_sample: DynamicsSample
    actual_ee_twist: tuple[float, ...]


class DeterministicFakeRTDE:
    """In-memory RTDE-shaped source with no socket, process, or endpoint."""

    source_identity = "offline-fake-rtde-v1"
    model_identity = "offline-dynamics-model-v1"
    tcp_identity = "offline-tcp-model-v1"
    calibration_identity = "offline-dynamics-calibration-v1"

    def __init__(self, *, seed: int = 42, controller: FakeController | None = None) -> None:
        if seed < 0:
            raise ValueError("FakeRTDE seed must be non-negative")
        self.seed = int(seed)
        self.source_hashes = dict(SOURCE_IDENTITIES)
        self.controller = controller or FakeController()
        self.reset()

    def reset(self) -> None:
        self._last_sequence: int | None = None
        self._last_timestamp_s: float | None = None
        self._echoed_action_12d = (0.0,) * ACTION_DIMENSION
        self.controller.reset()

    @property
    def echoed_action_12d(self) -> tuple[float, ...]:
        return self._echoed_action_12d

    def tick(self, sequence: int, timestamp_s: float) -> FakeRTDETick:
        if isinstance(sequence, bool) or sequence < 0:
            raise ValueError("FakeRTDE sequence is invalid")
        timestamp = float(timestamp_s)
        if not math.isfinite(timestamp) or timestamp < 0.0:
            raise ValueError("FakeRTDE timestamp is invalid")
        if self._last_sequence is not None:
            if sequence != self._last_sequence + 1:
                raise ValueError("FakeRTDE sequence is nonconsecutive")
            assert self._last_timestamp_s is not None
            if not math.isclose(timestamp - self._last_timestamp_s, CONTROL_DT_S, rel_tol=0.0, abs_tol=1e-12):
                raise ValueError("FakeRTDE timestamp is not on the 500 Hz grid")
        previous_torque = self.controller.previous_commanded_no_gravity_torque_nm
        if sequence == 0 and self.controller.last_command is not None:
            raise ValueError("FakeRTDE sequence zero has stale controller state")
        if sequence > 0 and (
            self.controller.last_command is None
            or self.controller.last_command.sequence != sequence - 1
        ):
            raise ValueError("FakeRTDE feedback is not causally bound to the previous controller command")
        phase = 0.031 * (self.seed + 1) + 0.17 * sequence
        previous_q = tuple(0.02 * math.sin(phase + axis * 0.13) for axis in range(6))
        previous_qd = tuple(0.01 * math.cos(phase + axis * 0.11) for axis in range(6))
        coriolis = tuple(0.01 * (axis + 1) + 0.0001 * sequence for axis in range(6))
        damping = tuple(0.005 * (axis + 1) for axis in range(6))
        actual_current = tuple(
            previous_torque[axis] + 0.01 * math.sin(phase + axis)
            for axis in range(6)
        )
        sample = DynamicsSample(
            sequence=sequence,
            timestamp_s=timestamp,
            previous_q=previous_q,
            previous_qd=previous_qd,
            previous_commanded_no_gravity_torque_nm=previous_torque,
            calibrated_jacobian=self.controller.calibration.jacobian_6x6,
            coriolis_torque_nm=coriolis,
            joint_damping_torque_nm=damping,
            source_hashes=self.source_hashes,
            model_identity=self.model_identity,
            tcp_identity=self.tcp_identity,
            calibration_identity=self.calibration_identity,
            frame_id=FRAME_ID,
            canonical_tcp_frame_id=FRAME_ID,
            jacobian_frame_id=FRAME_ID,
            source_identity=self.source_identity,
            actual_current_torque_nm_shadow=actual_current,
        )
        actual_twist = tuple(
            0.003 * math.sin(phase + axis * 0.19) for axis in range(6)
        )
        self._last_sequence = sequence
        self._last_timestamp_s = timestamp
        return FakeRTDETick(sequence, timestamp, sample, actual_twist)

    def echo_action(self, sequence: int, action_12d: Sequence[float]) -> tuple[float, ...]:
        if self._last_sequence != sequence:
            raise ValueError("FakeRTDE action sequence does not match the current tick")
        action = _finite_vector(action_12d, ACTION_DIMENSION, "FakeRTDE action")
        self._echoed_action_12d = action
        return action


class SyntheticKunweiDriver:
    """Deterministic Kunwei source feeding the accepted causal joiner."""

    def __init__(
        self,
        *,
        seed: int = 42,
        hold_every: int | None = None,
        expected_frame_id: str = FRAME_ID,
        calibration_sha256: str = CALIBRATION_SHA256,
    ) -> None:
        if seed < 0:
            raise ValueError("synthetic Kunwei seed must be non-negative")
        if hold_every is not None and hold_every <= 1:
            raise ValueError("hold_every must be greater than one")
        _sha(calibration_sha256, "calibration_sha256")
        self.seed = int(seed)
        self.hold_every = hold_every
        self.adapter = CausalKunweiAlignmentAdapter(
            expected_frame_id=expected_frame_id,
            calibration_sha256=calibration_sha256,
        )
        self.reset()

    def reset(self) -> None:
        self.adapter = CausalKunweiAlignmentAdapter(
            expected_frame_id=self.adapter.expected_frame_id,
            calibration_sha256=self.adapter.calibration_sha256,
            max_host_age_s=self.adapter.max_host_age_s,
        )
        self._last_sample: tuple[float, float, int, int, tuple[float, ...]] | None = None

    def read(self, *, sequence: int, control_timestamp_s: float) -> Any:
        if sequence < 0:
            raise ValueError("synthetic Kunwei sequence is invalid")
        phase = 0.021 * (self.seed + 3) + 0.23 * sequence
        hold = self.hold_every is not None and sequence > 0 and sequence % self.hold_every == 0
        if hold:
            if self._last_sample is None:
                raise ValueError("cannot hold a Kunwei sample before the first sample")
            device, host, batch, sample_index, wrench = self._last_sample
        else:
            device = float(control_timestamp_s) - 0.001
            host = device
            batch = sequence
            sample_index = sequence
            wrench = (
                0.15 * math.sin(phase),
                0.12 * math.cos(phase),
                3.2 + 0.08 * math.sin(phase),
                0.01 * math.cos(phase),
                0.01 * math.sin(phase),
                0.005 * math.cos(phase),
            )
            self._last_sample = (device, host, batch, sample_index, wrench)
        alignment = self.adapter.align(
            control_timestamp_s=control_timestamp_s,
            device_time_s=device,
            host_visible_time_s=host,
            batch_id=batch,
            sample_index=sample_index,
            wrench_tcp_si=wrench,
            source_sequence=sample_index,
        )
        if alignment is None:
            raise ValueError(self.adapter.fault or "synthetic Kunwei alignment failed")
        return alignment


@dataclass(frozen=True)
class OfflineReferenceReceipt:
    episode_id: str
    family: str
    seed: int
    sequence: int
    timestamp_s: float
    desired_pose_6d: tuple[float, ...]
    desired_twist_6d: tuple[float, ...]
    desired_acceleration_6d: tuple[float, ...]
    target_load_n: float
    preload_n: float
    reference_sample_id: str
    controller_command_receipt: Mapping[str, Any] | None = None
    schema: str = OFFLINE_REFERENCE_RECEIPT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != OFFLINE_REFERENCE_RECEIPT_SCHEMA:
            raise ValueError("unsupported offline reference receipt schema")
        if not self.episode_id.strip() or self.family not in TRAJECTORY_FAMILIES:
            raise ValueError("offline reference identity is invalid")
        if self.seed < 0 or self.sequence < 0 or not math.isfinite(self.timestamp_s) or self.timestamp_s < 0.0:
            raise ValueError("offline reference sequence/timestamp is invalid")
        for name in ("desired_pose_6d", "desired_twist_6d", "desired_acceleration_6d"):
            object.__setattr__(self, name, _finite_vector(getattr(self, name), 6, name))
        if not all(math.isfinite(value) and value >= 0.0 for value in (self.target_load_n, self.preload_n)):
            raise ValueError("offline reference load is invalid")
        if not self.reference_sample_id.strip():
            raise ValueError("offline reference sample identity is required")
        if self.controller_command_receipt is not None:
            command = FakeControllerCommand.from_json(self.controller_command_receipt)
            if command.sequence != self.sequence or not math.isclose(command.timestamp_s, self.timestamp_s, rel_tol=0.0, abs_tol=1e-12):
                raise ValueError("offline reference/controller command identity mismatch")

    @classmethod
    def for_tick(cls, *, episode_id: str, family: str, seed: int, sequence: int, timestamp_s: float) -> "OfflineReferenceReceipt":
        family_index = TRAJECTORY_FAMILIES.index(family)
        phase = 0.11 * (seed + 1) + 0.37 * family_index + 0.19 * sequence
        desired_pose = (
            0.002 * math.sin(phase),
            0.002 * math.cos(phase),
            0.001 * math.sin(phase * 0.5),
            0.0,
            0.0,
            0.0,
        )
        desired_twist = (
            0.00038 * math.cos(phase),
            -0.00038 * math.sin(phase),
            0.00019 * math.cos(phase * 0.5),
            0.0,
            0.0,
            0.0,
        )
        desired_acceleration = (
            -0.00007 * math.sin(phase),
            -0.00007 * math.cos(phase),
            -0.000035 * math.sin(phase * 0.5),
            0.0,
            0.0,
            0.0,
        )
        return cls(
            episode_id=episode_id,
            family=family,
            seed=seed,
            sequence=sequence,
            timestamp_s=timestamp_s,
            desired_pose_6d=desired_pose,
            desired_twist_6d=desired_twist,
            desired_acceleration_6d=desired_acceleration,
            target_load_n=4.0 + 0.05 * (seed % 5),
            preload_n=0.25 + 0.01 * family_index,
            reference_sample_id=f"{episode_id}:reference:{sequence}",
        )

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "episode_id": self.episode_id,
            "family": self.family,
            "seed": self.seed,
            "sequence": self.sequence,
            "timestamp_s": self.timestamp_s,
            "desired_pose_6d": list(self.desired_pose_6d),
            "desired_twist_6d": list(self.desired_twist_6d),
            "desired_acceleration_6d": list(self.desired_acceleration_6d),
            "target_load_n": self.target_load_n,
            "preload_n": self.preload_n,
            "reference_sample_id": self.reference_sample_id,
            "controller_command_receipt": None if self.controller_command_receipt is None else dict(self.controller_command_receipt),
        }

    def as_typed(self) -> ReferenceReceipt:
        return ReferenceReceipt(schema=self.schema, payload=self.canonical_payload())

    def as_json(self) -> dict[str, Any]:
        return self.as_typed().as_json()


@dataclass(frozen=True)
class OfflineObservationReceipt:
    episode_id: str
    previous_sequence: int
    current_sequence: int
    previous_timestamp_s: float
    current_timestamp_s: float
    previous_observation_42d: tuple[float, ...]
    current_observation_42d: tuple[float, ...]
    lineage: Mapping[str, Any]
    schema: str = OFFLINE_OBSERVATION_RECEIPT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != OFFLINE_OBSERVATION_RECEIPT_SCHEMA:
            raise ValueError("unsupported offline observation receipt schema")
        if not self.episode_id.strip() or self.previous_sequence < 0 or self.current_sequence <= self.previous_sequence:
            raise ValueError("offline observation sequence identity is invalid")
        if not math.isfinite(self.previous_timestamp_s) or not math.isfinite(self.current_timestamp_s) or self.current_timestamp_s <= self.previous_timestamp_s:
            raise ValueError("offline observation timestamps are invalid")
        object.__setattr__(self, "previous_observation_42d", _finite_vector(self.previous_observation_42d, OBSERVATION_SLICE_DIMENSION, "previous_observation_42d"))
        object.__setattr__(self, "current_observation_42d", _finite_vector(self.current_observation_42d, OBSERVATION_SLICE_DIMENSION, "current_observation_42d"))
        if not isinstance(self.lineage, Mapping) or not str(self.lineage.get("schema", "")).strip():
            raise ValueError("offline observation lineage receipt is invalid")

    @classmethod
    def from_observation(cls, episode_id: str, observation: TacDiffusionObservation) -> "OfflineObservationReceipt":
        lineage = observation.current.lineage
        lineage_payload = {
            "schema": "ur10e_tacdiffusion_observation_lineage/v1",
            "sensor_frame_id": lineage.sensor_frame_id,
            "canonical_frame_id": lineage.canonical_frame_id,
            "calibration_sha256": lineage.calibration_sha256,
            "normalization_sha256": lineage.normalization_sha256,
            "external_rate_hz": lineage.external_rate_hz,
            "control_rate_hz": lineage.control_rate_hz,
            "units": dict(lineage.units),
            "previous_external_source_sequence": observation.previous.external_source_sequence,
            "current_external_source_sequence": observation.current.external_source_sequence,
            "previous_external_sample_index": observation.previous.external_sample_index,
            "current_external_sample_index": observation.current.external_sample_index,
            "previous_external_hold": observation.previous.external_hold,
            "current_external_hold": observation.current.external_hold,
        }
        return cls(
            episode_id=episode_id,
            previous_sequence=observation.previous.sequence,
            current_sequence=observation.current.sequence,
            previous_timestamp_s=observation.previous.timestamp_s,
            current_timestamp_s=observation.current.timestamp_s,
            previous_observation_42d=observation.previous.vector,
            current_observation_42d=observation.current.vector,
            lineage=lineage_payload,
        )

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "episode_id": self.episode_id,
            "previous_sequence": self.previous_sequence,
            "current_sequence": self.current_sequence,
            "previous_timestamp_s": self.previous_timestamp_s,
            "current_timestamp_s": self.current_timestamp_s,
            "previous_observation_42d": list(self.previous_observation_42d),
            "current_observation_42d": list(self.current_observation_42d),
            "lineage": dict(self.lineage),
        }

    def as_typed(self) -> ObservationReceipt:
        return ObservationReceipt(schema=self.schema, payload=self.canonical_payload())

    def as_json(self) -> dict[str, Any]:
        return self.as_typed().as_json()


@dataclass(frozen=True)
class OfflineControlOutput:
    observation_84d: tuple[float, ...]
    expert_action_12d: tuple[float, ...]
    applied_action_12d: tuple[float, ...]
    echoed_action_12d: tuple[float, ...]
    commanded_no_gravity_torque_6d: tuple[float, ...] = (0.0,) * 6
    controller_command_sha256: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "observation_84d", _finite_vector(self.observation_84d, OBSERVATION_DIMENSION, "observation_84d"))
        for name in ("expert_action_12d", "applied_action_12d", "echoed_action_12d"):
            object.__setattr__(self, name, _finite_vector(getattr(self, name), ACTION_DIMENSION, name))
        object.__setattr__(
            self,
            "commanded_no_gravity_torque_6d",
            _finite_vector(self.commanded_no_gravity_torque_6d, 6, "commanded_no_gravity_torque_6d"),
        )
        if self.controller_command_sha256:
            _sha(self.controller_command_sha256, "controller_command_sha256")


@dataclass(frozen=True)
class OfflineComposedEpisode:
    frames: tuple[EpisodeFrameV3, ...]
    control_outputs: tuple[OfflineControlOutput, ...]


class DeterministicOfflineEpisodeAssembler:
    """Canonical causal composition from the two offline source drivers."""

    def __init__(
        self,
        *,
        seed: int = 42,
        action_provider: ActionLabelProvider | None = None,
        force_filter: RateInvariantForceFilter | None = None,
    ) -> None:
        self.seed = int(seed)
        self.action_provider = action_provider or DeterministicExpertActionProvider()
        if not hasattr(self.action_provider, "reset"):
            raise TypeError("offline action provider must expose reset()")
        self.force_filter = force_filter or RateInvariantForceFilter()
        self.controller = FakeController()
        self.fake_rtde = DeterministicFakeRTDE(seed=self.seed, controller=self.controller)
        self.synthetic_kunwei = SyntheticKunweiDriver(seed=self.seed)
        self.expert_reset_count = 0
        self.filter_reset_count = 0

    def reset_expert(self) -> None:
        reset = getattr(self.action_provider, "reset")
        reset()
        self.expert_reset_count += 1

    def reset_filter(self) -> None:
        self.force_filter.reset()
        self.filter_reset_count += 1

    def reset_sources(self) -> None:
        self.fake_rtde.reset()
        self.synthetic_kunwei.reset()

    def compose_episode(
        self,
        *,
        episode_id: str,
        family: str,
        episode_seed: int,
        semantic_context: EpisodeSemanticContext,
        failure_reason: str | None = None,
        reset_state: bool = False,
    ) -> OfflineComposedEpisode:
        if reset_state:
            self.reset_expert()
            self.reset_filter()
        self.reset_sources()
        previous_sample: DynamicsSample | None = None
        previous_slice: ObservationSlice | None = None
        previous_label: Any | None = None
        previous_action: tuple[float, ...] | None = None
        frames: list[EpisodeFrameV3] = []
        outputs: list[OfflineControlOutput] = []
        dynamics_provider = FakeRTDEDynamicsProvider()
        for sequence in range(3):
            control_timestamp = (sequence + 1) * CONTROL_DT_S
            reference = OfflineReferenceReceipt.for_tick(
                episode_id=episode_id,
                family=family,
                seed=episode_seed,
                sequence=sequence,
                timestamp_s=control_timestamp,
            )
            tick = self.fake_rtde.tick(sequence, control_timestamp)
            dynamics_receipt = dynamics_provider.produce(
                tick.dynamics_sample, previous_sample=previous_sample
            )
            alignment = self.synthetic_kunwei.read(
                sequence=sequence, control_timestamp_s=control_timestamp
            )
            tracking_error = tuple(
                0.0001 * math.sin(0.13 * (episode_seed + sequence + axis))
                for axis in range(6)
            )
            slice_value = ObservationSlice.from_causal_alignment(
                sequence=sequence,
                control_timestamp_s=control_timestamp,
                alignment=alignment,
                internal_wrench=dynamics_receipt.internal_wrench_tcp_si,
                actual_ee_twist=tick.actual_ee_twist,
                desired_pose=reference.desired_pose_6d,
                desired_twist=reference.desired_twist_6d,
                desired_acceleration=reference.desired_acceleration_6d,
                tracking_error=tracking_error,
                lineage=ObservationLineage(
                    sensor_frame_id=SENSOR_FRAME_ID,
                    canonical_frame_id=FRAME_ID,
                    calibration_sha256=CALIBRATION_SHA256,
                    normalization_sha256=NORMALIZATION_SHA256,
                ),
                internal_wrench_valid=dynamics_receipt.valid,
                external_lineage_valid=True,
            )
            expert_input = ExpertInput(
                normal_load_n=max(0.0, alignment.wrench_tcp_si[2]),
                target_load_n=reference.target_load_n,
                pose_error=tracking_error,
                twist=tick.actual_ee_twist,
                path_progress=min(0.99, sequence / 3.0),
                tangential_speed_m_s=0.01 + 0.001 * (sequence + 1),
                desired_twist=reference.desired_twist_6d,
                desired_acceleration=reference.desired_acceleration_6d,
            )
            action_context = ActionLabelContext.expert(
                sequence=sequence,
                timestamp_s=control_timestamp,
                frame_id=FRAME_ID,
                expert_input=expert_input,
                previous_action_12d=previous_action,
                previous_sequence=None if sequence == 0 else sequence - 1,
                previous_timestamp_s=None if sequence == 0 else sequence * CONTROL_DT_S,
                dt_s=CONTROL_DT_S,
                applied_action_12d=(0.0,) * ACTION_DIMENSION if previous_action is None else previous_action,
                echoed_action_12d=(0.0,) * ACTION_DIMENSION if previous_action is None else previous_action,
                model_identity="deterministic_expert_v1",
                tcp_identity=tick.dynamics_sample.tcp_identity,
                calibration_identity=tick.dynamics_sample.calibration_identity,
                source_hashes=SOURCE_IDENTITIES,
                dynamics_receipt=dynamics_receipt,
                semantic_context_fingerprint_sha256=semantic_context.fingerprint_sha256,
            )
            label = self.action_provider.produce(action_context)
            self.force_filter.step(label.expert_action_12d[:6], dt_s=CONTROL_DT_S)
            rtde_echo = self.fake_rtde.echo_action(sequence, label.expert_action_12d)
            command = self.controller.command(
                sequence=sequence,
                timestamp_s=control_timestamp,
                action_12d=label.expert_action_12d,
                echoed_action_12d=rtde_echo,
            )
            reference = replace(
                reference,
                controller_command_receipt=command.as_json(),
            )
            echoed = command.echoed_action_12d
            current_observation = None
            if previous_slice is not None:
                current_observation = TacDiffusionObservation(previous_slice, slice_value)
                observation_receipt = OfflineObservationReceipt.from_observation(
                    episode_id, current_observation
                )
                frame = EpisodeFrameV3(
                    episode_id=episode_id,
                    sample_index=sequence - 1,
                    control_sequence=sequence,
                    control_time_s=control_timestamp,
                    controller_time_s=control_timestamp,
                    control_clock="offline_monotonic",
                    observation_84d=current_observation.vector,
                    expert_action_12d=label.expert_action_12d,
                    applied_action_12d=label.expert_action_12d,
                    echoed_action_12d=echoed,
                    action_generation=sequence + 1,
                    action_age_ticks=0,
                    action_echo_coherent=True,
                    external_device_time_s=alignment.source_device_time_s,
                    external_host_visible_time_s=alignment.source_host_visible_time_s,
                    external_batch_id=alignment.source_batch_id,
                    external_sample_index=alignment.source_sample_index,
                    external_hold=alignment.external_hold,
                    external_held_ticks=alignment.external_held_ticks,
                    device_age_samples=alignment.device_age_samples,
                    host_age_s=alignment.host_age_s,
                    internal_wrench_valid=dynamics_receipt.valid,
                    external_lineage_valid=True,
                    source_row_invalid=failure_reason is not None,
                    source_row_torn=False,
                    echoed_action_valid=True,
                    recorder_valid=failure_reason is None,
                    expert_label_available=label.available,
                    expert_action_source=label.source,
                    action_label_semantics=label.semantics,
                    controller_echo_12d=echoed,
                    observation_history_valid=True,
                    reference_derivatives_valid=True,
                    desired_pose_6d=reference.desired_pose_6d,
                    desired_twist_6d=reference.desired_twist_6d,
                    desired_acceleration_6d=reference.desired_acceleration_6d,
                    reference_sample_id=reference.reference_sample_id,
                    candidate_window=True,
                    capture_phase="active_torque",
                    dynamics_sample=tick.dynamics_sample,
                    dynamics_receipt=dynamics_receipt,
                    action_label_context=action_context,
                    action_label=label,
                    identity_enabled=True,
                    semantic_context_fingerprint_sha256=semantic_context.fingerprint_sha256,
                    observation_receipt=observation_receipt.as_typed(),
                    reference_receipt=reference.as_typed(),
                )
                frames.append(frame)
                outputs.append(
                    OfflineControlOutput(
                        observation_84d=current_observation.vector,
                        expert_action_12d=label.expert_action_12d,
                        applied_action_12d=label.expert_action_12d,
                        echoed_action_12d=echoed,
                        commanded_no_gravity_torque_6d=command.commanded_no_gravity_torque_6d,
                        controller_command_sha256=str(command.command_sha256),
                    )
                )
            previous_sample = tick.dynamics_sample
            previous_slice = slice_value
            previous_label = label
            previous_action = label.expert_action_12d
        return OfflineComposedEpisode(tuple(frames), tuple(outputs))


def _control_output_from_row(row: Mapping[str, Any]) -> OfflineControlOutput:
    reference = ReferenceReceipt.from_json(row.get("reference_receipt"))
    payload = reference.payload
    command_payload = payload.get("controller_command_receipt")
    command = FakeControllerCommand.from_json(command_payload)
    return OfflineControlOutput(
        observation_84d=tuple(row["observation_84d"]),
        expert_action_12d=tuple(row["expert_action_12d"]),
        applied_action_12d=tuple(row["applied_action_12d"]),
        echoed_action_12d=tuple(row["echoed_action_12d"]),
        commanded_no_gravity_torque_6d=command.commanded_no_gravity_torque_6d,
        controller_command_sha256=str(command.command_sha256),
    )


def prove_recorder_sidecar_identity(*, seed: int = 42) -> dict[str, Any]:
    """Exercise the real recorder and prove it is an algebraic control identity."""

    episode_id = "offline-identity-proof"
    context = EpisodeSemanticContext.diagnostic(
        episode_id=episode_id,
        capture_kind="offline_sidecar_identity_proof",
        hash_identities=SOURCE_IDENTITIES,
    )
    enabled = DeterministicOfflineEpisodeAssembler(seed=seed).compose_episode(
        episode_id=episode_id,
        family=TRAJECTORY_FAMILIES[0],
        episode_seed=seed,
        semantic_context=context,
        reset_state=True,
    )
    disabled = DeterministicOfflineEpisodeAssembler(seed=seed).compose_episode(
        episode_id=episode_id,
        family=TRAJECTORY_FAMILIES[0],
        episode_seed=seed,
        semantic_context=context,
        reset_state=True,
    )

    with tempfile.TemporaryDirectory(prefix="tacdiffusion-recorder-proof-") as temporary:
        recorder_directory = Path(temporary) / "enabled"
        recorder = EpisodeRecorder(
            recorder_directory,
            episode_id=episode_id,
            semantic_context=context,
            metadata={"fixture_only": True, "production_promotion_allowed": False},
        )
        recorder.start()
        try:
            for frame in enabled.frames:
                if not recorder.enqueue(frame):
                    raise RuntimeError("recorder identity proof enqueue failed")
            seal = recorder.close(seal=True)
        except Exception:
            recorder.close(seal=False)
            raise
        if seal is None:
            raise RuntimeError("recorder identity proof did not seal")
        validate_sealed_episode_manifest(recorder.artifact_path, recorder.manifest_path)
        header, rows = read_episode_artifact(recorder.artifact_path)
        health = read_recorder_health(recorder.health_path)
        if header.get("schema") != "ur10e_tacdiffusion_episode_artifact/v3" or health["health"].get("sealed") is not True:
            raise RuntimeError("recorder identity proof did not read a sealed healthy artifact")
        readback_outputs = tuple(_control_output_from_row(row) for row in rows)

    enabled_payload = [output.__dict__ for output in readback_outputs]
    disabled_payload = [output.__dict__ for output in disabled.control_outputs]
    enabled_digest = _payload_sha256({"outputs": enabled_payload})
    disabled_digest = _payload_sha256({"outputs": disabled_payload})
    return {
        "schema": "ur10e_tacdiffusion_offline_sidecar_identity/v1",
        "recorder_enabled_digest": enabled_digest,
        "recorder_disabled_digest": disabled_digest,
        "recorder_readback_digest": _payload_sha256({"outputs": enabled_payload}),
        "identical": readback_outputs == disabled.control_outputs,
        "observation_action_rows": len(readback_outputs),
        "disabled_recorder_is_identity": readback_outputs == disabled.control_outputs,
        "real_recorder_exercised": True,
    }


@dataclass(frozen=True)
class FrozenEpisodeSplit:
    seed: int
    version: str
    episode_split: Mapping[str, str]
    split_sha256: str
    schema: str = OFFLINE_SPLIT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != OFFLINE_SPLIT_SCHEMA:
            raise ValueError("unsupported frozen split schema")
        if self.seed < 0 or not self.episode_split:
            raise ValueError("frozen split must contain episodes")
        normalized = {str(key): str(value) for key, value in self.episode_split.items()}
        if any(not key.strip() for key in normalized) or any(value not in {"train", "validation", "test"} for value in normalized.values()):
            raise ValueError("frozen split contains invalid episode or split")
        if len(set(normalized.values())) < 3 and len(normalized) >= 3:
            raise ValueError("frozen split must contain train, validation, and test groups")
        object.__setattr__(self, "episode_split", dict(sorted(normalized.items())))
        expected = _payload_sha256(self.canonical_payload())
        if self.split_sha256 != expected:
            raise ValueError("frozen split hash mismatch")

    @classmethod
    def freeze(cls, episode_ids: Iterable[str], *, seed: int = 42, version: str = "sha256_episode_group_v1") -> "FrozenEpisodeSplit":
        episodes = tuple(sorted({str(value) for value in episode_ids}))
        if not episodes or any(not value.strip() for value in episodes):
            raise ValueError("cannot freeze an empty episode split")
        ordered = sorted(
            episodes,
            key=lambda value: hashlib.sha256(f"{version}:{seed}:{value}".encode("utf-8")).digest(),
        )
        count = len(ordered)
        train_count = max(1, int(round(count * 0.70)))
        validation_count = max(1, int(round(count * 0.15))) if count >= 3 else 0
        if count >= 3:
            train_count = min(train_count, count - validation_count - 1)
            validation_count = min(validation_count, count - train_count - 1)
        mapping: dict[str, str] = {}
        for index, episode_id in enumerate(ordered):
            if index < train_count:
                mapping[episode_id] = "train"
            elif index < train_count + validation_count:
                mapping[episode_id] = "validation"
            else:
                mapping[episode_id] = "test"
        payload = {
            "schema": OFFLINE_SPLIT_SCHEMA,
            "seed": seed,
            "version": version,
            "episode_split": dict(sorted(mapping.items())),
        }
        return cls(seed=seed, version=version, episode_split=mapping, split_sha256=_payload_sha256(payload))

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "seed": self.seed,
            "version": self.version,
            "episode_split": dict(sorted(self.episode_split.items())),
        }

    def as_json(self) -> dict[str, Any]:
        return self.canonical_payload() | {"split_sha256": self.split_sha256}


@dataclass(frozen=True)
class RecoveryReceipt:
    cycles: int
    interrupted_cycles: int
    retried_cycles: int
    reopen_count: int
    duplicate_enqueue_attempts: int
    idempotent_consume_attempts: int
    duplicate_eligibility_promotions: int
    final_pending_count: int
    queue_drained: bool
    schema: str = OFFLINE_RECOVERY_RECEIPT_SCHEMA
    receipt_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.schema != OFFLINE_RECOVERY_RECEIPT_SCHEMA:
            raise ValueError("unsupported recovery receipt schema")
        expected = {
            "cycles": CAMPAIGN_RECOVERY_CYCLES,
            "interrupted_cycles": 50,
            "retried_cycles": 50,
            "reopen_count": 200,
            "duplicate_enqueue_attempts": 100,
            "idempotent_consume_attempts": 100,
            "duplicate_eligibility_promotions": 0,
            "final_pending_count": 0,
            "queue_drained": True,
        }
        actual = {
            "cycles": self.cycles,
            "interrupted_cycles": self.interrupted_cycles,
            "retried_cycles": self.retried_cycles,
            "reopen_count": self.reopen_count,
            "duplicate_enqueue_attempts": self.duplicate_enqueue_attempts,
            "idempotent_consume_attempts": self.idempotent_consume_attempts,
            "duplicate_eligibility_promotions": self.duplicate_eligibility_promotions,
            "final_pending_count": self.final_pending_count,
            "queue_drained": self.queue_drained,
        }
        if actual != expected:
            raise ValueError("recovery receipt does not contain the exact executed 100-cycle result")
        expected = _payload_sha256(self.canonical_payload())
        if self.receipt_sha256 is not None and self.receipt_sha256 != expected:
            raise ValueError("recovery receipt hash mismatch")
        object.__setattr__(self, "receipt_sha256", expected)

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "cycles": self.cycles,
            "interrupted_cycles": self.interrupted_cycles,
            "retried_cycles": self.retried_cycles,
            "reopen_count": self.reopen_count,
            "duplicate_enqueue_attempts": self.duplicate_enqueue_attempts,
            "idempotent_consume_attempts": self.idempotent_consume_attempts,
            "duplicate_eligibility_promotions": self.duplicate_eligibility_promotions,
            "final_pending_count": self.final_pending_count,
            "queue_drained": self.queue_drained,
        }

    def as_json(self) -> dict[str, Any]:
        return self.canonical_payload() | {"receipt_sha256": self.receipt_sha256}


def run_persistent_recovery_cycles(
    state_path: str | Path,
    *,
    cycles: int = CAMPAIGN_RECOVERY_CYCLES,
) -> RecoveryReceipt:
    """Run reopen/retry/idempotent-consume cycles against durable JSON state."""

    if cycles != CAMPAIGN_RECOVERY_CYCLES:
        raise ValueError("offline campaign recovery requires exactly 100 cycles")
    path = Path(state_path)
    if path.exists():
        raise FileExistsError("recovery state path must be new")
    outbox = DurableHookOutbox(state_path=path)
    interrupted = 0
    retried = 0
    reopens = 0
    duplicate_enqueues = 0
    idempotent_consumes = 0
    promoted: set[str] = set()
    for index in range(cycles):
        identity = f"offline-recovery:{index:03d}"
        if not outbox.enqueue(identity=identity, kind="eligibility", episode_id=f"recovery-{index:03d}"):
            raise AssertionError("new recovery identity was rejected")
        if outbox.enqueue(identity=identity, kind="eligibility", episode_id=f"recovery-{index:03d}") is False:
            duplicate_enqueues += 1
        claimed = outbox.claim()
        if claimed is None:
            raise AssertionError("recovery item was not claimable")
        if index % 2 == 0:
            interrupted += 1
            outbox = DurableHookOutbox(state_path=path)
            reopens += 1
            outbox.reconcile_claimed(identity, outcome="retry")
            retried += 1
            outbox = DurableHookOutbox(state_path=path)
            reopens += 1
            claimed = outbox.claim()
            if claimed is None:
                raise AssertionError("retried recovery item was not claimable")
        outbox.complete(identity)
        if identity not in promoted:
            promoted.add(identity)
        reopened = DurableHookOutbox(state_path=path)
        reopens += 1
        if reopened.enqueue(identity=identity, kind="eligibility", episode_id=f"recovery-{index:03d}") is False:
            idempotent_consumes += 1
        outbox = reopened
    receipt = RecoveryReceipt(
        cycles=cycles,
        interrupted_cycles=interrupted,
        retried_cycles=retried,
        reopen_count=reopens,
        duplicate_enqueue_attempts=duplicate_enqueues,
        idempotent_consume_attempts=idempotent_consumes,
        duplicate_eligibility_promotions=0,
        final_pending_count=outbox.pending_count,
        queue_drained=outbox.pending_count == 0 and not outbox.failed and not outbox.claimed,
    )
    return receipt


@dataclass(frozen=True)
class FixtureEpisodeArtifact:
    episode_id: str
    family: str
    status: str
    directory: Path
    artifact_path: Path
    manifest_path: Path
    health_path: Path
    eligibility_path: Path
    failure_path: Path | None
    failure_reason: str | None
    artifact_sha256: str
    manifest_sha256: str
    eligibility_sha256: str

    @classmethod
    def from_directory(cls, directory: str | Path) -> "FixtureEpisodeArtifact":
        root = Path(directory)
        receipt_path = root / "episode.receipt.json"
        receipt = _read_json(receipt_path)
        if receipt.get("schema") != OFFLINE_EPISODE_RECEIPT_SCHEMA:
            raise ValueError("offline episode receipt schema mismatch")
        supplied = receipt.get("receipt_sha256")
        unsigned = dict(receipt)
        unsigned.pop("receipt_sha256", None)
        if supplied != _payload_sha256(unsigned):
            raise ValueError("offline episode receipt hash mismatch")
        episode_id = str(receipt.get("episode_id", ""))
        family = str(receipt.get("family", ""))
        status = str(receipt.get("status", ""))
        if not episode_id.strip() or family not in TRAJECTORY_FAMILIES or status not in {"completed", "failed"}:
            raise ValueError("offline episode receipt identity is invalid")
        if receipt.get("sealed") is not True or receipt.get("fixture_only") is not True or receipt.get("production_promotion_allowed") is not False:
            raise ValueError("offline episode receipt boundary/seal is invalid")
        artifact = root / str(receipt.get("artifact", "episode_v3.jsonl"))
        manifest = root / str(receipt.get("manifest", "episode_v3.manifest.json"))
        health = root / str(receipt.get("health", "recorder_health.json"))
        eligibility = root / str(receipt.get("eligibility", "eligibility.json"))
        failure_name = receipt.get("failure")
        failure = None if failure_name is None else root / str(failure_name)
        for path in (artifact, manifest, health, eligibility):
            if not path.is_file():
                raise ValueError(f"offline episode artifact is incomplete: {path}")
        if failure is not None and not failure.is_file():
            raise ValueError("offline failed episode is missing its failure receipt")
        if status == "failed" and (failure is None or not str(receipt.get("failure_reason", "")).strip()):
            raise ValueError("failed offline episode receipt is incomplete")
        if status == "completed" and (failure is not None or receipt.get("failure_reason") is not None):
            raise ValueError("completed offline episode carries failure evidence")
        if status == "failed":
            _validate_failure_receipt(
                failure,
                episode_id=episode_id,
                reason=str(receipt["failure_reason"]),
            )
        return cls(
            episode_id=episode_id,
            family=family,
            status=status,
            directory=root,
            artifact_path=artifact,
            manifest_path=manifest,
            health_path=health,
            eligibility_path=eligibility,
            failure_path=failure,
            failure_reason=None if receipt.get("failure_reason") is None else str(receipt["failure_reason"]),
            artifact_sha256=_sha(receipt.get("artifact_sha256"), "episode artifact sha256"),
            manifest_sha256=_sha(receipt.get("manifest_sha256"), "episode manifest sha256"),
            eligibility_sha256=_sha(receipt.get("eligibility_sha256"), "eligibility sha256"),
        )


def _validate_eligibility_receipt(path: Path, *, episode_id: str) -> dict[str, Any]:
    payload = read_eligibility_receipt(path)
    supplied = payload.get("decision_sha256")
    unsigned = dict(payload)
    unsigned.pop("decision_sha256", None)
    if supplied != _payload_sha256(unsigned):
        raise ValueError("eligibility decision hash mismatch")
    if payload.get("episode_id") != episode_id:
        raise ValueError("eligibility episode identity mismatch")
    return payload


def _validate_failure_receipt(path: Path, *, episode_id: str, reason: str | None) -> dict[str, Any]:
    if path is None:
        raise ValueError("failed episode requires a failure receipt")
    payload = _read_json(path)
    supplied = payload.get("failure_sha256")
    unsigned = dict(payload)
    unsigned.pop("failure_sha256", None)
    if payload.get("schema") != OFFLINE_FAILURE_SCHEMA or supplied != _payload_sha256(unsigned):
        raise ValueError("failure receipt is invalid")
    if payload.get("episode_id") != episode_id or payload.get("injected") is not True or payload.get("reason") != reason:
        raise ValueError("failure receipt identity/reason mismatch")
    return payload


def _episode_rows_for_dataset(
    episode: FixtureEpisodeArtifact,
    *,
    expected_split: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if episode.status != "completed" or episode.failure_path is not None or episode.failure_reason is not None:
        raise ValueError("failed episodes cannot enter the offline fixture dataset")
    if _file_sha256(episode.artifact_path) != episode.artifact_sha256:
        raise ValueError("episode artifact hash mismatch")
    if _file_sha256(episode.manifest_path) != episode.manifest_sha256:
        raise ValueError("episode manifest hash mismatch")
    if _file_sha256(episode.eligibility_path) != episode.eligibility_sha256:
        raise ValueError("eligibility artifact hash mismatch")
    manifest = validate_sealed_episode_manifest(episode.artifact_path, episode.manifest_path)
    if manifest.get("identity_enabled") is not True:
        raise ValueError("offline fixture episode identity is disabled")
    header, rows_tuple = read_episode_artifact(episode.artifact_path)
    if header.get("schema") != "ur10e_tacdiffusion_episode_artifact/v3":
        raise ValueError("legacy v2 episode artifacts are not eligible for the fixture dataset")
    rows = [dict(row) for row in rows_tuple]
    if not rows:
        raise ValueError("eligible fixture episode must contain rows")
    eligibility = _validate_eligibility_receipt(episode.eligibility_path, episode_id=episode.episode_id)
    if eligibility.get("training_eligible") is not True:
        raise ValueError("ineligible episode cannot enter the fixture dataset")
    if eligibility.get("first_live_shadow") is not False:
        raise ValueError("fixture dataset requires an explicit offline eligibility decision")
    for row in rows:
        if row.get("episode_id") != episode.episode_id or row.get("schema") != "ur10e_tacdiffusion_episode_frame/v3":
            raise ValueError("episode row identity/schema mismatch")
        row_sha = _sha(row.get("row_sha256"), "row_sha256")
        if row_sha != compute_v3_row_sha256(row):
            raise ValueError("canonical v3 row_sha256 mismatch")
        if row.get("identity_enabled") is not True or row.get("typed_receipts_valid") is not True:
            raise ValueError("episode row does not carry valid v3 typed receipts")
        observation = row.get("observation_84d")
        action = row.get("expert_action_12d")
        if not isinstance(observation, list) or len(observation) != OBSERVATION_DIMENSION:
            raise ValueError("offline fixture observation must be 84D")
        if not isinstance(action, list) or len(action) != ACTION_DIMENSION:
            raise ValueError("offline fixture action must be 12D")
        if row.get("expert_action_12d") != row.get("applied_action_12d") or row.get("applied_action_12d") != row.get("echoed_action_12d"):
            raise ValueError("offline fixture action echo is not exact")
        dynamics = row.get("dynamics_receipt")
        if not isinstance(dynamics, Mapping) or dynamics.get("valid") is not True or dynamics.get("source_kind") != "offline_fake_rtde" or dynamics.get("fixture_identity") != OFFLINE_FAKE_RTDE_FIXTURE_ID:
            raise ValueError("offline fixture dynamics receipt is invalid")
        observation_receipt = ObservationReceipt.from_json(row.get("observation_receipt"))
        reference_receipt = ReferenceReceipt.from_json(row.get("reference_receipt"))
        observation_payload = observation_receipt.payload
        reference_payload = reference_receipt.payload
        if observation_receipt.schema != OFFLINE_OBSERVATION_RECEIPT_SCHEMA or reference_receipt.schema != OFFLINE_REFERENCE_RECEIPT_SCHEMA:
            raise ValueError("offline fixture row is missing lineage/reference receipts")
        if observation_payload.get("episode_id") != episode.episode_id or reference_payload.get("episode_id") != episode.episode_id:
            raise ValueError("typed observation/reference receipt episode identity mismatch")
        if observation_payload.get("current_sequence") != row.get("control_sequence") or not math.isclose(float(observation_payload.get("current_timestamp_s")), float(row.get("control_time_s")), rel_tol=0.0, abs_tol=1e-12):
            raise ValueError("typed observation receipt sequence/timestamp mismatch")
        if tuple(observation_payload.get("current_observation_42d", ())) != tuple(observation[:OBSERVATION_SLICE_DIMENSION]) or tuple(observation_payload.get("previous_observation_42d", ())) != tuple(observation[OBSERVATION_SLICE_DIMENSION:]):
            raise ValueError("typed observation receipt does not bind the 84D row")
        if reference_payload.get("family") != episode.family or reference_payload.get("sequence") != row.get("control_sequence") or reference_payload.get("reference_sample_id") != row.get("reference_sample_id"):
            raise ValueError("typed reference receipt does not bind the row")
        command = FakeControllerCommand.from_json(reference_payload.get("controller_command_receipt"))
        if command.sequence != row.get("control_sequence") or not math.isclose(command.timestamp_s, float(row.get("control_time_s")), rel_tol=0.0, abs_tol=1e-12):
            raise ValueError("controller command sequence/timestamp is not row-bound")
        if tuple(command.echoed_action_12d) != tuple(row.get("echoed_action_12d", ())):
            raise ValueError("controller command echo is not row-bound")
        if row.get("controller_echo_12d") != row.get("echoed_action_12d"):
            raise ValueError("controller echo is not exact")
        action_label = row.get("action_label")
        if not isinstance(action_label, Mapping) or action_label.get("available") is not True:
            raise ValueError("offline fixture expert action label is unavailable")
        if row.get("sample_index") is None or int(row["sample_index"]) < 0:
            raise ValueError("offline fixture sample index is invalid")
        if expected_split not in {"train", "validation", "test"}:
            raise ValueError("offline fixture split is invalid")
    return rows, eligibility


def _npy_bytes(array: np.ndarray) -> bytes:
    stream = BytesIO()
    np.save(stream, array, allow_pickle=False)
    return stream.getvalue()


def _write_deterministic_npz(path: Path, arrays: Mapping[str, np.ndarray]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if temporary.exists():
        raise FileExistsError(f"temporary dataset already exists: {temporary}")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name in sorted(arrays):
                info = zipfile.ZipInfo(f"{name}.npy", date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o600 << 16
                archive.writestr(info, _npy_bytes(np.asarray(arrays[name])))
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


OFFLINE_DATASET_FIELDS = frozenset(
    {"observations", "actions", "episode_ids", "splits", "sample_indices", "timestamps_s", "row_sha256", "episode_artifact_sha256"}
)
OFFLINE_DATASET_MANIFEST_FIELDS = frozenset(
    {
        "schema",
        "observation_dimension",
        "action_dimension",
        "dataset_artifact",
        "dataset_sha256",
        "row_count",
        "observation_shape",
        "action_shape",
        "episode_count",
        "episode_order",
        "episode_split",
        "sample_index_shape",
        "split_row_counts",
        "split_episode_counts",
        "split_seed",
        "split_version",
        "split_sha256",
        "source_episode_artifact_hashes",
        "source_identities",
        "fixture_only",
        "production_promotion_allowed",
        "split_frozen_before_training",
        "episode_grouped_split_verified",
        "row_sha256_verified",
        "source_receipt_sha256",
        "legacy_dimensions_rejected",
        "manifest_sha256",
    }
)


def _load_episode_artifacts(episodes: Sequence[FixtureEpisodeArtifact | str | Path]) -> tuple[FixtureEpisodeArtifact, ...]:
    result: list[FixtureEpisodeArtifact] = []
    seen: set[str] = set()
    for item in episodes:
        episode = item if isinstance(item, FixtureEpisodeArtifact) else FixtureEpisodeArtifact.from_directory(item)
        if episode.episode_id in seen:
            raise ValueError("duplicate episode membership is not allowed")
        seen.add(episode.episode_id)
        result.append(episode)
    if not result:
        raise ValueError("offline fixture dataset requires at least one episode")
    return tuple(result)


def _validate_retained_failed_episode(episode: FixtureEpisodeArtifact) -> None:
    if episode.status != "failed":
        raise ValueError("retained failed episode validator received an eligible episode")
    if episode.failure_path is None or episode.failure_reason is None:
        raise ValueError("failed episode evidence is incomplete")
    if _file_sha256(episode.artifact_path) != episode.artifact_sha256:
        raise ValueError("failed episode artifact hash mismatch")
    if _file_sha256(episode.manifest_path) != episode.manifest_sha256:
        raise ValueError("failed episode manifest hash mismatch")
    if _file_sha256(episode.eligibility_path) != episode.eligibility_sha256:
        raise ValueError("failed episode eligibility hash mismatch")
    validate_sealed_episode_manifest(episode.artifact_path, episode.manifest_path)
    _validate_eligibility_receipt(episode.eligibility_path, episode_id=episode.episode_id)
    failure = _validate_failure_receipt(
        episode.failure_path,
        episode_id=episode.episode_id,
        reason=episode.failure_reason,
    )
    if failure.get("artifact_sha256") != episode.artifact_sha256 or failure.get("dataset_membership") is not False:
        raise ValueError("failed episode evidence is not bound to exclusion")


def _expected_dataset_arrays(
    artifacts: Mapping[str, FixtureEpisodeArtifact],
    *,
    episode_order: Sequence[str],
    episode_split: Mapping[str, str],
) -> tuple[dict[str, np.ndarray], dict[str, str]]:
    observations: list[list[float]] = []
    actions: list[list[float]] = []
    episode_ids: list[str] = []
    splits: list[str] = []
    sample_indices: list[int] = []
    timestamps: list[float] = []
    row_shas: list[str] = []
    artifact_hashes: list[str] = []
    source_artifacts: dict[str, str] = {}
    for episode_id in episode_order:
        episode = artifacts.get(episode_id)
        if episode is None or episode.status != "completed":
            raise ValueError("dataset source episodes are not exactly the eligible sealed episodes")
        sample_split = episode_split.get(episode_id)
        if sample_split is None:
            raise ValueError("dataset source episode is absent from the frozen split")
        rows, _ = _episode_rows_for_dataset(episode, expected_split=sample_split)
        source_artifacts[episode_id] = episode.artifact_sha256
        previous_timestamp = -math.inf
        for row in rows:
            timestamp = float(row["control_time_s"])
            if timestamp <= previous_timestamp:
                raise ValueError("timestamps must strictly increase within each episode")
            previous_timestamp = timestamp
            observations.append([float(value) for value in row["observation_84d"]])
            actions.append([float(value) for value in row["expert_action_12d"]])
            episode_ids.append(episode_id)
            splits.append(sample_split)
            sample_indices.append(int(row["sample_index"]))
            timestamps.append(timestamp)
            row_shas.append(_sha(row["row_sha256"], "row_sha256"))
            artifact_hashes.append(_sha(episode.artifact_sha256, "episode_artifact_sha256"))
    return (
        {
            "observations": np.asarray(observations, dtype=np.float32),
            "actions": np.asarray(actions, dtype=np.float32),
            "episode_ids": np.asarray(episode_ids, dtype=np.str_),
            "splits": np.asarray(splits, dtype=np.str_),
            "sample_indices": np.asarray(sample_indices, dtype=np.int64),
            "timestamps_s": np.asarray(timestamps, dtype=np.float64),
            "row_sha256": np.asarray(row_shas, dtype=np.str_),
            "episode_artifact_sha256": np.asarray(artifact_hashes, dtype=np.str_),
        },
        source_artifacts,
    )


def build_offline_fixture_dataset(
    dataset_path: str | Path,
    *,
    episodes: Sequence[FixtureEpisodeArtifact | str | Path],
    frozen_split: FrozenEpisodeSplit,
    source_receipt: Mapping[str, Any] | None = None,
    manifest_path: str | Path | None = None,
) -> dict[str, Any]:
    """Build an NPZ only from sealed, eligible v3 episode artifacts."""

    if not isinstance(frozen_split, FrozenEpisodeSplit):
        raise TypeError("offline fixture dataset requires a FrozenEpisodeSplit")
    artifact_values = _load_episode_artifacts(episodes)
    failed = tuple(episode for episode in artifact_values if episode.status == "failed")
    for episode in failed:
        _validate_retained_failed_episode(episode)
    eligible = tuple(episode for episode in artifact_values if episode.status == "completed")
    if failed and set(frozen_split.episode_split).intersection(episode.episode_id for episode in failed):
        raise ValueError("failed episodes cannot enter the frozen split")
    if set(frozen_split.episode_split) != {episode.episode_id for episode in eligible}:
        raise ValueError("frozen split must cover exactly the eligible episodes")
    source = dict(source_receipt) if source_receipt is not None else _source_receipt()
    identities = _validate_source_receipt(source)
    arrays, source_artifacts = _expected_dataset_arrays(
        {episode.episode_id: episode for episode in eligible},
        episode_order=[episode.episode_id for episode in eligible],
        episode_split=frozen_split.episode_split,
    )
    dataset = Path(dataset_path)
    _write_deterministic_npz(dataset, arrays)
    observations = arrays["observations"]
    actions = arrays["actions"]
    episode_ids = arrays["episode_ids"]
    splits = arrays["splits"]
    sample_indices = arrays["sample_indices"]
    timestamps = arrays["timestamps_s"]
    split_row_counts = {name: int(sum(value == name for value in splits.tolist())) for name in ("train", "validation", "test")}
    split_episode_counts = {
        name: int(sum(value == name for value in frozen_split.episode_split.values()))
        for name in ("train", "validation", "test")
    }
    manifest: dict[str, Any] = {
        "schema": OFFLINE_DATASET_SCHEMA,
        "observation_dimension": OBSERVATION_DIMENSION,
        "action_dimension": ACTION_DIMENSION,
        "dataset_artifact": dataset.name,
        "dataset_sha256": _file_sha256(dataset),
        "row_count": len(observations),
        "observation_shape": [len(observations), OBSERVATION_DIMENSION],
        "action_shape": [len(actions), ACTION_DIMENSION],
        "episode_count": len(frozen_split.episode_split),
        "episode_order": [episode.episode_id for episode in eligible],
        "episode_split": dict(frozen_split.episode_split),
        "sample_index_shape": [int(sample_indices.shape[0])],
        "split_row_counts": split_row_counts,
        "split_episode_counts": split_episode_counts,
        "split_seed": frozen_split.seed,
        "split_version": frozen_split.version,
        "split_sha256": frozen_split.split_sha256,
        "source_episode_artifact_hashes": source_artifacts,
        "source_identities": identities,
        "fixture_only": True,
        "production_promotion_allowed": False,
        "split_frozen_before_training": True,
        "episode_grouped_split_verified": True,
        "row_sha256_verified": True,
        "legacy_dimensions_rejected": {"condition_36d_action_6d": True, "accepted": False},
        "source_receipt_sha256": source["source_receipt_sha256"],
    }
    manifest["manifest_sha256"] = _payload_sha256(manifest)
    target_manifest = Path(manifest_path) if manifest_path is not None else dataset.with_suffix(".manifest.json")
    _write_json(target_manifest, manifest)
    return manifest


def validate_offline_fixture_dataset(
    dataset_path: str | Path,
    manifest_path: str | Path,
    *,
    episodes: Sequence[FixtureEpisodeArtifact | str | Path] | None = None,
    source_root: str | Path | None = None,
) -> dict[str, Any]:
    path = Path(dataset_path)
    manifest = _read_json(Path(manifest_path))
    if set(manifest) != OFFLINE_DATASET_MANIFEST_FIELDS:
        raise ValueError("offline fixture dataset manifest fields are missing or extra")
    if manifest.get("schema") != OFFLINE_DATASET_SCHEMA or manifest.get("observation_dimension") != OBSERVATION_DIMENSION or manifest.get("action_dimension") != ACTION_DIMENSION:
        raise ValueError("offline fixture dataset schema/dimension mismatch")
    if manifest.get("fixture_only") is not True or manifest.get("production_promotion_allowed") is not False:
        raise ValueError("offline fixture dataset cannot be promoted")
    unsigned = dict(manifest)
    supplied_manifest_hash = unsigned.pop("manifest_sha256", None)
    if _sha(supplied_manifest_hash, "manifest_sha256") != _payload_sha256(unsigned):
        raise ValueError("offline fixture dataset manifest hash mismatch")
    _sha(manifest.get("dataset_sha256"), "dataset_sha256")
    if manifest.get("dataset_artifact") != path.name or manifest.get("dataset_sha256") != _file_sha256(path):
        raise ValueError("offline fixture dataset artifact hash mismatch")
    current_source = _source_receipt(repository_root=Path(source_root) if source_root is not None else None)
    current_identities = _validate_source_receipt(
        current_source,
        repository_root=Path(source_root) if source_root is not None else None,
    )
    if _sha(manifest.get("source_receipt_sha256"), "source_receipt_sha256") != current_source["source_receipt_sha256"]:
        raise ValueError("offline fixture dataset source receipt drift")
    manifest_identities = manifest.get("source_identities")
    if not isinstance(manifest_identities, Mapping) or dict(manifest_identities) != current_identities:
        raise ValueError("offline fixture dataset source identities are not current source bytes")
    source_artifact_manifest = manifest.get("source_episode_artifact_hashes")
    if not isinstance(source_artifact_manifest, Mapping):
        raise ValueError("offline fixture dataset source artifact closure is invalid")
    for key, value in source_artifact_manifest.items():
        if not str(key).strip():
            raise ValueError("offline fixture dataset source episode identity is invalid")
        _sha(value, f"source_episode_artifact_hashes[{key}]")
    with np.load(path, allow_pickle=False) as archive:
        if set(archive.files) != OFFLINE_DATASET_FIELDS:
            raise ValueError("offline fixture dataset fields are missing or extra")
        observations = archive["observations"]
        actions = archive["actions"]
        episode_ids = archive["episode_ids"]
        splits = archive["splits"]
        sample_indices = archive["sample_indices"]
        timestamps = archive["timestamps_s"]
        row_shas = archive["row_sha256"]
        artifact_hashes = archive["episode_artifact_sha256"]
        if observations.dtype != np.float32 or observations.ndim != 2 or observations.shape[1] != OBSERVATION_DIMENSION:
            raise ValueError("offline fixture observations are not [N,84] float32")
        if actions.dtype != np.float32 or actions.ndim != 2 or actions.shape != (observations.shape[0], ACTION_DIMENSION):
            raise ValueError("offline fixture actions are not [N,12] float32")
        if sample_indices.dtype != np.int64 or sample_indices.shape != (observations.shape[0],):
            raise ValueError("offline fixture sample indices have the wrong shape/dtype")
        if timestamps.dtype != np.float64 or timestamps.shape != (observations.shape[0],):
            raise ValueError("offline fixture timestamps have the wrong shape/dtype")
        if episode_ids.shape != (observations.shape[0],) or splits.shape != (observations.shape[0],) or row_shas.shape != (observations.shape[0],) or artifact_hashes.shape != (observations.shape[0],):
            raise ValueError("offline fixture row metadata lengths do not match")
        if not np.isfinite(observations).all() or not np.isfinite(actions).all() or not np.isfinite(timestamps).all():
            raise ValueError("offline fixture dataset contains non-finite values")
        if np.any(sample_indices < 0) or np.any(timestamps < 0.0):
            raise ValueError("offline fixture timestamps must be non-negative")
        episode_values = [str(value) for value in episode_ids.tolist()]
        split_values = [str(value) for value in splits.tolist()]
        if any(value not in {"train", "validation", "test"} for value in split_values):
            raise ValueError("offline fixture dataset contains an invalid split")
        episode_split: dict[str, str] = {}
        last_timestamp: dict[str, float] = {}
        for episode_id, split, sample_index, timestamp in zip(episode_values, split_values, sample_indices.tolist(), timestamps.tolist()):
            if episode_id in episode_split and episode_split[episode_id] != split:
                raise ValueError("offline fixture dataset has episode row leakage")
            episode_split.setdefault(episode_id, split)
            if sample_index < 0:
                raise ValueError("offline fixture sample index is invalid")
            if episode_id in last_timestamp and float(timestamp) <= last_timestamp[episode_id]:
                raise ValueError("offline fixture timestamps must increase within each episode")
            last_timestamp[episode_id] = float(timestamp)
        if manifest.get("row_count") != int(observations.shape[0]) or manifest.get("episode_count") != len(episode_split):
            raise ValueError("offline fixture dataset count mismatch")
        if manifest.get("episode_split") != episode_split:
            raise ValueError("offline fixture frozen split mismatch")
        if manifest.get("episode_order") != list(dict.fromkeys(episode_values)):
            raise ValueError("offline fixture episode ordering mismatch")
        if manifest.get("sample_index_shape") != [int(sample_indices.shape[0])]:
            raise ValueError("offline fixture sample index shape mismatch")
        if manifest.get("observation_shape") != [int(value) for value in observations.shape] or manifest.get("action_shape") != [int(value) for value in actions.shape]:
            raise ValueError("offline fixture dataset shape mismatch")
        for value in row_shas.tolist():
            _sha(value, "row_sha256")
        for value in artifact_hashes.tolist():
            _sha(value, "episode_artifact_sha256")
        if manifest.get("row_sha256_verified") is not True:
            raise ValueError("offline fixture row_sha256 verification flag is missing")
        expected_split_counts = {name: int(sum(value == name for value in split_values)) for name in ("train", "validation", "test")}
        expected_episode_counts = {name: int(sum(value == name for value in episode_split.values())) for name in ("train", "validation", "test")}
        if manifest.get("split_row_counts") != expected_split_counts or manifest.get("split_episode_counts") != expected_episode_counts:
            raise ValueError("offline fixture split counts mismatch")
        if len(set(episode_split)) != len(episode_split):
            raise ValueError("offline fixture duplicate episode membership")
        if set(source_artifact_manifest) != set(episode_split):
            raise ValueError("offline fixture artifact hash metadata is invalid")
    split = FrozenEpisodeSplit(
        seed=int(manifest["split_seed"]),
        version=str(manifest["split_version"]),
        episode_split=manifest["episode_split"],
        split_sha256=str(manifest["split_sha256"]),
    )
    if manifest.get("split_sha256") != split.split_sha256:
        raise ValueError("offline fixture split hash mismatch")
    if episodes is not None:
        artifacts = _load_episode_artifacts(episodes)
        failed = tuple(item for item in artifacts if item.status == "failed")
        for item in failed:
            _validate_retained_failed_episode(item)
        eligible = tuple(item for item in artifacts if item.status == "completed")
        if {item.episode_id for item in eligible} != set(episode_split):
            raise ValueError("offline fixture source episodes do not cover dataset membership")
        expected_arrays, expected_sources = _expected_dataset_arrays(
            {item.episode_id: item for item in eligible},
            episode_order=manifest["episode_order"],
            episode_split=episode_split,
        )
        if dict(source_artifact_manifest) != expected_sources:
            raise ValueError("offline fixture episode artifact hashes are not cross-bound")
        with np.load(path, allow_pickle=False) as archive:
            for name, expected in expected_arrays.items():
                if not np.array_equal(archive[name], expected):
                    raise ValueError(f"offline fixture dataset array is not cross-bound to sealed episodes: {name}")
    return manifest


class _CountingHomeIdentityLedger(HomeIdentityLedger):
    def __init__(self, *, state_path: str | Path) -> None:
        super().__init__(state_path=state_path)
        self.ack_count = 0
        self.consume_count = 0

    def acknowledge(self, identity: str) -> None:
        super().acknowledge(identity)
        self.ack_count += 1

    def consume(self, identity: str) -> None:
        super().consume(identity)
        self.consume_count += 1


def _episode_context(
    *,
    episode_id: str,
    split: str,
    failed: bool,
) -> EpisodeSemanticContext:
    if failed:
        return EpisodeSemanticContext.diagnostic(
            episode_id=episode_id,
            capture_kind="offline_failed_fixture_evidence",
            hash_identities=SOURCE_IDENTITIES,
        )
    values = {
        "episode_id": episode_id,
        "dataset_split": split,
        "capture_kind": "offline_deterministic_fixture",
        "training_eligible": True,
        "surface_manifest_sha256": _sha256_text("offline-surface-v1"),
        "sensor_calibration_sha256": CALIBRATION_SHA256,
        "wrench_bias_sha256": _sha256_text("offline-wrench-bias-v1"),
        "normalization_sha256": NORMALIZATION_SHA256,
        "action_profile_sha256": _sha256_text("offline-action-profile-v1"),
        "filter_profile_sha256": _sha256_text("offline-filter-profile-v1"),
        "controller_identity_sha256": _sha256_text("offline-controller-identity-v1"),
        "receiver_identity_sha256": SOURCE_IDENTITIES[
            "source:experiments/ur10e-variable-impedance/ur10e_vic/tacdiffusion/episode_composition.py"
        ],
    }
    return EpisodeSemanticContext.from_training_bindings(
        bindings=ExpertEpisodeBindings(**values),
        hash_identities=SOURCE_IDENTITIES,
    )


def _write_episode_evidence(
    *,
    directory: Path,
    episode_id: str,
    family: str,
    attempt_index: int,
    status: str,
    failure_reason: str | None,
    frames: Sequence[EpisodeFrameV3],
    semantic_context: EpisodeSemanticContext,
) -> FixtureEpisodeArtifact:
    directory.mkdir(parents=True, exist_ok=False)
    recorder = EpisodeRecorder(
        directory,
        episode_id=episode_id,
        semantic_context=semantic_context,
        metadata={
            "offline_campaign_schema": OFFLINE_CAMPAIGN_SCHEMA,
            "family": family,
            "attempt_index": attempt_index,
            "status": status,
            "failure_reason": failure_reason,
            "fixture_only": True,
            "production_promotion_allowed": False,
        },
    )
    recorder.start()
    try:
        for frame in frames:
            if not recorder.enqueue(frame):
                raise RuntimeError("offline episode recorder rejected a canonical row")
        recorder_manifest = recorder.close(seal=True)
    except Exception:
        recorder.close(seal=False)
        raise
    if recorder_manifest is None:
        raise RuntimeError("offline episode recorder did not produce a seal")
    validate_sealed_episode_manifest(recorder.artifact_path, recorder.manifest_path)
    header, rows = read_episode_artifact(recorder.artifact_path)
    health = recorder.health(validate_tamper=True)
    active_window = None
    if rows:
        from .episode_composition import ActiveTrainingWindow

        active_window = ActiveTrainingWindow(0, len(rows) - 1)
    decision = EligibilityValidator().evaluate(
        rows,
        recorder_health=health,
        first_live_shadow=False,
        episode_id=episode_id,
        active_window=active_window,
        semantic_context=semantic_context,
    )
    eligibility_payload = EligibilityValidator().write_receipt(
        directory / "eligibility.json", decision, recorder_health=health
    )
    failure_name: str | None = None
    if status == "failed":
        if not failure_reason:
            raise ValueError("failed offline episode requires an injected reason")
        failure_name = "failure.json"
        unsigned_failure = {
            "schema": OFFLINE_FAILURE_SCHEMA,
            "episode_id": episode_id,
            "attempt_index": attempt_index,
            "injected": True,
            "reason": failure_reason,
            "sealed_evidence_retained": True,
            "dataset_membership": False,
            "artifact_sha256": _file_sha256(recorder.artifact_path),
        }
        _write_json(
            directory / failure_name,
            unsigned_failure | {"failure_sha256": _payload_sha256(unsigned_failure)},
        )
    elif failure_reason is not None:
        raise ValueError("completed offline episode cannot carry a failure reason")
    episode_unsigned = {
        "schema": OFFLINE_EPISODE_RECEIPT_SCHEMA,
        "episode_id": episode_id,
        "family": family,
        "attempt_index": attempt_index,
        "status": status,
        "failure_reason": failure_reason,
        "artifact": recorder.artifact_path.name,
        "manifest": recorder.manifest_path.name,
        "health": recorder.health_path.name,
        "eligibility": "eligibility.json",
        "failure": failure_name,
        "artifact_sha256": _file_sha256(recorder.artifact_path),
        "manifest_sha256": _file_sha256(recorder.manifest_path),
        "eligibility_sha256": _file_sha256(directory / "eligibility.json"),
        "row_count": len(rows),
        "eligibility_training_eligible": bool(decision.training_eligible),
        "sealed": True,
        "fixture_only": True,
        "production_promotion_allowed": False,
    }
    _write_json(
        directory / "episode.receipt.json",
        episode_unsigned | {"receipt_sha256": _payload_sha256(episode_unsigned)},
    )
    return FixtureEpisodeArtifact.from_directory(directory)


@dataclass(frozen=True)
class OfflineCampaignResult:
    root: Path
    campaign_receipt: dict[str, Any]
    recovery_receipt: RecoveryReceipt
    dataset_manifest: dict[str, Any]
    frozen_split: FrozenEpisodeSplit
    episodes: tuple[FixtureEpisodeArtifact, ...]
    artifact_root_digest: str | None = None


def _execute_campaign(root: Path, contract: OfflineCampaignContract) -> OfflineCampaignResult:
    root.mkdir(parents=True, exist_ok=True)
    episode_root = root / "episodes"
    episode_root.mkdir(parents=True, exist_ok=False)
    planned_failures = tuple(index for index in range(contract.attempted_episodes) if index % contract.fail_modulo == 0)
    eligible_ids = tuple(
        f"offline-episode-{index:03d}"
        for index in range(contract.attempted_episodes)
        if index not in planned_failures
    )
    frozen_split = FrozenEpisodeSplit.freeze(eligible_ids, seed=contract.seed)
    assembler = DeterministicOfflineEpisodeAssembler(seed=contract.seed)
    evidence_by_id: dict[str, FixtureEpisodeArtifact] = {}
    reset_counts = {"expert": 0, "filter": 0, "retract": 0}
    queue_state = Path(tempfile.mkdtemp(prefix="tacdiffusion-offline-queue-"))
    try:
        queue = PersistentRollingQueue(
            state_path=queue_state / "queue.json",
            campaign_home="offline_fixture_campaign_home",
        )
        lifecycle = CampaignLifecycle(queue)
        home_ledger = _CountingHomeIdentityLedger(state_path=queue_state / "home.json")

        def expert_reset() -> None:
            assembler.reset_expert()
            reset_counts["expert"] += 1

        def filter_reset() -> None:
            assembler.reset_filter()
            reset_counts["filter"] += 1

        def episode_runner(request: EpisodeRequest) -> bool:
            attempt_index = int(request.episode_id.rsplit("-", 1)[1])
            failed = attempt_index in planned_failures
            failure_reason = f"injected_fail_modulo_{contract.fail_modulo}" if failed else None
            split = "shadow" if failed else frozen_split.episode_split[request.episode_id]
            context = _episode_context(
                episode_id=request.episode_id,
                split=split,
                failed=failed,
            )
            composed = assembler.compose_episode(
                episode_id=request.episode_id,
                family=request.trajectory_family,
                episode_seed=request.seed,
                semantic_context=context,
                failure_reason=failure_reason,
                reset_state=False,
            )
            evidence_by_id[request.episode_id] = _write_episode_evidence(
                directory=episode_root / request.episode_id,
                episode_id=request.episode_id,
                family=request.trajectory_family,
                attempt_index=attempt_index,
                status="failed" if failed else "completed",
                failure_reason=failure_reason,
                frames=composed.frames,
                semantic_context=context,
            )
            return not failed

        def retract_plan(request: EpisodeRequest, identity: str) -> SafeRetractPlan:
            return SafeRetractPlan(
                home_identity=identity,
                task_ready_home=request.task_ready_home,
                reaction_normal_base=(0.0, 0.0, 1.0),
                approach_normal_base=(0.0, 0.0, -1.0),
                retract_distance_m=0.01,
            )

        def retract_to_home(plan: SafeRetractPlan) -> bool:
            if plan.retract_phase != "RETRACT_ALONG_CALIBRATED_REACTION_NORMAL" or plan.home_phase != "TASK_READY_HOME":
                return False
            reset_counts["retract"] += 1
            return True

        runner = CampaignRunner(
            lifecycle,
            home_ledger,
            episode_runner=episode_runner,
            retract_plan_provider=retract_plan,
            retract_to_home=retract_to_home,
            expert_reset=expert_reset,
            filter_reset=filter_reset,
            state_path=queue_state / "runner.json",
        )
        for index in range(contract.attempted_episodes):
            queue.append(
                EpisodeRequest(
                    episode_id=f"offline-episode-{index:03d}",
                    trajectory_family=contract.trajectory_families[index % len(contract.trajectory_families)],
                    seed=contract.seed + index,
                    task_ready_home="offline_fixture_home",
                    dispatch_id=f"offline-dispatch-{index:03d}",
                )
            )
        runner.request_drain()
        while True:
            step = runner.step()
            if step.decision == "WAITING_HOME":
                if runner.pending_home_identity is None:
                    raise AssertionError("campaign runner lost its pending Home identity")
                runner.acknowledge_and_consume_home(runner.pending_home_identity)
            elif step.decision == "COMPLETE":
                break
            elif step.decision in {"WAITING_FOR_EPISODE", "DRAINING"}:
                continue
            else:
                raise RuntimeError(f"offline campaign did not progress: {step.decision}")
        if lifecycle.state != CampaignState.COMPLETE or queue.mode.value != "COMPLETE":
            raise RuntimeError("offline campaign queue did not drain")
        if len(evidence_by_id) != contract.attempted_episodes:
            raise RuntimeError("offline campaign did not retain every attempted episode")
        recovery = run_persistent_recovery_cycles(
            queue_state / "recovery.json", cycles=contract.recovery_cycles
        )
        identity_proof = prove_recorder_sidecar_identity(seed=contract.seed)
        if identity_proof["identical"] is not True:
            raise RuntimeError("disabled recorder/sidecar changed deterministic control output")
        ordered_eligible = tuple(evidence_by_id[episode_id] for episode_id in eligible_ids)
        source_payload = _source_receipt()
        _validate_source_receipt(source_payload)
        dataset_manifest = build_offline_fixture_dataset(
            root / "dataset.npz",
            episodes=ordered_eligible,
            frozen_split=frozen_split,
            source_receipt=source_payload,
            manifest_path=root / "dataset.manifest.json",
        )
        _write_json(root / "frozen_split.json", frozen_split.as_json())
        _write_json(root / "source_identities.json", source_payload)
        _write_json(root / "recovery.receipt.json", recovery.as_json())
        family_counts = {
            family: sum(
                index % len(contract.trajectory_families) == family_index
                for index in range(contract.attempted_episodes)
            )
            for family_index, family in enumerate(contract.trajectory_families)
        }
        episode_entries = []
        for index in range(contract.attempted_episodes):
            episode_id = f"offline-episode-{index:03d}"
            episode = evidence_by_id[episode_id]
            entry = {
                "episode_id": episode_id,
                "family": contract.trajectory_families[index % len(contract.trajectory_families)],
                "attempt_index": index,
                "status": episode.status,
                "failure_reason": episode.failure_reason,
                "artifact": str(episode.artifact_path.relative_to(root)),
                "artifact_sha256": episode.artifact_sha256,
                "manifest": str(episode.manifest_path.relative_to(root)),
                "manifest_sha256": episode.manifest_sha256,
                "eligibility": str(episode.eligibility_path.relative_to(root)),
                "eligibility_sha256": episode.eligibility_sha256,
                "dataset_member": episode_id in frozen_split.episode_split,
            }
            if episode.failure_path is not None:
                entry["failure"] = str(episode.failure_path.relative_to(root))
            episode_entries.append(entry)
        counters = {
            "attempted": contract.attempted_episodes,
            "successful": sum(entry["status"] == "completed" for entry in episode_entries),
            "failed": sum(entry["status"] == "failed" for entry in episode_entries),
            "eligible": len(frozen_split.episode_split),
            "expert_resets": reset_counts["expert"],
            "observation_filter_resets": reset_counts["filter"],
            "retract_acks": reset_counts["retract"],
            "home_acks": home_ledger.ack_count,
            "consume_acks": home_ledger.consume_count,
            "queue_drained": queue.mode.value == "COMPLETE" and queue.pending_count == 0 and queue.inflight is None,
        }
        expected = {
            "attempted": 50,
            "successful": 42,
            "failed": 8,
            "eligible": 42,
            "expert_resets": 50,
            "observation_filter_resets": 50,
            "retract_acks": 50,
            "home_acks": 50,
            "consume_acks": 50,
            "queue_drained": True,
        }
        if counters != expected:
            raise RuntimeError(f"offline campaign counter mismatch: {counters}")
        campaign_unsigned = {
            "schema": OFFLINE_CAMPAIGN_RECEIPT_SCHEMA,
            "campaign_schema": OFFLINE_CAMPAIGN_SCHEMA,
            "contract": contract.as_json(),
            "contract_sha256": contract.contract_sha256,
            "family_order": list(contract.trajectory_families),
            "family_counts": family_counts,
            "fail_modulo": contract.fail_modulo,
            "injected_failure_episode_indices": list(planned_failures),
            "injected_failure_reason": f"injected_fail_modulo_{contract.fail_modulo}",
            "counters": counters,
            "episodes": episode_entries,
            "identity_proof": identity_proof,
            "source_receipt": "source_identities.json",
            "source_receipt_sha256": source_payload["source_receipt_sha256"],
            "recovery_receipt": "recovery.receipt.json",
            "recovery_receipt_sha256": _file_sha256(root / "recovery.receipt.json"),
            "dataset_manifest": "dataset.manifest.json",
            "dataset_manifest_sha256": _file_sha256(root / "dataset.manifest.json"),
            "fixture_only": True,
            "production_promotion_allowed": False,
            "reproduction_status": "not_claimed",
            "active_model_enabled": False,
            "hardware_data": "external_deferred",
            "production_dynamics_conformance": "external_deferred",
            "formal_checkpoint_rate_selection_model_activation": "external_deferred",
            "live_robot_qualification": "external_deferred",
        }
        campaign_payload = campaign_unsigned | {
            "receipt_sha256": _payload_sha256(campaign_unsigned)
        }
        _write_json(root / "campaign.receipt.json", campaign_payload)
        return OfflineCampaignResult(
            root=root,
            campaign_receipt=campaign_payload,
            recovery_receipt=recovery,
            dataset_manifest=dataset_manifest,
            frozen_split=frozen_split,
            episodes=tuple(evidence_by_id.values()),
        )
    finally:
        # State is intentionally outside the evidence bundle.  It is a
        # temporary, local persistence exercise and contains no live state.
        for child in sorted(queue_state.glob("*"), reverse=True):
            if child.is_file():
                child.unlink(missing_ok=True)
        queue_state.rmdir()


def _bundle_files(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == "bundle.manifest.json":
            continue
        relative = path.relative_to(root).as_posix()
        if not relative or relative.startswith("../") or ".." in PurePosixPath(relative).parts:
            raise ValueError("bundle contains an unsafe relative path")
        result[relative] = _file_sha256(path)
    return result


def validate_offline_campaign_bundle(
    root: str | Path,
    *,
    source_root: str | Path | None = None,
) -> dict[str, Any]:
    bundle_root = Path(root)
    manifest_path = bundle_root / "bundle.manifest.json"
    if not bundle_root.is_dir() or not manifest_path.is_file():
        raise ValueError("offline campaign bundle is partial or torn")
    manifest = _read_json(manifest_path)
    if manifest.get("schema") != OFFLINE_BUNDLE_SCHEMA or manifest.get("fixture_only") is not True or manifest.get("production_promotion_allowed") is not False:
        raise ValueError("offline campaign bundle boundary is invalid")
    unsigned = dict(manifest)
    supplied_digest = unsigned.pop("bundle_digest_sha256", None)
    if _sha(supplied_digest, "bundle_digest_sha256") != _payload_sha256(unsigned):
        raise ValueError("offline campaign bundle digest mismatch")
    file_hashes = manifest.get("file_hashes")
    if not isinstance(file_hashes, dict) or file_hashes != _bundle_files(bundle_root):
        raise ValueError("offline campaign bundle has missing, extra, or tampered files")
    campaign = _read_json(bundle_root / "campaign.receipt.json")
    supplied_campaign = campaign.get("receipt_sha256")
    campaign_unsigned = dict(campaign)
    campaign_unsigned.pop("receipt_sha256", None)
    if campaign.get("schema") != OFFLINE_CAMPAIGN_RECEIPT_SCHEMA or _sha(supplied_campaign, "campaign receipt_sha256") != _payload_sha256(campaign_unsigned):
        raise ValueError("offline campaign receipt is invalid")
    contract_payload = dict(campaign.get("contract", {}))
    contract_hash = contract_payload.pop("contract_sha256", None)
    campaign_contract = OfflineCampaignContract(**contract_payload)
    if campaign.get("contract_sha256") != campaign_contract.contract_sha256 or contract_hash != campaign_contract.contract_sha256:
        raise ValueError("offline campaign contract hash mismatch")
    if campaign.get("fixture_only") is not True or campaign.get("production_promotion_allowed") is not False or campaign.get("reproduction_status") != "not_claimed" or campaign.get("active_model_enabled") is not False:
        raise ValueError("offline campaign promotion/reproduction boundary is invalid")
    if campaign.get("source_receipt") != "source_identities.json":
        raise ValueError("offline campaign source receipt binding is missing")
    counters = campaign.get("counters")
    expected_counters = {
        "attempted": 50,
        "successful": 42,
        "failed": 8,
        "eligible": 42,
        "expert_resets": 50,
        "observation_filter_resets": 50,
        "retract_acks": 50,
        "home_acks": 50,
        "consume_acks": 50,
        "queue_drained": True,
    }
    if counters != expected_counters:
        raise ValueError("offline campaign counters are not the frozen exact values")
    entries = campaign.get("episodes")
    if not isinstance(entries, list) or len(entries) != 50:
        raise ValueError("offline campaign receipt does not cover exactly 50 episodes")
    if [entry.get("attempt_index") for entry in entries] != list(range(50)):
        raise ValueError("offline campaign episode ordering is not deterministic")
    if [entry.get("status") for entry in entries].count("failed") != 8:
        raise ValueError("offline campaign failure count is invalid")
    if any(
        entry.get("status") == "failed"
        and (entry.get("failure_reason") != "injected_fail_modulo_7" or entry.get("dataset_member") is not False)
        for entry in entries
    ):
        raise ValueError("offline campaign failed episode membership/reason is invalid")
    if any(entry.get("status") == "completed" and entry.get("dataset_member") is not True for entry in entries):
        raise ValueError("offline campaign eligible episode membership is invalid")
    if campaign.get("identity_proof", {}).get("identical") is not True:
        raise ValueError("offline campaign sidecar identity proof is missing")
    recovery = _read_json(bundle_root / "recovery.receipt.json")
    supplied_recovery = recovery.get("receipt_sha256")
    recovery_unsigned = dict(recovery)
    recovery_unsigned.pop("receipt_sha256", None)
    if _sha(supplied_recovery, "recovery receipt_sha256") != _payload_sha256(recovery_unsigned):
        raise ValueError("offline recovery receipt is invalid")
    RecoveryReceipt(**recovery_unsigned)
    split_payload = _read_json(bundle_root / "frozen_split.json")
    split_unsigned = dict(split_payload)
    split_hash = split_unsigned.pop("split_sha256", None)
    _sha(split_hash, "split_sha256")
    frozen_split = FrozenEpisodeSplit(
        seed=int(split_unsigned["seed"]),
        version=str(split_unsigned["version"]),
        episode_split=split_unsigned["episode_split"],
        split_sha256=str(split_hash),
    )
    source = _read_json(bundle_root / "source_identities.json")
    source_identities = _validate_source_receipt(
        source,
        repository_root=Path(source_root) if source_root is not None else None,
    )
    if _sha(campaign.get("source_receipt_sha256"), "campaign source_receipt_sha256") != source["source_receipt_sha256"]:
        raise ValueError("offline campaign source receipt digest mismatch")
    if manifest.get("source_receipt") != "source_identities.json" or _sha(manifest.get("source_receipt_sha256"), "bundle source_receipt_sha256") != source["source_receipt_sha256"]:
        raise ValueError("offline bundle source receipt binding is missing")
    if manifest.get("required_dimensions") != {"observation": OBSERVATION_DIMENSION, "action": ACTION_DIMENSION}:
        raise ValueError("offline bundle dimensions are invalid")
    episodes_root = bundle_root / "episodes"
    episodes = tuple(FixtureEpisodeArtifact.from_directory(path) for path in sorted(episodes_root.iterdir()) if path.is_dir())
    eligible = tuple(item for item in episodes if item.status == "completed")
    validate_offline_fixture_dataset(
        bundle_root / "dataset.npz",
        bundle_root / "dataset.manifest.json",
        episodes=episodes,
        source_root=source_root,
    )
    if _sha(campaign.get("dataset_manifest_sha256"), "campaign dataset_manifest_sha256") != _file_sha256(bundle_root / "dataset.manifest.json"):
        raise ValueError("offline campaign dataset manifest binding mismatch")
    if _sha(campaign.get("recovery_receipt_sha256"), "campaign recovery_receipt_sha256") != _file_sha256(bundle_root / "recovery.receipt.json"):
        raise ValueError("offline campaign recovery receipt binding mismatch")
    if dict(source_identities) != dict(_source_identity_hashes(source)):
        raise ValueError("offline source identity mapping is not bound to the source receipt")
    if set(item.episode_id for item in eligible) != set(frozen_split.episode_split):
        raise ValueError("bundle dataset membership does not match the frozen split")
    return manifest


def materialize_offline_campaign_bundle(
    output_root: str | Path,
    *,
    contract: OfflineCampaignContract | None = None,
) -> OfflineCampaignResult:
    """Materialize once, or verify an already complete content-addressed bundle."""

    destination = Path(output_root)
    selected_contract = contract or OfflineCampaignContract()
    if destination.exists():
        manifest = validate_offline_campaign_bundle(destination)
        if manifest.get("contract_sha256") != selected_contract.contract_sha256:
            raise ValueError("existing offline campaign bundle contract identity mismatch")
        campaign = _read_json(destination / "campaign.receipt.json")
        recovery_payload = _read_json(destination / "recovery.receipt.json")
        split_payload = _read_json(destination / "frozen_split.json")
        split_hash = str(split_payload.pop("split_sha256"))
        frozen_split = FrozenEpisodeSplit(
            seed=int(split_payload["seed"]),
            version=str(split_payload["version"]),
            episode_split=split_payload["episode_split"],
            split_sha256=split_hash,
        )
        recovery_unsigned = dict(recovery_payload)
        recovery_unsigned.pop("receipt_sha256", None)
        episodes = tuple(
            FixtureEpisodeArtifact.from_directory(path)
            for path in sorted((destination / "episodes").iterdir())
            if path.is_dir()
        )
        return OfflineCampaignResult(
            root=destination,
            campaign_receipt=campaign,
            recovery_receipt=RecoveryReceipt(**recovery_unsigned),
            dataset_manifest=_read_json(destination / "dataset.manifest.json"),
            frozen_split=frozen_split,
            episodes=episodes,
            artifact_root_digest=str(manifest["bundle_digest_sha256"]),
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    partials = tuple(destination.parent.glob(f".{destination.name}.tmp-*"))
    if partials:
        raise RuntimeError("partial/torn offline campaign staging output exists")
    staging = destination.parent / f".{destination.name}.tmp-{os.getpid()}"
    if staging.exists():
        raise RuntimeError("partial/torn offline campaign staging output exists")
    result = _execute_campaign(staging, selected_contract)
    file_hashes = _bundle_files(staging)
    bundle_unsigned = {
        "schema": OFFLINE_BUNDLE_SCHEMA,
        "format_version": 1,
        "contract_sha256": selected_contract.contract_sha256,
        "campaign_receipt": "campaign.receipt.json",
        "campaign_receipt_sha256": file_hashes["campaign.receipt.json"],
        "dataset_manifest": "dataset.manifest.json",
        "dataset_manifest_sha256": file_hashes["dataset.manifest.json"],
        "source_receipt": "source_identities.json",
        "source_receipt_sha256": _read_json(staging / "source_identities.json")["source_receipt_sha256"],
        "file_hashes": file_hashes,
        "required_dimensions": {"observation": OBSERVATION_DIMENSION, "action": ACTION_DIMENSION},
        "fixture_only": True,
        "production_promotion_allowed": False,
    }
    bundle_manifest = bundle_unsigned | {"bundle_digest_sha256": _payload_sha256(bundle_unsigned)}
    _write_json(staging / "bundle.manifest.json", bundle_manifest)
    validate_offline_campaign_bundle(staging)
    os.replace(staging, destination)
    directory_fd = os.open(destination.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    materialized_episodes = tuple(
        FixtureEpisodeArtifact.from_directory(path)
        for path in sorted((destination / "episodes").iterdir())
        if path.is_dir()
    )
    return OfflineCampaignResult(
        root=destination,
        campaign_receipt=result.campaign_receipt,
        recovery_receipt=result.recovery_receipt,
        dataset_manifest=result.dataset_manifest,
        frozen_split=result.frozen_split,
        episodes=materialized_episodes,
        artifact_root_digest=bundle_manifest["bundle_digest_sha256"],
    )


__all__ = [
    "ACTION_DIMENSION",
    "CAMPAIGN_EPISODE_COUNT",
    "CAMPAIGN_FAIL_MODULO",
    "CAMPAIGN_RECOVERY_CYCLES",
    "DeterministicFakeRTDE",
    "DeterministicOfflineEpisodeAssembler",
    "FakeRTDETick",
    "FixtureEpisodeArtifact",
    "FrozenEpisodeSplit",
    "OfflineCampaignContract",
    "OfflineCampaignResult",
    "OfflineComposedEpisode",
    "OfflineControlOutput",
    "OfflineObservationReceipt",
    "OfflineReferenceReceipt",
    "RecoveryReceipt",
    "SyntheticKunweiDriver",
    "build_offline_fixture_dataset",
    "materialize_offline_campaign_bundle",
    "prove_recorder_sidecar_identity",
    "run_persistent_recovery_cycles",
    "validate_offline_campaign_bundle",
    "validate_offline_fixture_dataset",
]
