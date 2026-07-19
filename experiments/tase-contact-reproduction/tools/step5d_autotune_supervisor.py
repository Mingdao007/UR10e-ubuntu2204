#!/usr/bin/env python3
"""Pure host supervisor for a continuous Step5d-native autotune campaign.

The supervisor has no controller, RTDE, process, or filesystem side effects.
The caller must first durably write an immutable trial bundle, then present its
path before this state machine can emit an ACK packet.
"""

from __future__ import annotations

import math
from collections.abc import Collection
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from step5d_autotune_contract import (
    CODEX_I_SCALE_MULTIPLIERS,
    CampaignSpec,
    CaptureManifest,
    Evaluation,
    ExecutionProfile,
    ForceCandidate,
    GovernorDecision,
    SearchAttestation,
    SearchTier,
    TrialSource,
    TrialTransition,
    TrialTransitionKind,
    TrialDisposition,
    TrialSpec,
    codex_i_scale_probe_transition,
    trial_source_from_trial,
)
from step5d_autotune_governor import (
    AbEvidence,
    AbTrialIdentity,
    SaturationSample,
    SaturationTriggerEvidence,
    assess_ab,
    propose_change_from_trigger,
)
from step5d_autotune_optimizer import (
    Observation,
    choose_candidate,
    live_trust_region_step,
    success_confirmed,
    unlocked_tier,
)
from step5d_autotune_state_machine import (
    ClosureEvidence,
    HostCommand,
    HostPacket,
    classify_terminal_reason,
)


class CampaignPhase(str, Enum):
    HOME = "home"
    TRIAL_ACTIVE = "trial_active"
    WAIT_ACK = "wait_ack"
    WAIT_INFRA_READY = "wait_infra_ready"
    PAUSED_CODE_BUG = "paused_code_bug"
    MANUAL_RECOVERY = "manual_recovery"
    STOPPED_SAFETY = "stopped_safety"
    STOPPED_PARAMETER = "stopped_parameter"
    STOPPED_OPERATOR = "stopped_operator"
    STOPPED_FAIL_CLOSED = "stopped_fail_closed"
    SUCCEEDED = "succeeded"


TERMINAL_PHASES = {
    CampaignPhase.MANUAL_RECOVERY,
    CampaignPhase.STOPPED_SAFETY,
    CampaignPhase.STOPPED_PARAMETER,
    CampaignPhase.STOPPED_OPERATOR,
    CampaignPhase.STOPPED_FAIL_CLOSED,
    CampaignPhase.SUCCEEDED,
}


def candidate_transition_allowed_for_policy(
    selection_policy: str,
    source: ForceCandidate,
    target: ForceCandidate,
) -> bool:
    """Use the live supervisor's exact transition rule at pre-Play gates."""

    if selection_policy not in {"adaptive", "codex_batches"}:
        raise ValueError("selection_policy must be adaptive or codex_batches")
    if live_trust_region_step(source, target):
        return True
    return (
        selection_policy == "codex_batches"
        and codex_i_scale_probe_transition(source, target)
    )


@dataclass(frozen=True)
class TrialIntent:
    trial: TrialSpec
    execution_profile_integer_id: int
    selection: Mapping[str, Any]
    retry_kind: str | None


@dataclass(frozen=True)
class CloseDecision:
    disposition: TrialDisposition
    phase: CampaignPhase
    ack_permitted: bool
    post_ack_phase: CampaignPhase | None
    same_candidate_retry_pending: bool
    reason: str


@dataclass(frozen=True)
class GovernorProbeState:
    """Exact A/B/A-prime provenance for one low-frequency profile probe."""

    profile_a: ExecutionProfile
    profile_b: ExecutionProfile
    layer: str
    force_candidate: ForceCandidate
    identity_a: AbTrialIdentity
    stage: str = "b"
    identity_b: AbTrialIdentity | None = None
    identity_a_prime: AbTrialIdentity | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.profile_a, ExecutionProfile) or not isinstance(
            self.profile_b, ExecutionProfile
        ):
            raise ValueError("governor probe profiles must be ExecutionProfile")
        if self.profile_a == self.profile_b:
            raise ValueError("governor probe profiles must differ")
        if self.layer not in {
            "normal_filter_rate",
            "host_qdot_slew",
            "tp_speedj_acceleration",
        }:
            raise ValueError("governor probe layer is invalid")
        if not isinstance(self.force_candidate, ForceCandidate):
            raise ValueError("governor probe candidate must be a ForceCandidate")
        if not isinstance(self.identity_a, AbTrialIdentity):
            raise ValueError("governor probe requires an exact A trial identity")
        if self.stage not in {"b", "a_prime"}:
            raise ValueError("governor probe stage must be b or a_prime")
        if self.identity_a.force_candidate_uid != self.force_candidate.candidate_uid:
            raise ValueError("governor A identity differs from the frozen force candidate")
        if self.identity_a.profile_id != self.profile_a.profile_id:
            raise ValueError("governor A identity differs from profile A")
        for name, identity, profile in (
            ("B", self.identity_b, self.profile_b),
            ("A-prime", self.identity_a_prime, self.profile_a),
        ):
            if identity is None:
                continue
            if not isinstance(identity, AbTrialIdentity):
                raise ValueError(f"governor {name} identity is invalid")
            if identity.force_candidate_uid != self.force_candidate.candidate_uid:
                raise ValueError(f"governor {name} identity changed the force candidate")
            if identity.profile_id != profile.profile_id:
                raise ValueError(f"governor {name} identity differs from its profile")
            if identity.plant_epoch != self.identity_a.plant_epoch:
                raise ValueError(f"governor {name} identity changed plant epoch")
        identities = [
            identity.trial_uid
            for identity in (self.identity_a, self.identity_b, self.identity_a_prime)
            if identity is not None
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("governor A/B/A-prime trial UIDs must be distinct")
        if self.stage == "b" and self.identity_a_prime is not None:
            raise ValueError("A-prime identity cannot exist during the B stage")
        if self.stage == "a_prime" and self.identity_b is None:
            raise ValueError("A-prime stage requires a completed B trial")

    @property
    def expected_profile(self) -> ExecutionProfile:
        return self.profile_b if self.stage == "b" else self.profile_a

    @property
    def stage_identity(self) -> AbTrialIdentity | None:
        return self.identity_b if self.stage == "b" else self.identity_a_prime


@dataclass(frozen=True)
class SupervisorRecoverySnapshot:
    """Complete in-memory policy state bound to one durable journal revision."""

    phase: CampaignPhase
    outcome_timeline: tuple[Observation, ...]
    trial_counter: int
    command_seq: int
    candidate_tokens: Mapping[str, int]
    active: TrialIntent | None
    pending_retry: tuple[
        ForceCandidate,
        int,
        str,
        TrialSource,
    ] | None
    pending_ack: tuple[TrialIntent, CampaignPhase] | None
    prepared_ack: HostPacket | None
    cooldown_remaining: int
    governor_probe: GovernorProbeState | None


def execution_profile_integer_id(profile: ExecutionProfile) -> int:
    """Encode normal/slew/TP-accel levels for the TP ones-digit contract."""

    normal_levels = {0.010: 1, 0.015: 2, 0.020: 3, 0.030: 4, 0.050: 5}
    actuator_levels = {0.1: 1, 0.2: 2, 0.5: 3}
    try:
        normal = normal_levels[profile.normal_max_rate_rad_s]
        host_slew = actuator_levels[profile.host_qdot_slew_rad_s2]
        tp_accel = actuator_levels[profile.tp_speedj_accel_rad_s2]
    except KeyError as exc:  # ExecutionProfile should already reject this.
        raise ValueError("execution profile is outside the frozen profile lattice") from exc
    return 100 * normal + 10 * host_slew + tp_accel


class CampaignSupervisor:
    """Single-writer policy state for one bounded, explicitly authorized campaign."""

    def __init__(
        self,
        *,
        campaign: CampaignSpec,
        backend_id: str,
        source_fingerprint: str,
        config_fingerprint: str,
        execution_profile: ExecutionProfile,
        plant_epoch: int = 1,
        selection_policy: str = "adaptive",
    ) -> None:
        if plant_epoch < 1:
            raise ValueError("plant_epoch must be positive")
        if selection_policy not in {"adaptive", "codex_batches"}:
            raise ValueError("selection_policy must be adaptive or codex_batches")
        self.campaign = campaign
        self.backend_id = backend_id
        self.source_fingerprint = source_fingerprint
        self.config_fingerprint = config_fingerprint
        self.execution_profile = execution_profile
        self.plant_epoch = plant_epoch
        self.selection_policy = selection_policy
        self.phase = CampaignPhase.HOME
        # ``outcome_timeline`` is authoritative for tier unlock, replay
        # attestation, and recovery. ``observations`` remains the legacy
        # objective-only view used by callers that feed a GP directly.
        self.outcome_timeline: list[Observation] = []
        self.observations: list[Observation] = []
        self.archived_epoch_outcomes: list[tuple[int, tuple[Observation, ...]]] = []
        self.archived_epoch_observations: list[tuple[int, tuple[Observation, ...]]] = []
        self._archived_trial_sources: dict[str, TrialSource] = {}
        self._trial_counter = 0
        self._command_seq = 0
        self._next_candidate_token = 1
        self._candidate_tokens: dict[str, int] = {}
        self._active: TrialIntent | None = None
        self._pending_retry: tuple[
            ForceCandidate,
            int,
            str,
            TrialSource,
        ] | None = None
        self._pending_ack: tuple[TrialIntent, CampaignPhase] | None = None
        self._prepared_ack: HostPacket | None = None
        self._cooldown_remaining = 0
        self._governor_probe: GovernorProbeState | None = None

    @property
    def active_trial(self) -> TrialSpec | None:
        return None if self._active is None else self._active.trial

    @property
    def attempted_candidate_uids(self) -> frozenset[str]:
        return frozenset(
            outcome.candidate.candidate_uid for outcome in self._all_outcomes()
        )

    @property
    def current_epoch_attempted_candidate_uids(self) -> frozenset[str]:
        return frozenset(outcome.candidate.candidate_uid for outcome in self.outcome_timeline)

    @property
    def current_search_tier(self) -> SearchTier:
        return unlocked_tier(
            [
                outcome
                for outcome in self._all_outcomes()
                if outcome.profile_id == self.execution_profile.profile_id
                and outcome.plant_epoch == self.plant_epoch
            ]
        )

    def planned_candidate_within_policy_envelope(
        self,
        candidate: ForceCandidate,
    ) -> bool:
        """Accept Codex-planned positive-I probes without widening P/D early."""

        tier = self.current_search_tier
        if candidate.within_tier(tier):
            return True
        return self.selection_policy == "codex_batches" and candidate.within_codex_hybrid_i_envelope()

    def planned_candidate_transition_allowed(
        self,
        source: ForceCandidate,
        target: ForceCandidate,
    ) -> bool:
        return candidate_transition_allowed_for_policy(
            self.selection_policy,
            source,
            target,
        )

    def seed_command_sequence_from_tp(self, consumed_command_seq: int) -> None:
        """Continue the TP-global command sequence for a fresh campaign epoch."""

        if (
            isinstance(consumed_command_seq, bool)
            or not isinstance(consumed_command_seq, int)
            or consumed_command_seq < 0
        ):
            raise ValueError("consumed_command_seq must be a non-negative integer")
        if any(
            (
                self.phase is not CampaignPhase.HOME,
                self._trial_counter != 0,
                self._command_seq != 0,
                self._active is not None,
                self._pending_ack is not None,
            )
        ):
            raise RuntimeError("TP command sequence can be seeded only before first ARM")
        self._command_seq = consumed_command_seq

    @property
    def cooldown_remaining(self) -> int:
        return self._cooldown_remaining

    def _token_for(self, candidate: ForceCandidate) -> int:
        token = self._candidate_tokens.get(candidate.candidate_uid)
        if token is None:
            token = self._next_candidate_token
            self._next_candidate_token += 1
            self._candidate_tokens[candidate.candidate_uid] = token
        return token

    def _next_sequence(self) -> int:
        self._command_seq += 1
        return self._command_seq

    def _source_from_outcome(self, outcome: Observation) -> TrialSource:
        archived = self._archived_trial_sources.get(outcome.evaluation.trial_uid)
        if archived is not None:
            return archived
        return TrialSource(
            trial_uid=outcome.evaluation.trial_uid,
            candidate=outcome.candidate,
            profile_id=outcome.profile_id,
            plant_epoch=outcome.plant_epoch,
            campaign_id=self.campaign.campaign_id,
            campaign_epoch=self.campaign.campaign_epoch,
            campaign_fingerprint=self.campaign.campaign_fingerprint,
            backend_id=self.backend_id,
            source_fingerprint=self.source_fingerprint,
            config_fingerprint=self.config_fingerprint,
        )

    def _source_for_trial_uid(self, trial_uid: str) -> TrialSource:
        matches = [
            outcome
            for outcome in self._all_outcomes()
            if outcome.evaluation.trial_uid == trial_uid
        ]
        if len(matches) != 1:
            raise RuntimeError("transition source trial is missing or not unique")
        return self._source_from_outcome(matches[0])

    def _all_outcomes(self) -> list[Observation]:
        return [
            outcome
            for _epoch, archived in self.archived_epoch_outcomes
            for outcome in archived
        ] + list(self.outcome_timeline)

    def _forbidden_candidate_uids(self, profile_id: str) -> frozenset[str]:
        return frozenset(
            outcome.candidate.candidate_uid
            for outcome in self._all_outcomes()
            if outcome.profile_id == profile_id
        )

    def next_trial(
        self,
        *,
        require_cuda_botorch: bool = True,
        cuda_fit_mode: str = "serial",
        parallel_cuda_verified: bool = False,
        search_attestations: Sequence[SearchAttestation] = (),
        forced_candidate: ForceCandidate | None = None,
        forbidden_candidate_uids: Collection[str] = (),
        allow_archived_code_fix_replay: bool = False,
        allow_exact_incomplete_batch_retry: bool = False,
    ) -> TrialIntent:
        if self.phase is not CampaignPhase.HOME:
            raise RuntimeError(f"campaign cannot arm from phase {self.phase.value}")
        if self._active is not None or self._pending_ack is not None:
            raise RuntimeError("a trial or bundle ACK is already active")
        if self._prepared_ack is not None:
            raise RuntimeError("a durable ACK is still awaiting exact TP consumption")
        probe = self._governor_probe
        if probe is not None and probe.stage_identity is not None:
            raise RuntimeError(
                f"governor {probe.stage} trial is closed and awaiting assessment"
            )
        trial_profile = self.execution_profile if probe is None else probe.expected_profile
        external_forbidden = frozenset(forbidden_candidate_uids)
        if any(
            not isinstance(value, str) or len(value) != 64
            for value in external_forbidden
        ):
            raise ValueError("forbidden candidate identities must be SHA-256 strings")
        # The pending record preserves an infra/code pause origin for durable
        # reconciliation only.  It never authorizes reuse of its candidate.
        pending_release_kind = (
            self._pending_retry[2]
            if self._pending_retry is not None
            and self._pending_retry[2] == "infrastructure"
            else None
        )
        self._pending_retry = None
        code_fix_replay = False
        if probe is not None:
            candidate = probe.force_candidate
            token = self._token_for(candidate)
            retry_kind = pending_release_kind
            selected_attestation = None
            source_identity = (
                probe.identity_a
                if probe.stage == "b"
                else probe.identity_b
            )
            if source_identity is None:
                raise RuntimeError("governor probe transition source is missing")
            transition = TrialTransition(
                kind=TrialTransitionKind.GOVERNOR_PROBE,
                source=self._source_for_trial_uid(source_identity.trial_uid),
            )
            selection = {
                "selection": f"governor_{probe.stage}_probe",
                "governor_probe_stage": probe.stage,
                "governor_a_trial_uid": probe.identity_a.trial_uid,
                "force_candidate_frozen": True,
            }
        elif forced_candidate is not None:
            if (
                forced_candidate.candidate_uid in external_forbidden
                and not allow_exact_incomplete_batch_retry
            ):
                raise ValueError("forced live candidate was already physically attempted")
            context = [
                outcome
                for outcome in self._all_outcomes()
                if outcome.profile_id == trial_profile.profile_id
                and outcome.plant_epoch == self.plant_epoch
            ]
            replay_sources = [
                outcome
                for outcome in self._all_outcomes()
                if outcome.candidate == forced_candidate
                and outcome.evaluation.trial_uid in self._archived_trial_sources
                and outcome.profile_id == trial_profile.profile_id
                and outcome.plant_epoch == self.plant_epoch
            ]
            incomplete_retry_sources = [
                outcome
                for outcome in context
                if outcome.candidate == forced_candidate
            ]
            code_fix_replay = bool(
                allow_archived_code_fix_replay
                and replay_sources
                and forced_candidate.candidate_uid
                not in self.current_epoch_attempted_candidate_uids
            )
            tier = unlocked_tier(context, pending_candidate=forced_candidate)
            if not self.planned_candidate_within_policy_envelope(forced_candidate):
                raise ValueError(
                    "planned candidate is outside the current selection-policy "
                    f"envelope (evidence tier {tier.value})"
                )
            anchors = [
                outcome
                for outcome in self._all_outcomes()
                if outcome.profile_id == trial_profile.profile_id
                and outcome.plant_epoch == self.plant_epoch
                and self.planned_candidate_transition_allowed(
                    outcome.candidate,
                    forced_candidate,
                )
            ]
            baseline_start = not context and forced_candidate == ForceCandidate()
            if (
                not anchors
                and not baseline_start
                and not code_fix_replay
                and not (
                    allow_exact_incomplete_batch_retry
                    and incomplete_retry_sources
                )
            ):
                raise ValueError(
                    "forced live candidate must be one lattice step from an executed candidate"
                )
            source_outcome = (
                None
                if baseline_start
                else incomplete_retry_sources[-1]
                if allow_exact_incomplete_batch_retry and incomplete_retry_sources
                else replay_sources[-1]
                if code_fix_replay
                else anchors[-1]
            )
            candidate = forced_candidate
            token = self._token_for(candidate)
            retry_kind = pending_release_kind
            selected_attestation = None
            transition = TrialTransition(
                kind=(
                    TrialTransitionKind.BASELINE
                    if source_outcome is None
                    else TrialTransitionKind.RETRY
                    if allow_exact_incomplete_batch_retry
                    else TrialTransitionKind.RETRY
                    if code_fix_replay
                    else TrialTransitionKind.CODE_EPOCH_SEARCH
                    if source_outcome.evaluation.trial_uid
                    in self._archived_trial_sources
                    else TrialTransitionKind.I_SCALE_PROBE
                    if codex_i_scale_probe_transition(
                        source_outcome.candidate,
                        forced_candidate,
                    )
                    else TrialTransitionKind.FORCE_SEARCH
                ),
                source=(
                    None
                    if source_outcome is None
                    else self._source_from_outcome(source_outcome)
                ),
                retry_kind=(
                    "evidence"
                    if allow_exact_incomplete_batch_retry
                    and source_outcome is not None
                    else "code_fix"
                    if code_fix_replay
                    else None
                ),
            )
            selection = {
                "selection": (
                    "codex_log10_i_scale_candidate"
                    if source_outcome is not None
                    and codex_i_scale_probe_transition(
                        source_outcome.candidate,
                        forced_candidate,
                    )
                    else "codex_log2_batch_candidate"
                    if self.selection_policy == "codex_batches"
                    else "operator_bounded_candidate"
                ),
                "tier": tier.value,
                "codex_positive_i_radius_octaves": (
                    math.log2(max(CODEX_I_SCALE_MULTIPLIERS))
                    if self.selection_policy == "codex_batches"
                    else tier.positive_i_radius_octaves
                ),
                "source_trial_uid": (
                    None if source_outcome is None else source_outcome.evaluation.trial_uid
                ),
                "exact_parameter_set_reuse_allowed": (
                    code_fix_replay or allow_exact_incomplete_batch_retry
                ),
                "exact_incomplete_batch_retry": allow_exact_incomplete_batch_retry,
            }
        else:
            candidate, selection = choose_candidate(
                self._all_outcomes(),
                profile_id=self.execution_profile.profile_id,
                plant_epoch=self.plant_epoch,
                require_cuda_botorch=require_cuda_botorch,
                cuda_fit_mode=cuda_fit_mode,
                parallel_cuda_verified=parallel_cuda_verified,
                search_attestations=search_attestations,
                forbidden_candidate_uids=self._forbidden_candidate_uids(
                    trial_profile.profile_id
                )
                | external_forbidden,
            )
            token = self._token_for(candidate)
            retry_kind = pending_release_kind
            attestation_uid = selection.get("search_attestation_uid")
            selected_attestation = None
            if attestation_uid is not None:
                matching = [
                    item
                    for item in search_attestations
                    if item.attestation_uid == attestation_uid
                ]
                if len(matching) != 1:
                    raise ValueError(
                        "selected search attestation is missing or not unique"
                    )
                selected_attestation = matching[0]
            selection_kind = selection.get("selection")
            if selection_kind == "exact_v35_baseline":
                transition = TrialTransition(kind=TrialTransitionKind.BASELINE)
            elif selection_kind == "plant_epoch_anchor_replication":
                source_uid = selection.get("source_trial_uid")
                if not isinstance(source_uid, str):
                    raise RuntimeError("plant epoch anchor lacks its source trial")
                transition = TrialTransition(
                    kind=TrialTransitionKind.PLANT_EPOCH_ANCHOR,
                    source=self._source_for_trial_uid(source_uid),
                )
            else:
                history = [
                    outcome
                    for outcome in self._all_outcomes()
                    if outcome.profile_id == trial_profile.profile_id
                    and outcome.plant_epoch == self.plant_epoch
                ]
                if not history:
                    raise RuntimeError("non-baseline selection lacks an executed source")
                source = self._source_from_outcome(history[-1])
                transition = TrialTransition(
                    kind=(
                        TrialTransitionKind.CODE_EPOCH_SEARCH
                        if source.campaign_epoch != self.campaign.campaign_epoch
                        else (
                            TrialTransitionKind.REPLICATION
                            if source.candidate == candidate
                            else TrialTransitionKind.FORCE_SEARCH
                        )
                    ),
                    source=source,
                )
        if (
            not code_fix_replay
            and not allow_exact_incomplete_batch_retry
            and candidate.candidate_uid
            in self._forbidden_candidate_uids(trial_profile.profile_id)
        ):
            raise RuntimeError("exact parameter set has already been executed")
        self._trial_counter += 1
        trial = TrialSpec(
            campaign=self.campaign,
            trial_id=self._trial_counter,
            candidate_token=token,
            command_seq=self._next_sequence(),
            plant_epoch=self.plant_epoch,
            candidate=candidate,
            execution_profile=trial_profile,
            backend_id=self.backend_id,
            source_fingerprint=self.source_fingerprint,
            config_fingerprint=self.config_fingerprint,
            transition=transition,
            search_attestation=selected_attestation,
        )
        intent = TrialIntent(
            trial=trial,
            execution_profile_integer_id=execution_profile_integer_id(
                trial_profile
            ),
            selection=dict(selection),
            retry_kind=retry_kind,
        )
        self._active = intent
        self.phase = CampaignPhase.TRIAL_ACTIVE
        return intent

    @staticmethod
    def _bundle_closed(bundle_path: Path | None, trial: TrialSpec) -> bool:
        return (
            isinstance(bundle_path, Path)
            and bundle_path.is_file()
            and bundle_path.name == "immutable_trial_bundle.json"
            and bundle_path.parent.name == trial.trial_uid
        )

    def close_trial(
        self,
        *,
        manifest: CaptureManifest,
        evaluation: Evaluation,
        safe_closure: ClosureEvidence,
        bundle_path: Path | None,
        failure_cause: str = "evidence",
    ) -> CloseDecision:
        if self.phase is not CampaignPhase.TRIAL_ACTIVE or self._active is None:
            raise RuntimeError("no active trial can be closed")
        trial = self._active.trial
        if manifest.trial_uid != trial.trial_uid or evaluation.trial_uid != trial.trial_uid:
            raise ValueError("trial/capture/evaluation identity mismatch")
        if manifest.candidate_token != trial.candidate_token:
            raise ValueError("capture candidate token mismatch")
        if safe_closure != manifest.safe_closure_evidence:
            raise ValueError(
                "runtime safe closure proof differs from the immutable capture manifest"
            )
        if evaluation.safe_closure is not safe_closure.returned_safe:
            raise ValueError("evaluation safe closure differs from the runtime proof")
        if failure_cause not in {"evidence", "code"}:
            raise ValueError("failure_cause must be evidence or code")

        probe = self._governor_probe
        if probe is not None and any(
            (
                trial.candidate != probe.force_candidate,
                trial.execution_profile != probe.expected_profile,
                trial.plant_epoch != probe.identity_a.plant_epoch,
            )
        ):
            raise ValueError(
                "active governor trial differs from the frozen candidate/profile/epoch"
            )

        returned_safe = safe_closure.returned_safe and manifest.returned_safe
        disposition = classify_terminal_reason(
            manifest.terminal_reason,
            host_cause=manifest.host_cause,
            safe_closure=returned_safe,
            eligible_evidence=evaluation.eligible,
        )
        if evaluation.disposition is not disposition:
            raise ValueError("evaluation disposition differs from terminal classification")
        bundle_closed = self._bundle_closed(bundle_path, trial)
        ack_permitted = (
            returned_safe
            and bundle_closed
            and manifest.immutable_bundle_written
            and manifest.fingerprint_closed
        )

        post_ack_phase: CampaignPhase | None
        retry = False
        reason = disposition.value
        outcome = Observation(
            candidate=trial.candidate,
            evaluation=evaluation,
            profile_id=trial.execution_profile.profile_id,
            plant_epoch=trial.plant_epoch,
            latest_trace_sha256=manifest.csv_sha256,
        )
        self.outcome_timeline.append(outcome)
        profile_diagnostic = self._is_profile_diagnostic(outcome)
        if disposition is TrialDisposition.OBJECTIVE:
            if self._cooldown_remaining > 0:
                self._cooldown_remaining -= 1
            objective_confirmed = success_confirmed(
                [*self.observations, outcome],
                profile_id=trial.execution_profile.profile_id,
                plant_epoch=trial.plant_epoch,
            )
            # A B-profile result still requires the A/B decision because the
            # retained plant/profile context remains A.  A-prime, by contrast,
            # is an exact retained-A replicate and may directly finish the
            # campaign under the global confirmation rule.
            post_ack_phase = (
                CampaignPhase.SUCCEEDED
                if self.selection_policy == "adaptive"
                and objective_confirmed
                and not (probe is not None and probe.stage == "b")
                else CampaignPhase.HOME
            )
        elif disposition is TrialDisposition.WAIT_INFRA_READY:
            self._pending_retry = (
                trial.candidate,
                trial.candidate_token,
                "infrastructure",
                trial_source_from_trial(trial),
            )
            post_ack_phase = CampaignPhase.WAIT_INFRA_READY
        elif disposition is TrialDisposition.CODE_CONTRACT_BUG:
            self._pending_retry = (
                trial.candidate,
                trial.candidate_token,
                "code_fix",
                trial_source_from_trial(trial),
            )
            post_ack_phase = CampaignPhase.PAUSED_CODE_BUG
            reason = "code_bug_requires_new_fingerprint_epoch_and_authorization"
        elif disposition is TrialDisposition.SAFETY_STOP:
            post_ack_phase = CampaignPhase.STOPPED_SAFETY
        elif disposition is TrialDisposition.MANUAL_RECOVERY:
            post_ack_phase = CampaignPhase.MANUAL_RECOVERY
        elif disposition is TrialDisposition.PARAMETER_EVENT:
            post_ack_phase = CampaignPhase.STOPPED_PARAMETER
        elif disposition is TrialDisposition.OPERATOR_STOP:
            post_ack_phase = CampaignPhase.STOPPED_OPERATOR
        elif (
            disposition is TrialDisposition.FAIL_CLOSED
            and failure_cause == "code"
        ):
            self._pending_retry = (
                trial.candidate,
                trial.candidate_token,
                "code_fix",
                trial_source_from_trial(trial),
            )
            post_ack_phase = CampaignPhase.PAUSED_CODE_BUG
            reason = "code_bug_requires_new_fingerprint_epoch_and_authorization"
        elif (
            disposition is TrialDisposition.FAIL_CLOSED
            and returned_safe
            and ack_permitted
            and profile_diagnostic
        ):
            # Persistent normal-rate limiting can itself violate the <=5%
            # orientation-duty qualification.  Preserve its force MAE only in
            # the typed non-trainable diagnostic seam: it may seed/complete a
            # governor comparison, but it never enters BO observations.
            if probe is None and self._cooldown_remaining > 0:
                self._cooldown_remaining -= 1
            post_ack_phase = CampaignPhase.HOME
            reason = (
                "governor_a_prime_profile_diagnostic_closed"
                if probe is not None and probe.stage == "a_prime"
                else "governor_profile_diagnostic_ready"
            )
        elif disposition is TrialDisposition.FAIL_CLOSED and returned_safe:
            # Preserve the failed evidence as a non-trainable outcome.  Fix
            # the evidence path separately; the next trial must use a new set.
            post_ack_phase = CampaignPhase.HOME
            reason = "nonparameter_evidence_recorded_no_parameter_repeat"
        else:
            post_ack_phase = CampaignPhase.STOPPED_FAIL_CLOSED

        governor_outcome_closed = bool(
            disposition is TrialDisposition.OBJECTIVE and evaluation.eligible
            or profile_diagnostic
            and probe is not None
            and probe.stage in {"b", "a_prime"}
        )
        if probe is not None and governor_outcome_closed and returned_safe and ack_permitted:
            identity = AbTrialIdentity(
                trial_uid=trial.trial_uid,
                force_candidate_uid=trial.candidate.candidate_uid,
                profile_id=trial.execution_profile.profile_id,
                plant_epoch=trial.plant_epoch,
            )
            if probe.stage == "b":
                if probe.identity_b is not None:
                    raise RuntimeError("governor B trial identity is already closed")
                self._governor_probe = replace(probe, identity_b=identity)
            else:
                if probe.identity_a_prime is not None:
                    raise RuntimeError("governor A-prime trial identity is already closed")
                self._governor_probe = replace(probe, identity_a_prime=identity)
                if post_ack_phase is CampaignPhase.SUCCEEDED:
                    # A and A-prime are already two exact confirmations under
                    # the retained A profile.  Campaign success takes priority
                    # over the optional burden probe, so do not leave an
                    # uncompletable governor state behind the terminal ACK.
                    self._governor_probe = None

        if ack_permitted:
            self._pending_ack = (self._active, post_ack_phase)
            self.phase = CampaignPhase.WAIT_ACK
        else:
            # No ACK means the TP cannot start another loop. Preserve the more
            # specific unsafe/manual stop state for operator recovery.
            self.phase = (
                CampaignPhase.MANUAL_RECOVERY
                if disposition is TrialDisposition.MANUAL_RECOVERY
                else CampaignPhase.STOPPED_SAFETY
                if not returned_safe
                else CampaignPhase.STOPPED_FAIL_CLOSED
            )
        self._active = None
        return CloseDecision(
            disposition=disposition,
            phase=self.phase,
            ack_permitted=ack_permitted,
            post_ack_phase=post_ack_phase if ack_permitted else None,
            same_candidate_retry_pending=retry,
            reason=reason,
        )

    def prepare_ack_packet(self) -> HostPacket:
        """Reserve the exact ACK sequence without advancing policy state.

        The coordinator must fsync a wait_ack journal revision containing this
        packet before it may return the packet to a TP writer.
        """

        if self.phase is not CampaignPhase.WAIT_ACK or self._pending_ack is None:
            raise RuntimeError("ACK is permitted only after closed immutable bundle evidence")
        if self._prepared_ack is not None:
            return self._prepared_ack
        intent, post_ack_phase = self._pending_ack
        trial = intent.trial
        packet = HostPacket(
            campaign_epoch=trial.campaign.campaign_epoch,
            trial_id=trial.trial_id,
            command=HostCommand.ACK_BUNDLE,
            candidate_token=trial.candidate_token,
            execution_profile_id=intent.execution_profile_integer_id,
            command_seq=self._next_sequence(),
        )
        self._prepared_ack = packet
        return packet

    def confirm_ack_consumed(self, packet: HostPacket) -> None:
        """Advance only after the coordinator verified exact TP consumed echo."""

        if self.phase is not CampaignPhase.WAIT_ACK or self._pending_ack is None:
            raise RuntimeError("no pending ACK can be confirmed")
        if self._prepared_ack is None or packet != self._prepared_ack:
            raise ValueError("ACK confirmation differs from the prepared durable packet")
        intent, post_ack_phase = self._pending_ack
        trial_uid = intent.trial.trial_uid
        matches = [
            outcome
            for outcome in self.outcome_timeline
            if outcome.evaluation.trial_uid == trial_uid
        ]
        if len(matches) != 1:
            raise RuntimeError(
                "ACK confirmation requires one exact closed outcome"
            )
        outcome = matches[0]
        if outcome.eligible:
            if any(
                observed.evaluation.trial_uid == trial_uid
                for observed in self.observations
            ):
                raise RuntimeError("ACK outcome was already admitted to the optimizer")
            self.observations.append(outcome)
        self._pending_ack = None
        self._prepared_ack = None
        self.phase = post_ack_phase

    def ack_bundle(self) -> HostPacket:
        """Legacy pure-state helper; live issuance must use CampaignCoordinator."""

        packet = self.prepare_ack_packet()
        self.confirm_ack_consumed(packet)
        return packet

    def mark_infra_ready(self) -> None:
        if self.phase is not CampaignPhase.WAIT_INFRA_READY:
            raise RuntimeError("campaign is not waiting for infrastructure")
        self.phase = CampaignPhase.HOME

    def cancel_unconsumed_arm(
        self,
        tp_snapshot: TpSnapshot,
        *,
        persisted_trial_uid: str,
        has_dispatch_receipt: bool,
    ) -> TrialSpec:
        """Release one persisted ARM proved absent from an exact Home TP state."""

        if (
            not isinstance(persisted_trial_uid, str)
            or len(persisted_trial_uid) != 64
            or any(
                character not in "0123456789abcdef"
                for character in persisted_trial_uid
            )
        ):
            raise ValueError(
                "persisted_trial_uid must be 64 lowercase hexadecimal characters"
            )
        if type(has_dispatch_receipt) is not bool:
            raise ValueError("has_dispatch_receipt must be bool")

        if self.phase is not CampaignPhase.TRIAL_ACTIVE or self._active is None:
            raise RuntimeError("no durable active ARM can be cancelled")
        trial = self._active.trial
        if persisted_trial_uid != trial.trial_uid:
            raise RuntimeError("journal persistence does not bind the current ARM")
        if has_dispatch_receipt:
            raise RuntimeError("a dispatched ARM cannot be proven unconsumed")
        if (
            tp_snapshot.state != "READY_HOME"
            or tp_snapshot.consumed_command_seq >= trial.command_seq
            or any(
                value != 0
                for value in (
                    tp_snapshot.campaign_epoch_echo,
                    tp_snapshot.trial_id_echo,
                    tp_snapshot.candidate_token_echo,
                    tp_snapshot.execution_profile_integer_id_echo,
                    tp_snapshot.terminal_reason,
                )
            )
        ):
            raise RuntimeError("TP does not prove the durable ARM was unconsumed at Home")
        self._active = None
        self.phase = CampaignPhase.HOME
        return trial

    def terminalize_consumed_infra_abort(
        self,
        tp_snapshot: TpSnapshot,
        *,
        persisted_trial_uid: str,
    ) -> TrialSpec:
        """Retire one dispatched/consumed ARM after externally proven bridge stop."""

        if self.phase is not CampaignPhase.TRIAL_ACTIVE or self._active is None:
            raise RuntimeError("no durable active ARM can be terminalized")
        trial = self._active.trial
        if persisted_trial_uid != trial.trial_uid:
            raise RuntimeError("journal persistence does not bind the active ARM")
        if (
            tp_snapshot.state != "READY_HOME"
            or tp_snapshot.consumed_command_seq != trial.command_seq
            or any(
                value != 0
                for value in (
                    tp_snapshot.campaign_epoch_echo,
                    tp_snapshot.trial_id_echo,
                    tp_snapshot.candidate_token_echo,
                    tp_snapshot.execution_profile_integer_id_echo,
                    tp_snapshot.terminal_reason,
                )
            )
        ):
            raise RuntimeError(
                "TP does not prove the consumed ARM returned to exact Home"
            )
        self._active = None
        self.phase = CampaignPhase.HOME
        return trial

    def resume_after_code_change(
        self,
        *,
        campaign: CampaignSpec,
        source_fingerprint: str,
        config_fingerprint: str,
        search_attestation: SearchAttestation | None = None,
    ) -> None:
        if self.phase not in {CampaignPhase.PAUSED_CODE_BUG, CampaignPhase.HOME}:
            raise RuntimeError("campaign is not safely paused for a code fix")
        pending_code_fix = bool(
            self.phase is CampaignPhase.PAUSED_CODE_BUG
            and self._pending_retry is not None
            and self._pending_retry[2] == "code_fix"
        )
        if any(
            value is not None
            for value in (
                self._active,
                self._pending_ack,
                self._prepared_ack,
                None if pending_code_fix else self._pending_retry,
                self._governor_probe,
            )
        ):
            raise RuntimeError("code epoch cannot change with pending campaign state")
        if (
            campaign.campaign_id != self.campaign.campaign_id
            or campaign.campaign_epoch <= self.campaign.campaign_epoch
            or campaign.campaign_fingerprint == self.campaign.campaign_fingerprint
        ):
            raise ValueError("code changes require a new campaign epoch and fingerprint")
        for outcome in self.outcome_timeline:
            source = self._source_from_outcome(outcome)
            self._archived_trial_sources[source.trial_uid] = source
        self.archived_epoch_observations.append(
            (self.campaign.campaign_epoch, tuple(self.observations))
        )
        self.archived_epoch_outcomes.append(
            (self.campaign.campaign_epoch, tuple(self.outcome_timeline))
        )
        self.observations.clear()
        self.outcome_timeline.clear()
        self.campaign = campaign
        self.source_fingerprint = source_fingerprint
        self.config_fingerprint = config_fingerprint
        self._pending_retry = None
        self._prepared_ack = None
        # Code/guard changes invalidate every prior profile comparison.  The
        # A/B provenance may not cross a campaign fingerprint epoch, and prior
        # exact parameter sets remain forbidden after the code fix.
        self._governor_probe = None
        self.phase = CampaignPhase.HOME

    @staticmethod
    def _ab_identity(outcome: Observation) -> AbTrialIdentity:
        return AbTrialIdentity(
            trial_uid=outcome.evaluation.trial_uid,
            force_candidate_uid=outcome.candidate.candidate_uid,
            profile_id=outcome.profile_id,
            plant_epoch=outcome.plant_epoch,
        )

    def _closed_governor_outcome(
        self,
        identity: AbTrialIdentity,
        *,
        allow_profile_diagnostic: bool = False,
    ) -> Observation:
        matches = [
            outcome
            for outcome in self.outcome_timeline
            if outcome.evaluation.trial_uid == identity.trial_uid
        ]
        if len(matches) != 1:
            raise RuntimeError(
                "governor identity does not name one supervisor-closed outcome"
            )
        outcome = matches[0]
        accepted = outcome.eligible or (
            allow_profile_diagnostic
            and self._is_profile_diagnostic(outcome)
        )
        if not accepted or not outcome.evaluation.safe_closure or self._ab_identity(
            outcome
        ) != identity:
            raise RuntimeError(
                "governor identity does not bind an accepted safely closed outcome"
            )
        return outcome

    @staticmethod
    def _profile_diagnostic_payload(outcome: Observation) -> Mapping[str, Any] | None:
        governor = outcome.evaluation.metrics.get("governor")
        if not isinstance(governor, Mapping):
            return None
        diagnostic = governor.get("nontrainable_profile_diagnostic")
        if not isinstance(diagnostic, Mapping):
            return None
        failure_scope = diagnostic.get("failure_scope")
        failures = outcome.evaluation.structural_failures
        legacy_scope = bool(
            failure_scope == "orientation_profile_unqualified"
            and failures == ("orientation_profile_unqualified",)
        )
        diagnostic_scope = bool(
            failure_scope == "governor_profile_nontrainable"
            and diagnostic.get("structural_failures") == tuple(failures)
            and failures
            and set(failures).issubset(
                {
                    "orientation_profile_unqualified",
                    "cadence_failed",
                    "feedback_failed",
                }
            )
        )
        if any(
            (
                diagnostic.get("available") is not True,
                diagnostic.get("trainable_objective") is not False,
                not (legacy_scope or diagnostic_scope),
                outcome.evaluation.eligible,
            )
        ):
            return None
        return diagnostic

    @classmethod
    def _is_profile_diagnostic(cls, outcome: Observation) -> bool:
        return cls._profile_diagnostic_payload(outcome) is not None

    @staticmethod
    def _bind_identity(
        *,
        label: str,
        supplied: AbTrialIdentity | None,
        actual: AbTrialIdentity,
    ) -> AbTrialIdentity:
        if supplied is not None and supplied != actual:
            raise ValueError(
                f"caller {label} identity differs from the supervisor-closed trial"
            )
        return actual

    @classmethod
    def _actual_mae(cls, outcome: Observation) -> float | None:
        if outcome.evaluation.eligible:
            return outcome.evaluation.objective_mae_n
        diagnostic = cls._profile_diagnostic_payload(outcome)
        if diagnostic is None:
            return None
        value = diagnostic.get("force_mae_n")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        parsed = float(value)
        return parsed if math.isfinite(parsed) and parsed >= 0.0 else None

    @classmethod
    def _require_actual_mae(
        cls, label: str, supplied: float, outcome: Observation
    ) -> None:
        actual = cls._actual_mae(outcome)
        if actual is None or not math.isclose(
            float(supplied), float(actual), rel_tol=0.0, abs_tol=1e-12
        ):
            raise ValueError(
                f"caller {label} MAE differs from the supervisor-closed evaluation"
            )

    @staticmethod
    def _finite_governor_metric(name: str, value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RuntimeError(f"immutable governor metric {name} is not numeric")
        parsed = float(value)
        if not math.isfinite(parsed):
            raise RuntimeError(f"immutable governor metric {name} is not finite")
        return parsed

    @classmethod
    def _governor_trial_metrics(
        cls,
        outcome: Observation,
        *,
        layer: str,
        allow_profile_diagnostic: bool = False,
    ) -> dict[str, Any]:
        profile_diagnostic = cls._is_profile_diagnostic(outcome)
        if not outcome.eligible and not (
            allow_profile_diagnostic and profile_diagnostic
        ):
            raise RuntimeError("governor trial is neither eligible nor a profile diagnostic")
        governor = outcome.evaluation.metrics.get("governor")
        if not isinstance(governor, Mapping):
            raise RuntimeError("eligible governor trial lacks immutable metrics")
        burdens = governor.get("burden_by_layer")
        burden_complete = governor.get("burden_evidence_complete_by_layer")
        tracking = governor.get("tracking")
        orientation = governor.get("orientation")
        if not all(
            isinstance(value, Mapping)
            for value in (burdens, burden_complete, tracking, orientation)
        ):
            raise RuntimeError("immutable governor metric groups are incomplete")
        assert isinstance(burdens, Mapping)
        assert isinstance(burden_complete, Mapping)
        assert isinstance(tracking, Mapping)
        assert isinstance(orientation, Mapping)
        if burden_complete.get(layer) is not True:
            raise RuntimeError(f"immutable governor burden evidence is incomplete for {layer}")
        if tracking.get("evidence_complete") is not True:
            raise RuntimeError("immutable governor tracking evidence is incomplete")
        if orientation.get("evidence_complete") is not True:
            raise RuntimeError("immutable governor orientation evidence is incomplete")
        if orientation.get("qualified") is not True and not profile_diagnostic:
            raise RuntimeError("immutable governor orientation qualification failed")
        return {
            "profile_diagnostic": profile_diagnostic,
            "burden": cls._finite_governor_metric(
                f"{layer}.burden", burdens.get(layer)
            ),
            "lag_s": cls._finite_governor_metric("tracking.lag_s", tracking.get("lag_s")),
            "correlation": cls._finite_governor_metric(
                "tracking.correlation", tracking.get("correlation")
            ),
            "nrmse": cls._finite_governor_metric(
                "tracking.nrmse", tracking.get("nrmse")
            ),
            "orientation_p95": cls._finite_governor_metric(
                "orientation.p95_error_rad", orientation.get("p95_error_rad")
            ),
            "orientation_max": cls._finite_governor_metric(
                "orientation.max_error_rad", orientation.get("max_error_rad")
            ),
            "orientation_duty": cls._finite_governor_metric(
                "orientation.saturation_duty", orientation.get("saturation_duty")
            ),
        }

    @staticmethod
    def _require_supplied_metric(name: str, supplied: Any, actual: Any) -> None:
        if isinstance(actual, bool):
            if supplied is not actual:
                raise ValueError(
                    f"caller {name} differs from immutable trial evidence"
                )
            return
        try:
            matches = math.isclose(
                float(supplied), float(actual), rel_tol=0.0, abs_tol=1e-12
            )
        except (TypeError, ValueError):
            matches = False
        if not matches:
            raise ValueError(f"caller {name} differs from immutable trial evidence")

    @classmethod
    def _derived_governor_metrics(
        cls,
        *,
        layer: str,
        outcome_a: Observation,
        outcome_b: Observation,
        outcome_a_prime: Observation | None,
    ) -> dict[str, Any]:
        a = cls._governor_trial_metrics(
            outcome_a, layer=layer, allow_profile_diagnostic=True
        )
        b = cls._governor_trial_metrics(
            outcome_b, layer=layer, allow_profile_diagnostic=True
        )
        a_prime = (
            None
            if outcome_a_prime is None
            else cls._governor_trial_metrics(
                outcome_a_prime,
                layer=layer,
                allow_profile_diagnostic=True,
            )
        )
        references = [a] if a_prime is None else [a, a_prime]
        lag_reference = min(row["lag_s"] for row in references)
        correlation_reference = max(row["correlation"] for row in references)
        nrmse_reference = min(row["nrmse"] for row in references)
        tracking_not_worse = bool(
            b["lag_s"] <= lag_reference + 1e-12
            and b["correlation"] + 1e-12 >= correlation_reference
            and b["nrmse"] <= nrmse_reference + 1e-12
        )
        orientation_not_worse = bool(
            b["orientation_p95"]
            <= min(row["orientation_p95"] for row in references) + 1e-12
            and b["orientation_max"]
            <= min(row["orientation_max"] for row in references) + 1e-12
            and b["orientation_duty"]
            <= min(row["orientation_duty"] for row in references) + 1e-12
        )
        guards_clean = bool(
            (
                outcome_b.evaluation.eligible
                and not outcome_b.evaluation.structural_failures
                or cls._is_profile_diagnostic(outcome_b)
            )
            and all(
                row.evaluation.eligible
                and not row.evaluation.structural_failures
                or cls._is_profile_diagnostic(row)
                for row in (
                    outcome_a,
                    *((outcome_a_prime,) if outcome_a_prime is not None else ()),
                )
            )
        )
        safe_closure = all(
            row.evaluation.safe_closure
            for row in (
                outcome_a,
                outcome_b,
                *((outcome_a_prime,) if outcome_a_prime is not None else ()),
            )
        )
        return {
            "burden_a": a["burden"],
            "burden_b": b["burden"],
            "tracking_not_worse": tracking_not_worse,
            "orientation_not_worse": orientation_not_worse,
            "guards_clean": guards_clean,
            "safe_closure": safe_closure,
            "lag_improvement_s": lag_reference - b["lag_s"],
            "nrmse_improvement_ratio": (
                nrmse_reference - b["nrmse"]
            ) / max(nrmse_reference, 1e-12),
            "correlation_improvement": b["correlation"] - correlation_reference,
            "burden_a_prime": None if a_prime is None else a_prime["burden"],
        }

    @classmethod
    def _bound_governor_metrics(
        cls,
        *,
        layer: str,
        evidence: AbEvidence,
        outcome_a: Observation,
        outcome_b: Observation,
        outcome_a_prime: Observation | None,
    ) -> dict[str, Any]:
        actual = cls._derived_governor_metrics(
            layer=layer,
            outcome_a=outcome_a,
            outcome_b=outcome_b,
            outcome_a_prime=outcome_a_prime,
        )
        for name, value in actual.items():
            supplied = getattr(evidence, name)
            if value is None:
                if supplied is not None:
                    raise ValueError(
                        f"caller {name} has no immutable A-prime trial"
                    )
            else:
                cls._require_supplied_metric(name, supplied, value)
        return actual

    def begin_governor_probe(
        self,
        samples: Sequence[SaturationSample] | None = None,
    ) -> tuple[ExecutionProfile | None, GovernorDecision]:
        if (
            self.phase is not CampaignPhase.HOME
            or self._active is not None
            or self._pending_ack is not None
            or self._prepared_ack is not None
        ):
            raise RuntimeError("governor may probe only while safely at campaign home")
        if self._pending_retry is not None:
            raise RuntimeError("governor cannot bypass a durable infra/code pause")
        if self._governor_probe is not None:
            raise RuntimeError("one governor A/B layer is already active")
        accepted_a = [
            outcome
            for outcome in self.outcome_timeline
            if outcome.profile_id == self.execution_profile.profile_id
            and outcome.plant_epoch == self.plant_epoch
            and outcome.evaluation.safe_closure
            and (outcome.eligible or self._is_profile_diagnostic(outcome))
        ]
        if not accepted_a:
            raise RuntimeError(
                "governor probe requires a completed eligible or profile-diagnostic safe A trial"
            )
        outcome_a = accepted_a[-1]
        identity_a = self._ab_identity(outcome_a)
        governor = outcome_a.evaluation.metrics.get("governor")
        trigger_metrics = (
            governor.get("trigger") if isinstance(governor, Mapping) else None
        )
        if not isinstance(trigger_metrics, Mapping):
            raise RuntimeError("governor A trial lacks immutable trigger evidence")
        if trigger_metrics.get("source") != "immutable_trial_csv":
            raise RuntimeError("governor trigger evidence is not trace-derived")
        sample_count = trigger_metrics.get("sample_count")
        if (
            isinstance(sample_count, bool)
            or not isinstance(sample_count, int)
            or sample_count < 2
        ):
            raise RuntimeError("governor trigger evidence has insufficient samples")
        trigger_fields = {
            name: trigger_metrics.get(name)
            for name in SaturationTriggerEvidence.__dataclass_fields__
        }
        trigger = SaturationTriggerEvidence.from_payload(trigger_fields)
        if samples is not None:
            supplied_trigger = SaturationTriggerEvidence.from_samples(samples)
            if supplied_trigger != trigger:
                raise ValueError(
                    "caller saturation trigger differs from immutable trial evidence"
                )
        candidate, decision = propose_change_from_trigger(
            self.execution_profile,
            trigger,
            plant_epoch=self.plant_epoch,
            cooldown_remaining=self._cooldown_remaining,
            evidence_available=True,
        )
        if candidate is not None:
            self._governor_trial_metrics(
                outcome_a,
                layer=decision.layer,
                allow_profile_diagnostic=True,
            )
            self._governor_probe = GovernorProbeState(
                profile_a=self.execution_profile,
                profile_b=candidate,
                layer=decision.layer,
                force_candidate=outcome_a.candidate,
                identity_a=identity_a,
            )
        return candidate, decision

    def build_governor_evidence(self) -> AbEvidence:
        """Derive A/B evidence only from supervisor-closed immutable trials."""

        probe = self._governor_probe
        if probe is None or probe.identity_b is None:
            raise RuntimeError("closed governor A/B trials are not available")
        outcome_a = self._closed_governor_outcome(
            probe.identity_a, allow_profile_diagnostic=True
        )
        outcome_b = self._closed_governor_outcome(
            probe.identity_b, allow_profile_diagnostic=True
        )
        outcome_a_prime = (
            None
            if probe.identity_a_prime is None
            else self._closed_governor_outcome(
                probe.identity_a_prime, allow_profile_diagnostic=True
            )
        )
        mae_a = self._actual_mae(outcome_a)
        mae_b = self._actual_mae(outcome_b)
        mae_a_prime = (
            None if outcome_a_prime is None else self._actual_mae(outcome_a_prime)
        )
        if mae_a is None or mae_b is None or (
            outcome_a_prime is not None and mae_a_prime is None
        ):
            raise RuntimeError("governor closed trial lacks measured force MAE")
        metrics = self._derived_governor_metrics(
            layer=probe.layer,
            outcome_a=outcome_a,
            outcome_b=outcome_b,
            outcome_a_prime=outcome_a_prime,
        )
        return AbEvidence(
            **metrics,
            mae_a_n=mae_a,
            mae_b_n=mae_b,
            mae_a_prime_n=mae_a_prime,
            phase="a_prime" if outcome_a_prime is not None else "ab",
            identity_a=probe.identity_a,
            identity_b=probe.identity_b,
            identity_a_prime=probe.identity_a_prime,
        )

    def complete_governor_probe(self, evidence: AbEvidence) -> GovernorDecision:
        probe = self._governor_probe
        if probe is None:
            raise RuntimeError("no governor A/B probe is active")
        if self.phase is not CampaignPhase.HOME or self._active is not None:
            raise RuntimeError(
                "governor may complete only after the probe trial ACK closes at home"
            )
        if probe.stage == "b":
            if probe.identity_b is None:
                raise RuntimeError("governor B trial has not closed eligible and safe")
            if evidence.phase != "ab":
                raise ValueError("B-stage completion requires ab evidence")
        else:
            if probe.identity_a_prime is None:
                raise RuntimeError("governor A-prime trial has not closed eligible and safe")
            if evidence.phase != "a_prime":
                raise ValueError("A-prime completion requires a_prime evidence")

        outcome_a = self._closed_governor_outcome(
            probe.identity_a, allow_profile_diagnostic=True
        )
        assert probe.identity_b is not None
        outcome_b = self._closed_governor_outcome(
            probe.identity_b, allow_profile_diagnostic=True
        )
        self._require_actual_mae("A", evidence.mae_a_n, outcome_a)
        self._require_actual_mae("B", evidence.mae_b_n, outcome_b)
        actual_a_prime = probe.identity_a_prime
        outcome_a_prime: Observation | None = None
        if actual_a_prime is not None:
            outcome_a_prime = self._closed_governor_outcome(
                actual_a_prime, allow_profile_diagnostic=True
            )
            if evidence.mae_a_prime_n is None:
                raise ValueError("A-prime completion requires its measured MAE")
            self._require_actual_mae(
                "A-prime", evidence.mae_a_prime_n, outcome_a_prime
            )
        elif evidence.identity_a_prime is not None:
            raise ValueError(
                "caller A-prime identity has no supervisor-closed trial"
            )
        actual_metrics = self._bound_governor_metrics(
            layer=probe.layer,
            evidence=evidence,
            outcome_a=outcome_a,
            outcome_b=outcome_b,
            outcome_a_prime=outcome_a_prime,
        )
        bound_evidence = replace(
            evidence,
            **actual_metrics,
            identity_a=self._bind_identity(
                label="A", supplied=evidence.identity_a, actual=probe.identity_a
            ),
            identity_b=self._bind_identity(
                label="B", supplied=evidence.identity_b, actual=probe.identity_b
            ),
            identity_a_prime=(
                None
                if actual_a_prime is None
                else self._bind_identity(
                    label="A-prime",
                    supplied=evidence.identity_a_prime,
                    actual=actual_a_prime,
                )
            ),
        )
        decision = assess_ab(
            layer=probe.layer,
            profile_a=probe.profile_a,
            profile_b=probe.profile_b,
            evidence=bound_evidence,
            plant_epoch=self.plant_epoch,
        )
        if decision.action == "repeat_a_prime":
            self._governor_probe = replace(probe, stage="a_prime")
            return decision
        self._governor_probe = None
        if decision.keep is True:
            self.execution_profile = probe.profile_b
            self.plant_epoch = decision.plant_epoch_after
        else:
            self.execution_profile = probe.profile_a
        self._cooldown_remaining = decision.cooldown_eligible_trials
        return decision

    def recovery_snapshot(self) -> SupervisorRecoverySnapshot:
        """Return a defensive copy for journal serialization and reconciliation."""

        return SupervisorRecoverySnapshot(
            phase=self.phase,
            outcome_timeline=tuple(self.outcome_timeline),
            trial_counter=self._trial_counter,
            command_seq=self._command_seq,
            candidate_tokens=dict(self._candidate_tokens),
            active=self._active,
            pending_retry=self._pending_retry,
            pending_ack=self._pending_ack,
            prepared_ack=self._prepared_ack,
            cooldown_remaining=self._cooldown_remaining,
            governor_probe=self._governor_probe,
        )

    def restore_recovery_snapshot(
        self,
        snapshot: SupervisorRecoverySnapshot,
    ) -> None:
        """Restore one already-verified journal/history state into a fresh manager."""

        if not isinstance(snapshot, SupervisorRecoverySnapshot):
            raise ValueError("recovery snapshot has the wrong type")
        if self.phase is not CampaignPhase.HOME or any(
            (
                self.outcome_timeline,
                self.observations,
                self._candidate_tokens,
                self._active,
                self._pending_retry,
                self._pending_ack,
                self._prepared_ack,
            )
        ):
            raise RuntimeError("recovery may populate only a fresh CampaignSupervisor")
        for name, value in (
            ("trial_counter", snapshot.trial_counter),
            ("command_seq", snapshot.command_seq),
            ("cooldown_remaining", snapshot.cooldown_remaining),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not isinstance(snapshot.phase, CampaignPhase):
            raise ValueError("recovery phase must be CampaignPhase")
        if any(not isinstance(row, Observation) for row in snapshot.outcome_timeline):
            raise ValueError("recovery outcome timeline contains an invalid row")
        tokens = dict(snapshot.candidate_tokens)
        if any(
            not isinstance(uid, str)
            or not uid
            or isinstance(token, bool)
            or not isinstance(token, int)
            or token < 1
            for uid, token in tokens.items()
        ):
            raise ValueError("recovery candidate token map is invalid")
        if set(tokens.values()) != set(range(1, len(tokens) + 1)):
            raise ValueError("recovery candidate tokens must be unique and contiguous")
        if snapshot.active is not None and not isinstance(snapshot.active, TrialIntent):
            raise ValueError("recovery active intent is invalid")
        if (snapshot.phase is CampaignPhase.TRIAL_ACTIVE) != (
            snapshot.active is not None
        ):
            raise ValueError("recovery active intent differs from phase")
        if (snapshot.phase is CampaignPhase.WAIT_ACK) != (
            snapshot.pending_ack is not None
        ):
            raise ValueError("recovery pending ACK differs from phase")
        pending_trial_uid = (
            None
            if snapshot.pending_ack is None
            else snapshot.pending_ack[0].trial.trial_uid
        )
        if pending_trial_uid is not None:
            pending_outcomes = [
                outcome
                for outcome in snapshot.outcome_timeline
                if outcome.evaluation.trial_uid == pending_trial_uid
            ]
            if len(pending_outcomes) != 1:
                raise ValueError(
                    "recovery pending ACK requires one exact closed outcome"
                )
        if snapshot.prepared_ack is not None:
            if snapshot.pending_ack is None or not isinstance(
                snapshot.prepared_ack, HostPacket
            ):
                raise ValueError("recovery prepared ACK lacks a pending ACK")
            intent, _ = snapshot.pending_ack
            trial = intent.trial
            packet = snapshot.prepared_ack
            if any(
                (
                    packet.command is not HostCommand.ACK_BUNDLE,
                    packet.campaign_epoch != trial.campaign.campaign_epoch,
                    packet.trial_id != trial.trial_id,
                    packet.candidate_token != trial.candidate_token,
                    packet.execution_profile_id
                    != intent.execution_profile_integer_id,
                    packet.command_seq > snapshot.command_seq,
                )
            ):
                raise ValueError("recovery prepared ACK identity is invalid")
        if snapshot.cooldown_remaining < 0:
            raise ValueError("recovery cooldown cannot be negative")
        probe = snapshot.governor_probe
        if probe is not None:
            if not isinstance(probe, GovernorProbeState):
                raise ValueError("recovery governor probe is invalid")
            if (
                self.execution_profile != probe.profile_a
                or self.plant_epoch != probe.identity_a.plant_epoch
            ):
                raise ValueError(
                    "recovery governor probe differs from retained profile/plant epoch"
                )
            outcomes_by_uid: dict[str, list[Observation]] = {}
            for outcome in snapshot.outcome_timeline:
                outcomes_by_uid.setdefault(
                    outcome.evaluation.trial_uid, []
                ).append(outcome)
            for label, identity in (
                ("A", probe.identity_a),
                ("B", probe.identity_b),
                ("A-prime", probe.identity_a_prime),
            ):
                if identity is None:
                    continue
                rows = outcomes_by_uid.get(identity.trial_uid, [])
                if (
                    len(rows) != 1
                    or not rows[0].eligible
                    or not rows[0].evaluation.safe_closure
                    or self._ab_identity(rows[0]) != identity
                ):
                    raise ValueError(
                        f"recovery governor {label} identity lacks one closed outcome"
                    )
            transitional = (
                snapshot.active
                if snapshot.active is not None
                else snapshot.pending_ack[0]
                if snapshot.pending_ack is not None
                else None
            )
            if transitional is not None and any(
                (
                    transitional.trial.candidate != probe.force_candidate,
                    transitional.trial.execution_profile != probe.expected_profile,
                    transitional.trial.plant_epoch != probe.identity_a.plant_epoch,
                )
            ):
                raise ValueError(
                    "recovery governor transitional trial breaks frozen binding"
                )
        self.phase = snapshot.phase
        self.outcome_timeline = list(snapshot.outcome_timeline)
        self.observations = [
            row
            for row in self.outcome_timeline
            if row.eligible
            and row.evaluation.trial_uid != pending_trial_uid
        ]
        self._trial_counter = snapshot.trial_counter
        self._command_seq = snapshot.command_seq
        self._candidate_tokens = tokens
        self._next_candidate_token = len(tokens) + 1
        self._active = snapshot.active
        self._pending_retry = snapshot.pending_retry
        self._pending_ack = snapshot.pending_ack
        self._prepared_ack = snapshot.prepared_ack
        self._cooldown_remaining = snapshot.cooldown_remaining
        self._governor_probe = probe

    def snapshot(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign.campaign_id,
            "campaign_epoch": self.campaign.campaign_epoch,
            "campaign_fingerprint": self.campaign.campaign_fingerprint,
            "phase": self.phase.value,
            "plant_epoch": self.plant_epoch,
            "execution_profile": self.execution_profile.payload(),
            "eligible_observations": len(self.observations),
            "outcome_timeline_count": len(self.outcome_timeline),
            "active_trial_uid": None if self.active_trial is None else self.active_trial.trial_uid,
            "same_candidate_retry_pending": False,
            "durable_pause_origin_pending": self._pending_retry is not None,
            "cooldown_remaining": self._cooldown_remaining,
            "governor_probe_stage": (
                None if self._governor_probe is None else self._governor_probe.stage
            ),
            "trial_budget_stop_enabled": False,
            "low_ei_stop_enabled": False,
        }
