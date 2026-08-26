from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step6_figure8_autotune_v1.v5_camera_observer import (  # noqa: E402
    ArmCameraObserverV1,
    CameraObservationStatusV1,
    CameraRingBufferV1,
    FFmpegRTSPFrameSourceV1,
    RTSP_URL,
    V5CameraObserverError,
)
import step6_figure8_autotune_v1.v5_camera_observer as camera_module  # noqa: E402


def _jpeg(value: int) -> bytes:
    return b"\xff\xd8" + bytes([value % 256]) * 12 + b"\xff\xd9"


def test_camera_ring_is_15_seconds_one_hz_plus_events_and_drop_oldest():
    ring = CameraRingBufferV1()
    assert ring.offer(_jpeg(1), monotonic_ns=0, source_timestamp_ns=10)["captured"]
    assert ring.offer(_jpeg(2), monotonic_ns=500_000_000, source_timestamp_ns=20)["disposition"] == "SKIPPED_RATE"
    event = ring.offer(_jpeg(3), monotonic_ns=600_000_000, source_timestamp_ns=30, event_codes=("ROLLOVER",))
    assert event["disposition"] == "CAPTURED_EVENT"
    assert ring.offer(_jpeg(4), monotonic_ns=1_000_000_000, source_timestamp_ns=40)["captured"]
    ring.offer(_jpeg(5), monotonic_ns=16_100_000_000, source_timestamp_ns=50)
    assert all(frame.monotonic_ns >= 1_100_000_000 for frame in ring.frames)
    assert ring.dropped_oldest >= 3


def test_camera_stale_is_unknown_and_recommendation_has_no_authority(tmp_path: Path):
    observer = ArmCameraObserverV1(tmp_path / "camera")
    unknown = observer.recommendation(now_monotonic_ns=1_000_000_000)
    assert unknown.observation_status is CameraObservationStatusV1.UNKNOWN
    assert unknown.recommended_primitive is None and unknown.confidence == 0.0
    assert unknown.as_dict()["controller_write_allowed"] is False
    assert unknown.as_dict()["safety_bypass_allowed"] is False

    capture = observer.observe(_jpeg(7), monotonic_ns=2_000_000_000, source_timestamp_ns=100, event_codes=("HOME",))
    assert capture["captured"]
    fresh = observer.recommendation(now_monotonic_ns=2_100_000_000)
    assert fresh.observation_status is CameraObservationStatusV1.FRESH_ADVISORY
    assert fresh.recommended_primitive is None
    manifest = json.loads(Path(capture["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["ring_duration_s"] == 15.0
    assert manifest["regular_sample_hz"] == 1.0
    assert manifest["authority"] == "advisory_only"
    assert Path(manifest["frames"][0]["event_path"]).is_file()

    stale = observer.recommendation(now_monotonic_ns=5_000_000_001)
    assert stale.observation_status is CameraObservationStatusV1.UNKNOWN
    assert stale.reason_codes == ("STALE_FRAME",)


def test_camera_event_evidence_survives_ring_eviction(tmp_path: Path):
    observer = ArmCameraObserverV1(tmp_path / "camera")
    first = observer.observe(_jpeg(1), monotonic_ns=0, source_timestamp_ns=0, event_codes=("CHAIN_START",))
    first_manifest = json.loads(Path(first["manifest_path"]).read_text(encoding="utf-8"))
    event_path = Path(first_manifest["frames"][0]["event_path"])
    observer.observe(_jpeg(2), monotonic_ns=16_000_000_000, source_timestamp_ns=16_000_000_000)
    assert event_path.is_file()
    assert not any(path.name == event_path.name for path in (tmp_path / "camera" / "ring").glob("*.jpg"))


def test_camera_disk_retention_enforces_total_quota_and_reports_eviction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(camera_module, "HARD_QUOTA_BYTES", 40)
    monkeypatch.setattr(camera_module, "RETENTION_METADATA_RESERVE_BYTES", 0)
    monkeypatch.setattr(camera_module, "EVENT_PIN_MAX_BYTES", 0)
    observer = ArmCameraObserverV1(tmp_path / "camera")
    observer.observe(_jpeg(1), monotonic_ns=0, source_timestamp_ns=0, event_codes=("A",))
    second = observer.observe(_jpeg(2), monotonic_ns=16_000_000_000, source_timestamp_ns=16_000_000_000, event_codes=("B",))
    manifest = json.loads(Path(second["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["retention_policy_version"] == 2
    assert manifest["hard_quota_bytes"] == 40
    assert manifest["bytes_used"] <= 40
    assert manifest["evicted_file_count"] > 0 or manifest["quota_drop_count"] > 0


def test_camera_quota_drops_new_frame_when_only_pins_remain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(camera_module, "HARD_QUOTA_BYTES", 30)
    monkeypatch.setattr(camera_module, "RETENTION_METADATA_RESERVE_BYTES", 0)
    monkeypatch.setattr(camera_module, "EVENT_PIN_MAX_BYTES", 1 << 20)
    observer = ArmCameraObserverV1(tmp_path / "camera")
    observer.observe(_jpeg(1), monotonic_ns=0, source_timestamp_ns=0, event_codes=("PIN",))
    second = observer.observe(
        _jpeg(2), monotonic_ns=16_000_000_000, source_timestamp_ns=16_000_000_000, event_codes=("PIN2",)
    )
    manifest = json.loads(Path(second["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["quota_drop_count"] > 0
    image_bytes = sum(
        path.stat().st_size
        for directory in (tmp_path / "camera" / "ring", tmp_path / "camera" / "events")
        for path in directory.glob("*.jpg")
    )
    assert image_bytes <= 30


def test_ffmpeg_source_is_fixed_single_url_and_jpeg_parser_is_bounded(tmp_path: Path):
    with pytest.raises(V5CameraObserverError):
        FFmpegRTSPFrameSourceV1(url="rtsp://example.invalid/arm")
    source = FFmpegRTSPFrameSourceV1(url=RTSP_URL, lock_path=tmp_path / "camera.lock")
    buffer = bytearray(b"noise" + _jpeg(9) + b"tail")
    assert source._extract_jpeg(buffer) == _jpeg(9)
    assert bytes(buffer) == b"tail"


def test_camera_source_has_no_controller_or_sensor_transport_imports():
    source = (ROOT / "tools" / "step6_figure8_autotune_v1" / "v5_camera_observer.py").read_text(encoding="utf-8").lower()
    for forbidden in ("import rtde", "import kunwei", "ur_robot_driver", "ros2_control", "tell_exact("):
        assert forbidden not in source
