"""Native provider + qualification.step inside the mature execute_attempt loop."""
import pytest
from contact_yield_protocol import PERIOD_S, law_seed_parameters
from step5d_autotune_v4_r004_live_writer import LiveWriterError
from test_contact_benchmark_runtime import lib
from yield_full_writer_offline import (
    PRODUCTION_RUNTIME_DEADLINE_S,
    exercise_full_writer,
    protocol_parameters,
)


def test_g50_parameters_are_the_frozen_transfer_protocol_not_original_msfc_seed():
    msfc = protocol_parameters("MSFC")
    assert msfc["g"] == 0.08583341909109758
    assert msfc["g"] != law_seed_parameters("MSFC")["g"]
    assert PRODUCTION_RUNTIME_DEADLINE_S == 0.0015


def test_full_writer_binds_native_instance_qdot_phase_and_retains_formal_evidence(tmp_path, monkeypatch, lib):
    run = exercise_full_writer(tmp_path, monkeypatch, qp_library=lib, method="MSFC",clock_origin_s=0.)
    assert run.error is None, run.error
    assert run.deadline_s is None
    assert run.runtime.deadline_s is None
    assert run.provider is run.writer._qualification_control.contact_command_provider
    assert run.provider_id == id(run.provider)
    entry_count=run.phases.count('entry')
    # Floating-point sample grids can land on either side of the 1 s seam.
    # Coverage and phase clocks, rather than a special clock origin, decide.
    assert entry_count in (500,501)
    assert run.phases[:entry_count] == ["entry"] * entry_count
    assert len(run.phases[entry_count:]) in (31415,31416)
    assert all(p=='path' for p in run.phases[entry_count:])
    assert run.published, "native PATH/entry packets were not published"
    for packet in run.published:
        assert packet["provider_id"] == run.provider_id
        assert packet["qdot"] == packet["native_qdot"]
        assert packet["reference_phase"] == packet["phase"]
        assert packet["reference_phase"] in {"entry", "path"}
    consumed = [sample.source_sequences["tp"] for sample in run.samples]
    assert consumed == sorted(consumed)
    assert 0 <= run.samples[0].path_time_s <= .002+1e-9
    assert run.samples[0].observed_at_s >= run.start_t + 1.0
    assert run.samples[-1].path_time_s > 62.82
    assert run.evidence is not None
    assert run.evidence.metrics["full_path_bin_count"] == 629
    assert run.evidence.metrics["entry_in_formal_coverage"] is False
    assert run.evidence.path_duration_s >= PERIOD_S
    assert run.provider.last_result["phase"] == "path"
    assert run.provider.last_result["formal_time_s"] == pytest.approx(62.830, abs=0.002)
    assert run.runtime.phase == "path"
    assert .998-1e-9 <= run.runtime.last_entry_time < 1.0
    assert run.evidence.eligible is False
    assert 63.83 < run.clock.t-run.start_t < 63.85


def assert_fault_stopped(run):
    assert run.writer._failed_closed and run.writer._stopped
    assert run.stop_packets and run.stop_packets[-1]['qdot']==(0.,)*6
    assert run.stop_packets[-1]['sequence']==run.writer._last_writer_sequence
    assert run.before_fault is not None and run.final_snapshot==run.before_fault


def test_nonzero_clock_origin_retains_incomplete_duration_rejection(tmp_path,monkeypatch,lib):
    # Known incomplete execution boundary, NOT a passed full-period trial.
    # Retain its strict rejection while the writer's termination is repaired.
    run=exercise_full_writer(tmp_path,monkeypatch,qp_library=lib,method='MSFC',clock_origin_s=100.)
    assert isinstance(run.error,LiveWriterError)
    assert 'path duration is 62.830000' in str(run.error)
    assert run.evidence is None
    assert run.writer._failed_closed and run.stop_packets[-1]['qdot']==(0.,)*6


def test_full_writer_stale_receive_clock_fails_closed_without_commit(tmp_path, monkeypatch, lib):
    run = exercise_full_writer(
        tmp_path, monkeypatch, qp_library=lib, method="MSFC", stale_after_path=True
    )
    assert run.error is not None
    assert isinstance(run.error, LiveWriterError)
    assert "80ms" in str(run.error)
    assert run.clock.stale_injected
    assert run.runtime.phase == "path"
    assert run.provider.last_result["phase"] == "path"
    assert run.provider.last_result["formal_time_s"] == pytest.approx(0.0, abs=0.002)
    assert run.writer._qualification_control.contact_command_provider is run.provider
    assert_fault_stopped(run)


def test_full_writer_cached_poll_does_not_step_or_publish(tmp_path, monkeypatch, lib):
    run = exercise_full_writer(
        tmp_path, monkeypatch, qp_library=lib, method="MSFC", cache_after_path=True
    )
    assert run.clock.cache_injected
    assert run.error is not None
    assert "cached-poll fixture stop" in str(run.error)
    assert run.phases[-1] == "path"
    last = run.provider.last_result
    assert last["phase"] == "path"
    assert last["formal_time_s"] == pytest.approx(0.0, abs=0.002)
    assert run.published[-1]["qdot"] == tuple(last["qdot_rad_s"])
    assert run.published[-1]["sequence"] < run.writer._last_writer_sequence
    assert_fault_stopped(run)


def test_full_writer_hash_mismatch_rolls_back_native_state(tmp_path, monkeypatch, lib):
    run = exercise_full_writer(
        tmp_path, monkeypatch, qp_library=lib, method="MSFC", hash_mismatch=True
    )
    assert run.error is not None
    assert "hash binding differs" in str(run.error)
    assert run.runtime.phase == "baseline"
    assert run.provider.last_result["phase"] == "baseline"
    assert run.phases == []
    assert run.writer._qualification_control.contact_command_provider is run.provider
    assert_fault_stopped(run)
