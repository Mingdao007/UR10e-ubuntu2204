from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

import run_step5d_release_contract as command
from step5d_autotune_v3 import release_contract
from step5d_autotune_v3.release_certificate import (
    ReleaseCertificateError,
    load_release_certificate,
)


ROOT = Path(__file__).resolve().parents[1]


def digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def scope(subject_kind: str = "autotune_v3") -> dict[str, object]:
    return {
        "schema": "step5d.autotune-v3/release-contract-scope-v1",
        "subject_kind": subject_kind,
        "release_manifest_sha256": digest(f"{subject_kind}-release"),
        "source_fingerprint": digest("source"),
        "source_files_fingerprint": digest("source-files"),
        "launcher_sha256": digest("launcher"),
        "control_environment_sha256": digest("environment"),
        "runtime_epoch": digest("runtime"),
        "contract_profile": release_contract.PROFILE,
    }


def _environment() -> dict[str, str]:
    return {
        release_contract.CANONICAL_LAUNCH_ENV: str(
            ROOT / "scripts/step5d-autotune-v3.sh"
        )
    }


def test_command_reports_exact_candidate_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    candidate = tmp_path / "candidate.json"
    candidate.write_text("{}\n", encoding="utf-8")
    identity = SimpleNamespace(manifest_sha256=digest("autotune_v3-release"))
    monkeypatch.setattr(
        command,
        "load_local_release_candidate",
        lambda *_args: (identity, {}),
    )
    monkeypatch.setattr(
        command,
        "run_release_contract_check",
        lambda *_args, **_kwargs: (
            {
                "state": release_contract.PROVEN,
                "scope": {
                    "subject_kind": "autotune_v3",
                    "release_manifest_sha256": identity.manifest_sha256,
                },
            },
            {"path": str(tmp_path / "certificate.json")},
        ),
    )

    assert command.main(
        [
            "--experiment-root",
            str(ROOT),
            "--output-root",
            str(tmp_path),
            "--release-candidate",
            str(candidate),
        ]
    ) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["release_candidate"] == str(candidate.resolve())


def test_contract_is_pure_fast_and_cacheable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_scope = scope()
    identity = SimpleNamespace(manifest_sha256=expected_scope["release_manifest_sha256"])
    monkeypatch.setattr(release_contract, "_v3_release", lambda *_args: identity)
    monkeypatch.setattr(
        release_contract,
        "release_contract_scope_for_release",
        lambda *_args, **_kwargs: expected_scope,
    )

    started = time.monotonic()
    payload, certificate = release_contract.run_release_contract_check(
        ROOT,
        tmp_path,
        environment=_environment(),
    )
    cold_elapsed = time.monotonic() - started
    cached_payload, cached_certificate = release_contract.run_release_contract_check(
        ROOT,
        tmp_path,
        environment=_environment(),
        reuse_only=True,
    )

    assert cold_elapsed <= release_contract.COLD_BUDGET_S
    assert payload["state"] == release_contract.PROVEN
    assert payload["timing"]["core_transition_s"] <= release_contract.CORE_BUDGET_S
    assert payload["invariants"] == {
        "physical_trial": False,
        "network_started": False,
        "subprocess_started": False,
        "wall_clock_trial_wait": False,
    }
    assert cached_payload == payload
    assert cached_certificate == certificate


def test_contract_requires_canonical_shell_and_has_no_simulator_modules(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        release_contract.ReleaseContractError,
        match="worker is internal",
    ):
        release_contract.require_canonical_launcher(ROOT, {})

    source = (ROOT / "tools/step5d_autotune_v3/release_contract.py").read_text(
        encoding="utf-8"
    )
    assert "import subprocess" not in source
    assert "import socket" not in source
    assert "time.sleep" not in source
    assert not (ROOT / "tools/step5d_autotune_v3/qualification.py").exists()
    assert not (ROOT / "tools/step5d_autotune_v3/qualification_endpoints.py").exists()
    assert not (ROOT / "tools/step5d_manual_qualification.py").exists()
    assert not (ROOT / "tools/run_step5d_autotune_v3_qualification.py").exists()


def test_contract_certificate_detects_evidence_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_scope = scope("manual_v2")
    monkeypatch.setattr(
        release_contract,
        "manual_release_contract_scope",
        lambda *_args, **_kwargs: expected_scope,
    )
    _payload, certificate = release_contract.run_release_contract_check(
        ROOT,
        tmp_path,
        environment=_environment(),
        subject_kind="manual_v2",
    )
    _certificate, evidence_path, _evidence = load_release_certificate(
        tmp_path,
        Path(certificate["path"]),
        expected_scope=expected_scope,
    )
    evidence_path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(
        ReleaseCertificateError,
        match="contract evidence SHA-256 differs",
    ):
        load_release_certificate(
            tmp_path,
            Path(certificate["path"]),
            expected_scope=expected_scope,
        )
