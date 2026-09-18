from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

from build_contact_laws import SOURCE_NAMES, build_shared_library  # noqa: E402
from contact_laws import (  # noqa: E402
    ContactLaw,
    ContactLawError,
    DEFAULT_CONFIG_PATH,
    PUBLIC_LAWS,
)


SOURCE_DIR = Path(
    "/home/andy/.codex-worktrees/smysfc-tro-ubuntu-followup-20260906/cpp/controller_suite"
)
OLD_PYTHON_SRC = Path(
    "/home/andy/.codex-worktrees/smysfc-tro-ubuntu-followup-20260906/src"
)


def _close(lhs, rhs, *, rel: float = 2e-9, abs_: float = 2e-12) -> None:
    assert lhs == pytest.approx(rhs, rel=rel, abs=abs_)


def _step_componentwise(previous, force, *, m, mu, g, dt, damping):
    acceleration = tuple((f - damping(p)) / m for p, f in zip(previous, force))
    state = tuple(p + dt * a for p, a in zip(previous, acceleration))
    return acceleration, state, tuple(g * value for value in state)


def test_config_exposes_six_labels_and_preserves_msfc_provenance() -> None:
    root = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    assert tuple(root["public_laws"]) == PUBLIC_LAWS
    assert tuple(root["laws"]) == PUBLIC_LAWS
    assert "RPSFC" not in root["laws"]
    assert root["hardware_qualified"] is False
    assert root["endpoint"] == "none"
    assert root["laws"]["MSFC"]["parameter_status"].startswith("latest_")
    latest = root["laws"]["MSFC"]["parameters"]
    frozen = root["provenance"]["frozen_baseline"]["msfc_parameters"]
    assert latest["minimum_metric_eigenvalue"] != frozen["minimum_metric_eigenvalue"]
    assert latest["tau_force_s"] != frozen["tau_force_s"]
    assert root["metric_definition"]["formula"] == "B = beta I + (1 - beta) exp(S)"
    assert root["metric_definition"]["beta_parameter"] == "minimum_metric_eigenvalue"


def test_builder_copies_exact_sources_and_writes_offline_manifest(tmp_path: Path) -> None:
    result = build_shared_library(source_dir=SOURCE_DIR, build_root=tmp_path / "build")
    assert result.library_path.is_file()
    manifest = json.loads(result.provenance_path.read_text(encoding="utf-8"))
    assert manifest["offline_only"] is True
    assert manifest["endpoint"] == "none"
    assert manifest["hardware_qualified"] is False
    assert manifest["library"]["sha256"] == hashlib.sha256(result.library_path.read_bytes()).hexdigest()
    assert [record["filename"] for record in manifest["source_files"]] == list(SOURCE_NAMES)
    for record in manifest["source_files"]:
        copied = Path(record["copied_path"])
        assert copied.is_file()
        digest = hashlib.sha256(copied.read_bytes()).hexdigest()
        assert digest == record["sha256"]
        assert digest == hashlib.sha256((SOURCE_DIR / record["filename"]).read_bytes()).hexdigest()


def test_from_config_rejects_a_drifted_native_source(tmp_path: Path) -> None:
    copied_source = tmp_path / "controller_suite"
    copied_source.mkdir()
    for name in SOURCE_NAMES:
        (copied_source / name).write_bytes((SOURCE_DIR / name).read_bytes())
    altered = copied_source / SOURCE_NAMES[0]
    altered.write_bytes(altered.read_bytes() + b"\n")
    with pytest.raises(ContactLawError, match="native source hash mismatch"):
        ContactLaw.from_config(
            "LAC",
            source_dir=copied_source,
            build_root=tmp_path / "build",
        )


def test_lac_is_componentwise_and_command_uses_state_gain(tmp_path: Path) -> None:
    controller = ContactLaw(
        "LAC",
        {"m": 2.0, "mu": 4.0, "g": 0.5},
        dimension=3,
        dt_s=0.1,
        build_root=tmp_path / "build",
    )
    first = controller.step((2.0, -1.0, 0.5))
    _close(first.acceleration, (1.0, -0.5, 0.25))
    _close(first.state, (0.1, -0.05, 0.025))
    _close(first.command, (0.05, -0.025, 0.0125))
    second = controller.step((0.0, 0.0, 0.0))
    _close(second.previous_state, first.state)
    _close(second.acceleration, (-0.2, 0.1, -0.05))
    _close(second.state, (0.08, -0.04, 0.02))
    _close(second.command, (0.04, -0.02, 0.01))
    controller.close()


@pytest.mark.parametrize("law", ("NAC", "SFC", "ISFC"))
def test_componentwise_extended_laws_match_their_native_equations(
    law: str, tmp_path: Path
) -> None:
    if law == "NAC":
        parameters = {"m": 1.5, "mu": 1.0, "g": 0.7, "alpha": 2.0, "sigma": 1.0}
        force = (1.0, 2.0, -0.5)
        previous = (0.0, 0.0, 0.0)
        dt = 0.2

        def damping(value, index):
            gain = parameters["mu"] + parameters["alpha"] * (
                1.0 - __import__("math").exp(-(force[index] ** 2) / parameters["sigma"] ** 2)
            )
            return gain * value

        expected_acceleration = tuple(
            (force[index] - damping(previous[index], index)) / parameters["m"]
            for index in range(3)
        )
    elif law == "SFC":
        parameters = {"m": 1.5, "mu": 2.0, "n": 3.0, "g": 0.7}
        force = (1.0, 2.0, -0.5)
        previous = (0.0, 0.0, 0.0)
        dt = 0.2
        expected_acceleration = tuple(f / parameters["m"] for f in force)
    else:
        parameters = {
            "m": 1.5,
            "mu": 2.0,
            "n": 3.0,
            "eta0": 5.0,
            "g": 0.7,
            "v_ref": 1.0,
            "substeps": 2,
        }
        force = (1.0, 2.0, -0.5)
        previous = (0.0, 0.0, 0.0)
        dt = 0.2
        sub_dt = dt / parameters["substeps"]
        expected_acceleration = (0.0, 0.0, 0.0)
        expected_state = previous
        for _ in range(parameters["substeps"]):
            expected_acceleration, expected_state, _ = _step_componentwise(
                expected_state,
                force,
                m=parameters["m"],
                mu=parameters["mu"],
                g=parameters["g"],
                dt=sub_dt,
                damping=lambda value: parameters["mu"] * abs(value) ** (parameters["n"] - 1.0) * value
                + parameters["eta0"] * __import__("math").exp(-abs(value) / parameters["v_ref"]) * value,
            )
        controller = ContactLaw(law, parameters, dt_s=dt, build_root=tmp_path / "build")
        result = controller.step(force)
        _close(result.acceleration, expected_acceleration)
        _close(result.state, expected_state)
        _close(result.command, tuple(parameters["g"] * value for value in expected_state))
        return

    controller = ContactLaw(law, parameters, dt_s=dt, build_root=tmp_path / "build")
    first = controller.step(force)
    expected_state = tuple(previous[index] + dt * expected_acceleration[index] for index in range(3))
    _close(first.acceleration, expected_acceleration)
    _close(first.state, expected_state)
    _close(first.command, tuple(parameters["g"] * value for value in expected_state))

    second_force = (-0.25, 0.5, 1.25)
    if law == "NAC":
        damping_fn = lambda value, index: (
            parameters["mu"]
            + parameters["alpha"] * (1.0 - __import__("math").exp(-(second_force[index] ** 2) / parameters["sigma"] ** 2))
        ) * value
    else:
        damping_fn = lambda value, index: parameters["mu"] * abs(value) ** (parameters["n"] - 1.0) * value
    expected_acceleration, expected_state, expected_command = _step_componentwise(
        first.state,
        second_force,
        m=parameters["m"],
        mu=parameters["mu"],
        g=parameters["g"],
        dt=dt,
        damping=lambda value: damping_fn(value, 0),
    )
    # Compute each NAC coordinate with its own force-dependent gain.
    if law == "NAC":
        expected_acceleration = tuple(
            (second_force[index] - damping_fn(first.state[index], index)) / parameters["m"]
            for index in range(3)
        )
        expected_state = tuple(first.state[index] + dt * expected_acceleration[index] for index in range(3))
        expected_command = tuple(parameters["g"] * value for value in expected_state)
    second = controller.step(second_force)
    _close(second.acceleration, expected_acceleration)
    _close(second.state, expected_state)
    _close(second.command, expected_command)


def test_dsfc_matches_existing_python_radial_resolvent(tmp_path: Path) -> None:
    sys.path.insert(0, str(OLD_PYTHON_SRC))
    from m11_ysfc.ysfc import YsfcControllerV1, YsfcParametersV1

    parameters = {
        "m": 4.0,
        "g": 0.214,
        "p": 0.1,
        "a": 1.2,
        "n": 3.0,
        "mu": 310.66177089084647,
        "max_iterations": 32,
        "residual_tolerance_n": 1e-9,
        "relative_radius_tolerance": 1e-10,
    }
    native = ContactLaw("DSFC", parameters, dt_s=0.002, build_root=tmp_path / "build")
    python = YsfcControllerV1(
        dimension=3,
        params=YsfcParametersV1(**parameters, dt_s=0.002),
    )
    for force in ((5.0, 3.0, 1.0), (1.0, -2.0, 4.0), (-0.5, 0.25, 2.0)):
        expected = python.step(force)
        actual = native.step(force)
        _close(actual.state, expected.state, rel=2e-8, abs_=2e-11)
        _close(actual.command, expected.command, rel=2e-8, abs_=2e-11)
        _close(actual.acceleration, expected.acceleration, rel=2e-8, abs_=2e-8)


def test_msfc_matches_existing_python_memory_resolvent_and_metric(tmp_path: Path) -> None:
    sys.path.insert(0, str(OLD_PYTHON_SRC))
    from m14_smysfc.smysfc import SmysfcControllerV1, SmysfcParametersV1

    parameters = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))["laws"]["MSFC"]["parameters"]
    native = ContactLaw("MSFC", parameters, dt_s=0.002, build_root=tmp_path / "build")
    python = SmysfcControllerV1(
        dimension=3,
        params=SmysfcParametersV1(**parameters, dt_s=0.002),
    )
    for force in ((5.0, 3.0, 1.0), (1.0, -2.0, 4.0)):
        expected = python.step(force)
        actual = native.step(force)
        _close(actual.state, expected.state, rel=2e-7, abs_=2e-11)
        _close(actual.command, expected.command, rel=2e-7, abs_=2e-11)
        _close(actual.acceleration, expected.acceleration, rel=2e-7, abs_=2e-8)
        snapshot = native.snapshot()
        _close(snapshot.force_history, expected.force_history, rel=2e-7, abs_=2e-12)
        _close(snapshot.metric_eigenvalues, expected.metric_eigenvalues, rel=2e-7, abs_=2e-12)
        for actual_row, expected_row in zip(snapshot.structure, expected.structure):
            _close(actual_row, expected_row, rel=2e-7, abs_=2e-12)


def test_snapshot_restore_replays_msfc_memory_and_rejects_cross_binding(tmp_path: Path) -> None:
    parameters = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))["laws"]["MSFC"]["parameters"]
    first = ContactLaw("MSFC", parameters, dt_s=0.002, build_root=tmp_path / "build")
    peer = ContactLaw("MSFC", parameters, dt_s=0.002, build_root=tmp_path / "build")
    first.step((5.0, 3.0, 1.0))
    token = first.snapshot()
    peer.restore(token)
    expected = first.step((1.0, -2.0, 4.0))
    first.restore(token)
    replay = first.step((1.0, -2.0, 4.0))
    _close(replay.state, expected.state, rel=0.0, abs_=0.0)
    _close(replay.command, expected.command, rel=0.0, abs_=0.0)
    _close(replay.acceleration, expected.acceleration, rel=0.0, abs_=0.0)
    peer_replay = peer.step((1.0, -2.0, 4.0))
    _close(replay.state, peer_replay.state, rel=0.0, abs_=0.0)
    _close(first.snapshot().values, peer.snapshot().values, rel=0.0, abs_=0.0)

    different_parameters = dict(parameters)
    different_parameters["minimum_metric_eigenvalue"] = 0.5
    different = ContactLaw("MSFC", different_parameters, dt_s=0.002, build_root=tmp_path / "build")
    with pytest.raises(ContactLawError, match="binding"):
        different.restore(token)


def test_fixed_dt_and_failed_step_leave_state_and_memory_unchanged(tmp_path: Path) -> None:
    parameters = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))["laws"]["MSFC"]["parameters"]
    controller = ContactLaw("MSFC", parameters, dt_s=0.002, build_root=tmp_path / "build")
    controller.step((5.0, 3.0, 1.0))
    before = controller.snapshot()
    with pytest.raises(ContactLawError, match="fixed dt_s"):
        controller.step((0.0, 0.0, 0.0), dt_s=0.004)
    assert controller.snapshot().values == before.values
    with pytest.raises(ContactLawError, match="finite"):
        controller.step((float("nan"), 0.0, 0.0))
    assert controller.snapshot().values == before.values


def test_dimension_is_active_and_nonfinite_unknown_laws_are_rejected(tmp_path: Path) -> None:
    controller = ContactLaw(
        "LAC",
        (2.0, 4.0, 0.5),
        dimension=2,
        dt_s=0.1,
        build_root=tmp_path / "build",
    )
    result = controller.step((2.0, -1.0))
    assert len(result.state) == 2
    assert result.state == pytest.approx((0.1, -0.05))
    assert controller.snapshot().values[6] == 0.0
    with pytest.raises(ContactLawError, match="RPSFC"):
        ContactLaw("RPSFC", (1.0,), build_root=tmp_path / "build")
    with pytest.raises(ContactLawError, match="finite"):
        ContactLaw("LAC", (2.0, float("nan"), 0.5), build_root=tmp_path / "build")


def test_elapsed_dt_integrates_actual_interval_without_changing_nominal_identity():
    with ContactLaw('LAC',{'m':4.,'mu':17.,'g':.17}) as law:
        state=(0.,0.,0.)
        for dt in (.001,.003,.0025,.004):
            force=(2.,-1.,.5)
            acceleration,state,command=_step_componentwise(state,force,m=4.,mu=17.,g=.17,dt=dt,damping=lambda v:17.*v)
            result=law.step_elapsed(force,dt_s=dt)
            _close(result.state,state);_close(result.command,command)
            assert result.dt_s==dt and law.dt_s==.002
        before=law.snapshot()
        for dt in (0.,-.001,.004001,float('nan')):
            with pytest.raises(ContactLawError):law.step_elapsed((1,0,0),dt_s=dt)
            assert law.snapshot()==before
