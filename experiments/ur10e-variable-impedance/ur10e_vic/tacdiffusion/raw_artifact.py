"""Durable 500 Hz raw evidence and non-destructive training views."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Iterable, Sequence


RAW_ARTIFACT_SCHEMA = "ur10e_tacdiffusion_raw_frame/v3"
OBSERVATION_SCHEMA = "ur10e_tacdiffusion_observation/v2"
ACTION_SCHEMA = "ur10e_tacdiffusion_action/v2"
RAW_RATE_HZ = 500


def _six(values: Iterable[float], name: str) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if len(result) != 6:
        raise ValueError(f"{name} must contain six values")
    if not all(math.isfinite(value) for value in result):
        raise ValueError(f"{name} must contain finite values")
    return result


@dataclass(frozen=True)
class RawFrameRecord:
    episode_id: str
    sample_index: int
    source_timestamp_s: float
    frame_timestamp_s: float
    model_timestamp_s: float
    raw_f_df: tuple[float, ...] | Sequence[float]
    filtered_f_ff: tuple[float, ...] | Sequence[float]
    filter_velocity: tuple[float, ...] | Sequence[float]
    stiffness_k: tuple[float, ...] | Sequence[float]
    damping_d: tuple[float, ...] | Sequence[float]
    model_sequence: int
    inference_latency_s: float
    observation_schema: str = OBSERVATION_SCHEMA
    action_schema: str = ACTION_SCHEMA

    def __post_init__(self) -> None:
        if not self.episode_id.strip() or self.sample_index < 0 or self.model_sequence < 0:
            raise ValueError("raw frame identity is invalid")
        timestamps = (self.source_timestamp_s, self.frame_timestamp_s, self.model_timestamp_s)
        if not all(math.isfinite(value) and value >= 0.0 for value in timestamps):
            raise ValueError("raw frame timestamps are invalid")
        if self.source_timestamp_s > self.frame_timestamp_s:
            raise ValueError("source timestamp cannot follow frame timestamp")
        if self.model_timestamp_s > self.frame_timestamp_s:
            raise ValueError("model timestamp cannot follow frame timestamp")
        if not math.isfinite(self.inference_latency_s) or self.inference_latency_s < 0.0:
            raise ValueError("raw frame inference latency is invalid")
        for name in ("raw_f_df", "filtered_f_ff", "filter_velocity", "stiffness_k", "damping_d"):
            object.__setattr__(self, name, _six(getattr(self, name), name))
        if self.observation_schema != OBSERVATION_SCHEMA or self.action_schema != ACTION_SCHEMA:
            raise ValueError("raw frame schema mismatch")

    def as_json(self) -> dict[str, object]:
        payload = asdict(self)
        payload["schema"] = RAW_ARTIFACT_SCHEMA
        return payload


def _header(episode_id: str) -> dict[str, object]:
    return {
        "schema": RAW_ARTIFACT_SCHEMA,
        "episode_id": episode_id,
        "rate_hz": RAW_RATE_HZ,
        "observation_schema": OBSERVATION_SCHEMA,
        "action_schema": ACTION_SCHEMA,
    }


def _parse_artifact(path: Path, *, expected_episode_id: str | None = None) -> tuple[dict[str, object], tuple[RawFrameRecord, ...]]:
    data = path.read_bytes()
    if not data or not data.endswith(b"\n"):
        raise ValueError("raw artifact has an incomplete or unterminated final line")
    lines = data.splitlines(keepends=True)
    try:
        header = json.loads(lines[0].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("raw artifact header is invalid") from exc
    if header != _header(str(header.get("episode_id", ""))):
        raise ValueError("raw artifact header/schema mismatch")
    if expected_episode_id is not None and header["episode_id"] != expected_episode_id:
        raise ValueError("raw artifact episode identity mismatch")

    result: list[RawFrameRecord] = []
    last_source = -math.inf
    last_frame = -math.inf
    last_model_timestamp = -math.inf
    last_model_sequence = -1
    for expected_index, line in enumerate(lines[1:]):
        if not line.endswith(b"\n"):
            raise ValueError("raw artifact has an incomplete final line")
        try:
            payload = json.loads(line.decode("utf-8"))
            if payload.pop("schema", None) != RAW_ARTIFACT_SCHEMA:
                raise ValueError("raw frame schema mismatch")
            frame = RawFrameRecord(**payload)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, KeyError, ValueError) as exc:
            if isinstance(exc, ValueError) and str(exc) == "raw frame schema mismatch":
                raise
            raise ValueError(f"raw artifact row {expected_index} is invalid") from exc
        if frame.episode_id != header["episode_id"] or frame.sample_index != expected_index:
            raise ValueError("raw artifact frame identity/index mismatch")
        if frame.source_timestamp_s <= last_source or frame.frame_timestamp_s <= last_frame:
            raise ValueError("raw artifact source/frame timestamps are not increasing")
        if frame.model_timestamp_s < last_model_timestamp:
            raise ValueError("raw artifact model timestamps regress")
        if frame.model_sequence < last_model_sequence:
            raise ValueError("raw artifact model sequence regresses")
        result.append(frame)
        last_source = frame.source_timestamp_s
        last_frame = frame.frame_timestamp_s
        last_model_timestamp = frame.model_timestamp_s
        last_model_sequence = frame.model_sequence
    return header, tuple(result)


def _recover_incomplete_tail(path: Path) -> None:
    """Remove only a non-newline-terminated, non-JSON tail fragment."""

    data = path.read_bytes()
    if not data or data.endswith(b"\n"):
        return
    fragment = data[data.rfind(b"\n") + 1 :]
    try:
        json.loads(fragment.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        with path.open("r+b") as handle:
            handle.truncate(len(data) - len(fragment))
            handle.flush()
            os.fsync(handle.fileno())
        return
    raise ValueError("raw artifact has a complete JSON row without a terminating newline")


class DurableRawFrameWriter:
    def __init__(self, path: str | Path, *, episode_id: str, flush_every: int = 1) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not episode_id.strip() or flush_every != 1:
            raise ValueError("episode_id is required and flush_every must be exactly 1")
        self.episode_id = episode_id
        self.flush_every = flush_every
        if self.path.exists():
            _recover_incomplete_tail(self.path)
            _, frames = _parse_artifact(self.path, expected_episode_id=episode_id)
            self._handle = self.path.open("r+b")
            self._handle.seek(0, os.SEEK_END)
        else:
            self._handle = self.path.open("w+b")
            header_line = (json.dumps(_header(episode_id), sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
            self._write_complete(header_line)
            directory_fd = os.open(self.path.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            frames = ()
        self._count = len(frames)
        self._last_source_timestamp = frames[-1].source_timestamp_s if frames else -math.inf
        self._last_frame_timestamp = frames[-1].frame_timestamp_s if frames else -math.inf
        self._last_model_timestamp = frames[-1].model_timestamp_s if frames else -math.inf
        self._last_model_sequence = frames[-1].model_sequence if frames else -1

    def _write_complete(self, data: bytes) -> None:
        written = self._handle.write(data)
        if written != len(data):
            raise OSError("raw artifact write was incomplete")
        self._handle.flush()
        os.fsync(self._handle.fileno())

    def append(self, frame: RawFrameRecord) -> None:
        if frame.episode_id != self.episode_id or frame.sample_index != self._count:
            raise ValueError("raw frame episode/index identity mismatch")
        if frame.source_timestamp_s <= self._last_source_timestamp or frame.frame_timestamp_s <= self._last_frame_timestamp:
            raise ValueError("raw frame source/frame timestamp regression")
        if frame.model_timestamp_s < self._last_model_timestamp:
            raise ValueError("raw frame model timestamp regression")
        if frame.model_sequence < self._last_model_sequence:
            raise ValueError("raw frame model sequence regression")
        data = (json.dumps(frame.as_json(), sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
        self._write_complete(data)
        self._count += 1
        self._last_source_timestamp = frame.source_timestamp_s
        self._last_frame_timestamp = frame.frame_timestamp_s
        self._last_model_timestamp = frame.model_timestamp_s
        self._last_model_sequence = frame.model_sequence

    def flush(self) -> None:
        self._handle.flush()
        os.fsync(self._handle.fileno())

    def close(self) -> None:
        if not self._handle.closed:
            self.flush()
            self._handle.close()

    def __enter__(self) -> "DurableRawFrameWriter":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def read_raw_frames(path: str | Path) -> tuple[RawFrameRecord, ...]:
    _, frames = _parse_artifact(Path(path))
    return frames


def raw_artifact_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def derive_training_view(path: str | Path, *, target_rate_hz: int) -> tuple[RawFrameRecord, ...]:
    if target_rate_hz not in {50, 100}:
        raise ValueError("training view rate must be 50 or 100 Hz")
    frames = read_raw_frames(path)
    stride = RAW_RATE_HZ // target_rate_hz
    return tuple(frame for frame in frames if frame.sample_index % stride == 0)
