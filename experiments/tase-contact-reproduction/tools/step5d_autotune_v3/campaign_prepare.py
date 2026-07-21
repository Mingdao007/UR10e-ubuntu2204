"""Canonical, crash-recoverable preparation of one rolling-v2 campaign plan."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from step5d_autotune_batch_plan import (
    ENVELOPE_ID,
    R008_BATCH_SIZE,
    SCHEMA_VERSION_ROLLING,
    SCHEMA_VERSION_ROLLING_V2,
    PlanLifecycle,
    candidate_from_log2_payload,
    candidate_log2_payload,
    load_plan,
)
from step5d_autotune_r008_policy import initialization_batch

from .runtime_profile import (
    DEFAULT_OVERLAY,
    OVERLAY_FIELDS,
    LaunchProfile,
    normalize_trial_overlay,
    normalized_overlay_sha256,
)
from .state import CampaignPaths, StateError, atomic_json, control_lock, read_strict_json


RESULT_SCHEMA = "step5d.autotune-v3/campaign-prepare-result-v1"
INTENT_SCHEMA = "step5d.autotune-v3/campaign-prepare-intent-v1"
OVERLAY_SCHEMA = "step5d.autotune-v3/trial-overlay-plan-v2"
INITIALIZATION_SOURCE = "rolling-v1-formal-initialization-batch-a"


class CampaignPrepareError(RuntimeError):
    """Campaign metadata cannot be prepared without inventing history."""

    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail


CrashHook = Callable[[str], None]


def _json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise CampaignPrepareError("JSON_INVALID", str(exc)) from exc


def _json_sha256(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _atomic_json_sha256(value: Any) -> str:
    encoded = (
        json.dumps(value, allow_nan=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise CampaignPrepareError("STATE_UNSAFE", f"missing regular file: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _overlay_fingerprint(profile_fingerprint: str, batches: Sequence[Any]) -> str:
    return _json_sha256(
        {
            "launch_profile_fingerprint": profile_fingerprint,
            "batches": list(batches),
        }
    )


def _overlay_payload(profile_fingerprint: str, batches: Sequence[Any]) -> dict[str, Any]:
    detached = json.loads(_json_bytes(list(batches)).decode("ascii"))
    return {
        "schema": OVERLAY_SCHEMA,
        "revision": len(detached),
        "candidate_count": sum(len(batch["trials"]) for batch in detached),
        "launch_profile_fingerprint": profile_fingerprint,
        "fingerprint": _overlay_fingerprint(profile_fingerprint, detached),
        "batches": detached,
    }


def _initial_target(
    campaign_id: str,
    profile: LaunchProfile,
) -> tuple[dict[str, Any], dict[str, Any]]:
    candidate_rows: list[dict[str, Any]] = []
    overlay_rows: list[dict[str, Any]] = []
    for occurrence in initialization_batch(1):
        raw_overlay = {
            **DEFAULT_OVERLAY,
            "force_p_gain": occurrence.candidate.force_p_gain,
            "force_i_gain": occurrence.candidate.force_i_gain,
            "force_damping": occurrence.candidate.force_damping,
        }
        raw_overlay.pop("control_candidate_uid", None)
        overlay = normalize_trial_overlay(raw_overlay, profile=profile)
        bound = occurrence.bind_control_candidate_uid(overlay["control_candidate_uid"])
        candidate_rows.append(
            {
                "candidate": candidate_log2_payload(bound.candidate),
                "occurrence_uid": str(bound.occurrence_uid),
                "transport_candidate_uid": str(bound.transport_candidate_uid),
                "control_candidate_uid": str(bound.control_candidate_uid),
                "role": bound.selection_role,
                "replicate_ordinal": bound.replicate_ordinal,
            }
        )
        overlay_rows.append(
            {
                "occurrence_uid": str(bound.occurrence_uid),
                "transport_candidate_uid": str(bound.transport_candidate_uid),
                "control_candidate_uid": str(bound.control_candidate_uid),
                "normalized_overlay_sha256": normalized_overlay_sha256(profile, overlay),
                "overlay": overlay,
            }
        )
    plan = {
        "schema_version": SCHEMA_VERSION_ROLLING_V2,
        "envelope_id": ENVELOPE_ID,
        "campaign_id": campaign_id,
        "revision": 1,
        "batch_size": R008_BATCH_SIZE,
        "closed": False,
        "lifecycle": PlanLifecycle.OPEN_READY.value,
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
    overlays = _overlay_payload(
        profile.fingerprint,
        [
            {
                "batch_id": 1,
                "source": INITIALIZATION_SOURCE,
                "trials": overlay_rows,
            }
        ],
    )
    return plan, overlays


def _empty_v2_plan(campaign_id: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION_ROLLING_V2,
        "envelope_id": ENVELOPE_ID,
        "campaign_id": campaign_id,
        "revision": 0,
        "batch_size": R008_BATCH_SIZE,
        "closed": False,
        "lifecycle": PlanLifecycle.OPEN_EMPTY.value,
        "closure": None,
        "batches": [],
    }


def _read_optional(path: Path, role: str) -> Mapping[str, Any] | None:
    if path.is_symlink():
        raise CampaignPrepareError("STATE_UNSAFE", f"{role} must not be a symlink")
    if not path.exists():
        return None
    try:
        payload = read_strict_json(path, role=role)
    except (OSError, StateError) as exc:
        raise CampaignPrepareError("STATE_INVALID", str(exc)) from exc
    if not isinstance(payload, Mapping):
        raise CampaignPrepareError("STATE_INVALID", f"{role} must be an object")
    return payload


def _validate_empty_v2(
    plan: Any,
    overlay: Mapping[str, Any] | None,
    profile: LaunchProfile,
) -> None:
    if (
        plan.payload["schema_version"] != SCHEMA_VERSION_ROLLING_V2
        or plan.revision != 0
        or plan.lifecycle is not PlanLifecycle.OPEN_EMPTY
        or plan.closed
        or plan.payload["batches"]
    ):
        raise CampaignPrepareError("REVISION_ZERO_INVALID", "rolling-v2 revision 0 is not pristine")
    if overlay is not None and overlay != _overlay_payload(profile.fingerprint, []):
        raise CampaignPrepareError("OVERLAY_STATE_INVALID", "revision-0 overlay is not exact")


def _validate_legacy_material(
    plan: Mapping[str, Any],
    overlays: Mapping[str, Any],
    *,
    campaign_id: str,
    profile: LaunchProfile,
) -> None:
    required_plan = {
        "schema_version",
        "envelope_id",
        "campaign_id",
        "revision",
        "batch_size",
        "closed",
        "lifecycle",
        "closure",
        "batches",
    }
    if not isinstance(plan, Mapping) or set(plan) != required_plan:
        raise CampaignPrepareError("LEGACY_PLAN_INVALID", "rolling-v1 plan fields differ")
    if (
        plan["schema_version"] != SCHEMA_VERSION_ROLLING
        or plan["envelope_id"] != ENVELOPE_ID
        or plan["campaign_id"] != campaign_id
        or plan["revision"] != 1
        or plan["batch_size"] != R008_BATCH_SIZE
        or plan["closed"] is not False
        or plan["lifecycle"] != PlanLifecycle.OPEN_READY.value
        or plan["closure"] is not None
        or not isinstance(plan["batches"], list)
        or len(plan["batches"]) != 1
    ):
        raise CampaignPrepareError(
            "LEGACY_PLAN_NOT_PRISTINE",
            "only the exact open revision-1 rolling-v1 initialization is migratable",
        )
    batch = plan["batches"][0]
    if (
        not isinstance(batch, Mapping)
        or set(batch) != {"batch_id", "plan_revision", "source", "occurrences"}
        or batch["batch_id"] != 1
        or batch["plan_revision"] != 1
        or batch["source"] != INITIALIZATION_SOURCE
        or not isinstance(batch["occurrences"], list)
        or len(batch["occurrences"]) != R008_BATCH_SIZE
    ):
        raise CampaignPrepareError("LEGACY_BATCH_MISMATCH", "legacy initialization batch differs")
    expected_rows = initialization_batch(1)
    for index, (actual, expected) in enumerate(
        zip(batch["occurrences"], expected_rows, strict=True), start=1
    ):
        required_row = {
            "candidate",
            "occurrence_uid",
            "transport_candidate_uid",
            "role",
            "replicate_ordinal",
        }
        try:
            candidate = candidate_from_log2_payload(actual["candidate"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CampaignPrepareError(
                "LEGACY_BATCH_MISMATCH", f"legacy row {index} cannot be decoded"
            ) from exc
        if (
            not isinstance(actual, Mapping)
            or set(actual) != required_row
            or candidate != expected.candidate
            or actual["occurrence_uid"] != str(expected.occurrence_uid)
            or actual["transport_candidate_uid"] != str(expected.transport_candidate_uid)
            or actual["role"] != expected.selection_role
            or actual["replicate_ordinal"] != expected.replicate_ordinal
        ):
            raise CampaignPrepareError(
                "LEGACY_BATCH_MISMATCH", f"legacy row {index} is not initialization_batch(1)"
            )

    required_overlay = {
        "schema",
        "revision",
        "candidate_count",
        "launch_profile_fingerprint",
        "fingerprint",
        "batches",
    }
    if not isinstance(overlays, Mapping) or set(overlays) != required_overlay:
        raise CampaignPrepareError("LEGACY_OVERLAY_INVALID", "legacy overlay fields differ")
    if (
        overlays["schema"] != OVERLAY_SCHEMA
        or overlays["revision"] != 1
        or overlays["candidate_count"] != R008_BATCH_SIZE
        or overlays["launch_profile_fingerprint"] != profile.fingerprint
        or not isinstance(overlays["batches"], list)
        or len(overlays["batches"]) != 1
        or overlays["fingerprint"]
        != _overlay_fingerprint(profile.fingerprint, overlays["batches"])
    ):
        raise CampaignPrepareError("LEGACY_OVERLAY_INVALID", "legacy overlay identity differs")
    overlay_batch = overlays["batches"][0]
    if (
        not isinstance(overlay_batch, Mapping)
        or set(overlay_batch) != {"batch_id", "source", "trials"}
        or overlay_batch["batch_id"] != 1
        or overlay_batch["source"] != INITIALIZATION_SOURCE
        or not isinstance(overlay_batch["trials"], list)
        or len(overlay_batch["trials"]) != R008_BATCH_SIZE
    ):
        raise CampaignPrepareError("LEGACY_OVERLAY_INVALID", "legacy overlay batch differs")
    for index, (trial, legacy_row, expected) in enumerate(
        zip(
            overlay_batch["trials"],
            batch["occurrences"],
            expected_rows,
            strict=True,
        ),
        start=1,
    ):
        required_trial = {
            "occurrence_uid",
            "transport_candidate_uid",
            "control_candidate_uid",
            "normalized_overlay_sha256",
            "overlay",
        }
        if not isinstance(trial, Mapping) or set(trial) != required_trial:
            raise CampaignPrepareError(
                "LEGACY_OVERLAY_INVALID", f"legacy overlay row {index} fields differ"
            )
        old_overlay = trial["overlay"]
        if not isinstance(old_overlay, Mapping) or set(old_overlay) != set(OVERLAY_FIELDS):
            raise CampaignPrepareError(
                "LEGACY_OVERLAY_INVALID", f"legacy overlay row {index} shape differs"
            )
        replacement = dict(old_overlay)
        replacement.pop("control_candidate_uid", None)
        try:
            normalized = normalize_trial_overlay(replacement, profile=profile)
        except (RuntimeError, TypeError, ValueError) as exc:
            raise CampaignPrepareError(
                "LEGACY_OVERLAY_INVALID", f"legacy overlay row {index} cannot normalize"
            ) from exc
        typed_control = normalized["control_candidate_uid"]
        legacy_control = typed_control.rsplit(":", 1)[-1]
        coordinates = (
            normalized["force_p_gain"],
            normalized["force_i_gain"],
            normalized["force_damping"],
        )
        expected_coordinates = (
            expected.candidate.force_p_gain,
            expected.candidate.force_i_gain,
            expected.candidate.force_damping,
        )
        if (
            trial["occurrence_uid"] != legacy_row["occurrence_uid"]
            or trial["transport_candidate_uid"] != legacy_row["transport_candidate_uid"]
            or trial["control_candidate_uid"] != legacy_control
            or old_overlay["control_candidate_uid"] != legacy_control
            or trial["normalized_overlay_sha256"] != _json_sha256(old_overlay)
            or coordinates != expected_coordinates
        ):
            raise CampaignPrepareError(
                "LEGACY_OVERLAY_MISMATCH", f"legacy overlay row {index} is not exact"
            )


def _require_pristine_physical_state(
    paths: CampaignPaths,
    campaign_id: str,
    *,
    required: bool,
) -> None:
    store_root = paths.root / "store"
    journal_root = paths.root / "journal"
    store_present = store_root.exists() or store_root.is_symlink()
    journal_present = journal_root.exists() or journal_root.is_symlink()
    if not store_present and not journal_present and not required:
        return
    if not store_present or not journal_present:
        raise CampaignPrepareError(
            "PHYSICAL_TRUTH_INCOMPLETE", "store and journal must either both exist or both be absent"
        )
    if (
        store_root.is_symlink()
        or journal_root.is_symlink()
        or not store_root.is_dir()
        or not journal_root.is_dir()
    ):
        raise CampaignPrepareError(
            "PHYSICAL_TRUTH_UNSAFE", "store and journal must be real directories"
        )
    campaign = _read_optional(store_root / "campaign.json", "campaign store identity")
    index = _read_optional(store_root / "trial_index.json", "campaign trial index")
    if campaign is None or index is None:
        raise CampaignPrepareError("STORE_INVALID", "campaign store identity/index is missing")
    campaign_row = campaign.get("campaign")
    if not isinstance(campaign_row, Mapping) or campaign_row.get("campaign_id") != campaign_id:
        raise CampaignPrepareError("CAMPAIGN_ID_MISMATCH", "store campaign identity differs")
    if (
        set(index) != {"schema_version", "trial_uids", "capture_uids"}
        or index["schema_version"] != "step5d.autotune.store-index/v1"
        or index["trial_uids"] != {}
        or index["capture_uids"] != {}
    ):
        raise CampaignPrepareError("TRIAL_HISTORY_PRESENT", "trial index is not exactly empty")
    history = store_root / "history.jsonl"
    if history.is_symlink() or (
        history.exists()
        and (not history.is_file() or history.stat().st_size != 0)
    ):
        raise CampaignPrepareError("TRIAL_HISTORY_PRESENT", "campaign history is not empty")
    trials = store_root / "trials"
    if trials.is_symlink() or (
        trials.exists()
        and (not trials.is_dir() or any(trials.iterdir()))
    ):
        raise CampaignPrepareError("TRIAL_HISTORY_PRESENT", "trial artifact tree is not empty")

    try:
        from step5d_autotune_journal import HighWaterMarks, SupervisorJournal

        latest = SupervisorJournal(journal_root.resolve()).load_latest()
    except Exception as exc:
        raise CampaignPrepareError("JOURNAL_INVALID", str(exc)) from exc
    state = latest.state
    if state.campaign.campaign_id != campaign_id:
        raise CampaignPrepareError("CAMPAIGN_ID_MISMATCH", "journal campaign identity differs")
    if (
        state.phase != "home"
        or state.active_trial is not None
        or state.pending_ack is not None
        or state.pending_advance is not None
        or state.pending_retry is not None
        or state.high_water != HighWaterMarks()
        or dict(state.candidate_tokens)
        or state.observation_references
        or state.history_references
        or state.terminal_fates
        or state.governor_probe is not None
    ):
        raise CampaignPrepareError(
            "CAMPAIGN_NOT_PRISTINE_HOME", "journal contains active or historical trial truth"
        )
    if paths.mailbox.exists() or paths.mailbox.is_symlink():
        raise CampaignPrepareError("ACTIVE_COMMAND_PRESENT", "command mailbox already exists")


def _validate_v2_coherence(
    paths: CampaignPaths,
    *,
    campaign_id: str,
    profile: LaunchProfile,
) -> tuple[Any, Mapping[str, Any], str]:
    try:
        plan = load_plan(paths.candidate_plan, campaign_id=campaign_id)
    except (OSError, TypeError, ValueError) as exc:
        raise CampaignPrepareError("CANDIDATE_STATE_INVALID", str(exc)) from exc
    if plan.payload["schema_version"] != SCHEMA_VERSION_ROLLING_V2:
        raise CampaignPrepareError("CANDIDATE_SCHEMA_INVALID", "rolling-v2 is required")
    overlays = _read_optional(paths.trial_overlays, "trial-overlay plan")
    if overlays is None:
        raise CampaignPrepareError("OVERLAY_STATE_INVALID", "trial-overlay plan is missing")
    required = {
        "schema",
        "revision",
        "candidate_count",
        "launch_profile_fingerprint",
        "fingerprint",
        "batches",
    }
    if not isinstance(overlays, Mapping) or set(overlays) != required:
        raise CampaignPrepareError("OVERLAY_STATE_INVALID", "trial-overlay fields differ")
    if (
        overlays["schema"] != OVERLAY_SCHEMA
        or overlays["revision"] != plan.revision
        or overlays["candidate_count"] != len(plan.candidates)
        or overlays["launch_profile_fingerprint"] != profile.fingerprint
        or not isinstance(overlays["batches"], list)
        or len(overlays["batches"]) != plan.revision
        or overlays["fingerprint"]
        != _overlay_fingerprint(profile.fingerprint, overlays["batches"])
    ):
        raise CampaignPrepareError("OVERLAY_STATE_INVALID", "trial-overlay identity differs")
    coherence: list[dict[str, Any]] = []
    for batch_index, (candidate_batch, overlay_batch) in enumerate(
        zip(plan.payload["batches"], overlays["batches"], strict=True), start=1
    ):
        if (
            not isinstance(overlay_batch, Mapping)
            or set(overlay_batch) != {"batch_id", "source", "trials"}
            or overlay_batch["batch_id"] != batch_index
            or overlay_batch["source"] != candidate_batch["source"]
            or not isinstance(overlay_batch["trials"], list)
            or len(overlay_batch["trials"]) != len(plan.occurrences[batch_index - 1])
        ):
            raise CampaignPrepareError(
                "OVERLAY_COHERENCE_MISMATCH", f"overlay batch {batch_index} differs"
            )
        for row_index, (occurrence, trial) in enumerate(
            zip(
                plan.occurrences[batch_index - 1],
                overlay_batch["trials"],
                strict=True,
            ),
            start=1,
        ):
            required_trial = {
                "occurrence_uid",
                "transport_candidate_uid",
                "control_candidate_uid",
                "normalized_overlay_sha256",
                "overlay",
            }
            if not isinstance(trial, Mapping) or set(trial) != required_trial:
                raise CampaignPrepareError(
                    "OVERLAY_COHERENCE_MISMATCH",
                    f"overlay row {batch_index}:{row_index} fields differ",
                )
            try:
                normalized = normalize_trial_overlay(trial["overlay"], profile=profile)
            except (RuntimeError, TypeError, ValueError) as exc:
                raise CampaignPrepareError(
                    "OVERLAY_COHERENCE_MISMATCH",
                    f"overlay row {batch_index}:{row_index} is invalid",
                ) from exc
            expected_identity = (
                str(occurrence.occurrence_uid),
                str(occurrence.transport_candidate_uid),
                str(occurrence.control_candidate_uid),
            )
            actual_identity = (
                trial["occurrence_uid"],
                trial["transport_candidate_uid"],
                trial["control_candidate_uid"],
            )
            if (
                trial["overlay"] != normalized
                or actual_identity != expected_identity
                or normalized["control_candidate_uid"] != expected_identity[2]
                or trial["normalized_overlay_sha256"]
                != normalized_overlay_sha256(profile, normalized)
                or (
                    normalized["force_p_gain"],
                    normalized["force_i_gain"],
                    normalized["force_damping"],
                )
                != (
                    occurrence.candidate.force_p_gain,
                    occurrence.candidate.force_i_gain,
                    occurrence.candidate.force_damping,
                )
            ):
                raise CampaignPrepareError(
                    "OVERLAY_COHERENCE_MISMATCH",
                    f"overlay row {batch_index}:{row_index} identity differs",
                )
            coherence.append(
                {
                    "occurrence_uid": expected_identity[0],
                    "transport_candidate_uid": expected_identity[1],
                    "control_candidate_uid": expected_identity[2],
                    "normalized_overlay_sha256": trial["normalized_overlay_sha256"],
                }
            )
    return plan, overlays, _json_sha256(coherence)


def _state(path: Path, role: str) -> dict[str, Any]:
    payload = _read_optional(path, role)
    return {
        "exists": payload is not None,
        "payload": None if payload is None else dict(payload),
        "sha256": None if payload is None else _file_sha256(path),
    }


def _state_matches(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    return actual == expected


def _target_state(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "exists": True,
        "payload": dict(payload),
        "sha256": _atomic_json_sha256(payload),
    }


def _validate_state_record(value: Any, role: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"exists", "payload", "sha256"}:
        raise CampaignPrepareError("INTENT_INVALID", f"intent {role} state fields differ")
    if value["exists"] is False:
        if value["payload"] is not None or value["sha256"] is not None:
            raise CampaignPrepareError("INTENT_INVALID", f"absent {role} state carries bytes")
    elif value["exists"] is True:
        if not isinstance(value["payload"], Mapping):
            raise CampaignPrepareError("INTENT_INVALID", f"present {role} lacks a payload")
        digest = value["sha256"]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise CampaignPrepareError("INTENT_INVALID", f"intent {role} SHA-256 is invalid")
    else:
        raise CampaignPrepareError("INTENT_INVALID", f"intent {role} existence is invalid")
    return value


def _write_intent(paths: CampaignPaths, payload: Mapping[str, Any]) -> None:
    if (
        paths.control.joinpath("campaign_prepare_intent.json").exists()
        or paths.control.joinpath("campaign_prepare_intent.json").is_symlink()
    ):
        raise CampaignPrepareError("INTENT_CONFLICT", "campaign prepare intent already exists")
    atomic_json(paths.control / "campaign_prepare_intent.json", payload)


def _clear_file(path: Path, role: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise CampaignPrepareError("STATE_UNSAFE", f"{role} is not a regular file")
    path.unlink()
    descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _result(
    paths: CampaignPaths,
    *,
    campaign_id: str,
    profile: LaunchProfile,
    action: str,
    recovered: bool,
    predecessor: Mapping[str, Any] | None,
    transaction_fingerprint: str | None,
) -> dict[str, Any]:
    plan, _overlays, coherence = _validate_v2_coherence(
        paths, campaign_id=campaign_id, profile=profile
    )
    material = {
        "schema": RESULT_SCHEMA,
        "ok": True,
        "action": action,
        "recovered": recovered,
        "campaign_id": campaign_id,
        "launch_profile_fingerprint": profile.fingerprint,
        "plan_schema": plan.payload["schema_version"],
        "plan_revision": plan.revision,
        "plan_lifecycle": plan.lifecycle.value,
        "candidate_plan_sha256": _file_sha256(paths.candidate_plan),
        "trial_overlay_plan_sha256": _file_sha256(paths.trial_overlays),
        "coherence_fingerprint": coherence,
        "predecessor": None if predecessor is None else dict(predecessor),
        "transaction_fingerprint": transaction_fingerprint,
    }
    return {**material, "fingerprint": _json_sha256(material)}


def _apply_intent(
    paths: CampaignPaths,
    intent: Mapping[str, Any],
    *,
    campaign_id: str,
    profile: LaunchProfile,
    crash_hook: CrashHook | None,
    recovering: bool,
) -> dict[str, Any]:
    required = {
        "schema",
        "operation",
        "campaign_id",
        "launch_profile_fingerprint",
        "predecessor",
        "target_plan",
        "target_overlays",
        "fingerprint",
    }
    if not isinstance(intent, Mapping) or set(intent) != required:
        raise CampaignPrepareError("INTENT_INVALID", "campaign prepare intent fields differ")
    material = {key: intent[key] for key in required - {"fingerprint"}}
    if (
        intent["schema"] != INTENT_SCHEMA
        or intent["campaign_id"] != campaign_id
        or intent["launch_profile_fingerprint"] != profile.fingerprint
        or intent["fingerprint"] != _json_sha256(material)
    ):
        raise CampaignPrepareError("INTENT_INVALID", "campaign prepare intent identity differs")
    target_plan, target_overlays = _initial_target(campaign_id, profile)
    if intent["target_plan"] != target_plan or intent["target_overlays"] != target_overlays:
        raise CampaignPrepareError("INTENT_INVALID", "intent target is not canonical initialization")
    predecessor = intent["predecessor"]
    if not isinstance(predecessor, Mapping) or set(predecessor) != {"plan", "overlays"}:
        raise CampaignPrepareError("INTENT_INVALID", "intent predecessor fields differ")
    predecessor_plan = _validate_state_record(predecessor["plan"], "candidate plan")
    predecessor_overlays = _validate_state_record(
        predecessor["overlays"], "trial-overlay plan"
    )
    if intent["operation"] == "migrate_rolling_v1":
        if not predecessor_plan["exists"] or not predecessor_overlays["exists"]:
            raise CampaignPrepareError("INTENT_INVALID", "migration predecessor is missing")
        _validate_legacy_material(
            predecessor_plan["payload"],
            predecessor_overlays["payload"],
            campaign_id=campaign_id,
            profile=profile,
        )
        _require_pristine_physical_state(paths, campaign_id, required=True)
    elif intent["operation"] == "initialize":
        if predecessor_plan["exists"] or predecessor_overlays["exists"]:
            raise CampaignPrepareError("INTENT_INVALID", "initialize predecessor is not empty")
        _require_pristine_physical_state(paths, campaign_id, required=False)
    elif intent["operation"] == "initialize_revision_zero":
        empty_overlay = _overlay_payload(profile.fingerprint, [])
        if (
            predecessor_plan["payload"] != _empty_v2_plan(campaign_id)
            or (
                predecessor_overlays["exists"]
                and predecessor_overlays["payload"] != empty_overlay
            )
        ):
            raise CampaignPrepareError("INTENT_INVALID", "revision-zero predecessor differs")
        _require_pristine_physical_state(paths, campaign_id, required=False)
    else:
        raise CampaignPrepareError("INTENT_INVALID", "intent operation is unsupported")

    actual_plan = _state(paths.candidate_plan, "candidate plan")
    actual_overlays = _state(paths.trial_overlays, "trial-overlay plan")
    target_plan_state = _target_state(target_plan)
    target_overlay_state = _target_state(target_overlays)
    plan_is_target = _state_matches(actual_plan, target_plan_state)
    overlays_are_target = _state_matches(actual_overlays, target_overlay_state)
    plan_is_old = _state_matches(actual_plan, predecessor_plan)
    overlays_are_old = _state_matches(actual_overlays, predecessor_overlays)
    if plan_is_old and overlays_are_old:
        atomic_json(paths.candidate_plan, target_plan)
        if crash_hook is not None:
            crash_hook("plan_replaced")
        actual_plan = _state(paths.candidate_plan, "candidate plan")
        plan_is_target = _state_matches(actual_plan, target_plan_state)
    if plan_is_target and overlays_are_old:
        atomic_json(paths.trial_overlays, target_overlays)
        if crash_hook is not None:
            crash_hook("overlay_replaced")
        actual_overlays = _state(paths.trial_overlays, "trial-overlay plan")
        overlays_are_target = _state_matches(actual_overlays, target_overlay_state)
    if not plan_is_target or not overlays_are_target:
        raise CampaignPrepareError(
            "RECOVERY_NOT_PROVABLE",
            "campaign files are neither the exact predecessor nor exact target",
        )
    result = _result(
        paths,
        campaign_id=campaign_id,
        profile=profile,
        action=intent["operation"],
        recovered=recovering,
        predecessor={
            "candidate_plan_sha256": predecessor_plan["sha256"],
            "trial_overlay_plan_sha256": predecessor_overlays["sha256"],
        },
        transaction_fingerprint=intent["fingerprint"],
    )
    atomic_json(paths.control / "campaign_prepare_evidence.json", result)
    if crash_hook is not None:
        crash_hook("evidence_persisted")
    _clear_file(paths.control / "campaign_prepare_intent.json", "campaign prepare intent")
    return result


def prepare_campaign(
    campaign_root: Path,
    *,
    campaign_id: str,
    launch_profile: LaunchProfile,
    crash_hook: CrashHook | None = None,
) -> dict[str, Any]:
    """Prepare or validate the canonical rolling-v2 plan without live I/O."""

    if not isinstance(campaign_id, str) or not campaign_id.strip():
        raise CampaignPrepareError("CAMPAIGN_ID_INVALID", "campaign_id must be non-empty")
    if not isinstance(launch_profile, LaunchProfile):
        raise CampaignPrepareError("PROFILE_INVALID", "launch_profile must be a LaunchProfile")
    paths = CampaignPaths(Path(campaign_root))
    with control_lock(paths):
        producer_intent = paths.control / "batch_producer_intent.json"
        if producer_intent.exists() or producer_intent.is_symlink():
            raise CampaignPrepareError(
                "PRODUCER_INTENT_PRESENT", "batch producer intent forbids campaign preparation"
            )
        intent_path = paths.control / "campaign_prepare_intent.json"
        if intent_path.exists() or intent_path.is_symlink():
            intent = _read_optional(intent_path, "campaign prepare intent")
            assert intent is not None
            return _apply_intent(
                paths,
                intent,
                campaign_id=campaign_id,
                profile=launch_profile,
                crash_hook=crash_hook,
                recovering=True,
            )

        current_plan = _read_optional(paths.candidate_plan, "candidate plan")
        current_overlays = _read_optional(paths.trial_overlays, "trial-overlay plan")
        if current_plan is None:
            if current_overlays is not None:
                raise CampaignPrepareError("ORPHAN_OVERLAY", "overlay exists without a plan")
            _require_pristine_physical_state(paths, campaign_id, required=False)
            operation = "initialize"
        elif current_plan.get("schema_version") == SCHEMA_VERSION_ROLLING:
            if current_overlays is None:
                raise CampaignPrepareError("LEGACY_OVERLAY_INVALID", "legacy overlay is missing")
            _validate_legacy_material(
                current_plan,
                current_overlays,
                campaign_id=campaign_id,
                profile=launch_profile,
            )
            _require_pristine_physical_state(paths, campaign_id, required=True)
            operation = "migrate_rolling_v1"
        elif current_plan.get("schema_version") == SCHEMA_VERSION_ROLLING_V2:
            try:
                plan = load_plan(paths.candidate_plan, campaign_id=campaign_id)
            except (OSError, TypeError, ValueError) as exc:
                raise CampaignPrepareError("CANDIDATE_STATE_INVALID", str(exc)) from exc
            if plan.revision > 0:
                return _result(
                    paths,
                    campaign_id=campaign_id,
                    profile=launch_profile,
                    action="validated_existing_rolling_v2",
                    recovered=False,
                    predecessor=None,
                    transaction_fingerprint=None,
                )
            _validate_empty_v2(plan, current_overlays, launch_profile)
            _require_pristine_physical_state(paths, campaign_id, required=False)
            operation = "initialize_revision_zero"
        else:
            raise CampaignPrepareError(
                "CANDIDATE_SCHEMA_INVALID", "only rolling-v1 or rolling-v2 is supported"
            )

        target_plan, target_overlays = _initial_target(campaign_id, launch_profile)
        predecessor = {
            "plan": _state(paths.candidate_plan, "candidate plan"),
            "overlays": _state(paths.trial_overlays, "trial-overlay plan"),
        }
        material = {
            "schema": INTENT_SCHEMA,
            "operation": operation,
            "campaign_id": campaign_id,
            "launch_profile_fingerprint": launch_profile.fingerprint,
            "predecessor": predecessor,
            "target_plan": target_plan,
            "target_overlays": target_overlays,
        }
        intent = {**material, "fingerprint": _json_sha256(material)}
        _write_intent(paths, intent)
        if crash_hook is not None:
            crash_hook("intent_persisted")
        return _apply_intent(
            paths,
            intent,
            campaign_id=campaign_id,
            profile=launch_profile,
            crash_hook=crash_hook,
            recovering=False,
        )


__all__ = ["CampaignPrepareError", "RESULT_SCHEMA", "prepare_campaign"]
