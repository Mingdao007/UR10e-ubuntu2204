"""B3 Wave 1: state-20 travel-vs-force sidecar helper (observation only)."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

import step5d_autotune_v4_r004.contracts as r004_contracts
import step5d_autotune_v4_r004_live_writer as writer_cli
import step5d_autotune_v4_r006.live_adapter as r006_live
from step5d_autotune_v4_r006.live_adapter import R006LiveWriter
from step5d_autotune_v4_r008.state20_search_trace import (
    STATE20_SEARCH_TRACE_SCHEMA,
    STATE20_SEARCH_TRACE_SIDECAR_NAME,
    STATE20_STOP_DOMINANT_NAME,
    State20SearchTrace,
    append_state20_sidecar,
    attach_state20_trace,
    build_state20_row,
    build_stop_dominant_context,
    dump_stop_dominant_json,
    format_stop_dominant_error,
    merge_state20_into_exception_detail,
)


def test_build_row_and_sidecar_roundtrip(tmp_path: Path) -> None:
    row = build_state20_row(
        monotonic_s=10.5,
        wall_time_s=1_700_000_000.0,
        tcp_pose_m_rad=(0.1, 0.2, 0.3, 0.0, 0.0, 0.0),
        normal_load_n=12.5,
        force_norm_n=13.0,
        filtered_normal_n=12.4,
        torque_norm_nm=0.05,
        sensor_fresh=True,
        wrench=(1.0, 2.0, -12.5, 0.0, 0.0, 0.01),
        command_mode=0,
        packet_sequence=42,
        attempt_ordinal=1,
    )
    assert row["schema"] == STATE20_SEARCH_TRACE_SCHEMA
    assert row["tcp_pose_m_rad"][2] == 0.3
    assert row["normal_load_n"] == 12.5
    path = append_state20_sidecar(tmp_path, row)
    assert path.name == STATE20_SEARCH_TRACE_SIDECAR_NAME
    loaded = json.loads(path.read_text(encoding="utf-8").strip())
    assert loaded["force_norm_n"] == 13.0
    assert loaded["sensor_fresh"] is True


def test_trace_observe_ring_and_stop_dump(tmp_path: Path) -> None:
    trace = State20SearchTrace(tmp_path, ring_size=3)
    for index in range(5):
        trace.observe(
            build_state20_row(
                monotonic_s=float(index),
                tcp_pose_m_rad=(0.0, 0.0, 0.01 * index, 0.0, 0.0, 0.0),
                normal_load_n=float(index),
                force_norm_n=float(index) + 0.1,
            )
        )
    assert trace.rows_written == 5
    assert len(trace.recent_rows()) == 3
    assert trace.recent_rows()[0]["normal_load_n"] == 2.0
    context = trace.stop_dominant_context(
        reason_code=61,
        reason="hard_abs_normal_60n",
        wrench=(0.0, 0.0, -61.0, 0.0, 0.0, 0.0),
        tp_state=20,
        sensor_fresh=True,
        command_mode=0,
        normal_load_n=61.0,
        force_norm_n=61.0,
    )
    dump = trace.dump_stop_dominant(context)
    assert dump is not None
    assert dump.name == STATE20_STOP_DOMINANT_NAME
    payload = json.loads(dump.read_text(encoding="utf-8"))
    assert payload["reason_code"] == 61
    assert payload["tp_state"] == 20
    assert len(payload["last_state20_samples"]) == 3
    lines = (tmp_path / STATE20_SEARCH_TRACE_SIDECAR_NAME).read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 5


def test_memory_only_trace_does_not_touch_disk(tmp_path: Path) -> None:
    trace = State20SearchTrace(run_dir=None)
    trace.observe(
        build_state20_row(
            monotonic_s=1.0,
            tcp_pose_m_rad=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            normal_load_n=1.0,
            force_norm_n=1.0,
        )
    )
    assert trace.rows_written == 0
    assert trace.dump_stop_dominant({"schema": "x"}) is None
    assert list(tmp_path.iterdir()) == []


def test_attach_state20_trace_binds_run_dir(tmp_path: Path) -> None:
    owner = type("Owner", (), {})()
    trace = attach_state20_trace(owner, tmp_path)
    assert owner._state20_trace is trace
    assert trace.run_dir == tmp_path


def test_format_stop_dominant_keeps_legacy_substring() -> None:
    context = build_stop_dominant_context(
        reason_code=3,
        reason="sensor_stale",
        wrench=None,
        tp_state=20,
        sensor_fresh=False,
        command_mode=0,
    )
    message = format_stop_dominant_error(
        prefix="r006 stop-dominant packet: ",
        reason_code=3,
        reason="sensor_stale",
        context=context,
    )
    assert "reason_code=3 reason=sensor_stale" in message
    assert "tp_state" in message


def test_r006_stop_dominant_includes_diag_and_optional_dump(tmp_path: Path, monkeypatch) -> None:
    writer = object.__new__(R006LiveWriter)
    writer._stopped = False
    writer._last_output = type("Out", (), {"integer_echoes": {26: 20}})()
    attach_state20_trace(writer, tmp_path)
    writer._state20_trace.observe(
        build_state20_row(
            monotonic_s=1.0,
            tcp_pose_m_rad=(0.1, 0.0, 0.2, 0.0, 0.0, 0.0),
            normal_load_n=55.0,
            force_norm_n=55.0,
            sensor_fresh=True,
        )
    )
    packet = type(
        "Packet",
        (),
        {"stop_dominant": True, "reason_code": 61, "reason": "hard_abs_normal_60n"},
    )()
    monkeypatch.setattr(
        r006_live.LiveR004Writer,
        "_send_packet",
        lambda self, *args, **kwargs: packet,
    )
    sensor = type(
        "Sensor",
        (),
        {
            "wrench": (0.0, 0.0, -61.0, 0.0, 0.0, 0.0),
            "sensor_fresh": True,
            "normal_load_n": 61.0,
            "force_norm_n": 61.0,
            "torque_norm_nm": 0.1,
            "filtered_normal_n": 61.0,
        },
    )()
    with pytest.raises(writer_cli.LiveWriterError, match="reason_code=61 reason=hard_abs_normal_60n") as caught:
        writer._send_packet(sensor, command_mode=0)
    message = str(caught.value)
    assert "stop_diag=" in message
    assert "wrench" in message
    assert (tmp_path / STATE20_STOP_DOMINANT_NAME).is_file()


def test_merge_state20_into_exception_detail() -> None:
    trace = State20SearchTrace(run_dir=None)
    trace.observe(
        build_state20_row(
            monotonic_s=2.0,
            tcp_pose_m_rad=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            normal_load_n=9.0,
            force_norm_n=9.0,
        )
    )
    detail = merge_state20_into_exception_detail("boom; baseline_diag={}", trace=trace)
    assert detail.startswith("boom;")
    assert "state20_diag=" in detail
    assert "normal_load_n" in detail


def test_r006_send_packet_records_state20_without_frozen_writer_edits() -> None:
    """State-20 observe lives on R006LiveWriter so r004 source closure stays pinned.

    Claude plan authorized frozen-path field adds; Wave-1 B3 live always uses
    ``R006LiveWriter``, so the equivalent observe hook is placed on the r006
    override.  That keeps sealed ``r004_live_writer.py`` digest unchanged while
    still producing the travel-vs-force sidecar.
    """

    source = inspect.getsource(R006LiveWriter._send_packet)
    assert "tp_state == 20" in source
    assert "build_state20_row" in source
    assert "trace.observe" in source
    assert "V3_R034_SAFETY_ENVELOPE" not in source
    assert "soft_stop" not in source
    frozen = inspect.getsource(writer_cli.LiveR004Writer.execute_attempt)
    assert "build_state20_row" not in frozen
    assert "state20_search_trace" not in inspect.getsource(writer_cli)
    envelope = r004_contracts.V3_R034_SAFETY_ENVELOPE
    assert envelope.max_abs_normal_n == 60.0


def test_r006_state20_observe_writes_sidecar_on_send(tmp_path: Path, monkeypatch) -> None:
    """Non-stop-dominant state-20 send appends one sidecar row; HOLD path unchanged."""

    writer = object.__new__(R006LiveWriter)
    writer._stopped = False
    writer._packet_sequence = 7
    writer._ordinal = 1
    writer.prerequisites = type("P", (), {"session_epoch": 3})()
    writer._mono_clock = lambda: 12.5
    writer._last_output = type(
        "Out",
        (),
        {
            "integer_echoes": {26: 20},
            "tcp_pose_m_rad": (0.1, 0.2, 0.3, 0.0, 0.0, 0.0),
            "timestamp": 99.0,
            "observed_at_s": 1_700_000_000.0,
        },
    )()
    attach_state20_trace(writer, tmp_path)
    packet = type(
        "Packet",
        (),
        {"stop_dominant": False, "reason_code": 0, "reason": ""},
    )()
    monkeypatch.setattr(
        r006_live.LiveR004Writer,
        "_send_packet",
        lambda self, *args, **kwargs: packet,
    )
    sensor = type(
        "Sensor",
        (),
        {
            "wrench": (0.0, 0.0, -5.0, 0.0, 0.0, 0.0),
            "sensor_fresh": True,
            "normal_load_n": 5.0,
            "force_norm_n": 5.1,
            "torque_norm_nm": 0.02,
            "filtered_normal_n": 5.0,
        },
    )()
    out = writer._send_packet(sensor, command_mode=0)
    assert out is packet
    lines = (tmp_path / STATE20_SEARCH_TRACE_SIDECAR_NAME).read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["tp_state"] == 20
    assert row["normal_load_n"] == 5.0
    assert row["tcp_pose_m_rad"][2] == 0.3

    # Non-state-20: no additional row.
    writer._last_output.integer_echoes[26] = 21
    writer._send_packet(sensor, command_mode=0)
    lines2 = (tmp_path / STATE20_SEARCH_TRACE_SIDECAR_NAME).read_text(encoding="utf-8").strip().splitlines()
    assert len(lines2) == 1


def test_dump_stop_dominant_json_standalone(tmp_path: Path) -> None:
    path = dump_stop_dominant_json(
        tmp_path,
        {"schema": "x", "reason_code": 61, "wrench": [0, 0, -60, 0, 0, 0]},
    )
    assert path.name == STATE20_STOP_DOMINANT_NAME
    assert json.loads(path.read_text(encoding="utf-8"))["reason_code"] == 61


def test_r008_live_adapter_imports_attach_helper() -> None:
    import step5d_autotune_v4_r008.live_adapter as r008_live

    source = inspect.getsource(r008_live.R008LiveAdapter.run_forever)
    assert "attach_state20_trace" in source
    assert "run_dir" in source
