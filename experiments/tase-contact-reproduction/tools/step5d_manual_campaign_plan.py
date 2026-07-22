"""Deterministic 18-group Manual V2 campaign and Top-2 repeats."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
from typing import Iterable, Mapping

from step5d_manual_queue import enqueue, load_queue


GRID_SOURCE_PREFIX = "governed_grid"
REPEAT_SOURCE_PREFIX = "governed_top2_repeat"
P_VALUES = (0.001, 0.0008408964152537145, 0.001189207115002721)
I_VALUES = (0.00001, 0.0001)
DAMPING_VALUES = (7.0, 14.0, 3.5)
ORIENTATION_KO = 0.4
GUARD_OVERLAY = {
    "execution_profile_id": "nf100-slew050-a050",
    "step5d_preload_filtered_min_n": 7.5,
    "step5d_preload_filtered_max_n": 14.0,
    "step5d_preload_raw_min_n": 7.0,
    "step5d_preload_raw_max_n": 15.0,
    "step5d_preload_force_norm_max_n": 25.0,
    "step5d_preload_hold_s": 0.1,
    "step5d_preload_timeout_s": 10.0,
}


class ManualCampaignPlanError(RuntimeError):
    pass


@dataclass(frozen=True)
class CandidateGroup:
    index: int
    force_p_gain: float
    force_i_gain: float
    force_damping: float

    @property
    def group_id(self) -> str:
        return f"G{self.index:02d}"

    @property
    def grid_source(self) -> str:
        return f"{GRID_SOURCE_PREFIX}:{self.group_id}"


@dataclass(frozen=True)
class TrialScore:
    group_id: str
    eligible: bool
    objective_mae_n: float | None
    gross_peak_force_norm_n: float | None
    qdot_saturation_duty: float | None


def _groups() -> tuple[CandidateGroup, ...]:
    rows: list[CandidateGroup] = []
    for damping in DAMPING_VALUES:
        for force_p in P_VALUES:
            for force_i in I_VALUES:
                rows.append(
                    CandidateGroup(
                        index=len(rows) + 1,
                        force_p_gain=force_p,
                        force_i_gain=force_i,
                        force_damping=damping,
                    )
                )
    return tuple(rows)


INITIAL_GROUPS = _groups()
GROUP_BY_ID = {group.group_id: group for group in INITIAL_GROUPS}


def _nonce(campaign_id: str, release_sha: str, source: str) -> str:
    return hashlib.sha256(
        f"step5d-manual-v2\0{campaign_id}\0{release_sha}\0{source}".encode("ascii")
    ).hexdigest()[:32]


def _matches(request: Mapping[str, object], group: CandidateGroup, source: str) -> bool:
    overlay = request.get("overlay")
    if not isinstance(overlay, Mapping) or request.get("source") != source:
        return False
    expected = {
        "force_p_gain": group.force_p_gain,
        "force_i_gain": group.force_i_gain,
        "force_damping": group.force_damping,
        "orientation_ko": ORIENTATION_KO,
        **GUARD_OVERLAY,
    }
    return all(overlay.get(key) == value for key, value in expected.items())


def seed_initial_grid(
    queue_path: Path,
    *,
    campaign_id: str,
    release_manifest_sha256: str,
    launch_profile_path: Path,
) -> tuple[dict, ...]:
    existing = load_queue(queue_path)["requests"] if queue_path.exists() else []
    if len(existing) > len(INITIAL_GROUPS):
        existing = existing[: len(INITIAL_GROUPS)]
    for offset, request in enumerate(existing):
        group = INITIAL_GROUPS[offset]
        if not _matches(request, group, group.grid_source):
            raise ManualCampaignPlanError(
                f"manual initial grid differs at {group.group_id}"
            )
    for group in INITIAL_GROUPS[len(existing) :]:
        enqueue(
            queue_path,
            campaign_id=campaign_id,
            release_manifest_sha256=release_manifest_sha256,
            launch_profile_path=launch_profile_path,
            force_p=group.force_p_gain,
            force_i=group.force_i_gain,
            force_damping=group.force_damping,
            orientation_ko=ORIENTATION_KO,
            source=group.grid_source,
            occurrence_nonce=_nonce(
                campaign_id, release_manifest_sha256, group.grid_source
            ),
        )
    return tuple(load_queue(queue_path)["requests"][: len(INITIAL_GROUPS)])


def rank_top_groups(scores: Iterable[TrialScore]) -> tuple[CandidateGroup, ...]:
    eligible: list[tuple[tuple[float, float, float, int], CandidateGroup]] = []
    seen: set[str] = set()
    for score in scores:
        if score.group_id in seen:
            raise ManualCampaignPlanError(f"duplicate score for {score.group_id}")
        seen.add(score.group_id)
        group = GROUP_BY_ID.get(score.group_id)
        values = (
            score.objective_mae_n,
            score.gross_peak_force_norm_n,
            score.qdot_saturation_duty,
        )
        if not score.eligible:
            continue
        if group is None or any(
            value is None or not math.isfinite(value) or value < 0.0
            for value in values
        ):
            raise ManualCampaignPlanError(
                f"eligible score is incomplete for {score.group_id}"
            )
        key = (float(values[0]), float(values[1]), float(values[2]), group.index)
        eligible.append((key, group))
    eligible.sort(key=lambda item: item[0])
    return tuple(group for _, group in eligible[:2])


def append_top2_repeats(
    queue_path: Path,
    *,
    campaign_id: str,
    release_manifest_sha256: str,
    launch_profile_path: Path,
    scores: Iterable[TrialScore],
) -> tuple[dict, ...]:
    queue = load_queue(queue_path)
    requests = queue["requests"]
    if len(requests) < len(INITIAL_GROUPS):
        raise ManualCampaignPlanError("Top-2 repeats require all 18 initial groups")
    ranked = rank_top_groups(scores)
    expected = [
        (group, repeat, f"{REPEAT_SOURCE_PREFIX}:{group.group_id}:R{repeat}")
        for group in ranked
        for repeat in (1, 2)
    ]
    existing = requests[len(INITIAL_GROUPS) :]
    if len(existing) > len(expected):
        raise ManualCampaignPlanError("manual queue contains unexpected repeat requests")
    for request, (group, _, source) in zip(existing, expected):
        if not _matches(request, group, source):
            raise ManualCampaignPlanError("manual Top-2 repeat queue differs")
    for group, _, source in expected[len(existing) :]:
        enqueue(
            queue_path,
            campaign_id=campaign_id,
            release_manifest_sha256=release_manifest_sha256,
            launch_profile_path=launch_profile_path,
            force_p=group.force_p_gain,
            force_i=group.force_i_gain,
            force_damping=group.force_damping,
            orientation_ko=ORIENTATION_KO,
            source=source,
            occurrence_nonce=_nonce(campaign_id, release_manifest_sha256, source),
        )
    return tuple(load_queue(queue_path)["requests"][len(INITIAL_GROUPS) :])


__all__ = [
    "GUARD_OVERLAY",
    "INITIAL_GROUPS",
    "CandidateGroup",
    "ManualCampaignPlanError",
    "TrialScore",
    "append_top2_repeats",
    "rank_top_groups",
    "seed_initial_grid",
]
