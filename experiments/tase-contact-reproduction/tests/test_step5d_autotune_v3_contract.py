#!/usr/bin/env python3
"""Offline production-config contract tests for Step5d autotune v3."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SRC = ROOT.parents[1] / "src" / "ur10e_experiment_runtime"
sys.path.insert(0, str(RUNTIME_SRC))
if os.environ.get("STEP5D_V3_HERMETIC_PARSER_CI") == "1":
    sys.path.insert(0, str(ROOT / "tests"))
    from step5d_v3_parser_ci_stubs import install as install_parser_ci_stubs

    install_parser_ci_stubs()
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v3.launcher import (  # noqa: E402
    build_bridge_argv,
    check_effective_config,
    main,
    validate_effective_config,
)
from step5d_autotune_v3.profile import (  # noqa: E402
    CATEGORIES,
    ContractViolation,
    control_fingerprint,
    load_contract,
    normalize_candidate,
)


CONTRACT = load_contract()
CLI_ROWS = tuple(tuple(row) for row in CONTRACT["cli_arguments"])


def test_real_parser_is_bound_to_sha_protected_bridge_source() -> None:
    import kunwei_rtde_bridge as bridge

    protected = (ROOT / "tools/kunwei_rtde_bridge.py").resolve()
    assert Path(bridge.parse_args.__code__.co_filename).resolve() == protected
    assert hashlib.sha256(protected.read_bytes()).hexdigest() == (
        "e0b3f52e41710ae92b8756ae08f2374e5d23bb3f7d114e1e64535d975ab4681f"
    )


def test_physical_prior_binding_and_search_stop_use_production_bridge_path() -> None:
    import kunwei_rtde_bridge as bridge
    from ur10e_experiment_runtime.physical_prior import STEP5D_V3_PHYSICAL_PRIOR

    prior = STEP5D_V3_PHYSICAL_PRIOR
    args = SimpleNamespace(
        bridge_profile=bridge.STEP5D_AUTOTUNE_STAGE_ID,
        step5d_autotune_handshake={"trial_id": 1},
        step5d_physical_prior_reaction_normal_b=prior.reaction_normal_b,
        step5d_physical_prior_approach_axis_b=prior.approach_axis_b,
        step5d_physical_prior_precontact_rotvec_rad=prior.precontact_rotvec_rad,
        step5d_physical_prior_identity_payload=prior.identity_payload(),
        step5d_physical_prior_sha256=prior.fingerprint,
        step5d_physical_prior_binding_valid=True,
    )
    state = bridge.BridgeState()
    state.step5d_v30_deferred_diagnostics = SimpleNamespace(
        reset_for_trial=lambda: None
    )
    assert bridge.reset_step5d_autotune_diagnostics_for_trial(state, args) is True
    assert state.step5d_physical_prior_approach_axis_b == prior.approach_axis_b
    assert state.step5d_physical_prior_sha256 == prior.fingerprint

    values = {name: 1.0 for name in bridge.BRIDGE_INPUT_NAMES[:6]}
    values["step4e_cmd_valid"] = 1.0
    values["stop_request"] = 0.0
    bridge.apply_step5d_search_pose_fail_stop(values)
    assert [values[name] for name in bridge.BRIDGE_INPUT_NAMES[:6]] == [0.0] * 6
    assert values["step4e_cmd_valid"] == 0.0
    assert values["stop_request"] == 1.0
    assert values["_step5d_contact_safety_reason"] == (
        "physical_prior_search_pose_mismatch"
    )

    args.step5d_physical_prior_approach_axis_b = (0.0, 0.0, -1.0)
    args.step5d_autotune_handshake = {"trial_id": 2}
    with pytest.raises(RuntimeError, match="fingerprint binding differs"):
        bridge.reset_step5d_autotune_diagnostics_for_trial(state, args)


def _remove_flag(argv: list[str], flag: str) -> list[str]:
    result = list(argv)
    index = result.index(flag)
    row = next(row for row in CLI_ROWS if row[0] == flag)
    del result[index : index + len(row)]
    return result


def _duplicate_flag(argv: list[str], flag: str) -> list[str]:
    result = list(argv)
    index = result.index(flag)
    row = next(row for row in CLI_ROWS if row[0] == flag)
    duplicate = result[index : index + len(row)]
    result[index:index] = duplicate
    return result


def _replace_flag_value(argv: list[str], flag: str, value: str) -> list[str]:
    result = list(argv)
    index = result.index(flag)
    result[index + 1] = value
    return result


def test_real_parser_round_trip_classifies_every_effective_field() -> None:
    report = check_effective_config(environ={})
    assert report["ok"] is True
    assert report["no_motion"] is True
    assert report["frozen_baseline"]["commit"] == (
        "6f9ef0912842ac003545eb1906b38d13c7552218"
    )
    assert report["deployment_tp_identity"] == {
        "program": "step5d_strict_rnn_autotune_v3",
        "mode": "explicit_v3_identity_precontact_pose_frozen_v1_control",
        "artifact_dir": "programs/step5/step5d",
        "readback_manifest": "config/step5d_autotune_controller_readback_v3.json",
        "readback_manifest_sha256": (
            "149917930a93ddb2f6bf8ddb438e24aa4bc3db59d3f0d7f22daab5c13d4a2ca0"
        ),
        "tp_fingerprint": (
            "1c42b3f73e5c8ac27f7739117bff2f1af1e127ceb5e94d38aecc63fcbf5893a0"
        ),
    }
    assert report["execution_profile_id"] == "nf050-slew050-a050"
    categories = report["field_categories"]
    classified = [name for category in CATEGORIES for name in categories[category]]
    assert len(classified) == len(set(classified)) == 126
    assert set(classified) == set(report["effective_config"])
    effective = report["effective_config"]
    assert effective["target_force_n"] == 12.0
    assert effective["bridge_total_linear_limit_m_s"] == 1.0
    assert effective["bridge_normal_velocity_limit_m_s"] == 1.0
    assert effective["bridge_normal_filter_alpha"] == 0.0
    assert effective["step5d_epsilon"] == 0.01
    assert effective["step5d_sigr_exponent_r"] == 0.8
    assert effective["step5d_rnn_inner_iterations"] == 512
    assert effective["bridge_angular_limit_rad_s"] == 0.05


def test_candidate_schema_is_exact_and_accepts_decimal_json_numbers() -> None:
    candidate = normalize_candidate(
        {
            "force_p_gain": Decimal("0.001"),
            "force_i_gain": Decimal("0.00001"),
            "force_damping": Decimal("7"),
        }
    )
    assert candidate == {
        "force_p_gain": 0.001,
        "force_i_gain": 0.00001,
        "force_damping": 7.0,
    }


@pytest.mark.parametrize(
    "candidate",
    [
        {"force_p_gain": 0.001, "force_i_gain": 0.00001},
        {
            "force_p_gain": 0.001,
            "force_i_gain": 0.00001,
            "force_damping": 7.0,
            "target_force_n": 12.0,
        },
        {"force_p_gain": True, "force_i_gain": 0.00001, "force_damping": 7.0},
        {"force_p_gain": 0.0011, "force_i_gain": 0.00001, "force_damping": 7.0},
        {"force_p_gain": 0.001, "force_i_gain": float("nan"), "force_damping": 7.0},
    ],
)
def test_candidate_schema_rejects_missing_unknown_nonfinite_and_off_lattice(
    candidate: dict[str, object],
) -> None:
    with pytest.raises(ContractViolation):
        normalize_candidate(candidate)


@pytest.mark.parametrize("flag", [row[0] for row in CLI_ROWS])
def test_every_production_flag_rejects_missing_and_duplicate_forms(flag: str) -> None:
    argv = build_bridge_argv(Path("/tmp/step5d-v3-contract-matrix"))
    with pytest.raises(ContractViolation, match="missing|required"):
        check_effective_config(environ={}, argv=_remove_flag(argv, flag))
    with pytest.raises(ContractViolation, match="repeats"):
        check_effective_config(environ={}, argv=_duplicate_flag(argv, flag))


@pytest.mark.parametrize(
    ("flag", "known_bad_v2"),
    [
        ("--bridge-angular-limit-rad-s", "0.015"),
        ("--step5d-epsilon", "0.022"),
        ("--step5d-sigr-exponent-r", "1"),
        ("--step5d-rnn-inner-iterations", "1"),
    ],
)
def test_known_bad_v2_four_field_fixture_fails_exactly(
    flag: str,
    known_bad_v2: str,
) -> None:
    argv = build_bridge_argv(Path("/tmp/step5d-v3-known-bad-v2"))
    with pytest.raises(ContractViolation, match=flag):
        check_effective_config(
            environ={},
            runtime_root=Path("/tmp/step5d-v3-known-bad-v2"),
            argv=_replace_flag_value(argv, flag, known_bad_v2),
        )


def test_unknown_and_forbidden_flags_fail_before_parser() -> None:
    runtime = Path("/tmp/step5d-v3-unknown-flag")
    argv = build_bridge_argv(runtime)
    with pytest.raises(ContractViolation, match="unknown or unclassified"):
        check_effective_config(
            environ={}, runtime_root=runtime, argv=[*argv, "--future-control-knob", "1"]
        )
    with pytest.raises(ContractViolation, match="forbidden flag"):
        check_effective_config(
            environ={},
            runtime_root=runtime,
            argv=[*argv, "--bridge-normal-filter-alpha", "0.35"],
        )


@pytest.mark.parametrize("name", CONTRACT["forbidden_environment"])
def test_every_governed_environment_override_fails_closed(name: str) -> None:
    with pytest.raises(ContractViolation, match="environment attempts governed overrides"):
        check_effective_config(environ={name: "override"})


def test_new_parser_field_is_unclassified_and_fails_closed() -> None:
    expected = check_effective_config(environ={})["effective_config"]
    observed = {**expected, "future_control_knob": 1}
    with pytest.raises(ContractViolation, match="unclassified=.*future_control_knob"):
        validate_effective_config(expected, observed)


def test_parser_default_drift_is_detected_even_when_raw_argv_is_unchanged() -> None:
    expected = check_effective_config(environ={})["effective_config"]
    observed = {**expected, "connect_timeout_s": 4.0}
    with pytest.raises(ContractViolation, match="connect_timeout_s"):
        validate_effective_config(expected, observed)


def test_candidate_changes_do_not_change_deployment_control_fingerprint() -> None:
    baseline = check_effective_config(environ={})
    candidate = check_effective_config(
        {
            "force_p_gain": 0.001,
            "force_i_gain": 0.0001,
            "force_damping": 7.0,
        },
        environ={},
    )
    assert candidate["effective_config"]["step5d_autotune_force_i"] == 0.0001
    assert candidate["control_fingerprint"] == baseline["control_fingerprint"]
    assert baseline["control_fingerprint"] == control_fingerprint()


def test_check_cli_emits_json_without_starting_any_process(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--check", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["no_motion"] is True


def test_launcher_source_has_no_process_or_network_execution_surface() -> None:
    source = (ROOT / "tools/step5d_autotune_v3/launcher.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_roots = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_roots.update(
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert imported_roots.isdisjoint({"subprocess", "socket"})
    forbidden_calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert forbidden_calls.isdisjoint({"execv", "execve", "execvp", "execvpe", "Popen"})
