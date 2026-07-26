"""Durable, hash-bound 84D observation plus 12D expert-action episodes."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Mapping, Sequence


EXPERT_EPISODE_SCHEMA = "ur10e_tacdiffusion_expert_episode/v1"
EXPERT_EPISODE_FRAME_SCHEMA = "ur10e_tacdiffusion_expert_episode_frame/v1"
OBSERVATION_DIMENSION = 84
ACTION_DIMENSION = 12
ALLOWED_SPLITS = frozenset({"train", "validation", "test", "shadow"})


def _vector(values: Sequence[float], length: int, name: str) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if len(result) != length or not all(math.isfinite(value) for value in result):
        raise ValueError(f"{name} must contain {length} finite values")
    return result


def _sha(value: str, name: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return value


@dataclass(frozen=True)
class ExpertEpisodeBindings:
    episode_id: str
    dataset_split: str
    capture_kind: str
    training_eligible: bool
    surface_manifest_sha256: str
    sensor_calibration_sha256: str
    wrench_bias_sha256: str
    normalization_sha256: str
    action_profile_sha256: str
    filter_profile_sha256: str
    controller_identity_sha256: str
    receiver_identity_sha256: str

    def __post_init__(self) -> None:
        if not self.episode_id.strip() or not self.capture_kind.strip():
            raise ValueError("episode identity and capture kind are required")
        if self.dataset_split not in ALLOWED_SPLITS:
            raise ValueError("dataset split is invalid")
        if self.training_eligible and self.dataset_split == "shadow":
            raise ValueError("shadow capture cannot be training eligible")
        for name in (
            "surface_manifest_sha256",
            "sensor_calibration_sha256",
            "wrench_bias_sha256",
            "normalization_sha256",
            "action_profile_sha256",
            "filter_profile_sha256",
            "controller_identity_sha256",
            "receiver_identity_sha256",
        ):
            _sha(getattr(self, name), name)


@dataclass(frozen=True)
class ExpertEpisodeFrame:
    episode_id: str
    sample_index: int
    control_sequence: int
    control_timestamp_s: float
    external_source_sequence: int
    external_source_timestamp_s: float
    observation_84d: tuple[float, ...] | Sequence[float]
    expert_action_12d: tuple[float, ...] | Sequence[float]
    applied_action_12d: tuple[float, ...] | Sequence[float]
    filtered_f_ff: tuple[float, ...] | Sequence[float]
    filter_velocity: tuple[float, ...] | Sequence[float]
    receiver_ack_sequence: int

    def __post_init__(self) -> None:
        if not self.episode_id.strip() or min(
            self.sample_index,
            self.control_sequence,
            self.external_source_sequence,
            self.receiver_ack_sequence,
        ) < 0:
            raise ValueError("expert episode frame identity is invalid")
        if not all(
            math.isfinite(value) and value >= 0.0
            for value in (self.control_timestamp_s, self.external_source_timestamp_s)
        ):
            raise ValueError("expert episode timestamps are invalid")
        if self.external_source_timestamp_s > self.control_timestamp_s + 1e-12:
            raise ValueError("external source timestamp is not causal")
        object.__setattr__(
            self, "observation_84d", _vector(self.observation_84d, 84, "observation_84d")
        )
        object.__setattr__(
            self, "expert_action_12d", _vector(self.expert_action_12d, 12, "expert_action_12d")
        )
        object.__setattr__(
            self, "applied_action_12d", _vector(self.applied_action_12d, 12, "applied_action_12d")
        )
        object.__setattr__(
            self, "filtered_f_ff", _vector(self.filtered_f_ff, 6, "filtered_f_ff")
        )
        object.__setattr__(
            self, "filter_velocity", _vector(self.filter_velocity, 6, "filter_velocity")
        )

    def as_json(self) -> dict[str, object]:
        return {"schema": EXPERT_EPISODE_FRAME_SCHEMA, **asdict(self)}


def _header(bindings: ExpertEpisodeBindings) -> dict[str, object]:
    return {
        "schema": EXPERT_EPISODE_SCHEMA,
        "rate_hz": 500,
        "observation_dimension": OBSERVATION_DIMENSION,
        "action_dimension": ACTION_DIMENSION,
        "bindings": asdict(bindings),
    }


def _line(payload: Mapping[str, object]) -> bytes:
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")


class DurableExpertEpisodeWriter:
    """Append one fsync-complete row at a time; never drops or overwrites rows."""

    def __init__(
        self,
        path: str | Path,
        *,
        bindings: ExpertEpisodeBindings,
    ) -> None:
        self.path = Path(path)
        self.bindings = bindings
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            raise FileExistsError(f"expert episode artifact already exists: {self.path}")
        self._handle = self.path.open("xb")
        self._count = 0
        self._last_control_timestamp = -math.inf
        self._last_source_timestamp = -math.inf
        self._last_source_sequence = -1
        self._last_ack = -1
        self._write(_line(_header(bindings)))
        directory_fd = os.open(self.path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    @property
    def row_count(self) -> int:
        return self._count

    def _write(self, payload: bytes) -> None:
        if self._handle.write(payload) != len(payload):
            raise OSError("expert episode artifact write was incomplete")
        self._handle.flush()
        os.fsync(self._handle.fileno())

    def append(self, frame: ExpertEpisodeFrame) -> None:
        if frame.episode_id != self.bindings.episode_id or frame.sample_index != self._count:
            raise ValueError("expert episode frame identity/index mismatch")
        if frame.control_timestamp_s <= self._last_control_timestamp:
            raise ValueError("control timestamp must strictly increase")
        if frame.external_source_timestamp_s <= self._last_source_timestamp:
            raise ValueError("external source timestamp must strictly increase")
        if frame.external_source_sequence <= self._last_source_sequence:
            raise ValueError("external source sequence must strictly increase")
        if frame.receiver_ack_sequence < self._last_ack:
            raise ValueError("receiver ACK sequence regressed")
        if self.bindings.training_eligible and frame.expert_action_12d != frame.applied_action_12d:
            raise ValueError("training-eligible frame expert/applied action mismatch")
        self._write(_line(frame.as_json()))
        self._count += 1
        self._last_control_timestamp = frame.control_timestamp_s
        self._last_source_timestamp = frame.external_source_timestamp_s
        self._last_source_sequence = frame.external_source_sequence
        self._last_ack = frame.receiver_ack_sequence

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.flush()
            os.fsync(self._handle.fileno())
            self._handle.close()

    def finalize(self, manifest_path: str | Path) -> dict[str, object]:
        self.close()
        if self._count <= 0:
            raise ValueError("expert episode artifact is empty")
        manifest = {
            "schema": EXPERT_EPISODE_SCHEMA,
            "artifact": self.path.name,
            "artifact_sha256": hashlib.sha256(self.path.read_bytes()).hexdigest(),
            "row_count": self._count,
            "rate_hz": 500,
            "observation_dimension": 84,
            "action_dimension": 12,
            "bindings": asdict(self.bindings),
        }
        destination = Path(manifest_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise FileExistsError(f"expert episode manifest already exists: {destination}")
        temporary = destination.with_name(destination.name + ".tmp")
        with temporary.open("xb") as handle:
            handle.write(_line(manifest))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        directory_fd = os.open(destination.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return manifest

    def __enter__(self) -> "DurableExpertEpisodeWriter":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def read_expert_episode(
    path: str | Path,
) -> tuple[ExpertEpisodeBindings, tuple[ExpertEpisodeFrame, ...]]:
    artifact = Path(path)
    data = artifact.read_bytes()
    if not data or not data.endswith(b"\n"):
        raise ValueError("expert episode artifact has an incomplete final line")
    rows = data.splitlines()
    try:
        header = json.loads(rows[0])
    except json.JSONDecodeError as exc:
        raise ValueError("expert episode header is invalid") from exc
    if not isinstance(header, dict) or header.get("schema") != EXPERT_EPISODE_SCHEMA:
        raise ValueError("expert episode header schema mismatch")
    if header.get("rate_hz") != 500 or header.get("observation_dimension") != 84 or header.get("action_dimension") != 12:
        raise ValueError("expert episode dimensional/rate contract mismatch")
    try:
        bindings = ExpertEpisodeBindings(**header["bindings"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("expert episode bindings are invalid") from exc
    frames: list[ExpertEpisodeFrame] = []
    for index, raw in enumerate(rows[1:]):
        try:
            payload = json.loads(raw)
            if payload.pop("schema", None) != EXPERT_EPISODE_FRAME_SCHEMA:
                raise ValueError("expert episode frame schema mismatch")
            frame = ExpertEpisodeFrame(**payload)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError(f"expert episode frame {index} is invalid") from exc
        if frame.sample_index != index or frame.episode_id != bindings.episode_id:
            raise ValueError("expert episode frame order/identity mismatch")
        if frames:
            previous = frames[-1]
            if (
                frame.control_timestamp_s <= previous.control_timestamp_s
                or frame.external_source_timestamp_s <= previous.external_source_timestamp_s
                or frame.external_source_sequence <= previous.external_source_sequence
                or frame.receiver_ack_sequence < previous.receiver_ack_sequence
            ):
                raise ValueError("expert episode frame lineage regressed")
        if bindings.training_eligible and frame.expert_action_12d != frame.applied_action_12d:
            raise ValueError("training-eligible expert/applied action mismatch")
        frames.append(frame)
    return bindings, tuple(frames)


def validate_expert_episode_manifest(
    artifact_path: str | Path,
    manifest_path: str | Path,
) -> dict[str, object]:
    artifact = Path(artifact_path)
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    bindings, frames = read_expert_episode(artifact)
    expected = {
        "schema": EXPERT_EPISODE_SCHEMA,
        "artifact": artifact.name,
        "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "row_count": len(frames),
        "rate_hz": 500,
        "observation_dimension": 84,
        "action_dimension": 12,
        "bindings": asdict(bindings),
    }
    if manifest != expected:
        raise ValueError("expert episode manifest does not match artifact")
    return expected
