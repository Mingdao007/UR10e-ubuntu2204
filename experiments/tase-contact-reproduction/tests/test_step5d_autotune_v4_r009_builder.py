from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

import build_step5d_autotune_v4_r009 as builder_module  # noqa: E402
from build_step5d_autotune_v4_r009 import (  # noqa: E402
    DEFAULT_CLOSURE_PATH,
    DEFAULT_CONTRACT_PATH,
    R009BuilderError,
    build,
    build_closure_document,
    build_release,
    load_closure,
    persist_release,
    validate_closure_document,
)
from step5d_autotune_v4_r009.contracts import (  # noqa: E402
    build_contract,
    load_contract,
    validate_contract,
)
from step5d_autotune_v4_r009.identity import (  # noqa: E402
    R009IdentityError,
    R009SourceSet,
    default_source_set,
    sha256_bytes,
    validate_behavior_manifest,
)
from step5d_autotune_v4_r009.identity import canonical_bytes  # noqa: E402
from step5d_autotune_v4_r009.ledger import (  # noqa: E402
    R009HistoricalLineageError,
    load_historical_r008_read_only,
)


BEHAVIOR_MANIFEST_PATH = ROOT / "config/step5d/autotune_v4_r009.behavior-manifest.json"


def _manifest():
    return validate_behavior_manifest(
        json.loads(BEHAVIOR_MANIFEST_PATH.read_text(encoding="utf-8"))
    )


def _snapshot(paths: list[Path]) -> dict[str, tuple[bytes, int]]:
    return {
        str(path): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in paths
    }


def test_pure_build_load_validate_preserves_canonical_contract_bytes_and_mtime() -> None:
    contract_path = DEFAULT_CONTRACT_PATH
    before = _snapshot([contract_path])[str(contract_path)]
    loaded = load_contract(contract_path)

    result = build_release(behavior_manifest=_manifest())
    assert result["contract_sha256"] == loaded.sha256
    assert validate_contract(loaded).sha256 == loaded.sha256
    assert load_contract(contract_path).sha256 == loaded.sha256

    after = _snapshot([contract_path])[str(contract_path)]
    assert after == before


def test_explicit_persist_is_byte_and_mtime_idempotent(tmp_path: Path) -> None:
    build_dir = tmp_path / "build"
    output_dir = tmp_path / "release/programs/step5/step5d"
    contract_path = tmp_path / "release/config/step5d/autotune_v4_r009.json"
    behavior_path = (
        tmp_path / "release/config/step5d/autotune_v4_r009.behavior-manifest.json"
    )
    closure_path = tmp_path / "release/config/step5d/autotune_v4_r009_offline_closure.json"
    identity_alias_path = tmp_path / "release/config/step5d/r009_release_identity.json"

    result = build(build_dir, behavior_manifest=_manifest())
    first = persist_release(
        result,
        output_dir=output_dir,
        contract_path=contract_path,
        behavior_manifest_path=behavior_path,
        closure_path=closure_path,
        identity_alias_path=identity_alias_path,
        release_root=tmp_path / "release",
    )
    identity_path = contract_path.with_name(
        "autotune_v4_r009.release-identity.json"
    )
    persisted_paths = [
        *(Path(path) for path in first["artifacts"].values()),
        contract_path,
        identity_path,
        behavior_path,
        identity_alias_path,
        closure_path,
    ]
    before = _snapshot(persisted_paths)

    time.sleep(0.02)
    second = persist_release(
        result,
        output_dir=output_dir,
        contract_path=contract_path,
        behavior_manifest_path=behavior_path,
        closure_path=closure_path,
        identity_alias_path=identity_alias_path,
        release_root=tmp_path / "release",
    )
    after = _snapshot(persisted_paths)

    assert second["campaign_fingerprint"] == first["campaign_fingerprint"]
    assert after == before


def test_closure_requires_exact_artifact_keys_and_triplet_identity(tmp_path: Path) -> None:
    release_root = tmp_path / "release"
    result = build(tmp_path / "build", behavior_manifest=_manifest())
    closure_path = release_root / "config/step5d/autotune_v4_r009_offline_closure.json"
    persist_release(
        result,
        output_dir=release_root / "programs/step5/step5d",
        contract_path=release_root / "config/step5d/autotune_v4_r009.json",
        behavior_manifest_path=release_root
        / "config/step5d/autotune_v4_r009.behavior-manifest.json",
        closure_path=closure_path,
        identity_alias_path=release_root / "config/step5d/r009_release_identity.json",
        release_root=release_root,
    )
    document = json.loads(closure_path.read_text(encoding="utf-8"))

    empty = dict(document)
    empty["artifacts"] = {}
    empty_basis = dict(empty)
    empty_basis.pop("content_address")
    empty["content_address"] = {
        **document["content_address"],
        "sha256": sha256_bytes(canonical_bytes(empty_basis)),
    }
    with pytest.raises(R009BuilderError, match="artifact key set"):
        validate_closure_document(empty, root=release_root)

    resealed = dict(document)
    artifacts = dict(resealed["artifacts"])
    script_row = dict(artifacts["script"])
    alternate = release_root / "legal-alternate.script"
    shutil.copyfile(
        release_root
        / "programs/step5/step5d/step5d_strict_rnn_autotune_v4_r009.txt",
        alternate,
    )
    script_row["path"] = alternate.relative_to(release_root).as_posix()
    script_row["sha256"] = sha256_bytes(alternate.read_bytes())
    artifacts["script"] = script_row
    resealed["artifacts"] = artifacts
    resealed_basis = dict(resealed)
    resealed_basis.pop("content_address")
    resealed["content_address"] = {
        **document["content_address"],
        "sha256": sha256_bytes(canonical_bytes(resealed_basis)),
    }
    with pytest.raises(R009BuilderError, match="bound to controller triplet"):
        validate_closure_document(resealed, root=release_root)


def test_persist_prevalidation_is_failure_atomic_for_triplet_targets(tmp_path: Path) -> None:
    release_root = tmp_path / "release"
    result = build(tmp_path / "build", behavior_manifest=_manifest())
    persist_release(
        result,
        output_dir=release_root / "programs/step5/step5d",
        contract_path=release_root / "config/step5d/autotune_v4_r009.json",
        behavior_manifest_path=release_root
        / "config/step5d/autotune_v4_r009.behavior-manifest.json",
        closure_path=release_root / "config/step5d/autotune_v4_r009_offline_closure.json",
        identity_alias_path=release_root / "config/step5d/r009_release_identity.json",
        release_root=release_root,
    )
    target_paths = [
        release_root / "programs/step5/step5d" / f"step5d_strict_rnn_autotune_v4_r009{suffix}"
        for suffix in (".script", ".txt", ".urp", ".numeric-sanity.json", ".deploy-manifest.json")
    ]
    before = _snapshot(target_paths)
    source_script = Path(result["artifacts"][".script"])
    source_script.write_bytes(source_script.read_bytes() + b"\n# tampered source\n")
    with pytest.raises(R009BuilderError):
        persist_release(
            result,
            output_dir=release_root / "programs/step5/step5d",
            contract_path=release_root / "config/step5d/autotune_v4_r009.json",
            behavior_manifest_path=release_root
            / "config/step5d/autotune_v4_r009.behavior-manifest.json",
            closure_path=release_root / "config/step5d/autotune_v4_r009_offline_closure.json",
            identity_alias_path=release_root / "config/step5d/r009_release_identity.json",
            release_root=release_root,
        )
    assert _snapshot(target_paths) == before


def test_persist_cross_file_replace_failure_rolls_back_all_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_root = tmp_path / "release"
    initial = build(tmp_path / "build-initial", behavior_manifest=_manifest())
    persist_release(
        initial,
        output_dir=release_root / "programs/step5/step5d",
        contract_path=release_root / "config/step5d/autotune_v4_r009.json",
        behavior_manifest_path=release_root
        / "config/step5d/autotune_v4_r009.behavior-manifest.json",
        closure_path=release_root / "config/step5d/autotune_v4_r009_offline_closure.json",
        identity_alias_path=release_root / "config/step5d/r009_release_identity.json",
        release_root=release_root,
    )
    result = build(
        tmp_path / "build-next",
        behavior_manifest=_manifest(),
        stamp="2026-08-08T1201HKT_STEP5D_AUTOTUNE_V4_R009",
    )
    persisted = [
        *(release_root / "programs/step5/step5d").glob(
            "step5d_strict_rnn_autotune_v4_r009.*"
        ),
        release_root / "config/step5d/autotune_v4_r009.json",
        release_root / "config/step5d/autotune_v4_r009.release-identity.json",
        release_root / "config/step5d/autotune_v4_r009.behavior-manifest.json",
        release_root / "config/step5d/r009_release_identity.json",
        release_root / "config/step5d/autotune_v4_r009_offline_closure.json",
    ]
    before = _snapshot(persisted)
    real_replace = builder_module.os.replace
    replace_count = 0

    def fail_on_second_replace(source: str | bytes, target: str | bytes) -> None:
        nonlocal replace_count
        replace_count += 1
        if replace_count == 2:
            raise OSError("injected cross-file commit failure")
        real_replace(source, target)

    monkeypatch.setattr(builder_module.os, "replace", fail_on_second_replace)
    with pytest.raises(OSError, match="injected cross-file commit failure"):
        persist_release(
            result,
            output_dir=release_root / "programs/step5/step5d",
            contract_path=release_root / "config/step5d/autotune_v4_r009.json",
            behavior_manifest_path=release_root
            / "config/step5d/autotune_v4_r009.behavior-manifest.json",
            closure_path=release_root
            / "config/step5d/autotune_v4_r009_offline_closure.json",
            identity_alias_path=release_root / "config/step5d/r009_release_identity.json",
            release_root=release_root,
        )
    assert _snapshot(persisted) == before
    assert not any(path.name.startswith(".") for path in release_root.rglob("*"))


def test_cli_without_persist_is_pure_and_does_not_write_canonical_triplet() -> None:
    canonical_triplet = sorted(
        (ROOT / "programs/step5/step5d").glob("step5d_strict_rnn_autotune_v4_r009.*")
    )
    before = _snapshot(canonical_triplet)
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(TOOLS), environment.get("PYTHONPATH", ""))
    ).rstrip(os.pathsep)
    command = subprocess.run(
        [sys.executable, str(ROOT / "tools/build_step5d_autotune_v4_r009.py")],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert command.returncode == 0, f"stdout={command.stdout}\nstderr={command.stderr}"
    assert _snapshot(canonical_triplet) == before


def test_canonical_closure_cold_read_binds_contract_triplet_ledger_and_observation() -> None:
    code = """
import json
from build_step5d_autotune_v4_r009 import DEFAULT_CLOSURE_PATH, load_closure

validated = load_closure()
document = json.loads(DEFAULT_CLOSURE_PATH.read_text(encoding="utf-8"))
identity = validated["release_identity"]
print(json.dumps({
    "campaign_fingerprint": validated["contract"].campaign_fingerprint,
    "contract_sha256": validated["contract"].sha256,
    "triplet": dict(identity.controller_triplet_sha256),
    "ledger_header_identity": document["ledger_header_identity"],
    "observation_binding": document["observation_binding"],
}, sort_keys=True))
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(TOOLS), environment.get("PYTHONPATH", ""))
    ).rstrip(os.pathsep)
    cold = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert cold.returncode == 0, (
        f"cold read rc={cold.returncode}\nstdout={cold.stdout}\nstderr={cold.stderr}"
    )
    observed = json.loads(cold.stdout.strip())
    loaded = load_contract(DEFAULT_CONTRACT_PATH)
    identity = loaded.release_identity
    assert observed["campaign_fingerprint"] == loaded.campaign_fingerprint
    assert observed["contract_sha256"] == loaded.sha256
    assert observed["triplet"] == dict(identity.controller_triplet_sha256)
    assert observed["ledger_header_identity"]["release_identity"] == identity.as_dict()
    assert (
        observed["ledger_header_identity"]["release_identity_sha256"]
        == identity.release_identity_sha256
    )
    assert (
        observed["ledger_header_identity"]["campaign_fingerprint"]
        == identity.campaign_fingerprint
    )
    assert observed["observation_binding"] == {
        "schema": "step5d.autotune-v4/r009-observation-release-binding-v1",
        "release_identity_sha256": identity.release_identity_sha256,
        "campaign_fingerprint": identity.campaign_fingerprint,
    }


def test_behavior_and_source_byte_drift_change_campaign_and_release_identity() -> None:
    manifest = _manifest()
    baseline = build_release(behavior_manifest=manifest)

    behavior_drift = dataclasses.replace(
        manifest,
        raw_codec=f"{manifest.raw_codec}_behavior_drift",
    )
    behavior_result = build_release(behavior_manifest=behavior_drift)
    assert behavior_result["campaign_fingerprint"] != baseline["campaign_fingerprint"]
    assert (
        behavior_result["release_identity_sha256"]
        != baseline["release_identity_sha256"]
    )
    assert behavior_result["contract_sha256"] != baseline["contract_sha256"]

    source_files = dict(manifest.source_set.files)
    first_path = next(iter(source_files))
    source_files[first_path] = sha256_bytes(b"R009 source byte drift")
    source_drift = dataclasses.replace(
        manifest,
        source_set=R009SourceSet(files=source_files),
    )
    source_result = build_release(behavior_manifest=source_drift)
    assert source_result["source_closure_sha256"] != baseline["source_closure_sha256"]
    assert source_result["campaign_fingerprint"] != baseline["campaign_fingerprint"]
    assert (
        source_result["release_identity_sha256"]
        != baseline["release_identity_sha256"]
    )


def test_default_source_set_covers_r009_execution_and_quarantine_surface() -> None:
    actual_behavior_paths = {
        "tools/run_step5d_autotune_campaign.py",
        "tools/step5d_machine_campaign_binding.py",
        "tools/launch_step5d_autotune_v4_r008_control.py",
        "tools/run_step5d_autotune_v4_r008.py",
        "tools/run_step5d_autotune_v4_r008_b3_two_stage.py",
    }
    source_set = default_source_set(ROOT)
    assert actual_behavior_paths <= set(source_set.files)
    with pytest.raises(R009IdentityError, match="historical R008 input"):
        R009SourceSet(files={"tools/step5d_autotune_v4_r008/unpinned.py": "a" * 64})


def test_every_pinned_behavior_digest_changes_manifest_campaign_and_release_identity() -> None:
    manifest = _manifest()
    triplet = {
        "script": "1" * 64,
        "txt": "2" * 64,
        "urp": "3" * 64,
    }
    baseline = build_contract(
        behavior_manifest=manifest,
        controller_triplet_sha256=triplet,
    )

    for relative in sorted(manifest.source_set.files):
        drifted_files = dict(manifest.source_set.files)
        drifted_files[relative] = sha256_bytes(
            f"R009 behavior-byte drift:{relative}".encode("utf-8")
        )
        drifted_manifest = dataclasses.replace(
            manifest,
            source_set=R009SourceSet(files=drifted_files),
        )
        drifted = build_contract(
            behavior_manifest=drifted_manifest,
            controller_triplet_sha256=triplet,
        )
        assert (
            drifted.behavior_manifest.behavior_manifest_sha256
            != baseline.behavior_manifest.behavior_manifest_sha256
        ), relative
        assert drifted.campaign_fingerprint != baseline.campaign_fingerprint, relative
        assert (
            drifted.release_identity_sha256 != baseline.release_identity_sha256
        ), relative


def test_historical_ledger_optimizer_and_resume_inputs_remain_rejected(
    tmp_path: Path,
) -> None:
    historical_path = tmp_path / "historical-r008-ledger.jsonl"
    historical_path.write_text(
        json.dumps(
            {
                "schema": "step5d.autotune-v4/r008-ledger-v1",
                "record_type": "r008_observation",
                "campaign_fingerprint": "r008-historical",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    historical = load_historical_r008_read_only(historical_path)
    assert len(historical.rows) == 1
    with pytest.raises(R009HistoricalLineageError, match="optimizer"):
        historical.optimizer_input()
    with pytest.raises(R009HistoricalLineageError, match="resume"):
        historical.resume_input()
