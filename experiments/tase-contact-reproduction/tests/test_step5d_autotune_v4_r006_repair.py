"""Repair-turn proofs for the r006 production adapters and raw boundaries."""

from __future__ import annotations

import gzip
import hashlib
import html
import inspect
import os
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT.parents[1] / "src" / "ur10e_experiment_runtime"))

from step5d_autotune_v4_r004.motion_profile import R004_MOTION_PROFILE  # noqa: E402
from step5d_autotune_v4_r004 import wire as r004_wire  # noqa: E402
from step5d_autotune_v4_r004.evidence import PathSample  # noqa: E402
from step5d_autotune_v4_r005.observations import ObservationLedger  # noqa: E402
from step5d_autotune_v4_r005.contracts import Candidate as R005Candidate  # noqa: E402
from step5d_autotune_v4_r006 import live_adapter as r006_live  # noqa: E402
from step5d_autotune_v4_r006.contracts import load_contract  # noqa: E402
from step5d_autotune_v4_r006.lattice import (  # noqa: E402
    ANCHOR_POINT,
    IMode,
    ParameterPoint,
    transition_is_legal,
)
from step5d_autotune_v4_r006.live_adapter import (  # noqa: E402
    R006Candidate,
    R006LiveAdapter,
    R006LiveWriter,
    R006MatureWriter,
    R006NativeV3DurableQueueAdapter,
    R006ObservationLedger,
    R006PathEvidenceCollector,
    R006ProductionQueue,
    _candidate_from_point,
    _point_from_candidate,
)
from step5d_autotune_v4_r006.queue import R006V3DurableQueueAdapter  # noqa: E402
from step5d_autotune_v4_r006.sidecar import R006ObjectiveSidecar, R006SidecarError  # noqa: E402
from step5d_autotune_v4_r006.motion_profile import (  # noqa: E402
    ACTIVE_MOTION_ENVELOPE_V2,
    PATH_SNAPSHOT_BINDING,
    r006_runtime_path_reference,
)
from step5d_autotune_v4_r006.objective import (  # noqa: E402
    R006ObjectiveBuilder,
    R006ObjectiveError,
    build_receipt_from_samples,
)
from step5d_autotune_v4_r006.optimizer import R006CudaQLogNEI  # noqa: E402
from step5d_autotune_v4_r006.parent import load_frozen_r005_contract  # noqa: E402
from step5d_autotune_v4_r006.runtime import RuntimeThresholds  # noqa: E402
from step5d_autotune_v4_r006.tp import (  # noqa: E402
    R006_STAMP,
    numeric_sanity,
    validate_triplet,
)

try:
    from step5d_autotune_v4_r004.calibrated_runtime import V4CalibratedRuntime  # noqa: E402
except (ImportError, AttributeError):  # ROS/Pinocchio is not in the host test lane.
    V4CalibratedRuntime = None  # type: ignore[assignment,misc]


def _sample(path_time_s: float, force_n: float, sequence: int):
    from step5d_force_objective import ForcePathSample

    return ForcePathSample(
        path_time_s=path_time_s,
        path_phase=25,
        filtered_normal_n=force_n,
        source_sequences={"controller": sequence, "rtde": sequence},
        source_ages_s={"controller": 0.001, "rtde": 0.001},
        timestamp_s=1000.0 + path_time_s,
    )


def _complete_samples(force_n: float, *, sequence_offset: int = 0):
    samples = [
        _sample(0.05 + index * 0.1, force_n, sequence_offset + index + 1)
        for index in range(550)
    ]
    samples.extend(
        _sample(55.05 + index * 0.1, force_n, sequence_offset + 550 + index + 1)
        for index in range(50)
    )
    return tuple(samples)


def _receipt(contract, point: ParameterPoint, sequence: int, *, force_n: float = 5.25):
    return build_receipt_from_samples(
        _complete_samples(force_n, sequence_offset=sequence * 10_000),
        attempt_sequence=sequence,
        execution_id=f"r006-repair-execution-{sequence}",
        campaign_fingerprint=contract.campaign_fingerprint,
        candidate_uid=point.uid,
    )


def test_real_r005_derived_triplet_is_gzip_cached_exact_and_allowlisted() -> None:
    contract = load_contract()
    package_root = ROOT / "programs/step5/step5d"
    stem = package_root / contract.program
    script = stem.with_suffix(".script").read_text(encoding="utf-8")
    txt = stem.with_suffix(".txt").read_text(encoding="utf-8")
    urp = stem.with_suffix(".urp").read_bytes()
    checks = validate_triplet(script, txt, urp, R006_STAMP, contract)
    assert all(checks.values())
    assert numeric_sanity(script, ACTIVE_MOTION_ENVELOPE_V2)["passed"]
    assert urp[:2] == b"\x1f\x8b"
    xml = ET.fromstring(gzip.decompress(urp).decode("utf-8"))
    assert xml.tag == "URProgram"
    cached = next(
        html.unescape(node.text or "")
        for node in xml.iter()
        if node.tag.rsplit("}", 1)[-1] == "cachedContents"
    )
    assert cached == script
    assert script.count("codex_r006_finite(qdot, 5.000000000)") == 1
    assert script.count("speedj(") == 3
    assert script.count("40.000000000") >= 3
    assert "r005" not in script.lower()
    assert "return 0" not in script[script.index("def step5d_strict_rnn_autotune_v4_r006") :]
    manifest = json.loads(stem.with_suffix(".deploy-manifest.json").read_text(encoding="utf-8"))
    assert manifest["same_basename_triplet"] is True
    assert manifest["controller_directory"] == "/programs/andyl/kunwei/step5"
    assert manifest["timestamp"].endswith("HKT_STEP5D_AUTOTUNE_V4_R006")
    for suffix in (".script", ".txt", ".urp"):
        assert manifest["artifacts"][suffix.lstrip(".")]["sha256"] == hashlib.sha256(
            stem.with_suffix(suffix).read_bytes()
        ).hexdigest()


def test_runtime_path_snapshot_is_bound_and_consumed_by_calibrated_runtime() -> None:
    contract = load_contract()
    assert contract.raw["motion"]["path_snapshot"] == PATH_SNAPSHOT_BINDING
    assert ACTIVE_MOTION_ENVELOPE_V2.as_dict["path_reference_binding"] == PATH_SNAPSHOT_BINDING
    calls: list[tuple[str, float]] = []

    def injected(stage_id, pose_xy, elapsed_s):
        calls.append((stage_id, float(elapsed_s)))
        return {
            "desired_xy": tuple(float(value) for value in pose_xy),
            "desired_velocity_xy": (0.0, 0.0),
            "path_error_xy": (0.0, 0.0),
        }

    if V4CalibratedRuntime is not None:
        runtime = object.__new__(V4CalibratedRuntime)
        runtime.path_reference = injected
        runtime.path_errors(
            actual_tcp_pose=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            path_time_s=3.0,
            motion_kp=1.5,
        )
        assert calls == [("step5d_strict_rnn_autotune_v1", 3.0)]
    else:
        source = (ROOT / "tools/step5d_autotune_v4_r004/calibrated_runtime.py").read_text(encoding="utf-8")
        assert source.count("reference = step5_path_reference(") == 2
    assert r006_runtime_path_reference("step5d_strict_rnn_autotune_v1", (0.0, 0.0), 3.0)["stage_id"] == "step5d_strict_rnn_autotune_v1"
    assert not hasattr(R004_MOTION_PROFILE, "active_cap_override")


def test_production_v3_queue_preserves_two_plus_one_and_ordinal_continuity(tmp_path: Path) -> None:
    profile = ROOT / "config/step5/step5d_autotune_v4_r006_launch_profile.json"
    queue = R006V3DurableQueueAdapter(
        tmp_path / "queue",
        campaign_id="a" * 64,
        launch_profile_path=profile,
        release_manifest_sha256="b" * 64,
    )
    queue.bind_home(campaign_epoch=1, last_trial_id=0, last_command_seq=0)
    first = queue.enqueue(R006ProductionQueue.candidate(ANCHOR_POINT), kind="WARM_START_1", epoch=1)
    ticket = queue.prepare_next()
    assert ticket is not None and ticket.entry.request_uid == first.request_uid
    queue.record_execution(ticket, attempt_sequence=4, execution_id="r006-execution-4")
    queue.complete(ticket, status="SUCCEEDED")
    queue.enqueue(
        R006ProductionQueue.candidate(ANCHOR_POINT.with_step("P", 1)),
        kind="WARM_START_1",
        epoch=1,
    )
    next_ticket = queue.prepare_next()
    assert next_ticket is not None
    queue.record_execution(next_ticket, attempt_sequence=5, execution_id="r006-execution-5")
    assert queue.last_attempt_sequence == 5


def test_production_sidecar_cold_read_rejects_inline_forgery_and_tamper(tmp_path: Path) -> None:
    contract = load_contract()
    sidecar = R006ObjectiveSidecar(
        tmp_path / "r006-objectives.jsonl",
        campaign_fingerprint=contract.campaign_fingerprint,
    )
    point = ANCHOR_POINT
    receipt = _receipt(contract, point, 1)
    with pytest.raises(R006SidecarError, match="typed builder receipt"):
        sidecar.append(
            {"objective_mae_n": 0.0},
            epoch=1,
            candidate_uid=point.uid,
            kind="WARM_START_1",
            point_key=list(point.key),
        )
    row = sidecar.append(
        receipt,
        epoch=1,
        candidate_uid=point.uid,
        kind="WARM_START_1",
        point_key=list(point.key),
    )
    duplicate = sidecar.append(
        receipt,
        epoch=1,
        candidate_uid=point.uid,
        kind="WARM_START_1",
        point_key=list(point.key),
    )
    assert duplicate["attempt_sequence"] == row["attempt_sequence"]
    assert len(sidecar.fresh_process_verify()) == 1
    artifact = Path(row["artifact_path"])
    corrupted = bytearray(artifact.read_bytes())
    corrupted[-2] = ord("0") if corrupted[-2] != ord("0") else ord("1")
    artifact.write_bytes(bytes(corrupted))
    with pytest.raises(R006SidecarError):
        sidecar.fresh_process_verify()


def test_incomplete_v3_dispatch_reconciles_same_logical_uid_with_new_execution(tmp_path: Path) -> None:
    profile = ROOT / "config/step5/step5d_autotune_v4_r006_launch_profile.json"
    queue_root = tmp_path / "queue"
    queue = R006V3DurableQueueAdapter(
        queue_root,
        campaign_id="c" * 64,
        launch_profile_path=profile,
        release_manifest_sha256="d" * 64,
    )
    queue.bind_home(campaign_epoch=1, last_trial_id=0, last_command_seq=0)
    queue.enqueue(R006ProductionQueue.candidate(ANCHOR_POINT), kind="WARM_START_1", epoch=1)
    ticket = queue.prepare_next()
    assert ticket is not None
    logical_uid = ticket.entry.logical_uid
    queue.record_execution(ticket, attempt_sequence=4, execution_id="r006-incomplete-4")
    resumed_queue = R006V3DurableQueueAdapter(
        queue_root,
        campaign_id="c" * 64,
        launch_profile_path=profile,
        release_manifest_sha256="d" * 64,
    )
    resumed = resumed_queue.reconcile_inflight_after_home()
    assert resumed is not None and resumed.logical_uid == logical_uid
    resumed_ticket = resumed_queue.prepare_next()
    assert resumed_ticket is not None and resumed_ticket.entry.logical_uid == logical_uid
    resumed_queue.record_execution(resumed_ticket, attempt_sequence=5, execution_id="r006-resumed-5")
    assert resumed_queue.last_attempt_sequence == 5


def test_r006_live_adapter_is_owner_gated_and_injects_doubled_profile(monkeypatch) -> None:
    contract = load_contract()
    _published, parent = load_frozen_r005_contract()
    adapter = R006LiveAdapter(contract=contract, parent_contract=parent)
    descriptor = adapter.descriptor()
    assert descriptor["queue"].startswith("R006V3DurableQueueAdapter")
    assert descriptor["ledger"].startswith("ObservationLedger")
    assert adapter.production_stop_after_ordinal is False
    assert adapter.motion_profile.xy_path_speed_m_s == pytest.approx(0.008)
    assert adapter.motion_profile.qdot_cap_rad_s == pytest.approx(5.0)
    captured = {}

    def fake_builder(*args, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(r006_live, "build_verified_mature_r006_writer", fake_builder)
    assert adapter.build_verified_writer(type("Inputs", (), {"parent": object()})(), path_sample_sink=None) is not None
    assert captured["contract"] is contract
    assert captured["parent_contract"] is parent


def _activate_fake_r006_control_injection(monkeypatch):
    events: list[tuple[str, object]] = []
    original_path_reference = object()

    class FakeCalibrated:
        pass

    calibrated = FakeCalibrated()
    calibrated.step5_path_reference = original_path_reference
    monkeypatch.setitem(
        r006_live.sys.modules,
        "step5d_autotune_v4_r004.calibrated_runtime",
        calibrated,
    )

    class FakeControl:
        def __init__(
            self,
            candidate,
            *,
            attempt_id,
            release_contract,
            path_requested=False,
            motion_profile=None,
            canonical_runtime_only=False,
        ) -> None:
            events.append(("construct", self))
            self.candidate = candidate
            self.attempt_id = attempt_id
            self.release_contract = release_contract
            self.path_requested = path_requested
            self.motion_profile = motion_profile
            self.canonical_runtime_only = canonical_runtime_only

    monkeypatch.setattr(r006_live.r004_writer_module, "CanonicalQualificationControl", FakeControl)
    injection = r006_live._R006ScopedRuntimeInjection(
        motion_profile=object(),
        path_reference=lambda *args: {"stage_id": "r006-test"},
    )
    injection.activate()
    return injection, events, calibrated, original_path_reference


def _r006_arm_fixture(
    injection,
    events,
    *,
    arm_raises: bool = False,
    candidate=None,
):
    release_contract = object()

    class FakeWriter:
        attempt_id = "r006-test-attempt"
        session_epoch = 3
        _canonical_runtime_only = True

        def __init__(self) -> None:
            self.contract = release_contract
            self.candidate = None

        def arm_unbounded(self, **kwargs) -> None:
            events.append(("base-arm", kwargs))
            if arm_raises:
                raise RuntimeError("synthetic mature ARM failure")

        def close(self) -> None:
            events.append(("close", self))

    writer = FakeWriter()
    adapted = r006_live.R006MatureWriter(writer, injection=injection)
    attempt = SimpleNamespace(
        attempt_sequence=8,
        kind="ROUTE",
        candidate=R005Candidate() if candidate is None else candidate,
    )
    adapted.dispatch(attempt, object())
    return adapted, writer, attempt, release_contract


def test_r006_prepared_control_is_built_before_arm_and_consumed_once(monkeypatch) -> None:
    injection, events, calibrated, original_path_reference = _activate_fake_r006_control_injection(monkeypatch)
    try:
        adapted, writer, attempt, release_contract = _r006_arm_fixture(injection, events)
        adapted.arm(attempt)

        assert [event[0] for event in events] == ["construct", "base-arm"]
        assert calibrated.step5_path_reference is injection.path_reference
        prepared = injection._prepared_control
        assert prepared is not None
        factory = r006_live.r004_writer_module.CanonicalQualificationControl
        consumed = factory(
            writer.candidate,
            attempt_id="r006-test-attempt-e3-o8",
            release_contract=release_contract,
            path_requested=True,
            canonical_runtime_only=True,
        )
        assert consumed is prepared
        assert len([event for event in events if event[0] == "construct"]) == 1
        with pytest.raises(r006_live.R006LiveAdapterError, match="missing or already consumed"):
            factory(
                writer.candidate,
                attempt_id="r006-test-attempt-e3-o8",
                release_contract=release_contract,
                path_requested=True,
                canonical_runtime_only=True,
            )
    finally:
        injection.deactivate()
    assert calibrated.step5_path_reference is original_path_reference


def test_r006_prepared_control_key_mismatch_fails_closed(monkeypatch) -> None:
    injection, events, _calibrated, _original_path_reference = _activate_fake_r006_control_injection(monkeypatch)
    try:
        candidate = object()
        release_contract = object()
        injection.prepare_control(
            candidate=candidate,
            attempt_id="r006-test-attempt-e3-o8",
            release_contract=release_contract,
            path_requested=True,
            canonical_runtime_only=True,
        )
        factory = r006_live.r004_writer_module.CanonicalQualificationControl
        with pytest.raises(r006_live.R006LiveAdapterError, match="key mismatch"):
            factory(
                object(),
                attempt_id="r006-test-attempt-e3-o8",
                release_contract=release_contract,
                path_requested=True,
                canonical_runtime_only=True,
            )
        assert injection._prepared_control is None
        assert len([event for event in events if event[0] == "construct"]) == 1
        with pytest.raises(r006_live.R006LiveAdapterError, match="missing or already consumed"):
            factory(
                candidate,
                attempt_id="r006-test-attempt-e3-o8",
                release_contract=release_contract,
                path_requested=True,
                canonical_runtime_only=True,
            )
    finally:
        injection.deactivate()


def test_r006_prepared_control_cache_clears_on_arm_failure_close_and_deactivate(monkeypatch) -> None:
    injection, events, calibrated, original_path_reference = _activate_fake_r006_control_injection(monkeypatch)
    try:
        adapted, _writer, attempt, _release_contract = _r006_arm_fixture(
            injection,
            events,
            arm_raises=True,
        )
        with pytest.raises(RuntimeError, match="synthetic mature ARM failure"):
            adapted.arm(attempt)
        assert injection._prepared_control is None

        injection.prepare_control(
            candidate=object(),
            attempt_id="r006-close-e3-o9",
            release_contract=object(),
            path_requested=False,
            canonical_runtime_only=True,
        )
        assert injection._prepared_control is not None
        adapted.close()
        assert injection.active is False
        assert injection._prepared_control is None
        assert calibrated.step5_path_reference is original_path_reference

        injection.activate()
        injection.prepare_control(
            candidate=object(),
            attempt_id="r006-deactivate-e3-o10",
            release_contract=object(),
            path_requested=False,
            canonical_runtime_only=True,
        )
        assert injection._prepared_control is not None
    finally:
        injection.deactivate()
    assert injection._prepared_control is None


def test_r006_wire_target_adapter_builds_native_packet_and_rejects_other_types(monkeypatch) -> None:
    original_assert_target = r004_wire.assert_target
    injection, _events, _calibrated, _original_path_reference = _activate_fake_r006_control_injection(monkeypatch)
    candidate = R006ProductionQueue.candidate(ParameterPoint(ko_step=-1, kp_step=-1))
    sensor = r004_wire.SensorPacket(
        normal_load_n=0.0,
        force_norm_n=0.0,
        heartbeat=1.0,
        sensor_fresh=True,
        stop_request=False,
        eoat_get_ack=True,
        torque_norm_nm=0.0,
        wrench=(0.0,) * 6,
        filtered_normal_n=0.0,
    )
    session = r004_wire.SessionInput(
        baseline_consecutive_successes=3,
        command_mode=r004_wire.CommandMode.HOLD,
        sticky_one_newton_latched=0,
        session_command=r004_wire.SessionCommand.HOLD,
        session_command_sequence=1,
        session_epoch=1,
        logical_attempt_ordinal=17,
        attempt_kind=r004_wire.AttemptKind.BATCH_A,
        candidate_token=1,
    )
    try:
        packet = r004_wire.build_wire_packet(
            object(),
            candidate,
            sensor=sensor,
            proposed_qdot=(0.0,) * 6,
            internal_setpoint_n=1.0,
            packet_sequence=0,
            session=session,
            motion_profile=R004_MOTION_PROFILE,
        )
        assert packet.sequence == 0

        wrong_target = object.__new__(R006Candidate)
        for field, value in candidate.__dict__.items():
            object.__setattr__(wrong_target, field, value)
        object.__setattr__(wrong_target, "target_force_n", 4.0)
        with pytest.raises(r006_live.R006LiveAdapterError, match="exactly 5.0 N"):
            r004_wire.build_wire_packet(
                object(),
                wrong_target,
                sensor=sensor,
                proposed_qdot=(0.0,) * 6,
                internal_setpoint_n=1.0,
                packet_sequence=0,
                session=session,
                motion_profile=R004_MOTION_PROFILE,
            )

        class DuckCandidate:
            target_force_n = 5.0

        with pytest.raises(Exception, match="candidate must be typed"):
            r004_wire.build_wire_packet(
                object(),
                DuckCandidate(),
                sensor=sensor,
                proposed_qdot=(0.0,) * 6,
                internal_setpoint_n=1.0,
                packet_sequence=0,
                session=session,
                motion_profile=R004_MOTION_PROFILE,
            )
    finally:
        injection.deactivate()
    assert r004_wire.assert_target is original_assert_target


def test_r006_wire_target_adapter_restores_on_deactivate_and_activation_exception(monkeypatch) -> None:
    original_assert_target = r004_wire.assert_target
    injection = r006_live._R006ScopedRuntimeInjection(
        motion_profile=object(),
        path_reference=lambda *args: {"stage_id": "r006-test"},
    )
    injection.activate()
    assert r004_wire.assert_target is not original_assert_target
    injection.deactivate()
    assert r004_wire.assert_target is original_assert_target

    monkeypatch.delattr(r006_live.r004_writer_module, "PathEvidenceCollector")
    failed = r006_live._R006ScopedRuntimeInjection(
        motion_profile=object(),
        path_reference=lambda *args: {"stage_id": "r006-test"},
    )
    with pytest.raises(AttributeError):
        failed.activate()
    assert failed.active is False
    assert r004_wire.assert_target is original_assert_target
    failed.deactivate()
    assert r004_wire.assert_target is original_assert_target


def test_r006_native_candidate_keeps_symmetric_domain_and_rejects_bad_edges() -> None:
    ko_minus = ParameterPoint(ko_step=-1)
    kp_minus = ParameterPoint(ko_step=-1, kp_step=-1)
    ko_candidate = R006ProductionQueue.candidate(ko_minus)
    kp_candidate = R006ProductionQueue.candidate(kp_minus)

    assert type(ko_candidate) is R006Candidate
    assert _point_from_candidate(ko_candidate) == ko_minus
    assert _point_from_candidate(kp_candidate) == kp_minus
    assert transition_is_legal(ko_minus, kp_minus)
    assert not transition_is_legal(ANCHOR_POINT, kp_minus)

    invalid = (
        {"orientation_ko": 0.0},
        {"motion_kp": -1.0},
        {"force_p_gain": float("nan")},
        {"target_force_n": 4.0},
        {"force_p_gain": 0.0003535533906 * (2.0**0.1)},
        {"force_i_gain": 0.0, "i_mode": IMode.ON},
    )
    for fields in invalid:
        with pytest.raises(r006_live.R006LiveAdapterError):
            R006Candidate(**fields)

    illegal_host = object.__new__(r006_live.R006HostLoop)
    illegal_host.cursor = R006Candidate()
    illegal_host.queue = SimpleNamespace(enqueue=lambda *args, **kwargs: None)
    illegal_host.epoch = 17
    illegal_host.events = []
    with pytest.raises(Exception, match="one-coordinate quarter-octave"):
        illegal_host._queue_request(kp_candidate, kind="WARM_START_1")


def test_r006_native_policy_restores_v3_functions_after_exit_and_exception() -> None:
    import step5d_parameter_queue as v3_queue

    profile = ROOT / "config/step5/step5d_autotune_v4_r006_launch_profile.json"
    originals = (
        v3_queue._request_document,
        v3_queue._request_candidate,
        v3_queue.search_candidate_allowed,
    )

    with r006_live._r006_v3_native_policy(profile):
        assert v3_queue._request_document is not originals[0]
        assert v3_queue._request_candidate is not originals[1]
        assert v3_queue.search_candidate_allowed is not originals[2]
    assert (
        v3_queue._request_document,
        v3_queue._request_candidate,
        v3_queue.search_candidate_allowed,
    ) == originals

    with pytest.raises(RuntimeError, match="forced native policy failure"):
        with r006_live._r006_v3_native_policy(profile):
            assert v3_queue._request_document is not originals[0]
            assert v3_queue._request_candidate is not originals[1]
            assert v3_queue.search_candidate_allowed is not originals[2]
            raise RuntimeError("forced native policy failure")
    assert (
        v3_queue._request_document,
        v3_queue._request_candidate,
        v3_queue.search_candidate_allowed,
    ) == originals


def test_r006_native_refill_beyond_seq16_is_durable_and_prearm_ready(tmp_path: Path, monkeypatch) -> None:
    profile = ROOT / "config/step5/step5d_autotune_v4_r006_launch_profile.json"
    queue_root = tmp_path / "native-queue"
    queue = R006NativeV3DurableQueueAdapter(
        queue_root,
        campaign_id="e" * 64,
        launch_profile_path=profile,
        release_manifest_sha256="f" * 64,
    )
    queue.bind_home(campaign_epoch=1, last_trial_id=0, last_command_seq=0)

    def prepare_native(candidate: R006Candidate, attempt_sequence: int) -> None:
        injection, events, _calibrated, _original = _activate_fake_r006_control_injection(monkeypatch)

        class NativeFakeControl:
            def __init__(self, candidate, **kwargs) -> None:
                assert type(candidate) is R006Candidate
                events.append(("native-construct", candidate, kwargs))
                self.candidate = candidate

        monkeypatch.setattr(r006_live, "_R006NativeCanonicalQualificationControl", NativeFakeControl)
        try:
            adapted, writer, attempt, release_contract = _r006_arm_fixture(
                injection,
                events,
                candidate=candidate,
            )
            attempt.attempt_sequence = attempt_sequence
            adapted.arm(attempt)
            assert writer.candidate is candidate
            factory = r006_live.r004_writer_module.CanonicalQualificationControl
            consumed = factory(
                writer.candidate,
                attempt_id=f"r006-test-attempt-e3-o{attempt_sequence}",
                release_contract=release_contract,
                path_requested=True,
                canonical_runtime_only=True,
            )
            assert consumed.candidate is candidate
            assert [event[0] for event in events] == ["native-construct", "base-arm"]
        finally:
            injection.deactivate()

    ko_candidate = R006ProductionQueue.candidate(ParameterPoint(ko_step=-1))
    first = queue.enqueue(ko_candidate, kind="WARM_START_1", epoch=1)
    first_request = queue._load_v3().list_requests(queue_root)[-1]
    assert first_request["overlay"]["orientation_ko"] == pytest.approx(
        ko_candidate.orientation_ko
    )
    assert first_request["overlay"]["orientation_ko"] < 0.1
    first_ticket = queue.prepare_next()
    assert first_ticket is not None
    assert type(first_ticket.candidate) is R006Candidate
    assert _point_from_candidate(first_ticket.candidate) == ParameterPoint(ko_step=-1)
    prepare_native(first_ticket.candidate, 17)
    queue.record_execution(first_ticket, attempt_sequence=17, execution_id="r006-seq17")
    queue.complete(first_ticket, status="SUCCEEDED")

    cold = R006NativeV3DurableQueueAdapter(
        queue_root,
        campaign_id="e" * 64,
        launch_profile_path=profile,
        release_manifest_sha256="f" * 64,
    )
    assert cold.pending() == ()
    assert cold.inflight is None

    kp_candidate = R006ProductionQueue.candidate(ParameterPoint(ko_step=-1, kp_step=-1))
    second = cold.enqueue(kp_candidate, kind="WARM_START_1", epoch=1)
    second_request = cold._load_v3().list_requests(queue_root)[-1]
    assert second_request["overlay"]["motion_kp"] == pytest.approx(kp_candidate.motion_kp)
    assert second_request["overlay"]["motion_kp"] < 1.5
    assert type(second.candidate) is R006Candidate
    cold_read = R006NativeV3DurableQueueAdapter(
        queue_root,
        campaign_id="e" * 64,
        launch_profile_path=profile,
        release_manifest_sha256="f" * 64,
    )
    pending = cold_read.pending()
    assert len(pending) == 1
    assert type(pending[0].candidate) is R006Candidate
    assert pending[0].candidate.candidate_uid == kp_candidate.candidate_uid
    assert _point_from_candidate(pending[0].candidate) == ParameterPoint(ko_step=-1, kp_step=-1)
    second_ticket = cold_read.prepare_next()
    assert second_ticket is not None
    prepare_native(second_ticket.candidate, 18)
    cold_read.record_execution(second_ticket, attempt_sequence=18, execution_id="r006-seq18")
    cold_read.complete(second_ticket, status="SUCCEEDED")


def test_r006_duration_accepts_only_ready_home_structural_endpoint() -> None:
    collector = R006PathEvidenceCollector()
    collector._bins = {index: [5.0] for index in range(550)}
    collector._path_samples = [
        PathSample(
            observed_at_s=60.0,
            filtered_normal_n=5.0,
            force_norm_n=5.0,
            torque_norm_nm=0.0,
            sensor_fresh=True,
            state=25,
            safety_normal=True,
            path_time_s=59.996,
            path_phase=6,
        )
    ]
    collector._states = {25}
    assert collector._canonicalize_full_duration(
        59.998,
        coverage_interval_s=0.002,
        rounding_bound_s=0.0,
    ) == pytest.approx(59.998)

    collector._states.add(78)
    assert collector._canonicalize_full_duration(
        59.998,
        coverage_interval_s=0.002,
        rounding_bound_s=0.0,
    ) == pytest.approx(60.0)

    collector._bins.pop(549)
    assert collector._canonicalize_full_duration(
        59.998,
        coverage_interval_s=0.002,
        rounding_bound_s=0.0,
    ) == pytest.approx(59.998)


def test_r006_stop_packet_preserves_typed_reason_and_tp_ordering(monkeypatch) -> None:
    script = (ROOT / "programs/step5/step5d/step5d_strict_rnn_autotune_v4_r006.script").read_text()
    # The currently built package is refreshed after source tests; render the
    # source to prove the release generator owns the semantic change.
    from step5d_autotune_v4_r006.tp import render_script

    rendered = render_script(load_contract(verify_source_closure=False))
    assert "elif layout != 606.0:" in rendered
    assert "valid < 0.5 and read_input_float_register(28) < 0.5" in rendered
    assert "layout != 606.0 or valid < 0.5" not in rendered

    writer = object.__new__(R006LiveWriter)
    writer._stopped = False
    packet = type(
        "Packet",
        (),
        {"stop_dominant": True, "reason_code": 3, "reason": "sensor_stale"},
    )()
    monkeypatch.setattr(
        r006_live.LiveR004Writer,
        "_send_packet",
        lambda self, *args, **kwargs: packet,
    )
    with pytest.raises(Exception, match="reason_code=3 reason=sensor_stale"):
        writer._send_packet(object(), command_mode=object())


def test_managed_cuda_worker_uses_exact_step5d_r008_venv(monkeypatch, tmp_path: Path) -> None:
    exact_python = Path("/home/andy/.venvs/step5d-r008/bin/python")
    if not exact_python.is_file():
        pytest.fail(f"required managed CUDA interpreter is missing: {exact_python}")
    contract = load_contract()
    ledger = ObservationLedger(
        tmp_path / "observations.jsonl",
        campaign_fingerprint=contract.campaign_fingerprint,
        eoat_sha256="f" * 64,
    )
    sidecar = R006ObservationLedger(ledger, campaign_fingerprint=contract.campaign_fingerprint)
    for sequence, point in enumerate(
        (ANCHOR_POINT, ParameterPoint(p_step=1), ParameterPoint(d_step=1)),
        start=1,
    ):
        receipt = _receipt(contract, point, sequence, force_n=5.20 + sequence * 0.03)
        sidecar.sidecar.append(
            receipt,
            epoch=1,
            candidate_uid=point.uid,
            kind="WARM_START_1",
            point_key=list(point.key),
        )
    import step5d_autotune_v4_r006.optimizer as optimizer_module
    from step5d_managed_runtime import resolve_managed_optimizer_runtime

    managed = resolve_managed_optimizer_runtime(contract.runtime_manifest)
    exact_runtime = replace(managed.runtime, python_executable=exact_python)
    exact_binding = replace(managed, runtime=exact_runtime)
    monkeypatch.setattr(optimizer_module, "resolve_managed_optimizer_runtime", lambda _manifest: exact_binding)
    raw_rows = sidecar.sidecar.fresh_process_verify()
    artifact_binding = {
        "sidecar_path": str(sidecar.sidecar.path.resolve(strict=True)),
        "sidecar_sha256": hashlib.sha256(sidecar.sidecar.path.read_bytes()).hexdigest(),
        "campaign_fingerprint": contract.campaign_fingerprint,
        "rows": [
            {"attempt_sequence": row["attempt_sequence"], "execution_id": row["execution_id"]}
            for row in raw_rows
        ],
    }
    client = R006CudaQLogNEI(
        runtime_manifest=contract.runtime_manifest,
        artifact_binding=artifact_binding,
        seed=6006,
    )
    client.fit_group_once(1)
    client.fit_group_once(2)
    client.freeze()
    ask = client.ask(
        choices=(
            ParameterPoint(p_step=1, d_step=1),
            ParameterPoint(p_step=1),
            ParameterPoint(d_step=1),
            ParameterPoint(tau_step=1),
        ),
        pending=(ParameterPoint(p_step=1),),
        incumbent=ANCHOR_POINT,
        q=1,
    )
    assert ask.metadata["worker"] == "r006_managed_cuda_botorch"
    assert ask.metadata["qlognei"] == "qLogNEI"
    assert ask.metadata["x_pending"] is True
    assert ask.metadata["initialization"]["method"] == "repeats_plus_minus_probes"


def test_threshold_receipt_is_required_before_live_host_loop() -> None:
    contract = load_contract()
    with pytest.raises(Exception, match="threshold receipt"):
        r006_live.R006HostLoop(
            r006_contract=contract,
            r006_ledger=object(),
            threshold_policy=None,
        )
    help_text = subprocess.run(
        [sys.executable, str(ROOT / "tools/run_step5d_autotune_v4_r006.py"), "live", "--help"],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PYTHONPATH": f"{ROOT / 'tools'}:{ROOT.parents[1] / 'src' / 'ur10e_experiment_runtime'}",
        },
    ).stdout
    assert "stop-after-ordinal" not in help_text
