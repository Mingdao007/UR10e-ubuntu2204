"""Lightweight Step5d campaign identity and epoch discovery."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping

from step5d_autotune_contract import CampaignSpec


def campaign_spec(
    root: Path,
    fingerprint: str,
    epoch: int,
    *,
    campaign_id: str | None = None,
) -> CampaignSpec:
    source = json.loads(
        (root / "config/step5d_autotune_campaign_v1.json").read_text(
            encoding="utf-8"
        )
    )
    baseline = source["baseline"]
    objective = source["objective"]
    return CampaignSpec(
        campaign_id=campaign_id or f"step5d-native-{epoch}",
        campaign_epoch=epoch,
        campaign_fingerprint=fingerprint,
        target_force_n=float(baseline["target_force_n"]),
        objective_window_start_s=float(objective["window_s"][0]),
        objective_window_end_s=float(objective["window_s"][1]),
        objective_bin_s=float(objective["bin_s"]),
        required_bins=int(objective["required_complete_bins"]),
        success_mae_n=float(objective["success_mae_n"]),
        confirmation_relative_delta_max=float(
            objective.get("confirmation_relative_delta_max", 0.15)
        ),
        f0_shadow_reaction_normal_base=tuple(
            float(value) for value in baseline["f0_shadow_reaction_normal_base"]
        ),
    )


def _campaign_from_payload(payload: Mapping[str, Any]) -> CampaignSpec:
    values = dict(payload)
    shadow = values.get("f0_shadow_reaction_normal_base")
    if shadow is not None:
        values["f0_shadow_reaction_normal_base"] = tuple(shadow)
    return CampaignSpec(**values)


@dataclass(frozen=True)
class CampaignEpochLayout:
    epoch: int
    root: Path
    store_root: Path
    journal_root: Path
    manifest: Mapping[str, Any]
    campaign: CampaignSpec


def discover_campaign_epochs(
    campaign_root: Path,
) -> tuple[CampaignEpochLayout, ...]:
    campaign_root = campaign_root.resolve()
    roots: list[Path] = []
    if (campaign_root / "store/campaign.json").is_file():
        roots.append(campaign_root)
    epochs_root = campaign_root / "epochs"
    if epochs_root.is_dir():
        for candidate in sorted(epochs_root.iterdir()):
            if candidate.is_symlink() or not candidate.is_dir():
                raise RuntimeError("campaign epochs directory contains an unsafe entry")
            if len(candidate.name) != 10 or not candidate.name.isdigit():
                raise RuntimeError("campaign epoch directory name must be ten digits")
            if (candidate / "store/campaign.json").is_file():
                roots.append(candidate.resolve())

    layouts: list[CampaignEpochLayout] = []
    seen_epochs: set[int] = set()
    campaign_id: str | None = None
    for epoch_root in roots:
        manifest_path = epoch_root / "store/campaign.json"
        if manifest_path.is_symlink():
            raise RuntimeError("campaign manifest must not be a symlink")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or not isinstance(
            manifest.get("campaign"), Mapping
        ):
            raise RuntimeError("campaign manifest lacks a campaign object")
        campaign = _campaign_from_payload(manifest["campaign"])
        if (
            epoch_root.parent == epochs_root
            and int(epoch_root.name) != campaign.campaign_epoch
        ):
            raise RuntimeError("campaign epoch directory disagrees with its manifest")
        if campaign.campaign_epoch in seen_epochs:
            raise RuntimeError("campaign epoch appears in more than one store")
        if campaign_id is not None and campaign.campaign_id != campaign_id:
            raise RuntimeError("campaign epoch chain crosses campaign_id")
        seen_epochs.add(campaign.campaign_epoch)
        campaign_id = campaign_id or campaign.campaign_id
        layouts.append(
            CampaignEpochLayout(
                epoch=campaign.campaign_epoch,
                root=epoch_root,
                store_root=epoch_root / "store",
                journal_root=epoch_root / "journal",
                manifest=manifest,
                campaign=campaign,
            )
        )
    return tuple(sorted(layouts, key=lambda row: row.epoch))
