"""Crash-safe physical campaign runner for Autotuner V5.

The runner is the sole fan-in point between a resident single-writer owner,
the sealed physical admission ledger, the exactly-once optimizer journal, and
the role-isolated campaign decision ledger.  It never reads a sensor or RTDE
stream itself and it never constructs a performance/readiness gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

try:  # Tests may put ``tools`` directly on sys.path.
    from step6_figure8_autotune_v1.v5_campaign import (
        CampaignRoleV2,
        OutcomeV2,
        TrialStageV2,
        V5CampaignV2,
        V5TrialPlan,
    )
    from step6_figure8_autotune_v1.v5_composition_contract import (
        RolloverCommand,
        V5TPState,
    )
    from step6_figure8_autotune_v1.v5_lifecycle_ledger import (
        BoundaryMode,
        FigureEightPhysicalRecordV2,
        LedgerRole,
        TellState,
        V5PhysicalAdmissionLedgerV2,
        bind_v5_chain,
        canonical_sha256,
        cold_activation_publication_boundaries,
        physical_record_from_mapping,
    )
    from step6_figure8_autotune_v1.v5_live_owner import (
        V5LiveChainRequestV1,
        V5RolloverActivationJournalV1,
        V5SealedChainResultV1,
        V5_SEALED_RESULT_SCHEMA,
        V5_SEALED_RESULT_VERSION,
        candidate_identity_from_plan,
    )
    from step6_figure8_autotune_v1.v5_optimizer_journal import (
        V5OptimizerTellJournalV1,
    )
except ModuleNotFoundError:  # pragma: no cover - repository-root imports
    from tools.step6_figure8_autotune_v1.v5_campaign import (
        CampaignRoleV2,
        OutcomeV2,
        TrialStageV2,
        V5CampaignV2,
        V5TrialPlan,
    )
    from tools.step6_figure8_autotune_v1.v5_composition_contract import (
        RolloverCommand,
        V5TPState,
    )
    from tools.step6_figure8_autotune_v1.v5_lifecycle_ledger import (
        BoundaryMode,
        FigureEightPhysicalRecordV2,
        LedgerRole,
        TellState,
        V5PhysicalAdmissionLedgerV2,
        bind_v5_chain,
        canonical_sha256,
        cold_activation_publication_boundaries,
        physical_record_from_mapping,
    )
    from tools.step6_figure8_autotune_v1.v5_live_owner import (
        V5LiveChainRequestV1,
        V5RolloverActivationJournalV1,
        V5SealedChainResultV1,
        V5_SEALED_RESULT_SCHEMA,
        V5_SEALED_RESULT_VERSION,
        candidate_identity_from_plan,
    )
    from tools.step6_figure8_autotune_v1.v5_optimizer_journal import (
        V5OptimizerTellJournalV1,
    )


EXECUTION_SCHEMA = "step6.autotune/figure8-v5-execution-journal-v1"
EXECUTION_VERSION = 1
RECOVERABLE_FAILURE_SCHEMA = "step6.autotune/figure8-v5-recoverable-failure-v1"
RECOVERABLE_FAILURE_VERSION = 1
RECOVERABLE_CONTINUATION_MARKER = "V5_RECOVERABLE_CONTINUATION_V1"
GENESIS_SHA256 = "0" * 64
_NOVEL_STAGES = frozenset(
    {TrialStageV2.PRIMARY_NOVEL, TrialStageV2.CORRECTION_NOVEL}
)


class V5CampaignRunnerError(RuntimeError):
    """A V5 physical/campaign transition could not be proven exact."""


class V5AmbiguousPhysicalDispatch(V5CampaignRunnerError):
    """Motion was dispatched but no sealed result became durable."""


class V5RecoverableFailureClass(str, Enum):
    """The only owner failure admitted to the V5 continuation seam."""

    CONNECTION_RESET = "connection_reset"


def _valid_sha(value: Any, role: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise V5CampaignRunnerError(f"{role} must be a lowercase SHA-256")
    return value


def _valid_path(value: Any, role: str) -> str:
    if not isinstance(value, str) or not value:
        raise V5CampaignRunnerError(f"{role} path is missing")
    path = Path(value)
    if path != path.resolve() or path.is_symlink():
        raise V5CampaignRunnerError(f"{role} path must be resolved and non-symlinked")
    return str(path)


@dataclass(frozen=True)
class V5RecoverableFailureReceiptV1:
    """Typed owner evidence that closes one physical dispatch without GP data.

    The lifecycle receipt is intentionally retained as incomplete evidence;
    the separate writer-release receipt is the sole continuation authority for
    Home/STOPPED/writer-released state.  No field in this receipt authorizes a
    second dispatch on the same owner epoch.
    """

    chain_id: str
    failure_class: V5RecoverableFailureClass
    reason: str
    partial_receipt_path: str
    partial_receipt_sha256: str
    partial_artifact_path: str
    partial_artifact_sha256: str
    writer_release_receipt_path: str
    writer_release_receipt_sha256: str
    partial_receipt: Mapping[str, Any]
    writer_release_receipt: Mapping[str, Any]
    home_verified: bool = True
    physical_dispatch_counted: bool = True
    gp_eligible: bool = False
    schema: str = RECOVERABLE_FAILURE_SCHEMA
    version: int = RECOVERABLE_FAILURE_VERSION

    def __post_init__(self) -> None:
        if (
            not isinstance(self.chain_id, str)
            or not self.chain_id
            or any(
                character
                not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
                for character in self.chain_id
            )
        ):
            raise V5CampaignRunnerError("recoverable failure chain identity is invalid")
        try:
            failure_class = (
                self.failure_class
                if isinstance(self.failure_class, V5RecoverableFailureClass)
                else V5RecoverableFailureClass(self.failure_class)
            )
        except (TypeError, ValueError) as exc:
            raise V5CampaignRunnerError(
                "recoverable failure class is not typed"
            ) from exc
        if self.schema != RECOVERABLE_FAILURE_SCHEMA or self.version != RECOVERABLE_FAILURE_VERSION:
            raise V5CampaignRunnerError("recoverable failure schema/version differs")
        if not isinstance(self.reason, str) or "connection reset by peer" not in self.reason.lower():
            raise V5CampaignRunnerError(
                "connection-reset continuation reason is not canonical"
            )
        for name in ("home_verified", "physical_dispatch_counted", "gp_eligible"):
            if type(getattr(self, name)) is not bool:
                raise V5CampaignRunnerError(
                    f"recoverable failure {name} must be bool"
                )
        if not self.home_verified or not self.physical_dispatch_counted or self.gp_eligible:
            raise V5CampaignRunnerError(
                "recoverable continuation requires verified Home and a non-GP counted dispatch"
            )
        for value, role in (
            (self.partial_receipt_path, "partial lifecycle receipt"),
            (self.partial_artifact_path, "partial lifecycle artifact"),
            (self.writer_release_receipt_path, "writer release receipt"),
        ):
            _valid_path(value, role)
        for value, role in (
            (self.partial_receipt_sha256, "partial lifecycle receipt"),
            (self.partial_artifact_sha256, "partial lifecycle artifact"),
            (self.writer_release_receipt_sha256, "writer release receipt"),
        ):
            _valid_sha(value, role)
        if not isinstance(self.partial_receipt, Mapping) or not isinstance(
            self.writer_release_receipt, Mapping
        ):
            raise V5CampaignRunnerError("recoverable failure evidence is untyped")
        partial = dict(self.partial_receipt)
        release = dict(self.writer_release_receipt)
        if (
            partial.get("artifact_path") != self.partial_artifact_path
            or partial.get("artifact_sha256") != self.partial_artifact_sha256
            or partial.get("status") != "incomplete"
            or partial.get("coverage_complete") is not False
            or partial.get("home_verified") is not False
        ):
            raise V5CampaignRunnerError(
                "partial lifecycle receipt is not explicitly incomplete"
            )
        if (
            release.get("home_verified") is not True
            or release.get("stopped") is not True
            or release.get("writer_released") is not True
        ):
            raise V5CampaignRunnerError(
                "writer release receipt is not verified Home/STOPPED/released"
            )
        object.__setattr__(self, "failure_class", failure_class)
        object.__setattr__(self, "partial_receipt", partial)
        object.__setattr__(self, "writer_release_receipt", release)
        object.__setattr__(self, "partial_receipt_path", _valid_path(self.partial_receipt_path, "partial lifecycle receipt"))
        object.__setattr__(self, "partial_artifact_path", _valid_path(self.partial_artifact_path, "partial lifecycle artifact"))
        object.__setattr__(self, "writer_release_receipt_path", _valid_path(self.writer_release_receipt_path, "writer release receipt"))

    @property
    def failure_signature(self) -> str:
        return f"v5.recoverable.{self.failure_class.value}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "chain_id": self.chain_id,
            "failure_class": self.failure_class.value,
            "reason": self.reason,
            "partial_receipt_path": self.partial_receipt_path,
            "partial_receipt_sha256": self.partial_receipt_sha256,
            "partial_artifact_path": self.partial_artifact_path,
            "partial_artifact_sha256": self.partial_artifact_sha256,
            "writer_release_receipt_path": self.writer_release_receipt_path,
            "writer_release_receipt_sha256": self.writer_release_receipt_sha256,
            "partial_receipt": dict(self.partial_receipt),
            "writer_release_receipt": dict(self.writer_release_receipt),
            "home_verified": self.home_verified,
            "physical_dispatch_counted": self.physical_dispatch_counted,
            "gp_eligible": self.gp_eligible,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "V5RecoverableFailureReceiptV1":
        allowed = {
            "schema", "version", "chain_id", "failure_class", "reason",
            "partial_receipt_path", "partial_receipt_sha256",
            "partial_artifact_path", "partial_artifact_sha256",
            "writer_release_receipt_path", "writer_release_receipt_sha256",
            "partial_receipt", "writer_release_receipt", "home_verified",
            "physical_dispatch_counted", "gp_eligible",
        }
        if not isinstance(value, Mapping) or set(value) != allowed:
            raise V5CampaignRunnerError("recoverable failure receipt keys differ")
        try:
            return cls(
                str(value["chain_id"]),
                V5RecoverableFailureClass(value["failure_class"]),
                str(value["reason"]),
                str(value["partial_receipt_path"]),
                str(value["partial_receipt_sha256"]),
                str(value["partial_artifact_path"]),
                str(value["partial_artifact_sha256"]),
                str(value["writer_release_receipt_path"]),
                str(value["writer_release_receipt_sha256"]),
                value["partial_receipt"],
                value["writer_release_receipt"],
                value["home_verified"],
                value["physical_dispatch_counted"],
                value["gp_eligible"],
                schema=value["schema"],
                version=value["version"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise V5CampaignRunnerError(
                "recoverable failure receipt mapping is invalid"
            ) from exc


class V5RecoverableOwnerFailure(V5CampaignRunnerError):
    """Typed owner exception eligible for exactly one serial continuation."""

    def __init__(self, receipt: V5RecoverableFailureReceiptV1):
        if not isinstance(receipt, V5RecoverableFailureReceiptV1):
            raise TypeError("recoverable owner failure requires a typed receipt")
        self.receipt = receipt
        super().__init__(
            f"{RECOVERABLE_CONTINUATION_MARKER}: {receipt.reason}"
        )


# Descriptive alias retained for callers that name the cause explicitly.
V5RecoverableConnectionReset = V5RecoverableOwnerFailure


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise V5CampaignRunnerError(
            "execution journal value is not canonical JSON"
        ) from exc


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _verify_recoverable_failure_files(
    receipt: V5RecoverableFailureReceiptV1,
) -> None:
    """Cold-verify retained partial evidence and the independent Home proof."""

    for path_value, expected, role in (
        (
            receipt.partial_receipt_path,
            receipt.partial_receipt_sha256,
            "partial lifecycle receipt",
        ),
        (
            receipt.partial_artifact_path,
            receipt.partial_artifact_sha256,
            "partial lifecycle artifact",
        ),
        (
            receipt.writer_release_receipt_path,
            receipt.writer_release_receipt_sha256,
            "writer release receipt",
        ),
    ):
        path = Path(path_value)
        if not path.is_file() or path.is_symlink():
            raise V5CampaignRunnerError(f"{role} is absent or symlinked")
        try:
            observed = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise V5CampaignRunnerError(f"{role} is unreadable") from exc
        if observed != expected:
            raise V5CampaignRunnerError(f"{role} bytes differ from its receipt")
    try:
        partial_raw = json.loads(
            Path(receipt.partial_receipt_path).read_text(encoding="utf-8")
        )
        release_raw = json.loads(
            Path(receipt.writer_release_receipt_path).read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise V5CampaignRunnerError("recoverable owner evidence is unreadable") from exc
    if partial_raw != dict(receipt.partial_receipt) or release_raw != dict(
        receipt.writer_release_receipt
    ):
        raise V5CampaignRunnerError("recoverable owner evidence differs from bytes")


def _ledger_role(role: CampaignRoleV2) -> LedgerRole:
    return LedgerRole.PRIMARY if role is CampaignRoleV2.PRIMARY else LedgerRole.CORRECTION


@dataclass(frozen=True)
class V5DurableChainResultV1:
    chain_id: str
    plans: tuple[V5TrialPlan, ...]
    records: tuple[FigureEightPhysicalRecordV2, ...]
    lifecycle_receipt: Mapping[str, Any]
    event_bundle_sha256: str
    home_evidence_sha256: str
    activation_receipt: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.chain_id or not self.plans or len(self.plans) != len(self.records):
            raise V5CampaignRunnerError("durable chain result cardinality differs")
        if tuple(plan.trial_id for plan in self.plans) != tuple(
            record.trial_id for record in self.records
        ):
            raise V5CampaignRunnerError("durable chain result trial order differs")
        if any(
            record.boundary.mode is not BoundaryMode.CONTACT_ROLLOVER
            for record in self.records[:-1]
        ) or self.records[-1].boundary.mode is not BoundaryMode.HOME:
            raise V5CampaignRunnerError(
                "durable chain boundary sequence is not rollover* then Home"
            )
        for value, role in (
            (self.event_bundle_sha256, "event bundle"),
            (self.home_evidence_sha256, "Home evidence"),
        ):
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise V5CampaignRunnerError(f"{role} hash is invalid")
        if not isinstance(self.activation_receipt, Mapping):
            raise V5CampaignRunnerError("activation receipt is absent")


def validate_durable_chain_result(
    result: V5DurableChainResultV1,
    *,
    campaign_fingerprint: str,
    release_identity_sha256: str,
    role: LedgerRole,
    durable_result_path: Path,
    expected_plans: Sequence[V5TrialPlan] | None = None,
) -> None:
    """Cold-reconstruct one owner-sealed chain without campaign side effects."""

    if not isinstance(result, V5DurableChainResultV1) or not isinstance(role, LedgerRole):
        raise V5CampaignRunnerError("durable chain result type/role differs")
    plans = tuple(result.plans)
    records = tuple(result.records)
    if expected_plans is not None and plans != tuple(expected_plans):
        raise V5CampaignRunnerError("durable chain plans differ from dispatch")
    if not records or any(
        not isinstance(record, FigureEightPhysicalRecordV2) for record in records
    ):
        raise V5CampaignRunnerError("durable chain records are not typed")
    first_artifact = records[0].source_artifact
    artifact_identity = (
        first_artifact.artifact_path,
        first_artifact.artifact_sha256,
        first_artifact.artifact_size,
    )
    if (
        dict(result.lifecycle_receipt) != dict(first_artifact.receipt)
        or result.event_bundle_sha256
        != canonical_sha256(dict(first_artifact.event_bundle))
    ):
        raise V5CampaignRunnerError("durable chain artifact receipts differ")
    for plan, record in zip(plans, records, strict=True):
        if (
            record.chain_id != result.chain_id
            or record.trial_id != plan.trial_id
            or record.attempt_id != plan.attempt_id
            or record.candidate_identity != candidate_identity_from_plan(plan)
            or record.campaign_fingerprint != campaign_fingerprint
            or record.release_identity_sha256 != release_identity_sha256
            or record.role is not role
            or (
                record.source_artifact.artifact_path,
                record.source_artifact.artifact_sha256,
                record.source_artifact.artifact_size,
            )
            != artifact_identity
            or dict(record.source_artifact.receipt)
            != dict(first_artifact.receipt)
            or canonical_sha256(dict(record.source_artifact.event_bundle))
            != result.event_bundle_sha256
        ):
            raise V5CampaignRunnerError(
                "durable physical result identity/artifact differs from its plan"
            )
    try:
        bind_v5_chain(
            first_artifact,
            tuple(record.trial_slice for record in records),
            campaign_fingerprint=campaign_fingerprint,
            release_identity_sha256=release_identity_sha256,
            role=role,
            chain_id=result.chain_id,
        )
    except Exception as exc:
        raise V5CampaignRunnerError(
            "durable chain lifecycle reconstruction failed"
        ) from exc
    terminal_proof = records[-1].boundary.boundary
    if (
        records[-1].boundary.mode is not BoundaryMode.HOME
        or terminal_proof.home_evidence_sha256 != result.home_evidence_sha256
    ):
        raise V5CampaignRunnerError("durable terminal Home evidence differs")

    result_path = Path(durable_result_path).resolve()
    request = V5LiveChainRequestV1(
        result.chain_id,
        plans,
        campaign_fingerprint,
        release_identity_sha256,
        role,
        str(result_path),
    )
    try:
        _activation_receipt, activation_rows = (
            V5RolloverActivationJournalV1.verify_receipt_with_rows(
            request,
            result.activation_receipt,
            )
        )
    except Exception as exc:
        raise V5CampaignRunnerError(
            "durable activation receipt is not cold-valid"
        ) from exc
    switch_receipts = tuple(first_artifact.event_bundle["switch_gate_receipts"])
    publication_receipts = result.activation_receipt.get(
        "publication_receipts"
    )
    if (
        not isinstance(publication_receipts, Sequence)
        or isinstance(publication_receipts, (str, bytes))
        or len(publication_receipts) != len(records) - 1
    ):
        raise V5CampaignRunnerError(
            "durable activation publication evidence is absent"
        )
    cold_publication_boundaries = cold_activation_publication_boundaries(
        first_artifact
    )
    if len(cold_publication_boundaries) != len(publication_receipts):
        raise V5CampaignRunnerError(
            "cold activation publication boundary count differs"
        )
    for generation, (record, successor, switch_receipt) in enumerate(
        zip(records[:-1], records[1:], switch_receipts, strict=True),
        start=1,
    ):
        acknowledgements = tuple(
            row
            for row in activation_rows
            if row.get("generation") == generation
            and row.get("state") == "COMMIT_ACK"
        )
        activations = tuple(
            row
            for row in activation_rows
            if row.get("generation") == generation
            and row.get("state") == "ACTIVATED"
        )
        preparations = tuple(
            row
            for row in activation_rows
            if row.get("generation") == generation
            and row.get("state") == "PREPARED"
        )
        intents = tuple(
            row
            for row in activation_rows
            if row.get("generation") == generation
            and row.get("state") == "COMMIT_INTENT"
        )
        proof = record.boundary.boundary
        if (
            len(preparations) != 1
            or len(intents) != 1
            or len(acknowledgements) != 1
            or len(activations) != 1
        ):
            raise V5CampaignRunnerError("durable activation seam count differs")
        preparation = preparations[0]["details"]
        intent = intents[0]["details"]
        acknowledgement = acknowledgements[0]["details"]
        activation = activations[0]["details"]
        publication = publication_receipts[generation - 1]
        cold_publication = cold_publication_boundaries[generation - 1]
        if not isinstance(publication, Mapping):
            raise V5CampaignRunnerError(
                "durable activation publication evidence is untyped"
            )
        preparation_sample = preparation.get("sample_index")
        intent_sample = intent.get("sample_index")
        if (
            type(preparation_sample) is not int
            or type(intent_sample) is not int
            or not record.trial_slice.sample_start_index
            <= preparation_sample
            < intent_sample
            < record.trial_slice.sample_end_index
        ):
            raise V5CampaignRunnerError(
                "durable prepare/COMMIT intent sample order differs"
            )
        preparation_row = first_artifact.rows[preparation_sample]
        intent_row = first_artifact.rows[intent_sample]
        preparation_overlay = preparation.get("controller_overlay")
        intent_wire = intent.get("wire_input")
        commit_seed = intent.get("commit_seed_qdot")
        expected_wire = {
            33: int(RolloverCommand.COMMIT),
            34: generation,
            35: 0,
            36: successor.candidate_identity.ordinal,
            37: int(successor.candidate_identity.attempt_kind),
            38: successor.candidate_identity.candidate_token,
            39: generation,
        }
        if (
            preparation_row.get("tp_state")
            != int(V5TPState.ROLLOVER_PREPARED)
            or float(preparation_row["monotonic_s"])
            - record.trial_slice.clock_start_s
            >= 60.0
            or not math.isclose(
                float(preparation.get("rtde_timestamp_s", math.nan)),
                float(preparation_row["rtde_timestamp_s"]),
                rel_tol=0.0,
                abs_tol=1e-12,
            )
            or not isinstance(preparation_overlay, Mapping)
            or canonical_sha256(dict(preparation_overlay))
            != canonical_sha256(
                {
                    29: generation,
                    30: generation - 1,
                    31: successor.candidate_identity.candidate_token,
                }
            )
            or intent_row.get("tp_state")
            not in {
                int(V5TPState.CLOSURE_TAIL),
                int(V5TPState.ROLLOVER_PREPARED),
            }
            or float(intent_row["monotonic_s"])
            - record.trial_slice.clock_start_s
            < 60.0
            or not math.isclose(
                float(intent.get("rtde_timestamp_s", math.nan)),
                float(intent_row["rtde_timestamp_s"]),
                rel_tol=0.0,
                abs_tol=1e-12,
            )
            or not isinstance(intent_wire, Mapping)
            or canonical_sha256(dict(intent_wire))
            != canonical_sha256(expected_wire)
            or not isinstance(commit_seed, Sequence)
            or isinstance(commit_seed, (str, bytes))
            or len(commit_seed) != 6
            or tuple(float(value) for value in commit_seed)
            != tuple(float(value) for value in intent_row["qdot"])
        ):
            raise V5CampaignRunnerError(
                "durable PREPARED/COMMIT intent evidence differs"
            )
        acknowledgement_overlay = acknowledgement.get("controller_overlay")
        switch_overlay = switch_receipt.get("controller_overlay")
        pending_samples = publication.get("pending_sample_indices")
        first_successor_sample = publication.get(
            "first_successor_sample_index"
        )
        if (
            not isinstance(pending_samples, Sequence)
            or isinstance(pending_samples, (str, bytes))
            or not pending_samples
            or any(type(value) is not int for value in pending_samples)
            or type(first_successor_sample) is not int
            or pending_samples[0] != proof.commit_sample_index
            or first_successor_sample != pending_samples[-1] + 1
            or first_successor_sample >= len(first_artifact.rows)
            or cold_publication.get("generation") != generation
            or tuple(cold_publication.get("pending_sample_indices", ()))
            != tuple(pending_samples)
            or cold_publication.get("first_successor_sample_index")
            != first_successor_sample
        ):
            raise V5CampaignRunnerError(
                "durable activation publication sample boundary differs"
            )
        pending_rows = tuple(
            first_artifact.rows[index] for index in pending_samples
        )
        first_successor_row = first_artifact.rows[first_successor_sample]
        if (
            record.boundary.mode is not BoundaryMode.CONTACT_ROLLOVER
            or proof.prepared_identity != successor.candidate_identity
            or proof.controller_commit_ack.active_identity
            != successor.candidate_identity
            or proof.generation != generation
            or proof.qdot_generation != generation
            or record.trial_slice.sample_end_index
            != successor.trial_slice.sample_start_index
            or record.trial_slice.clock_end_s
            != successor.trial_slice.clock_start_s
            or acknowledgement.get("sample_index") != proof.commit_sample_index
            or not math.isclose(
                float(acknowledgement.get("rtde_timestamp_s", math.nan)),
                proof.commit_rtde_timestamp_s,
                rel_tol=0.0,
                abs_tol=1e-12,
            )
            or acknowledgement.get("switch_gate_receipt_sha256")
            != proof.switch_gate_receipt_sha256
            or not isinstance(acknowledgement_overlay, Mapping)
            or not isinstance(switch_overlay, Mapping)
            or canonical_sha256(dict(acknowledgement_overlay))
            != canonical_sha256(dict(switch_overlay))
            or activation.get("sample_index") != proof.commit_sample_index
            or activation.get("candidate_token")
            != successor.candidate_identity.candidate_token
            or activation.get("candidate_ordinal")
            != successor.candidate_identity.ordinal
            or tuple(float(value) for value in activation.get("activation_qdot", ()))
            != tuple(float(value) for value in switch_receipt["commit_seed_qdot"])
            or activation.get("successor_path_packet_follows_fsync") is not True
            or publication.get("generation") != generation
            or publication.get("old_identity")
            != record.candidate_identity.as_dict()
            or publication.get("next_identity")
            != successor.candidate_identity.as_dict()
            or any(
                row.get("tp_state")
                != int(V5TPState.ROLLOVER_COMMITTED)
                or tuple(float(value) for value in row.get("qdot", ()))
                != tuple(
                    float(value)
                    for value in switch_receipt["commit_seed_qdot"]
                )
                for row in pending_rows
            )
            or first_successor_row.get("command_mode") != 2
            or first_successor_row.get("packet_sequence")
            != publication.get("first_successor_packet_sequence")
            or not math.isclose(
                float(first_successor_row.get("monotonic_s", math.nan)),
                float(
                    publication.get(
                        "first_successor_publish_monotonic_s", math.nan
                    )
                ),
                rel_tol=0.0,
                abs_tol=1e-12,
            )
            or float(
                publication.get("durability_settled_monotonic_s", math.inf)
            )
            >= float(
                publication.get(
                    "first_successor_publish_monotonic_s", -math.inf
                )
            )
        ):
            raise V5CampaignRunnerError(
                "durable rollover successor/activation evidence differs"
            )


class V5ExecutionJournalV1:
    """Append-only motion intent/result authority.

    A dispatch without a result is deliberately ambiguous and cannot be
    retried automatically.  A durable result can be reconstructed entirely
    from sealed artifacts and reconciled without issuing motion again.
    """

    def __init__(
        self,
        path: Path,
        *,
        campaign_fingerprint: str,
        release_identity_sha256: str,
        role: LedgerRole,
    ) -> None:
        self.path = Path(path).resolve()
        self.campaign_fingerprint = str(campaign_fingerprint)
        self.release_identity_sha256 = str(release_identity_sha256)
        self.role = role
        if self.path.is_symlink() or not isinstance(role, LedgerRole):
            raise V5CampaignRunnerError("execution journal path/role is invalid")
        for value in (self.campaign_fingerprint, self.release_identity_sha256):
            if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise V5CampaignRunnerError("execution journal identity is invalid")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            with self.path.open("x", encoding="utf-8") as stream:
                stream.write(_canonical(self._header()) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        self._head_sha256 = GENESIS_SHA256
        self._dispatches: dict[str, dict[str, Any]] = {}
        self._results: dict[str, V5DurableChainResultV1] = {}
        self._recoveries: dict[str, V5RecoverableFailureReceiptV1] = {}
        self.cold_verify()

    def _header(self) -> dict[str, Any]:
        return {
            "schema": EXECUTION_SCHEMA,
            "version": EXECUTION_VERSION,
            "record_type": "header",
            "campaign_fingerprint": self.campaign_fingerprint,
            "release_identity_sha256": self.release_identity_sha256,
            "role": self.role.value,
            "genesis_sha256": GENESIS_SHA256,
        }

    @property
    def head_sha256(self) -> str:
        return self._head_sha256

    @property
    def results(self) -> tuple[V5DurableChainResultV1, ...]:
        return tuple(self._results.values())

    @property
    def recoverable_failures(self) -> tuple[V5RecoverableFailureReceiptV1, ...]:
        return tuple(self._recoveries.values())

    @property
    def recoverable_chain_ids(self) -> tuple[str, ...]:
        return tuple(self._recoveries)

    def dispatched_plans(self, chain_id: str) -> tuple[V5TrialPlan, ...]:
        """Return one cold-verified dispatch plan tuple for a supervisor seam."""

        self.cold_verify()
        row = self._dispatches.get(chain_id)
        if row is None:
            raise V5CampaignRunnerError("execution dispatch is absent")
        return tuple(V5TrialPlan.from_mapping(item) for item in row["plans"])

    @property
    def ambiguous_chain_ids(self) -> tuple[str, ...]:
        return tuple(
            chain_id
            for chain_id in self._dispatches
            if chain_id not in self._results and chain_id not in self._recoveries
        )

    def result_path(self, chain_id: str) -> Path:
        if not isinstance(chain_id, str) or not chain_id or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
            for character in chain_id
        ):
            raise V5CampaignRunnerError("execution chain identity is invalid")
        directory = self.path.parent / f"{self.path.stem}.sealed-results"
        directory.mkdir(parents=True, exist_ok=True)
        result = (directory / f"{chain_id}.json").resolve()
        if result.parent != directory.resolve() or result.is_symlink():
            raise V5CampaignRunnerError("durable result locator escapes its namespace")
        return result

    def _append(self, record_type: str, payload: Mapping[str, Any]) -> None:
        row = {
            "schema": EXECUTION_SCHEMA,
            "version": EXECUTION_VERSION,
            "record_type": record_type,
            "campaign_fingerprint": self.campaign_fingerprint,
            "release_identity_sha256": self.release_identity_sha256,
            "role": self.role.value,
            "previous_sha256": self._head_sha256,
            **dict(payload),
        }
        row["row_sha256"] = canonical_sha256(row)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(_canonical(row) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.cold_verify()

    @staticmethod
    def _parse_result(row: Mapping[str, Any]) -> V5DurableChainResultV1:
        try:
            plans = tuple(V5TrialPlan.from_mapping(item) for item in row["plans"])
            records = tuple(
                physical_record_from_mapping(item) for item in row["records"]
            )
            result = V5DurableChainResultV1(
                str(row["chain_id"]),
                plans,
                records,
                dict(row["lifecycle_receipt"]),
                str(row["event_bundle_sha256"]),
                str(row["home_evidence_sha256"]),
                dict(row["activation_receipt"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise V5CampaignRunnerError("execution result row is invalid") from exc
        expected = _sha(
            {
                "chain_id": result.chain_id,
                "plans": [plan.as_dict() for plan in result.plans],
                "records": [record.as_dict() for record in result.records],
                "lifecycle_receipt": dict(result.lifecycle_receipt),
                "event_bundle_sha256": result.event_bundle_sha256,
                "home_evidence_sha256": result.home_evidence_sha256,
                "activation_receipt": dict(result.activation_receipt),
            }
        )
        if row.get("result_sha256") != expected:
            raise V5CampaignRunnerError("execution result content hash differs")
        return result

    def cold_verify(self) -> str:
        try:
            rows = [
                json.loads(line)
                for line in self.path.read_text(encoding="utf-8").splitlines()
            ]
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise V5CampaignRunnerError("execution journal is unreadable") from exc
        if not rows or rows[0] != self._header():
            raise V5CampaignRunnerError("execution journal header differs")
        previous = GENESIS_SHA256
        dispatches: dict[str, dict[str, Any]] = {}
        results: dict[str, V5DurableChainResultV1] = {}
        recoveries: dict[str, V5RecoverableFailureReceiptV1] = {}
        for row in rows[1:]:
            unsigned = {key: value for key, value in row.items() if key != "row_sha256"}
            if (
                row.get("schema") != EXECUTION_SCHEMA
                or row.get("version") != EXECUTION_VERSION
                or row.get("campaign_fingerprint") != self.campaign_fingerprint
                or row.get("release_identity_sha256") != self.release_identity_sha256
                or row.get("role") != self.role.value
                or row.get("previous_sha256") != previous
                or row.get("row_sha256") != canonical_sha256(unsigned)
            ):
                raise V5CampaignRunnerError("execution journal hash/namespace differs")
            chain_id = row.get("chain_id")
            if not isinstance(chain_id, str) or not chain_id:
                raise V5CampaignRunnerError("execution journal chain identity is invalid")
            if row.get("record_type") == "dispatch":
                if chain_id in dispatches:
                    raise V5CampaignRunnerError("execution dispatch is duplicated")
                plans = tuple(V5TrialPlan.from_mapping(item) for item in row.get("plans", ()))
                durable_result_path = row.get("durable_result_path")
                if (
                    not plans
                    or durable_result_path != str(self.result_path(chain_id))
                    or row.get("request_sha256")
                    != _sha(
                        {
                            "chain_id": chain_id,
                            "plans": [plan.as_dict() for plan in plans],
                            "durable_result_path": durable_result_path,
                        }
                    )
                ):
                    raise V5CampaignRunnerError("execution dispatch content differs")
                dispatches[chain_id] = dict(row)
            elif row.get("record_type") == "result":
                if chain_id not in dispatches or chain_id in results or chain_id in recoveries:
                    raise V5CampaignRunnerError("execution result lacks one dispatch")
                result = self._parse_result(row)
                dispatched_plans = tuple(
                    V5TrialPlan.from_mapping(item)
                    for item in dispatches[chain_id]["plans"]
                )
                if result.plans != dispatched_plans:
                    raise V5CampaignRunnerError("execution result plans differ from dispatch")
                validate_durable_chain_result(
                    result,
                    campaign_fingerprint=self.campaign_fingerprint,
                    release_identity_sha256=self.release_identity_sha256,
                    role=self.role,
                    durable_result_path=Path(
                        str(dispatches[chain_id]["durable_result_path"])
                    ),
                    expected_plans=dispatched_plans,
                )
                results[chain_id] = result
            elif row.get("record_type") == "recoverable_failure":
                if (
                    chain_id not in dispatches
                    or chain_id in results
                    or chain_id in recoveries
                ):
                    raise V5CampaignRunnerError(
                        "recoverable failure lacks one unclosed dispatch"
                    )
                try:
                    receipt = V5RecoverableFailureReceiptV1.from_mapping(
                        row["receipt"]
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise V5CampaignRunnerError(
                        "recoverable failure row is malformed"
                    ) from exc
                dispatched_plans = tuple(
                    V5TrialPlan.from_mapping(item)
                    for item in dispatches[chain_id]["plans"]
                )
                if (
                    receipt.chain_id != chain_id
                    or row.get("request_sha256")
                    != dispatches[chain_id]["request_sha256"]
                    or row.get("receipt_sha256")
                    != canonical_sha256(receipt.as_dict())
                    or len(dispatched_plans) != 1
                    or dispatched_plans[0].entry_mode != "HOME_ONLY_V1"
                    or not dispatched_plans[0].requires_home
                    or dispatched_plans[0].packable
                ):
                    raise V5CampaignRunnerError(
                        "recoverable failure identity/serial boundary differs"
                    )
                _verify_recoverable_failure_files(receipt)
                recoveries[chain_id] = receipt
            else:
                raise V5CampaignRunnerError("execution journal record type is unknown")
            previous = row["row_sha256"]
        self._dispatches = dispatches
        self._results = results
        self._recoveries = recoveries
        self._head_sha256 = previous
        return previous

    def append_dispatch(
        self,
        chain_id: str,
        plans: Sequence[V5TrialPlan],
        *,
        durable_result_path: Path | str | None = None,
    ) -> Path:
        plan_tuple = tuple(plans)
        if chain_id in self._dispatches:
            raise V5CampaignRunnerError("execution chain was already dispatched")
        result_path = (
            self.result_path(chain_id)
            if durable_result_path is None
            else Path(durable_result_path).resolve()
        )
        if result_path != self.result_path(chain_id):
            raise V5CampaignRunnerError("durable result locator is not deterministic")
        request = {
            "chain_id": chain_id,
            "plans": [plan.as_dict() for plan in plan_tuple],
            "durable_result_path": str(result_path),
        }
        self._append(
            "dispatch",
            {**request, "request_sha256": _sha(request)},
        )
        return result_path

    def append_recoverable_failure(
        self,
        chain_id: str,
        receipt: V5RecoverableFailureReceiptV1,
    ) -> V5RecoverableFailureReceiptV1:
        """Durably close one typed Home-only dispatch without sealing it."""

        self.cold_verify()
        if chain_id not in self._dispatches:
            raise V5CampaignRunnerError(
                "recoverable failure lacks durable dispatch"
            )
        if chain_id in self._results or chain_id in self._recoveries:
            raise V5CampaignRunnerError(
                "recoverable failure is duplicated or already sealed"
            )
        if not isinstance(receipt, V5RecoverableFailureReceiptV1):
            raise TypeError("recoverable failure receipt must be typed")
        plans = tuple(
            V5TrialPlan.from_mapping(item)
            for item in self._dispatches[chain_id]["plans"]
        )
        if (
            receipt.chain_id != chain_id
            or len(plans) != 1
            or plans[0].entry_mode != "HOME_ONLY_V1"
            or not plans[0].requires_home
            or plans[0].packable
        ):
            raise V5CampaignRunnerError(
                "recoverable failure requires one serial HOME-only dispatch"
            )
        _verify_recoverable_failure_files(receipt)
        request_sha256 = self._dispatches[chain_id]["request_sha256"]
        self._append(
            "recoverable_failure",
            {
                "chain_id": chain_id,
                "request_sha256": request_sha256,
                "receipt": receipt.as_dict(),
                "receipt_sha256": canonical_sha256(receipt.as_dict()),
            },
        )
        return self._recoveries[chain_id]

    def _load_durable_result(self, chain_id: str) -> V5DurableChainResultV1:
        dispatch = self._dispatches.get(chain_id)
        if dispatch is None:
            raise V5CampaignRunnerError("durable result lacks a dispatch")
        path = Path(str(dispatch["durable_result_path"]))
        if path != self.result_path(chain_id) or path.is_symlink() or not path.is_file():
            raise V5CampaignRunnerError("durable sealed result is absent")
        try:
            payload_bytes = path.read_bytes()
            value = json.loads(payload_bytes)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise V5CampaignRunnerError("durable sealed result is unreadable") from exc
        if not isinstance(value, Mapping):
            raise V5CampaignRunnerError("durable sealed result is not an object")
        unsigned = {key: item for key, item in value.items() if key != "result_sha256"}
        if (
            value.get("schema") != V5_SEALED_RESULT_SCHEMA
            or value.get("version") != V5_SEALED_RESULT_VERSION
            or value.get("record_type") != "sealed_chain_result"
            or value.get("campaign_fingerprint") != self.campaign_fingerprint
            or value.get("release_identity_sha256") != self.release_identity_sha256
            or value.get("role") != self.role.value
            or value.get("chain_id") != chain_id
            or value.get("durable_result_path") != str(path)
            or value.get("dispatch_request_sha256") != dispatch["request_sha256"]
            or value.get("result_sha256") != canonical_sha256(unsigned)
        ):
            raise V5CampaignRunnerError("durable sealed result identity/hash differs")
        try:
            plans = tuple(V5TrialPlan.from_mapping(item) for item in value["plans"])
            records = tuple(
                physical_record_from_mapping(item) for item in value["records"]
            )
            event_bundle = dict(value["event_bundle"])
            result = V5DurableChainResultV1(
                chain_id,
                plans,
                records,
                dict(value["lifecycle_receipt"]),
                str(value["event_bundle_sha256"]),
                str(value["home_evidence_sha256"]),
                dict(value["activation_receipt"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise V5CampaignRunnerError("durable sealed result payload is invalid") from exc
        dispatched_plans = tuple(
            V5TrialPlan.from_mapping(item) for item in dispatch["plans"]
        )
        if (
            result.plans != dispatched_plans
            or result.event_bundle_sha256 != canonical_sha256(event_bundle)
        ):
            raise V5CampaignRunnerError("durable sealed result content differs")
        activation_request = V5LiveChainRequestV1(
            chain_id,
            dispatched_plans,
            self.campaign_fingerprint,
            self.release_identity_sha256,
            self.role,
            str(path),
        )
        try:
            V5RolloverActivationJournalV1.verify_receipt(
                activation_request,
                result.activation_receipt,
            )
        except Exception as exc:
            raise V5CampaignRunnerError(
                "durable activation receipt is not cold-valid"
            ) from exc
        validate_durable_chain_result(
            result,
            campaign_fingerprint=self.campaign_fingerprint,
            release_identity_sha256=self.release_identity_sha256,
            role=self.role,
            durable_result_path=path,
            expected_plans=dispatched_plans,
        )
        return result

    def _append_durable_result(self, result: V5DurableChainResultV1) -> None:
        body = {
            "chain_id": result.chain_id,
            "plans": [plan.as_dict() for plan in result.plans],
            "records": [record.as_dict() for record in result.records],
            "lifecycle_receipt": dict(result.lifecycle_receipt),
            "event_bundle_sha256": result.event_bundle_sha256,
            "home_evidence_sha256": result.home_evidence_sha256,
            "activation_receipt": dict(result.activation_receipt),
        }
        self._append("result", {**body, "result_sha256": _sha(body)})

    def recover_sealed_results(self) -> int:
        """Attach owner-sealed results for dispatches interrupted before fan-in."""

        self.cold_verify()
        recovered = 0
        for chain_id in tuple(self.ambiguous_chain_ids):
            path = Path(str(self._dispatches[chain_id]["durable_result_path"]))
            if not path.exists():
                continue
            result = self._load_durable_result(chain_id)
            self._append_durable_result(result)
            recovered += 1
        return recovered

    def append_result(
        self,
        plans: Sequence[V5TrialPlan],
        result: V5SealedChainResultV1,
    ) -> V5DurableChainResultV1:
        plan_tuple = tuple(plans)
        if result.chain.chain_id not in self._dispatches:
            raise V5CampaignRunnerError("sealed result lacks durable dispatch")
        if result.chain.chain_id in self._results:
            raise V5CampaignRunnerError("sealed result is duplicated")
        durable = self._load_durable_result(result.chain.chain_id)
        durable_path = Path(str(self._dispatches[result.chain.chain_id]["durable_result_path"]))
        if (
            result.durable_result_path != str(durable_path)
            or result.durable_result_sha256
            != hashlib.sha256(durable_path.read_bytes()).hexdigest()
            or durable.plans != plan_tuple
            or tuple(record.record_sha256 for record in durable.records)
            != tuple(record.record_sha256 for record in result.records)
            or dict(durable.lifecycle_receipt) != dict(result.lifecycle_receipt)
            or durable.event_bundle_sha256
            != canonical_sha256(dict(result.event_bundle))
            or durable.home_evidence_sha256 != result.home_evidence_sha256
            or dict(durable.activation_receipt)
            != dict(result.activation_receipt or {})
        ):
            raise V5CampaignRunnerError("returned sealed result differs from durable bytes")
        self._append_durable_result(durable)
        return self._results[result.chain.chain_id]


class V5PhysicalCampaignRunnerV1:
    """Run and durably account one isolated PRIMARY or CORRECTION campaign."""

    def __init__(
        self,
        *,
        campaign: V5CampaignV2,
        owner: Any,
        physical_ledger: V5PhysicalAdmissionLedgerV2,
        optimizer_journal: V5OptimizerTellJournalV1,
        execution_journal: V5ExecutionJournalV1,
        max_chain_attempts: int = 5,
    ) -> None:
        if not isinstance(campaign, V5CampaignV2):
            raise TypeError("V5 runner requires V5CampaignV2")
        role = _ledger_role(campaign.identity.role)
        expected = (
            campaign.identity.campaign_fingerprint,
            campaign.identity.release_identity_sha256,
            role,
        )
        for component, name in (
            (physical_ledger, "physical ledger"),
            (optimizer_journal, "optimizer journal"),
            (execution_journal, "execution journal"),
        ):
            observed = (
                component.campaign_fingerprint,
                component.release_identity_sha256,
                component.role,
            )
            if observed != expected:
                raise V5CampaignRunnerError(f"{name} crosses campaign namespace")
        if not callable(getattr(owner, "execute_chain", None)):
            raise TypeError("V5 runner owner lacks execute_chain")
        self.campaign = campaign
        self.owner = owner
        self.physical_ledger = physical_ledger
        self.optimizer_journal = optimizer_journal
        self.execution_journal = execution_journal
        self.role = role
        if type(max_chain_attempts) is not int or not 1 <= max_chain_attempts <= 5:
            raise V5CampaignRunnerError("runner chain bound must be one to five")
        self.max_chain_attempts = max_chain_attempts

    def _campaign_plan(self, plan: V5TrialPlan) -> V5TrialPlan:
        matches = [item for item in self.campaign.plans if item.trial_id == plan.trial_id]
        if len(matches) != 1 or matches[0] != plan:
            raise V5CampaignRunnerError("execution result plan differs from campaign ledger")
        return matches[0]

    def _append_or_verify_record(
        self, record: FigureEightPhysicalRecordV2
    ) -> FigureEightPhysicalRecordV2:
        self.physical_ledger.fresh_process_verify()
        matches = [
            item
            for item in self.physical_ledger.records
            if item.trial_id == record.trial_id
        ]
        if not matches:
            self.physical_ledger.append_record(record)
            return record
        if len(matches) != 1 or matches[0].record_sha256 != record.record_sha256:
            raise V5CampaignRunnerError("physical record replay differs")
        return matches[0]

    def _commit_novel_tell(
        self,
        plan: V5TrialPlan,
        record: FigureEightPhysicalRecordV2,
    ) -> None:
        authorization = self.physical_ledger.prepare_tell(record)
        if authorization.state is TellState.PREPARED:
            authorization = self.physical_ledger.authorize_tell(record.trial_id)
        receipt = self.optimizer_journal.receipt_for(record.trial_id)
        if authorization.state is TellState.AUTHORIZED:
            receipt = self.optimizer_journal.tell_exact(
                authorization,
                record,
                candidate=plan.candidate.as_dict(),
            )
        elif authorization.state is TellState.AMBIGUOUS and receipt is None:
            raise V5CampaignRunnerError(
                "physical tell is ambiguous and has no durable optimizer receipt"
            )
        elif authorization.state in {TellState.OPTIMIZER_RECEIPT, TellState.COMMITTED}:
            if receipt is None:
                raise V5CampaignRunnerError(
                    "physical tell state lacks its optimizer journal receipt"
                )
        if receipt is None:
            raise V5CampaignRunnerError("optimizer receipt was not made durable")
        self.physical_ledger.reconcile_tell(record.trial_id, receipt)
        self.physical_ledger.commit_tell(record.trial_id, receipt)

    def _account_record(
        self,
        plan: V5TrialPlan,
        record: FigureEightPhysicalRecordV2,
    ) -> None:
        plan = self._campaign_plan(plan)
        if (
            record.trial_id != plan.trial_id
            or record.attempt_id != plan.attempt_id
            or record.campaign_fingerprint != self.campaign.identity.campaign_fingerprint
            or record.release_identity_sha256
            != self.campaign.identity.release_identity_sha256
            or record.role is not self.role
        ):
            raise V5CampaignRunnerError("physical result identity differs from plan")
        durable = self._append_or_verify_record(record)
        if plan.stage in _NOVEL_STAGES and durable.eligible:
            self._commit_novel_tell(plan, durable)
        self.campaign.record_physical_result(plan, durable, self.physical_ledger)

    def _preflight_chain(self, result: V5DurableChainResultV1) -> None:
        """Validate the entire sealed chain before the first durable side effect."""

        validate_durable_chain_result(
            result,
            campaign_fingerprint=self.campaign.identity.campaign_fingerprint,
            release_identity_sha256=self.campaign.identity.release_identity_sha256,
            role=self.role,
            durable_result_path=self.execution_journal.result_path(result.chain_id),
            expected_plans=result.plans,
        )
        if result.chain_id != result.records[0].chain_id:
            raise V5CampaignRunnerError("sealed chain identity differs")
        self.physical_ledger.fresh_process_verify()
        physical_by_trial = {
            record.trial_id: record for record in self.physical_ledger.records
        }
        outcome_by_trial = {
            outcome.trial_id: outcome for outcome in self.campaign.outcomes
        }
        first_artifact = result.records[0].source_artifact
        activation_request = V5LiveChainRequestV1(
            result.chain_id,
            result.plans,
            self.campaign.identity.campaign_fingerprint,
            self.campaign.identity.release_identity_sha256,
            self.role,
            str(self.execution_journal.result_path(result.chain_id)),
        )
        try:
            V5RolloverActivationJournalV1.verify_receipt(
                activation_request,
                result.activation_receipt,
            )
        except Exception as exc:
            raise V5CampaignRunnerError(
                "sealed rollover activation receipt is not cold-valid"
            ) from exc
        artifact_identity = (
            first_artifact.artifact_path,
            first_artifact.artifact_sha256,
            first_artifact.artifact_size,
        )
        if (
            dict(result.lifecycle_receipt) != dict(first_artifact.receipt)
            or result.event_bundle_sha256
            != canonical_sha256(dict(first_artifact.event_bundle))
        ):
            raise V5CampaignRunnerError("sealed chain artifact receipts differ")

        for index, (plan, record) in enumerate(
            zip(result.plans, result.records, strict=True)
        ):
            ledger_plan = self._campaign_plan(plan)
            if (
                ledger_plan != plan
                or record.chain_id != result.chain_id
                or record.trial_id != plan.trial_id
                or record.attempt_id != plan.attempt_id
                or record.candidate_identity.as_dict() != plan.candidate_identity
                or record.campaign_fingerprint
                != self.campaign.identity.campaign_fingerprint
                or record.release_identity_sha256
                != self.campaign.identity.release_identity_sha256
                or record.role is not self.role
                or (
                    record.source_artifact.artifact_path,
                    record.source_artifact.artifact_sha256,
                    record.source_artifact.artifact_size,
                )
                != artifact_identity
                or dict(record.source_artifact.receipt)
                != dict(first_artifact.receipt)
                or canonical_sha256(dict(record.source_artifact.event_bundle))
                != result.event_bundle_sha256
            ):
                raise V5CampaignRunnerError(
                    "sealed physical result identity/artifact differs from its plan"
                )
            existing_record = physical_by_trial.get(plan.trial_id)
            if (
                existing_record is not None
                and existing_record.record_sha256 != record.record_sha256
            ):
                raise V5CampaignRunnerError("physical record replay differs")
            existing_outcome = outcome_by_trial.get(plan.trial_id)
            if existing_outcome is not None and (
                existing_outcome.candidate_key != plan.candidate_key
                or existing_outcome.stage is not plan.stage
                or existing_outcome.record_sha256 != record.record_sha256
            ):
                raise V5CampaignRunnerError("campaign outcome replay differs")

            if index + 1 < len(result.records):
                proof = record.boundary.boundary
                successor = result.records[index + 1]
                if (
                    record.boundary.mode is not BoundaryMode.CONTACT_ROLLOVER
                    or proof.prepared_identity != successor.candidate_identity
                    or proof.controller_commit_ack.active_identity
                    != successor.candidate_identity
                    or proof.generation != index + 1
                    or proof.qdot_generation != proof.generation
                    or record.trial_slice.sample_end_index
                    != successor.trial_slice.sample_start_index
                    or record.trial_slice.clock_end_s
                    != successor.trial_slice.clock_start_s
                ):
                    raise V5CampaignRunnerError(
                        "sealed rollover successor/ACK identity differs"
                    )
            else:
                proof = record.boundary.boundary
                if (
                    record.boundary.mode is not BoundaryMode.HOME
                    or proof.home_evidence_sha256 != result.home_evidence_sha256
                ):
                    raise V5CampaignRunnerError("sealed terminal Home evidence differs")

    def _account_chain(self, result: V5DurableChainResultV1) -> int:
        self._preflight_chain(result)
        outcome_ids = {outcome.trial_id for outcome in self.campaign.outcomes}
        accounted = 0
        for index, (plan, record) in enumerate(
            zip(result.plans, result.records, strict=True)
        ):
            if plan.trial_id not in outcome_ids:
                self._account_record(plan, record)
                accounted += 1
                outcome_ids.add(plan.trial_id)
            if index + 1 < len(result.plans):
                successor = result.plans[index + 1]
                if successor.trial_id not in outcome_ids:
                    active = tuple(self.campaign._active_plans())
                    if not active:
                        activated = self.campaign.plan_next()
                        if activated != successor:
                            raise V5CampaignRunnerError(
                                "campaign activated a different prefetched successor"
                            )
                    elif active != (successor,):
                        raise V5CampaignRunnerError(
                            "campaign active plan differs from sealed successor"
                        )
        self.campaign.write_checkpoint()
        return accounted

    def _account_recoverable_failure(
        self,
        receipt: V5RecoverableFailureReceiptV1,
    ) -> bool:
        """Close the campaign plan while retaining no GP/tell evidence."""

        dispatch = self.execution_journal._dispatches.get(receipt.chain_id)
        if dispatch is None:
            raise V5CampaignRunnerError(
                "recoverable failure campaign plan lacks execution dispatch"
            )
        plans = tuple(
            V5TrialPlan.from_mapping(item) for item in dispatch["plans"]
        )
        if len(plans) != 1:
            raise V5CampaignRunnerError(
                "recoverable failure cannot close a rollover chain"
            )
        plan = plans[0]
        prior = {outcome.trial_id: outcome for outcome in self.campaign.outcomes}.get(
            plan.trial_id
        )
        if prior is not None:
            if not (
                prior.disposition is OutcomeV2.FAILURE
                and prior.dispatch_budget_counted
                and prior.failure_signature == receipt.failure_signature
            ):
                raise V5CampaignRunnerError(
                    "recoverable failure campaign outcome differs"
                )
            return False
        self.campaign.record_recoverable_failure(
            plan,
            signature=receipt.failure_signature,
        )
        self.campaign.write_checkpoint()
        return True

    def reconcile_durable_results(self) -> int:
        """Finish ledger fan-in for sealed results without issuing motion."""

        self.execution_journal.recover_sealed_results()
        self.execution_journal.cold_verify()
        recovered_failures = 0
        for receipt in self.execution_journal.recoverable_failures:
            recovered_failures += int(self._account_recoverable_failure(receipt))
        total = 0
        for result in self.execution_journal.results:
            if any(
                plan.trial_id not in {outcome.trial_id for outcome in self.campaign.outcomes}
                for plan in result.plans
            ):
                total += self._account_chain(result)
        return total + recovered_failures

    @staticmethod
    def _chain_id(plans: Sequence[V5TrialPlan]) -> str:
        plan_tuple = tuple(plans)
        return "v5-chain-" + _sha(
            {
                "trial_ids": [plan.trial_id for plan in plan_tuple],
                "attempt_ids": [plan.attempt_id for plan in plan_tuple],
            }
        )[:24]

    def run_next_chain(self) -> V5DurableChainResultV1 | None:
        """Execute at most one Home-terminated chain and account it exactly once."""

        self.reconcile_durable_results()
        ambiguous = self.execution_journal.ambiguous_chain_ids
        if ambiguous:
            raise V5AmbiguousPhysicalDispatch(
                "motion dispatch has no sealed durable result: " + ",".join(ambiguous)
            )
        active = tuple(self.campaign._active_plans())
        if len(active) > 1:
            raise V5CampaignRunnerError("campaign has more than one active plan")
        current = active[0] if active else self.campaign.plan_next()
        if current is None:
            return None
        plans = [current]
        if current.packable and not current.requires_home:
            plans = list(
                self.campaign.build_pending_chain(
                    max_attempts=self.max_chain_attempts
                )
            )
            if not plans or plans[0] != current or len(plans) > 5:
                raise V5CampaignRunnerError("campaign pending chain differs from active plan")
        chain_id = self._chain_id(plans)
        durable_result_path = self.execution_journal.append_dispatch(chain_id, plans)
        request = V5LiveChainRequestV1(
            chain_id,
            tuple(plans),
            self.campaign.identity.campaign_fingerprint,
            self.campaign.identity.release_identity_sha256,
            self.role,
            str(durable_result_path),
        )
        try:
            sealed = self.owner.execute_chain(request)
        except V5RecoverableOwnerFailure as exc:
            receipt = exc.receipt
            if receipt.chain_id != chain_id:
                raise V5CampaignRunnerError(
                    "recoverable owner failure chain differs from dispatch"
                ) from exc
            self.execution_journal.append_recoverable_failure(chain_id, receipt)
            self._account_recoverable_failure(receipt)
            # A typed continuation always stops this owner session.  The next
            # process must open a fresh resident after the durable Home proof.
            raise
        durable = self.execution_journal.append_result(plans, sealed)
        self._account_chain(durable)
        return durable

    def run_until_complete(self, *, max_chains: int | None = None) -> Any:
        """Run serial chains until campaign closeout or an optional test bound."""

        count = 0
        while max_chains is None or count < max_chains:
            result = self.run_next_chain()
            if result is None:
                return self.campaign.report()
            count += 1
        return None


__all__ = [
    "EXECUTION_SCHEMA",
    "EXECUTION_VERSION",
    "RECOVERABLE_CONTINUATION_MARKER",
    "V5AmbiguousPhysicalDispatch",
    "V5CampaignRunnerError",
    "V5DurableChainResultV1",
    "V5ExecutionJournalV1",
    "V5PhysicalCampaignRunnerV1",
    "V5RecoverableConnectionReset",
    "V5RecoverableFailureClass",
    "V5RecoverableFailureReceiptV1",
    "V5RecoverableOwnerFailure",
]
