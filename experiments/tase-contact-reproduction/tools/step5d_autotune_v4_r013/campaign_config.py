"""Strict typed loader for the fresh R013 budgeted-floor campaign config."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .campaign import BUDGETED_FLOOR_V1, CompletionPolicy
from .floor_coordinator import (
    FloorDiscoveryPolicyV1,
    HandoffABPlanV1,
    HandoffSelectionReceiptV1,
)
from .handoff import (
    HandoffPolicy,
    bind_handoff_policy_identity,
    validate_handoff_policy,
)
from .baseline_policy import (
    MotionAdmissionProfileV1,
    R013BaselineResidualPolicyV1,
    R013BaselineTransitionProfileV1,
)
from .feedforward import FeedforwardProfile
from .identity import (
    CampaignFingerprint,
    bind_r013_profile_identities,
    validate_campaign_fingerprint,
)


CONFIG_SCHEMA = "step5d.autotune-v4/r013-budgeted-floor-config-v1"
CONFIG_VERSION = 1
OFFLINE_PREPARED_STATUS = "offline_prepared_awaiting_live_prerequisites"
LIVE_READY_STATUS = "live_ready_awaiting_campaign_start"
OFFLINE_READINESS_BLOCKERS = (
    "live_handoff_ab_not_executed",
    "outward_boundary_runtime_primitive_not_installed",
    "context_correction_runtime_primitive_not_installed",
    "cycloid_200_novel_not_executed",
)
PROVISIONAL_HANDOFF_FINGERPRINT = "pending_handoff_selection_v1"
DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "step5d" / "r013_budgeted_floor_v1.json"
)
HOME_TARE_PROCEDURE_SCHEMA = "step5d.autotune-v4/r013-home-tare-procedure-v1"
HOME_TARE_PROCEDURE_VERSION = 1
HOME_TARE_BASELINE_CONTRACT = (
    "step5d.autotune-v4/r005-software-baseline-v1|"
    "source=fresh_kunwei_live_stream|zero_tare_config_write=false"
)
RUNTIME_INSTALLATION_RECEIPT_SCHEMA = (
    "step5d.autotune-v4/r013-runtime-installation-receipt-v1"
)
LIVE_READY_TRANSITION_SCHEMA = (
    "step5d.autotune-v4/r013-live-ready-transition-receipt-v1"
)
RUNTIME_INSTALLATION_RECEIPT_VERSION = 1
LIVE_READY_TRANSITION_VERSION = 1

# This is the bounded executable host surface that turns a prepared campaign
# fingerprint into physical R013 commands and sealed evidence.  The controller
# triplet alone cannot identify Python behavior: live_002 proved that changing
# the transition implementation left the old source identity untouched.
R013_HOST_SOURCE_PATHS = (
    # Canonical controller/runtime contracts and the V4 stage owner.  These
    # files are part of the executable behavior surface even when the mature
    # R006 writer remains the physical packet owner.
    "tools/step5c_strict_rnn.py",
    "tools/step5d_paper_outer_loop.py",
    "tools/contact_semantics.py",
    "tools/run_v4_stage_live.py",
    "tools/run_v4_stage_recovery_supervisor.py",
    "tools/run_v4_two_stage_campaign.py",
    "tools/run_v4_fixed_confirmation.py",
    "tools/step5d_autotune_v4_r013/v4_two_stage_campaign.py",
    "tools/step5d_autotune_v4_r013/v4_stage_live_adapter.py",
    "tools/run_step5d_autotune_v4_r013_demo.py",
    "tools/step5d_autotune_v4_r004/baseline_runtime.py",
    "tools/step5d_autotune_v4_r004/calibrated_runtime.py",
    "tools/step5d_autotune_v4_r004/contracts.py",
    "tools/step5d_autotune_v4_r004/arm_transition.py",
    "tools/step5d_autotune_v4_r004/evidence.py",
    "tools/step5d_autotune_v4_r004/home.py",
    "tools/step5d_autotune_v4_r004/home_profile.py",
    "tools/step5d_autotune_v4_r004/policies.py",
    "tools/step5d_autotune_v4_r004/qualification.py",
    "tools/step5d_autotune_v4_r004/runtime.py",
    "tools/step5d_autotune_v4_r004/session.py",
    "tools/step5d_autotune_v4_r004/timing.py",
    "tools/step5d_autotune_v4_r004/transport.py",
    "tools/step5d_autotune_v4_r004/wire.py",
    "tools/step5d_autotune_v4_live_writer.py",
    "tools/step5d_autotune_v4_r004_live_writer.py",
    "tools/step5d_autotune_v4_r005/live_adapter.py",
    "tools/step5d_autotune_v4_r005/observations.py",
    "tools/step5d_autotune_v4_r005/runtime.py",
    "tools/step5d_autotune_v4_r006/contracts.py",
    "tools/step5d_autotune_v4_r006/live_adapter.py",
    "tools/step5d_autotune_v4_r006/motion_profile.py",
    "tools/step5d_autotune_v4_r006/parent.py",
    "tools/step5d_autotune_v4_r006/runtime.py",
    "tools/step5d_autotune_v4_r006/thresholds.py",
    "tools/step5d_autotune_v4_r008/live_adapter.py",
    "tools/step5d_autotune_v4_r008/bounded_resume_ledger.py",
    "tools/step5d_autotune_v4_r008/host_hard_tube.py",
    "tools/step5d_autotune_v4_r008/state20_search_trace.py",
    "tools/step5d_autotune_v4_r008/state25_path_trace.py",
    "tools/step5d_autotune_v4_r008/timing.py",
    "tools/step5d_autotune_v4_r012/compat_identity.py",
    "tools/step5d_autotune_v4_r012/safety_filter.py",
    "tools/step5d_autotune_v4_r012/censor.py",
    "tools/step5d_autotune_v4_r012/live_host.py",
    "tools/step5d_autotune_v4_r012/path_cbf_live.py",
    "tools/step5d_autotune_v4_r012/register_transport.py",
    "tools/step5d_autotune_v3/dashboard.py",
    "tools/step5d_autotune_v3/rtde_client.py",
    "tools/step5d_bridge_authority.py",
    "tools/step5d_eoat_profiles.py",
    "tools/step5d_remote_startup.py",
    "tools/step6_figure8_autotune_v1/__init__.py",
    "tools/step6_figure8_autotune_v1/live_composition.py",
    "tools/upload_ur_tp_package.py",
    "tools/step5d_autotune_v4_r013/contact_search_strategy.py",
    "tools/step5d_autotune_v4_r013/contact_transient.py",
    "tools/step5d_autotune_v4_r013/v4_stage_censor.py",
    "tools/step5d_autotune_v4_r013/baseline_policy.py",
    "tools/step5d_autotune_v4_r013/bounded_bo.py",
    "tools/step5d_autotune_v4_r013/campaign.py",
    "tools/step5d_autotune_v4_r013/campaign_config.py",
    # Direct campaign/owner dependencies are part of the semantic identity;
    # omitting them would let a proposal, ledger, or runtime-law change reuse
    # the same source hash.
    "tools/step5d_autotune_v4_r013/controller_triplet.py",
    "tools/step5d_autotune_v4_r013/domain.py",
    "tools/step5d_autotune_v4_r013/floor_coordinator.py",
    "tools/step5d_autotune_v4_r013/feedforward.py",
    "tools/step5d_autotune_v4_r013/gp.py",
    "tools/step5d_autotune_v4_r013/handoff.py",
    "tools/step5d_autotune_v4_r013/identity.py",
    "tools/step5d_autotune_v4_r013/ledger.py",
    "tools/step5d_autotune_v4_r013/lifecycle_trace.py",
    "tools/step5d_autotune_v4_r013/live_owner.py",
    "tools/step5d_autotune_v4_r013/recovery.py",
    "tools/step5d_autotune_v4_r013/runtime_strategy.py",
    "tools/step5d_autotune_v4_r013/timing_scheduler.py",
    "tools/step5d_autotune_v4_r013/live_runtime.py",
    "tools/step5d_autotune_v4_r013/prepare_live.py",
    "tools/step5d_autotune_v4_r013/path_context.py",
    "tools/step5d_autotune_v4_r013/state21_baseline_trace.py",
    "tools/ur10e_parallel.py",
    "tools/run_step5d_autotune_v4_r013_live.py",
    # V5 live/campaign paths are included now so a later V5 identity cannot
    # silently reuse a runtime closure that was changed after V4.
    "tools/run_step6_figure8_autotune_v1_live.py",
    "tools/run_step6_figure8_no_contact_canary_v1_live.py",
    "tools/step6_figure8_autotune_v1/campaign_runner.py",
    "tools/step6_figure8_autotune_v1/optimizer_bridge.py",
    "tools/step6_figure8_autotune_v1/optimizer_worker.py",
    "tools/step6_figure8_autotune_v1/v5_campaign.py",
    "tools/step6_figure8_autotune_v1/v5_campaign_runner.py",
    "tools/step6_figure8_autotune_v1/v5_capability_acceptance.py",
    "tools/step6_figure8_autotune_v1/v5_composition_contract.py",
    "tools/step6_figure8_autotune_v1/v5_live_owner.py",
    "tools/step6_figure8_autotune_v1/v5_live_runtime.py",
    "tools/step6_figure8_autotune_v1/v5_lifecycle_ledger.py",
    "tools/step6_figure8_autotune_v1/v5_optimizer_journal.py",
    "tools/step6_figure8_autotune_v1/v5_production_bundle.py",
    "tools/step6_figure8_autotune_v1/v5_register_transport.py",
    "tools/step6_figure8_autotune_v1/v5_resident_protocol.py",
    "tools/step6_figure8_autotune_v1/v5_rollover.py",
    "tools/step6_figure8_autotune_v1/v5_camera_observer.py",
    "tools/step6_figure8_autotune_v1/v5_kernel_selection.py",
    "tools/step6_figure8_autotune_v1/v5_ros2_observation_mirror.py",
    "tools/step6_figure8_autotune_v1/v5_sidecar_bundle.py",
    "tools/step6_figure8_autotune_v1/physical_candidate.py",
    "tools/step6_figure8_autotune_v1/physical_censor.py",
    "tools/step6_figure8_autotune_v1/physical_ledger.py",
)


class R013CampaignConfigError(ValueError):
    """The fresh budgeted-floor config is malformed or not bound."""


MANUAL_CANARY_PREPARATION_SCHEMA = (
    "step5d.autotune-v4/r013-manual-canary-preparation-v1"
)
MANUAL_CANARY_PREPARATION_VERSION = 1
MANUAL_CANARY_ROLE = "manual_canary"


@dataclass(frozen=True)
class R013ManualCanaryPreparationProfileV1:
    """Non-formal preparation role for one fixed-candidate demo limb."""

    feedforward_profile: FeedforwardProfile
    schema: str = MANUAL_CANARY_PREPARATION_SCHEMA
    version: int = MANUAL_CANARY_PREPARATION_VERSION
    role: str = MANUAL_CANARY_ROLE
    formal_campaign_tell_exact: bool = False
    launch_ready: bool = False

    def __post_init__(self) -> None:
        if (
            self.schema != MANUAL_CANARY_PREPARATION_SCHEMA
            or type(self.version) is not int
            or self.version != MANUAL_CANARY_PREPARATION_VERSION
            or self.role != MANUAL_CANARY_ROLE
            or self.formal_campaign_tell_exact is not False
            or self.launch_ready is not False
            or not isinstance(self.feedforward_profile, FeedforwardProfile)
        ):
            raise R013CampaignConfigError(
                "R013 manual canary preparation profile differs"
            )

    @classmethod
    def from_value(cls, feedforward_mode: Any = None) -> "R013ManualCanaryPreparationProfileV1":
        return cls(feedforward_profile=FeedforwardProfile.from_value(feedforward_mode))

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "role": self.role,
            "feedforward_profile": self.feedforward_profile.as_dict(),
            "formal_campaign_tell_exact": self.formal_campaign_tell_exact,
            "launch_ready": self.launch_ready,
        }


def _bind_materialized_r013_profiles(
    fingerprint: CampaignFingerprint,
    feedforward_profile: FeedforwardProfile,
) -> CampaignFingerprint:
    if not isinstance(feedforward_profile, FeedforwardProfile):
        raise R013CampaignConfigError("R013 feedforward profile is not typed")
    return bind_r013_profile_identities(
        fingerprint,
        feedforward_profile=feedforward_profile,
        motion_admission_profile=MotionAdmissionProfileV1.from_feedforward(
            feedforward_profile
        ),
        baseline_transition_profile=R013BaselineTransitionProfileV1(),
        baseline_residual_policy=R013BaselineResidualPolicyV1(),
    )


def _sha256_hex(value: Any, *, role: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise R013CampaignConfigError(f"R013 {role} must be a lowercase SHA-256 identity")
    return value


def r013_host_source_closure(
    *, source_root: Path | None = None
) -> dict[str, Any]:
    """Cold-hash the explicit R013 host behavior surface."""

    root = (
        Path(__file__).resolve().parents[2]
        if source_root is None
        else Path(source_root).resolve()
    )
    files: dict[str, str] = {}
    for relative in R013_HOST_SOURCE_PATHS:
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise R013CampaignConfigError(
                "R013 host source closure escapes its source root"
            ) from exc
        if not candidate.is_file():
            raise R013CampaignConfigError(
                f"R013 host source closure file is missing: {relative}"
            )
        files[relative] = hashlib.sha256(candidate.read_bytes()).hexdigest()
    basis = {
        "schema": "step5d.autotune-v4/r013-host-source-closure-v1",
        "version": 1,
        "files": files,
    }
    digest = hashlib.sha256(
        json.dumps(
            basis,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return {**basis, "sha256": digest}


def controller_source_identity_sha256(
    triplet_sha256: Mapping[str, Any],
    *,
    source_root: Path | None = None,
) -> str:
    """Bind the verified controller triplet and current host source closure."""

    if not isinstance(triplet_sha256, Mapping) or set(triplet_sha256) != {
        "script", "txt", "urp"
    }:
        raise R013CampaignConfigError("R013 controller source triplet identity is incomplete")
    triplet = {
        role: _sha256_hex(triplet_sha256[role], role=f"controller {role} source")
        for role in ("script", "txt", "urp")
    }
    payload = json.dumps(
        {
            "schema": "step5d.autotune-v4/r013-runtime-source-identity-v2",
            "version": 2,
            "triplet_sha256": triplet,
            "host_source_closure": r013_host_source_closure(
                source_root=source_root
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def home_tare_procedure_identity(script1_source_sha256: str) -> str:
    """Return a stable identity for the fixed Home/read-only-tare procedure."""

    return (
        f"{HOME_TARE_PROCEDURE_SCHEMA}|version={HOME_TARE_PROCEDURE_VERSION}|"
        f"script1_sha256={_sha256_hex(script1_source_sha256, role='Script1 source')}|"
        f"baseline_contract={HOME_TARE_BASELINE_CONTRACT}"
    )


def materialize_campaign_fingerprint(
    config: "R013BudgetedFloorConfig",
    *,
    runtime_strategy_sha256_value: str,
    controller_triplet_sha256: Mapping[str, Any],
    eoat_identity_sha256: str,
    script1_source_sha256: str,
    feedforward_profile: FeedforwardProfile | None = None,
) -> CampaignFingerprint:
    """Materialize live-derived limbs only after typed handoff readiness exists."""

    if not isinstance(config, R013BudgetedFloorConfig):
        raise R013CampaignConfigError("R013 budgeted-floor config is not typed")
    config.require_launch_ready()
    materialized = _materialize_campaign_fingerprint_unchecked(
        config,
        runtime_strategy_sha256_value=runtime_strategy_sha256_value,
        controller_triplet_sha256=controller_triplet_sha256,
        eoat_identity_sha256=eoat_identity_sha256,
        script1_source_sha256=script1_source_sha256,
    )
    if materialized != config.campaign_fingerprint:
        raise R013CampaignConfigError(
            "R013 live-ready campaign fingerprint differs from supplied identities"
        )
    if feedforward_profile is None:
        return materialized
    return _bind_materialized_r013_profiles(materialized, feedforward_profile)


def _materialize_campaign_fingerprint_unchecked(
    config: "R013BudgetedFloorConfig",
    *,
    runtime_strategy_sha256_value: str,
    controller_triplet_sha256: Mapping[str, Any],
    eoat_identity_sha256: str,
    script1_source_sha256: str,
) -> CampaignFingerprint:
    plan = config.handoff_plan
    selected = config.selected_handoff_policy
    receipt = config.handoff_selection_receipt
    if selected is None or receipt is None:
        raise R013CampaignConfigError("R013 completed handoff selection receipt is missing")
    expected_receipt = plan.selection_receipt()
    if expected_receipt != receipt:
        raise R013CampaignConfigError("R013 handoff selection receipt is not bound to its plan")
    handoff_identity = bind_handoff_policy_identity(
        selected,
        receipt.receipt_sha256,
    )
    materialized = replace(
        config.campaign_fingerprint,
        handoff_policy=handoff_identity,
        correction_runtime_strategy_identity=_sha256_hex(
            runtime_strategy_sha256_value,
            role="runtime strategy",
        ),
        source_identity=controller_source_identity_sha256(controller_triplet_sha256),
        eoat_identity=_sha256_hex(eoat_identity_sha256, role="EOAT"),
        home_tare_identity=home_tare_procedure_identity(script1_source_sha256),
    )
    return materialized


@dataclass(frozen=True)
class R013RuntimeInstallationReceiptV1:
    """Hash-bound, current installation evidence for one R013 runtime primitive."""

    primitive: str
    source_identity: str
    runtime_identity: str
    campaign_fingerprint_sha256: str
    installation_status: str = "installed_current"
    schema: str = RUNTIME_INSTALLATION_RECEIPT_SCHEMA
    version: int = RUNTIME_INSTALLATION_RECEIPT_VERSION
    receipt_sha256: str = ""

    def __post_init__(self) -> None:
        if (
            self.schema != RUNTIME_INSTALLATION_RECEIPT_SCHEMA
            or type(self.version) is not int
            or self.version != RUNTIME_INSTALLATION_RECEIPT_VERSION
        ):
            raise R013CampaignConfigError(
                "R013 runtime installation receipt schema/version differs"
            )
        if self.primitive not in {"outward_boundary", "six_context_correction"}:
            raise R013CampaignConfigError("R013 runtime installation primitive differs")
        if self.installation_status != "installed_current":
            raise R013CampaignConfigError(
                "R013 runtime installation receipt is missing current installation status"
            )
        for value, role in (
            (self.source_identity, "runtime source"),
            (self.runtime_identity, "runtime"),
            (self.campaign_fingerprint_sha256, "campaign fingerprint"),
        ):
            _sha256_hex(value, role=role)
        expected = _token(self._payload())
        if self.receipt_sha256 and self.receipt_sha256 != expected:
            raise R013CampaignConfigError("R013 runtime installation receipt hash differs")
        object.__setattr__(self, "receipt_sha256", expected)

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "primitive": self.primitive,
            "source_identity": self.source_identity,
            "runtime_identity": self.runtime_identity,
            "campaign_fingerprint_sha256": self.campaign_fingerprint_sha256,
            "installation_status": self.installation_status,
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "receipt_sha256": self.receipt_sha256}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "R013RuntimeInstallationReceiptV1":
        required = {
            "schema", "version", "primitive", "source_identity", "runtime_identity",
            "campaign_fingerprint_sha256", "installation_status", "receipt_sha256",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise R013CampaignConfigError(
                "R013 runtime installation receipt fields differ"
            )
        return cls(**dict(value))


def _token(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        dict(value), sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class R013LiveReadyTransitionReceiptV1:
    """The append-only log entry for the offline-to-live-ready transition."""

    source_config_fingerprint_sha256: str
    target_campaign_fingerprint_sha256: str
    handoff_selection_receipt_sha256: str
    outward_boundary_receipt_sha256: str
    six_context_correction_receipt_sha256: str
    transition: str = "offline_to_live_ready"
    schema: str = LIVE_READY_TRANSITION_SCHEMA
    version: int = LIVE_READY_TRANSITION_VERSION
    receipt_sha256: str = ""

    def __post_init__(self) -> None:
        if (
            self.schema != LIVE_READY_TRANSITION_SCHEMA
            or type(self.version) is not int
            or self.version != LIVE_READY_TRANSITION_VERSION
        ):
            raise R013CampaignConfigError(
                "R013 live-ready transition schema/version differs"
            )
        if self.transition != "offline_to_live_ready":
            raise R013CampaignConfigError("R013 live-ready transition kind differs")
        for value, role in (
            (self.source_config_fingerprint_sha256, "source config fingerprint"),
            (self.target_campaign_fingerprint_sha256, "target campaign fingerprint"),
            (self.handoff_selection_receipt_sha256, "handoff selection receipt"),
            (self.outward_boundary_receipt_sha256, "outward-boundary receipt"),
            (self.six_context_correction_receipt_sha256, "six-context receipt"),
        ):
            _sha256_hex(value, role=role)
        expected = _token(self._payload())
        if self.receipt_sha256 and self.receipt_sha256 != expected:
            raise R013CampaignConfigError("R013 live-ready transition receipt hash differs")
        object.__setattr__(self, "receipt_sha256", expected)

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "source_config_fingerprint_sha256": self.source_config_fingerprint_sha256,
            "target_campaign_fingerprint_sha256": self.target_campaign_fingerprint_sha256,
            "handoff_selection_receipt_sha256": self.handoff_selection_receipt_sha256,
            "outward_boundary_receipt_sha256": self.outward_boundary_receipt_sha256,
            "six_context_correction_receipt_sha256": self.six_context_correction_receipt_sha256,
            "transition": self.transition,
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "receipt_sha256": self.receipt_sha256}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "R013LiveReadyTransitionReceiptV1":
        required = {
            "schema", "version", "source_config_fingerprint_sha256",
            "target_campaign_fingerprint_sha256", "handoff_selection_receipt_sha256",
            "outward_boundary_receipt_sha256", "six_context_correction_receipt_sha256",
            "transition", "receipt_sha256",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise R013CampaignConfigError(
                "R013 live-ready transition receipt fields differ"
            )
        return cls(**dict(value))


@dataclass(frozen=True)
class R013BudgetedFloorConfig:
    schema: str
    version: int
    status: str
    launch_ready: bool
    blockers: tuple[str, ...]
    completion_policy: CompletionPolicy
    handoff_policy: HandoffPolicy | None
    selected_handoff_policy: HandoffPolicy | None
    handoff_plan: HandoffABPlanV1
    handoff_selection_receipt: HandoffSelectionReceiptV1 | None
    floor_discovery_policy: FloorDiscoveryPolicyV1
    campaign_fingerprint: CampaignFingerprint
    outward_boundary_runtime_receipt: R013RuntimeInstallationReceiptV1 | None = None
    six_context_correction_runtime_receipt: R013RuntimeInstallationReceiptV1 | None = None
    live_ready_transition_receipt: R013LiveReadyTransitionReceiptV1 | None = None
    transition_source_config_fingerprint_sha256: str | None = None

    def __post_init__(self) -> None:
        if (
            self.schema != CONFIG_SCHEMA
            or type(self.version) is not int
            or self.version != CONFIG_VERSION
        ):
            raise R013CampaignConfigError("R013 budgeted-floor config schema/version differs")
        if type(self.launch_ready) is not bool:
            raise R013CampaignConfigError("R013 budgeted-floor launch_ready must be bool")
        blockers = tuple(self.blockers)
        if any(type(blocker) is not str or not blocker for blocker in blockers):
            raise R013CampaignConfigError("R013 budgeted-floor readiness blockers are invalid")
        if len(set(blockers)) != len(blockers):
            raise R013CampaignConfigError("R013 budgeted-floor readiness blockers are duplicated")
        if self.launch_ready:
            if self.status != LIVE_READY_STATUS or blockers != ():
                raise R013CampaignConfigError(
                    "R013 budgeted-floor config is offline-only; live-ready status/blockers differ"
                )
        else:
            if self.status != OFFLINE_PREPARED_STATUS or blockers != OFFLINE_READINESS_BLOCKERS:
                raise R013CampaignConfigError(
                    "R013 offline template blockers differ from the frozen prerequisite list"
                )
        object.__setattr__(self, "blockers", blockers)
        if self.completion_policy.policy != BUDGETED_FLOOR_V1:
            raise R013CampaignConfigError("R013 budgeted-floor config requires budgeted_floor_v1")
        if isinstance(self.handoff_plan, Mapping):
            object.__setattr__(
                self, "handoff_plan", HandoffABPlanV1.from_mapping(self.handoff_plan)
            )
        if not isinstance(self.handoff_plan, HandoffABPlanV1):
            raise R013CampaignConfigError("R013 handoff A/B plan is not typed")
        for field_name in ("handoff_policy", "selected_handoff_policy"):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, HandoffPolicy):
                object.__setattr__(self, field_name, validate_handoff_policy(value))
        if self.handoff_policy != self.selected_handoff_policy:
            raise R013CampaignConfigError(
                "R013 handoff_policy and selected_handoff_policy differ"
            )
        receipt = self.handoff_selection_receipt
        if isinstance(receipt, Mapping):
            receipt = HandoffSelectionReceiptV1.from_mapping(receipt)
            object.__setattr__(self, "handoff_selection_receipt", receipt)
        expected_receipt = self.handoff_plan.selection_receipt()
        if expected_receipt != receipt:
            raise R013CampaignConfigError("R013 handoff selection receipt differs from its plan")
        if self.selected_handoff_policy is None:
            if self.handoff_plan.selected_policy is not None:
                raise R013CampaignConfigError(
                    "R013 handoff plan has a selected policy without a selected field"
                )
            expected_handoff_identity = PROVISIONAL_HANDOFF_FINGERPRINT
        else:
            if not self.handoff_plan.complete:
                raise R013CampaignConfigError(
                    "R013 selected handoff policy lacks complete A/B evidence"
                )
            if self.handoff_plan.materialize_handoff_policy() != self.selected_handoff_policy.policy:
                raise R013CampaignConfigError("R013 selected handoff policy differs from its plan")
            expected_handoff_identity = bind_handoff_policy_identity(
                self.selected_handoff_policy,
                receipt.receipt_sha256,
            )
        if self.campaign_fingerprint.handoff_policy != expected_handoff_identity:
            raise R013CampaignConfigError(
                "R013 budgeted-floor config handoff/fingerprint binding differs"
            )
        if not self.launch_ready:
            if any(
                value is not None
                for value in (
                    self.outward_boundary_runtime_receipt,
                    self.six_context_correction_runtime_receipt,
                    self.live_ready_transition_receipt,
                    self.transition_source_config_fingerprint_sha256,
                )
            ):
                raise R013CampaignConfigError(
                    "R013 offline config cannot contain live-ready receipts"
                )
            return
        if self.selected_handoff_policy is None or self.handoff_selection_receipt is None:
            raise R013CampaignConfigError(
                "R013 live-ready config requires completed handoff evidence"
            )
        if not self.handoff_plan.complete:
            raise R013CampaignConfigError("R013 live-ready handoff plan is incomplete")
        if not isinstance(
            self.outward_boundary_runtime_receipt, R013RuntimeInstallationReceiptV1
        ) or self.outward_boundary_runtime_receipt.primitive != "outward_boundary":
            raise R013CampaignConfigError(
                "R013 live-ready outward-boundary receipt is missing or invalid"
            )
        if not isinstance(
            self.six_context_correction_runtime_receipt,
            R013RuntimeInstallationReceiptV1,
        ) or self.six_context_correction_runtime_receipt.primitive != "six_context_correction":
            raise R013CampaignConfigError(
                "R013 live-ready six-context correction receipt is missing or invalid"
            )
        for receipt in (
            self.outward_boundary_runtime_receipt,
            self.six_context_correction_runtime_receipt,
        ):
            if receipt.campaign_fingerprint_sha256 != self.campaign_fingerprint.sha256:
                raise R013CampaignConfigError(
                    "R013 live-ready runtime receipt fingerprint differs"
                )
            if receipt.source_identity != self.campaign_fingerprint.source_identity:
                raise R013CampaignConfigError(
                    "R013 live-ready runtime source identity differs"
                )
        if (
            self.outward_boundary_runtime_receipt.runtime_identity
            != self.campaign_fingerprint.source_identity
            or self.six_context_correction_runtime_receipt.runtime_identity
            != self.campaign_fingerprint.correction_runtime_strategy_identity
        ):
            raise R013CampaignConfigError("R013 live-ready runtime identity differs")
        _sha256_hex(
            self.transition_source_config_fingerprint_sha256,
            role="transition source config fingerprint",
        )
        if not isinstance(
            self.live_ready_transition_receipt, R013LiveReadyTransitionReceiptV1
        ):
            raise R013CampaignConfigError("R013 live-ready transition receipt is missing")
        transition = self.live_ready_transition_receipt
        if (
            transition.source_config_fingerprint_sha256
            != self.transition_source_config_fingerprint_sha256
            or transition.target_campaign_fingerprint_sha256
            != self.campaign_fingerprint.sha256
            or transition.handoff_selection_receipt_sha256
            != self.handoff_selection_receipt.receipt_sha256
            or transition.outward_boundary_receipt_sha256
            != self.outward_boundary_runtime_receipt.receipt_sha256
            or transition.six_context_correction_receipt_sha256
            != self.six_context_correction_runtime_receipt.receipt_sha256
        ):
            raise R013CampaignConfigError("R013 live-ready transition receipt binding differs")

    def as_dict(self) -> dict[str, Any]:
        value = {
            "schema": self.schema,
            "version": self.version,
            "status": self.status,
            "launch_ready": self.launch_ready,
            "blockers": list(self.blockers),
            "completion_policy": self.completion_policy.as_dict(),
            "handoff_policy": (
                None if self.handoff_policy is None else self.handoff_policy.as_dict()
            ),
            "selected_handoff_policy": (
                None
                if self.selected_handoff_policy is None
                else self.selected_handoff_policy.as_dict()
            ),
            "handoff_plan": self.handoff_plan.as_dict(),
            "handoff_selection_receipt": (
                None
                if self.handoff_selection_receipt is None
                else self.handoff_selection_receipt.as_dict()
            ),
            "floor_discovery_policy": self.floor_discovery_policy.as_dict(),
            "campaign_fingerprint": self.campaign_fingerprint.as_dict(),
        }
        if self.launch_ready:
            value.update(
                {
                    "outward_boundary_runtime_receipt": (
                        self.outward_boundary_runtime_receipt.as_dict()
                    ),
                    "six_context_correction_runtime_receipt": (
                        self.six_context_correction_runtime_receipt.as_dict()
                    ),
                    "live_ready_transition_receipt": (
                        self.live_ready_transition_receipt.as_dict()
                    ),
                    "transition_source_config_fingerprint_sha256": (
                        self.transition_source_config_fingerprint_sha256
                    ),
                }
            )
        return value

    @property
    def campaign_fingerprint_sha256(self) -> str:
        return self.campaign_fingerprint.sha256

    def require_launch_ready(self) -> None:
        if not self.launch_ready:
            raise R013CampaignConfigError(
                "R013 live preparation is blocked: launch_ready=false; "
                f"blockers={','.join(self.blockers)}"
            )
        if self.selected_handoff_policy is None or self.handoff_selection_receipt is None:
            raise R013CampaignConfigError(
                "R013 live preparation requires a completed typed handoff-selection receipt"
            )


def materialize_r013_handoff_selection(
    config: R013BudgetedFloorConfig,
    handoff_plan: HandoffABPlanV1 | Mapping[str, Any],
) -> R013BudgetedFloorConfig:
    """Purely bind completed A/B selection while retaining offline blockers."""

    if not isinstance(config, R013BudgetedFloorConfig):
        raise R013CampaignConfigError("R013 budgeted-floor config is not typed")
    plan = (
        handoff_plan
        if isinstance(handoff_plan, HandoffABPlanV1)
        else HandoffABPlanV1.from_mapping(handoff_plan)
    )
    receipt = plan.selection_receipt()
    if receipt is None:
        raise R013CampaignConfigError(
            "R013 handoff selection requires complete selected-policy n>=5 evidence"
        )
    selected = validate_handoff_policy(plan.materialize_handoff_policy())
    fingerprint = replace(
        config.campaign_fingerprint,
        handoff_policy=bind_handoff_policy_identity(selected, receipt.receipt_sha256),
    )
    return replace(
        config,
        handoff_policy=selected,
        selected_handoff_policy=selected,
        handoff_plan=plan,
        handoff_selection_receipt=receipt,
        campaign_fingerprint=fingerprint,
        # This is intentionally not a launch transition.  The runtime and
        # 200-novel prerequisites remain explicit in the immutable blocker set.
        launch_ready=False,
        blockers=OFFLINE_READINESS_BLOCKERS,
        status=OFFLINE_PREPARED_STATUS,
    )


def materialize_r013_live_ready_config(
    config: R013BudgetedFloorConfig,
    *,
    handoff_plan: HandoffABPlanV1 | Mapping[str, Any],
    handoff_selection_receipt: HandoffSelectionReceiptV1 | Mapping[str, Any],
    outward_boundary_runtime_receipt: R013RuntimeInstallationReceiptV1 | Mapping[str, Any],
    six_context_correction_runtime_receipt: (
        R013RuntimeInstallationReceiptV1 | Mapping[str, Any]
    ),
    runtime_strategy_sha256_value: str,
    controller_triplet_sha256: Mapping[str, Any],
    eoat_identity_sha256: str,
    script1_source_sha256: str,
) -> R013BudgetedFloorConfig:
    """Purely transition one offline config into a receipt-bound live config."""

    if not isinstance(config, R013BudgetedFloorConfig):
        raise R013CampaignConfigError("R013 budgeted-floor config is not typed")
    if config.launch_ready:
        raise R013CampaignConfigError("R013 live-ready transition source is not offline")
    if handoff_selection_receipt is None:
        raise R013CampaignConfigError(
            "R013 live-ready transition requires a complete hash-bound handoff receipt"
        )
    try:
        plan = (
            handoff_plan
            if isinstance(handoff_plan, HandoffABPlanV1)
            else HandoffABPlanV1.from_mapping(handoff_plan)
        )
        supplied_handoff_receipt = (
            handoff_selection_receipt
            if isinstance(handoff_selection_receipt, HandoffSelectionReceiptV1)
            else HandoffSelectionReceiptV1.from_mapping(handoff_selection_receipt)
        )
    except (TypeError, ValueError) as exc:
        raise R013CampaignConfigError(
            "R013 live-ready transition handoff receipt is malformed"
        ) from exc
    expected_handoff_receipt = plan.selection_receipt()
    if expected_handoff_receipt is None or expected_handoff_receipt != supplied_handoff_receipt:
        raise R013CampaignConfigError(
            "R013 live-ready transition requires a complete hash-bound handoff receipt"
        )
    offline_config = materialize_r013_handoff_selection(config, plan)
    if offline_config.handoff_selection_receipt != supplied_handoff_receipt:
        raise R013CampaignConfigError("R013 live-ready handoff receipt differs from config")
    fingerprint = _materialize_campaign_fingerprint_unchecked(
        offline_config,
        runtime_strategy_sha256_value=runtime_strategy_sha256_value,
        controller_triplet_sha256=controller_triplet_sha256,
        eoat_identity_sha256=eoat_identity_sha256,
        script1_source_sha256=script1_source_sha256,
    )
    source_identity = fingerprint.source_identity
    correction_identity = fingerprint.correction_runtime_strategy_identity
    outward_receipt = (
        outward_boundary_runtime_receipt
        if isinstance(outward_boundary_runtime_receipt, R013RuntimeInstallationReceiptV1)
        else R013RuntimeInstallationReceiptV1.from_mapping(outward_boundary_runtime_receipt)
    )
    correction_receipt = (
        six_context_correction_runtime_receipt
        if isinstance(
            six_context_correction_runtime_receipt, R013RuntimeInstallationReceiptV1
        )
        else R013RuntimeInstallationReceiptV1.from_mapping(
            six_context_correction_runtime_receipt
        )
    )
    if (
        outward_receipt.primitive != "outward_boundary"
        or outward_receipt.source_identity != source_identity
        or outward_receipt.runtime_identity != source_identity
        or outward_receipt.campaign_fingerprint_sha256 != fingerprint.sha256
    ):
        raise R013CampaignConfigError(
            "R013 outward-boundary runtime receipt is stale or mismatched"
        )
    if (
        correction_receipt.primitive != "six_context_correction"
        or correction_receipt.source_identity != source_identity
        or correction_receipt.runtime_identity != correction_identity
        or correction_receipt.campaign_fingerprint_sha256 != fingerprint.sha256
    ):
        raise R013CampaignConfigError(
            "R013 six-context correction runtime receipt is stale or mismatched"
        )
    transition = R013LiveReadyTransitionReceiptV1(
        source_config_fingerprint_sha256=offline_config.campaign_fingerprint.sha256,
        target_campaign_fingerprint_sha256=fingerprint.sha256,
        handoff_selection_receipt_sha256=supplied_handoff_receipt.receipt_sha256,
        outward_boundary_receipt_sha256=outward_receipt.receipt_sha256,
        six_context_correction_receipt_sha256=correction_receipt.receipt_sha256,
    )
    return replace(
        offline_config,
        status=LIVE_READY_STATUS,
        launch_ready=True,
        blockers=(),
        campaign_fingerprint=fingerprint,
        outward_boundary_runtime_receipt=outward_receipt,
        six_context_correction_runtime_receipt=correction_receipt,
        live_ready_transition_receipt=transition,
        transition_source_config_fingerprint_sha256=(
            offline_config.campaign_fingerprint.sha256
        ),
    )


materialize_r013_budgeted_floor_config = materialize_r013_handoff_selection


def load_r013_budgeted_floor_config(path: Path | None = None) -> R013BudgetedFloorConfig:
    source = DEFAULT_CONFIG_PATH if path is None else Path(path)
    if source.is_symlink() or not source.is_file():
        raise R013CampaignConfigError(
            f"R013 budgeted-floor config must be a regular file: {source}"
        )
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise R013CampaignConfigError("R013 budgeted-floor config is unreadable") from exc
    if not isinstance(value, Mapping):
        raise R013CampaignConfigError("R013 budgeted-floor config must be an object")
    required = {
        "schema", "version", "status", "launch_ready", "blockers",
        "completion_policy", "handoff_policy", "selected_handoff_policy",
        "handoff_plan", "handoff_selection_receipt", "floor_discovery_policy",
        "campaign_fingerprint",
    }
    live_ready_fields = {
        "outward_boundary_runtime_receipt",
        "six_context_correction_runtime_receipt",
        "live_ready_transition_receipt",
        "transition_source_config_fingerprint_sha256",
    }
    expected_fields = required | live_ready_fields if value.get("launch_ready") is True else required
    if set(value) != expected_fields:
        raise R013CampaignConfigError("R013 budgeted-floor config fields differ")
    try:
        completion_policy = CompletionPolicy.from_value(value["completion_policy"])
        handoff_policy = (
            None
            if value["handoff_policy"] is None
            else validate_handoff_policy(value["handoff_policy"])
        )
        selected_handoff_policy = (
            None
            if value["selected_handoff_policy"] is None
            else validate_handoff_policy(value["selected_handoff_policy"])
        )
        handoff_plan = HandoffABPlanV1.from_mapping(value["handoff_plan"])
        handoff_selection_receipt = (
            None
            if value["handoff_selection_receipt"] is None
            else HandoffSelectionReceiptV1.from_mapping(value["handoff_selection_receipt"])
        )
        floor_discovery_policy = FloorDiscoveryPolicyV1.from_mapping(
            value["floor_discovery_policy"]
        )
        campaign_fingerprint = validate_campaign_fingerprint(value["campaign_fingerprint"])
        outward_boundary_runtime_receipt = (
            None
            if not value.get("launch_ready")
            else R013RuntimeInstallationReceiptV1.from_mapping(
                value["outward_boundary_runtime_receipt"]
            )
        )
        six_context_correction_runtime_receipt = (
            None
            if not value.get("launch_ready")
            else R013RuntimeInstallationReceiptV1.from_mapping(
                value["six_context_correction_runtime_receipt"]
            )
        )
        live_ready_transition_receipt = (
            None
            if not value.get("launch_ready")
            else R013LiveReadyTransitionReceiptV1.from_mapping(
                value["live_ready_transition_receipt"]
            )
        )
        parsed = R013BudgetedFloorConfig(
            schema=value["schema"],
            version=value["version"],
            status=value["status"],
            launch_ready=value["launch_ready"],
            blockers=tuple(value["blockers"]),
            completion_policy=completion_policy,
            handoff_policy=handoff_policy,
            selected_handoff_policy=selected_handoff_policy,
            handoff_plan=handoff_plan,
            handoff_selection_receipt=handoff_selection_receipt,
            floor_discovery_policy=floor_discovery_policy,
            campaign_fingerprint=campaign_fingerprint,
            outward_boundary_runtime_receipt=outward_boundary_runtime_receipt,
            six_context_correction_runtime_receipt=six_context_correction_runtime_receipt,
            live_ready_transition_receipt=live_ready_transition_receipt,
            transition_source_config_fingerprint_sha256=value.get(
                "transition_source_config_fingerprint_sha256"
            ),
        )
    except (TypeError, ValueError, RuntimeError) as exc:
        if isinstance(exc, R013CampaignConfigError):
            raise
        raise R013CampaignConfigError("R013 budgeted-floor config values differ") from exc
    return parsed


def require_campaign_config_binding(
    *,
    config: R013BudgetedFloorConfig,
    snapshot: Mapping[str, Any],
) -> None:
    persisted = snapshot.get("budgeted_floor_config")
    if persisted != config.as_dict():
        raise R013CampaignConfigError(
            "R013 resumed campaign budgeted-floor config differs"
        )


__all__ = [
    "CONFIG_SCHEMA",
    "CONFIG_VERSION",
    "DEFAULT_CONFIG_PATH",
    "OFFLINE_PREPARED_STATUS",
    "LIVE_READY_STATUS",
    "OFFLINE_READINESS_BLOCKERS",
    "PROVISIONAL_HANDOFF_FINGERPRINT",
    "MANUAL_CANARY_PREPARATION_SCHEMA",
    "MANUAL_CANARY_PREPARATION_VERSION",
    "MANUAL_CANARY_ROLE",
    "HOME_TARE_BASELINE_CONTRACT",
    "HOME_TARE_PROCEDURE_SCHEMA",
    "HOME_TARE_PROCEDURE_VERSION",
    "R013BudgetedFloorConfig",
    "R013CampaignConfigError",
    "R013ManualCanaryPreparationProfileV1",
    "R013RuntimeInstallationReceiptV1",
    "R013LiveReadyTransitionReceiptV1",
    "FloorDiscoveryPolicyV1",
    "R013_HOST_SOURCE_PATHS",
    "controller_source_identity_sha256",
    "home_tare_procedure_identity",
    "load_r013_budgeted_floor_config",
    "materialize_campaign_fingerprint",
    "materialize_r013_budgeted_floor_config",
    "materialize_r013_handoff_selection",
    "materialize_r013_live_ready_config",
    "r013_host_source_closure",
    "require_campaign_config_binding",
]
