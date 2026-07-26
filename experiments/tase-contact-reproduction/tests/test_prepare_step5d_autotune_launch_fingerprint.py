from __future__ import annotations

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


def test_prepare_keeps_explicit_campaign_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_manifest_sha256 = "a" * 64
    campaign_root = tmp_path / "campaign"
    request, initial_manifest = _bootstrap_prepare_request(
        tmp_path,
        campaign_root=campaign_root,
    )
    request = launch.LaunchPreparationRequest(
        **{**request.__dict__, "campaign_fingerprint": "b" * 64}
    )
    expected = launch._campaign_fingerprint(
        release_manifest_sha256=release_manifest_sha256,
        launch_profile_sha256=launch._sha256_path(request.launch_profile_path),
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

    request = launch.LaunchPreparationRequest(
        **{**request.__dict__, "campaign_fingerprint": expected}
    )
    result = launch.prepare(request)
    assert called
    assert called[0][1] == expected
    assert result["campaign_fingerprint"] == expected
    assert result["campaign_id"] == expected_campaign_id
    assert result["campaign_root"] == str(campaign_root.resolve())
    assert result["launch_profile_sha256"] == launch._sha256_path(request.launch_profile_path)


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
