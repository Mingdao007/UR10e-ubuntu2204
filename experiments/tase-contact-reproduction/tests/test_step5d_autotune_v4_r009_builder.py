from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

from build_step5d_autotune_v4_r009 import (  # noqa: E402
    DEFAULT_CLOSURE_PATH,
    DEFAULT_CONTRACT_PATH,
    build,
    build_release,
    load_closure,
    persist_release,
)
from step5d_autotune_v4_r009.contracts import (  # noqa: E402
    load_contract,
    validate_contract,
)
from step5d_autotune_v4_r009.identity import (  # noqa: E402
    R009SourceSet,
    sha256_bytes,
    validate_behavior_manifest,
)
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
    )
    after = _snapshot(persisted_paths)

    assert second["campaign_fingerprint"] == first["campaign_fingerprint"]
    assert after == before


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
