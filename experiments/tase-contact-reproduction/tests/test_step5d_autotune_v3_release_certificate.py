from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from step5d_autotune_v3.release_certificate import (
    REFERENCE_SCHEMA,
    ReleaseCertificateError,
    certificate_path,
    load_release_certificate,
    release_certificate_scope,
    scope_sha256,
    write_release_certificate,
)


def digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def scope(**overrides: object) -> dict[str, object]:
    values = {
        "release_manifest_sha256": digest("release"),
        "source_fingerprint": digest("source"),
        "source_files_fingerprint": digest("source-files"),
        "launcher_sha256": digest("launcher"),
        "control_environment_sha256": digest("control-environment"),
        "process_tree_fingerprint": digest("safety-process-tree"),
        "runtime_epoch": digest("runtime-epoch"),
        "endpoint_content_sha256": digest("endpoint-content"),
        "qualification_profile": "formal_transition_v1",
        "claim_class": "state_machine_contract",
        "optimizer_exercised": False,
    }
    values.update(overrides)
    return release_certificate_scope(**values)


def qualification_evidence(root: Path) -> tuple[Path, str]:
    path = root / "qualification/evidence/result/qualification.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"ok":true}\n', encoding="utf-8")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_certificate_is_scope_addressed_and_round_trips(tmp_path: Path) -> None:
    evidence, evidence_sha256 = qualification_evidence(tmp_path)
    expected_scope = scope()

    payload, reference = write_release_certificate(
        tmp_path,
        scope=expected_scope,
        qualification_evidence_path=evidence,
        qualification_evidence_sha256=evidence_sha256,
        completed_at_unix_ns=123,
    )

    expected_path = certificate_path(tmp_path, expected_scope)
    assert expected_path == (
        tmp_path.resolve()
        / "release-certificates"
        / expected_scope["release_manifest_sha256"]
        / scope_sha256(expected_scope)
        / "certificate.json"
    )
    assert reference["schema"] == REFERENCE_SCHEMA
    assert Path(reference["path"]) == expected_path
    observed, observed_evidence, observed_qualification = load_release_certificate(
        tmp_path,
        expected_path,
        expected_scope=expected_scope,
    )
    assert observed == payload
    assert observed_evidence == evidence
    assert observed_qualification == {"ok": True}


def test_exact_certificate_rewrite_is_idempotent_but_conflict_is_refused(
    tmp_path: Path,
) -> None:
    evidence, evidence_sha256 = qualification_evidence(tmp_path)
    expected_scope = scope()
    arguments = {
        "scope": expected_scope,
        "qualification_evidence_path": evidence,
        "qualification_evidence_sha256": evidence_sha256,
        "completed_at_unix_ns": 123,
    }

    first = write_release_certificate(tmp_path, **arguments)
    second = write_release_certificate(tmp_path, **arguments)
    assert second == first

    with pytest.raises(
        ReleaseCertificateError,
        match="immutable release certificate bytes differ",
    ):
        write_release_certificate(
            tmp_path,
            **{**arguments, "completed_at_unix_ns": 124},
        )


def test_certificate_rejects_scope_drift_and_evidence_tampering(
    tmp_path: Path,
) -> None:
    evidence, evidence_sha256 = qualification_evidence(tmp_path)
    expected_scope = scope()
    write_release_certificate(
        tmp_path,
        scope=expected_scope,
        qualification_evidence_path=evidence,
        qualification_evidence_sha256=evidence_sha256,
        completed_at_unix_ns=123,
    )
    path = certificate_path(tmp_path, expected_scope)

    with pytest.raises(ReleaseCertificateError, match="scope differs"):
        load_release_certificate(
            tmp_path,
            path,
            expected_scope=scope(launcher_sha256=digest("changed-launcher")),
        )

    evidence.write_text('{"ok":false}\n', encoding="utf-8")
    with pytest.raises(
        ReleaseCertificateError,
        match="qualification evidence SHA-256 differs",
    ):
        load_release_certificate(
            tmp_path,
            path,
            expected_scope=expected_scope,
        )


def test_v1_certificate_is_not_accepted_as_v2(tmp_path: Path) -> None:
    expected_scope = scope()
    path = certificate_path(tmp_path, expected_scope)
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"schema":"step5d.autotune-v3/release-certificate-v1"}\n',
        encoding="utf-8",
    )

    with pytest.raises(ReleaseCertificateError, match="fields differ"):
        load_release_certificate(
            tmp_path,
            path,
            expected_scope=expected_scope,
        )
