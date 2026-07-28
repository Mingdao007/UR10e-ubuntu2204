"""Focused offline proof for the v3 observation/recording seam."""

from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
import time
from dataclasses import replace

import pytest

from ur10e_vic.tacdiffusion.action import (
    TacDiffusionAction,
    apply_guard_policy,
)
from ur10e_vic.tacdiffusion.eligibility import EligibilityValidator
from ur10e_vic.tacdiffusion.episode_recorder import (
    BATCH_SIZE,
    DURABILITY_MODE,
    MAX_UNSEALED_TAIL,
    RECORDER_HEALTH_SCHEMA,
    SPOOL_CAPACITY,
    BoundedEpisodeSpool,
    BatchFsync10Sealer,
    EpisodeFrameV2,
    EpisodeRecorder,
    RecorderHealth,
    read_episode_artifact,
    read_recorder_health,
    validate_sealed_episode_manifest,
)
from ur10e_vic.tacdiffusion.observation import (
    OBSERVATION_V3_SCHEMA_VERSION,
    ObservationLineage,
    ObservationSlice,
    TacDiffusionObservation,
)
from ur10e_vic.tacdiffusion.offline_geometry import (
    ANCHOR_FAMILY,
    CIRCLE_FAMILY,
    LINE_FAMILY,
    anchor_circle_2s,
    build_offline_reference_set,
    full_circle_radius_0_5mm_8s,
    line_out_and_back_4s,
)
from ur10e_vic.tacdiffusion.signals import (
    CanonicalWrenchSample,
    causal_sync_wrench_1khz_to_control_500hz,
)
from ur10e_vic.tacdiffusion.unknown_surface_episode import load_unknown_surface_tube


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SURFACE_MANIFEST = PACKAGE_ROOT / "config" / "tacdiffusion_surface_input_20260726.json"
ANCHOR = (0.463, 0.172, 0.045, -3.14155, 0.0058, 0.0)
SHA = "a" * 64


def _sample(
    sequence: int,
    *,
    device_time: float,
    host_time: float,
    batch_id: int,
    sample_index: int,
) -> CanonicalWrenchSample:
    return CanonicalWrenchSample(
        sequence=sequence,
        timestamp_s=device_time,
        device_time_s=device_time,
        host_visible_time_s=host_time,
        batch_id=batch_id,
        sample_index=sample_index,
        wrench_tcp_si=(float(sequence), 0.0, 0.0, 0.0, 0.0, 0.0),
        frame_id="tool0_tcp",
        calibration_sha256=SHA,
    )


def _frame(
    index: int,
    *,
    control_time_s: float | None = None,
    external_sample_index: int | None = None,
    valid: bool = True,
    expert_equals_applied: bool = True,
) -> EpisodeFrameV2:
    action = tuple(float(index + axis) for axis in range(12))
    applied = action if expert_equals_applied else tuple(0.0 for _ in range(12))
    control_time = 0.002 * (index + 1) if control_time_s is None else control_time_s
    source_index = index if external_sample_index is None else external_sample_index
    return EpisodeFrameV2(
        episode_id="episode-offline",
        sample_index=index,
        control_sequence=index,
        control_time_s=control_time,
        observation_84d=(float(index),) * 84,
        expert_action_12d=action,
        applied_action_12d=applied,
        echoed_action_12d=action,
        action_generation=1,
        action_age_ticks=0,
        action_echo_coherent=valid,
        external_device_time_s=0.001 * source_index,
        external_host_visible_time_s=control_time - 0.001,
        external_batch_id=source_index,
        external_sample_index=source_index,
        external_hold=False,
        external_held_ticks=0,
        device_age_samples=0,
        host_age_s=0.001,
        internal_wrench_valid=valid,
        external_lineage_valid=valid,
        source_row_torn=not valid,
        source_row_invalid=not valid,
        echoed_action_valid=valid,
        recorder_valid=valid,
    )


def _healthy(*, sealed: bool = True, tamper_free: bool = True, **overrides: object) -> RecorderHealth:
    values: dict[str, object] = {
        "capacity": SPOOL_CAPACITY,
        "queue_depth": 0,
        "enqueued_rows": 2,
        "durable_rows": 2,
        "rejected_rows": 0,
        "dropped_rows": 0,
        "overflowed": False,
        "stalled": False,
        "writer_error": None,
        "fault": None,
        "durability_mode": DURABILITY_MODE,
        "unsealed_tail": 0,
        "max_unsealed_tail": MAX_UNSEALED_TAIL,
        "sealed": sealed,
        "manifest_written": sealed,
        "tamper_free": tamper_free,
    }
    values.update(overrides)
    return RecorderHealth(**values)


def test_dual_clock_joiner_uses_host_visibility_and_explicit_holds() -> None:
    samples = (
        _sample(0, device_time=10.000, host_time=0.080, batch_id=4, sample_index=0),
        _sample(1, device_time=10.001, host_time=0.080, batch_id=4, sample_index=1),
        _sample(2, device_time=10.002, host_time=0.082, batch_id=5, sample_index=2),
    )
    aligned = causal_sync_wrench_1khz_to_control_500hz(
        samples,
        (0.080, 0.082, 0.084),
        expected_frame_id="tool0_tcp",
        expected_calibration_sha256=SHA,
    )
    assert [row.source_sequence for row in aligned] == [1, 2, 2]
    assert [row.external_hold for row in aligned] == [False, False, True]
    assert aligned[-1].external_held_ticks == 1
    assert aligned[-1].device_age_samples == 2
    assert aligned[-1].host_age_s == pytest.approx(0.002)
    assert aligned[0].source_device_time_s == pytest.approx(10.001)
    assert aligned[0].source_host_visible_time_s == pytest.approx(0.080)
    with pytest.raises(ValueError, match="causally available"):
        causal_sync_wrench_1khz_to_control_500hz(
            samples,
            (0.079, 0.081),
            expected_frame_id="tool0_tcp",
            expected_calibration_sha256=SHA,
        )


def test_v3_observation_preserves_device_clock_when_host_clock_is_causal() -> None:
    samples = (
        _sample(0, device_time=20.000, host_time=0.080, batch_id=7, sample_index=0),
        _sample(1, device_time=20.001, host_time=0.082, batch_id=8, sample_index=1),
    )
    aligned = causal_sync_wrench_1khz_to_control_500hz(
        samples,
        (0.080, 0.082),
        expected_frame_id="tool0_tcp",
        expected_calibration_sha256=SHA,
    )
    lineage = ObservationLineage("tool0_tcp", "tool0_tcp", SHA, "b" * 64)

    def make_slice(index: int) -> ObservationSlice:
        return ObservationSlice.from_causal_alignment(
            sequence=index + 1,
            control_timestamp_s=aligned[index].control_timestamp_s,
            alignment=aligned[index],
            internal_wrench=(0.0,) * 6,
            actual_ee_twist=(0.0,) * 6,
            desired_pose=(0.0,) * 6,
            desired_twist=(0.0,) * 6,
            desired_acceleration=(0.0,) * 6,
            tracking_error=(0.0,) * 6,
            lineage=lineage,
            internal_wrench_valid=index == 0,
        )

    observation = TacDiffusionObservation(make_slice(0), make_slice(1))
    assert observation.schema_version == OBSERVATION_V3_SCHEMA_VERSION
    assert observation.current.external_device_time_s == pytest.approx(20.001)
    assert observation.current.external_host_visible_time_s == pytest.approx(0.082)
    assert observation.current.external_hold is False
    assert observation.current.internal_wrench_valid is False
    assert observation.vector and len(observation.vector) == 84


def test_spool_is_bounded_nonblocking_and_never_overwrites() -> None:
    spool = BoundedEpisodeSpool(capacity=2)
    assert spool.enqueue(_frame(0))
    assert spool.enqueue(_frame(1))
    assert not spool.enqueue(_frame(2))
    assert spool.fault == "spool_overflow"
    assert spool.rejected_rows == 1
    assert spool.depth == 2
    assert [spool.get(0.0).sample_index, spool.get(0.0).sample_index] == [0, 1]
    assert all(value >= 0.0 for value in spool.enqueue_latency_us)


def test_episode_v2_retains_hold_lineage_and_downgrades_non_strict_control_time(
    tmp_path: Path,
) -> None:
    recorder = EpisodeRecorder(tmp_path, episode_id="held-lineage")
    recorder.start()
    first = _frame(0, external_sample_index=7)
    held = replace(
        _frame(1, external_sample_index=7),
        external_device_time_s=first.external_device_time_s,
        external_host_visible_time_s=first.external_host_visible_time_s,
        external_batch_id=first.external_batch_id,
    )
    non_strict = _frame(2, control_time_s=held.control_time_s)
    assert recorder.enqueue(first)
    assert recorder.enqueue(held)
    assert recorder.enqueue(non_strict)
    recorder.close(seal=True)
    _, retained = read_episode_artifact(recorder.artifact_path)
    assert retained[1]["external_hold"] is True
    assert retained[1]["external_held_ticks"] == 1
    assert retained[1]["device_age_samples"] == 2
    assert retained[2]["control_time_strict"] is False


def test_spool_enqueue_latency_meets_offline_budget() -> None:
    spool = BoundedEpisodeSpool(capacity=2048)
    for index in range(1000):
        assert spool.enqueue(_frame(index))
    latencies = sorted(spool.enqueue_latency_us)
    p99 = latencies[int(len(latencies) * 0.99) - 1]
    assert p99 <= 100.0
    assert max(latencies) <= 500.0


def test_complete_episode_producer_seam_meets_offline_budget(tmp_path: Path) -> None:
    recorder = EpisodeRecorder(
        tmp_path,
        episode_id="producer-latency",
        capacity=2048,
    )
    recorder.start()
    latencies: list[float] = []
    started_run = time.perf_counter()
    for index in range(500):
        frame = _frame(index)
        started = time.perf_counter()
        assert recorder.enqueue(frame)
        latencies.append((time.perf_counter() - started) * 1e6)
        deadline = started_run + (index + 1) * 0.002
        remaining = deadline - time.perf_counter()
        if remaining > 0.0:
            time.sleep(remaining)
    recorder.close(seal=True)
    latencies.sort()
    p99 = latencies[int(len(latencies) * 0.99) - 1]
    assert p99 <= 100.0
    assert max(latencies) <= 500.0


def test_batch_fsync_seal_manifest_and_tamper_detection(tmp_path: Path) -> None:
    recorder = EpisodeRecorder(tmp_path, episode_id="episode-offline")
    batch_sizes: list[int] = []
    original_write_batch = recorder.sealer._write_batch

    def record_batch_size(batch: object) -> None:
        batch_sizes.append(len(batch))  # type: ignore[arg-type]
        original_write_batch(batch)  # type: ignore[arg-type]

    recorder.sealer._write_batch = record_batch_size  # type: ignore[method-assign]
    recorder.start()
    for index in range(21):
        assert recorder.enqueue(_frame(index))
        # A paced 500 Hz producer exposed the old per-row-fsync bug.  The
        # sealer must retain a partial tail until ten rows or explicit close.
        time.sleep(0.002)
    manifest = recorder.close(seal=True)
    assert manifest is not None
    health = recorder.health()
    assert health.sealed and health.manifest_written and health.tamper_free
    assert health.durable_rows == 21
    assert health.durability_mode == DURABILITY_MODE
    assert health.max_unsealed_tail <= MAX_UNSEALED_TAIL
    assert batch_sizes == [10, 10, 1]
    header, rows = read_episode_artifact(recorder.artifact_path)
    assert header["batch_size"] == BATCH_SIZE
    assert len(rows) == 21
    assert validate_sealed_episode_manifest(recorder.artifact_path, recorder.manifest_path) == manifest
    health_receipt = read_recorder_health(recorder.health_path)
    assert health_receipt["schema"] == RECORDER_HEALTH_SCHEMA
    assert health_receipt["health"]["sealed"] is True
    with recorder.artifact_path.open("ab") as handle:
        handle.write(b"tamper\n")
    with pytest.raises(ValueError, match="hash mismatch"):
        validate_sealed_episode_manifest(recorder.artifact_path, recorder.manifest_path)
    assert recorder.health(validate_tamper=True).tamper_free is False


def test_batch_fsync_stall_and_writer_crash_are_latched(tmp_path: Path) -> None:
    stall_spool = BoundedEpisodeSpool(capacity=32)
    stall = BatchFsync10Sealer(
        stall_spool,
        tmp_path / "stall.jsonl",
        tmp_path / "stall.manifest.json",
        episode_id="stall",
        stall_timeout_s=0.01,
    )
    original_write = stall._write_batch

    def slow_write(batch: object) -> None:
        time.sleep(0.05)
        original_write(batch)  # type: ignore[arg-type]

    stall._write_batch = slow_write  # type: ignore[method-assign]
    stall.start()
    for index in range(11):
        assert stall_spool.enqueue(_frame(index))
    deadline = time.monotonic() + 1.0
    while stall.poll_fault() is None and time.monotonic() < deadline:
        time.sleep(0.002)
    assert stall.poll_fault() == "writer_stall"
    stall.close()
    assert stall.health().stalled is True

    crash_spool = BoundedEpisodeSpool(capacity=16)
    crash = BatchFsync10Sealer(
        crash_spool,
        tmp_path / "crash.jsonl",
        tmp_path / "crash.manifest.json",
        episode_id="crash",
    )

    def crash_write(_batch: object) -> None:
        raise OSError("synthetic writer crash")

    crash._write_batch = crash_write  # type: ignore[method-assign]
    crash.start()
    for index in range(BATCH_SIZE):
        assert crash_spool.enqueue(_frame(index))
    deadline = time.monotonic() + 1.0
    while crash.poll_fault() is None and time.monotonic() < deadline:
        time.sleep(0.002)
    assert crash.poll_fault() is not None
    assert crash.poll_fault().startswith("writer_error:")
    crash.close()
    assert not (tmp_path / "crash.manifest.json").exists()


def test_stall_clock_starts_when_work_arrives_not_at_construction(tmp_path: Path) -> None:
    spool = BoundedEpisodeSpool(capacity=16)
    sealer = BatchFsync10Sealer(
        spool,
        tmp_path / "idle.jsonl",
        tmp_path / "idle.manifest.json",
        episode_id="idle",
        stall_timeout_s=0.01,
    )
    sealer.start()
    time.sleep(0.02)
    assert spool.enqueue(_frame(0))
    assert sealer.poll_fault() is None
    sealer.close()
    assert sealer.poll_fault() is None


def test_manifest_creation_is_atomic_and_never_overwrites(tmp_path: Path) -> None:
    manifest = tmp_path / "episode_v2.manifest.json"
    manifest.write_text("preserve", encoding="utf-8")
    recorder = EpisodeRecorder(tmp_path, episode_id="atomic")
    with pytest.raises(FileExistsError):
        recorder.start()
    assert manifest.read_text(encoding="utf-8") == "preserve"


def test_eligibility_is_the_only_final_verdict_and_downgrades_rows(tmp_path: Path) -> None:
    validator = EligibilityValidator()
    frames = (_frame(0), _frame(1))
    shadow = validator.evaluate(frames, recorder_health=_healthy(), first_live_shadow=True)
    assert not shadow.training_eligible
    assert "first_live_shadow_forced_ineligible" in shadow.reasons

    eligible = validator.evaluate(frames, recorder_health=_healthy(), first_live_shadow=False)
    assert eligible.training_eligible
    assert not eligible.live_ready
    filtered_echo = replace(
        _frame(1),
        echoed_action_12d=(99.0,) * 12,
        action_echo_coherent=True,
        echoed_action_valid=True,
    )
    assert validator.evaluate(
        (_frame(0), filtered_echo),
        recorder_health=_healthy(),
        first_live_shadow=False,
    ).training_eligible
    receipt = validator.write_receipt(tmp_path / "eligibility.json", eligible, recorder_health=_healthy())
    assert receipt["training_eligible"] is True
    assert validator.evaluate(
        (_frame(0, valid=False), _frame(1)),
        recorder_health=_healthy(),
        first_live_shadow=False,
    ).training_eligible is False
    non_strict_order = (
        _frame(0),
        replace(_frame(1), sample_index=0, control_sequence=0),
    )
    assert validator.evaluate(
        non_strict_order,
        recorder_health=_healthy(),
        first_live_shadow=False,
    ).predicates["strict_control_time"] is False
    missing_health = validator.evaluate(
        frames,
        recorder_health={},
        first_live_shadow=False,
    )
    assert not missing_health.training_eligible
    assert not missing_health.predicates["no_spool_overflow"]
    assert not missing_health.predicates["complete_seal"]
    undurable = validator.evaluate(
        frames,
        recorder_health=_healthy(
            enqueued_rows=2,
            durable_rows=1,
            unsealed_tail=1,
        ),
        first_live_shadow=False,
    )
    assert not undurable.predicates["complete_seal"]
    with pytest.raises(FileExistsError):
        validator.write_receipt(tmp_path / "eligibility.json", eligible)


def test_runner_seam_keeps_recorder_out_of_direct_torque_controller() -> None:
    direct_torque = (
        Path(__file__).resolve().parents[1]
        / "ur10e_vic"
        / "tacdiffusion"
        / "direct_torque_live_v4.py"
    ).read_text(encoding="utf-8")
    runner = (
        Path(__file__).resolve().parents[2]
        / "tase-contact-reproduction"
        / "tools"
        / "run_tacdiffusion_remote_direct_torque_v4.py"
    ).read_text(encoding="utf-8")
    assert "episode_recorder" not in direct_torque
    assert "TaskExecutor" in runner and "EpisodeRecorder" in runner
    enqueue_source = inspect.getsource(EpisodeRecorder.enqueue)
    assert all(token not in enqueue_source for token in ("json", "fsync", "open(", ".write("))


def test_empty_and_disabled_guard_policies_are_identity() -> None:
    action = TacDiffusionAction((1.0,) * 6, (2.0,) * 6)
    calls: list[int] = []

    def unexpected(value: TacDiffusionAction) -> TacDiffusionAction:
        calls.append(1)
        return TacDiffusionAction((9.0,) * 6, (9.0,) * 6)

    assert apply_guard_policy(action) is action
    assert apply_guard_policy(action, (unexpected,), enabled=False) is action
    assert calls == []
    assert apply_guard_policy(action, (unexpected,)).vector12 == (9.0,) * 12


def test_geometry_references_are_hash_bound_inside_tube_and_no_motion() -> None:
    tube = load_unknown_surface_tube(
        SURFACE_MANIFEST,
        anchor_pose_base=ANCHOR,
        normal_half_width_m=0.002,
    )
    references = build_offline_reference_set(tube, ANCHOR)
    assert set(references) == {ANCHOR_FAMILY, LINE_FAMILY, CIRCLE_FAMILY}
    line = line_out_and_back_4s(tube, ANCHOR)
    circle = full_circle_radius_0_5mm_8s(tube, ANCHOR)
    anchor = anchor_circle_2s(tube, ANCHOR)
    assert len(line.samples) == 2001
    assert len(circle.samples) == 4001
    assert len(anchor.samples) == 1001
    assert anchor.samples[0].u_offset_m == pytest.approx(0.0)
    assert anchor.samples[-1].u_offset_m == pytest.approx(0.0)
    assert line.samples[0].u_offset_m == pytest.approx(0.0)
    assert line.samples[500].u_offset_m == pytest.approx(0.0005)
    assert line.samples[1500].u_offset_m == pytest.approx(-0.0005)
    assert line.samples[-1].u_offset_m == pytest.approx(0.0)
    assert max(abs(sample.u_offset_m) for sample in line.samples) == pytest.approx(0.0005)
    assert max(
        ((sample.u_offset_m + 0.0005) ** 2 + sample.v_offset_m**2) ** 0.5
        for sample in circle.samples
    ) == pytest.approx(0.0005)
    assert all(sample.pose_base[3:] == pytest.approx(ANCHOR[3:]) for sample in circle.samples)
    assert all(reference.numeric_sanity["tube_rejects_one_um_outside"] for reference in references.values())
    assert all(reference.as_json()["motion_enabled"] is False for reference in references.values())
    assert all(reference.surface_manifest_sha256 == tube.surface_manifest_sha256 for reference in references.values())
    for reference in references.values():
        assert all(
            abs(sample.u_offset_m) <= tube.safe_u_half_width_m
            and abs(sample.v_offset_m) <= tube.safe_v_half_width_m
            for sample in reference.samples
        )
