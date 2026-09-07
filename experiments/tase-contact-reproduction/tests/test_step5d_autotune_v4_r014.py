from __future__ import annotations

import json
import itertools
from pathlib import Path
import sys

import numpy as np
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r014.catalog import (  # noqa: E402
    CATALOG_SIZE,
    I_MAX,
    build_frozen_catalog,
    catalog_document,
    deterministic_maximin_sobol,
    normalized_log_coordinates,
)
from step5d_autotune_v4_r014.certification import (  # noqa: E402
    AttemptAdmission,
    CertificationEngine,
    CertificationOutcome,
    PHYSICAL_ATTEMPT_CAP,
    anytime_radius,
    gp_training_rows,
)
from step5d_autotune_v4_r014.common import (  # noqa: E402
    R014Error,
    atomic_write_json,
    sha256_file,
    sha256_value,
)
from step5d_autotune_v4_r014.dispatcher import (  # noqa: E402
    Dispatcher,
    WriterLock,
    _validate_closed_backend_state,
    parse_request,
)
from step5d_autotune_v4_r014.discovery import (  # noqa: E402
    DiscreteQLogNEISelector,
    censored_revisit_ids,
    should_censor,
    warm_start_arm_ids,
    warm_start_proposal,
)
from step5d_autotune_v4_r014.profiles import (  # noqa: E402
    FORMAL_PROFILE_NAME,
    POINTER_SCHEMA,
    ProfileRegistry,
)
from step5d_autotune_v4_r014.qualification import (  # noqa: E402
    INCUMBENT,
    QUALIFICATION_SCHEMA,
    promote_small_qualification,
)
from step5d_autotune_v4_r014.metrics import (  # noqa: E402
    METRIC_FINGERPRINT,
    force_bin_metric,
)
from step5d_autotune_v4_r014.solver_profile import (  # noqa: E402
    FINITE_TIME_R08,
    LEGACY_R1,
    SolverProfile,
    strict_rnn_config,
)
import step5d_autotune_v4_r013.live_runtime as r013_live_runtime  # noqa: E402


_TEST_ATTEMPTS = itertools.count()


def _attempt(
    arm_id: str,
    loss: float | None,
    *,
    exact: bool = True,
    qualification: bool = True,
    full: bool = True,
    censored: bool = False,
    hard_veto: bool = False,
    certification: bool = False,
    physical_attempt_id: str | None = None,
) -> AttemptAdmission:
    if physical_attempt_id is None:
        physical_attempt_id = f"test-attempt-{next(_TEST_ATTEMPTS)}"
    return AttemptAdmission(
        arm_id=arm_id,
        loss_n=loss,
        motion_gate=True,
        timing_gate=True,
        qualification=qualification,
        exact=exact,
        sealed=True,
        full_duration=full,
        censored=censored,
        hard_safety_veto=hard_veto,
        certification_pull=certification,
        physical_attempt_id=physical_attempt_id,
    )


def test_frozen_catalog_is_deterministic_unique_and_feasible() -> None:
    first = build_frozen_catalog()
    second = build_frozen_catalog()
    assert first == second
    assert first is second
    assert len(first) == CATALOG_SIZE
    assert len({arm.arm_id for arm in first}) == CATALOG_SIZE
    assert len({tuple(arm.as_dict().values()) for arm in first}) == CATALOG_SIZE
    assert all(arm.force_i_gain <= I_MAX + 1e-15 for arm in first[:128])
    assert first[-2].arm_id == "human-anchor"
    assert first[-1].arm_id == "r013-incumbent-coordinates"
    assert catalog_document()["catalog_sha256"] == catalog_document()["catalog_sha256"]
    warm = deterministic_maximin_sobol()
    assert len(warm) == 6 and len(set(warm)) == 6
    assert all(arm_id.startswith("sobol-") for arm_id in warm)
    document = catalog_document()
    document["arms"][0]["force_p_gain"] = -1.0
    assert catalog_document()["arms"][0]["force_p_gain"] != -1.0
    coordinates = normalized_log_coordinates(first[0])
    coordinates[0] = -999.0
    assert normalized_log_coordinates(first[0])[0] != -999.0


def test_gp_and_certificate_admission_exclude_censored_and_ineligible() -> None:
    arm_id = build_frozen_catalog()[0].arm_id
    eligible = _attempt(arm_id, 0.2)
    censored = _attempt(arm_id, 0.3, censored=True, full=False)
    ineligible = _attempt(arm_id, 0.4, qualification=False)
    assert gp_training_rows([eligible, censored, ineligible]) == [eligible]
    engine = CertificationEngine()
    engine.record(censored)
    engine.record(ineligible)
    assert engine.arms[arm_id].n == 0
    assert not engine.arms[arm_id].forced_full_seen


def test_certificate_forces_full_revisit_and_stops_only_after_all_arms() -> None:
    engine = CertificationEngine()
    arm_ids = list(engine.arms)
    assert engine.next_arm() == arm_ids[0]
    for index, arm_id in enumerate(arm_ids):
        loss = 0.0 if index == 0 else 2.0
        snapshot = engine.record(_attempt(arm_id, loss, certification=True))
        if index < len(arm_ids) - 1:
            assert snapshot.outcome is CertificationOutcome.RUNNING
    assert snapshot.outcome is CertificationOutcome.CERTIFIED
    assert snapshot.best_arm_id == arm_ids[0]
    assert snapshot.all_arms_forced_full


def test_certificate_hard_veto_qualification_failure_and_cap_are_terminal() -> None:
    arm_id = build_frozen_catalog()[0].arm_id
    hard = CertificationEngine()
    assert hard.record(
        _attempt(arm_id, None, hard_veto=True, exact=False, certification=True)
    ).outcome is CertificationOutcome.INVALID_SAFETY

    qualification = CertificationEngine()
    for _ in range(2):
        assert qualification.record(
            _attempt(arm_id, None, exact=False, qualification=False)
        ).outcome is CertificationOutcome.RUNNING
    assert qualification.record(
        _attempt(arm_id, None, exact=False, qualification=False)
    ).outcome is CertificationOutcome.PAUSED_QUALIFICATION

    cap = CertificationEngine()
    for index in range(PHYSICAL_ATTEMPT_CAP):
        snapshot = cap.record(
            AttemptAdmission(
                arm_id=arm_id,
                loss_n=None,
                motion_gate=False,
                timing_gate=False,
                qualification=True,
                exact=False,
                sealed=False,
                full_duration=False,
                physical_attempt_id="cap-attempt-" + str(index),
            )
        )
    assert snapshot.outcome is CertificationOutcome.INCONCLUSIVE_CAP


def test_attempt_identity_is_idempotent_and_distinct_trials_count_separately() -> None:
    engine = CertificationEngine()
    arm_id = next(iter(engine.arms))
    first = _attempt(
        arm_id, 0.2, certification=True, physical_attempt_id="physical-1"
    )
    snapshot = engine.record(first)
    assert engine.record(first) == snapshot
    assert engine.attempt_count == 1
    assert engine.certification_pulls == 1
    assert engine.arms[arm_id].n == 1
    with pytest.raises(R014Error, match="payload conflicts"):
        engine.record(
            _attempt(
                arm_id, 0.3, certification=True, physical_attempt_id="physical-1"
            )
        )
    assert engine.attempt_count == 1 and engine.arms[arm_id].n == 1
    engine.record(
        _attempt(arm_id, 0.3, certification=True, physical_attempt_id="physical-2")
    )
    assert engine.attempt_count == 2 and engine.arms[arm_id].n == 2


def test_attempt_validation_precedes_counters_and_requires_identity() -> None:
    engine = CertificationEngine()
    arm_id = next(iter(engine.arms))
    missing_id = AttemptAdmission(
        arm_id=arm_id,
        loss_n=0.2,
        motion_gate=True,
        timing_gate=True,
        qualification=True,
        exact=True,
        sealed=True,
        full_duration=True,
    )
    with pytest.raises(R014Error, match="physical attempt id"):
        engine.record(missing_id)
    assert engine.attempt_count == 0
    bad = _attempt(arm_id, float("nan"), physical_attempt_id="nan-loss")
    with pytest.raises(R014Error, match="finite"):
        engine.record(bad)
    assert engine.attempt_count == 0
    negative = _attempt(arm_id, -0.1, physical_attempt_id="negative-loss")
    with pytest.raises(R014Error, match="nonnegative"):
        engine.record(negative)
    assert engine.attempt_count == 0
    strict = _attempt(arm_id, 0.2, physical_attempt_id="strict-flag")
    object.__setattr__(strict, "timing_gate", 1)
    with pytest.raises(R014Error, match="strict boolean"):
        engine.record(strict)
    assert engine.attempt_count == 0


def test_discovery_loss_never_enters_certificate_and_hard_veto_is_terminal() -> None:
    engine = CertificationEngine()
    arm_id = next(iter(engine.arms))
    engine.record(
        _attempt(arm_id, 0.2, physical_attempt_id="discovery-1", certification=False)
    )
    assert engine.arms[arm_id].n == 0
    assert engine.certification_pulls == 0
    engine.record(
        _attempt(arm_id, 0.3, physical_attempt_id="certification-1", certification=True)
    )
    assert engine.arms[arm_id].n == 1
    veto = CertificationEngine()
    outcome = veto.record(
        _attempt(
            arm_id,
            None,
            exact=False,
            full=False,
            hard_veto=True,
            physical_attempt_id="early-veto",
        )
    )
    assert outcome.outcome is CertificationOutcome.INVALID_SAFETY


def test_anytime_radius_decreases_on_practical_range() -> None:
    radii = [anytime_radius(n) for n in (1, 2, 5, 10, 50, 100, 500)]
    assert all(left > right for left, right in zip(radii, radii[1:]))


def test_cli_contract_rejects_invalid_combinations_and_reports_home_first(
    capsys, tmp_path: Path
) -> None:
    with pytest.raises(SystemExit):
        parse_request(["--trajectory", "arc", "--dry-run"])
    assert "unsupported trajectory" in capsys.readouterr().err
    with pytest.raises(R014Error, match="requires --attempts"):
        parse_request(["--mode", "budgeted", "--dry-run"])
    with pytest.raises(R014Error, match="forces early-stop=off"):
        parse_request(["--mode", "certify", "--early-stop", "on", "--dry-run"])

    registry = _minimal_registry(tmp_path)
    registry.install_defaults()
    request = parse_request(["--strategy", "finite-time", "--dry-run"])
    plan = Dispatcher(registry.experiment_root, state_root=tmp_path / "state").dispatch(request)
    assert plan["motion"] is False
    assert plan["home_before_arm"] is True
    assert plan["state_machine"].index("HOME_VERIFIED") < plan["state_machine"].index("RUNNING")
    assert "profile-not-qualified:pending" in plan["blockers"]


def test_bare_live_command_fails_closed_without_current_qualified_pointer(tmp_path: Path) -> None:
    registry = _minimal_registry(tmp_path)
    dispatcher = Dispatcher(registry.experiment_root, state_root=tmp_path / "state")
    with pytest.raises(R014Error, match="current-qualified pointer is absent"):
        dispatcher.dispatch(parse_request([]))


def test_qualified_live_owner_dispatch_requires_contract_and_closed_home_readback(
    tmp_path: Path,
) -> None:
    registry = _minimal_registry(tmp_path)
    pending = registry.install_defaults()["finite-time-r08-formal-v1"]
    qualified = dict(pending.raw)
    qualified["qualification"] = {"status": "qualified", "receipts": ["test"]}
    qualified_path = registry.root / "profiles" / sha256_value(qualified) / "profile.json"
    atomic_write_json(qualified_path, qualified)
    profile = registry.promote(qualified_path)
    dispatcher = Dispatcher(registry.experiment_root, state_root=tmp_path / "state")
    request = parse_request(["--strategy", "current", "--run", "qualified-run"])

    with pytest.raises(R014Error, match="contract is absent"):
        dispatcher.dispatch(request)
    assert not dispatcher.runs_root.exists()

    owner = tmp_path / "owner_backend.py"
    owner.write_text(
        """#!/usr/bin/env python3
import json
import sys
from pathlib import Path

run_dir = Path(sys.argv[sys.argv.index("--run-dir") + 1])
plan_path = Path(sys.argv[sys.argv.index("--plan-path") + 1])
assert plan_path.is_file()
state_path = run_dir / "state.json"
state = json.loads(state_path.read_text(encoding="utf-8"))
state["transitions"] = [
    "CREATED", "PREFLIGHT_OK", "HOME_VERIFIED", "RUNNING",
    "RETURN_HOME", "STOPPED_HOME", "CLOSED",
]
state["state"] = "CLOSED"
state["home_verified"] = True
state["closed_home"] = True
state["terminal_home"] = {
    "state": "STOPPED_HOME", "home_verified": True, "closed_home": True,
}
state_path.write_text(json.dumps(state), encoding="utf-8")
""",
        encoding="utf-8",
    )
    owner.chmod(0o755)
    atomic_write_json(
        registry.root / "qualification/live-backend.json",
        {
            "schema": "step5d.autotuner-r014/live-backend-contract-v1",
            "version": 1,
            "executable": str(owner),
            "arguments": ["--run-dir", "{run_dir}", "--plan-path", "{plan_path}"],
            "profile_sha256": profile.sha256,
            "source_identity": dict(profile.raw["host_source"]),
            "catalog_sha256": catalog_document()["catalog_sha256"],
            "metric": dict(profile.raw["metric"]),
        },
    )

    result = dispatcher.dispatch(request)
    assert result["ok"] is True
    state = json.loads((dispatcher.runs_root / "qualified-run" / "state.json").read_text())
    assert state["state"] == "CLOSED"
    assert state["transitions"][-2:] == ["STOPPED_HOME", "CLOSED"]
    assert state["home_verified"] is True and state["closed_home"] is True


def _minimal_registry(tmp_path: Path) -> ProfileRegistry:
    experiment = tmp_path / "experiment"
    release = experiment / "config/step5d/releases"
    manifest_body = {"schema": "test-release", "version": 1}
    manifest_path = release / "test-release" / "manifest.json"
    atomic_write_json(manifest_path, manifest_body)
    manifest_sha = sha256_file(manifest_path)
    atomic_write_json(
        experiment / "config/step5d/current.json",
        {
            "schema": "step5d.autotune-v3/current-release-pointer-v1",
            "manifest_path": "config/step5d/releases/test-release/manifest.json",
            "manifest_sha256": manifest_sha,
        },
    )
    atomic_write_json(
        experiment / "config/step5d/r014_source_capture.json",
        {"schema": "step5d.r014/source-capture-v1", "manifest_sha256": "a" * 64},
    )
    registry = ProfileRegistry(experiment)
    host_manifest = registry.root / "source-closures/test/manifest.json"
    atomic_write_json(
        host_manifest,
        {"schema": "step5d.autotuner-r014/host-source-closure-v1", "files": {}},
    )
    atomic_write_json(
        registry.host_source_pointer_path,
        {
            "schema": "step5d.autotuner-r014/host-source-pointer-v1",
            "manifest_path": "source-closures/test/manifest.json",
            "manifest_sha256": sha256_file(host_manifest),
        },
    )
    return registry


def _qualified_registry(tmp_path: Path) -> tuple[ProfileRegistry, object]:
    registry = _minimal_registry(tmp_path)
    pending = registry.install_defaults()["finite-time-r08-formal-v1"]
    qualified = dict(pending.raw)
    qualified["qualification"] = {"status": "qualified", "receipts": ["test"]}
    qualified_path = registry.root / "profiles" / sha256_value(qualified) / "profile.json"
    atomic_write_json(qualified_path, qualified)
    return registry, registry.promote(qualified_path)


def _tree_snapshot(root: Path) -> dict[str, bytes | None]:
    snapshot: dict[str, bytes | None] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        snapshot[relative] = path.read_bytes() if path.is_file() else None
    return snapshot


def _run_state_from_plan(
    plan: dict[str, object], run_id: str, *, state_name: str = "CLOSED"
) -> dict[str, object]:
    command = plan["command"]
    assert isinstance(command, dict)
    transitions = ["CREATED", "PREFLIGHT_OK", "HOME_VERIFIED", "RUNNING"]
    if state_name == "STOPPED_HOME":
        transitions.extend(("RETURN_HOME", "STOPPED_HOME"))
    else:
        transitions.extend(("RETURN_HOME", "STOPPED_HOME", "CLOSED"))
    return {
        "schema": "step5d.autotuner-r014/run-state-v1",
        "version": 1,
        "run_id": run_id,
        "state": state_name,
        "profile_path": command["profile_path"],
        "profile_sha256": command["profile_sha256"],
        "source_identity": command["source_identity"],
        "catalog_sha256": command["catalog_sha256"],
        "metric": command["metric"],
        "campaign_fingerprint": plan["campaign_fingerprint"],
        "home_before_arm": True,
        "home_verified": True,
        "closed_home": True,
        "terminal_home": {
            "state": "STOPPED_HOME",
            "home_verified": True,
            "closed_home": True,
        },
        "transitions": transitions,
    }


def test_profile_promotion_requires_qualified_content_address(tmp_path: Path) -> None:
    registry = _minimal_registry(tmp_path)
    pending = registry.install_defaults()["finite-time-r08-formal-v1"]
    with pytest.raises(R014Error, match="exact qualified formal"):
        registry.promote(pending.path)

    qualified = dict(pending.raw)
    qualified["qualification"] = {
        "status": "qualified",
        "receipts": ["small-qualification-receipt-sha256"],
    }
    identity = sha256_value(qualified)
    qualified_path = registry.root / "profiles" / identity / "profile.json"
    atomic_write_json(qualified_path, qualified)
    promoted = registry.promote(qualified_path)
    assert promoted.sha256 == identity
    pointer = json.loads(registry.pointer_path.read_text(encoding="utf-8"))
    assert pointer["schema"] == POINTER_SCHEMA
    assert registry.current_qualified().sha256 == identity


def test_profile_lookup_and_all_dry_runs_are_read_only(tmp_path: Path) -> None:
    registry = _minimal_registry(tmp_path)
    dispatcher = Dispatcher(registry.experiment_root, state_root=tmp_path / "state")
    before = _tree_snapshot(tmp_path)

    profile = registry.by_name("finite-time")
    assert profile.name == FORMAL_PROFILE_NAME
    start = dispatcher.dispatch(
        parse_request(["--strategy", "finite-time", "--dry-run"])
    )
    assert start["dry_run"] is True
    status = dispatcher.dispatch(parse_request(["status", "--dry-run"]))
    assert status["ok"] is True
    with pytest.raises(R014Error):
        dispatcher.dispatch(parse_request(["stop", "--run", "missing", "--dry-run"]))
    with pytest.raises(R014Error):
        dispatcher.dispatch(parse_request(["resume", "--dry-run"]))
    assert _tree_snapshot(tmp_path) == before


def test_dry_run_plan_is_not_live_ready_without_backend_contract(tmp_path: Path) -> None:
    registry, _profile = _qualified_registry(tmp_path)
    dispatcher = Dispatcher(registry.experiment_root, state_root=tmp_path / "state")
    before = _tree_snapshot(tmp_path)
    plan = dispatcher.dispatch(parse_request(["--strategy", "current", "--dry-run"]))
    assert plan["ready_for_live"] is False
    assert "live backend contract is absent" in plan["blockers"]
    assert plan["backend_validation"]["status"] == "absent"
    assert _tree_snapshot(tmp_path) == before


def test_stop_only_publishes_idempotent_marker_and_preserves_owner_state(
    tmp_path: Path,
) -> None:
    registry = _minimal_registry(tmp_path)
    dispatcher = Dispatcher(registry.experiment_root, state_root=tmp_path / "state")
    run_dir = dispatcher.runs_root / "active-run"
    state = {
        "schema": "step5d.autotuner-r014/run-state-v1",
        "version": 1,
        "run_id": "active-run",
        "state": "RUNNING",
        "owner_counter": 7,
    }
    atomic_write_json(run_dir / "state.json", state)
    state_before = (run_dir / "state.json").read_bytes()

    first = dispatcher.dispatch(parse_request(["stop", "--run", "active-run"]))
    assert first["request_submitted"] is True
    assert (run_dir / "state.json").read_bytes() == state_before
    assert (run_dir / "stop.requested").read_bytes() == b"graceful\n"

    state["state"] = "RETURN_HOME"
    atomic_write_json(run_dir / "state.json", state)
    state_before_second = (run_dir / "state.json").read_bytes()
    second = dispatcher.dispatch(parse_request(["stop", "--run", "active-run"]))
    assert second["request_submitted"] is False
    assert (run_dir / "state.json").read_bytes() == state_before_second


def test_late_stop_on_terminal_run_is_noop(tmp_path: Path) -> None:
    registry = _minimal_registry(tmp_path)
    dispatcher = Dispatcher(registry.experiment_root, state_root=tmp_path / "state")
    run_dir = dispatcher.runs_root / "closed-run"
    state = {
        "schema": "step5d.autotuner-r014/run-state-v1",
        "version": 1,
        "run_id": "closed-run",
        "state": "CLOSED",
    }
    atomic_write_json(run_dir / "state.json", state)
    before = (run_dir / "state.json").read_bytes()
    result = dispatcher.dispatch(parse_request(["stop", "--run", "closed-run"]))
    assert result["late"] is True
    assert result["state"] == "CLOSED"
    assert not (run_dir / "stop.requested").exists()
    assert (run_dir / "state.json").read_bytes() == before


def test_run_id_is_safe_and_status_reports_qualification_backend_without_writes(
    tmp_path: Path,
) -> None:
    registry = _minimal_registry(tmp_path)
    dispatcher = Dispatcher(registry.experiment_root, state_root=tmp_path / "state")
    for unsafe in ("../escape", str(tmp_path / "absolute"), ""):
        with pytest.raises(R014Error, match="run id"):
            dispatcher.dispatch(parse_request(["stop", "--run", unsafe, "--dry-run"]))
    before = _tree_snapshot(tmp_path)
    status = dispatcher.dispatch(parse_request(["status", "--dry-run"]))
    assert status["qualification"]["current_qualified"] is False
    assert status["backend"]["resume_capability"] is False
    assert _tree_snapshot(tmp_path) == before


def test_closed_validator_accepts_acknowledged_and_late_stop_marker(tmp_path: Path) -> None:
    registry, _profile = _qualified_registry(tmp_path)
    dispatcher = Dispatcher(registry.experiment_root, state_root=tmp_path / "state")
    plan = dispatcher.dispatch(
        parse_request(["--strategy", "current", "--dry-run"])
    )
    base = _run_state_from_plan(plan, "validator-run")
    run_dir = dispatcher.runs_root / "validator-run"
    marker = run_dir / "stop.requested"
    atomic_write_json(run_dir / "state.json", base)
    marker.write_text("graceful\n", encoding="utf-8")
    _validate_closed_backend_state(
        base,
        run_id="validator-run",
        plan=plan,
        stop_marker=marker,
    )
    acknowledged = dict(base)
    acknowledged["transitions"] = [
        "CREATED",
        "PREFLIGHT_OK",
        "HOME_VERIFIED",
        "RUNNING",
        "STOP_REQUESTED",
        "RETURN_HOME",
        "STOPPED_HOME",
        "CLOSED",
    ]
    _validate_closed_backend_state(
        acknowledged,
        run_id="validator-run",
        plan=plan,
        stop_marker=marker,
    )


def test_resume_accepts_closed_or_stopped_home_but_normal_is_blocked(
    tmp_path: Path,
) -> None:
    registry, _profile = _qualified_registry(tmp_path)
    dispatcher = Dispatcher(registry.experiment_root, state_root=tmp_path / "state")
    plan = dispatcher.dispatch(
        parse_request(["--strategy", "current", "--dry-run"])
    )
    for state_name in ("STOPPED_HOME", "CLOSED"):
        run_dir = dispatcher.runs_root / "resume-run"
        atomic_write_json(
            run_dir / "state.json",
            _run_state_from_plan(plan, "resume-run", state_name=state_name),
        )
        atomic_write_json(run_dir / "dispatch-plan.json", plan)
        before = _tree_snapshot(tmp_path)
        dry = dispatcher.dispatch(
            parse_request(["resume", "--run", "resume-run", "--dry-run"])
        )
        assert dry["status"] == "ELIGIBLE"
        assert dry["dispatched"] is False
        assert _tree_snapshot(tmp_path) == before
        blocked = dispatcher.dispatch(
            parse_request(["resume", "--run", "resume-run"])
        )
        assert blocked["status"] == "BLOCKED"
        assert blocked["ok"] is False
        assert blocked["dispatched"] is False


def _qualification_bundle(registry: ProfileRegistry, tmp_path: Path) -> Path:
    pending = registry.install_defaults()["finite-time-r08-formal-v1"]
    evidence = tmp_path / "evidence.json"
    atomic_write_json(evidence, {"schema": "test-evidence", "passed": True})
    bundle = {
        "schema": QUALIFICATION_SCHEMA,
        "version": 1,
        "profile_sha256": pending.sha256,
        "host_source_manifest_sha256": pending.raw["host_source"]["host_source_manifest_sha256"],
        "release_manifest_sha256": pending.raw["release"]["manifest_sha256"],
        "fatal_failure": False,
        "formal_timing": {
            "solver": {"passed": True, "samples": 10_000},
            "full_tick": {"passed": True, "samples": 30_000},
            "safe_hold": {"passed": True, "samples": 30_000},
        },
        "tp_package": {"uploaded": True, "read_back": True, "exact_bytes": True},
        "home_no_contact_canary": {
            "verified_home": True,
            "safety_normal": True,
            "no_contact": True,
        },
        "contact_acquisitions": [
            {"role": "NON_BO", "qualified": True, "safe_return_home": True}
            for _ in range(3)
        ],
        "incumbent_trials": [
            {
                "candidate": INCUMBENT,
                "phase_correction": "off",
                "duration_s": 60.0,
                "exact": True,
                "sealed": True,
                "qualified": True,
                "safe_return_home": True,
                "hard_safety_veto": False,
            }
            for _ in range(3)
        ],
        "evidence": [{"path": str(evidence), "sha256": sha256_file(evidence)}],
    }
    path = tmp_path / "qualification.json"
    atomic_write_json(path, bundle)
    return path


def test_qualification_failure_preserves_pointer_and_valid_bundle_promotes(tmp_path: Path) -> None:
    registry = _minimal_registry(tmp_path)
    bundle_path = _qualification_bundle(registry, tmp_path)
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    bundle["home_no_contact_canary"]["verified_home"] = False
    atomic_write_json(bundle_path, bundle)
    with pytest.raises(R014Error, match="Home/no-contact gate failed"):
        promote_small_qualification(registry.experiment_root, bundle_path)
    assert not registry.pointer_path.exists()

    bundle_path = _qualification_bundle(registry, tmp_path)
    result = promote_small_qualification(registry.experiment_root, bundle_path)
    assert result.qualified_profile.qualification_status == "qualified"
    assert registry.current_qualified().sha256 == result.qualified_profile.sha256


def test_whole_flow_falsifiers_cover_figure8_writer_stop_and_resume(tmp_path: Path) -> None:
    registry = _minimal_registry(tmp_path)
    registry.install_defaults()
    dispatcher = Dispatcher(registry.experiment_root, state_root=tmp_path / "state")
    figure8 = dispatcher.dispatch(
        parse_request(["--trajectory", "figure8", "--strategy", "finite-time", "--dry-run"])
    )
    assert any(blocker.startswith("figure8-") for blocker in figure8["blockers"])

    lock = WriterLock(tmp_path / "writer.lock")
    with lock:
        with pytest.raises(R014Error, match="single-writer"):
            with WriterLock(tmp_path / "writer.lock"):
                pass

    run_dir = dispatcher.runs_root / "run-a"
    atomic_write_json(
        run_dir / "state.json",
        {
            "schema": "step5d.autotuner-r014/run-state-v1",
            "run_id": "run-a",
            "state": "RUNNING",
        },
    )
    stopped = dispatcher.dispatch(parse_request(["stop", "--run", "run-a"]))
    assert stopped["state"] == "STOP_REQUESTED"
    assert (run_dir / "stop.requested").read_text(encoding="utf-8") == "graceful\n"

    for name in ("closed-a", "closed-b"):
        atomic_write_json(
            dispatcher.runs_root / name / "state.json",
            {
                "schema": "step5d.autotuner-r014/run-state-v1",
                "run_id": name,
                "state": "STOPPED_HOME",
            },
        )
    with pytest.raises(R014Error, match="exactly one"):
        dispatcher.dispatch(parse_request(["resume"]))


def test_timing_ineligible_attempt_can_be_retried_exactly() -> None:
    engine = CertificationEngine()
    arm_id = next(iter(engine.arms))
    timing_bad = AttemptAdmission(
        arm_id=arm_id,
        loss_n=0.2,
        motion_gate=True,
        timing_gate=False,
        qualification=True,
        exact=True,
        sealed=True,
        full_duration=True,
        certification_pull=True,
        physical_attempt_id="timing-bad",
    )
    engine.record(timing_bad)
    assert engine.arms[arm_id].n == 0
    assert engine.next_arm() == arm_id
    engine.record(_attempt(arm_id, 0.2, certification=True))
    assert engine.arms[arm_id].n == 1
    assert engine.arms[arm_id].forced_full_seen


def test_discovery_schedule_and_early_censor_boundary_are_exact() -> None:
    warm = warm_start_arm_ids()
    assert warm[:2] == ("human-anchor", "r013-incumbent-coordinates")
    assert len(warm) == 8 and len(set(warm)) == 8
    assert all(warm_start_proposal(index).forced_full for index in range(1, 9))
    assert should_censor(
        physical_attempt_index=9,
        role="ordinary_novel_bo",
        prefix_bin_mae_n=[0.61] * 25,
        qualification_incumbent_repeat_mean_n=0.30,
    )
    for role in ("anchor", "handoff", "repeat", "boundary", "fixed", "certification"):
        assert not should_censor(
            physical_attempt_index=9,
            role=role,
            prefix_bin_mae_n=[10.0] * 25,
            qualification_incumbent_repeat_mean_n=0.30,
        )
    assert not should_censor(
        physical_attempt_index=65,
        role="ordinary_novel_bo",
        prefix_bin_mae_n=[10.0] * 25,
        qualification_incumbent_repeat_mean_n=0.30,
    )


def test_censored_arm_is_revisited_but_never_trained_and_no_qlognei_fallback() -> None:
    arms = build_frozen_catalog()
    censored = _attempt(
        arms[0].arm_id,
        1.0,
        censored=True,
        full=False,
        physical_attempt_id="censored-1",
    )
    assert censored_revisit_ids([censored]) == (arms[0].arm_id,)
    assert gp_training_rows([censored]) == []
    with pytest.raises(R014Error, match="eight forced-full"):
        DiscreteQLogNEISelector().select([])


def test_gp_training_deduplicates_identity_and_requires_valid_physical_id() -> None:
    arm_id = build_frozen_catalog()[0].arm_id
    eligible = _attempt(arm_id, 0.2, physical_attempt_id="gp-1")
    assert gp_training_rows([eligible, eligible]) == [eligible]
    with pytest.raises(R014Error, match="payload conflicts"):
        gp_training_rows(
            [
                eligible,
                _attempt(arm_id, 0.4, physical_attempt_id="gp-1"),
            ]
        )
    no_id = AttemptAdmission(
        arm_id=arm_id,
        loss_n=0.2,
        motion_gate=True,
        timing_gate=True,
        qualification=True,
        exact=True,
        sealed=True,
        full_duration=True,
    )
    with pytest.raises(R014Error, match="physical attempt id"):
        gp_training_rows([no_id])


def test_qlognei_requires_the_actual_eight_warm_start_arms() -> None:
    arm_id = build_frozen_catalog()[0].arm_id
    attempts = [
        _attempt(arm_id, 0.2, physical_attempt_id=f"same-arm-{index}")
        for index in range(8)
    ]
    with pytest.raises(R014Error, match="distinct arms"):
        DiscreteQLogNEISelector().select(attempts)


def test_primary_and_secondary_metrics_use_exact_declared_bins() -> None:
    rows = [(index * 0.1 + 0.05, 6.0 if index < 50 else 5.5) for index in range(600)]
    metric = force_bin_metric(rows)
    assert metric.primary_mae_n == pytest.approx((50 * 1.0 + 550 * 0.5) / 600)
    assert metric.secondary_mae_n == pytest.approx(0.5)
    assert metric.metric_fingerprint == METRIC_FINGERPRINT
    with pytest.raises(R014Error, match="600 complete bins"):
        force_bin_metric(rows[:-1])


def test_confidence_radius_monte_carlo_coverage_sanity() -> None:
    """Diagnostic only: the anytime union radius should be conservative."""

    generator = np.random.default_rng(20260821)
    failures = 0
    simulations = 400
    n_max = 50
    radii = np.asarray([anytime_radius(n) for n in range(1, n_max + 1)])
    for _ in range(simulations):
        samples = generator.normal(0.0, 0.10, size=(130, n_max))
        means = np.cumsum(samples, axis=1) / np.arange(1, n_max + 1)
        if np.any(np.abs(means) > radii):
            failures += 1
    assert failures / simulations <= 0.07


def test_solver_profiles_are_exact_isolated_and_not_bo_dimensions() -> None:
    assert FINITE_TIME_R08.as_dict() == {
        "schema": "step5d.autotuner-r014/solver-profile-v1",
        "version": 1,
        "id": "finite-time-r08",
        "r": 0.8,
        "epsilon": 0.01,
        "iterations": 512,
        "backend": "cupy",
        "qdot_limit_rad_s": 0.05,
        "preconstruct_outside_tick": True,
    }
    assert LEGACY_R1.r == 1.0 and LEGACY_R1.backend == "numpy"
    assert LEGACY_R1.iterations == 1 and LEGACY_R1.qdot_limit_rad_s == 0.15
    with pytest.raises(R014Error, match="fields differ"):
        SolverProfile(
            profile_id="finite-time-r08",
            r=1.0,
            epsilon=0.01,
            iterations=512,
            backend="cupy",
            qdot_limit_rad_s=0.05,
            preconstruct_outside_tick=True,
        )
    for arm in build_frozen_catalog():
        assert not {"r", "epsilon", "iterations", "backend"}.intersection(arm.as_dict())
    formal_config = strict_rnn_config(
        FINITE_TIME_R08,
        paper_truth_path=ROOT / "config/step5d_liveprep_solver_gate.json",
        motion_qdot_limit_rad_s=0.08,
    )
    assert formal_config.qdot_limit_rad_s == 0.05
    assert formal_config.epsilon == 0.01
    assert formal_config.sigr_exponent_r == 0.8
    assert formal_config.inner_iterations == 512
    assert formal_config.backend == "cupy"


def test_r013_runtime_patch_injects_selected_solver_before_any_tick(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Base:
        def __init__(self, *args, solver_profile=None, feedforward_enabled=True, **kwargs):
            del args, kwargs
            captured["solver_profile"] = solver_profile
            captured["feedforward_enabled"] = feedforward_enabled
            self.candidate = None

    monkeypatch.setattr(r013_live_runtime, "_ACTIVE_SOLVER_PROFILE", FINITE_TIME_R08)
    runtime_type = r013_live_runtime._make_runtime_class(Base)
    runtime = runtime_type()
    assert captured["solver_profile"] is FINITE_TIME_R08
    assert runtime._r013_solver_profile is FINITE_TIME_R08
    with pytest.raises(ValueError, match="solver profile differs"):
        runtime_type(solver_profile=LEGACY_R1)
