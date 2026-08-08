"""PATH state-25 force+integral sidecar (hard-stop reconstructability)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT.parents[1] / "src" / "ur10e_experiment_runtime"))

from step5d_autotune_v4_r008.state25_path_trace import (  # noqa: E402
    STATE25_PATH_TRACE_OPTIONAL_DIAG_FIELDS,
    STATE25_PATH_TRACE_SCHEMA,
    STATE25_PATH_TRACE_SIDECAR_NAME,
    STATE25_STOP_DOMINANT_NAME,
    State25PathTrace,
    append_state25_sidecar,
    attach_state25_trace,
    build_state25_row,
    build_state25_stop_dominant_context,
    dump_state25_stop_dominant_json,
    integral_saturation_fraction,
    is_integral_saturated,
    read_live_force_integral,
)


def test_build_row_includes_integral_and_z(tmp_path: Path) -> None:
    row = build_state25_row(
        monotonic_s=10.5,
        wall_time_s=1_700_000_000.0,
        tcp_pose_m_rad=(0.1, 0.2, 0.0194, 0.0, 0.0, 0.0),
        normal_load_n=12.5,
        force_norm_n=13.0,
        force_integral_n_s=29.035,
        force_integral_limit_n_s=50.0,
        filtered_normal_n=12.4,
        command_mode=2,
        packet_sequence=42,
        attempt_ordinal=1,
    )
    assert row["schema"] == STATE25_PATH_TRACE_SCHEMA
    assert row["tcp_z_m"] == pytest.approx(0.0194)
    assert row["force_integral_n_s"] == pytest.approx(29.035)
    assert row["force_integral_limit_n_s"] == pytest.approx(50.0)
    assert row["integral_saturated"] is False
    path = append_state25_sidecar(tmp_path, row)
    assert path.name == STATE25_PATH_TRACE_SIDECAR_NAME
    loaded = json.loads(path.read_text(encoding="utf-8").strip())
    assert loaded["force_integral_n_s"] == pytest.approx(29.035)


def test_build_row_optional_ki_pocket_diag_fields() -> None:
    row = build_state25_row(
        monotonic_s=1.0,
        tcp_pose_m_rad=(0.0, 0.0, 0.02, 0.0, 0.0, 0.0),
        normal_load_n=6.5,
        force_norm_n=6.6,
        force_integral_n_s=49.8,
        force_integral_limit_n_s=50.0,
        command_mode=2,
        attempt_ordinal=10,
        force_i_gain=0.001810193359837562,
        force_p_gain=0.0028284271248,
        force_damping=28.0,
        force_target_n=5.0,
        force_error_n=-1.5,
    )
    assert row["force_i_gain"] == pytest.approx(0.001810193359837562)
    assert row["force_p_gain"] == pytest.approx(0.0028284271248)
    assert row["force_damping"] == pytest.approx(28.0)
    assert row["force_target_n"] == pytest.approx(5.0)
    assert row["force_error_n"] == pytest.approx(-1.5)
    assert row["integral_saturated"] is True
    assert integral_saturation_fraction(row) == pytest.approx(0.996)
    assert is_integral_saturated(row) is True
    for key in STATE25_PATH_TRACE_OPTIONAL_DIAG_FIELDS:
        assert key in row


def test_integral_saturation_helpers_handle_missing_limit() -> None:
    row = {"force_integral_n_s": 12.0}
    assert integral_saturation_fraction(row) is None
    assert is_integral_saturated(row) is None


def test_trace_observe_ring_stop_dump_has_integral_stats(tmp_path: Path) -> None:
    trace = State25PathTrace(tmp_path, ring_size=3)
    for index in range(5):
        trace.observe(
            build_state25_row(
                monotonic_s=float(index),
                tcp_pose_m_rad=(0.0, 0.0, 0.01 * index, 0.0, 0.0, 0.0),
                normal_load_n=float(10 + index),
                force_norm_n=float(10 + index) + 0.1,
                force_integral_n_s=float(index * 5.0),
                force_integral_limit_n_s=50.0,
                command_mode=2,
            )
        )
    assert trace.rows_written == 5
    assert len(trace.recent_rows()) == 3
    assert trace.abs_force_integral_max_n_s == pytest.approx(20.0)
    context = trace.stop_dominant_context(
        reason_code=61,
        reason="hard_abs_normal_60n",
        wrench=(0.0, 0.0, -61.0, 0.0, 0.0, 0.0),
        tp_state=25,
        sensor_fresh=True,
        command_mode=2,
        normal_load_n=61.0,
        force_norm_n=61.0,
        force_integral_n_s=20.0,
        force_integral_limit_n_s=50.0,
    )
    dump = trace.dump_stop_dominant(context)
    assert dump is not None
    assert dump.name == STATE25_STOP_DOMINANT_NAME
    payload = json.loads(dump.read_text(encoding="utf-8"))
    assert payload["reason_code"] == 61
    assert payload["tp_state"] == 25
    assert payload["ring_source"] == "tp_state_25"
    assert payload["force_integral_n_s"] == pytest.approx(20.0)
    assert payload["force_integral_limit_n_s"] == pytest.approx(50.0)
    assert payload["abs_force_integral_max_n_s"] == pytest.approx(20.0)
    assert len(payload["last_state25_samples"]) == 3
    trace.flush()
    lines = (tmp_path / STATE25_PATH_TRACE_SIDECAR_NAME).read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 5
    trace.close()


def test_read_live_force_integral_from_qualification_runtime() -> None:
    owner = SimpleNamespace(
        _qualification_control=SimpleNamespace(
            _runtime=SimpleNamespace(
                _outer_state=SimpleNamespace(force_integral_n_s=12.5),
                force_integral_limit_n_s=50.0,
            )
        )
    )
    integral, limit = read_live_force_integral(owner)
    assert integral == pytest.approx(12.5)
    assert limit == pytest.approx(50.0)


def test_attach_state25_trace_binds_run_dir(tmp_path: Path) -> None:
    owner = type("Owner", (), {})()
    trace = attach_state25_trace(owner, tmp_path)
    assert owner._state25_trace is trace
    assert trace.run_dir == tmp_path


def test_attempt_ordinal_change_resets_integral_max(tmp_path: Path) -> None:
    trace = State25PathTrace(tmp_path, ring_size=8)
    trace.observe(
        build_state25_row(
            monotonic_s=1.0,
            tcp_pose_m_rad=(0.0, 0.0, 0.02, 0.0, 0.0, 0.0),
            normal_load_n=5.0,
            force_norm_n=5.0,
            force_integral_n_s=40.0,
            force_integral_limit_n_s=50.0,
            attempt_ordinal=41,
        )
    )
    assert trace.abs_force_integral_max_n_s == pytest.approx(40.0)
    trace.observe(
        build_state25_row(
            monotonic_s=2.0,
            tcp_pose_m_rad=(0.0, 0.0, 0.02, 0.0, 0.0, 0.0),
            normal_load_n=5.0,
            force_norm_n=5.0,
            force_integral_n_s=3.0,
            force_integral_limit_n_s=50.0,
            attempt_ordinal=42,
        )
    )
    assert len(trace.recent_rows()) == 1
    assert trace.abs_force_integral_max_n_s == pytest.approx(3.0)
    trace.close()


def test_observe_does_not_open_sidecar_on_calling_thread(tmp_path: Path, monkeypatch) -> None:
    """Motion-thread observe must enqueue only; disk open stays in writer thread."""

    import threading

    import step5d_autotune_v4_r008.state25_path_trace as mod

    caller = threading.current_thread().ident
    opened_on_caller = []

    real_open = Path.open

    def tracked_open(self, *args, **kwargs):
        if self.name == STATE25_PATH_TRACE_SIDECAR_NAME:
            opened_on_caller.append(threading.current_thread().ident == caller)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", tracked_open)
    # Construct after patch so writer thread's open is tracked separately.
    trace = State25PathTrace(tmp_path, ring_size=8, write_queue_max=64)
    # Writer may already have opened for append at start — that must not be caller.
    assert all(flag is False for flag in opened_on_caller)
    opened_on_caller.clear()
    for index in range(20):
        trace.observe(
            build_state25_row(
                monotonic_s=float(index),
                tcp_pose_m_rad=(0.0, 0.0, 0.02, 0.0, 0.0, 0.0),
                normal_load_n=5.0,
                force_norm_n=5.0,
                force_integral_n_s=1.0,
                force_integral_limit_n_s=50.0,
                command_mode=2,
            )
        )
    assert opened_on_caller == [] or all(flag is False for flag in opened_on_caller)
    assert trace.rows_written == 20
    trace.flush()
    lines = (tmp_path / STATE25_PATH_TRACE_SIDECAR_NAME).read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 20
    trace.close()


def test_writer_drop_oldest_when_queue_saturated(tmp_path: Path) -> None:
    trace = State25PathTrace(tmp_path, ring_size=8, write_queue_max=2)
    # Pause writer by flooding faster than disk; drop-oldest must not raise.
    for index in range(50):
        trace.observe(
            build_state25_row(
                monotonic_s=float(index),
                tcp_pose_m_rad=(0.0, 0.0, 0.02, 0.0, 0.0, 0.0),
                normal_load_n=5.0,
                force_norm_n=5.0,
                force_integral_n_s=1.0,
                force_integral_limit_n_s=50.0,
                command_mode=2,
            )
        )
    assert trace.rows_written == 50
    trace.flush()
    assert trace.sidecar_dropped >= 0
    trace.close()


def test_dump_helper_writes_named_file(tmp_path: Path) -> None:
    context = build_state25_stop_dominant_context(
        reason_code=61,
        reason="hard_abs_normal_60n",
        wrench=(0.0, 0.0, -61.0, 0.0, 0.0, 0.0),
        tp_state=25,
        sensor_fresh=True,
        command_mode=2,
        force_integral_n_s=40.0,
        force_integral_limit_n_s=50.0,
        abs_force_integral_max_n_s=40.0,
    )
    path = dump_state25_stop_dominant_json(tmp_path, context)
    assert path.name == STATE25_STOP_DOMINANT_NAME
    assert json.loads(path.read_text(encoding="utf-8"))["force_integral_n_s"] == 40.0


def test_r006_send_packet_source_observes_state25() -> None:
    """Source-level check: PATH observe lives on R006LiveWriter, not frozen r004."""

    import ast

    live_path = ROOT / "tools" / "step5d_autotune_v4_r006" / "live_adapter.py"
    source = live_path.read_text(encoding="utf-8")
    assert "tp_state == 25" in source
    assert "build_state25_row" in source
    assert "state25_path_trace" in source
    # Ensure the frozen writer file still has no PATH observe hook.
    frozen = (ROOT / "tools" / "step5d_autotune_v4_r004_live_writer.py").read_text(encoding="utf-8")
    assert "build_state25_row" not in frozen
    assert "state25_path_trace" not in frozen
    # Syntax of the r006 module must still parse after the edit.
    ast.parse(source)


def test_r006_path_stop_writes_state25_stop_dominant(tmp_path: Path, monkeypatch) -> None:
    pytest.importorskip("pinocchio")
    import step5d_autotune_v4_r006.live_adapter as r006_live
    import step5d_autotune_v4_r004_live_writer as writer_cli
    from step5d_autotune_v4_r006.live_adapter import R006LiveWriter
    from step5d_autotune_v4_r008.state20_search_trace import STATE20_STOP_DOMINANT_NAME

    writer = object.__new__(R006LiveWriter)
    writer._stopped = False
    writer._last_output = type(
        "Out",
        (),
        {
            "integer_echoes": {26: 25},
            "tcp_pose_m_rad": (0.48, 0.13, 0.0194, 0.0, 0.0, 0.0),
            "observed_at_s": 100.0,
            "timestamp": 200.0,
        },
    )()
    writer._packet_sequence = 9
    writer._ordinal = 42
    writer.prerequisites = SimpleNamespace(session_epoch=1)
    writer._mono_clock = lambda: 50.0  # type: ignore[method-assign]
    writer._qualification_control = SimpleNamespace(
        _runtime=SimpleNamespace(
            _outer_state=SimpleNamespace(force_integral_n_s=33.0),
            force_integral_limit_n_s=50.0,
        )
    )
    attach_state25_trace(writer, tmp_path)

    ok_packet = type(
        "Packet",
        (),
        {"stop_dominant": False, "reason_code": 0, "reason": "ok"},
    )()
    monkeypatch.setattr(
        r006_live.LiveR004Writer,
        "_send_packet",
        lambda self, *args, **kwargs: ok_packet,
    )
    sensor = type(
        "Sensor",
        (),
        {
            "wrench": (0.0, 0.0, -8.0, 0.0, 0.0, 0.0),
            "sensor_fresh": True,
            "normal_load_n": 8.0,
            "force_norm_n": 8.2,
            "torque_norm_nm": 0.1,
            "filtered_normal_n": 8.0,
        },
    )()
    writer._send_packet(sensor, command_mode=2)
    assert (tmp_path / STATE25_PATH_TRACE_SIDECAR_NAME).is_file()
    row = json.loads(
        (tmp_path / STATE25_PATH_TRACE_SIDECAR_NAME).read_text(encoding="utf-8").strip()
    )
    assert row["tp_state"] == 25
    assert row["force_integral_n_s"] == pytest.approx(33.0)

    stop_packet = type(
        "Packet",
        (),
        {"stop_dominant": True, "reason_code": 61, "reason": "hard_abs_normal_60n"},
    )()
    monkeypatch.setattr(
        r006_live.LiveR004Writer,
        "_send_packet",
        lambda self, *args, **kwargs: stop_packet,
    )
    sensor_stop = type(
        "Sensor",
        (),
        {
            "wrench": (0.0, 0.0, -61.0, 0.0, 0.0, 0.0),
            "sensor_fresh": True,
            "normal_load_n": 60.7,
            "force_norm_n": 61.1,
            "torque_norm_nm": 0.2,
            "filtered_normal_n": 58.0,
        },
    )()
    with pytest.raises(writer_cli.LiveWriterError, match="reason_code=61 reason=hard_abs_normal_60n"):
        writer._send_packet(sensor_stop, command_mode=2)

    dump = json.loads((tmp_path / STATE25_STOP_DOMINANT_NAME).read_text(encoding="utf-8"))
    assert dump["tp_state"] == 25
    assert dump["force_integral_n_s"] == pytest.approx(33.0)
    assert dump["abs_force_integral_max_n_s"] == pytest.approx(33.0)
    assert not (tmp_path / STATE20_STOP_DOMINANT_NAME).exists()
