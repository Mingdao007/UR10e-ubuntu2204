"""Canonical source contract for the two independent formal V4 campaigns."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Mapping, Sequence

from .trajectory import TRAJECTORY_FAMILIES


FORMAL_CAMPAIGN_SOURCE_SCHEMA_V1 = "ur10e_tacdiffusion_formal_campaign_source/v1"
FORMAL_CAMPAIGN_KINDS = ("fixed_k", "variable_k")
FORMAL_CAMPAIGN_PHASES = ("pilot", "formal_training")
FORMAL_CAMPAIGN_EPISODE_COUNT = 200
FORMAL_CAMPAIGN_PILOT_COUNT = 50
FORMAL_CAMPAIGN_SHADOW_COUNT = 2
FORMAL_CAMPAIGN_SHADOW_DURATION_S = 45.0
FORMAL_FIXED_TARGET_LOADS_N = (3.0, 5.0, 8.0)
FORMAL_TRAJECTORY_FAMILIES = tuple(TRAJECTORY_FAMILIES)
FORMAL_FIXED_CAMPAIGN_ID = "fixed_k_formal_v4"
FORMAL_VARIABLE_CAMPAIGN_ID = "variable_k_formal_v4"


def _finite_target_loads(values: Sequence[float], name: str) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if not result or not all(math.isfinite(value) and value > 0.0 for value in result):
        raise ValueError(f"{name} must contain positive finite loads")
    return result


@dataclass(frozen=True)
class FormalLiveShadowContractV1:
    shadow_id: str
    duration_s: float = FORMAL_CAMPAIGN_SHADOW_DURATION_S
    model_active: bool = False
    shadow_only: bool = True

    def __post_init__(self) -> None:
        if not str(self.shadow_id).strip():
            raise ValueError("formal live-shadow id is required")
        if not math.isfinite(self.duration_s) or not 40.0 <= self.duration_s <= 50.0:
            raise ValueError("formal live-shadow duration must be about 45 seconds")
        if self.model_active is not False or self.shadow_only is not True:
            raise ValueError("formal live shadows must remain inactive and shadow-only")

    def as_json(self) -> dict[str, object]:
        return {
            "shadow_id": self.shadow_id,
            "duration_s": self.duration_s,
            "model_active": self.model_active,
            "shadow_only": self.shadow_only,
        }


@dataclass(frozen=True)
class FormalCampaignEpisodeV1:
    episode_index: int
    phase: str
    trajectory_family: str
    target_load_n: float
    training_included: bool = True

    def __post_init__(self) -> None:
        if self.episode_index < 0:
            raise ValueError("formal episode index must be non-negative")
        if self.phase not in FORMAL_CAMPAIGN_PHASES:
            raise ValueError("formal episode phase is invalid")
        if self.trajectory_family not in FORMAL_TRAJECTORY_FAMILIES:
            raise ValueError("formal episode trajectory family is invalid")
        if not math.isfinite(self.target_load_n) or self.target_load_n <= 0.0:
            raise ValueError("formal episode target load must be positive and finite")
        if self.training_included is not True:
            raise ValueError("pilot episodes are included in formal training count")

    def as_json(self) -> dict[str, object]:
        return {
            "episode_index": self.episode_index,
            "phase": self.phase,
            "trajectory_family": self.trajectory_family,
            "target_load_n": self.target_load_n,
            "training_included": self.training_included,
        }


@dataclass(frozen=True)
class FormalQualificationReceiptV1:
    """Typed prerequisite receipt for starting the variable-K campaign."""

    fixed_campaign_id: str
    fixed_campaign_complete: bool
    k_load_qualified: bool
    source_sha256: str

    def __post_init__(self) -> None:
        if self.fixed_campaign_id != FORMAL_FIXED_CAMPAIGN_ID:
            raise ValueError("qualification receipt must bind the fixed-K campaign")
        if self.fixed_campaign_complete is not True or self.k_load_qualified is not True:
            raise ValueError("fixed-K completion and K/load qualification are required")
        if len(self.source_sha256) != 64 or any(char not in "0123456789abcdef" for char in self.source_sha256):
            raise ValueError("qualification source_sha256 must be lowercase SHA-256")

    def as_json(self) -> dict[str, object]:
        return {
            "schema_version": "ur10e_tacdiffusion_formal_qualification_receipt/v1",
            "fixed_campaign_id": self.fixed_campaign_id,
            "fixed_campaign_complete": self.fixed_campaign_complete,
            "k_load_qualified": self.k_load_qualified,
            "source_sha256": self.source_sha256,
        }


@dataclass(frozen=True)
class FormalCampaignContractV1:
    campaign_id: str
    kind: str
    total_episodes: int = FORMAL_CAMPAIGN_EPISODE_COUNT
    pilot_episodes: int = FORMAL_CAMPAIGN_PILOT_COUNT
    trajectory_families: tuple[str, ...] = FORMAL_TRAJECTORY_FAMILIES
    target_loads_n: tuple[float, ...] = FORMAL_FIXED_TARGET_LOADS_N
    training_includes_pilot: bool = True
    live_shadows: tuple[FormalLiveShadowContractV1, ...] = ()
    starts_after_campaign_id: str | None = None
    requires_fixed_k_qualification: bool = False
    model_active: bool = False
    shadow_only: bool = True

    def __post_init__(self) -> None:
        if not str(self.campaign_id).strip() or self.kind not in FORMAL_CAMPAIGN_KINDS:
            raise ValueError("formal campaign identity/kind is invalid")
        if self.total_episodes != FORMAL_CAMPAIGN_EPISODE_COUNT:
            raise ValueError("each formal campaign must contain exactly 200 episodes")
        if self.pilot_episodes != FORMAL_CAMPAIGN_PILOT_COUNT or self.pilot_episodes >= self.total_episodes:
            raise ValueError("each formal campaign must include the first 50 pilot episodes")
        if self.training_includes_pilot is not True:
            raise ValueError("the first 50 pilot episodes must be included in training")
        families = tuple(self.trajectory_families)
        if families != FORMAL_TRAJECTORY_FAMILIES or len(set(families)) != 7:
            raise ValueError("formal campaign must bind all seven trajectory families")
        loads = _finite_target_loads(self.target_loads_n, "target_loads_n")
        if self.kind == "fixed_k" and loads != FORMAL_FIXED_TARGET_LOADS_N:
            raise ValueError("fixed-K campaign targets must be exactly 3, 5, and 8 N")
        object.__setattr__(self, "trajectory_families", families)
        object.__setattr__(self, "target_loads_n", loads)
        if len(self.live_shadows) != FORMAL_CAMPAIGN_SHADOW_COUNT:
            raise ValueError("each formal campaign requires exactly two live shadows")
        shadow_ids = {shadow.shadow_id for shadow in self.live_shadows}
        if len(shadow_ids) != FORMAL_CAMPAIGN_SHADOW_COUNT:
            raise ValueError("formal live-shadow ids must be unique")
        if self.kind == "fixed_k":
            if self.starts_after_campaign_id is not None or self.requires_fixed_k_qualification is not False:
                raise ValueError("fixed-K campaign cannot depend on a prior campaign")
        else:
            if self.starts_after_campaign_id != FORMAL_FIXED_CAMPAIGN_ID:
                raise ValueError("variable-K campaign must start after fixed-K campaign")
            if self.requires_fixed_k_qualification is not True:
                raise ValueError("variable-K campaign must require fixed-K and K/load qualification")
        if self.model_active is not False or self.shadow_only is not True:
            raise ValueError("formal campaigns must exclude model-active operation")

    @property
    def phase_counts(self) -> dict[str, int]:
        return {
            "pilot": self.pilot_episodes,
            "formal_training": self.total_episodes - self.pilot_episodes,
        }

    def build_episode_plan(self) -> tuple[FormalCampaignEpisodeV1, ...]:
        plan = tuple(
            FormalCampaignEpisodeV1(
                episode_index=index,
                phase="pilot" if index < self.pilot_episodes else "formal_training",
                trajectory_family=self.trajectory_families[index % len(self.trajectory_families)],
                target_load_n=self.target_loads_n[index % len(self.target_loads_n)],
            )
            for index in range(self.total_episodes)
        )
        self.validate_episode_plan(plan)
        return plan

    def validate_episode_plan(self, plan: Sequence[FormalCampaignEpisodeV1]) -> None:
        records = tuple(plan)
        if len(records) != self.total_episodes:
            raise ValueError("formal campaign episode plan count mismatch")
        if tuple(record.episode_index for record in records) != tuple(range(self.total_episodes)):
            raise ValueError("formal campaign episode indices must be contiguous")
        if sum(record.phase == "pilot" for record in records) != self.pilot_episodes:
            raise ValueError("formal campaign pilot phase count mismatch")
        if any(record.phase != ("pilot" if record.episode_index < self.pilot_episodes else "formal_training") for record in records):
            raise ValueError("formal campaign phase boundary is not first-50 pilot inclusive")
        if {record.trajectory_family for record in records} != set(FORMAL_TRAJECTORY_FAMILIES):
            raise ValueError("formal campaign plan does not cover all seven trajectory families")
        if not set(record.target_load_n for record in records) >= set(self.target_loads_n):
            raise ValueError("formal campaign plan does not cover every target load")
        if not all(record.training_included for record in records):
            raise ValueError("formal campaign plan excludes a pilot/training row")

    def validate_dependency(self, qualification: FormalQualificationReceiptV1 | None = None) -> None:
        if self.kind != "variable_k":
            if qualification is not None:
                raise ValueError("fixed-K campaign cannot consume a variable-K qualification receipt")
            return
        if qualification is None:
            raise ValueError("variable-K campaign requires fixed-K and K/load qualification")
        if qualification.fixed_campaign_id != self.starts_after_campaign_id:
            raise ValueError("variable-K qualification campaign binding mismatch")

    def as_json(self) -> dict[str, object]:
        return {
            "campaign_id": self.campaign_id,
            "kind": self.kind,
            "total_episodes": self.total_episodes,
            "pilot_episodes": self.pilot_episodes,
            "trajectory_families": list(self.trajectory_families),
            "target_loads_n": list(self.target_loads_n),
            "training_includes_pilot": self.training_includes_pilot,
            "live_shadows": [shadow.as_json() for shadow in self.live_shadows],
            "starts_after_campaign_id": self.starts_after_campaign_id,
            "requires_fixed_k_qualification": self.requires_fixed_k_qualification,
            "model_active": self.model_active,
            "shadow_only": self.shadow_only,
        }


@dataclass(frozen=True)
class FormalCampaignSourceContractV1:
    campaigns: tuple[FormalCampaignContractV1, ...]
    schema_version: str = FORMAL_CAMPAIGN_SOURCE_SCHEMA_V1
    model_active: bool = False
    shadow_only: bool = True

    def __post_init__(self) -> None:
        if self.schema_version != FORMAL_CAMPAIGN_SOURCE_SCHEMA_V1:
            raise ValueError("unsupported formal campaign source schema")
        if tuple(campaign.campaign_id for campaign in self.campaigns) != (
            FORMAL_FIXED_CAMPAIGN_ID,
            FORMAL_VARIABLE_CAMPAIGN_ID,
        ):
            raise ValueError("formal source must contain fixed-K then variable-K campaigns")
        for campaign in self.campaigns:
            campaign.build_episode_plan()
        if self.model_active is not False or self.shadow_only is not True:
            raise ValueError("formal campaign source must exclude model-active operation")

    @property
    def fixed_campaign(self) -> FormalCampaignContractV1:
        return self.campaigns[0]

    @property
    def variable_campaign(self) -> FormalCampaignContractV1:
        return self.campaigns[1]

    def validate_variable_dependency(self, qualification: FormalQualificationReceiptV1 | None) -> None:
        self.variable_campaign.validate_dependency(qualification)

    def as_json(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "model_active": self.model_active,
            "shadow_only": self.shadow_only,
            "campaigns": [campaign.as_json() for campaign in self.campaigns],
        }


def build_formal_campaign_source_contract() -> FormalCampaignSourceContractV1:
    """Build the canonical source object used by the JSON loader tests."""

    def shadows(campaign_id: str) -> tuple[FormalLiveShadowContractV1, ...]:
        return tuple(
            FormalLiveShadowContractV1(f"{campaign_id}_shadow_{index}")
            for index in (1, 2)
        )

    return FormalCampaignSourceContractV1(
        campaigns=(
            FormalCampaignContractV1(
                campaign_id=FORMAL_FIXED_CAMPAIGN_ID,
                kind="fixed_k",
                live_shadows=shadows(FORMAL_FIXED_CAMPAIGN_ID),
            ),
            FormalCampaignContractV1(
                campaign_id=FORMAL_VARIABLE_CAMPAIGN_ID,
                kind="variable_k",
                live_shadows=shadows(FORMAL_VARIABLE_CAMPAIGN_ID),
                starts_after_campaign_id=FORMAL_FIXED_CAMPAIGN_ID,
                requires_fixed_k_qualification=True,
            ),
        )
    )


def load_formal_campaign_source_contract(path: str | Path) -> FormalCampaignSourceContractV1:
    source_path = Path(path)
    raw = json.loads(source_path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("formal campaign source must be a JSON object")
    if raw.get("schema_version") != FORMAL_CAMPAIGN_SOURCE_SCHEMA_V1:
        raise ValueError("unsupported formal campaign source schema")
    raw_campaigns = raw.get("campaigns")
    if not isinstance(raw_campaigns, Sequence) or isinstance(raw_campaigns, (str, bytes)):
        raise ValueError("formal campaign source campaigns are missing")
    campaigns: list[FormalCampaignContractV1] = []
    for raw_campaign in raw_campaigns:
        if not isinstance(raw_campaign, Mapping):
            raise ValueError("formal campaign entry must be an object")
        raw_shadows = raw_campaign.get("live_shadows")
        if not isinstance(raw_shadows, Sequence) or isinstance(raw_shadows, (str, bytes)):
            raise ValueError("formal campaign live_shadows are missing")
        shadows = tuple(FormalLiveShadowContractV1(**dict(item)) for item in raw_shadows)
        campaigns.append(
            FormalCampaignContractV1(
                campaign_id=str(raw_campaign.get("campaign_id", "")),
                kind=str(raw_campaign.get("kind", "")),
                total_episodes=int(raw_campaign.get("total_episodes", -1)),
                pilot_episodes=int(raw_campaign.get("pilot_episodes", -1)),
                trajectory_families=tuple(str(item) for item in raw_campaign.get("trajectory_families", ())),
                target_loads_n=tuple(float(item) for item in raw_campaign.get("target_loads_n", ())),
                training_includes_pilot=bool(raw_campaign.get("training_includes_pilot", False)),
                live_shadows=shadows,
                starts_after_campaign_id=(
                    None
                    if raw_campaign.get("starts_after_campaign_id") in (None, "")
                    else str(raw_campaign.get("starts_after_campaign_id"))
                ),
                requires_fixed_k_qualification=bool(raw_campaign.get("requires_fixed_k_qualification", False)),
                model_active=bool(raw_campaign.get("model_active", True)),
                shadow_only=bool(raw_campaign.get("shadow_only", False)),
            )
        )
    result = FormalCampaignSourceContractV1(
        campaigns=tuple(campaigns),
        schema_version=str(raw.get("schema_version", "")),
        model_active=bool(raw.get("model_active", True)),
        shadow_only=bool(raw.get("shadow_only", False)),
    )
    if result.as_json() != json.loads(json.dumps(raw, sort_keys=True, allow_nan=False)):
        raise ValueError("formal campaign source is not canonical")
    return result


__all__ = [
    "FORMAL_CAMPAIGN_EPISODE_COUNT",
    "FORMAL_CAMPAIGN_KINDS",
    "FORMAL_CAMPAIGN_PHASES",
    "FORMAL_CAMPAIGN_PILOT_COUNT",
    "FORMAL_CAMPAIGN_SHADOW_COUNT",
    "FORMAL_CAMPAIGN_SHADOW_DURATION_S",
    "FORMAL_CAMPAIGN_SOURCE_SCHEMA_V1",
    "FORMAL_FIXED_CAMPAIGN_ID",
    "FORMAL_FIXED_TARGET_LOADS_N",
    "FORMAL_TRAJECTORY_FAMILIES",
    "FORMAL_VARIABLE_CAMPAIGN_ID",
    "FormalCampaignContractV1",
    "FormalCampaignEpisodeV1",
    "FormalCampaignSourceContractV1",
    "FormalLiveShadowContractV1",
    "FormalQualificationReceiptV1",
    "build_formal_campaign_source_contract",
    "load_formal_campaign_source_contract",
]
