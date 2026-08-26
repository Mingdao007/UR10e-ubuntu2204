"""Non-launchable Figure-eight transfer template for R013 Outcome 4."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from .metrics import FIGURE8_METRIC, MetricFingerprintV1
from .path_context import FIGURE8_GEOMETRY_STATUS, FIGURE8_PATH_ID


FIGURE8_TRANSFER_SCHEMA = "step5d.autotune-v4/r013-figure8-transfer-package-v1"
FIGURE8_TRANSFER_VERSION = 1
AWAITING_CYCLOID_STATUS = "awaiting_cycloid_empirical_floor"
FIGURE8_SEED_MANIFEST_SCHEMA = "step5d.autotune-v4/r013-figure8-seed-manifest-v1"
FIGURE8_SEED_MANIFEST_VERSION = 1
FIGURE8_WARM_START_DESIGN_SEED = 813013
FIGURE8_SEED_MANIFEST_STATUS = "offline_transfer_ready"
FIGURE8_SEED_MANIFEST_BLOCKERS = (
    "figure8_runtime_seam_not_frozen",
    "figure8_geometry_not_live_accepted",
)
FIGURE8_FROZEN_COMPONENTS = (
    "logger", "handoff", "controller_seam", "objective", "admission", "report_renderer",
)
FIGURE8_LAUNCH_PACKAGE_SCHEMA = "step5d.autotune-v4/r013-figure8-launch-package-v1"
FIGURE8_LAUNCH_PACKAGE_VERSION = 1
FIGURE8_LAUNCH_PACKAGE_STATUS = "offline_transfer_package_ready"
FIGURE8_LAUNCH_ENTRYPOINT = "tools/run_step5d_autotune_v4_r013_live.py"
FIGURE8_LAUNCH_PACKAGE_BLOCKERS = (
    "figure8_runtime_seam_not_frozen",
    "figure8_geometry_not_live_accepted",
)
FIGURE8_LAUNCH_REQUIRED_RECEIPTS = (
    "cycloid_final_checkpoint_receipt",
    "cycloid_robust_core_receipt",
    "cycloid_compatible_correction_receipt",
    "handoff_selection_receipt",
    "figure8_runtime_installation_receipts",
    "figure8_component_freeze_receipt",
)
DEFAULT_CONVERGENCE_RULE = {
    "after_novel": 80,
    "fresh_valid_proposal_count": 25,
    "max_posterior_probability_strict_less_than": 0.05,
    "improvement_threshold_n": 0.01,
    "top3_count": 3,
    "top3_repeat_min_n": 5,
    "budget_stop_novel": 160,
    "no_absolute_mae_early_stop": True,
}


class FigureEightTransferError(ValueError):
    """The offline Figure-eight transfer template is incomplete or unsafe."""


def _sha256_payload(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _sha256_identity(value: Any, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise FigureEightTransferError(f"R013 Figure-eight {name} identity is invalid")
    return value


@dataclass(frozen=True)
class FigureEightTransferSeedManifestV1:
    """Offline, receipt-gated transfer seeds for the future one-campaign run."""

    source_cycloid_fingerprint: Mapping[str, Any] | str
    source_final_checkpoint_sha256: str
    source_robust_core_receipt_sha256: str
    source_correction_receipt_sha256: str
    controller_core_center: tuple[float, float, float, float]
    correction_weak_prior_weights: tuple[float, float, float, float, float, float]
    metric_fingerprint: MetricFingerprintV1 = field(default_factory=MetricFingerprintV1.figure8)
    warm_start_count: int = 24
    warm_start_design_seed: int = FIGURE8_WARM_START_DESIGN_SEED
    warm_start_design_dimensions: int = 4
    sin_cos_weights: tuple[float, float] = (0.0, 0.0)
    status: str = FIGURE8_SEED_MANIFEST_STATUS
    launch_ready: bool = False
    blockers: tuple[str, ...] = FIGURE8_SEED_MANIFEST_BLOCKERS
    frozen_components: tuple[str, ...] = FIGURE8_FROZEN_COMPONENTS
    schema: str = FIGURE8_SEED_MANIFEST_SCHEMA
    version: int = FIGURE8_SEED_MANIFEST_VERSION

    def __post_init__(self) -> None:
        if self.schema != FIGURE8_SEED_MANIFEST_SCHEMA or self.version != FIGURE8_SEED_MANIFEST_VERSION:
            raise FigureEightTransferError("R013 Figure-eight seed manifest schema/version differs")
        if self.status != FIGURE8_SEED_MANIFEST_STATUS or self.launch_ready is not False:
            raise FigureEightTransferError("R013 Figure-eight seed manifest must remain offline-only")
        if not self.source_cycloid_fingerprint:
            raise FigureEightTransferError("R013 Figure-eight seed source fingerprint is missing")
        for value, name in (
            (self.source_final_checkpoint_sha256, "final checkpoint"),
            (self.source_robust_core_receipt_sha256, "robust-core receipt"),
            (self.source_correction_receipt_sha256, "correction receipt"),
        ):
            _sha256_identity(value, name)
        if len(self.controller_core_center) != 4:
            raise FigureEightTransferError("R013 Figure-eight seed core dimensions differ")
        core = tuple(float(value) for value in self.controller_core_center)
        if any(not math.isfinite(value) for value in core):
            raise FigureEightTransferError("R013 Figure-eight seed core is not finite")
        if len(self.correction_weak_prior_weights) != 6:
            raise FigureEightTransferError("R013 Figure-eight weak prior dimensions differ")
        prior = tuple(float(value) for value in self.correction_weak_prior_weights)
        if any(not math.isfinite(value) for value in prior):
            raise FigureEightTransferError("R013 Figure-eight weak prior is not finite")
        if prior[4:] != (0.0, 0.0) or tuple(self.sin_cos_weights) != (0.0, 0.0):
            raise FigureEightTransferError("R013 Figure-eight sin/cos prior must be reset")
        if self.metric_fingerprint.metric_id != FIGURE8_METRIC:
            raise FigureEightTransferError("R013 Figure-eight seed metric differs")
        if self.warm_start_count != 24 or self.warm_start_design_dimensions != 4:
            raise FigureEightTransferError("R013 Figure-eight warm-start design differs")
        if type(self.warm_start_design_seed) is not int or self.warm_start_design_seed <= 0:
            raise FigureEightTransferError("R013 Figure-eight warm-start seed is invalid")
        if tuple(self.blockers) != FIGURE8_SEED_MANIFEST_BLOCKERS:
            raise FigureEightTransferError("R013 Figure-eight seed blockers differ")
        if tuple(self.frozen_components) != FIGURE8_FROZEN_COMPONENTS:
            raise FigureEightTransferError("R013 Figure-eight frozen components differ")
        object.__setattr__(self, "controller_core_center", core)
        object.__setattr__(self, "correction_weak_prior_weights", prior)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "status": self.status,
            "launch_ready": self.launch_ready,
            "source_cycloid_fingerprint": self.source_cycloid_fingerprint,
            "source_final_checkpoint_sha256": self.source_final_checkpoint_sha256,
            "source_robust_core_receipt_sha256": self.source_robust_core_receipt_sha256,
            "source_correction_receipt_sha256": self.source_correction_receipt_sha256,
            "controller_core_center": list(self.controller_core_center),
            "correction_weak_prior_weights": list(self.correction_weak_prior_weights),
            "sin_cos_weights": list(self.sin_cos_weights),
            "metric_fingerprint": self.metric_fingerprint.as_dict(),
            "warm_start_count": self.warm_start_count,
            "warm_start_design_seed": self.warm_start_design_seed,
            "warm_start_design_dimensions": self.warm_start_design_dimensions,
            "blockers": list(self.blockers),
            "frozen_components": list(self.frozen_components),
        }

    @classmethod
    def default(cls) -> "FigureEightTransferSeedManifestV1":
        return cls(
            source_cycloid_fingerprint="offline-source-pending",
            source_final_checkpoint_sha256="0" * 64,
            source_robust_core_receipt_sha256="0" * 64,
            source_correction_receipt_sha256="0" * 64,
            controller_core_center=(0.0, 0.0, 0.0, 0.0),
            correction_weak_prior_weights=(0.0,) * 6,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "FigureEightTransferSeedManifestV1":
        required = set(cls.default().as_dict())
        if not isinstance(value, Mapping) or set(value) != required:
            raise FigureEightTransferError("R013 Figure-eight seed manifest fields differ")
        return cls(
            source_cycloid_fingerprint=value["source_cycloid_fingerprint"],
            source_final_checkpoint_sha256=value["source_final_checkpoint_sha256"],
            source_robust_core_receipt_sha256=value["source_robust_core_receipt_sha256"],
            source_correction_receipt_sha256=value["source_correction_receipt_sha256"],
            controller_core_center=tuple(value["controller_core_center"]),
            correction_weak_prior_weights=tuple(value["correction_weak_prior_weights"]),
            sin_cos_weights=tuple(value["sin_cos_weights"]),
            metric_fingerprint=MetricFingerprintV1.from_mapping(value["metric_fingerprint"]),
            warm_start_count=value["warm_start_count"],
            warm_start_design_seed=value["warm_start_design_seed"],
            warm_start_design_dimensions=value["warm_start_design_dimensions"],
            status=value["status"],
            launch_ready=value["launch_ready"],
            blockers=tuple(value["blockers"]),
            frozen_components=tuple(value["frozen_components"]),
            schema=value["schema"],
            version=value["version"],
        )


def materialize_figure8_transfer_seed_manifest(
    coordinator: Any,
    final_checkpoint: Mapping[str, Any] | Any,
) -> FigureEightTransferSeedManifestV1:
    """Derive Figure-eight transfer seeds from a truthful cycloid final state.

    This is offline and read-only with respect to live systems. It rejects
    partial or forged final receipts and never changes launch-ready state.
    """

    from .checkpoint import R013CheckpointError, R013CheckpointReceiptV1

    try:
        checkpoint = (
            final_checkpoint
            if isinstance(final_checkpoint, R013CheckpointReceiptV1)
            else R013CheckpointReceiptV1.from_mapping(final_checkpoint)
        )
    except (R013CheckpointError, TypeError, ValueError) as exc:
        raise FigureEightTransferError(
            "R013 Figure-eight transfer final checkpoint is invalid"
        ) from exc
    if (
        checkpoint.checkpoint != "final"
        or checkpoint.observed_novel_count != 200
        or checkpoint.ready is not True
        or checkpoint.complete is not True
        or checkpoint.empirical_floor_claimed is not True
        or len(checkpoint.final_top3) != 3
        or any(group.n < 5 for group in checkpoint.final_top3)
    ):
        raise FigureEightTransferError(
            "R013 Figure-eight transfer requires a complete cycloid final receipt"
        )
    if not hasattr(coordinator, "snapshot"):
        raise FigureEightTransferError("R013 Figure-eight transfer coordinator is not typed")
    snapshot = coordinator.snapshot()
    if (
        not isinstance(snapshot, Mapping)
        or snapshot.get("novel_count") != 200
        or snapshot.get("complete") is not True
        or snapshot.get("fingerprint") != checkpoint.fingerprint
    ):
        raise FigureEightTransferError("R013 Figure-eight transfer cycloid state is incomplete")
    robust = getattr(coordinator, "robust_core_freeze", None)
    correction = getattr(coordinator, "compatible_correction_incumbent", None)
    if robust is None or correction is None:
        raise FigureEightTransferError("R013 Figure-eight transfer robust receipts are missing")
    if robust.fingerprint != snapshot["fingerprint"] or correction.fingerprint != snapshot["fingerprint"]:
        raise FigureEightTransferError("R013 Figure-eight transfer receipt fingerprint differs")
    if correction.controller_core != robust.controller_core:
        raise FigureEightTransferError("R013 Figure-eight transfer core/correction seam differs")
    correction_weights = tuple(correction.correction.weights)
    if len(correction_weights) != 6:
        raise FigureEightTransferError("R013 Figure-eight transfer correction dimensions differ")
    return FigureEightTransferSeedManifestV1(
        source_cycloid_fingerprint=checkpoint.fingerprint,
        source_final_checkpoint_sha256=_sha256_payload(checkpoint.as_dict()),
        source_robust_core_receipt_sha256=_sha256_payload(robust.as_dict()),
        source_correction_receipt_sha256=_sha256_payload(correction.as_dict()),
        controller_core_center=tuple(robust.controller_core.coordinates),
        correction_weak_prior_weights=correction_weights[:4] + (0.0, 0.0),
    )


@dataclass(frozen=True)
class FigureEightTransferPackageV1:
    metric_fingerprint: MetricFingerprintV1
    status: str = AWAITING_CYCLOID_STATUS
    launch_ready: bool = False
    transferable: tuple[str, ...] = (
        "P/D", "damping", "I/P", "tau", "anti_windup",
        "selected_handoff", "admission", "repeat_noise_model",
    )
    path_specific_unset: tuple[str, ...] = (
        "motion_Kp", "Ko", "figure8_correction",
    )
    weak_prior_fields: tuple[str, ...] = (
        "correction_bias_weight", "correction_speed_weight",
        "correction_signed_acceleration_weight", "correction_signed_curvature_weight",
    )
    sin_cos_weights: tuple[float, float] = (0.0, 0.0)
    max_novel: int = 160
    min_novel: int = 80
    designed_warm_start_count: int = 24
    convergence_rule: Mapping[str, Any] = field(default_factory=lambda: dict(DEFAULT_CONVERGENCE_RULE))
    blockers: tuple[str, ...] = (
        "cycloid_200_novel_empirical_floor_not_executed",
        "cycloid_final_top3_n5_not_completed",
        "live_handoff_ab_not_executed",
        "figure8_correction_runtime_not_installed",
        "figure8_controller_logger_admission_objective_seam_frozen_for_future_campaign",
        "figure8_geometry_is_offline_only_not_live_acceptance",
    )
    frozen_future_campaign_components: tuple[str, ...] = (
        "logger", "handoff", "controller_seam", "objective", "admission", "report_renderer",
    )
    path_id: str = FIGURE8_PATH_ID
    geometry_status: str = FIGURE8_GEOMETRY_STATUS
    schema: str = FIGURE8_TRANSFER_SCHEMA
    version: int = FIGURE8_TRANSFER_VERSION

    def __post_init__(self) -> None:
        if self.schema != FIGURE8_TRANSFER_SCHEMA or self.version != FIGURE8_TRANSFER_VERSION:
            raise FigureEightTransferError("R013 Figure-eight transfer schema/version differs")
        if not isinstance(self.metric_fingerprint, MetricFingerprintV1) or self.metric_fingerprint.metric_id != FIGURE8_METRIC:
            raise FigureEightTransferError("R013 Figure-eight transfer metric differs")
        if self.status != AWAITING_CYCLOID_STATUS or self.launch_ready is not False:
            raise FigureEightTransferError("R013 Figure-eight transfer must remain non-launchable")
        if self.path_id != FIGURE8_PATH_ID or self.geometry_status != FIGURE8_GEOMETRY_STATUS:
            raise FigureEightTransferError("R013 Figure-eight transfer geometry status differs")
        if self.max_novel != 160 or self.min_novel != 80 or self.designed_warm_start_count != 24:
            raise FigureEightTransferError("R013 Figure-eight transfer budget differs")
        if tuple(self.sin_cos_weights) != (0.0, 0.0):
            raise FigureEightTransferError("R013 Figure-eight sin/cos weights must be reset to zero")
        if not self.transferable or not self.path_specific_unset or not self.weak_prior_fields:
            raise FigureEightTransferError("R013 Figure-eight transfer field lists are incomplete")
        if self.frozen_future_campaign_components != (
            "logger", "handoff", "controller_seam", "objective", "admission", "report_renderer",
        ):
            raise FigureEightTransferError("R013 future campaign frozen component seam differs")
        expected_rule = DEFAULT_CONVERGENCE_RULE
        if dict(self.convergence_rule or {}) != expected_rule:
            raise FigureEightTransferError("R013 Figure-eight convergence rule differs")

    @classmethod
    def default(cls) -> "FigureEightTransferPackageV1":
        return cls(
            metric_fingerprint=MetricFingerprintV1.figure8(),
            convergence_rule=DEFAULT_CONVERGENCE_RULE,
        )

    def convergence_met(
        self,
        *,
        novel_count: int,
        fresh_valid_proposal_count: int,
        max_posterior_probability_of_improvement: float,
        improvement_threshold_n: float,
        top3_repeat_evidence: Sequence[int] | Mapping[str, int],
    ) -> bool:
        if not math.isclose(float(improvement_threshold_n), 0.01, rel_tol=0.0, abs_tol=1e-12):
            raise FigureEightTransferError(
                "R013 Figure-eight improvement threshold must equal 0.01 N"
            )
        if type(novel_count) is not int or type(fresh_valid_proposal_count) is not int:
            raise FigureEightTransferError("R013 Figure-eight convergence counts must be integers")
        probability = float(max_posterior_probability_of_improvement)
        if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
            raise FigureEightTransferError("R013 Figure-eight posterior probability is invalid")
        if isinstance(top3_repeat_evidence, Mapping):
            evidence = tuple(int(top3_repeat_evidence[key]) for key in sorted(top3_repeat_evidence))
        else:
            if isinstance(top3_repeat_evidence, (str, bytes)):
                raise FigureEightTransferError("R013 Figure-eight top-3 repeat evidence is invalid")
            evidence = tuple(int(value) for value in top3_repeat_evidence)
        return (
            novel_count >= self.min_novel
            and novel_count <= self.max_novel
            and fresh_valid_proposal_count == 25
            and probability < 0.05
            and len(evidence) == 3
            and all(value >= 5 for value in evidence)
        )

    def posterior_converged(self, **kwargs: Any) -> bool:
        """Name the posterior result separately from the 160-novel budget stop."""

        return self.convergence_met(**kwargs)

    def budget_stop(self, *, novel_count: int) -> bool:
        return novel_count == self.max_novel

    def completion_status(self, **kwargs: Any) -> dict[str, bool]:
        novel_count = int(kwargs["novel_count"])
        convergence_fields = {
            "fresh_valid_proposal_count",
            "max_posterior_probability_of_improvement",
            "improvement_threshold_n",
            "top3_repeat_evidence",
        }
        posterior_converged = (
            self.posterior_converged(**kwargs)
            if convergence_fields.issubset(kwargs)
            else False
        )
        return {
            "posterior_converged": posterior_converged,
            "budget_stop": self.budget_stop(novel_count=novel_count),
            "stop": posterior_converged or self.budget_stop(novel_count=novel_count),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "status": self.status,
            "launch_ready": self.launch_ready,
            "path_id": self.path_id,
            "geometry_status": self.geometry_status,
            "metric_fingerprint": self.metric_fingerprint.as_dict(),
            "transferable": list(self.transferable),
            "path_specific_unset": list(self.path_specific_unset),
            "weak_prior_fields": list(self.weak_prior_fields),
            "sin_cos_weights": list(self.sin_cos_weights),
            "max_novel": self.max_novel,
            "min_novel": self.min_novel,
            "designed_warm_start_count": self.designed_warm_start_count,
            "convergence_rule": dict(self.convergence_rule or {}),
            "blockers": list(self.blockers),
            "frozen_future_campaign_components": list(self.frozen_future_campaign_components),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "FigureEightTransferPackageV1":
        if not isinstance(value, Mapping):
            raise FigureEightTransferError("R013 Figure-eight transfer package is not a mapping")
        required = set(cls.default().as_dict())
        if set(value) != required:
            raise FigureEightTransferError("R013 Figure-eight transfer package fields differ")
        return cls(
            metric_fingerprint=MetricFingerprintV1.from_mapping(value["metric_fingerprint"]),
            status=value["status"],
            launch_ready=value["launch_ready"],
            transferable=tuple(value["transferable"]),
            path_specific_unset=tuple(value["path_specific_unset"]),
            weak_prior_fields=tuple(value["weak_prior_fields"]),
            sin_cos_weights=tuple(value["sin_cos_weights"]),
            max_novel=value["max_novel"],
            min_novel=value["min_novel"],
            designed_warm_start_count=value["designed_warm_start_count"],
            convergence_rule=dict(value["convergence_rule"]),
            blockers=tuple(value["blockers"]),
            frozen_future_campaign_components=tuple(value["frozen_future_campaign_components"]),
            path_id=value["path_id"],
            geometry_status=value["geometry_status"],
            schema=value["schema"],
            version=value["version"],
        )


@dataclass(frozen=True)
class FigureEightLaunchPackageV1:
    """Offline launch contract bound to a truthful cycloid transfer seed.

    The package is deliberately non-launchable.  It combines the generic
    Figure-eight campaign template with the receipt-gated seed manifest so a
    later owner can start one frozen campaign without reconstructing the
    transfer rules by hand.  It never installs a runtime seam or performs
    live I/O.
    """

    seed_manifest: FigureEightTransferSeedManifestV1
    transfer_template: FigureEightTransferPackageV1 = field(
        default_factory=FigureEightTransferPackageV1.default
    )
    campaign_entrypoint: str = FIGURE8_LAUNCH_ENTRYPOINT
    status: str = FIGURE8_LAUNCH_PACKAGE_STATUS
    launch_ready: bool = False
    blockers: tuple[str, ...] = FIGURE8_LAUNCH_PACKAGE_BLOCKERS
    required_receipts: tuple[str, ...] = FIGURE8_LAUNCH_REQUIRED_RECEIPTS
    schema: str = FIGURE8_LAUNCH_PACKAGE_SCHEMA
    version: int = FIGURE8_LAUNCH_PACKAGE_VERSION

    def __post_init__(self) -> None:
        if self.schema != FIGURE8_LAUNCH_PACKAGE_SCHEMA or self.version != FIGURE8_LAUNCH_PACKAGE_VERSION:
            raise FigureEightTransferError("R013 Figure-eight launch package schema/version differs")
        if self.status != FIGURE8_LAUNCH_PACKAGE_STATUS or self.launch_ready is not False:
            raise FigureEightTransferError("R013 Figure-eight launch package must remain offline-only")
        if not isinstance(self.seed_manifest, FigureEightTransferSeedManifestV1):
            raise FigureEightTransferError("R013 Figure-eight launch package seed manifest is not typed")
        if not isinstance(self.transfer_template, FigureEightTransferPackageV1):
            raise FigureEightTransferError("R013 Figure-eight launch package template is not typed")
        if self.seed_manifest.metric_fingerprint != self.transfer_template.metric_fingerprint:
            raise FigureEightTransferError("R013 Figure-eight launch package metric differs")
        if self.seed_manifest.source_cycloid_fingerprint == "offline-source-pending":
            raise FigureEightTransferError("R013 Figure-eight launch package source seed is a placeholder")
        if any(
            value == "0" * 64
            for value in (
                self.seed_manifest.source_final_checkpoint_sha256,
                self.seed_manifest.source_robust_core_receipt_sha256,
                self.seed_manifest.source_correction_receipt_sha256,
            )
        ):
            raise FigureEightTransferError("R013 Figure-eight launch package source receipts are placeholders")
        if self.transfer_template.launch_ready is not False:
            raise FigureEightTransferError("R013 Figure-eight launch package template is launch-ready")
        if self.campaign_entrypoint != FIGURE8_LAUNCH_ENTRYPOINT:
            raise FigureEightTransferError("R013 Figure-eight launch entrypoint differs")
        if tuple(self.blockers) != FIGURE8_LAUNCH_PACKAGE_BLOCKERS:
            raise FigureEightTransferError("R013 Figure-eight launch package blockers differ")
        if tuple(self.required_receipts) != FIGURE8_LAUNCH_REQUIRED_RECEIPTS:
            raise FigureEightTransferError("R013 Figure-eight launch package receipt list differs")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "status": self.status,
            "launch_ready": self.launch_ready,
            "campaign_entrypoint": self.campaign_entrypoint,
            "blockers": list(self.blockers),
            "required_receipts": list(self.required_receipts),
            "seed_manifest": self.seed_manifest.as_dict(),
            "transfer_template": self.transfer_template.as_dict(),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "FigureEightLaunchPackageV1":
        required = {
            "schema", "version", "status", "launch_ready", "campaign_entrypoint",
            "blockers", "required_receipts", "seed_manifest", "transfer_template",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise FigureEightTransferError("R013 Figure-eight launch package fields differ")
        return cls(
            seed_manifest=FigureEightTransferSeedManifestV1.from_mapping(value["seed_manifest"]),
            transfer_template=FigureEightTransferPackageV1.from_mapping(value["transfer_template"]),
            campaign_entrypoint=value["campaign_entrypoint"],
            status=value["status"],
            launch_ready=value["launch_ready"],
            blockers=tuple(value["blockers"]),
            required_receipts=tuple(value["required_receipts"]),
            schema=value["schema"],
            version=value["version"],
        )


def materialize_figure8_launch_package(
    seed_manifest: FigureEightTransferSeedManifestV1 | Mapping[str, Any],
    *,
    transfer_template: FigureEightTransferPackageV1 | Mapping[str, Any] | None = None,
) -> FigureEightLaunchPackageV1:
    """Bind a truthful cycloid seed to the frozen Figure-eight launch contract.

    This function is offline and read-only.  It rejects placeholder seeds and
    preserves ``launch_ready=false`` until a future owner supplies the
    separate Figure-eight runtime/freeze receipts.
    """

    try:
        parsed_seed = (
            seed_manifest
            if isinstance(seed_manifest, FigureEightTransferSeedManifestV1)
            else FigureEightTransferSeedManifestV1.from_mapping(seed_manifest)
        )
        parsed_template = (
            FigureEightTransferPackageV1.default()
            if transfer_template is None
            else (
                transfer_template
                if isinstance(transfer_template, FigureEightTransferPackageV1)
                else FigureEightTransferPackageV1.from_mapping(transfer_template)
            )
        )
        return FigureEightLaunchPackageV1(
            seed_manifest=parsed_seed,
            transfer_template=parsed_template,
        )
    except (FigureEightTransferError, TypeError, ValueError) as exc:
        if isinstance(exc, FigureEightTransferError):
            raise
        raise FigureEightTransferError(
            "R013 Figure-eight launch package materialization failed"
        ) from exc


def load_figure8_transfer_template(path: Path | None = None) -> FigureEightTransferPackageV1:
    selected = path or Path(__file__).resolve().parents[2] / "config/step5d/r013_figure8_transfer_template_v1.json"
    try:
        payload = json.loads(selected.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FigureEightTransferError("R013 Figure-eight transfer config cannot be read") from exc
    if not isinstance(payload, Mapping) or set(payload) != {"schema", "version", "transfer_package"}:
        raise FigureEightTransferError("R013 Figure-eight transfer config fields differ")
    if payload["schema"] != "step5d.autotune-v4/r013-figure8-transfer-config-v1" or payload["version"] != 1:
        raise FigureEightTransferError("R013 Figure-eight transfer config schema/version differs")
    return FigureEightTransferPackageV1.from_mapping(payload["transfer_package"])


__all__ = [
    "AWAITING_CYCLOID_STATUS", "FIGURE8_TRANSFER_SCHEMA",
    "FIGURE8_SEED_MANIFEST_SCHEMA", "FIGURE8_SEED_MANIFEST_VERSION",
    "FIGURE8_LAUNCH_PACKAGE_SCHEMA", "FIGURE8_LAUNCH_PACKAGE_VERSION",
    "FigureEightTransferError", "FigureEightTransferPackageV1",
    "FigureEightTransferSeedManifestV1", "materialize_figure8_transfer_seed_manifest",
    "FigureEightLaunchPackageV1", "materialize_figure8_launch_package",
    "load_figure8_transfer_template",
]
