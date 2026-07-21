from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SOURCE = ROOT.parents[1] / "src/ur10e_experiment_runtime"
sys.path.insert(0, str(RUNTIME_SOURCE))
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_batch_plan import (  # noqa: E402
    ENVELOPE_ID,
    SCHEMA_VERSION_ROLLING,
    SCHEMA_VERSION_ROLLING_V2,
    initialize_rolling_plan,
    load_plan,
)
from step5d_autotune_journal import (  # noqa: E402
    CampaignIdentity,
    HighWaterMarks,
    JournalState,
    SupervisorJournal,
)
from step5d_autotune_r008_policy import initialization_batch  # noqa: E402
from step5d_autotune_store import CampaignStore  # noqa: E402
from step5d_autotune_v3.campaign_prepare import (  # noqa: E402
    INITIALIZATION_SOURCE,
    RESULT_SCHEMA,
    CampaignPrepareError,
    prepare_campaign,
)
from step5d_autotune_v3.profile import canonical_json_bytes  # noqa: E402
from step5d_autotune_v3.runtime_profile import (  # noqa: E402
    DEFAULT_OVERLAY,
    ORIENTATION_KO_LATTICE,
    OVERLAY_FIELDS,
    LaunchProfile,
    normalize_trial_overlay,
)
from step5d_autotune_v3.state import (  # noqa: E402
    CampaignPaths,
    atomic_json,
    read_strict_json,
)


CAMPAIGN_ID = "step5d-native-1"


def launch_profile() -> LaunchProfile:
    policy = {}
    for field in OVERLAY_FIELDS:
        if field == "control_candidate_uid":
            policy[field] = {"derived": "sha256"}
        elif field == "execution_profile_id":
            policy[field] = {"allowed": [DEFAULT_OVERLAY[field]]}
        elif field == "orientation_ko":
            policy[field] = {"allowed": list(ORIENTATION_KO_LATTICE)}
        else:
            policy[field] = {"min": 0.0, "max": 100.0}
    return LaunchProfile(
        document={"test_only": True},
        launch_overrides={},
        trial_overlay_policy=policy,
        fingerprint="c" * 64,
    )


def overlay_fingerprint(profile_fingerprint: str, batches: list[dict]) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "launch_profile_fingerprint": profile_fingerprint,
                "batches": batches,
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def legacy_material(profile: LaunchProfile) -> tuple[dict, dict]:
    candidate_rows = []
    overlay_rows = []
    for occurrence in initialization_batch(1):
        candidate_rows.append(
            {
                "candidate": {
                    "log2_p": occurrence.candidate.log2_p,
                    "log2_i": occurrence.candidate.log2_i,
                    "log2_damping": occurrence.candidate.log2_damping,
                },
                "occurrence_uid": str(occurrence.occurrence_uid),
                "transport_candidate_uid": str(occurrence.transport_candidate_uid),
                "role": occurrence.selection_role,
                "replicate_ordinal": occurrence.replicate_ordinal,
            }
        )
        raw = {
            **DEFAULT_OVERLAY,
            "force_p_gain": occurrence.candidate.force_p_gain,
            "force_i_gain": occurrence.candidate.force_i_gain,
            "force_damping": occurrence.candidate.force_damping,
        }
        raw.pop("control_candidate_uid")
        typed = normalize_trial_overlay(raw, profile=profile)
        legacy_control = typed["control_candidate_uid"].rsplit(":", 1)[-1]
        old_overlay = {**typed, "control_candidate_uid": legacy_control}
        overlay_rows.append(
            {
                "occurrence_uid": str(occurrence.occurrence_uid),
                "transport_candidate_uid": str(occurrence.transport_candidate_uid),
                "control_candidate_uid": legacy_control,
                "normalized_overlay_sha256": hashlib.sha256(
                    canonical_json_bytes(old_overlay)
                ).hexdigest(),
                "overlay": old_overlay,
            }
        )
    plan = {
        "schema_version": SCHEMA_VERSION_ROLLING,
        "envelope_id": ENVELOPE_ID,
        "campaign_id": CAMPAIGN_ID,
        "revision": 1,
        "batch_size": 5,
        "closed": False,
        "lifecycle": "OPEN_READY",
        "closure": None,
        "batches": [
            {
                "batch_id": 1,
                "plan_revision": 1,
                "source": INITIALIZATION_SOURCE,
                "occurrences": candidate_rows,
            }
        ],
    }
    batches = [
        {
            "batch_id": 1,
            "source": INITIALIZATION_SOURCE,
            "trials": overlay_rows,
        }
    ]
    overlays = {
        "schema": "step5d.autotune-v3/trial-overlay-plan-v2",
        "revision": 1,
        "candidate_count": 5,
        "launch_profile_fingerprint": profile.fingerprint,
        "fingerprint": overlay_fingerprint(profile.fingerprint, batches),
        "batches": batches,
    }
    return plan, overlays


def pristine_physical(paths: CampaignPaths) -> None:
    CampaignStore(paths.root / "store").initialize(
        {"campaign": {"campaign_id": CAMPAIGN_ID}}
    )
    identity = CampaignIdentity(
        campaign_id=CAMPAIGN_ID,
        campaign_epoch=1,
        campaign_fingerprint="a" * 64,
        backend_id="step5d-v35-test",
        source_fingerprint="b" * 64,
        config_fingerprint="d" * 64,
    )
    state = JournalState(
        campaign=identity,
        phase="home",
        high_water=HighWaterMarks(),
        candidate_tokens={},
        plant_epoch=1,
        execution_profile_id=DEFAULT_OVERLAY["execution_profile_id"],
        execution_profile_integer_id=633,
    )
    SupervisorJournal((paths.root / "journal").resolve()).append(state)


def legacy_campaign(tmp_path: Path) -> tuple[CampaignPaths, LaunchProfile]:
    paths = CampaignPaths(tmp_path / "campaign")
    profile = launch_profile()
    plan, overlays = legacy_material(profile)
    atomic_json(paths.candidate_plan, plan)
    atomic_json(paths.trial_overlays, overlays)
    pristine_physical(paths)
    return paths, profile


def test_missing_plan_initializes_exact_revision_one_and_revalidates(tmp_path: Path) -> None:
    paths = CampaignPaths(tmp_path / "campaign")
    profile = launch_profile()
    result = prepare_campaign(
        paths.root,
        campaign_id=CAMPAIGN_ID,
        launch_profile=profile,
    )
    assert result["schema"] == RESULT_SCHEMA
    assert result["action"] == "initialize"
    assert result["plan_revision"] == 1
    assert result["plan_schema"] == SCHEMA_VERSION_ROLLING_V2
    assert result["plan_lifecycle"] == "OPEN_READY"
    assert result["fingerprint"]
    again = prepare_campaign(
        paths.root,
        campaign_id=CAMPAIGN_ID,
        launch_profile=profile,
    )
    assert again["action"] == "validated_existing_rolling_v2"
    assert again["coherence_fingerprint"] == result["coherence_fingerprint"]


def test_revision_zero_appends_exact_initialization_batch(tmp_path: Path) -> None:
    paths = CampaignPaths(tmp_path / "campaign")
    profile = launch_profile()
    initialize_rolling_plan(paths.candidate_plan, campaign_id=CAMPAIGN_ID)
    result = prepare_campaign(
        paths.root,
        campaign_id=CAMPAIGN_ID,
        launch_profile=profile,
    )
    assert result["action"] == "initialize_revision_zero"
    assert load_plan(paths.candidate_plan).revision == 1


def test_pristine_rolling_v1_migrates_by_rebuilding_typed_domains(tmp_path: Path) -> None:
    paths, profile = legacy_campaign(tmp_path)
    old_plan_sha = hashlib.sha256(paths.candidate_plan.read_bytes()).hexdigest()
    result = prepare_campaign(
        paths.root,
        campaign_id=CAMPAIGN_ID,
        launch_profile=profile,
    )
    assert result["action"] == "migrate_rolling_v1"
    assert result["recovered"] is False
    assert result["predecessor"]["candidate_plan_sha256"] == old_plan_sha
    plan = load_plan(paths.candidate_plan, campaign_id=CAMPAIGN_ID)
    assert plan.payload["schema_version"] == SCHEMA_VERSION_ROLLING_V2
    for occurrence in plan.occurrences[0]:
        assert str(occurrence.occurrence_uid).startswith("occurrence:v2:")
        assert str(occurrence.transport_candidate_uid).startswith("transport:v2:")
        assert str(occurrence.control_candidate_uid).startswith("control:v2:")
    overlays = read_strict_json(paths.trial_overlays, role="migrated overlays")
    assert all(
        row["control_candidate_uid"].startswith("control:v2:")
        for row in overlays["batches"][0]["trials"]
    )
    assert not (paths.control / "campaign_prepare_intent.json").exists()
    assert read_strict_json(
        paths.control / "campaign_prepare_evidence.json",
        role="campaign prepare evidence",
    ) == result


def test_migration_rejects_any_trial_index_history(tmp_path: Path) -> None:
    paths, profile = legacy_campaign(tmp_path)
    index = read_strict_json(paths.root / "store/trial_index.json", role="trial index")
    index["trial_uids"]["e" * 64] = {
        "relative_trial_dir": f"trials/{'e' * 64}",
        "trial_spec_sha256": "f" * 64,
    }
    atomic_json(paths.root / "store/trial_index.json", index)
    with pytest.raises(CampaignPrepareError, match="TRIAL_HISTORY_PRESENT"):
        prepare_campaign(
            paths.root,
            campaign_id=CAMPAIGN_ID,
            launch_profile=profile,
        )
    assert read_strict_json(paths.candidate_plan, role="legacy plan")[
        "schema_version"
    ] == SCHEMA_VERSION_ROLLING


def test_migration_rejects_non_initial_batch_without_schema_rewrite(tmp_path: Path) -> None:
    paths, profile = legacy_campaign(tmp_path)
    plan = read_strict_json(paths.candidate_plan, role="legacy plan")
    plan["batches"][0]["occurrences"][4]["role"] = "not-the-initial-policy"
    atomic_json(paths.candidate_plan, plan)
    with pytest.raises(CampaignPrepareError, match="LEGACY_BATCH_MISMATCH"):
        prepare_campaign(
            paths.root,
            campaign_id=CAMPAIGN_ID,
            launch_profile=profile,
        )
    assert read_strict_json(paths.candidate_plan, role="legacy plan")[
        "schema_version"
    ] == SCHEMA_VERSION_ROLLING


@pytest.mark.parametrize(
    "cut",
    ("intent_persisted", "plan_replaced", "overlay_replaced", "evidence_persisted"),
)
def test_each_crash_cut_recovers_only_the_exact_transaction(
    tmp_path: Path,
    cut: str,
) -> None:
    paths, profile = legacy_campaign(tmp_path)

    class SimulatedCrash(RuntimeError):
        pass

    def crash_hook(stage: str) -> None:
        if stage == cut:
            raise SimulatedCrash(stage)

    with pytest.raises(SimulatedCrash, match=cut):
        prepare_campaign(
            paths.root,
            campaign_id=CAMPAIGN_ID,
            launch_profile=profile,
            crash_hook=crash_hook,
        )
    assert (paths.control / "campaign_prepare_intent.json").is_file()
    recovered = prepare_campaign(
        paths.root,
        campaign_id=CAMPAIGN_ID,
        launch_profile=profile,
    )
    assert recovered["action"] == "migrate_rolling_v1"
    assert recovered["recovered"] is True
    assert recovered["plan_schema"] == SCHEMA_VERSION_ROLLING_V2
    assert recovered["plan_revision"] == 1
    assert not (paths.control / "campaign_prepare_intent.json").exists()
