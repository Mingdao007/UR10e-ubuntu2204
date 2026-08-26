"""Single-reader, advisory-only arm camera observer for Autotuner V5."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import subprocess
import time
from typing import Any, Callable, Mapping


CAMERA_SCHEMA = "step6.autotune/arm-camera-observer-v1"
CAMERA_VERSION = 1
FRAME_SCHEMA = "step6.autotune/arm-camera-frame-v1"
RECOMMENDATION_SCHEMA = "step6.autotune/primitive-recommendation-v1"
RTSP_URL = "rtsp://127.0.0.1:8554/arm"
RING_DURATION_S = 15.0
REGULAR_SAMPLE_HZ = 1.0
STALE_AFTER_S = 2.5
MAX_RING_FRAMES = 64
MAX_JPEG_BYTES = 10 * 1024 * 1024
RETENTION_POLICY_VERSION = 2
HARD_QUOTA_BYTES = 1 << 30
SOFT_FRAME_COUNT_TARGET = 1000
RETENTION_METADATA_RESERVE_BYTES = 1 << 20
EVENT_PIN_TTL_NS = 900 * 1_000_000_000
EVENT_PIN_MAX_BYTES = 256 << 20


class V5CameraObserverError(RuntimeError):
    """Camera evidence violates the advisory-only observer contract."""


class CameraObservationStatusV1(str, Enum):
    FRESH_ADVISORY = "FRESH_ADVISORY"
    UNKNOWN = "UNKNOWN"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise V5CameraObserverError(f"{name} must be finite")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise V5CameraObserverError(f"{name} must be finite") from exc
    if not math.isfinite(result):
        raise V5CameraObserverError(f"{name} must be finite")
    return result


def _atomic_bytes(path: Path, value: bytes) -> None:
    target = Path(path)
    if target.is_symlink() or target.parent.is_symlink():
        raise V5CameraObserverError("camera evidence path must not use symlinks")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        descriptor = os.open(str(target.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    _atomic_bytes(path, _canonical(dict(value)) + b"\n")


@dataclass(frozen=True)
class CameraFrameV1:
    monotonic_ns: int
    source_timestamp_ns: int
    jpeg_bytes: bytes
    event_codes: tuple[str, ...] = ()
    schema: str = FRAME_SCHEMA
    version: int = CAMERA_VERSION

    def __post_init__(self) -> None:
        if self.schema != FRAME_SCHEMA or self.version != CAMERA_VERSION:
            raise V5CameraObserverError("camera frame schema/version differs")
        if type(self.monotonic_ns) is not int or self.monotonic_ns < 0 or type(self.source_timestamp_ns) is not int or self.source_timestamp_ns < 0:
            raise V5CameraObserverError("camera frame timestamps are invalid")
        encoded = bytes(self.jpeg_bytes)
        if not 4 <= len(encoded) <= MAX_JPEG_BYTES or not encoded.startswith(b"\xff\xd8") or not encoded.endswith(b"\xff\xd9"):
            raise V5CameraObserverError("camera frame is not a bounded JPEG")
        events = tuple(self.event_codes)
        if any(type(item) is not str or not item or any(char in item for char in "\r\n") for item in events) or len(set(events)) != len(events):
            raise V5CameraObserverError("camera frame event codes are invalid")
        object.__setattr__(self, "jpeg_bytes", encoded)
        object.__setattr__(self, "event_codes", events)

    @property
    def jpeg_sha256(self) -> str:
        return _sha_bytes(self.jpeg_bytes)

    def metadata(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "monotonic_ns": self.monotonic_ns,
            "source_timestamp_ns": self.source_timestamp_ns,
            "jpeg_sha256": self.jpeg_sha256,
            "jpeg_size": len(self.jpeg_bytes),
            "event_codes": list(self.event_codes),
            "authority": "advisory_only",
        }


class CameraRingBufferV1:
    """Fifteen-second, 1 Hz plus event ring with drop-oldest eviction."""

    def __init__(
        self,
        *,
        duration_s: float = RING_DURATION_S,
        regular_sample_hz: float = REGULAR_SAMPLE_HZ,
        max_frames: int = MAX_RING_FRAMES,
    ) -> None:
        if _finite(duration_s, "camera ring duration") != RING_DURATION_S:
            raise V5CameraObserverError("camera ring duration must remain 15 seconds")
        if _finite(regular_sample_hz, "camera regular sample rate") != REGULAR_SAMPLE_HZ:
            raise V5CameraObserverError("camera regular sample rate must remain 1 Hz")
        if type(max_frames) is not int or not 16 <= max_frames <= 256:
            raise V5CameraObserverError("camera ring max frame count is outside bounds")
        self.duration_ns = int(duration_s * 1_000_000_000)
        self.regular_period_ns = int(1_000_000_000 / regular_sample_hz)
        self.max_frames = max_frames
        self._frames: deque[CameraFrameV1] = deque()
        self._last_regular_ns: int | None = None
        self._last_offered_ns: int | None = None
        self.dropped_oldest = 0
        self.skipped_rate = 0

    @property
    def frames(self) -> tuple[CameraFrameV1, ...]:
        return tuple(self._frames)

    def offer(
        self,
        jpeg_bytes: bytes,
        *,
        monotonic_ns: int,
        source_timestamp_ns: int,
        event_codes: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        if self._last_offered_ns is not None and monotonic_ns < self._last_offered_ns:
            raise V5CameraObserverError("camera monotonic clock regressed")
        events = tuple(event_codes)
        self._last_offered_ns = monotonic_ns
        if not events and self._last_regular_ns is not None and monotonic_ns - self._last_regular_ns < self.regular_period_ns:
            self.skipped_rate += 1
            return {
                "schema": CAMERA_SCHEMA,
                "version": CAMERA_VERSION,
                "disposition": "SKIPPED_RATE",
                "captured": False,
                "drop_policy": "drop_oldest",
                "authority": "advisory_only",
            }
        frame = CameraFrameV1(monotonic_ns, source_timestamp_ns, jpeg_bytes, events)
        self._frames.append(frame)
        if not events:
            self._last_regular_ns = monotonic_ns
        cutoff = monotonic_ns - self.duration_ns
        while self._frames and (self._frames[0].monotonic_ns < cutoff or len(self._frames) > self.max_frames):
            self._frames.popleft()
            self.dropped_oldest += 1
        return {
            "schema": CAMERA_SCHEMA,
            "version": CAMERA_VERSION,
            "disposition": "CAPTURED_EVENT" if events else "CAPTURED_REGULAR",
            "captured": True,
            "frame": frame.metadata(),
            "drop_policy": "drop_oldest",
            "authority": "advisory_only",
        }

    def status(self, *, now_monotonic_ns: int, stale_after_s: float = STALE_AFTER_S) -> dict[str, Any]:
        stale = _finite(stale_after_s, "camera stale interval")
        if not 1.0 <= stale <= 10.0:
            raise V5CameraObserverError("camera stale interval is outside bounds")
        latest = None if not self._frames else self._frames[-1]
        if latest is None or now_monotonic_ns < latest.monotonic_ns or now_monotonic_ns - latest.monotonic_ns > int(stale * 1_000_000_000):
            state = CameraObservationStatusV1.UNKNOWN
            reason = "NO_FRAME" if latest is None else "STALE_FRAME"
        else:
            state = CameraObservationStatusV1.FRESH_ADVISORY
            reason = "FRESH_FRAME"
        return {
            "schema": CAMERA_SCHEMA,
            "version": CAMERA_VERSION,
            "status": state.value,
            "reason": reason,
            "latest_frame_sha256": None if latest is None else latest.jpeg_sha256,
            "latest_frame_monotonic_ns": None if latest is None else latest.monotonic_ns,
            "ring_frame_count": len(self._frames),
            "dropped_oldest": self.dropped_oldest,
            "skipped_rate": self.skipped_rate,
            "authority": "advisory_only",
            "controller_write_allowed": False,
            "safety_bypass_allowed": False,
        }


@dataclass(frozen=True)
class PrimitiveRecommendationV1:
    observation_status: CameraObservationStatusV1
    recommended_primitive: str | None
    reason_codes: tuple[str, ...]
    visual_evidence_sha256: tuple[str, ...]
    confidence: float
    issued_monotonic_ns: int
    expiry_monotonic_ns: int
    schema: str = RECOMMENDATION_SCHEMA
    version: int = CAMERA_VERSION

    def __post_init__(self) -> None:
        if self.schema != RECOMMENDATION_SCHEMA or self.version != CAMERA_VERSION or not isinstance(self.observation_status, CameraObservationStatusV1):
            raise V5CameraObserverError("primitive recommendation schema/status differs")
        confidence = _finite(self.confidence, "recommendation confidence")
        if not 0.0 <= confidence <= 1.0 or type(self.issued_monotonic_ns) is not int or type(self.expiry_monotonic_ns) is not int or self.expiry_monotonic_ns < self.issued_monotonic_ns:
            raise V5CameraObserverError("primitive recommendation bounds differ")
        reasons = tuple(self.reason_codes)
        evidence = tuple(self.visual_evidence_sha256)
        if not reasons or any(type(item) is not str or not item for item in reasons):
            raise V5CameraObserverError("primitive recommendation needs typed reasons")
        if any(type(item) is not str or len(item) != 64 or any(char not in "0123456789abcdef" for char in item) for item in evidence):
            raise V5CameraObserverError("primitive recommendation visual evidence differs")
        if self.observation_status is CameraObservationStatusV1.UNKNOWN and (self.recommended_primitive is not None or confidence != 0.0):
            raise V5CameraObserverError("UNKNOWN camera state cannot recommend a primitive")
        if self.recommended_primitive is not None and (not self.recommended_primitive or any(char in self.recommended_primitive for char in "\r\n")):
            raise V5CameraObserverError("recommended primitive is invalid")
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "reason_codes", reasons)
        object.__setattr__(self, "visual_evidence_sha256", evidence)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "observation_status": self.observation_status.value,
            "recommended_primitive": self.recommended_primitive,
            "reason_codes": list(self.reason_codes),
            "visual_evidence_sha256": list(self.visual_evidence_sha256),
            "confidence": self.confidence,
            "issued_monotonic_ns": self.issued_monotonic_ns,
            "expiry_monotonic_ns": self.expiry_monotonic_ns,
            "authority": "advisory_only",
            "controller_write_allowed": False,
            "safety_bypass_allowed": False,
        }


class CameraEvidenceStoreV1:
    """Durable recent ring with a bounded event retention policy."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        if self.root.is_symlink():
            raise V5CameraObserverError("camera evidence root must not be a symlink")
        self.ring_root = self.root / "ring"
        self.event_root = self.root / "events"
        for directory in (self.ring_root, self.event_root):
            if directory.is_symlink():
                raise V5CameraObserverError("camera evidence directory must not be a symlink")
            directory.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.root / "camera_evidence_manifest.json"
        self.quota_drop_count = 0
        self.evicted_file_count = 0

    @staticmethod
    def _name(frame: CameraFrameV1) -> str:
        return f"{frame.monotonic_ns:020d}-{frame.jpeg_sha256[:16]}.jpg"

    def sync(self, frames: tuple[CameraFrameV1, ...]) -> Path:
        ring_names: set[str] = set()
        newly_created: list[Path] = []
        for frame in frames:
            name = self._name(frame)
            ring_names.add(name)
            ring_path = self.ring_root / name
            if not ring_path.exists():
                _atomic_bytes(ring_path, frame.jpeg_bytes)
                newly_created.append(ring_path)
            if frame.event_codes:
                event_path = self.event_root / name
                if not event_path.exists():
                    _atomic_bytes(event_path, frame.jpeg_bytes)
                    newly_created.append(event_path)
        for candidate in self.ring_root.glob("*.jpg"):
            if candidate.name not in ring_names and not candidate.is_symlink():
                candidate.unlink()

        current_mono = max((frame.monotonic_ns for frame in frames), default=0)
        current_event_names = {
            self._name(frame): frame
            for frame in frames
            if frame.event_codes
        }
        pinned: list[Path] = []
        pinned_bytes = 0
        for path in sorted(self.event_root.glob("*.jpg"), key=lambda item: item.name, reverse=True):
            frame = current_event_names.get(path.name)
            if frame is None:
                try:
                    frame_mono = int(path.name.split("-", 1)[0])
                except (ValueError, IndexError):
                    frame_mono = -1
            else:
                frame_mono = frame.monotonic_ns
            if frame_mono < 0 or current_mono - frame_mono > EVENT_PIN_TTL_NS:
                continue
            size = path.stat().st_size
            if pinned_bytes + size > EVENT_PIN_MAX_BYTES:
                continue
            pinned.append(path)
            pinned_bytes += size

        image_files = [
            path
            for root in (self.ring_root, self.event_root)
            for path in root.glob("*.jpg")
            if not path.is_symlink()
        ]
        image_limit = max(0, HARD_QUOTA_BYTES - RETENTION_METADATA_RESERVE_BYTES)
        total_bytes = sum(path.stat().st_size for path in image_files)
        for path in sorted(image_files, key=lambda item: item.name):
            if total_bytes <= image_limit:
                break
            if path in pinned:
                continue
            size = path.stat().st_size
            path.unlink()
            total_bytes -= size
            self.evicted_file_count += 1
        # If every remaining image is pinned, the newest frame must be dropped
        # rather than allowing the hard quota to grow.  This is intentionally
        # a quota-drop receipt, not an eviction of an active event pin.
        while total_bytes > image_limit:
            droppable = [
                path
                for path in reversed(newly_created)
                if path.exists()
            ]
            if not droppable:
                raise V5CameraObserverError(
                    "camera hard quota cannot be satisfied without evicting an active pin"
                )
            path = droppable[0]
            if path in pinned:
                pinned.remove(path)
                pinned_bytes -= path.stat().st_size
            size = path.stat().st_size
            path.unlink()
            total_bytes -= size
            self.quota_drop_count += 1
        remaining_files = [
            path
            for root in (self.ring_root, self.event_root)
            for path in root.glob("*.jpg")
            if not path.is_symlink()
        ]
        total_bytes = sum(path.stat().st_size for path in remaining_files)

        rows: list[dict[str, Any]] = []
        for frame in frames:
            name = self._name(frame)
            ring_path = self.ring_root / name
            event_path = self.event_root / name
            if not ring_path.exists():
                self.quota_drop_count += 1
            rows.append({
                **frame.metadata(),
                "ring_path": str(ring_path.resolve()) if ring_path.exists() else None,
                "event_path": str(event_path.resolve()) if event_path.exists() else None,
            })
        manifest = {
            "schema": "step6.autotune/arm-camera-evidence-manifest-v2",
            "version": RETENTION_POLICY_VERSION,
            "source": RTSP_URL,
            "ring_duration_s": RING_DURATION_S,
            "regular_sample_hz": REGULAR_SAMPLE_HZ,
            "ring_policy": "drop_oldest",
            "event_frames_immutable": False,
            "retention_policy_version": RETENTION_POLICY_VERSION,
            "hard_quota_bytes": HARD_QUOTA_BYTES,
            "soft_frame_count_target": SOFT_FRAME_COUNT_TARGET,
            "bytes_used": total_bytes,
            "metadata_reserve_bytes": RETENTION_METADATA_RESERVE_BYTES,
            "metadata_bytes_reserved": True,
            "frame_count": len(remaining_files),
            "pinned_frame_count": len(pinned),
            "pinned_bytes": pinned_bytes,
            "evicted_file_count": self.evicted_file_count,
            "quota_drop_count": self.quota_drop_count,
            "frames": rows,
            "authority": "advisory_only",
            "controller_write_allowed": False,
            "safety_bypass_allowed": False,
        }
        manifest["content_sha256"] = _sha_bytes(_canonical(manifest))
        _atomic_json(self.manifest_path, manifest)
        return self.manifest_path


class ArmCameraObserverV1:
    def __init__(
        self,
        evidence_root: Path | str,
        *,
        classifier: Callable[[CameraFrameV1], Mapping[str, Any]] | None = None,
    ) -> None:
        self.ring = CameraRingBufferV1()
        self.store = CameraEvidenceStoreV1(evidence_root)
        self.classifier = classifier

    def observe(self, jpeg_bytes: bytes, *, monotonic_ns: int, source_timestamp_ns: int, event_codes: tuple[str, ...] = ()) -> dict[str, Any]:
        receipt = self.ring.offer(jpeg_bytes, monotonic_ns=monotonic_ns, source_timestamp_ns=source_timestamp_ns, event_codes=event_codes)
        if receipt["captured"]:
            receipt["manifest_path"] = str(self.store.sync(self.ring.frames).resolve())
        else:
            receipt["manifest_path"] = None
        return receipt

    def recommendation(self, *, now_monotonic_ns: int, ttl_s: float = 1.0) -> PrimitiveRecommendationV1:
        status = self.ring.status(now_monotonic_ns=now_monotonic_ns)
        expires = now_monotonic_ns + int(_finite(ttl_s, "recommendation TTL") * 1_000_000_000)
        if status["status"] == CameraObservationStatusV1.UNKNOWN.value:
            return PrimitiveRecommendationV1(CameraObservationStatusV1.UNKNOWN, None, (status["reason"],), (), 0.0, now_monotonic_ns, expires)
        latest = self.ring.frames[-1]
        if self.classifier is None:
            return PrimitiveRecommendationV1(CameraObservationStatusV1.FRESH_ADVISORY, None, ("INFERENCE_UNAVAILABLE",), (latest.jpeg_sha256,), 0.0, now_monotonic_ns, expires)
        result = self.classifier(latest)
        if not isinstance(result, Mapping):
            raise V5CameraObserverError("camera classifier did not return a typed mapping")
        return PrimitiveRecommendationV1(
            CameraObservationStatusV1.FRESH_ADVISORY,
            None if result.get("recommended_primitive") is None else str(result["recommended_primitive"]),
            tuple(result.get("reason_codes", ())),
            (latest.jpeg_sha256,),
            result.get("confidence", 0.0),
            now_monotonic_ns,
            expires,
        )


class FFmpegRTSPFrameSourceV1:
    """Exactly one ffmpeg reader for the loopback arm stream."""

    def __init__(self, *, url: str = RTSP_URL, lock_path: Path | str = "/tmp/autotuner-v5-arm-camera.lock") -> None:
        if url != RTSP_URL:
            raise V5CameraObserverError("camera source URL differs from the fixed loopback stream")
        self.url = url
        self.lock_path = Path(lock_path)
        self._lock_fd: int | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._buffer = bytearray()

    def start(self) -> None:
        if self._process is not None:
            raise V5CameraObserverError("camera reader is already started")
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(descriptor)
            raise V5CameraObserverError("another arm camera reader already owns the stream") from exc
        command = [
            "/usr/bin/ffmpeg",
            "-nostdin",
            "-loglevel",
            "error",
            "-rtsp_transport",
            "tcp",
            "-i",
            self.url,
            "-an",
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "pipe:1",
        ]
        try:
            self._process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
            raise
        self._lock_fd = descriptor

    @staticmethod
    def _extract_jpeg(buffer: bytearray) -> bytes | None:
        start = buffer.find(b"\xff\xd8")
        if start < 0:
            if len(buffer) > 1:
                del buffer[:-1]
            return None
        end = buffer.find(b"\xff\xd9", start + 2)
        if end < 0:
            if start:
                del buffer[:start]
            if len(buffer) > MAX_JPEG_BYTES:
                raise V5CameraObserverError("camera JPEG exceeds the bounded frame size")
            return None
        frame = bytes(buffer[start:end + 2])
        del buffer[:end + 2]
        return frame

    def read(self, *, timeout_s: float = 2.0) -> bytes | None:
        if self._process is None or self._process.stdout is None:
            raise V5CameraObserverError("camera reader is not started")
        timeout = _finite(timeout_s, "camera read timeout")
        if not 0.0 <= timeout <= 10.0:
            raise V5CameraObserverError("camera read timeout is outside bounds")
        ready = self._extract_jpeg(self._buffer)
        if ready is not None:
            return ready
        selector = selectors.DefaultSelector()
        try:
            selector.register(self._process.stdout, selectors.EVENT_READ)
            if not selector.select(timeout):
                return None
            chunk = os.read(self._process.stdout.fileno(), 64 * 1024)
        finally:
            selector.close()
        if not chunk:
            raise V5CameraObserverError("camera reader ended without a frame")
        self._buffer.extend(chunk)
        return self._extract_jpeg(self._buffer)

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2.0)
        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None

    def __enter__(self) -> "FFmpegRTSPFrameSourceV1":
        self.start()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()


__all__ = [
    "ArmCameraObserverV1",
    "CameraEvidenceStoreV1",
    "CameraFrameV1",
    "CameraObservationStatusV1",
    "CameraRingBufferV1",
    "FFmpegRTSPFrameSourceV1",
    "PrimitiveRecommendationV1",
    "RTSP_URL",
    "HARD_QUOTA_BYTES",
    "SOFT_FRAME_COUNT_TARGET",
    "EVENT_PIN_TTL_NS",
    "EVENT_PIN_MAX_BYTES",
    "RETENTION_POLICY_VERSION",
    "V5CameraObserverError",
]
