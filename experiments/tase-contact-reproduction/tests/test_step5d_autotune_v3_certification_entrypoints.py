from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import finalize_step5d_autotune_v3_certification as finalizer
import issue_step5d_autotune_v3_certification_authorization as issuer


def test_issuer_requires_explicit_ref_and_writes_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = SimpleNamespace(
        release_basis_fingerprint="1" * 64,
        deployment_fingerprint="2" * 64,
        plant_epoch=7,
        deployment_readback_sha256="3" * 64,
    )
    monkeypatch.setattr(issuer, "load_bridge_start_context", lambda *args, **kwargs: context)
    output = (tmp_path / "authorization.json").resolve()
    authorization = issuer.issue(
        bridge_start_context_path=(tmp_path / "bridge.json").resolve(),
        authorization_ref="bench-owner:20260720:001",
        validity_minutes=15,
        output_path=output,
        now=datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc),
    )
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document == authorization.document()
    assert document["campaign_allowed"] is False
    assert document["optimizer_eligible"] is False
    assert document["authorization_source"] == (
        "explicit_user_ref:bench-owner:20260720:001"
    )
    with pytest.raises(issuer.CertificationIssueError, match="must be fresh"):
        issuer.issue(
            bridge_start_context_path=(tmp_path / "bridge.json").resolve(),
            authorization_ref="bench-owner:20260720:001",
            validity_minutes=15,
            output_path=output,
        )


@pytest.mark.parametrize("reference", ["", "short", "has space", "x" * 129])
def test_issuer_rejects_ambiguous_reference(
    tmp_path: Path, reference: str
) -> None:
    with pytest.raises(issuer.CertificationIssueError, match="authorization ref"):
        issuer.issue(
            bridge_start_context_path=(tmp_path / "bridge.json").resolve(),
            authorization_ref=reference,
            validity_minutes=15,
            output_path=(tmp_path / "authorization.json").resolve(),
        )


def test_finalizer_orders_extract_then_both_promotions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []
    monkeypatch.setattr(
        finalizer.extractor,
        "extract",
        lambda **kwargs: ({"kind": "stopping"}, {"kind": "return"}),
    )

    def write_once(path, payload):
        calls.append(("write", path.name, payload["kind"]))
        path.write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(finalizer.extractor, "_write_once", write_once)
    monkeypatch.setattr(
        finalizer.stop_promotion,
        "promote",
        lambda **kwargs: calls.append(("promote", "stopping")),
    )
    monkeypatch.setattr(
        finalizer.return_promotion,
        "promote",
        lambda **kwargs: calls.append(("promote", "return")),
    )
    paths = {
        name: (tmp_path / name).resolve()
        for name in (
            "capture",
            "bridge",
            "authorization",
            "readback",
            "ursim",
            "stopping-measurement",
            "return-telemetry",
            "stopping-artifact",
            "return-artifact",
        )
    }
    finalizer.finalize(
        capture_path=paths["capture"],
        bridge_start_context_path=paths["bridge"],
        authorization_path=paths["authorization"],
        deployment_readback_path=paths["readback"],
        ursim_trace_path=paths["ursim"],
        stopping_measurement_path=paths["stopping-measurement"],
        return_telemetry_path=paths["return-telemetry"],
        stopping_artifact_path=paths["stopping-artifact"],
        return_artifact_path=paths["return-artifact"],
    )
    assert calls == [
        ("write", "stopping-measurement", "stopping"),
        ("write", "return-telemetry", "return"),
        ("promote", "stopping"),
        ("promote", "return"),
    ]
