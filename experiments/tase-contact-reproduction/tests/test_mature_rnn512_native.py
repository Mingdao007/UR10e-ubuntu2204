"""Numerical identity and whole-call rollback for the I-1 native candidate."""

from dataclasses import replace
import json
import os
from pathlib import Path

import numpy as np
import pytest

from step5c_mature_rnn512_native import NativeMatureRnn512
from step5c_strict_rnn import StrictRnnConfig, StrictTaseRnnSolver
from step5d_autotune_v4_r014.solver_profile import MatureRnn512NativeProfile


@pytest.fixture
def library() -> Path:
    configured = os.environ.get("TASE_RNN512_TEST_LIB")
    if not configured:
        pytest.skip("set TASE_RNN512_TEST_LIB to the separately built candidate")
    return Path(configured).resolve(strict=True)


def test_512_full_dt_state_and_output_match_mature_numpy(library: Path) -> None:
    numpy_solver = StrictTaseRnnSolver(replace(
        StrictRnnConfig(),
        paper_truth_path=Path(__file__).resolve().parents[1] / "config/step5d_liveprep_solver_gate.json",
        inner_iterations=512,
    ))
    native = NativeMatureRnn512(library)
    rng = np.random.default_rng(928512)
    try:
        for index in range(12):
            jacobian = np.eye(6) + rng.normal(0, .05, size=(6, 6))
            twist = rng.normal(0, .003, size=6)
            lower = np.full(6, -.05)
            upper = np.full(6, .05)
            inputs = {"J": jacobian, "xdot_c": twist,
                      "omega_minus": lower, "omega_plus": upper}
            if index in (0, 5, 9):
                numpy_solver.warm_start(**inputs)
                native.warm_start(**inputs)
            target = dict(inputs, dt=.0015 + index * .00005,
                          epsilon=.022, r=1.0, cmd_valid=True)
            expected = numpy_solver.solve(actual_q=np.zeros(6), actual_qd=np.zeros(6),
                                          target_state=target)
            actual = native.solve(actual_q=np.zeros(6), actual_qd=np.zeros(6),
                                  target_state=target)
            assert actual.diagnostics["executed_inner_iterations"] == 512
            assert np.allclose(actual.qdot, expected.qdot, rtol=0, atol=1e-10)
            assert abs(actual.residual_norm - expected.residual_norm) <= 1e-10
            for key in ("theta_dot_state", "lambda_state"):
                assert np.allclose(native.snapshot()[key], numpy_solver.snapshot()[key],
                                   rtol=0, atol=1e-10)
    finally:
        native.close()


def test_rejected_call_retains_both_state_vectors(library: Path) -> None:
    native = NativeMatureRnn512(library)
    try:
        state = {"theta_dot_state": [.001] * 6, "lambda_state": [.02] * 6}
        native.restore(state)
        target = {"J": np.eye(6), "xdot_c": np.zeros(6),
                  "omega_minus": np.full(6, .01),
                  "omega_plus": np.full(6, -.01),
                  "dt": .002, "epsilon": .022, "r": 1.0, "cmd_valid": True}
        with pytest.raises(RuntimeError, match="rejected"):
            native.solve(actual_q=np.zeros(6), actual_qd=np.zeros(6),
                         target_state=target)
        assert native.snapshot() == state
        target["omega_minus"] = np.full(6, -.05)
        target["omega_plus"] = np.full(6, .05)
        target["dt"] = .08
        with pytest.raises(RuntimeError, match="rejected"):
            native.solve(actual_q=np.zeros(6), actual_qd=np.zeros(6),
                         target_state=target)
        assert native.snapshot() == state
    finally:
        native.close()


def test_candidate_profile_through_calibrated_runtime_and_rollback(library: Path) -> None:
    from contact_yield_live_contract import load_identity_contract
    from contact_yield_live_writer import native_motion_profile
    from step5d_autotune_v4.contracts import V4Candidate
    from tase_contact_provider import TaseContactProvider, current_model_binding

    profile = MatureRnn512NativeProfile(library)
    assert profile.as_dict()["library_sha256"]
    assert profile.as_dict()["iterations"] == 512
    provider = TaseContactProvider(
        contract=current_model_binding(), candidate=V4Candidate(),
        motion_profile=native_motion_profile(),
        home_pose=load_identity_contract().home_pose,
        solver_profile=profile,
    )
    try:
        assert provider._uses_recurrent_solver_state is True
        runtime = provider.runtime
        initial = runtime.dynamic_state_snapshot()
        q = (.7452071915341404, -1.8180657673475784,
             -2.5627204525097316, -.3123084932568952,
             1.527618967738308, 5.459402166676702)
        kwargs = dict(
            actual_q=q, actual_qd=(0.0,) * 6,
            actual_tcp_pose=load_identity_contract().home_pose,
            desired_twist=(0.0, 0.0, -.0002, 0.0, 0.0, 0.0),
            actual_dt_s=.002, mode="path", path_time_s=1.0,
        )
        first = runtime.command(**kwargs)
        assert runtime.last_solver_diagnostics["backend"] == "cpp-double"
        runtime.restore_dynamic_state(initial)
        second = runtime.command(**kwargs)
        assert np.allclose(first.qdot, second.qdot, rtol=0, atol=1e-12)
        assert runtime.dynamic_state_snapshot()["solver_profile_sha256"] == profile.sha256
    finally:
        provider.close()


def test_explicit_parameter_binding_selects_512_without_changing_legacy(
    library: Path, tmp_path: Path,
) -> None:
    from contact_yield_live_contract import load_identity_contract
    from contact_yield_live_writer import _select_tase_solver_profile
    from contact_yield_live_writer import native_motion_profile
    from step5d_autotune_v4.contracts import V4Candidate
    from tase_contact_provider import (
        TaseContactProvider, current_model_binding, load_tase_outer_config,
    )

    assert _select_tase_solver_profile(
        family="tase_mature", library="/tmp/unused-qp.so",
    ).profile_id == "legacy-r1"
    payload = json.loads((Path(__file__).resolve().parents[1]
                          / "config/tase_figure8_integral_0p1.json").read_text())
    profile = MatureRnn512NativeProfile(library)
    payload["solver_profile"] = {
        "id": profile.profile_id, "library": str(library),
        "library_sha256": profile.library_sha256,
    }
    parameter_file = tmp_path / "rnn512.json"
    parameter_file.write_text(json.dumps(payload))
    outer, binding = load_tase_outer_config(parameter_file)
    selected = _select_tase_solver_profile(
        family="tase_mature", library="/tmp/unused-qp.so",
        declared_profile=binding["solver_profile"],
    )
    assert selected.as_dict()["iterations"] == 512
    assert selected.as_dict()["library_sha256"] == profile.library_sha256
    provider = TaseContactProvider(
        contract=current_model_binding(), candidate=V4Candidate(),
        motion_profile=native_motion_profile(),
        home_pose=load_identity_contract().home_pose,
        solver_profile=selected, outer_loop_config=outer,
        parameter_binding=binding, protocol_id=binding["protocol_id"],
    )
    try:
        provider.apply_outer_parameters_at_home(parameter_file)
        legacy_file = tmp_path / "legacy.json"
        legacy_file.write_text(json.dumps({
            key: value for key, value in payload.items() if key != "solver_profile"
        }))
        with pytest.raises(ValueError, match="cannot change the frozen solver"):
            provider.apply_outer_parameters_at_home(legacy_file)
    finally:
        provider.close()
    binding["solver_profile"]["library_sha256"] = "0" * 64
    with pytest.raises(Exception, match="binary differs"):
        _select_tase_solver_profile(
            family="tase_mature", library="/tmp/unused-qp.so",
            declared_profile=binding["solver_profile"],
        )
