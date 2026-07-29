"""Fail-closed internal producer for the Step5d rolling five-row plan.

This module has deliberately no CLI or live entry point.  A supervisor may call
``poll_once`` or ``watch`` while the existing campaign runner consumes batches.
All work is campaign-local metadata: no robot, controller, bridge, or network
endpoint is imported or opened here.

Candidate-plan and trial-overlay updates are two separate atomic replacements.
The write-ahead intent below makes the only supported torn state -- one exact
candidate revision ahead of its overlay -- recoverable without guessing.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from step5d_autotune_contract import (
    Evaluation,
    ForceCandidate,
    SearchTier,
    TrialDisposition,
)
from step5d_autotune_batch_plan import (
    R008_BATCH_SIZE,
    SCHEMA_VERSION_ROLLING_V2,
    CandidateBatchPlan,
    PlanLifecycle,
    append_r008_batch,
    candidate_from_log2_payload,
    candidate_log2_payload,
    load_plan,
)
from step5d_autotune_v3.control_policy import (
    PlannedOccurrence,
    bo_gate,
    initialization_batch,
    recovery_batch,
)
from step5d_autotune_v3.optimizer_types import Observation
from step5d_autotune_store import CampaignStore
from step5d_parameter_search_domain import production_candidate_catalog
from ur10e_experiment_runtime import BatchJournal, ControlCandidateUid

from .runtime_profile import (
    DEFAULT_OVERLAY,
    LaunchProfile,
    normalize_trial_overlay,
    normalized_overlay_sha256,
)
from .state import CampaignPaths, StateError, atomic_json, control_lock, read_strict_json, utc_now


INTENT_SCHEMA = "step5d.autotune-v3/batch-producer-intent-v1"
HEARTBEAT_SCHEMA = "step5d.autotune-v3/batch-producer-heartbeat-v1"
EVIDENCE_SCHEMA = "step5d.autotune-v3/batch-producer-evidence-v1"
OVERLAY_PLAN_SCHEMA = "step5d.autotune-v3/trial-overlay-plan-v2"
OBSERVATION_MATERIAL_SCHEMA = "step5d.autotune-v3/optimizer-observations-v1"
BATCH_A_CLOSURE_SCHEMA = "step5d.autotune-v3/batch-a-closure-v1"
PRODUCTION_BATCH_A_SOURCE = "batch_producer.production_supercycle_a"
PRODUCTION_BATCH_B_SOURCE = "batch_producer.production_supercycle_b"
PRODUCTION_RECOVERY_SOURCE = "batch_producer.production_recovery"
_SHA256_HEX = frozenset("0123456789abcdef")


class BatchProducerError(RuntimeError):
    """The producer cannot prove that appending or recovering is safe."""

    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail


@dataclass(frozen=True)
class BatchProposal:
    """A five-row proposal returned by the existing optimizer policy.

    ``occurrences`` must already carry the target revision/sequence and rows
    1..5.  The producer binds their control identity to normalized V3 overlays.
    """

    occurrences: Sequence[PlannedOccurrence]
    source: str
    policy: str
    evidence: Mapping[str, Any]


@dataclass(frozen=True)
class ProducerSnapshot:
    phase: str
    plan_revision: int
    overlay_revision: int
    lifecycle: str
    appended: bool
    recovered: bool
    policy: str | None
    evidence_sha256: str | None


@dataclass(frozen=True)
class BatchAClosure:
    """Proof that Batch A is sealed and its exact outcomes fed the GP update."""

    batch_sequence: int
    batch_uid: str
    batch_result_path: str
    batch_result_sha256: str
    sealed_bundle_set_sha256: str
    cold_read_verified: bool
    observation_material: tuple[Mapping[str, Any], ...]
    observation_material_sha256: str
    gp_update_sha256: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.batch_sequence, bool)
            or not isinstance(self.batch_sequence, int)
            or self.batch_sequence < 3
            or self.batch_sequence % 2 != 1
        ):
            raise BatchProducerError(
                "BATCH_A_CLOSURE_INVALID", "Batch A sequence must be odd and at least 3"
            )
        for name in (
            "batch_uid",
            "batch_result_sha256",
            "sealed_bundle_set_sha256",
            "observation_material_sha256",
            "gp_update_sha256",
        ):
            _sha256(getattr(self, name), name=name)
        if self.cold_read_verified is not True:
            raise BatchProducerError(
                "BATCH_A_CLOSURE_INVALID", "Batch A history was not cold-read verified"
            )
        if not isinstance(self.batch_result_path, str) or not self.batch_result_path:
            raise BatchProducerError(
                "BATCH_A_CLOSURE_INVALID", "Batch A result path is missing"
            )
        material = tuple(_json_copy(row) for row in self.observation_material)
        if _json_sha256(list(material)) != self.observation_material_sha256:
            raise BatchProducerError(
                "BATCH_A_CLOSURE_INVALID", "Batch A observation material digest differs"
            )
        object.__setattr__(self, "observation_material", material)

    def policy_payload(self) -> dict[str, Any]:
        return {
            "batch_result_sha256": self.batch_result_sha256,
            "sealed_bundle_set_sha256": self.sealed_bundle_set_sha256,
            "cold_read_verified": True,
            "gp_update_sha256": self.gp_update_sha256,
        }

    def evidence(self) -> dict[str, Any]:
        return {
            "schema": BATCH_A_CLOSURE_SCHEMA,
            "batch_sequence": self.batch_sequence,
            "batch_uid": self.batch_uid,
            "batch_result_path": self.batch_result_path,
            **self.policy_payload(),
            "observation_material_sha256": self.observation_material_sha256,
            "observation_count": len(self.observation_material),
        }


@dataclass(frozen=True)
class _IntentOccurrence:
    logical_batch_sequence: int
    row_index: int
    candidate: Any
    plan_revision: int
    selection_role: str
    replicate_ordinal: int
    occurrence_uid: str
    transport_candidate_uid: str
    control_candidate_uid: str


@dataclass(frozen=True)
class _VerifiedRuntimeBatch:
    sequence: int
    identity: Any
    state: Any
    result: Mapping[str, Any]
    result_path: Path
    result_sha256: str


@dataclass(frozen=True)
class _ProductionTruth:
    observations: tuple[Observation, ...]
    observation_material: tuple[Mapping[str, Any], ...]
    observation_material_sha256: str
    history_by_trial: Mapping[str, Mapping[str, Any]]
    batches: Mapping[int, _VerifiedRuntimeBatch]


ProposalProvider = Callable[[int], BatchProposal | None]
CrashHook = Callable[[str], None]


def _sha256(value: Any, *, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _SHA256_HEX for character in value)
    ):
        raise BatchProducerError("IDENTITY_INVALID", f"{name} must be a lowercase SHA-256")
    return value


def _json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise BatchProducerError("JSON_INVALID", f"state is not canonical JSON: {exc}") from exc


def _json_copy(value: Any) -> Any:
    return json.loads(_json_bytes(value).decode("utf-8"))


def _json_sha256(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _file_sha256(path: Path, *, role: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise BatchProducerError("STATE_INVALID", f"{role} must be a real regular file")
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise BatchProducerError("STATE_INVALID", f"cannot read {role}: {exc}") from exc


def _overlay_plan_fingerprint(
    *, launch_profile_fingerprint: str, batches: Sequence[Mapping[str, Any]]
) -> str:
    return _json_sha256(
        {
            "launch_profile_fingerprint": launch_profile_fingerprint,
            "batches": list(batches),
        }
    )


def _overlay_plan_payload(
    *, launch_profile_fingerprint: str, batches: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    detached_batches = _json_copy(list(batches))
    return {
        "schema": OVERLAY_PLAN_SCHEMA,
        "revision": len(detached_batches),
        "candidate_count": sum(len(batch["trials"]) for batch in detached_batches),
        "launch_profile_fingerprint": launch_profile_fingerprint,
        "fingerprint": _overlay_plan_fingerprint(
            launch_profile_fingerprint=launch_profile_fingerprint,
            batches=detached_batches,
        ),
        "batches": detached_batches,
    }


def _active_epoch_root(campaign_root: Path) -> Path:
    root = campaign_root.resolve()
    if root.is_symlink() or not root.is_dir():
        raise BatchProducerError(
            "PRODUCTION_HISTORY_INVALID", "campaign root must be a real directory"
        )
    if (root / "store" / "campaign.json").is_file():
        return root
    epochs = root / "epochs"
    if not epochs.exists():
        return root
    if epochs.is_symlink() or not epochs.is_dir():
        raise BatchProducerError(
            "PRODUCTION_HISTORY_INVALID", "campaign epochs root is unsafe"
        )
    candidates = []
    for path in epochs.iterdir():
        if path.is_symlink() or not path.is_dir() or not path.name.isdigit():
            raise BatchProducerError(
                "PRODUCTION_HISTORY_INVALID", "campaign epochs contain an unsafe entry"
            )
        if (path / "store" / "campaign.json").is_file():
            candidates.append(path.resolve())
    return max(candidates, key=lambda path: int(path.name)) if candidates else root


def _evaluation_from_history(payload: Any) -> Evaluation:
    required = {
        "schema_version",
        "trial_uid",
        "backend_id",
        "eligible",
        "disposition",
        "objective_mae_n",
        "force_bias_n",
        "force_std_n",
        "coverage_12_plus_minus_1_ratio",
        "complete_bins",
        "safe_closure",
        "structural_failures",
        "metrics",
    }
    if not isinstance(payload, Mapping) or set(payload) != required:
        raise BatchProducerError(
            "PRODUCTION_HISTORY_INVALID", "history evaluation fields differ"
        )
    try:
        evaluation = Evaluation(
            trial_uid=payload["trial_uid"],
            backend_id=payload["backend_id"],
            eligible=payload["eligible"],
            disposition=TrialDisposition(payload["disposition"]),
            objective_mae_n=payload["objective_mae_n"],
            force_bias_n=payload["force_bias_n"],
            force_std_n=payload["force_std_n"],
            coverage_12_plus_minus_1_ratio=payload[
                "coverage_12_plus_minus_1_ratio"
            ],
            complete_bins=payload["complete_bins"],
            safe_closure=payload["safe_closure"],
            structural_failures=tuple(payload["structural_failures"]),
            metrics=payload["metrics"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise BatchProducerError(
            "PRODUCTION_HISTORY_INVALID", f"history evaluation is invalid: {exc}"
        ) from exc
    if evaluation.history_payload() != dict(payload):
        raise BatchProducerError(
            "PRODUCTION_HISTORY_INVALID", "history evaluation is not canonical"
        )
    return evaluation


def _observations_from_history(
    history: Sequence[Mapping[str, Any]],
    *,
    campaign_id: str,
    runtime_rows: Mapping[str, tuple[str, str, bool]],
) -> tuple[tuple[Observation, ...], tuple[Mapping[str, Any], ...]]:
    observations: list[Observation] = []
    material: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(history, start=1):
        if not isinstance(row, Mapping):
            raise BatchProducerError(
                "PRODUCTION_HISTORY_INVALID", f"history row {index} is not an object"
            )
        trial_uid = row.get("trial_uid")
        if trial_uid in seen or trial_uid not in runtime_rows:
            raise BatchProducerError(
                "PRODUCTION_HISTORY_INVALID",
                f"history row {index} lacks one exact runtime batch row",
            )
        seen.add(trial_uid)
        if row.get("campaign_id") != campaign_id:
            raise BatchProducerError(
                "PRODUCTION_HISTORY_INVALID", "history crosses campaign identity"
            )
        candidate_payload = row.get("candidate")
        profile_payload = row.get("execution_profile")
        provenance = row.get("artifact_provenance")
        if (
            not isinstance(candidate_payload, Mapping)
            or not isinstance(profile_payload, Mapping)
            or not isinstance(provenance, Mapping)
            or not isinstance(provenance.get("csv"), Mapping)
        ):
            raise BatchProducerError(
                "PRODUCTION_HISTORY_INVALID", "history candidate/profile/provenance differs"
            )
        try:
            candidate = ForceCandidate(
                target_force_n=candidate_payload["target_force_n"],
                force_p_gain=candidate_payload["force_p_gain"],
                force_i_gain=candidate_payload["force_i_gain"],
                force_damping=candidate_payload["force_damping"],
                orientation_ko=candidate_payload.get("orientation_ko", 0.4),
                normal_filter_tau_s=candidate_payload.get(
                    "normal_filter_tau_s", 0.35
                ),
            )
            evaluation = _evaluation_from_history(row.get("evaluation"))
            profile_id = profile_payload["profile_id"]
            plant_epoch = row["plant_epoch"]
            trace_sha256 = _sha256(
                provenance["csv"]["sha256"], name="history CSV SHA-256"
            )
            control_uid = ControlCandidateUid.parse(runtime_rows[trial_uid][0])
            observation = Observation(
                candidate,
                evaluation,
                profile_id,
                plant_epoch,
                trace_sha256,
                control_uid,
            )
        except BatchProducerError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise BatchProducerError(
                "PRODUCTION_HISTORY_INVALID", f"history row {index} is invalid: {exc}"
            ) from exc
        bundle_sha256, optimizer_eligible = runtime_rows[trial_uid][1:]
        if (
            row.get("candidate_uid") != candidate.candidate_uid
            or candidate.payload() != dict(candidate_payload)
            or evaluation.trial_uid != trial_uid
            or evaluation.eligible is not optimizer_eligible
        ):
            raise BatchProducerError(
                "PRODUCTION_HISTORY_INVALID", f"history row {index} identity differs"
            )
        observations.append(observation)
        material.append(
            {
                "history_identity": _sha256(
                    row.get("history_identity"), name="history identity"
                ),
                "trial_uid": trial_uid,
                "candidate_uid": candidate.candidate_uid,
                "control_candidate_uid": str(control_uid),
                "immutable_bundle_sha256": bundle_sha256,
                "profile_id": profile_id,
                "plant_epoch": plant_epoch,
                "latest_trace_sha256": trace_sha256,
                "candidate": candidate.payload(),
                "evaluation": evaluation.history_payload(),
            }
        )
    if seen != set(runtime_rows):
        raise BatchProducerError(
            "PRODUCTION_HISTORY_INVALID", "runtime batch rows and cold-read history differ"
        )
    return tuple(observations), tuple(material)


class ProductionProposalProvider:
    """Rebuild production optimizer proposals only from sealed campaign truth."""

    def __init__(
        self,
        *,
        campaign_root: Path,
        campaign_id: str,
        catalog: Sequence[ForceCandidate],
        optimizer_client: Any | None = None,
    ) -> None:
        if not isinstance(campaign_id, str) or not campaign_id.strip():
            raise BatchProducerError(
                "IDENTITY_INVALID", "production provider campaign_id is invalid"
            )
        candidates = tuple(catalog)
        if (
            len(candidates) < 5
            or any(not isinstance(row, ForceCandidate) for row in candidates)
            or len({row.candidate_uid for row in candidates}) != len(candidates)
        ):
            raise BatchProducerError(
                "POLICY_MISMATCH", "production optimizer catalog is not unique or large enough"
            )
        self.paths = CampaignPaths(campaign_root)
        self.campaign_id = campaign_id
        self.catalog = candidates
        self.optimizer_client = optimizer_client

    def _verified_runtime_batches(
        self,
        *,
        epoch_root: Path,
        plan: CandidateBatchPlan,
    ) -> tuple[
        dict[int, _VerifiedRuntimeBatch],
        dict[str, tuple[str, str, bool]],
        tuple[str, ...],
    ]:
        batches_root = epoch_root / "runtime_batches"
        if batches_root.is_symlink() or not batches_root.is_dir():
            raise BatchProducerError(
                "PRODUCTION_CLOSURE_INVALID", "runtime batch root is missing or unsafe"
            )
        roots = []
        for path in batches_root.iterdir():
            if path.is_symlink() or not path.is_dir():
                raise BatchProducerError(
                    "PRODUCTION_CLOSURE_INVALID", "runtime batch root contains an unsafe entry"
                )
            roots.append(path.resolve())
        batches: dict[int, _VerifiedRuntimeBatch] = {}
        runtime_rows: dict[str, tuple[str, str, bool]] = {}
        ordered_trials: list[str] = []
        try:
            for root in roots:
                journal = BatchJournal.open(root)
                identity = journal.identity()
                sequence = identity.logical_batch_sequence
                if (
                    identity.protocol != "v3_full_home_rolling_arm_v1"
                    or identity.plan_revision != sequence
                    or sequence in batches
                    or sequence < 1
                    or sequence > plan.revision
                ):
                    raise BatchProducerError(
                        "PRODUCTION_CLOSURE_INVALID", "runtime batch identity sequence differs"
                    )
                occurrences = plan.occurrences[sequence - 1]
                if len(identity.rows) != R008_BATCH_SIZE or len(occurrences) != R008_BATCH_SIZE:
                    raise BatchProducerError(
                        "PRODUCTION_CLOSURE_INVALID", "runtime batch row count differs"
                    )
                for runtime_row, occurrence in zip(
                    identity.rows, occurrences, strict=True
                ):
                    candidate = occurrence.candidate
                    if (
                        runtime_row.occurrence_uid != str(occurrence.occurrence_uid)
                        or runtime_row.transport_candidate_uid
                        != str(occurrence.transport_candidate_uid)
                        or runtime_row.control_candidate_uid
                        != str(occurrence.control_candidate_uid)
                        or runtime_row.role != occurrence.role
                        or runtime_row.replicate_ordinal
                        != occurrence.replicate_ordinal
                        or (
                            runtime_row.control_candidate["force_p_gain"],
                            runtime_row.control_candidate["force_i_gain"],
                            runtime_row.control_candidate["force_damping"],
                            runtime_row.control_candidate.get(
                                "normal_filter_tau_s", 0.35
                            ),
                        )
                        != (
                            candidate.force_p_gain,
                            candidate.force_i_gain,
                            candidate.force_damping,
                            candidate.normal_filter_tau_s,
                        )
                    ):
                        raise BatchProducerError(
                            "PRODUCTION_CLOSURE_INVALID",
                            f"runtime batch {sequence} differs from candidate plan",
                        )
                state = journal.state()
                if (
                    not state.complete
                    or not state.result_published
                    or state.unpublished_trial_brief_row_indices
                    or journal.verified_exit_code() != 0
                ):
                    raise BatchProducerError(
                        "PRODUCTION_CLOSURE_INVALID",
                        f"runtime batch {sequence} is not durably complete",
                    )
                result = read_strict_json(
                    journal.result_path, role=f"runtime BatchResult {sequence}"
                )
                result_sha = _file_sha256(
                    journal.result_path, role=f"runtime BatchResult {sequence}"
                )
                if (
                    not isinstance(result, Mapping)
                    or result.get("batch_uid") != identity.batch_uid
                    or result.get("row_count") != R008_BATCH_SIZE
                    or result.get("exit_code") != 0
                ):
                    raise BatchProducerError(
                        "PRODUCTION_CLOSURE_INVALID",
                        f"runtime BatchResult {sequence} identity differs",
                    )
                batch = _VerifiedRuntimeBatch(
                    sequence,
                    identity,
                    state,
                    _json_copy(result),
                    journal.result_path.resolve(),
                    result_sha,
                )
                batches[sequence] = batch
        except BatchProducerError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise BatchProducerError(
                "PRODUCTION_CLOSURE_INVALID", f"runtime batch verification failed: {exc}"
            ) from exc
        expected_sequences = set(range(1, plan.revision + 1))
        if set(batches) != expected_sequences:
            raise BatchProducerError(
                "PRODUCTION_CLOSURE_INVALID", "runtime batch sequences are not exact and contiguous"
            )
        store_root = epoch_root / "store"
        for sequence in sorted(batches):
            batch = batches[sequence]
            for identity_row, state_row in zip(
                batch.identity.rows, batch.state.rows, strict=True
            ):
                if (
                    state_row.trial_uid is None
                    or state_row.immutable_bundle_sha256 is None
                    or not isinstance(state_row.optimizer_eligible, bool)
                    or state_row.trial_uid in runtime_rows
                ):
                    raise BatchProducerError(
                        "PRODUCTION_CLOSURE_INVALID", "completed runtime row evidence differs"
                    )
                bundle_path = (
                    store_root
                    / "trials"
                    / state_row.trial_uid
                    / "immutable_trial_bundle.json"
                )
                if _file_sha256(bundle_path, role="immutable trial bundle") != (
                    state_row.immutable_bundle_sha256
                ):
                    raise BatchProducerError(
                        "PRODUCTION_CLOSURE_INVALID", "runtime bundle digest differs"
                    )
                runtime_rows[state_row.trial_uid] = (
                    identity_row.control_candidate_uid,
                    state_row.immutable_bundle_sha256,
                    state_row.optimizer_eligible,
                )
                ordered_trials.append(state_row.trial_uid)
        return batches, runtime_rows, tuple(ordered_trials)

    def _load_truth(self, plan: CandidateBatchPlan) -> _ProductionTruth:
        epoch_root = _active_epoch_root(self.paths.root)
        campaign_path = epoch_root / "store" / "campaign.json"
        if campaign_path.is_symlink() or not campaign_path.is_file():
            raise BatchProducerError(
                "PRODUCTION_HISTORY_INVALID", "campaign store manifest is missing"
            )
        batches, runtime_rows, ordered_trials = self._verified_runtime_batches(
            epoch_root=epoch_root, plan=plan
        )
        try:
            history = CampaignStore(epoch_root / "store").read_resume_history()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise BatchProducerError(
                "PRODUCTION_HISTORY_INVALID", f"cold-read history failed: {exc}"
            ) from exc
        if tuple(row.get("trial_uid") for row in history) != ordered_trials:
            raise BatchProducerError(
                "PRODUCTION_HISTORY_INVALID", "cold-read history order differs from runtime batches"
            )
        observations, material = _observations_from_history(
            history,
            campaign_id=self.campaign_id,
            runtime_rows=runtime_rows,
        )
        return _ProductionTruth(
            observations=observations,
            observation_material=material,
            observation_material_sha256=_json_sha256(list(material)),
            history_by_trial={row["trial_uid"]: row for row in history},
            batches=batches,
        )

    def _batch_a_closure(
        self,
        *,
        target_revision: int,
        plan: CandidateBatchPlan,
        truth: _ProductionTruth,
    ) -> BatchAClosure:
        batch_a_sequence = target_revision - 1
        prior = plan.payload["batches"][batch_a_sequence - 1]
        expected_roles = ["supercycle_anchor", *("qlognei_a",) * 4]
        if (
            prior["source"] != PRODUCTION_BATCH_A_SOURCE
            or [row["role"] for row in prior["occurrences"]] != expected_roles
        ):
            raise BatchProducerError(
                "BATCH_A_CLOSURE_NOT_PROVABLE",
                "preceding odd revision is not the production Batch A policy",
            )
        batch = truth.batches.get(batch_a_sequence)
        if batch is None or _file_sha256(
            batch.result_path, role="Batch A result"
        ) != batch.result_sha256:
            raise BatchProducerError(
                "BATCH_A_CLOSURE_NOT_PROVABLE", "Batch A result bytes are absent or changed"
            )
        sealed_rows = []
        batch_trial_uids = []
        for result_row in batch.result.get("rows", ()):
            if not isinstance(result_row, Mapping):
                raise BatchProducerError(
                    "BATCH_A_CLOSURE_NOT_PROVABLE", "Batch A result row is invalid"
                )
            trial_uid = result_row.get("trial_uid")
            history_row = truth.history_by_trial.get(trial_uid)
            if (
                not isinstance(history_row, Mapping)
                or result_row.get("immutable_bundle_sha256") is None
            ):
                raise BatchProducerError(
                    "BATCH_A_CLOSURE_NOT_PROVABLE",
                    "Batch A result lacks an exact cold-read history row",
                )
            batch_trial_uids.append(trial_uid)
            sealed_rows.append(
                {
                    "row_index": result_row.get("row_index"),
                    "trial_uid": trial_uid,
                    "immutable_bundle_sha256": _sha256(
                        result_row.get("immutable_bundle_sha256"),
                        name="Batch A immutable bundle",
                    ),
                    "history_identity": _sha256(
                        history_row.get("history_identity"),
                        name="Batch A history identity",
                    ),
                }
            )
        if (
            len(sealed_rows) != R008_BATCH_SIZE
            or [row["row_index"] for row in sealed_rows]
            != list(range(1, R008_BATCH_SIZE + 1))
            or len(set(batch_trial_uids)) != R008_BATCH_SIZE
        ):
            raise BatchProducerError(
                "BATCH_A_CLOSURE_NOT_PROVABLE", "Batch A sealed bundle set differs"
            )
        material_trial_uids = {
            row["trial_uid"] for row in truth.observation_material
        }
        if not set(batch_trial_uids).issubset(material_trial_uids):
            raise BatchProducerError(
                "BATCH_A_CLOSURE_NOT_PROVABLE",
                "Batch A observations are absent from exact GP material",
            )
        sealed_sha = _json_sha256(
            {
                "schema": "step5d.autotune-v3/sealed-bundle-set-v1",
                "batch_uid": batch.identity.batch_uid,
                "logical_batch_sequence": batch_a_sequence,
                "rows": sealed_rows,
            }
        )
        gp_update_sha = _json_sha256(
            {
                "schema": "step5d.autotune-v3/gp-update-material-v1",
                "batch_result_sha256": batch.result_sha256,
                "sealed_bundle_set_sha256": sealed_sha,
                "observation_material": list(truth.observation_material),
            }
        )
        return BatchAClosure(
            batch_sequence=batch_a_sequence,
            batch_uid=batch.identity.batch_uid,
            batch_result_path=str(batch.result_path),
            batch_result_sha256=batch.result_sha256,
            sealed_bundle_set_sha256=sealed_sha,
            cold_read_verified=True,
            observation_material=truth.observation_material,
            observation_material_sha256=truth.observation_material_sha256,
            gp_update_sha256=gp_update_sha,
        )

    def __call__(self, target_revision: int) -> BatchProposal:
        if (
            isinstance(target_revision, bool)
            or not isinstance(target_revision, int)
            or target_revision < 3
        ):
            raise BatchProducerError(
                "POLICY_MISMATCH", "production provider starts at revision 3"
            )
        try:
            plan = load_plan(self.paths.candidate_plan, campaign_id=self.campaign_id)
        except (OSError, ValueError) as exc:
            raise BatchProducerError("CANDIDATE_STATE_INVALID", str(exc)) from exc
        if (
            plan.payload["schema_version"] != SCHEMA_VERSION_ROLLING_V2
            or plan.revision != target_revision - 1
            or plan.lifecycle is not PlanLifecycle.OPEN_EMPTY
        ):
            raise BatchProducerError(
                "CANDIDATE_STATE_INVALID", "production proposal target is not current OPEN_EMPTY"
            )
        truth = self._load_truth(plan)
        base_evidence = {
            "observation_schema": OBSERVATION_MATERIAL_SCHEMA,
            "observation_count": len(truth.observations),
            "observation_material_sha256": truth.observation_material_sha256,
        }
        if not bo_gate(truth.observations):
            return BatchProposal(
                recovery_batch(target_revision),
                PRODUCTION_RECOVERY_SOURCE,
                "recovery_batch",
                {**base_evidence, "bo_gate": False},
            )
        try:
            if target_revision % 2 == 1:
                if self.optimizer_client is None:
                    raise BatchProducerError(
                        "OPTIMIZER_CLIENT_REQUIRED",
                        "BO-gated production proposals require the isolated optimizer",
                    )
                occurrences, optimizer_evidence = self.optimizer_client.propose(
                    mode="rolling_batch_a",
                    observations=truth.observations,
                    catalog=self.catalog,
                    sequence=target_revision,
                )
                return BatchProposal(
                    occurrences,
                    PRODUCTION_BATCH_A_SOURCE,
                    "supercycle_batch_a",
                    {
                        **base_evidence,
                        "bo_gate": True,
                        "optimizer": optimizer_evidence,
                    },
                )
            closure = self._batch_a_closure(
                target_revision=target_revision,
                plan=plan,
                truth=truth,
            )
            if self.optimizer_client is None:
                raise BatchProducerError(
                    "OPTIMIZER_CLIENT_REQUIRED",
                    "BO-gated production proposals require the isolated optimizer",
                )
            occurrences, optimizer_evidence = self.optimizer_client.propose(
                mode="rolling_batch_b",
                observations=truth.observations,
                catalog=self.catalog,
                sequence=target_revision,
                batch_a_closure=closure.policy_payload(),
            )
            return BatchProposal(
                occurrences,
                PRODUCTION_BATCH_B_SOURCE,
                "supercycle_batch_b_after_gp_update",
                {
                    **base_evidence,
                    "bo_gate": True,
                    "batch_a_closure": closure.evidence(),
                    "optimizer": optimizer_evidence,
                },
            )
        except BatchProducerError:
            raise
        except (RuntimeError, TypeError, ValueError) as exc:
            raise BatchProducerError(
                "OPTIMIZER_POLICY_FAILED", f"production optimizer failed: {exc}"
            ) from exc


class RollingBatchProducer:
    """Campaign-local watcher which appends only when the plan is ``OPEN_EMPTY``."""

    def __init__(
        self,
        *,
        campaign_root: Path,
        campaign_id: str,
        binding_fingerprint: str,
        launch_profile: LaunchProfile,
        instance_id: str | None = None,
        crash_hook: CrashHook | None = None,
        authority_guard: Callable[[], Any] | None = None,
    ) -> None:
        if not isinstance(campaign_id, str) or not campaign_id.strip():
            raise BatchProducerError("IDENTITY_INVALID", "campaign_id must be non-empty")
        self.paths = CampaignPaths(campaign_root)
        self.campaign_id = campaign_id
        self.binding_fingerprint = _sha256(
            binding_fingerprint, name="binding_fingerprint"
        )
        self.launch_profile = launch_profile
        self.launch_profile_fingerprint = _sha256(
            launch_profile.fingerprint, name="launch_profile fingerprint"
        )
        if instance_id is None:
            instance_id = secrets.token_hex(16)
        if (
            not isinstance(instance_id, str)
            or len(instance_id) != 32
            or any(character not in _SHA256_HEX for character in instance_id)
        ):
            raise BatchProducerError(
                "IDENTITY_INVALID", "instance_id must be 32 lowercase hexadecimal characters"
            )
        self.instance_id = instance_id
        self.crash_hook = crash_hook
        self.authority_guard = authority_guard

    def _assert_authority(self) -> None:
        if self.authority_guard is not None:
            self.authority_guard()

    @property
    def intent_path(self) -> Path:
        return self.paths.control / "batch_producer_intent.json"

    @property
    def heartbeat_path(self) -> Path:
        return self.paths.control / "batch_producer_heartbeat.json"

    @property
    def evidence_path(self) -> Path:
        return self.paths.control / "batch_producer_evidence.json"

    def _crash_point(self, stage: str) -> None:
        if self.crash_hook is not None:
            self.crash_hook(stage)

    def _read_overlay_plan(self) -> dict[str, Any]:
        try:
            payload = read_strict_json(
                self.paths.trial_overlays, role="V3 trial-overlay plan"
            )
        except (OSError, StateError) as exc:
            raise BatchProducerError("OVERLAY_STATE_INVALID", str(exc)) from exc
        required = {
            "schema",
            "revision",
            "candidate_count",
            "launch_profile_fingerprint",
            "fingerprint",
            "batches",
        }
        if not isinstance(payload, dict) or set(payload) != required:
            raise BatchProducerError(
                "OVERLAY_STATE_INVALID", "trial-overlay plan fields differ"
            )
        if payload["schema"] != OVERLAY_PLAN_SCHEMA:
            raise BatchProducerError(
                "OVERLAY_STATE_INVALID", "trial-overlay plan schema differs"
            )
        if payload["launch_profile_fingerprint"] != self.launch_profile_fingerprint:
            raise BatchProducerError(
                "BINDING_MISMATCH", "trial-overlay launch-profile fingerprint differs"
            )
        revision = payload["revision"]
        batches = payload["batches"]
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 0
            or not isinstance(batches, list)
            or revision != len(batches)
        ):
            raise BatchProducerError(
                "OVERLAY_STATE_INVALID", "trial-overlay revision is invalid"
            )
        if payload["candidate_count"] != revision * R008_BATCH_SIZE:
            raise BatchProducerError(
                "OVERLAY_STATE_INVALID", "trial-overlay candidate count differs"
            )
        expected_fingerprint = _overlay_plan_fingerprint(
            launch_profile_fingerprint=self.launch_profile_fingerprint,
            batches=batches,
        )
        if payload["fingerprint"] != expected_fingerprint:
            raise BatchProducerError(
                "OVERLAY_FINGERPRINT_MISMATCH", "trial-overlay fingerprint differs"
            )
        return payload

    def _validate_overlay_prefix(
        self,
        plan: CandidateBatchPlan,
        overlay_plan: Mapping[str, Any],
    ) -> None:
        revision = overlay_plan["revision"]
        if revision > plan.revision:
            raise BatchProducerError(
                "REVISION_DIVERGENCE", "trial-overlay plan is ahead of candidate plan"
            )
        if len(plan.occurrences) < revision:
            raise BatchProducerError(
                "CANDIDATE_STATE_INVALID", "candidate plan lacks rolling occurrences"
            )
        for batch_index, overlay_batch in enumerate(overlay_plan["batches"], start=1):
            expected_batch_fields = {"batch_id", "source", "trials"}
            if (
                not isinstance(overlay_batch, dict)
                or set(overlay_batch) != expected_batch_fields
                or overlay_batch["batch_id"] != batch_index
                or overlay_batch["source"]
                != plan.payload["batches"][batch_index - 1]["source"]
                or not isinstance(overlay_batch["trials"], list)
                or len(overlay_batch["trials"]) != R008_BATCH_SIZE
            ):
                raise BatchProducerError(
                    "OVERLAY_COHERENCE_MISMATCH",
                    f"trial-overlay batch {batch_index} differs from candidate plan",
                )
            occurrences = plan.occurrences[batch_index - 1]
            for row_index, (trial, occurrence) in enumerate(
                zip(overlay_batch["trials"], occurrences, strict=True), start=1
            ):
                required_trial_fields = {
                    "occurrence_uid",
                    "transport_candidate_uid",
                    "control_candidate_uid",
                    "normalized_overlay_sha256",
                    "overlay",
                }
                if not isinstance(trial, dict) or set(trial) != required_trial_fields:
                    raise BatchProducerError(
                        "OVERLAY_COHERENCE_MISMATCH",
                        f"trial-overlay row {batch_index}:{row_index} fields differ",
                    )
                try:
                    normalized = normalize_trial_overlay(
                        trial["overlay"], profile=self.launch_profile
                    )
                    normalized_sha = normalized_overlay_sha256(
                        self.launch_profile, normalized
                    )
                except (RuntimeError, TypeError, ValueError) as exc:
                    raise BatchProducerError(
                        "OVERLAY_COHERENCE_MISMATCH",
                        f"trial-overlay row {batch_index}:{row_index} is invalid: {exc}",
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
                candidate = occurrence.candidate
                candidate_coordinates = (
                    candidate.force_p_gain,
                    candidate.force_i_gain,
                    candidate.force_damping,
                    candidate.normal_filter_tau_s,
                )
                overlay_coordinates = (
                    normalized["force_p_gain"],
                    normalized["force_i_gain"],
                    normalized["force_damping"],
                    normalized.get("normal_filter_tau_s", 0.35),
                )
                if (
                    trial["overlay"] != normalized
                    or trial["normalized_overlay_sha256"] != normalized_sha
                    or actual_identity != expected_identity
                    or normalized["control_candidate_uid"] != expected_identity[2]
                    or overlay_coordinates != candidate_coordinates
                ):
                    raise BatchProducerError(
                        "OVERLAY_COHERENCE_MISMATCH",
                        f"trial-overlay row {batch_index}:{row_index} identity differs",
                    )

    def _load_plan(self) -> CandidateBatchPlan:
        try:
            plan = load_plan(self.paths.candidate_plan, campaign_id=self.campaign_id)
        except (OSError, ValueError) as exc:
            raise BatchProducerError("CANDIDATE_STATE_INVALID", str(exc)) from exc
        if plan.payload["schema_version"] != SCHEMA_VERSION_ROLLING_V2:
            raise BatchProducerError(
                "CANDIDATE_STATE_INVALID", "producer requires the rolling-v2 plan schema"
            )
        return plan

    def _validate_existing_evidence_binding(self) -> None:
        if not self.evidence_path.exists() and not self.evidence_path.is_symlink():
            return
        try:
            payload = read_strict_json(self.evidence_path, role="batch-producer evidence")
        except (OSError, StateError) as exc:
            raise BatchProducerError("EVIDENCE_INVALID", str(exc)) from exc
        required = {
            "schema",
            "created_at",
            "campaign_id",
            "binding_fingerprint",
            "launch_profile_fingerprint",
            "instance_id",
            "action",
            "target_revision",
            "source",
            "policy",
            "policy_evidence",
            "proposal_fingerprint",
            "candidate_plan_sha256",
            "overlay_plan_sha256",
            "coherence_sha256",
        }
        if not isinstance(payload, dict) or set(payload) != required:
            raise BatchProducerError("EVIDENCE_INVALID", "batch-producer evidence fields differ")
        if payload["schema"] != EVIDENCE_SCHEMA:
            raise BatchProducerError("EVIDENCE_INVALID", "batch-producer evidence schema differs")
        if (
            payload["campaign_id"] != self.campaign_id
            or payload["binding_fingerprint"] != self.binding_fingerprint
            or payload["launch_profile_fingerprint"] != self.launch_profile_fingerprint
        ):
            raise BatchProducerError(
                "BINDING_MISMATCH", "existing batch-producer evidence binding differs"
            )
        for name in (
            "proposal_fingerprint",
            "candidate_plan_sha256",
            "overlay_plan_sha256",
            "coherence_sha256",
        ):
            _sha256(payload[name], name=f"evidence {name}")

    def _proposal_for(
        self,
        target_revision: int,
        *,
        proposal: BatchProposal | None,
        proposal_provider: ProposalProvider | None,
    ) -> BatchProposal:
        if target_revision == 2:
            if proposal is not None:
                raise BatchProducerError(
                    "POLICY_MISMATCH", "revision 2 is reserved for initialization batch 2"
                )
            return BatchProposal(
                initialization_batch(2),
                "batch_producer.initialization_v2",
                "initialization_batch_2",
                {"target_revision": 2},
            )
        selected = proposal
        if selected is None and proposal_provider is not None:
            try:
                selected = proposal_provider(target_revision)
            except BatchProducerError:
                raise
            except Exception as exc:
                raise BatchProducerError(
                    "OPTIMIZER_POLICY_FAILED", f"proposal provider failed: {exc}"
                ) from exc
        if selected is not None:
            if not isinstance(selected, BatchProposal):
                raise BatchProducerError(
                    "POLICY_MISMATCH", "proposal provider returned the wrong type"
                )
            return selected
        return BatchProposal(
            recovery_batch(target_revision),
            "batch_producer.recovery_v1",
            "recovery_batch",
            {"target_revision": target_revision, "fallback": True},
        )

    def _prepare_intent(
        self,
        *,
        plan: CandidateBatchPlan,
        overlay_plan: Mapping[str, Any],
        proposal: BatchProposal,
    ) -> dict[str, Any]:
        target_revision = plan.revision + 1
        if not isinstance(proposal.source, str) or not proposal.source.strip():
            raise BatchProducerError("POLICY_MISMATCH", "proposal source must be non-empty")
        if not isinstance(proposal.policy, str) or not proposal.policy.strip():
            raise BatchProducerError("POLICY_MISMATCH", "proposal policy must be non-empty")
        if not isinstance(proposal.evidence, Mapping):
            raise BatchProducerError("POLICY_MISMATCH", "proposal evidence must be an object")
        policy_evidence = _json_copy(dict(proposal.evidence))
        if len(proposal.occurrences) != R008_BATCH_SIZE:
            raise BatchProducerError("POLICY_MISMATCH", "proposal must contain exactly five rows")

        candidate_rows: list[dict[str, Any]] = []
        overlay_rows: list[dict[str, Any]] = []
        occurrence_ids: set[str] = set()
        transport_ids: set[str] = set()
        for expected_row, raw_occurrence in enumerate(proposal.occurrences, start=1):
            if not isinstance(raw_occurrence, PlannedOccurrence):
                raise BatchProducerError(
                    "POLICY_MISMATCH", f"proposal row {expected_row} is not a PlannedOccurrence"
                )
            if (
                raw_occurrence.logical_batch_sequence != target_revision
                or raw_occurrence.plan_revision != target_revision
                or raw_occurrence.row_index != expected_row
            ):
                raise BatchProducerError(
                    "POLICY_MISMATCH",
                    f"proposal row {expected_row} sequence/revision/index differs",
                )
            overlay_input = {
                **DEFAULT_OVERLAY,
                "force_p_gain": raw_occurrence.candidate.force_p_gain,
                "force_i_gain": raw_occurrence.candidate.force_i_gain,
                "force_damping": raw_occurrence.candidate.force_damping,
                "normal_filter_tau_s": (
                    raw_occurrence.candidate.normal_filter_tau_s
                ),
            }
            overlay_input.pop("control_candidate_uid", None)
            try:
                overlay = normalize_trial_overlay(
                    overlay_input, profile=self.launch_profile
                )
            except (RuntimeError, TypeError, ValueError) as exc:
                raise BatchProducerError(
                    "POLICY_MISMATCH", f"proposal row {expected_row} is outside the overlay policy: {exc}"
                ) from exc
            occurrence = raw_occurrence.bind_control_candidate_uid(
                overlay["control_candidate_uid"]
            )
            occurrence_uid = str(occurrence.occurrence_uid)
            transport_uid = str(occurrence.transport_candidate_uid)
            control_uid = str(occurrence.control_candidate_uid)
            if occurrence_uid in occurrence_ids or transport_uid in transport_ids:
                raise BatchProducerError("POLICY_MISMATCH", "proposal repeats a row identity")
            occurrence_ids.add(occurrence_uid)
            transport_ids.add(transport_uid)
            candidate_rows.append(
                {
                    "candidate": candidate_log2_payload(occurrence.candidate),
                    "occurrence_uid": occurrence_uid,
                    "transport_candidate_uid": transport_uid,
                    "control_candidate_uid": control_uid,
                    "role": occurrence.selection_role,
                    "replicate_ordinal": occurrence.replicate_ordinal,
                }
            )
            overlay_rows.append(
                {
                    "occurrence_uid": occurrence_uid,
                    "transport_candidate_uid": transport_uid,
                    "control_candidate_uid": control_uid,
                    "normalized_overlay_sha256": normalized_overlay_sha256(
                        self.launch_profile, overlay
                    ),
                    "overlay": overlay,
                }
            )

        candidate_batch = {
            "batch_id": target_revision,
            "plan_revision": target_revision,
            "source": proposal.source,
            "occurrences": candidate_rows,
        }
        overlay_batch = {
            "batch_id": target_revision,
            "source": proposal.source,
            "trials": overlay_rows,
        }
        precondition = {
            "candidate_revision": plan.revision,
            "overlay_revision": overlay_plan["revision"],
            "candidate_payload_sha256": _json_sha256(plan.payload),
            "overlay_payload_sha256": _json_sha256(overlay_plan),
            "candidate_file_sha256": _file_sha256(
                self.paths.candidate_plan, role="candidate plan"
            ),
            "overlay_file_sha256": _file_sha256(
                self.paths.trial_overlays, role="trial-overlay plan"
            ),
        }
        proposal_material = {
            "campaign_id": self.campaign_id,
            "binding_fingerprint": self.binding_fingerprint,
            "launch_profile_fingerprint": self.launch_profile_fingerprint,
            "target_revision": target_revision,
            "source": proposal.source,
            "policy": proposal.policy,
            "policy_evidence": policy_evidence,
            "candidate_batch": candidate_batch,
            "overlay_batch": overlay_batch,
            "precondition": precondition,
        }
        return {
            "schema": INTENT_SCHEMA,
            **proposal_material,
            "proposal_fingerprint": _json_sha256(proposal_material),
            "created_at": utc_now(),
        }

    def _read_intent(self) -> dict[str, Any] | None:
        if not self.intent_path.exists() and not self.intent_path.is_symlink():
            return None
        try:
            intent = read_strict_json(self.intent_path, role="batch-producer intent")
        except (OSError, StateError) as exc:
            raise BatchProducerError("INTENT_INVALID", str(exc)) from exc
        required = {
            "schema",
            "campaign_id",
            "binding_fingerprint",
            "launch_profile_fingerprint",
            "target_revision",
            "source",
            "policy",
            "policy_evidence",
            "candidate_batch",
            "overlay_batch",
            "precondition",
            "proposal_fingerprint",
            "created_at",
        }
        if not isinstance(intent, dict) or set(intent) != required:
            raise BatchProducerError("INTENT_INVALID", "batch-producer intent fields differ")
        if intent["schema"] != INTENT_SCHEMA:
            raise BatchProducerError("INTENT_INVALID", "batch-producer intent schema differs")
        if (
            intent["campaign_id"] != self.campaign_id
            or intent["binding_fingerprint"] != self.binding_fingerprint
            or intent["launch_profile_fingerprint"] != self.launch_profile_fingerprint
        ):
            raise BatchProducerError("BINDING_MISMATCH", "batch-producer intent binding differs")
        material = {
            key: intent[key]
            for key in (
                "campaign_id",
                "binding_fingerprint",
                "launch_profile_fingerprint",
                "target_revision",
                "source",
                "policy",
                "policy_evidence",
                "candidate_batch",
                "overlay_batch",
                "precondition",
            )
        }
        if intent["proposal_fingerprint"] != _json_sha256(material):
            raise BatchProducerError("INTENT_FINGERPRINT_MISMATCH", "intent fingerprint differs")
        self._validate_intent_rows(intent)
        return intent

    def _validate_intent_rows(self, intent: Mapping[str, Any]) -> None:
        target = intent["target_revision"]
        if isinstance(target, bool) or not isinstance(target, int) or target < 2:
            raise BatchProducerError("INTENT_INVALID", "intent target revision is invalid")
        candidate_batch = intent["candidate_batch"]
        overlay_batch = intent["overlay_batch"]
        if (
            not isinstance(candidate_batch, dict)
            or set(candidate_batch) != {"batch_id", "plan_revision", "source", "occurrences"}
            or not isinstance(overlay_batch, dict)
            or set(overlay_batch) != {"batch_id", "source", "trials"}
            or candidate_batch["batch_id"] != target
            or candidate_batch["plan_revision"] != target
            or overlay_batch["batch_id"] != target
            or candidate_batch["source"] != intent["source"]
            or overlay_batch["source"] != intent["source"]
            or not isinstance(candidate_batch["occurrences"], list)
            or not isinstance(overlay_batch["trials"], list)
            or len(candidate_batch["occurrences"]) != R008_BATCH_SIZE
            or len(overlay_batch["trials"]) != R008_BATCH_SIZE
        ):
            raise BatchProducerError("INTENT_INVALID", "intent batch structure differs")
        for index, (candidate_row, overlay_row) in enumerate(
            zip(candidate_batch["occurrences"], overlay_batch["trials"], strict=True),
            start=1,
        ):
            if not isinstance(candidate_row, dict) or set(candidate_row) != {
                "candidate",
                "occurrence_uid",
                "transport_candidate_uid",
                "control_candidate_uid",
                "role",
                "replicate_ordinal",
            }:
                raise BatchProducerError("INTENT_INVALID", f"intent candidate row {index} differs")
            if not isinstance(overlay_row, dict) or set(overlay_row) != {
                "occurrence_uid",
                "transport_candidate_uid",
                "control_candidate_uid",
                "normalized_overlay_sha256",
                "overlay",
            }:
                raise BatchProducerError("INTENT_INVALID", f"intent overlay row {index} differs")
            try:
                candidate = candidate_from_log2_payload(candidate_row["candidate"])
                normalized = normalize_trial_overlay(
                    overlay_row["overlay"], profile=self.launch_profile
                )
            except (RuntimeError, TypeError, ValueError) as exc:
                raise BatchProducerError(
                    "INTENT_INVALID", f"intent row {index} cannot be decoded: {exc}"
                ) from exc
            candidate_identity = (
                candidate_row["occurrence_uid"],
                candidate_row["transport_candidate_uid"],
                candidate_row["control_candidate_uid"],
            )
            overlay_identity = (
                overlay_row["occurrence_uid"],
                overlay_row["transport_candidate_uid"],
                overlay_row["control_candidate_uid"],
            )
            if (
                candidate_identity != overlay_identity
                or overlay_row["overlay"] != normalized
                or overlay_row["normalized_overlay_sha256"]
                != normalized_overlay_sha256(self.launch_profile, normalized)
                or normalized["control_candidate_uid"] != candidate_row["control_candidate_uid"]
                or (
                    normalized["force_p_gain"],
                    normalized["force_i_gain"],
                    normalized["force_damping"],
                    normalized.get("normal_filter_tau_s", 0.35),
                )
                != (
                    candidate.force_p_gain,
                    candidate.force_i_gain,
                    candidate.force_damping,
                    candidate.normal_filter_tau_s,
                )
            ):
                raise BatchProducerError("INTENT_INVALID", f"intent row {index} is incoherent")
        precondition = intent["precondition"]
        if not isinstance(precondition, dict) or set(precondition) != {
            "candidate_revision",
            "overlay_revision",
            "candidate_payload_sha256",
            "overlay_payload_sha256",
            "candidate_file_sha256",
            "overlay_file_sha256",
        }:
            raise BatchProducerError("INTENT_INVALID", "intent precondition fields differ")
        if (
            precondition["candidate_revision"] != target - 1
            or precondition["overlay_revision"] != target - 1
        ):
            raise BatchProducerError("INTENT_INVALID", "intent precondition revision differs")
        for key in precondition:
            if key.endswith("sha256"):
                _sha256(precondition[key], name=f"intent {key}")

    @staticmethod
    def _intent_occurrences(intent: Mapping[str, Any]) -> tuple[_IntentOccurrence, ...]:
        target = intent["target_revision"]
        return tuple(
            _IntentOccurrence(
                logical_batch_sequence=target,
                row_index=index,
                candidate=candidate_from_log2_payload(row["candidate"]),
                plan_revision=target,
                selection_role=row["role"],
                replicate_ordinal=row["replicate_ordinal"],
                occurrence_uid=row["occurrence_uid"],
                transport_candidate_uid=row["transport_candidate_uid"],
                control_candidate_uid=row["control_candidate_uid"],
            )
            for index, row in enumerate(
                intent["candidate_batch"]["occurrences"], start=1
            )
        )

    @staticmethod
    def _candidate_predecessor(plan: CandidateBatchPlan) -> dict[str, Any]:
        predecessor = _json_copy(plan.payload)
        predecessor["revision"] = plan.revision - 1
        predecessor["batches"] = predecessor["batches"][:-1]
        predecessor["closed"] = False
        predecessor["lifecycle"] = PlanLifecycle.OPEN_EMPTY.value
        predecessor["closure"] = None
        return predecessor

    def _overlay_predecessor(self, overlay_plan: Mapping[str, Any]) -> dict[str, Any]:
        return _overlay_plan_payload(
            launch_profile_fingerprint=self.launch_profile_fingerprint,
            batches=overlay_plan["batches"][:-1],
        )

    def _prove_intent_state(
        self,
        *,
        intent: Mapping[str, Any],
        plan: CandidateBatchPlan,
        overlay_plan: Mapping[str, Any],
    ) -> str:
        target = intent["target_revision"]
        before = target - 1
        candidate_revision = plan.revision
        overlay_revision = overlay_plan["revision"]
        if (candidate_revision, overlay_revision) == (before, before):
            if plan.lifecycle is not PlanLifecycle.OPEN_EMPTY:
                raise BatchProducerError(
                    "REVISION_DIVERGENCE", "intent predecessor is no longer OPEN_EMPTY"
                )
            precondition = intent["precondition"]
            if (
                _json_sha256(plan.payload) != precondition["candidate_payload_sha256"]
                or _json_sha256(overlay_plan) != precondition["overlay_payload_sha256"]
                or _file_sha256(self.paths.candidate_plan, role="candidate plan")
                != precondition["candidate_file_sha256"]
                or _file_sha256(self.paths.trial_overlays, role="trial-overlay plan")
                != precondition["overlay_file_sha256"]
            ):
                raise BatchProducerError(
                    "INTENT_PRECONDITION_MISMATCH", "intent predecessor fingerprint differs"
                )
            return "before_both"
        if (candidate_revision, overlay_revision) == (target, before):
            if plan.lifecycle is not PlanLifecycle.OPEN_READY:
                raise BatchProducerError(
                    "REVISION_DIVERGENCE", "candidate-only recovery is not OPEN_READY"
                )
            if (
                plan.payload["batches"][-1] != intent["candidate_batch"]
                or _json_sha256(self._candidate_predecessor(plan))
                != intent["precondition"]["candidate_payload_sha256"]
                or _json_sha256(overlay_plan)
                != intent["precondition"]["overlay_payload_sha256"]
                or _file_sha256(self.paths.trial_overlays, role="trial-overlay plan")
                != intent["precondition"]["overlay_file_sha256"]
            ):
                raise BatchProducerError(
                    "RECOVERY_NOT_PROVABLE", "candidate-only revision differs from intent"
                )
            return "candidate_only"
        if (candidate_revision, overlay_revision) == (target, target):
            if (
                plan.payload["batches"][-1] != intent["candidate_batch"]
                or overlay_plan["batches"][-1] != intent["overlay_batch"]
                or _json_sha256(self._candidate_predecessor(plan))
                != intent["precondition"]["candidate_payload_sha256"]
                or _json_sha256(self._overlay_predecessor(overlay_plan))
                != intent["precondition"]["overlay_payload_sha256"]
            ):
                raise BatchProducerError(
                    "RECOVERY_NOT_PROVABLE", "completed revision differs from intent"
                )
            return "after_both"
        raise BatchProducerError(
            "REVISION_DIVERGENCE",
            "only an exact zero- or one-revision intent recovery is supported",
        )

    def _append_overlay_from_intent(
        self, *, intent: Mapping[str, Any], overlay_plan: Mapping[str, Any]
    ) -> dict[str, Any]:
        target = intent["target_revision"]
        if overlay_plan["revision"] != target - 1:
            raise BatchProducerError(
                "REVISION_DIVERGENCE", "overlay append precondition differs"
            )
        payload = _overlay_plan_payload(
            launch_profile_fingerprint=self.launch_profile_fingerprint,
            batches=[*overlay_plan["batches"], intent["overlay_batch"]],
        )
        try:
            self._assert_authority()
            atomic_json(self.paths.trial_overlays, payload)
        except (OSError, StateError) as exc:
            raise BatchProducerError("OVERLAY_WRITE_FAILED", str(exc)) from exc
        return self._read_overlay_plan()

    def _write_evidence(
        self,
        *,
        intent: Mapping[str, Any],
        plan: CandidateBatchPlan,
        overlay_plan: Mapping[str, Any],
        recovered: bool,
    ) -> tuple[dict[str, Any], str]:
        coherence_material = [
            {
                "occurrence_uid": trial["occurrence_uid"],
                "transport_candidate_uid": trial["transport_candidate_uid"],
                "control_candidate_uid": trial["control_candidate_uid"],
                "normalized_overlay_sha256": trial["normalized_overlay_sha256"],
            }
            for batch in overlay_plan["batches"]
            for trial in batch["trials"]
        ]
        payload = {
            "schema": EVIDENCE_SCHEMA,
            "created_at": utc_now(),
            "campaign_id": self.campaign_id,
            "binding_fingerprint": self.binding_fingerprint,
            "launch_profile_fingerprint": self.launch_profile_fingerprint,
            "instance_id": self.instance_id,
            "action": "recovered_exact_revision" if recovered else "published_revision",
            "target_revision": intent["target_revision"],
            "source": intent["source"],
            "policy": intent["policy"],
            "policy_evidence": intent["policy_evidence"],
            "proposal_fingerprint": intent["proposal_fingerprint"],
            "candidate_plan_sha256": _json_sha256(plan.payload),
            "overlay_plan_sha256": _json_sha256(overlay_plan),
            "coherence_sha256": _json_sha256(coherence_material),
        }
        try:
            self._assert_authority()
            atomic_json(self.evidence_path, payload)
        except (OSError, StateError) as exc:
            raise BatchProducerError("EVIDENCE_WRITE_FAILED", str(exc)) from exc
        return payload, _json_sha256(payload)

    def _clear_intent(self) -> None:
        self._assert_authority()
        if self.intent_path.is_symlink() or not self.intent_path.is_file():
            raise BatchProducerError("INTENT_INVALID", "intent must be a real regular file")
        try:
            self.intent_path.unlink()
            descriptor = os.open(
                self.intent_path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise BatchProducerError("INTENT_CLEAR_FAILED", str(exc)) from exc

    def _heartbeat(
        self,
        *,
        phase: str,
        plan_revision: int | None,
        overlay_revision: int | None,
        lifecycle: str | None,
        reason_code: str,
        detail: str,
        evidence_sha256: str | None,
    ) -> dict[str, Any]:
        payload = {
            "schema": HEARTBEAT_SCHEMA,
            "updated_at": utc_now(),
            "pid": os.getpid(),
            "instance_id": self.instance_id,
            "campaign_id": self.campaign_id,
            "binding_fingerprint": self.binding_fingerprint,
            "launch_profile_fingerprint": self.launch_profile_fingerprint,
            "phase": phase,
            "plan_revision": plan_revision,
            "overlay_revision": overlay_revision,
            "lifecycle": lifecycle,
            "reason_code": reason_code,
            "detail": detail,
            "evidence_path": str(self.evidence_path) if evidence_sha256 else None,
            "evidence_sha256": evidence_sha256,
        }
        try:
            self._assert_authority()
            atomic_json(self.heartbeat_path, payload)
        except (OSError, StateError) as exc:
            raise BatchProducerError("HEARTBEAT_WRITE_FAILED", str(exc)) from exc
        return payload

    def _failed_heartbeat(self, error: BatchProducerError) -> None:
        plan_revision: int | None = None
        overlay_revision: int | None = None
        lifecycle: str | None = None
        try:
            plan = self._load_plan()
            plan_revision = plan.revision
            lifecycle = plan.lifecycle.value
        except BatchProducerError:
            pass
        try:
            overlay = self._read_overlay_plan()
            overlay_revision = overlay["revision"]
        except BatchProducerError:
            pass
        try:
            self._heartbeat(
                phase="failed_closed",
                plan_revision=plan_revision,
                overlay_revision=overlay_revision,
                lifecycle=lifecycle,
                reason_code=error.reason_code,
                detail=error.detail,
                evidence_sha256=None,
            )
        except BatchProducerError:
            pass

    def _apply_intent(
        self, intent: Mapping[str, Any]
    ) -> ProducerSnapshot:
        plan = self._load_plan()
        overlay_plan = self._read_overlay_plan()
        self._validate_overlay_prefix(plan, overlay_plan)
        state = self._prove_intent_state(
            intent=intent, plan=plan, overlay_plan=overlay_plan
        )
        recovered = state != "before_both"
        if state == "before_both":
            try:
                self._assert_authority()
                plan = append_r008_batch(
                    self.paths.candidate_plan,
                    occurrences=self._intent_occurrences(intent),
                    source=intent["source"],
                )
            except (OSError, TypeError, ValueError) as exc:
                raise BatchProducerError("CANDIDATE_WRITE_FAILED", str(exc)) from exc
            self._crash_point("candidate_appended")
            overlay_plan = self._read_overlay_plan()
            state = "candidate_only"
        if state == "candidate_only":
            overlay_plan = self._append_overlay_from_intent(
                intent=intent, overlay_plan=overlay_plan
            )
            self._crash_point("overlay_appended")
        plan = self._load_plan()
        overlay_plan = self._read_overlay_plan()
        if plan.revision != intent["target_revision"] or overlay_plan["revision"] != plan.revision:
            raise BatchProducerError(
                "REVISION_DIVERGENCE", "published candidate and overlay revisions differ"
            )
        self._validate_overlay_prefix(plan, overlay_plan)
        evidence, evidence_sha = self._write_evidence(
            intent=intent,
            plan=plan,
            overlay_plan=overlay_plan,
            recovered=recovered,
        )
        self._crash_point("evidence_persisted")
        self._clear_intent()
        self._heartbeat(
            phase="batch_ready",
            plan_revision=plan.revision,
            overlay_revision=overlay_plan["revision"],
            lifecycle=plan.lifecycle.value,
            reason_code="BATCH_RECOVERED" if recovered else "BATCH_PUBLISHED",
            detail=f"revision {plan.revision} is exactly candidate/overlay coherent",
            evidence_sha256=evidence_sha,
        )
        return ProducerSnapshot(
            phase="batch_ready",
            plan_revision=plan.revision,
            overlay_revision=overlay_plan["revision"],
            lifecycle=plan.lifecycle.value,
            appended=True,
            recovered=recovered,
            policy=evidence["policy"],
            evidence_sha256=evidence_sha,
        )

    def poll_once(
        self,
        *,
        proposal: BatchProposal | None = None,
        proposal_provider: ProposalProvider | None = None,
    ) -> ProducerSnapshot:
        """Observe once and append at most one exact revision.

        The optimizer provider is called only for revision 3 or later and only
        after a coherent ``OPEN_EMPTY`` state has been established.
        """

        if proposal is not None and proposal_provider is not None:
            raise BatchProducerError(
                "API_INVALID", "provide either proposal or proposal_provider, not both"
            )
        try:
            self._assert_authority()
            with control_lock(self.paths):
                self._validate_existing_evidence_binding()
                intent = self._read_intent()
                if intent is not None:
                    return self._apply_intent(intent)
                plan = self._load_plan()
                overlay_plan = self._read_overlay_plan()
                self._validate_overlay_prefix(plan, overlay_plan)
                if plan.revision != overlay_plan["revision"]:
                    raise BatchProducerError(
                        "REVISION_DIVERGENCE",
                        "candidate/overlay divergence without a durable intent",
                    )
                if plan.revision < 1:
                    raise BatchProducerError(
                        "REVISION_ONE_REQUIRED",
                        "canonical live initialization must publish revision 1 first",
                    )
                if plan.lifecycle is PlanLifecycle.CLOSED_COMPLETE:
                    self._heartbeat(
                        phase="closed",
                        plan_revision=plan.revision,
                        overlay_revision=overlay_plan["revision"],
                        lifecycle=plan.lifecycle.value,
                        reason_code="PLAN_CLOSED",
                        detail="rolling plan is terminal; producer did not append",
                        evidence_sha256=None,
                    )
                    return ProducerSnapshot(
                        "closed",
                        plan.revision,
                        overlay_plan["revision"],
                        plan.lifecycle.value,
                        False,
                        False,
                        None,
                        None,
                    )
                if plan.lifecycle is PlanLifecycle.OPEN_READY:
                    self._heartbeat(
                        phase="waiting_open_empty",
                        plan_revision=plan.revision,
                        overlay_revision=overlay_plan["revision"],
                        lifecycle=plan.lifecycle.value,
                        reason_code="PLAN_OPEN_READY",
                        detail="current five-row batch has not been consumed",
                        evidence_sha256=None,
                    )
                    return ProducerSnapshot(
                        "waiting_open_empty",
                        plan.revision,
                        overlay_plan["revision"],
                        plan.lifecycle.value,
                        False,
                        False,
                        None,
                        None,
                    )
                if plan.lifecycle is not PlanLifecycle.OPEN_EMPTY:
                    raise BatchProducerError(
                        "CANDIDATE_STATE_INVALID", "rolling lifecycle is unsupported"
                    )
                target = plan.revision + 1
                selected = self._proposal_for(
                    target,
                    proposal=proposal,
                    proposal_provider=proposal_provider,
                )
                intent = self._prepare_intent(
                    plan=plan, overlay_plan=overlay_plan, proposal=selected
                )
                try:
                    self._assert_authority()
                    atomic_json(self.intent_path, intent)
                except (OSError, StateError) as exc:
                    raise BatchProducerError("INTENT_WRITE_FAILED", str(exc)) from exc
                self._crash_point("intent_persisted")
                return self._apply_intent(intent)
        except BatchProducerError as exc:
            self._failed_heartbeat(exc)
            raise
        except (OSError, StateError, TypeError, ValueError) as exc:
            error = BatchProducerError("PRODUCER_STATE_INVALID", str(exc))
            self._failed_heartbeat(error)
            raise error from exc

    def watch(
        self,
        *,
        stop_requested: Callable[[], bool],
        proposal_provider: ProposalProvider | None = None,
        poll_interval_s: float = 0.25,
        max_polls: int | None = None,
    ) -> ProducerSnapshot | None:
        """Continuously replenish consumed batches until the supervisor stops it."""

        if not callable(stop_requested):
            raise BatchProducerError("API_INVALID", "stop_requested must be callable")
        if (
            isinstance(poll_interval_s, bool)
            or not isinstance(poll_interval_s, (int, float))
            or not math.isfinite(float(poll_interval_s))
            or float(poll_interval_s) < 0.0
        ):
            raise BatchProducerError("API_INVALID", "poll interval must be finite and non-negative")
        if max_polls is not None and (
            isinstance(max_polls, bool) or not isinstance(max_polls, int) or max_polls < 1
        ):
            raise BatchProducerError("API_INVALID", "max_polls must be a positive integer")
        latest: ProducerSnapshot | None = None
        polls = 0
        while not stop_requested():
            latest = self.poll_once(proposal_provider=proposal_provider)
            polls += 1
            if max_polls is not None and polls >= max_polls:
                break
            if stop_requested():
                break
            time.sleep(float(poll_interval_s))
        return latest
