from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_manual_campaign_plan import (  # noqa: E402
    GUARD_OVERLAY,
    INITIAL_GROUPS,
    TrialScore,
    append_top2_repeats,
    rank_top_groups,
    seed_initial_grid,
)
from step5d_manual_queue import load_queue  # noqa: E402


PROFILE = ROOT / "config/step5d/manual/launch_profile.json"
RELEASE = "a" * 64


def _score(
    group_id: str,
    objective: float,
    peak: float,
    qdot: float,
    *,
    eligible: bool = True,
) -> TrialScore:
    return TrialScore(group_id, eligible, objective, peak, qdot)


def test_initial_grid_exact_order_guards_and_idempotence(tmp_path: Path) -> None:
    queue = tmp_path / "manual_queue.json"
    requests = seed_initial_grid(
        queue,
        campaign_id="manual-grid",
        release_manifest_sha256=RELEASE,
        launch_profile_path=PROFILE,
    )
    assert len(requests) == 18
    assert [
        (
            row["overlay"]["force_p_gain"],
            row["overlay"]["force_i_gain"],
            row["overlay"]["force_damping"],
        )
        for row in requests
    ] == [
        (group.force_p_gain, group.force_i_gain, group.force_damping)
        for group in INITIAL_GROUPS
    ]
    for index, row in enumerate(requests, start=1):
        assert row["source"] == f"governed_grid:G{index:02d}"
        assert row["row_index"] == 1
        assert row["overlay"]["orientation_ko"] == 0.4
        for key, value in GUARD_OVERLAY.items():
            assert row["overlay"][key] == value
    seed_initial_grid(
        queue,
        campaign_id="manual-grid",
        release_manifest_sha256=RELEASE,
        launch_profile_path=PROFILE,
    )
    assert load_queue(queue)["revision"] == 18


def test_top2_uses_objective_peak_qdot_then_original_order(tmp_path: Path) -> None:
    queue = tmp_path / "manual_queue.json"
    seed_initial_grid(
        queue,
        campaign_id="manual-grid",
        release_manifest_sha256=RELEASE,
        launch_profile_path=PROFILE,
    )
    scores = [
        _score(group.group_id, 10.0 + group.index, 20.0, 0.2)
        for group in INITIAL_GROUPS
    ]
    scores[4] = _score("G05", 0.3, 15.0, 0.2)
    scores[1] = _score("G02", 0.3, 14.0, 0.5)
    scores[2] = _score("G03", 0.3, 14.0, 0.4)
    assert [group.group_id for group in rank_top_groups(scores)] == ["G03", "G02"]
    repeats = append_top2_repeats(
        queue,
        campaign_id="manual-grid",
        release_manifest_sha256=RELEASE,
        launch_profile_path=PROFILE,
        scores=scores,
    )
    assert [row["source"] for row in repeats] == [
        "governed_top2_repeat:G03:R1",
        "governed_top2_repeat:G03:R2",
        "governed_top2_repeat:G02:R1",
        "governed_top2_repeat:G02:R2",
    ]
    append_top2_repeats(
        queue,
        campaign_id="manual-grid",
        release_manifest_sha256=RELEASE,
        launch_profile_path=PROFILE,
        scores=scores,
    )
    assert load_queue(queue)["revision"] == 22


def test_one_or_no_eligible_group_has_bounded_repeats(tmp_path: Path) -> None:
    one = tmp_path / "one.json"
    seed_initial_grid(
        one,
        campaign_id="one",
        release_manifest_sha256=RELEASE,
        launch_profile_path=PROFILE,
    )
    scores = [
        TrialScore(group.group_id, False, None, None, None)
        for group in INITIAL_GROUPS
    ]
    scores[6] = _score("G07", 0.2, 12.5, 0.1)
    assert len(
        append_top2_repeats(
            one,
            campaign_id="one",
            release_manifest_sha256=RELEASE,
            launch_profile_path=PROFILE,
            scores=scores,
        )
    ) == 2

    none = tmp_path / "none.json"
    seed_initial_grid(
        none,
        campaign_id="none",
        release_manifest_sha256=RELEASE,
        launch_profile_path=PROFILE,
    )
    assert append_top2_repeats(
        none,
        campaign_id="none",
        release_manifest_sha256=RELEASE,
        launch_profile_path=PROFILE,
        scores=[
            TrialScore(group.group_id, False, None, None, None)
            for group in INITIAL_GROUPS
        ],
    ) == ()
