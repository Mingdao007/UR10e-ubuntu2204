from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
import sys

from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import prepare_step5d_autotune_launch as launch  # noqa: E402


def _bootstrap_prepare_request(
    tmp_path: Path,
    *,
    campaign_fingerprint: str | None = None,
    campaign_root: Path | None = None,
) -> tuple[launch.LaunchPreparationRequest, Path]:
    campaign_root = campaign_root or (tmp_path / "campaign")
    launch_profile = tmp_path / "launch-profile.json"
    initial_manifest = tmp_path / "parameter_receiver_initial.json"
    launch_profile.write_text('{"schema":"unit-profile"}\n', encoding="utf-8")
    initial_manifest.write_text('{"schema":"unit-initial"}\n', encoding="utf-8")
    request = launch.LaunchPreparationRequest(
        experiment_root=tmp_path,
        campaign_root=campaign_root,
        binding_file=tmp_path / "campaign-binding.json",
        binding_source="fingerprint-regression",
        launch_profile_path=launch_profile,
        candidate_batch_size=5,
        rolling_plan=False,
        campaign_fingerprint=campaign_fingerprint,
    )
    return request, initial_manifest


def test_prepare_prefers_explicit_launch_basis_campaign_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launch_profile_sha256 = "abc972b6edb67d3df1b433825349ffc46bd523d1fff29fc8395e7acf603dc2ea"
    release_manifest_sha256 = "b141f66cd32ecf7d2d79f677f95e08e778a93cad73633de2c1624f7c5b902297"
    explicit_fingerprint = "222820891d0c1291f68f543a93d5f3de0bd271cb035b1c246666d69e04aab9db"
    campaign_root = tmp_path / "campaign"
    request, initial_manifest = _bootstrap_prepare_request(
        tmp_path,
        campaign_root=campaign_root,
    )
    monkeypatch.setattr(
        launch,
        "load_current_release",
        lambda _root: SimpleNamespace(manifest_sha256=release_manifest_sha256),
    )
    monkeypatch.setattr(
        launch,
        "release_payload_path",
        lambda *_args, **_kwargs: initial_manifest,
    )
    monkeypatch.setattr(launch, "discover_campaign_epochs", lambda _root: tuple())
    called: list[Any] = []
    expected_campaign_id = "step5d-native-1"

    def _fake_campaign_spec(
        root: Path,
        fingerprint: str,
        epoch: int,
        *,
        campaign_id: str | None = None,
    ) -> Any:
        called.append((str(root), fingerprint, epoch, campaign_id))
        return SimpleNamespace(
            campaign_id=expected_campaign_id,
            campaign_epoch=epoch,
            campaign_fingerprint=fingerprint,
        )

    monkeypatch.setattr(launch, "campaign_spec", _fake_campaign_spec)
    monkeypatch.setattr(
        launch,
        "_sha256_path",
        lambda *_args, **_kwargs: launch_profile_sha256,
    )

    request = launch.LaunchPreparationRequest(
        **{**request.__dict__, "campaign_fingerprint": explicit_fingerprint}
    )
    result = launch.prepare(request)
    assert called
    assert called[0][1] == explicit_fingerprint
    assert result["campaign_fingerprint"] == explicit_fingerprint
    assert result["campaign_id"] == expected_campaign_id
    assert result["campaign_root"] == str(campaign_root.resolve())
    assert result["launch_profile_sha256"] == launch_profile_sha256


def test_prepare_reuses_existing_receiver_campaign_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, initial_manifest = _bootstrap_prepare_request(tmp_path)
    request = launch.LaunchPreparationRequest(
        **{**request.__dict__, "campaign_fingerprint": "a" * 64}
    )
    release_manifest_sha256 = "b141f66cd32ecf7d2d79f677f95e08e778a93cad73633de2c1624f7c5b902297"
    handoff_campaign_id = "step5d_no_tube_handoff_20260728"
    receiver_root = request.campaign_root / "control" / "parameter_receiver"
    receiver_root.mkdir(parents=True)
    (receiver_root / "state.json").write_text(
        json.dumps(
            {
                "schema": "step5d.parameter-receiver/state-v2",
                "protocol": "v3_full_home_parameter_receiver_v1",
                "campaign_id": handoff_campaign_id,
                "revision": 0,
                "dispatch_sequence": 0,
                "home_identity": None,
                "inflight": None,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        launch,
        "load_current_release",
        lambda _root: SimpleNamespace(manifest_sha256=release_manifest_sha256),
    )
    monkeypatch.setattr(
        launch,
        "release_payload_path",
        lambda *_args, **_kwargs: initial_manifest,
    )
    monkeypatch.setattr(launch, "discover_campaign_epochs", lambda _root: tuple())
    monkeypatch.setattr(
        launch,
        "_sha256_path",
        lambda *_args, **_kwargs: "a" * 64,
    )

    def _campaign_spec(
        root: Path,
        fingerprint: str,
        epoch: int,
        *,
        campaign_id: str | None = None,
    ) -> Any:
        assert campaign_id == handoff_campaign_id
        return SimpleNamespace(
            campaign_id=campaign_id,
            campaign_epoch=epoch,
            campaign_fingerprint=fingerprint,
        )

    monkeypatch.setattr(launch, "campaign_spec", _campaign_spec)
    result = launch.prepare(request)

    assert result["campaign_id"] == handoff_campaign_id
    plan = json.loads(Path(result["candidate_plan"]).read_text(encoding="utf-8"))
    assert plan["campaign_id"] == handoff_campaign_id


def test_prepare_accepts_and_preserves_explicit_launch_basis_counterexample() -> None:
    explicit_fingerprint = "222820891d0c1291f68f543a93d5f3de0bd271cb035b1c246666d69e04aab9db"
    assert (
        explicit_fingerprint
        == "222820891d0c1291f68f543a93d5f3de0bd271cb035b1c246666d69e04aab9db"
    )
    assert (
        launch._validate_campaign_fingerprint(
            requested_fingerprint=explicit_fingerprint,
        )
        == explicit_fingerprint
    )


def test_prepare_no_legacy_campaign_fingerprint_in_source() -> None:
    source = Path(launch.__file__).read_text(encoding="utf-8")
    ast_tree = ast.parse(source)
    for node in ast.walk(ast_tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_campaign_fingerprint":
            raise AssertionError("legacy _campaign_fingerprint should not be defined")
        if isinstance(node, ast.Name) and node.id == "_campaign_fingerprint":
            raise AssertionError("legacy _campaign_fingerprint should not be referenced")


def test_prepare_rejects_malformed_explicit_campaign_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, initial_manifest = _bootstrap_prepare_request(
        tmp_path,
        campaign_fingerprint="bad-fingerprint",
    )
    monkeypatch.setattr(
        launch,
        "load_current_release",
        lambda _root: SimpleNamespace(manifest_sha256="a" * 64),
    )
    monkeypatch.setattr(
        launch,
        "release_payload_path",
        lambda *_args, **_kwargs: initial_manifest,
    )
    monkeypatch.setattr(launch, "discover_campaign_epochs", lambda _root: tuple())
    monkeypatch.setattr(
        launch,
        "campaign_spec",
        lambda *args, **kwargs: pytest.fail(
            "malformed campaign fingerprint should fail before campaign_spec"
        ),
    )
    with pytest.raises(RuntimeError, match="campaign_fingerprint.*malformed"):
        launch.prepare(request)


def test_prepare_rejects_missing_launch_basis_campaign_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, initial_manifest = _bootstrap_prepare_request(
        tmp_path,
        campaign_fingerprint=None,
    )
    monkeypatch.setattr(
        launch,
        "load_current_release",
        lambda _root: SimpleNamespace(manifest_sha256="a" * 64),
    )
    monkeypatch.setattr(
        launch,
        "release_payload_path",
        lambda *_args, **_kwargs: initial_manifest,
    )
    monkeypatch.setattr(launch, "discover_campaign_epochs", lambda _root: tuple())
    monkeypatch.setattr(
        launch,
        "campaign_spec",
        lambda *args, **kwargs: pytest.fail(
            "missing campaign fingerprint should fail before campaign_spec"
        ),
    )
    with pytest.raises(RuntimeError, match="campaign_fingerprint is required"):
        launch.prepare(request)
