from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import socket
import struct
import subprocess
import sys
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
VIC_ROOT = ROOT.parent / "ur10e-variable-impedance"
UR_HELPERS = Path(
    "/home/andy/codex-private-skills-shared-main/skills/ur10e-realsetup/scripts"
)
for path in (TOOLS, VIC_ROOT, UR_HELPERS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import step5d_v34_transport_primitives as transport_primitives  # noqa: E402
from run_tacdiffusion_remote_direct_torque_v4 import (  # noqa: E402
    COMPILE_PROBE_EVIDENCE_SCHEMA,
    COMPILE_PROBE_PROTOCOL_TOKEN,
    COMPILE_PROBE_STATE_ACTIVE,
    COMPILE_PROBE_STATE_COMPLETE,
    CANARY_EVIDENCE_SCHEMA,
    AckPacedScheduler,
    CanaryTimeline,
    KunweiSnapshot,
    CANARY_STAGE_HOLD,
    CANARY_STAGE_RAMP,
    CANARY_STAGE_REFERENCE,
    CANARY_STAGE_REFERENCE_3S_SOUND_DIAGNOSTIC,
    CANARY_STAGE_REFERENCE_7S_SOUND_DIAGNOSTIC,
    CANARY_STAGE_REFERENCE_10S_DIAGNOSTIC,
    LiveRTDE,
    _LIVE_WRITER_PATTERNS,
    _LIVE_WRITER_IGNORED_PATTERNS,
    INPUT_FIELDS,
    OUTPUT_FIELDS,
    LIVE_PROTOCOL_TOKEN,
    RUNTIME_PLAYING,
    RUNTIME_STOPPED,
    ReferenceTimeline,
    NO_CONTACT_RELEASE_TOLERANCE_M,
    STATE_STARTUP,
    STATE_WAITING,
    STATE_TORQUE,
    STATE_COMPLETE,
    MODE_ABORT,
    MODE_END,
    MODE_IDLE,
    MODE_RUN,
    ROBOT_MODE_RUNNING,
    SAFETY_MODE_NORMAL,
    WRENCH_FRAME_TOKEN,
    _detect_live_writer_processes,
    _enforce_no_live_writer_conflict,
    _counter_rate_hz,
    _maximum_active_control_update_gap_s,
    _is_stationary,
    _stationarity_window_evidence,
    _next_available_run_dir,
    _new_live_identity_pair,
    _prime_idle_inputs,
    _primary_client_observer,
    _sample_translation_error_sqm3,
    _send_urscript_with_primary_start_barrier,
    _stale_terminal_receiver_allows_one_resend,
    _wait_for_fresh_receiver_waiting,
    _update_compile_probe_markers,
    _update_receiver_handshake_markers,
    _run_live_locked,
    _write_json_new,
    analyze_entry_bumplessness,
    build_receiver_handshake_probe_source,
    command_values,
    validate_bundle,
    validate_compile_probe_evidence,
    validate_compile_probe_preflight,
    validate_live_preflight,
    validate_prior_stage_evidence,
    runtime_source_binding,
    run_live,
    summarize_receiver_handshake_samples,
)
from ur10e_vic.tacdiffusion.direct_torque_live_v4 import (  # noqa: E402
    build_compile_probe_source,
)


PASSIVE_RUN = ROOT / "runs" / "tacdiffusion" / "passive_remote_baseline_20260726"
REFERENCE = PASSIVE_RUN / "unknown_surface_anchor_circle_no_contact_2s_reference_v2.json"
BUILDER = VIC_ROOT / "tools" / "build_tacdiffusion_direct_torque_live_v4.py"
RUNNER = TOOLS / "run_tacdiffusion_remote_direct_torque_v4.py"
KUNWEI_CALIBRATION = ROOT / "config/step5d_tacdiffusion_sensor_frame_v1.json"
REFERENCE_10S_SANITY = (
    ROOT / "config/direct_torque_v4_reference_10s_diagnostic_sanity.json"
)


def test_cadence_counter_rate_uses_controller_time_and_counter_delta() -> None:
    rows = [
        {
            "receiver_state": STATE_TORQUE,
            "controller_timestamp_s": 10.000,
            "counter": 5,
            "action_echo_coherent": True,
        },
        {
            "receiver_state": STATE_TORQUE,
            "controller_timestamp_s": 10.100,
            "counter": 55,
            "action_echo_coherent": True,
        },
    ]
    assert _counter_rate_hz(rows, "counter") == pytest.approx(500.0)
    rows[-1]["counter"] = 25
    assert _counter_rate_hz(rows, "counter") == pytest.approx(200.0)


def test_cadence_max_gap_ignores_stale_waiting_register_value() -> None:
    rows = [
        {
            "receiver_state": STATE_WAITING,
            "maximum_control_update_gap_s": 524.0,
        },
        {
            "receiver_state": STATE_STARTUP,
            "maximum_control_update_gap_s": 0.002,
            "action_echo_coherent": True,
        },
        {
            "receiver_state": STATE_TORQUE,
            "maximum_control_update_gap_s": 0.006,
            "action_echo_coherent": True,
        },
        {
            "receiver_state": STATE_COMPLETE,
            "maximum_control_update_gap_s": 524.0,
        },
    ]
    assert _maximum_active_control_update_gap_s(rows) == pytest.approx(0.006)


def test_entry_replay_fails_large_transition_acceleration() -> None:
    def row(timestamp_s: float, qd_1: float) -> dict[str, float]:
        result = {
            "receiver_state": 1.0,
            "ack_sequence": 1.0,
            "controller_timestamp_s": timestamp_s,
            "action_echo_coherent": True,
        }
        for axis in range(6):
            result[f"commanded_joint_torque_nm_{axis}"] = 0.0
            result[f"command_desired_pose_{axis}"] = 0.0
            result[f"actual_TCP_pose_{axis}"] = 0.0
            result[f"actual_TCP_speed_{axis}"] = 0.0
            result[f"actual_q_{axis}"] = 0.0
            result[f"actual_qd_{axis}"] = qd_1 if axis == 1 else 0.0
        return result

    analysis = analyze_entry_bumplessness(
        [row(10.000, 0.0), row(10.002, 0.020)]
    )
    assert analysis["maximum_derived_abs_joint_acceleration_rad_s2"] == (
        pytest.approx(10.0)
    )
    assert analysis["outcome"] == "FAIL"
    assert analysis["transition_failures"] == [
        "joint_acceleration_gt_1rad_s2"
    ]
    assert analysis["motion_performed"] is False
    assert analysis["controller_io_performed"] is False


class FakeKunweiCapture:
    def __init__(self, **_kwargs: object) -> None:
        self.sample = KunweiSnapshot(
            sample_index=1100,
            receive_batch_id=44,
            t_monotonic_s=0.0,
            nominal_sensor_time_s=1.099,
            raw_si=(0.0,) * 6,
            wrench_tcp_si=(0.0,) * 6,
            normal_load_n=0.0,
            force_norm_n=0.0,
            torque_norm_nm=0.0,
        )

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def wait_preflight(self):
        return self.sample

    def snapshot(self, **_kwargs: object):
        return self.sample

    def stop(self) -> None:
        return None

    def summary(self) -> dict[str, Any]:
        return {
            "post_baseline_samples": 100,
            "parse_errors": 0,
            "dropped_sync_bytes": 0,
            "rate_hz_by_first_last": 1000.0,
            "max_delivery_gap_ms": 50.0,
        }


def _bundle(tmp_path: Path):
    if not REFERENCE.is_file():
        pytest.skip("fresh passive reference is unavailable")
    source = tmp_path / "receiver.script"
    manifest = tmp_path / "receiver.manifest.json"
    subprocess.run(
        [
            sys.executable,
            str(BUILDER),
            "--reference",
            str(REFERENCE),
            "--receiver-source",
            str(source),
            "--manifest",
            str(manifest),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return validate_bundle(manifest)


def _write_compile_probe_evidence(tmp_path: Path, **overrides: Any) -> Path:
    source_sha256 = hashlib.sha256(
        build_compile_probe_source().encode("utf-8")
    ).hexdigest()
    payload = {
        "schema": COMPILE_PROBE_EVIDENCE_SCHEMA,
        "claim_class": "live_controller_compile_probe_no_motion",
        "ok": True,
        "robot_host": "192.168.1.18",
        "compile_probe_source_sha256": source_sha256,
        "strict_success_gate": {"ok": True},
        "motion_performed": False,
        "direct_torque_called": False,
        "rtde_inputs_written": False,
        "kunwei_stream_started": False,
    }
    payload.update(overrides)
    path = tmp_path / "compile_probe_evidence.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_ack_paced_scheduler_has_no_gaps_at_approximately_83_hz() -> None:
    scheduler = AckPacedScheduler()
    sent: list[tuple[int, float]] = []
    ack = 0
    for robot_tick in range(1001):
        elapsed = robot_tick / 500.0
        if robot_tick % 6 == 0:
            sequence = scheduler.next_sequence(ack)
            assert sequence is not None
            sent.append((sequence, elapsed))
            ack = sequence
        else:
            assert scheduler.next_sequence(ack - 1) is None
    assert [sequence for sequence, _ in sent] == list(range(1, len(sent) + 1))
    assert sent[-1][1] == pytest.approx(1.992)
    assert len(sent) == 167


def test_ack_scheduler_uses_v34_absolute_deadline_without_burst_catchup() -> None:
    scheduler = AckPacedScheduler()
    scheduler.arm(10.0)
    assert scheduler.release_due(10.0)
    assert not scheduler.release_due(10.001)
    assert scheduler.release_due(10.0101)
    summary = scheduler.summary()
    assert summary["policy"] == "v34_absolute_deadline_ack_gated_no_burst"
    assert summary["missed_slots_total"] == 4
    assert summary["overrun_events"] == 1
    assert summary["burst_catchup_allowed"] is False


def test_receive_available_preserves_every_decoded_controller_packet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rtde = LiveRTDE.__new__(LiveRTDE)
    rtde.sock = object()
    packets = [
        (
            ord("U"),
            bytes([7]) + struct.pack("!di", index / 500.0, index),
        )
        for index in range(6)
    ]
    rtde._recv_packet = lambda: packets.pop(0)  # type: ignore[method-assign]
    readiness = iter([[rtde.sock]] + [[rtde.sock]] * 5 + [[]])

    def fake_select(*_args: object, **_kwargs: object):
        return next(readiness), [], []

    monkeypatch.setattr(
        "step5d_v34_transport_primitives.select.select",
        fake_select,
    )
    samples = rtde.receive_available(
        7,
        ["DOUBLE", "INT32"],
        ["timestamp", "ack_sequence"],
        0.1,
    )
    assert [sample["ack_sequence"] for sample in samples] == list(range(6))
    assert [sample["timestamp"] for sample in samples] == pytest.approx(
        [index / 500.0 for index in range(6)]
    )


def _fake_output_sample(
    *,
    pose: list[float],
    timestamp: float,
    ack_sequence: int = 0,
    state: int = STATE_TORQUE,
) -> dict[str, Any]:
    sample: dict[str, Any] = {
        "timestamp": timestamp,
        "runtime_state": RUNTIME_PLAYING,
        "robot_mode": ROBOT_MODE_RUNNING,
        "safety_mode": SAFETY_MODE_NORMAL,
        "output_int_register_24": state,
        "output_int_register_25": ack_sequence,
        "output_int_register_26": 0,
        "output_int_register_27": 111,
        "output_int_register_28": 0,
        "output_int_register_29": 1 if state in {STATE_STARTUP, STATE_TORQUE} else 0,
        "output_int_register_30": WRENCH_FRAME_TOKEN,
        "output_int_register_31": 222,
        "output_int_register_32": LIVE_PROTOCOL_TOKEN,
        "output_int_register_33": 1 if state in {STATE_STARTUP, STATE_TORQUE} else 0,
        "output_int_register_34": MODE_RUN,
        "output_int_register_35": 1,
        "output_double_register_24": 0.0,
        "output_double_register_25": 0.002,
        "output_double_register_44": 0.005,
        "output_double_register_45": timestamp * 200.0,
        "output_double_register_46": 0.005,
        "output_double_register_47": timestamp * 500.0,
        "actual_TCP_pose": pose,
        "actual_TCP_speed": [0.0] * 6,
        "actual_TCP_force": [0.0] * 6,
        "actual_q": [0.0] * 6,
        "actual_qd": [0.0] * 6,
        "target_moment": [0.0] * 6,
    }
    for index in range(18):
        sample[f"output_double_register_{26 + index}"] = float(index)
    return sample


def _fake_control_bundle(pose: list[float], *, unsafe_x: float | None = None):
    class FakeTube:
        def rebased(self, _anchor_pose: list[float]):
            return self

        def assert_contains_pose(self, candidate: list[float], *, role: str) -> None:
            if role == "actual" and unsafe_x is not None and candidate[0] == unsafe_x:
                raise RuntimeError("actual_pose_outside_tube")

    class FakeTimeline:
        # This transport-only fixture ends immediately after the first
        # observed torque batch. Real canary timelines remain strictly
        # positive and start their clock at first observed Direct Torque.
        duration_s = 0.0
        rows = [{"progress_s": 0.0, "desired_pose_base": tuple(pose)}]

        @staticmethod
        def row_at(_elapsed: float) -> dict[str, Any]:
            return {"progress_s": 0.0, "desired_pose_base": tuple(pose)}

    return SimpleNamespace(
        timeline=FakeTimeline(),
        tube=FakeTube(),
        source="generated receiver\n",
        source_sha256="source-sha",
        manifest_sha256="manifest-sha",
        reference_sha256="reference-sha",
        friction_profile="zero_isolation",
        viscous_scale=(0.0,) * 6,
        coulomb_scale=(0.0,) * 6,
    )


def _run_fake_batched_control(
    tmp_path: Path,
    *,
    unsafe_intermediate: bool,
) -> tuple[list[int], list[dict[str, Any]], dict[str, Any]]:
    pose = [0.0] * 6
    unsafe_x = 0.5 if unsafe_intermediate else None
    bundle = _fake_control_bundle(pose, unsafe_x=unsafe_x)
    waiting = _fake_output_sample(pose=pose, timestamp=0.0, state=STATE_WAITING)
    data_batch = [
        _fake_output_sample(
            pose=([unsafe_x] + pose[1:] if index == 2 and unsafe_x is not None else pose),
            timestamp=index / 500.0,
        )
        for index in range(6)
    ]
    terminal = _fake_output_sample(
        pose=pose,
        timestamp=6 / 500.0,
        ack_sequence=1,
        state=STATE_COMPLETE,
    )

    class FakeRTDE:
        batches = [data_batch, [terminal]]

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.sent_modes: list[int] = []

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def negotiate(self) -> None:
            return None

        def setup_outputs(self, *_args: object, **_kwargs: object):
            return 1, OUTPUT_FIELDS

        def setup_inputs(self, *_args: object, **_kwargs: object):
            return 1, ["DOUBLE"] * 24 + ["INT32"] * 12

        def start(self) -> None:
            return None

        def send_inputs(self, _recipe: int, _types: list[str], values: list[Any]) -> None:
            self.sent_modes.append(int(values[24]))

        def receive_available(self, *_args: object, **_kwargs: object):
            if not self.batches:
                return []
            return self.batches.pop(0)

    fake_rtde = FakeRTDE()
    compile_probe_evidence = _write_compile_probe_evidence(tmp_path)
    args = SimpleNamespace(
        canary_stage=CANARY_STAGE_HOLD,
        compile_probe_evidence=compile_probe_evidence,
        prior_stage_evidence=None,
        robot_host="192.168.1.18",
        output_dir=tmp_path / "capture",
        connect_timeout_s=1.0,
        receiver_wait_s=1.0,
        kunwei_calibration=Path("unused-calibration.json"),
        sensor_ip="192.168.50.25",
        sensor_port=5152,
    )
    clock = iter([0.0001 + 0.0002 * index for index in range(30)])
    kunwei_snapshot = KunweiSnapshot(
        sample_index=1100,
        receive_batch_id=44,
        t_monotonic_s=0.0,
        nominal_sensor_time_s=1.099,
        raw_si=(0.0,) * 6,
        wrench_tcp_si=(0.0,) * 6,
        normal_load_n=0.0,
        force_norm_n=0.0,
        torque_norm_nm=0.0,
    )

    class FakeKunwei:
        def __init__(self, **_kwargs: object) -> None:
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def wait_preflight(self):
            return kunwei_snapshot

        def snapshot(self, **_kwargs: object):
            return kunwei_snapshot

        def stop(self) -> None:
            return None

        def summary(self) -> dict[str, Any]:
            return {
                "post_baseline_samples": 100,
                "parse_errors": 0,
                "dropped_sync_bytes": 0,
                "rate_hz_by_first_last": 1000.0,
                "max_delivery_gap_ms": 50.0,
            }

    with patch(
        "run_tacdiffusion_remote_direct_torque_v4.LiveRTDE", return_value=fake_rtde
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4.KunweiGuardCapture", FakeKunwei
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4.validate_calibration",
        return_value=({"wrench_transform_sensor_to_tcp_6x6": []}, "calibration-sha"),
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4.validate_compile_probe_evidence",
        return_value={"ok": True},
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4.validate_prior_stage_evidence",
        return_value=None,
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4._new_live_identity_pair",
        return_value=(111, 222),
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4.readonly_status",
        return_value={"rtde": {"actual_TCP_pose": pose}},
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4.CanaryTimeline.from_stage",
        return_value=bundle.timeline,
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4.validate_live_preflight"
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4._enforce_no_live_writer_conflict"
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4._send_urscript"
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4._prime_idle_inputs",
        side_effect=lambda rtde, input_recipe, input_types, idle_values, *_args, **_kwargs: rtde.send_inputs(
            input_recipe, input_types, idle_values
        ),
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4._wait_for_fresh_receiver_waiting",
        return_value=(0.0, waiting),
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4.time.monotonic",
        side_effect=lambda: next(clock),
    ):
        result: dict[str, Any] | None = None
        error: Exception | None = None
        try:
            result = _run_live_locked(args, bundle)
        except Exception as exc:
            error = exc

    capture_dir = tmp_path / "capture"
    run_dir = capture_dir if capture_dir.exists() else tmp_path / "capture_1"
    if not (run_dir / "evidence.json").is_file() and error is not None:
        raise error
    evidence = json.loads((run_dir / "evidence.json").read_text(encoding="utf-8"))
    rows = []
    csv_path = run_dir / "direct_torque_rtde.csv"
    if csv_path.is_file():
        import csv

        with csv_path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    if unsafe_intermediate:
        assert error is not None
    else:
        assert error is None
        assert result is not None
    return fake_rtde.sent_modes, rows, evidence


def test_fake_batched_rtde_logs_six_samples_with_one_new_ack_command(
    tmp_path: Path,
) -> None:
    sent_modes, rows, evidence = _run_fake_batched_control(
        tmp_path, unsafe_intermediate=False
    )
    assert sent_modes.count(MODE_RUN) == 1
    assert sent_modes[:2] == [MODE_IDLE, MODE_RUN]
    assert len(rows) == 8
    assert [float(row["controller_timestamp_s"]) for row in rows[1:7]] == pytest.approx(
        [index / 500.0 for index in range(6)]
    )
    assert all(row["ack_command_lineage_missing"] == "0" for row in rows)
    assert rows[0]["acked_command_sequence"] == "0"
    assert rows[1]["acked_command_mode"] == "0"
    assert rows[1]["acked_command_progress_s"] == "0.0"
    assert rows[1]["acked_command_desired_pose_0"] == "0.0"
    assert rows[1]["acked_commanded_k_0"] == "600.0"
    assert rows[1]["acked_commanded_raw_f_ff_0"] == "0.0"
    assert rows[1]["acked_command_lease"] == "111"
    assert rows[1]["acked_command_episode"] == "222"
    assert rows[1]["acked_command_model_mode"] == "0"
    assert rows[1]["acked_command_frame_token"] == str(WRENCH_FRAME_TOKEN)
    assert rows[-1]["outgoing_command_mode"] == str(MODE_END)
    assert rows[-1]["outgoing_command_sequence"] == "1"
    assert evidence["total_rows"] == 8


def test_fake_batched_rtde_aborts_on_unsafe_intermediate_sample(tmp_path: Path) -> None:
    sent_modes, rows, evidence = _run_fake_batched_control(
        tmp_path, unsafe_intermediate=True
    )
    assert sent_modes.count(MODE_RUN) == 1
    assert sent_modes[-1] == MODE_ABORT
    assert len(rows) == 8
    assert rows[3]["controller_timestamp_s"] == "0.004"
    assert evidence["failure"].startswith("RuntimeError: output_batch_safety_failed:")


def test_reference_selection_depends_on_elapsed_time_not_ack_count() -> None:
    payload = json.loads(REFERENCE.read_text(encoding="utf-8"))
    timeline = ReferenceTimeline.from_payload(payload)
    assert timeline.row_at(0.0)["progress_s"] == 0.0
    assert timeline.row_at(1.0)["progress_s"] == pytest.approx(1.0)
    assert timeline.row_at(1.992)["progress_s"] == pytest.approx(1.992)
    assert timeline.row_at(99.0)["progress_s"] == pytest.approx(2.0)


def test_sample_translation_error_only_uses_xyz_component() -> None:
    assert _sample_translation_error_sqm3((0.1, 0.2, 0.3, 0, 0, 0), (0.4, 0.2, 0.3, 0, 0, 0)) == pytest.approx(
        0.3
    )


def test_command_packet_has_complete_register_identity() -> None:
    values = command_values(
        command=1,
        sequence=7,
        pose=(0.1, 0.2, 0.3, 3.14, 0.0, 0.0),
        lease_id=111,
        episode_identity=222,
    )
    assert len(values) == 36
    assert values[24:] == [
        1,
        7,
        7,
        111,
        0,
        0,
        0,
        5_252_001,
        0,
        0,
        0,
        222,
    ]
    assert values[18:24] == [0.0] * 6


def test_bundle_validation_and_cli_are_offline(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    assert len(bundle.timeline.rows) == 1001
    completed = subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "validate",
            "--bundle-manifest",
            str(bundle.manifest_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["ok"]
    assert payload["motion_performed"] is False
    assert payload["urscript_sent"] is False
    assert payload["rtde_inputs_written"] is False
    assert payload["kunwei_stream_started"] is False
    assert payload["runtime_source_binding"]["sha256"] == (
        payload["runtime_source_sha256"]
    )
    assert "v34_transport_primitives" in payload["runtime_source_binding"]["files"]


def test_remote_config_preserves_native_kunwei_rate_and_runtime_binding() -> None:
    config = json.loads(
        (ROOT / "config/direct_torque_remote_live_v4.json").read_text(
            encoding="utf-8"
        )
    )
    assert config["schema"].endswith("/v2")
    authority = config["authority"]
    assert authority["authorization_artifact_required"] is False
    assert authority["live_cli_gates_required"] is True
    capture = config["no_contact_wrench_guard"]
    assert capture["native_sensor_sample_rate_hz"] == 1000
    assert capture["sensor_delivery_watchdog_s"] == 0.08
    assert capture["causal_1khz_alignment_valid"] is False
    hold = config["hold_contract"]
    assert hold["stage_clock_origin"] == "first_observed_direct_torque_state"
    assert hold["stationary_entry_dwell_counts_toward_stage_duration"] is False
    control = config["direct_torque_control_contract"]
    assert control["startup_equilibrium_blend_s"] == 0.1
    assert control["startup_stationary_dwell_s"] == 0.05
    assert control["startup_stationary_dwell_ticks"] is None
    assert control["startup_stationary_clock"] == "monotonic_controller_time"
    assert control["startup_stationary_limits"] == {
        "maximum_abs_joint_speed_rad_s": 0.001,
        "maximum_tcp_translation_speed_m_s": 0.001,
        "maximum_tcp_rotation_speed_rad_s": 0.002,
    }
    assert control["startup_gain_blend_s"] == 0.0
    assert control["startup_gain_policy"] == (
        "full_commanded_gain_from_first_direct_torque_tick"
    )
    assert control["friction_compensation"]["startup_blend_s"] == 0.0
    assert control["friction_compensation"]["startup_policy"] == (
        "disabled_for_no_contact_entry_canary"
    )
    assert control["friction_compensation"]["viscous_scale_target"] == [0.0] * 6
    assert control["friction_compensation"]["coulomb_scale_target"] == [0.0] * 6
    assert control["active_speed_guards"] == {
        "maximum_abs_joint_speed_rad_s": 0.02,
        "maximum_abs_derived_joint_acceleration_rad_s2": 5.0,
        "joint_acceleration_definition": (
            "controller_get_actual_joint_accelerations_encoder_derived"
        ),
        "maximum_tcp_translation_speed_m_s": 0.01,
        "maximum_tcp_rotation_speed_rad_s": 0.02,
        "violation_action": "common_exit_stopj",
        "speed_fault_code": 11,
        "acceleration_fault_code": 12,
    }
    assert control["cadence_contract"]["direct_torque_application_hz"] == 500
    assert control["cadence_contract"]["control_update_rate_minimum_hz"] == 150
    assert control["cadence_contract"]["maximum_control_update_gap_s"] == 0.01
    serialized = json.dumps(config, sort_keys=True)
    assert "sensor_stale_s" not in serialized
    assert "actual_TCP_force" not in serialized


def test_v35_scheduler_gate_requires_sched_other_for_every_thread() -> None:
    good_process = {
        "policy": "SCHED_OTHER",
        "policy_value": transport_primitives.os.SCHED_OTHER,
        "priority": 0,
    }
    good_threads = {
        "threads": [
            {
                "tid": 1,
                "is_control_thread": True,
                **good_process,
            }
        ],
        "counts": {"SCHED_OTHER/0": 1},
    }
    with patch.object(
        transport_primitives,
        "runtime_scheduler_metadata",
        return_value=good_process,
    ), patch.object(
        transport_primitives,
        "runtime_thread_scheduler_snapshot",
        return_value=good_threads,
    ):
        assert (
            transport_primitives.require_v35_sched_other()["mode"]
            == "v35_quota_safe_sched_other"
        )
    bad_threads = {
        "threads": [
            {
                "tid": 2,
                "is_control_thread": False,
                "policy": "SCHED_FIFO",
                "policy_value": transport_primitives.os.SCHED_FIFO,
                "priority": 20,
            }
        ],
        "counts": {"SCHED_FIFO/20": 1},
    }
    with patch.object(
        transport_primitives,
        "runtime_scheduler_metadata",
        return_value=good_process,
    ), patch.object(
        transport_primitives,
        "runtime_thread_scheduler_snapshot",
        return_value=bad_threads,
    ), pytest.raises(RuntimeError, match="v35_sched_other_required"):
        transport_primitives.require_v35_sched_other()


def test_live_identities_are_positive_distinct_int31_values() -> None:
    lease_id, episode_identity = _new_live_identity_pair()
    assert 0 < lease_id <= 0x7FFFFFFF
    assert 0 < episode_identity <= 0x7FFFFFFF
    assert lease_id != episode_identity


def test_canary_timeline_has_fixed_hold_ramp_and_reference_stages(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    old_reference = list(bundle.timeline.rows[0]["desired_pose_base"])
    actual = list(old_reference)
    actual[0] += 0.0011
    actual[1] += 0.0007
    actual[2] += 0.0005
    hold = CanaryTimeline.from_stage(
        CANARY_STAGE_HOLD, actual_pose=actual, bundle=bundle
    )
    assert hold.duration_s == pytest.approx(0.1)
    assert hold.row_at(0.1)["desired_pose_base"] == pytest.approx(actual)

    ramp = CanaryTimeline.from_stage(
        CANARY_STAGE_RAMP, actual_pose=actual, bundle=bundle
    )
    delta = [
        ramp.row_at(0.5)["desired_pose_base"][index] - actual[index]
        for index in range(3)
    ]
    assert math.sqrt(sum(value * value for value in delta)) == pytest.approx(0.0002)

    reference = CanaryTimeline.from_stage(
        CANARY_STAGE_REFERENCE, actual_pose=actual, bundle=bundle
    )
    assert reference.duration_s == pytest.approx(bundle.timeline.duration_s)
    assert reference.row_at(0.0)["desired_pose_base"] == pytest.approx(actual)
    reference_last = bundle.timeline.row_at(bundle.timeline.duration_s)[
        "desired_pose_base"
    ]
    relative_last = [
        reference_last[index] - old_reference[index] for index in range(3)
    ]
    assert reference.row_at(reference.duration_s)["desired_pose_base"][:3] == pytest.approx(
        [actual[index] + relative_last[index] for index in range(3)]
    )
    assert reference.row_at(reference.duration_s)["desired_pose_base"][3:] == pytest.approx(
        actual[3:]
    )

    diagnostic = CanaryTimeline.from_stage(
        CANARY_STAGE_REFERENCE_10S_DIAGNOSTIC,
        actual_pose=actual,
        bundle=bundle,
    )
    assert diagnostic.duration_s == pytest.approx(10.0)
    assert diagnostic.row_at(0.0)["desired_pose_base"] == pytest.approx(actual)
    assert diagnostic.row_at(10.0)["desired_pose_base"] == pytest.approx(
        reference.row_at(reference.duration_s)["desired_pose_base"]
    )
    assert diagnostic.row_at(5.0)["desired_pose_base"] == pytest.approx(
        reference.row_at(bundle.timeline.duration_s / 2.0)["desired_pose_base"]
    )

    sound_diagnostic = CanaryTimeline.from_stage(
        CANARY_STAGE_REFERENCE_3S_SOUND_DIAGNOSTIC,
        actual_pose=actual,
        bundle=bundle,
    )
    assert sound_diagnostic.duration_s == pytest.approx(3.0)
    assert sound_diagnostic.row_at(0.0)["desired_pose_base"] == pytest.approx(actual)
    assert sound_diagnostic.row_at(3.0)["desired_pose_base"] == pytest.approx(
        reference.row_at(reference.duration_s)["desired_pose_base"]
    )
    assert sound_diagnostic.row_at(1.5)["desired_pose_base"] == pytest.approx(
        reference.row_at(bundle.timeline.duration_s / 2.0)["desired_pose_base"]
    )

    sound_diagnostic_7s = CanaryTimeline.from_stage(
        CANARY_STAGE_REFERENCE_7S_SOUND_DIAGNOSTIC,
        actual_pose=actual,
        bundle=bundle,
    )
    assert sound_diagnostic_7s.duration_s == pytest.approx(7.0)
    assert sound_diagnostic_7s.row_at(0.0)["desired_pose_base"] == pytest.approx(
        actual
    )
    assert sound_diagnostic_7s.row_at(7.0)["desired_pose_base"] == pytest.approx(
        reference.row_at(reference.duration_s)["desired_pose_base"]
    )
    assert sound_diagnostic_7s.row_at(3.5)["desired_pose_base"] == pytest.approx(
        reference.row_at(bundle.timeline.duration_s / 2.0)["desired_pose_base"]
    )


def test_reference_10s_numeric_sanity_is_a_time_only_stretch() -> None:
    sanity = json.loads(REFERENCE_10S_SANITY.read_text(encoding="utf-8"))
    factor = sanity["time_stretch_factor"]

    assert sanity["ok"] is True
    assert sanity["spatial_scale"] == 1.0
    assert sanity["maximum_speed_m_s"] == pytest.approx(
        sanity["source_maximum_speed_m_s"] / factor
    )
    assert sanity["maximum_acceleration_m_s2"] == pytest.approx(
        sanity["source_maximum_acceleration_m_s2"] / factor**2
    )
    assert sanity["training_dataset"] is False


def test_old_reference_drift_does_not_block_reanchored_canary_preflight(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    actual = list(bundle.timeline.rows[0]["desired_pose_base"])
    actual[0] += 0.0011
    actual[1] += 0.0007
    actual[2] += 0.0005
    assert math.dist(actual[:3], bundle.timeline.rows[0]["desired_pose_base"][:3]) > 0.001
    status = {
        "remote_control": True,
        "stopped": True,
        "stationary": True,
        "dashboard": {
            "PolyscopeVersion": "URSoftware 5.26.0",
            "safetystatus": "Safetystatus: NORMAL",
            "robotmode": "Robotmode: RUNNING",
        },
        "rtde": {
            "safety_mode": SAFETY_MODE_NORMAL,
            "robot_mode": ROBOT_MODE_RUNNING,
            "actual_TCP_pose": actual,
            "actual_TCP_force": [0.0] * 6,
        },
    }
    for stage in (
        CANARY_STAGE_HOLD,
        CANARY_STAGE_RAMP,
        CANARY_STAGE_REFERENCE,
        CANARY_STAGE_REFERENCE_3S_SOUND_DIAGNOSTIC,
        CANARY_STAGE_REFERENCE_7S_SOUND_DIAGNOSTIC,
        CANARY_STAGE_REFERENCE_10S_DIAGNOSTIC,
    ):
        timeline = CanaryTimeline.from_stage(stage, actual_pose=actual, bundle=bundle)
        validate_live_preflight(status, bundle, timeline=timeline)


def test_large_old_reference_offset_is_bounded_against_fresh_entry(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    actual = list(bundle.timeline.rows[0]["desired_pose_base"])
    actual[0] += 0.001
    actual[1] += 0.006
    actual[2] -= 0.025
    status = {
        "remote_control": True,
        "stopped": True,
        "stationary": True,
        "dashboard": {
            "PolyscopeVersion": "URSoftware 5.26.0",
            "safetystatus": "Safetystatus: NORMAL",
            "robotmode": "Robotmode: RUNNING",
        },
        "rtde": {
            "safety_mode": SAFETY_MODE_NORMAL,
            "robot_mode": ROBOT_MODE_RUNNING,
            "actual_TCP_pose": actual,
            "actual_TCP_force": [0.0] * 6,
        },
    }
    timeline = CanaryTimeline.from_stage(
        CANARY_STAGE_HOLD,
        actual_pose=actual,
        bundle=bundle,
    )
    validate_live_preflight(status, bundle, timeline=timeline)


def test_idle_prime_overwrites_stale_command_for_five_fresh_controller_ticks() -> None:
    class FakeRTDE:
        def __init__(self) -> None:
            self.sent: list[list[Any]] = []
            self.batch_index = 0

        def send_inputs(
            self, _recipe: int, _types: list[str], values: list[Any]
        ) -> None:
            self.sent.append(list(values))

        def receive_available(
            self,
            _recipe: int,
            _types: list[str],
            _fields: list[str],
            _timeout_s: float,
        ) -> list[dict[str, Any]]:
            timestamp = self.batch_index / 500.0
            self.batch_index += 1
            return [
                {
                    "timestamp": timestamp,
                    "runtime_state": RUNTIME_STOPPED,
                    "robot_mode": ROBOT_MODE_RUNNING,
                    "safety_mode": SAFETY_MODE_NORMAL,
                }
            ]

    rtde = FakeRTDE()
    idle_values = [0.0] * len(INPUT_FIELDS)
    _prime_idle_inputs(
        rtde, 1, ["DOUBLE"] * len(idle_values), idle_values, 2, [], OUTPUT_FIELDS
    )
    assert len(rtde.sent) >= 5
    assert all(values == idle_values for values in rtde.sent)
    assert rtde.batch_index >= 6


def test_primary_client_observer_is_read_only() -> None:
    class FakeConnection:
        def __init__(self) -> None:
            self.payloads = [b"controller-state"]
            self.closed = False

        def settimeout(self, _timeout_s: float) -> None:
            return None

        def recv(self, _size: int) -> bytes:
            if self.payloads:
                return self.payloads.pop(0)
            raise socket.timeout

        def sendall(self, _payload: bytes) -> None:
            raise AssertionError("primary observer must never write")

        def shutdown(self, _how: int) -> None:
            return None

        def close(self) -> None:
            self.closed = True

    connection = FakeConnection()
    with patch(
        "run_tacdiffusion_remote_direct_torque_v4.socket.create_connection",
        return_value=connection,
    ) as create_connection:
        with _primary_client_observer(
            "192.168.1.18",
            timeout_s=1.0,
        ) as stats:
            deadline = time.monotonic() + 0.1
            while stats["bytes_received"] == 0 and time.monotonic() < deadline:
                time.sleep(0.001)

    create_connection.assert_called_once_with(
        ("192.168.1.18", 30001),
        timeout=1.0,
    )
    assert stats == {
        "connected": True,
        "bytes_received": len(b"controller-state"),
    }
    assert connection.closed


def test_secondary_send_holds_fresh_primary_start_barrier() -> None:
    events: list[str] = []

    @contextmanager
    def fake_primary_observer(*_args: object, **_kwargs: object):
        events.append("primary_enter")
        try:
            yield {"connected": True, "bytes_received": 321}
        finally:
            events.append("primary_exit")

    with patch(
        "run_tacdiffusion_remote_direct_torque_v4._primary_client_observer",
        side_effect=fake_primary_observer,
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4._send_urscript",
        side_effect=lambda *_args, **_kwargs: events.append("secondary_send"),
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4.time.sleep",
        side_effect=lambda _seconds: events.append("barrier_hold"),
    ):
        stats = _send_urscript_with_primary_start_barrier(
            "192.168.1.18",
            "def probe():\nend\n",
            timeout_s=1.0,
        )

    assert events == [
        "primary_enter",
        "secondary_send",
        "barrier_hold",
        "primary_exit",
    ]
    assert stats == {
        "connected": True,
        "bytes_received": 321,
        "barrier_hold_s": 0.15,
    }


def test_compile_probe_evidence_is_strictly_no_motion(
    tmp_path: Path,
) -> None:
    evidence = _write_compile_probe_evidence(tmp_path)
    assert validate_compile_probe_evidence(
        evidence, robot_host="192.168.1.18"
    )["ok"]


def test_stationary_preflight_uses_the_probe_no_motion_limits() -> None:
    rtde = {
        "actual_TCP_speed": [
            -2.9e-6,
            -2.1e-6,
            -1.8e-6,
            -8.7e-6,
            1.3e-5,
            0.0,
        ],
        "actual_qd": [0.0, 0.0, 0.0, -1.6e-5, 0.0, 0.0],
    }
    assert _is_stationary(rtde)

    rtde["actual_TCP_speed"][0] = 0.00101
    assert not _is_stationary(rtde)
    rtde["actual_TCP_speed"][0] = 0.0
    rtde["actual_TCP_speed"][4] = 0.00101
    assert not _is_stationary(rtde)
    rtde["actual_TCP_speed"][4] = 0.0
    rtde["actual_qd"][3] = 0.00101
    assert not _is_stationary(rtde)


def test_stationarity_window_accepts_bounded_high_frequency_velocity() -> None:
    samples = []
    for index in range(50):
        phase = 2.0 * math.pi * 57.0 * index / 500.0
        q2 = 0.0001 * math.sin(phase)
        qd2 = 0.0036 * math.cos(phase)
        samples.append(
            {
                "actual_TCP_pose": [
                    0.48,
                    0.12,
                    0.08 + 0.00008 * math.sin(phase),
                    3.12,
                    -0.34 + q2,
                    0.0,
                ],
                "actual_TCP_speed": [
                    0.0,
                    0.0,
                    0.0018 * math.cos(phase),
                    0.0,
                    qd2,
                    0.0,
                ],
                "actual_q": [0.0, q2, 0.0, 0.0, 0.0, 0.0],
                "actual_qd": [0.0, qd2, 0.0, 0.0, 0.0, 0.0],
            }
        )

    evidence = _stationarity_window_evidence(samples)
    assert evidence["ok"]
    assert evidence["maximum_joint_position_excursion_rad"] < 0.0003
    assert max(abs(sample["actual_qd"][1]) for sample in samples) > 0.003


def test_stationarity_window_rejects_sustained_drift() -> None:
    samples = []
    for index in range(50):
        elapsed = index / 500.0
        samples.append(
            {
                "actual_TCP_pose": [
                    0.48 + 0.002 * elapsed,
                    0.12,
                    0.08,
                    3.12,
                    -0.34,
                    0.0,
                ],
                "actual_TCP_speed": [0.002, 0.0, 0.0, 0.0, 0.0, 0.0],
                "actual_q": [0.002 * elapsed, 0.0, 0.0, 0.0, 0.0, 0.0],
                "actual_qd": [0.002, 0.0, 0.0, 0.0, 0.0, 0.0],
            }
        )

    evidence = _stationarity_window_evidence(samples)
    assert not evidence["ok"]
    assert not evidence["checks"]["mean_tcp_translation_speed_le_1mm_s"]
    assert not evidence["checks"]["maximum_abs_mean_joint_speed_le_1mrad_s"]


def test_compile_probe_ignores_stale_complete_until_current_active() -> None:
    stale_complete = {
        "output_int_register_32": COMPILE_PROBE_PROTOCOL_TOKEN,
        "output_int_register_24": COMPILE_PROBE_STATE_COMPLETE,
        "runtime_state": RUNTIME_STOPPED,
    }
    assert _update_compile_probe_markers(False, False, stale_complete) == (
        False,
        False,
    )
    active = dict(stale_complete)
    active["output_int_register_24"] = COMPILE_PROBE_STATE_ACTIVE
    active["runtime_state"] = RUNTIME_PLAYING
    assert _update_compile_probe_markers(False, False, active) == (True, False)
    assert _update_compile_probe_markers(True, False, stale_complete) == (
        True,
        True,
    )


def test_receiver_handshake_probe_is_one_reversible_pre_main_injection(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    probe_source = build_receiver_handshake_probe_source(bundle.source)
    prefix = """  local receiver_handshake_probe_tick = 0
  while receiver_handshake_probe_tick < 50:
    sync()
    receiver_handshake_probe_tick = receiver_handshake_probe_tick + 1
  end
  running = False
"""
    assert probe_source.count(prefix) == 1
    assert probe_source.replace(prefix, "", 1) == bundle.source
    assert probe_source.index(prefix) < probe_source.index("  while running:\n")
    assert "thread torqueThread():" in probe_source
    assert "direct_torque(torque, viscous_scale=" in probe_source


def test_receiver_handshake_markers_reject_wrong_protocol_and_torque_state() -> None:
    waiting = {
        "output_int_register_24": STATE_WAITING,
        "output_int_register_32": LIVE_PROTOCOL_TOKEN,
        "runtime_state": RUNTIME_PLAYING,
    }
    assert _update_receiver_handshake_markers(False, False, waiting) == (
        True,
        False,
    )
    wrong_protocol = dict(waiting)
    wrong_protocol["output_int_register_32"] = 0
    assert _update_receiver_handshake_markers(False, False, wrong_protocol) == (
        False,
        False,
    )
    torque = dict(waiting)
    torque["output_int_register_24"] = STATE_TORQUE
    assert _update_receiver_handshake_markers(True, False, torque) == (
        True,
        True,
    )


def test_stage_evidence_prevents_skipping_a_live_canary_stage(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    with pytest.raises(RuntimeError, match="prior_stage_evidence_required"):
        validate_prior_stage_evidence(
            None,
            required_stage=CANARY_STAGE_HOLD,
            bundle=bundle,
            robot_host="192.168.1.18",
        )
    evidence = tmp_path / "hold_evidence.json"
    evidence.write_text(
        json.dumps(
            {
                "schema": CANARY_EVIDENCE_SCHEMA,
                "ok": True,
                "robot_host": "192.168.1.18",
                "receiver_source_sha256": bundle.source_sha256,
                "bundle_manifest_sha256": bundle.manifest_sha256,
                "friction_profile": bundle.friction_profile,
                "reference_artifact_sha256": bundle.reference_sha256,
                "runtime_source_sha256": runtime_source_binding()["sha256"],
                "canary_stage": CANARY_STAGE_HOLD,
                "kunwei_stream_started": True,
                "kunwei_force_source": "kunwei_software_baselined_sensor_to_tcp_si",
                "strict_success_gate": {"ok": True},
            }
        ),
        encoding="utf-8",
    )
    assert validate_prior_stage_evidence(
        evidence,
        required_stage=CANARY_STAGE_HOLD,
        bundle=bundle,
        robot_host="192.168.1.18",
    )["ok"]


def test_run_refuses_partial_cli_authority_before_any_io(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    completed = subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "run",
            "--bundle-manifest",
            str(bundle.manifest_path),
            "--canary-stage",
            CANARY_STAGE_HOLD,
            "--compile-probe-evidence",
            str(tmp_path / "not_read_probe.json"),
            "--output-dir",
            str(tmp_path / "out"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 2
    assert "all_independent_live_cli_gates_are_required" in completed.stderr
    assert not (tmp_path / "out").exists()


def test_manifest_hash_is_bundle_identity(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    assert bundle.manifest_sha256 == hashlib.sha256(
        bundle.manifest_path.read_bytes()
    ).hexdigest()


def test_live_writer_conflict_check_ignores_own_process(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_probe(cmd, *, capture_output: bool, text: bool, check: bool, timeout: float):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="321: python run_tacdiffusion_remote_direct_torque_v4.py\n",
            stderr="",
        )

    with patch("run_tacdiffusion_remote_direct_torque_v4.subprocess.run", fake_probe):
        monkeypatch.setattr(
            "run_tacdiffusion_remote_direct_torque_v4.os.getpid", lambda: 321
        )
        assert not _detect_live_writer_processes()


def test_live_writer_conflict_check_blocks_python_step5d_tacdiffusion_bridge_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_probe(cmd, *, capture_output: bool, text: bool, check: bool, timeout: float):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="123 python run_step5d_tacdiffusion_bridge.py\n",
            stderr="",
        )

    with patch("run_tacdiffusion_remote_direct_torque_v4.subprocess.run", fake_probe):
        monkeypatch.setattr(
            "run_tacdiffusion_remote_direct_torque_v4.os.getpid", lambda: 999
        )
        with pytest.raises(RuntimeError, match="active_live_writer_detected"):
            _enforce_no_live_writer_conflict()


def test_live_writer_conflict_check_blocks_direct_torque_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_probe(cmd, *, capture_output: bool, text: bool, check: bool, timeout: float):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="124 /usr/bin/python3 run_tacdiffusion_remote_direct_torque_v4.py\n",
            stderr="",
        )

    with patch("run_tacdiffusion_remote_direct_torque_v4.subprocess.run", fake_probe):
        monkeypatch.setattr(
            "run_tacdiffusion_remote_direct_torque_v4.os.getpid", lambda: 999
        )
        with pytest.raises(RuntimeError, match="active_live_writer_detected"):
            _enforce_no_live_writer_conflict()


def test_live_writer_conflict_check_ignores_codex_inline_prompt_mentions_rtde_kunwei_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_probe(cmd, *, capture_output: bool, text: bool, check: bool, timeout: float):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="125 python -m codex --prompt 'release bridge RTDE KUNWEI checks'\n",
            stderr="",
        )

    with patch("run_tacdiffusion_remote_direct_torque_v4.subprocess.run", fake_probe):
        monkeypatch.setattr(
            "run_tacdiffusion_remote_direct_torque_v4.os.getpid", lambda: 999
        )
        assert not _detect_live_writer_processes()


def test_live_writer_conflict_check_ignores_pytest_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_probe(cmd, *, capture_output: bool, text: bool, check: bool, timeout: float):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="126 python -m pytest tests/test_run_tacdiffusion_remote_direct_torque_v4.py\n",
            stderr="",
        )

    with patch("run_tacdiffusion_remote_direct_torque_v4.subprocess.run", fake_probe):
        monkeypatch.setattr(
            "run_tacdiffusion_remote_direct_torque_v4.os.getpid", lambda: 999
        )
        assert not _detect_live_writer_processes()


def test_live_writer_probe_fails_closed_on_probe_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_probe(cmd, *, capture_output: bool, text: bool, check: bool, timeout: float):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    with patch("run_tacdiffusion_remote_direct_torque_v4.subprocess.run", fake_probe):
        with pytest.raises(RuntimeError, match="active_writer_probe_failed"):
            _detect_live_writer_processes()


def test_live_writer_pattern_list_covers_expected_endpoints() -> None:
    assert "run_step5d_tacdiffusion_bridge.py" in _LIVE_WRITER_PATTERNS
    assert "run_tacdiffusion_remote_direct_torque_v4.py" in _LIVE_WRITER_PATTERNS
    assert "step5d_tacdiffusion_bridge" in _LIVE_WRITER_PATTERNS
    assert "codex" in _LIVE_WRITER_IGNORED_PATTERNS
    assert "pytest" in _LIVE_WRITER_IGNORED_PATTERNS
    assert "pgrep" in _LIVE_WRITER_IGNORED_PATTERNS


def test_live_preflight_requires_1mm_release_translation_check(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    expected = list(bundle.timeline.rows[0]["desired_pose_base"])
    drift_pose = [
        expected[0] + NO_CONTACT_RELEASE_TOLERANCE_M + 1e-6,
        *expected[1:],
    ]
    status = {
        "dashboard": {
            "PolyscopeVersion": "URSoftware 5.26.",
            "safetystatus": "Safetystatus: NORMAL",
            "robotmode": "Robotmode: RUNNING",
        },
        "rtde": {
            "runtime_state": 1,
            "robot_mode": 7,
            "safety_mode": 1,
            "actual_TCP_pose": drift_pose,
            "actual_TCP_speed": [0.0] * 6,
            "actual_TCP_force": [0.0] * 6,
            "actual_qd": [0.0] * 6,
        },
        "remote_control": True,
        "stopped": True,
        "stationary": True,
    }
    with pytest.raises(RuntimeError, match="release_translation_error_exceeds_1mm"):
        validate_live_preflight(status, bundle, timeline=bundle.timeline)
    status["rtde"]["actual_TCP_pose"] = [
        expected[0] + NO_CONTACT_RELEASE_TOLERANCE_M * 0.5,
        *expected[1:],
    ]
    validate_live_preflight(status, bundle, timeline=bundle.timeline)


def test_next_available_output_dir_is_not_overwritten(tmp_path: Path) -> None:
    existing = tmp_path / "live-run"
    existing.mkdir()
    (existing / "evidence.json").write_text("{}", encoding="utf-8")
    allocated = _next_available_run_dir(existing)
    assert allocated != existing
    assert allocated.name == "live-run_1"
    assert not allocated.exists()


def test_wait_for_handshake_rejects_stale_waiting_marker_before_playing(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeRTDE:
        def __init__(self, samples: list[dict[str, object]]):
            self.samples = list(samples)

        def receive_latest(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> dict[str, object] | None:
            if not self.samples:
                return None
            return self.samples.pop(0)

    stale_sample = {
        "output_int_register_24": STATE_WAITING,
        "runtime_state": RUNTIME_STOPPED,
        "output_int_register_32": LIVE_PROTOCOL_TOKEN,
    }

    class FakeClock:
        def __init__(self) -> None:
            self.now = 0.0

        def __call__(self) -> float:
            self.now += 0.01
            return self.now

    clock = FakeClock()
    with patch("run_tacdiffusion_remote_direct_torque_v4.time.monotonic", clock):
        with pytest.raises(RuntimeError, match="receiver_waiting_stale_after_send"):
            _wait_for_fresh_receiver_waiting(
                FakeRTDE([stale_sample]),
                0,
                [],
                [],
                receiver_wait_s=0.02,
                lease_id=111,
                episode_identity=222,
            )


def test_wait_for_handshake_accepts_playing_marker_with_matching_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeRTDE:
        def __init__(self, samples: list[dict[str, object]]):
            self.samples = list(samples)

        def receive_latest(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> dict[str, object] | None:
            if not self.samples:
                return None
            return self.samples.pop(0)

    samples = [
        {
            "output_int_register_24": STATE_WAITING,
            "runtime_state": RUNTIME_STOPPED,
            "output_int_register_32": LIVE_PROTOCOL_TOKEN,
        },
        {
            "output_int_register_24": STATE_WAITING,
            "runtime_state": RUNTIME_PLAYING,
            "output_int_register_27": 999,
            "output_int_register_31": 888,
            "output_int_register_32": LIVE_PROTOCOL_TOKEN,
            "output_int_register_34": MODE_RUN,
            "output_int_register_35": 1,
        },
        {
            "output_int_register_24": STATE_WAITING,
            "runtime_state": RUNTIME_PLAYING,
            "output_int_register_27": 111,
            "output_int_register_31": 222,
            "output_int_register_32": LIVE_PROTOCOL_TOKEN,
            "output_int_register_34": MODE_IDLE,
            "output_int_register_35": 1,
        },
    ]

    class FakeClock:
        def __init__(self) -> None:
            self.now = 0.0

        def __call__(self) -> float:
            self.now += 0.01
            return self.now

    clock = FakeClock()
    accepted_samples: list[dict[str, Any]] = []
    diagnostic_samples: list[dict[str, Any]] = []
    with patch("run_tacdiffusion_remote_direct_torque_v4.time.monotonic", clock):
        start, sample = _wait_for_fresh_receiver_waiting(
            FakeRTDE(samples),
            0,
            [],
            [],
            receiver_wait_s=1.0,
            lease_id=111,
            episode_identity=222,
            samples_out=accepted_samples,
            diagnostic_samples_out=diagnostic_samples,
        )
    assert start > 0.04
    assert sample["runtime_state"] == RUNTIME_PLAYING
    assert accepted_samples == [sample]
    assert diagnostic_samples == samples


def test_receiver_handshake_summary_localizes_startup_latch_failure() -> None:
    sample = {
        "timestamp": 10.0,
        "runtime_state": RUNTIME_PLAYING,
        "output_int_register_24": STATE_WAITING,
        "output_int_register_27": 0,
        "output_int_register_31": 0,
        "output_int_register_32": LIVE_PROTOCOL_TOKEN,
        "output_int_register_34": MODE_IDLE,
        "output_int_register_35": 0,
    }
    summary = summarize_receiver_handshake_samples(
        [sample],
        lease_id=111,
        episode_identity=222,
    )
    assert summary == {
        "total_rows": 1,
        "protocol_rows": 1,
        "waiting_rows": 1,
        "waiting_playing_rows": 1,
        "idle_echo_rows": 1,
        "episode_latched_rows": 0,
        "lease_match_rows": 0,
        "episode_match_rows": 0,
        "accepted_rows": 0,
        "last_observed": {
            "controller_timestamp_s": 10.0,
            "runtime_state": RUNTIME_PLAYING,
            "receiver_state": STATE_WAITING,
            "protocol_echo": LIVE_PROTOCOL_TOKEN,
            "observed_command_echo": MODE_IDLE,
            "episode_latched_echo": 0,
            "lease_echo": 0,
            "episode_echo": 0,
        },
    }


def test_stale_terminal_receiver_allows_one_resend() -> None:
    stale = {
        "timestamp": 10.0,
        "runtime_state": RUNTIME_STOPPED,
        "output_int_register_24": STATE_COMPLETE,
        "output_int_register_27": 111,
        "output_int_register_31": 222,
        "output_int_register_32": LIVE_PROTOCOL_TOKEN,
        "output_int_register_34": MODE_END,
        "output_int_register_35": 1,
    }
    assert _stale_terminal_receiver_allows_one_resend(
        [stale],
        lease_id=333,
        episode_identity=444,
    )


@pytest.mark.parametrize(
    ("runtime_state", "receiver_state", "lease_echo", "episode_echo"),
    [
        (RUNTIME_PLAYING, STATE_COMPLETE, 111, 222),
        (RUNTIME_STOPPED, STATE_WAITING, 111, 222),
        (RUNTIME_STOPPED, STATE_COMPLETE, 333, 222),
        (RUNTIME_STOPPED, STATE_COMPLETE, 111, 444),
    ],
)
def test_receiver_resend_is_blocked_after_any_new_execution_evidence(
    runtime_state: int,
    receiver_state: int,
    lease_echo: int,
    episode_echo: int,
) -> None:
    sample = {
        "timestamp": 10.0,
        "runtime_state": runtime_state,
        "output_int_register_24": receiver_state,
        "output_int_register_27": lease_echo,
        "output_int_register_31": episode_echo,
        "output_int_register_32": LIVE_PROTOCOL_TOKEN,
        "output_int_register_34": MODE_IDLE,
        "output_int_register_35": 1,
    }
    assert not _stale_terminal_receiver_allows_one_resend(
        [sample],
        lease_id=333,
        episode_identity=444,
    )


def test_run_live_rejects_partial_cli_gates_before_preflight_or_connect(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    args = SimpleNamespace(
        live=True,
        send_urscript=False,
        write_rtde_inputs=True,
        allow_direct_torque=True,
        allow_motion=True,
        no_contact=True,
        allow_kunwei_stream_command=True,
        canary_stage=CANARY_STAGE_REFERENCE,
        compile_probe_evidence=Path("unused-probe.json"),
        prior_stage_evidence=Path("unused-prior.json"),
        output_dir=tmp_path / "output",
        robot_host="192.168.1.18",
        connect_timeout_s=1.0,
        receiver_wait_s=1.0,
    )
    with patch(
        "run_tacdiffusion_remote_direct_torque_v4.readonly_status"
    ) as mock_status, patch(
        "run_tacdiffusion_remote_direct_torque_v4._live_writer_lease"
    ) as mock_lease:
        with pytest.raises(RuntimeError, match="all_independent_live_cli_gates_are_required"):
            run_live(args, bundle)
        mock_status.assert_not_called()
        mock_lease.assert_not_called()


def test_run_live_maps_canonical_writer_lock_contention_to_stable_error(
    tmp_path: Path,
) -> None:
    args = SimpleNamespace(
        live=True,
        send_urscript=True,
        write_rtde_inputs=True,
        allow_direct_torque=True,
        allow_motion=True,
        no_contact=True,
        allow_kunwei_stream_command=True,
        canary_stage=CANARY_STAGE_REFERENCE,
        compile_probe_evidence=Path("unused-probe.json"),
        prior_stage_evidence=Path("unused-prior.json"),
    )
    profile = object()
    with patch(
        "run_tacdiffusion_remote_direct_torque_v4.ResourceProfile.from_env",
        return_value=profile,
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4.writer_lease",
        side_effect=BlockingIOError,
    ):
        with pytest.raises(RuntimeError, match=r"^live_writer_lock_unavailable$"):
            run_live(args, object())


def test_run_live_lock_order_is_before_preflight_and_connection() -> None:
    events: list[str] = []
    profile = object()
    bundle = SimpleNamespace(
        timeline=SimpleNamespace(rows=[{"desired_pose_base": (0.0,) * 6}])
    )

    class FakeLease:
        def __enter__(self):
            events.append("lease_acquire")
            return self

        def __exit__(self, *exc: object) -> None:
            events.append("lease_release")

    def fake_writer_lease(*args: object, **kwargs: object) -> FakeLease:
        assert args == (profile, "tacdiffusion-remote-direct-torque-v4")
        assert kwargs == {"blocking": False}
        return FakeLease()

    args = SimpleNamespace(
        live=True,
        send_urscript=True,
        write_rtde_inputs=True,
        allow_direct_torque=True,
        allow_motion=True,
        no_contact=True,
        allow_kunwei_stream_command=True,
        canary_stage=CANARY_STAGE_REFERENCE,
        compile_probe_evidence=Path("unused-probe.json"),
        prior_stage_evidence=Path("unused-prior.json"),
        robot_host="192.168.1.18",
        output_dir=Path("unused-output"),
        connect_timeout_s=1.0,
        receiver_wait_s=1.0,
        kunwei_calibration=KUNWEI_CALIBRATION,
        sensor_ip="192.168.50.25",
        sensor_port=5152,
    )

    def fake_status(*args: object, **kwargs: object) -> dict[str, object]:
        events.append("readonly_status")
        return {"rtde": {"actual_TCP_pose": [0.0] * 6}}

    def fake_preflight(*args: object, **kwargs: object) -> None:
        events.append("preflight")

    def fake_conflict() -> None:
        events.append("legacy_conflict_scan")

    class FakeRTDE:
        def __init__(self, *args: object, **kwargs: object) -> None:
            events.append("controller_connection")
            raise RuntimeError("stop_before_controller_io")

    with patch(
        "run_tacdiffusion_remote_direct_torque_v4.ResourceProfile.from_env",
        return_value=profile,
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4.writer_lease",
        side_effect=fake_writer_lease,
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4.validate_compile_probe_evidence",
        return_value={"ok": True},
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4.validate_prior_stage_evidence",
        return_value={"ok": True},
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4.readonly_status",
        side_effect=fake_status,
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4.CanaryTimeline.from_stage",
        return_value=bundle.timeline,
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4.validate_live_preflight",
        side_effect=fake_preflight,
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4._enforce_no_live_writer_conflict",
        side_effect=fake_conflict,
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4._new_live_identity_pair",
        return_value=(111, 222),
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4.LiveRTDE",
        FakeRTDE,
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4.KunweiGuardCapture",
        FakeKunweiCapture,
    ):
        with pytest.raises(RuntimeError, match="stop_before_controller_io"):
            run_live(args, bundle)

    assert events == [
        "lease_acquire",
        "readonly_status",
        "preflight",
        "legacy_conflict_scan",
        "controller_connection",
        "lease_release",
    ]


def test_run_live_conflict_failure_stays_inside_lease_and_releases() -> None:
    events: list[str] = []
    profile = object()

    @contextmanager
    def fake_lease():
        events.append("lease_acquire")
        try:
            yield
        finally:
            events.append("lease_release")

    args = SimpleNamespace(
        live=True,
        send_urscript=True,
        write_rtde_inputs=True,
        allow_direct_torque=True,
        allow_motion=True,
        no_contact=True,
        allow_kunwei_stream_command=True,
        canary_stage=CANARY_STAGE_REFERENCE,
        compile_probe_evidence=Path("unused-probe.json"),
        prior_stage_evidence=Path("unused-prior.json"),
        robot_host="192.168.1.18",
        output_dir=Path("unused-output"),
        kunwei_calibration=KUNWEI_CALIBRATION,
    )

    def fake_conflict() -> None:
        events.append("legacy_conflict_scan")
        raise RuntimeError("active_live_writer_detected:legacy")

    with patch(
        "run_tacdiffusion_remote_direct_torque_v4.ResourceProfile.from_env",
        return_value=profile,
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4.writer_lease",
        return_value=fake_lease(),
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4.validate_compile_probe_evidence",
        return_value={"ok": True},
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4.validate_prior_stage_evidence",
        return_value={"ok": True},
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4.readonly_status",
        return_value={"rtde": {"actual_TCP_pose": [0.0] * 6}},
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4.CanaryTimeline.from_stage",
        return_value=SimpleNamespace(rows=[{"desired_pose_base": (0.0,) * 6}]),
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4.validate_live_preflight",
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4._enforce_no_live_writer_conflict",
        side_effect=fake_conflict,
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4.LiveRTDE",
    ) as mock_rtde:
        with pytest.raises(RuntimeError, match="active_live_writer_detected:legacy"):
            run_live(args, object())

    assert events == ["lease_acquire", "legacy_conflict_scan", "lease_release"]
    mock_rtde.assert_not_called()


def test_run_live_emits_failure_evidence_after_live_write_and_advances_run_dir(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    compile_probe_evidence = _write_compile_probe_evidence(tmp_path)
    output_root = tmp_path / "run"
    output_root.mkdir()
    (output_root / "evidence.json").write_text("{}", encoding="utf-8")

    handshake_sample: dict[str, Any] = {
        "timestamp": 0.0,
        "runtime_state": RUNTIME_PLAYING,
        "robot_mode": 7,
        "safety_mode": 1,
        "output_int_register_24": STATE_WAITING,
        "output_int_register_25": 0,
        "output_int_register_26": 0,
        "output_int_register_27": 111,
        "output_int_register_28": 0,
        "output_int_register_29": 0,
        "output_int_register_30": LIVE_PROTOCOL_TOKEN,
        "output_int_register_31": 222,
        "output_int_register_32": LIVE_PROTOCOL_TOKEN,
        "output_int_register_33": 0,
        "output_int_register_34": MODE_IDLE,
        "output_int_register_35": 1,
        "output_double_register_24": 0.0,
        "output_double_register_25": 0.0,
        "output_double_register_26": 0.0,
        "output_double_register_27": 0.0,
        "output_double_register_28": 0.0,
        "output_double_register_29": 0.0,
        "output_double_register_30": 0.0,
        "output_double_register_31": 0.0,
        "output_double_register_32": 0.0,
        "output_double_register_33": 0.0,
        "output_double_register_34": 0.0,
        "output_double_register_35": 0.0,
        "output_double_register_36": 0.0,
        "output_double_register_37": 0.0,
        "output_double_register_38": 0.0,
        "output_double_register_39": 0.0,
        "output_double_register_40": 0.0,
        "output_double_register_41": 0.0,
        "output_double_register_42": 0.0,
        "output_double_register_43": 0.0,
        "output_double_register_44": 0.0,
        "output_double_register_45": 0.0,
        "output_double_register_46": 0.0,
        "output_double_register_47": 0.0,
        "actual_TCP_pose": list(bundle.timeline.rows[0]["desired_pose_base"]),
        "actual_TCP_speed": [0.0] * 6,
        "actual_TCP_force": [0.0] * 6,
        "actual_q": [0.0] * 6,
        "actual_qd": [0.0] * 6,
        "target_moment": [0.0] * 6,
    }

    events: list[str] = []

    @contextmanager
    def fake_lease():
        events.append("lease_acquire")
        try:
            yield
        finally:
            events.append("lease_release")

    class FakeRTDE:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.calls = 0
            events.append("controller_connection")

        def __enter__(self):
            events.append("rtde_enter")
            return self

        def __exit__(self, *exc) -> None:
            events.append("rtde_close")
            return None

        def negotiate(self) -> None:
            return None

        def setup_outputs(self, *_args: object, **_kwargs: object):
            return 1, OUTPUT_FIELDS

        def setup_inputs(self, *_args: object, **_kwargs: object):
            events.append("rtde_input_setup")
            return 1, ["i"] * len(OUTPUT_FIELDS)

        def start(self) -> None:
            return None

        def send_inputs(self, *_args: object, **_kwargs: object) -> None:
            self.calls += 1
            events.append(f"rtde_input_write_{self.calls}")
            if self.calls == 3:
                events.append("safe_abort")
            if self.calls == 2:
                raise RuntimeError("injected_rtde_send_failure")

        def receive_latest(self, *_args: object, **_kwargs: object):
            return handshake_sample

    valid_status = {
        "dashboard": {
            "PolyscopeVersion": "URSoftware 5.26.",
            "robotmode": "Robotmode: RUNNING",
            "safetystatus": "Safetystatus: NORMAL",
            "is in remote control": "True",
        },
        "rtde": {
            "actual_TCP_pose": list(bundle.timeline.rows[0]["desired_pose_base"]),
            "actual_TCP_speed": [0.0] * 6,
            "actual_TCP_force": [0.0] * 6,
            "actual_qd": [0.0] * 6,
            "runtime_state": RUNTIME_STOPPED,
            "robot_mode": 7,
            "safety_mode": 1,
        },
        "remote_control": True,
        "stopped": True,
        "stationary": True,
    }

    args = SimpleNamespace(
        live=True,
        send_urscript=True,
        write_rtde_inputs=True,
        allow_direct_torque=True,
        allow_motion=True,
        no_contact=True,
        allow_kunwei_stream_command=True,
        canary_stage=CANARY_STAGE_HOLD,
        compile_probe_evidence=compile_probe_evidence,
        prior_stage_evidence=None,
        output_dir=output_root,
        robot_host="192.168.1.18",
        connect_timeout_s=1.0,
        receiver_wait_s=1.0,
        kunwei_calibration=KUNWEI_CALIBRATION,
        sensor_ip="192.168.50.25",
        sensor_port=5152,
    )

    def record_evidence_close(path: Path, payload: dict[str, Any]) -> None:
        _write_json_new(path, payload)
        events.append("evidence_close")

    def fake_urscript_send(*args: object, **kwargs: object) -> None:
        events.append("urscript_send")

    def fake_direct_torque_run(*args: object, **kwargs: object):
        events.append("direct_torque_run")
        return 123.0, handshake_sample

    with patch(
        "run_tacdiffusion_remote_direct_torque_v4._live_writer_lease",
        return_value=fake_lease(),
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4._write_json_new",
        side_effect=record_evidence_close,
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4.readonly_status",
        return_value=valid_status,
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4.LiveRTDE",
        FakeRTDE,
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4.KunweiGuardCapture",
        FakeKunweiCapture,
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4._new_live_identity_pair",
        return_value=(111, 222),
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4._enforce_no_live_writer_conflict",
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4._send_urscript",
        side_effect=fake_urscript_send,
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4._prime_idle_inputs",
        side_effect=lambda rtde, input_recipe, input_types, idle_values, *_args, **_kwargs: rtde.send_inputs(
            input_recipe, input_types, idle_values
        ),
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4.validate_live_preflight",
    ), patch(
        "run_tacdiffusion_remote_direct_torque_v4._wait_for_fresh_receiver_waiting",
        side_effect=fake_direct_torque_run,
    ):
        with pytest.raises(RuntimeError, match="injected_rtde_send_failure"):
            run_live(args, bundle)

    failure_run = output_root.parent / "run_1"
    assert failure_run.is_dir()
    assert (failure_run / "evidence.json").is_file()
    assert (output_root / "evidence.json").read_text(encoding="utf-8") == "{}"
    evidence = json.loads((failure_run / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["failure"] == "RuntimeError: injected_rtde_send_failure"
    assert evidence["sample_count"] > 0
    assert (failure_run / "direct_torque_rtde.csv").is_file()

    assert events.index("lease_acquire") < events.index("rtde_input_setup")
    assert events.index("rtde_input_setup") < events.index("rtde_input_write_1")
    assert events.index("rtde_input_write_1") < events.index("urscript_send")
    assert events.index("urscript_send") < events.index("direct_torque_run")
    assert events.index("direct_torque_run") < events.index("safe_abort")
    assert events.index("safe_abort") < events.index("evidence_close")
    assert events.index("evidence_close") < events.index("lease_release")
