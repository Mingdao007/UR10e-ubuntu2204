from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r009.contracts import (  # noqa: E402
    build_contract,
    load_contract,
    persist_contract,
    validate_contract,
)
from step5d_autotune_v4_r009.identity import (  # noqa: E402
    R009IdentityError,
    build_behavior_manifest,
    build_behavior_manifest_from_r006,
    source_set_digest,
)
from step5d_autotune_v4_r009.ledger import (  # noqa: E402
    R009HistoricalLineageError,
    R009IdentityAdmissionError,
    R009Ledger,
    load_historical_r008_read_only,
)
from step5d_autotune_v4_r009.quarantine import (  # noqa: E402
    R008HistoricalLineageDoNotResumeError,
)


def _manifest(*, parent: str = "a" * 64, source_value: str = "b" * 64):
    return build_behavior_manifest(
        parent_r006_contract_sha256=parent,
        parent_r006_source_closure_sha256="c" * 64,
        source_set={"tools/behavior.py": source_value},
    )


def _bundle(*, parent: str = "a" * 64, source_value: str = "b" * 64):
    return build_contract(
        behavior_manifest=_manifest(parent=parent, source_value=source_value),
        controller_triplet_sha256={
            "script": "1" * 64,
            "txt": "2" * 64,
            "urp": "3" * 64,
        },
    )


def test_parent_and_source_digest_drift_changes_campaign_and_release_identity() -> None:
    baseline = _bundle()
    parent_drift = _bundle(parent="d" * 64)
    source_drift = _bundle(source_value="e" * 64)

    assert baseline.campaign_fingerprint != parent_drift.campaign_fingerprint
    assert baseline.campaign_fingerprint != source_drift.campaign_fingerprint
    assert baseline.release_identity_sha256 != parent_drift.release_identity_sha256
    assert baseline.release_identity_sha256 != source_drift.release_identity_sha256


def test_manifest_contract_and_release_identity_are_structurally_immutable() -> None:
    bundle = _bundle()
    manifest = bundle.behavior_manifest
    baseline = (
        bundle.campaign_fingerprint,
        bundle.release_identity_sha256,
        bundle.sha256,
    )

    with pytest.raises(TypeError):
        manifest.source_set.files["tools/behavior.py"] = "d" * 64
    with pytest.raises(TypeError):
        manifest.contact_search_schedule.raw["stages"][0]["speed_m_s"] = 0.001
    with pytest.raises(TypeError):
        manifest.executable_behavior_config.raw["values"]["path_duration_s"] = 61.0
    with pytest.raises(TypeError):
        bundle.raw["behavior_manifest"]["contact_search_schedule"]["stages"][0][
            "speed_m_s"
        ] = 0.002
    with pytest.raises(TypeError):
        bundle.release_identity.controller_triplet_sha256["script"] = "f" * 64

    manifest_view = manifest.as_dict()
    assert isinstance(manifest_view, dict)
    assert isinstance(manifest_view["contact_search_schedule"]["stages"], list)
    assert isinstance(manifest_view["executable_behavior_config"]["values"], dict)
    manifest_view["contact_search_schedule"]["stages"][0]["speed_m_s"] = 0.003
    manifest_view["executable_behavior_config"]["values"]["path_duration_s"] = 62.0

    identity_view = bundle.release_identity.as_dict()
    identity_view["controller_triplet_sha256"]["script"] = "e" * 64

    contract_view = bundle.contract_document
    assert isinstance(contract_view, dict)
    contract_view["behavior_manifest"]["source_closure"]["files"][
        "tools/behavior.py"
    ] = "e" * 64
    contract_view["behavior_manifest"]["contact_search_schedule"]["stages"][0][
        "speed_m_s"
    ] = 0.004
    json.dumps(contract_view, sort_keys=True, separators=(",", ":"))

    assert (
        bundle.campaign_fingerprint,
        bundle.release_identity_sha256,
        bundle.sha256,
    ) == baseline


def test_parent_r006_binding_uses_declared_source_closure_basis_digest() -> None:
    parent = SimpleNamespace(
        sha256="a" * 64,
        raw={
            "source_closure": {
                "path": "missing-transport-file.json",
                "sha256": "c" * 64,
            }
        },
    )
    manifest = build_behavior_manifest_from_r006(
        parent=parent,
        source_set={"tools/behavior.py": "b" * 64},
    )
    assert manifest.parent_r006_source_closure_sha256 == "c" * 64


def test_source_digest_requires_explicit_generated_contract_exclusion() -> None:
    generated = "config/step5d/autotune_v4_r009.json"
    with pytest.raises(R009IdentityError, match="self-referential"):
        source_set_digest({generated: "a" * 64, "src.py": "b" * 64})
    digest = source_set_digest(
        {generated: "a" * 64, "src.py": "b" * 64}, exclude_paths=(generated,)
    )
    assert digest == source_set_digest({"src.py": "b" * 64})


def test_build_validate_load_leave_contract_sentinel_bytes_and_mtime_unchanged(
    tmp_path: Path,
) -> None:
    bundle = _bundle()
    contract_path = tmp_path / "r009.json"
    identity_path = tmp_path / "r009.identity.json"
    persist_contract(bundle, contract_path, identity_path=identity_path)
    before_bytes = contract_path.read_bytes()
    before_stat = contract_path.stat()
    before_hash = hashlib.sha256(before_bytes).hexdigest()

    rebuilt = _bundle()
    validate_contract(rebuilt)
    loaded = load_contract(contract_path, identity_path=identity_path)

    after_stat = contract_path.stat()
    assert rebuilt.sha256 == loaded.sha256 == before_hash
    assert contract_path.read_bytes() == before_bytes
    assert (after_stat.st_mtime_ns, after_stat.st_size) == (
        before_stat.st_mtime_ns,
        before_stat.st_size,
    )


def test_explicit_persist_is_deterministic_and_pure_load_validates(tmp_path: Path) -> None:
    bundle = _bundle()
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first_identity = tmp_path / "first.identity.json"
    second_identity = tmp_path / "second.identity.json"

    persist_contract(bundle, first, identity_path=first_identity)
    persist_contract(bundle, second, identity_path=second_identity)

    assert first.read_bytes() == second.read_bytes()
    assert first_identity.read_bytes() == second_identity.read_bytes()
    loaded = load_contract(first, identity_path=first_identity)
    assert loaded.release_identity.as_dict() == bundle.release_identity.as_dict()
    assert validate_contract(loaded).release_identity_sha256 == bundle.release_identity_sha256


def test_r009_ledger_requires_exact_header_and_observation_identity(tmp_path: Path) -> None:
    bundle = _bundle()
    ledger_path = tmp_path / "r009.jsonl"
    ledger = R009Ledger.create(ledger_path, bundle.release_identity)
    assert ledger.header["release_identity"] == bundle.release_identity.as_dict()
    assert ledger.header["release_identity_sha256"] == bundle.release_identity_sha256
    exact = {
        "release_identity_sha256": bundle.release_identity.release_identity_sha256,
        "campaign_fingerprint": bundle.campaign_fingerprint,
        "attempt_sequence": 1,
        "objective": 0.25,
    }
    ledger.append(exact)
    assert len(R009Ledger.load(ledger_path, expected_release_identity=bundle.release_identity).observations) == 1

    original = ledger_path.read_bytes()
    missing = dict(exact)
    missing.pop("release_identity_sha256")
    with pytest.raises(R009IdentityAdmissionError):
        ledger.append(missing)
    mismatched = dict(exact)
    mismatched["release_identity_sha256"] = "f" * 64
    with pytest.raises(R009IdentityAdmissionError):
        ledger.append(mismatched)
    assert ledger_path.read_bytes() == original

    other = _bundle(parent="9" * 64)
    with pytest.raises(R009IdentityAdmissionError):
        R009Ledger.load(ledger_path, expected_release_identity=other.release_identity)


def test_historical_r008_ledger_is_readable_only_through_read_only_api(tmp_path: Path) -> None:
    historical_path = tmp_path / "r008.jsonl"
    historical_path.write_text(
        json.dumps({"schema": "step5d.autotune-v4/r008-observations-v1", "record_type": "header"})
        + "\n"
        + json.dumps({"campaign_fingerprint": "b" * 64, "objective": 1.0})
        + "\n",
        encoding="utf-8",
    )
    historical = load_historical_r008_read_only(historical_path)
    assert len(historical.rows) == 2
    with pytest.raises(R009HistoricalLineageError, match="optimizer"):
        historical.optimizer_input()
    with pytest.raises(R009HistoricalLineageError, match="resume"):
        historical.resume_input()
    with pytest.raises(R009HistoricalLineageError, match="read-only"):
        historical.append({})

    with pytest.raises(R009IdentityAdmissionError):
        R009Ledger.load(historical_path)


@pytest.mark.parametrize(
    "allow_value",
    (None, "", "baseline_ramp_canary", "anything", "b461ed52", "wave5_far_double_canary"),
)
def test_formal_r008_entrypoints_have_no_probe_bypass_and_fail_closed_before_live_entry(
    monkeypatch, allow_value
) -> None:
    launcher_paths = (
        ROOT / "tools/run_step5d_autotune_v4_r008.py",
        ROOT / "tools/run_step5d_autotune_v4_r008_b3_two_stage.py",
        ROOT / "tools/launch_step5d_autotune_v4_r008_control.py",
        ROOT / "tools/step5d_autotune_v4_r008/live_adapter.py",
    )
    for path in launcher_paths:
        source = path.read_text(encoding="utf-8")
        assert "r008_rtde_seq_probe_inject" not in source
        assert "install_source_closure_bypass" not in source
        assert "contracts._validate_source_closure" not in source

    # These launchers import optional live-only dependencies at module import
    # time.  Check their entry guards without importing that dependency graph:
    # the quarantine call must precede every live construction/dispatch call.
    for path, function_name in zip(
        launcher_paths[:3], ("run_live", "run_live", "main"), strict=True
    ):
        source = path.read_text(encoding="utf-8")
        function = next(
            node
            for node in ast.parse(source).body
            if isinstance(node, ast.FunctionDef) and node.name == function_name
        )
        assert function.body, f"{path} formal entrypoint has no body"
        assert (
            isinstance(function.body[0], ast.Expr)
            and isinstance(function.body[0].value, ast.Constant)
            and isinstance(function.body[0].value.value, str)
        ), f"{path} formal entrypoint must start with a docstring"
        guard_index = next(
            (
                index
                for index, statement in enumerate(function.body[1:], start=1)
                if (
                    isinstance(statement, ast.Expr)
                    and isinstance(statement.value, ast.Call)
                    and isinstance(statement.value.func, ast.Name)
                    and statement.value.func.id == "reject_r008_formal_resume"
                )
            ),
            None,
        )
        assert guard_index is not None, f"{path} formal entrypoint guard is missing"
        guard_statement = function.body[guard_index]
        assert (
            isinstance(guard_statement, ast.Expr)
            and isinstance(guard_statement.value, ast.Call)
            and isinstance(guard_statement.value.func, ast.Name)
            and guard_statement.value.func.id == "reject_r008_formal_resume"
        ), f"{path} formal entrypoint guard is not docstring-first"
        assert not any(
            isinstance(node, (ast.If, ast.Return))
            or (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "os"
                and node.attr in {"environ", "getenv"}
            )
            or (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getenv"
            )
            or (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "os"
                and node.func.attr in {"environ", "getenv"}
            )
            for statement in function.body[:guard_index]
            for node in ast.walk(statement)
        )
        function_start = source.index(f"def {function_name}(")
        guard = source.index("reject_r008_formal_resume(", function_start)
        live_dispatch = source.find("launch_manifest_route(", function_start)
        if live_dispatch < 0:
            live_dispatch = source.find("try:", function_start)
        assert live_dispatch >= 0
        assert guard < live_dispatch

    adapter_source = launcher_paths[3].read_text(encoding="utf-8")
    adapter_function = adapter_source.index("def run_forever(")
    assert adapter_source.index("reject_r008_formal_resume(", adapter_function) < adapter_source.index(
        "adapted.open(", adapter_function
    )

    if allow_value is None:
        monkeypatch.delenv("R008_ALLOW_FORMAL_LIVE", raising=False)
    else:
        monkeypatch.setenv("R008_ALLOW_FORMAL_LIVE", allow_value)

    with pytest.raises(R008HistoricalLineageDoNotResumeError):
        from step5d_autotune_v4_r009.quarantine import reject_r008_formal_resume

        reject_r008_formal_resume("focused-test")

    from r008_rtde_seq_probe_inject import (  # noqa: E402
        R008ProbeBypassRetiredError,
        install_all,
    )

    with pytest.raises(R008ProbeBypassRetiredError):
        install_all()


def test_v3_current_metadata_is_byte_identical_to_checkpoint() -> None:
    expected_sha256 = "646edefaf6fbbd76b0cbfe86bc00b5d8ac26b787231f9ac345425256bbc78f5e"
    for relative in ("config/step5d/current.json",):
        path = ROOT / relative
        repo_relative = Path("experiments/tase-contact-reproduction") / relative
        checkpoint = subprocess.check_output(
            ["git", "show", f"HEAD:{repo_relative.as_posix()}"], cwd=ROOT.parent.parent
        )
        assert path.read_bytes() == checkpoint
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected_sha256
