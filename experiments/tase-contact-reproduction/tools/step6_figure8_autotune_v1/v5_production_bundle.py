"""Production composition for the isolated Autotuner V5 bundle.

This module owns no sensor/controller socket.  It prepares one resident V5
session per campaign role, opens the sole V5 writer, and composes the durable
campaign runner.  PRIMARY and CORRECTION use different fingerprints, state
roots, physical ledgers, optimizer journals, and proposal namespaces.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any, Mapping

from .campaign_runner import _stop_exact_resident
from .core import (
    FREEZE_CARRY_V1,
    FigureEightCampaignFingerprintV1,
    build_campaign_fingerprint,
    load_campaign_config,
)
from .live_composition import load_figure8_home_calibration_receipt
from .optimizer_bridge import SubprocessFigureEightProposalProviderV1
from .prepare_live import prepare_figure8_live_run, verify_controller_readback
from .source_identity import build_source_identity
from .v5_campaign import (
    CampaignConfigV2,
    CampaignIdentityV2,
    CampaignReportV2,
    CampaignRoleV2,
    ENTRY_MODE_HOME_ONLY_V1,
    ENTRY_MODE_ROLLOVER_CHAIN_V1,
    V5CampaignError,
    V5CampaignV2,
    _controller_hash,
)
from .v5_campaign_runner import (
    V5AmbiguousPhysicalDispatch,
    V5CampaignRunnerError,
    V5ExecutionJournalV1,
    V5PhysicalCampaignRunnerV1,
    V5RecoverableFailureClass,
    V5RecoverableFailureReceiptV1,
    V5RecoverableOwnerFailure,
)
from .v5_capability_acceptance import (
    V5CapabilityAcceptanceInputsV1,
    V5CapabilityAcceptanceV1,
    verify_no_contact_canary,
    verify_no_motion_recipe_receipt,
)
from .v5_lifecycle_ledger import (
    LedgerRole,
    V5PhysicalAdmissionLedgerV2,
    canonical_sha256,
)
from .v5_live_owner import (
    V5_LIVE_BINDING_SCHEMA,
    V5_LIVE_OWNER_VERSION,
    V5_READABLE_RUNTIME_IDENTITY,
    V5_RUNTIME_PROTOCOL,
    V5SingleWriterOwnerV1,
    build_v5_live_context,
)
from .v5_optimizer_journal import V5OptimizerTellJournalV1
from .v5_report import build_v5_evidence_report
from .v5_raw_archive import V5RawArchiveError, compress_r013life, verify_raw_archive_receipt
from .v5_extension_runner import V5ExtensionEpochV1
from .v5_sidecar_bundle import (
    V5PostHomeSidecarFanoutV1,
    V5SidecarBundleConfigV1,
)
try:
    from step5d_autotune_v4_r013.timing_scheduler import (
        LATE_CONTROL_FIFO_PROFILE,
        QUOTA_SAFE_OTHER_PROFILE,
        TimingSchedulerProfileV1,
    )
except ModuleNotFoundError:  # pragma: no cover
    from tools.step5d_autotune_v4_r013.timing_scheduler import (
        LATE_CONTROL_FIFO_PROFILE,
        QUOTA_SAFE_OTHER_PROFILE,
        TimingSchedulerProfileV1,
    )


V5_BUNDLE_SCHEMA = "step6.autotune/autotuner-v5-production-bundle-v1"
V5_BUNDLE_VERSION = 1
V5_RELEASE_SCHEMA = "step6.autotune/autotuner-v5-release-identity-v1"
V5_CAMPAIGN_FINGERPRINT_SCHEMA = (
    "step6.autotune/autotuner-v5-campaign-fingerprint-v1"
)
V5_FINAL_RECEIPT_SCHEMA = "step6.autotune/autotuner-v5-final-receipt-v1"
V5_RECOVERABLE_CONTINUATION_SCHEMA = (
    "step6.autotune/figure8-v5-recoverable-continuation-v1"
)


class V5ProductionBundleError(RuntimeError):
    """The V5 production bundle could not preserve its frozen identity."""


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise V5ProductionBundleError("V5 bundle value is not canonical JSON") from exc


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _file_sha(path: Path) -> str:
    target = Path(path)
    if target.is_symlink() or not target.is_file():
        raise V5ProductionBundleError(f"V5 identity file is unavailable: {target}")
    return hashlib.sha256(target.read_bytes()).hexdigest()


def _require_sha(value: Any, role: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise V5ProductionBundleError(f"V5 {role} is invalid")
    return value


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        destination.name + f".tmp-{os.getpid()}-{time.time_ns()}"
    )
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(dict(value), stream, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_json(path: Path, role: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise V5ProductionBundleError(f"{role} is unreadable") from exc
    if not isinstance(value, dict):
        raise V5ProductionBundleError(f"{role} is not an object")
    return value


def derive_v5_release_identity(
    *,
    source_identity: Mapping[str, Any],
    controller_triplet_sha256: Mapping[str, str],
    controller_readback_manifest_sha256: str,
    compatibility_fingerprint_sha256: str,
    home_calibration_receipt_sha256: str,
    composition_config_sha256: str,
    campaign_config_sha256: str,
    sidecars_config_sha256: str,
    timing_scheduler_profile: str = LATE_CONTROL_FIFO_PROFILE,
) -> dict[str, Any]:
    """Derive the immutable release identity used by both campaign roles."""

    triplet = dict(controller_triplet_sha256)
    TimingSchedulerProfileV1.from_id(timing_scheduler_profile)
    if set(triplet) != {"script", "txt", "urp"}:
        raise V5ProductionBundleError("V5 release controller triplet is incomplete")
    for role, digest in triplet.items():
        _require_sha(digest, f"release controller {role} SHA-256")
    body = {
        "schema": V5_RELEASE_SCHEMA,
        "version": V5_BUNDLE_VERSION,
        "source_identity_sha256": str(source_identity.get("source_sha256", "")),
        "controller_triplet_sha256": triplet,
        "controller_readback_manifest_sha256": controller_readback_manifest_sha256,
        "compatibility_fingerprint_sha256": compatibility_fingerprint_sha256,
        "home_geometry_calibration_receipt_sha256": home_calibration_receipt_sha256,
        "composition_config_sha256": composition_config_sha256,
        "campaign_config_sha256": campaign_config_sha256,
        "sidecars_config_sha256": sidecars_config_sha256,
        "timing_scheduler_profile": timing_scheduler_profile,
        "runtime_protocol": V5_RUNTIME_PROTOCOL,
        "runtime_revision": V5_READABLE_RUNTIME_IDENTITY[0],
        "runtime_extension": V5_READABLE_RUNTIME_IDENTITY[1],
        "layout": 607,
        "v4_layout_606_mutated": False,
    }
    for key, value in body.items():
        if key.endswith("sha256") and key != "controller_triplet_sha256":
            _require_sha(value, f"release {key}")
    return {**body, "release_identity_sha256": _sha(body)}


def derive_v5_campaign_fingerprint(
    *,
    role: CampaignRoleV2,
    release_identity_sha256: str,
    home_calibration_receipt_sha256: str,
    entry_mode: str = ENTRY_MODE_ROLLOVER_CHAIN_V1,
    parent: Mapping[str, Any] | None = None,
) -> str:
    """Derive one role-isolated campaign fingerprint."""

    if not isinstance(role, CampaignRoleV2):
        raise TypeError("V5 campaign fingerprint role must be typed")
    _require_sha(release_identity_sha256, "campaign release identity")
    _require_sha(home_calibration_receipt_sha256, "campaign Home calibration receipt")
    if entry_mode not in {ENTRY_MODE_ROLLOVER_CHAIN_V1, ENTRY_MODE_HOME_ONLY_V1}:
        raise V5ProductionBundleError("V5 campaign entry mode is unknown")
    if role is CampaignRoleV2.PRIMARY and parent is not None:
        raise V5ProductionBundleError("PRIMARY fingerprint cannot carry a parent")
    if role is CampaignRoleV2.CORRECTION:
        required = {
            "primary_campaign_fingerprint",
            "primary_closeout_sha256",
            "primary_physical_ledger_head_sha256",
            "primary_winner_controller_sha256",
        }
        if not isinstance(parent, Mapping) or set(parent) != required:
            raise V5ProductionBundleError("CORRECTION fingerprint parent is incomplete")
        for key in required:
            _require_sha(parent[key], f"CORRECTION parent {key}")
    body = {
        "schema": V5_CAMPAIGN_FINGERPRINT_SCHEMA,
        "version": V5_BUNDLE_VERSION,
        "role": role.value,
        "release_identity_sha256": release_identity_sha256,
        "home_geometry_calibration_receipt_sha256": home_calibration_receipt_sha256,
        "entry_mode": entry_mode,
        "parent": None if parent is None else dict(parent),
        "campaign_qualification": False,
        "epoch_qualification": False,
        "home_contacts_authority": "geometry_only",
        "performance_force_windows_blocking": False,
        "state_namespace_version": 2,
    }
    return _sha(body)


@dataclass(frozen=True)
class V5ProductionBundleInputsV1:
    root: Path
    state_root: Path
    controller_readback_dir: Path
    canary_controller_readback_dir: Path
    canary_dir: Path
    home_calibration_receipt: Path
    no_motion_recipe_receipt: Path
    optimizer_python: Path
    robot_host: str
    kunwei_host: str
    kunwei_port: int
    v5_campaign_config: Path
    compatibility_config: Path
    primary_campaign_fingerprint_override: str | None = None
    timing_scheduler_profile: str = LATE_CONTROL_FIFO_PROFILE

    def __post_init__(self) -> None:
        for value, role in (
            (self.root, "repository root"),
            (self.controller_readback_dir, "controller read-back"),
            (self.canary_controller_readback_dir, "canary controller read-back"),
            (self.canary_dir, "canary directory"),
            (self.home_calibration_receipt, "Home calibration receipt"),
            (self.no_motion_recipe_receipt, "no-motion recipe receipt"),
            (self.optimizer_python, "optimizer Python"),
            (self.v5_campaign_config, "V5 campaign config"),
            (self.compatibility_config, "compatibility config"),
        ):
            if not Path(value).resolve().exists():
                raise V5ProductionBundleError(f"V5 {role} is unavailable")
        if not self.robot_host or not self.kunwei_host or int(self.kunwei_port) <= 0:
            raise V5ProductionBundleError("V5 live endpoint inputs are invalid")
        TimingSchedulerProfileV1.from_id(self.timing_scheduler_profile)


@dataclass(frozen=True)
class V5BundleMaterialV1:
    release: Mapping[str, Any]
    primary_fingerprint: str
    compatibility_fingerprint: FigureEightCampaignFingerprintV1
    home_calibration: Mapping[str, Any]
    controller_triplet_sha256: Mapping[str, str]
    source_identity: Mapping[str, Any]
    no_motion_recipe: Mapping[str, Any]
    no_contact_canary: Mapping[str, Any]


class V5ProductionBundleV1:
    def __init__(self, inputs: V5ProductionBundleInputsV1) -> None:
        if not isinstance(inputs, V5ProductionBundleInputsV1):
            raise TypeError("V5 production bundle inputs must be typed")
        self.inputs = inputs
        self.root = Path(inputs.root).resolve()
        self.state_root = Path(inputs.state_root).resolve()
        self.campaign_config = CampaignConfigV2.from_path(inputs.v5_campaign_config)
        self.entry_mode = str(self.campaign_config.raw.get("entry_mode", ENTRY_MODE_ROLLOVER_CHAIN_V1))
        self.material = self._build_material()
        self._ensure_bundle_marker()
        self.capability = V5CapabilityAcceptanceV1(
            V5CapabilityAcceptanceInputsV1(
                root=self.root,
                state_root=self.state_root / "capability",
                controller_readback_dir=self.inputs.controller_readback_dir,
                canary_readback_dir=self.inputs.canary_controller_readback_dir,
                canary_dir=self.inputs.canary_dir,
                home_calibration_receipt=self.inputs.home_calibration_receipt,
                no_motion_recipe_receipt=self.inputs.no_motion_recipe_receipt,
                robot_host=self.inputs.robot_host,
                kunwei_host=self.inputs.kunwei_host,
                kunwei_port=int(self.inputs.kunwei_port),
                release_identity_sha256=str(
                    self.material.release["release_identity_sha256"]
                ),
                source_identity=self.material.source_identity,
                controller_triplet_sha256=self.material.controller_triplet_sha256,
                compatibility_fingerprint=self.material.compatibility_fingerprint,
            )
        )
        self.sidecars = V5PostHomeSidecarFanoutV1(
            self.state_root / "sidecars",
            V5SidecarBundleConfigV1.from_path(
                self.root / "config/step6/autotuner_v5_sidecars_v1.json"
            ),
        )

    @classmethod
    def for_extension(
        cls,
        inputs: V5ProductionBundleInputsV1,
        epoch: V5ExtensionEpochV1,
    ) -> "V5ProductionBundleV1":
        """Bind the same release/package to a fresh extension namespace."""

        if not isinstance(epoch, V5ExtensionEpochV1):
            raise TypeError("extension epoch must be typed")
        if inputs.v5_campaign_config.resolve() != (
            Path(inputs.root).resolve() / "config/step6/autotuner_v5_home_only_primary_v1.json"
        ).resolve():
            raise V5ProductionBundleError(
                "V5 extension requires the explicit Home-only campaign config"
            )
        extension_inputs = V5ProductionBundleInputsV1(
            root=inputs.root,
            state_root=epoch.state_root,
            controller_readback_dir=inputs.controller_readback_dir,
            canary_controller_readback_dir=inputs.canary_controller_readback_dir,
            canary_dir=inputs.canary_dir,
            home_calibration_receipt=inputs.home_calibration_receipt,
            no_motion_recipe_receipt=inputs.no_motion_recipe_receipt,
            optimizer_python=inputs.optimizer_python,
            robot_host=inputs.robot_host,
            kunwei_host=inputs.kunwei_host,
            kunwei_port=inputs.kunwei_port,
            v5_campaign_config=inputs.v5_campaign_config,
            compatibility_config=inputs.compatibility_config,
            primary_campaign_fingerprint_override=epoch.fingerprint,
            timing_scheduler_profile=inputs.timing_scheduler_profile,
        )
        return cls(extension_inputs)

    def _build_material(self) -> V5BundleMaterialV1:
        CampaignConfigV2.from_path(self.inputs.v5_campaign_config)
        source = build_source_identity(self.root)
        compatibility_config = load_campaign_config(self.inputs.compatibility_config)
        configured_source = str(
            compatibility_config.raw["controller"]["source_parent_sha256"]
        )
        if source["source_sha256"] != configured_source:
            raise V5ProductionBundleError(
                "V5 configured source identity is stale; rebuild closure before live"
            )
        readback, triplet = verify_controller_readback(
            root=self.root,
            readback_dir=self.inputs.controller_readback_dir,
        )
        home = load_figure8_home_calibration_receipt(
            self.inputs.home_calibration_receipt
        )
        compatibility = build_campaign_fingerprint(
            config=compatibility_config,
            source_sha256=source["source_sha256"],
            controller_triplet_sha256=triplet,
            handoff_policy=FREEZE_CARRY_V1,
            home_materialization=home,
            _allow_materialized_handoff=True,
        )
        no_motion = verify_no_motion_recipe_receipt(
            self.inputs.no_motion_recipe_receipt,
            expected_source_sha256=source["source_sha256"],
            expected_triplet_sha256=triplet,
        )
        canary = verify_no_contact_canary(
            root=self.root,
            canary_dir=self.inputs.canary_dir,
            canary_readback_dir=self.inputs.canary_controller_readback_dir,
            expected_home_pose=compatibility.home_pose,
            expected_home_receipt_sha256=str(home["receipt_sha256"]),
        )
        release = derive_v5_release_identity(
            source_identity=source,
            controller_triplet_sha256=triplet,
            controller_readback_manifest_sha256=_file_sha(
                Path(self.inputs.controller_readback_dir) / "manifest.json"
            ),
            compatibility_fingerprint_sha256=compatibility.sha256,
            home_calibration_receipt_sha256=str(home["receipt_sha256"]),
            composition_config_sha256=_file_sha(
                self.root / "config/step6/autotuner_v5_composition_contract_v2.json"
            ),
            campaign_config_sha256=_file_sha(self.inputs.v5_campaign_config),
            sidecars_config_sha256=_file_sha(
                self.root / "config/step6/autotuner_v5_sidecars_v1.json"
            ),
            timing_scheduler_profile=self.inputs.timing_scheduler_profile,
        )
        primary = derive_v5_campaign_fingerprint(
            role=CampaignRoleV2.PRIMARY,
            release_identity_sha256=release["release_identity_sha256"],
            home_calibration_receipt_sha256=str(home["receipt_sha256"]),
            entry_mode=self.entry_mode,
        )
        if self.inputs.primary_campaign_fingerprint_override is not None:
            _require_sha(
                self.inputs.primary_campaign_fingerprint_override,
                "primary campaign fingerprint override",
            )
            primary = self.inputs.primary_campaign_fingerprint_override
        # Keep normalized read-back content in the release evidence without
        # making it another identity source.
        release = {**release, "controller_readback": readback}
        return V5BundleMaterialV1(
            release,
            primary,
            compatibility,
            home,
            triplet,
            source,
            no_motion,
            canary,
        )

    def _bundle_marker(self) -> dict[str, Any]:
        body = {
            "schema": V5_BUNDLE_SCHEMA,
            "version": V5_BUNDLE_VERSION,
            "state_root": str(self.state_root),
            "release": dict(self.material.release),
            "primary_campaign_fingerprint": self.material.primary_fingerprint,
            "compatibility_fingerprint_sha256": self.material.compatibility_fingerprint.sha256,
            "home_calibration_receipt": str(
                Path(self.inputs.home_calibration_receipt).resolve()
            ),
            "home_calibration_receipt_sha256": str(
                self.material.home_calibration["receipt_sha256"]
            ),
            "old_v5_state_resume_eligible": False,
            "v4_layout_606_mutated": False,
            "no_motion_recipe": dict(self.material.no_motion_recipe),
            "no_contact_canary": dict(self.material.no_contact_canary),
        }
        return {**body, "bundle_identity_sha256": _sha(body)}

    def _ensure_bundle_marker(self) -> None:
        marker_path = self.state_root / "bundle_identity.json"
        expected = self._bundle_marker()
        if marker_path.exists():
            if _read_json(marker_path, "V5 bundle marker") != expected:
                raise V5ProductionBundleError(
                    "V5 state root identity changed; old state cannot resume"
                )
            self._reject_ambiguous_state_root()
            return
        if self.state_root.exists() and any(self.state_root.iterdir()):
            raise V5ProductionBundleError(
                "V5 state root is non-fresh and has no matching bundle marker"
            )
        self.state_root.mkdir(parents=True, exist_ok=True)
        _atomic_json(marker_path, expected)

    def _reject_ambiguous_state_root(self) -> None:
        """Fail closed on a dead/partial owner instead of resuming it.

        The outer managed launcher is not allowed to kill a live child between
        ARM and cleanup.  This guard is the second line of defense for older
        roots or host crashes: an ``owner_open`` phase, active authority, or
        lifecycle ``.part`` artifact requires a new state root and explicit
        recovery, never a second writer.
        """

        for phase_path in self.state_root.rglob("phase_runtime.json"):
            try:
                phase = _read_json(phase_path, "V5 phase runtime")
            except Exception as exc:
                raise V5ProductionBundleError(
                    f"V5 state root phase receipt is unreadable: {phase_path}"
                ) from exc
            if phase.get("status") in {"preparing", "owner_open"}:
                raise V5ProductionBundleError(
                    "V5 state root contains an ambiguous owner phase; fresh state root required"
                )
        active_authorities: list[Path] = []
        for authority_path in self.state_root.rglob("owner-authority.json"):
            try:
                authority = _read_json(authority_path, "V5 owner authority")
            except Exception as exc:
                raise V5ProductionBundleError(
                    f"V5 owner authority is unreadable: {authority_path}"
                ) from exc
            if authority.get("state") == "ACTIVE" or authority.get("revoked_at_unix_ns") is None:
                active_authorities.append(authority_path)
        if active_authorities:
            raise V5ProductionBundleError(
                "V5 state root contains active/unrevoked owner authority; fresh state root required"
            )
        partials = tuple(self.state_root.rglob("*.r013life.part"))
        if partials:
            raise V5ProductionBundleError(
                "V5 state root contains partial lifecycle artifacts; fresh state root required"
            )

    @staticmethod
    def _ledger_role(role: CampaignRoleV2) -> LedgerRole:
        return LedgerRole.PRIMARY if role is CampaignRoleV2.PRIMARY else LedgerRole.CORRECTION

    def _role_identity(
        self,
        *,
        role: CampaignRoleV2,
        fingerprint: str,
        fixed_controller_path: Mapping[str, Any] | None = None,
        primary_closeout_sha256: str | None = None,
        primary_ledger_head_sha256: str | None = None,
    ) -> CampaignIdentityV2:
        role_root = self.state_root / role.value.lower() / "campaign"
        if role is CampaignRoleV2.PRIMARY:
            return CampaignIdentityV2(
                role,
                fingerprint,
                str(self.material.release["release_identity_sha256"]),
                role_root,
                f"{role.value}:{fingerprint}",
                entry_mode=self.entry_mode,
            )
        if fixed_controller_path is None:
            raise V5ProductionBundleError("CORRECTION fixed controller is absent")
        return CampaignIdentityV2(
            role,
            fingerprint,
            str(self.material.release["release_identity_sha256"]),
            role_root,
            f"{role.value}:{fingerprint}",
            fixed_controller_path=dict(fixed_controller_path),
            primary_winner_controller_sha256=_controller_hash(fixed_controller_path),
            primary_closeout_sha256=primary_closeout_sha256,
            primary_ledger_head_sha256=primary_ledger_head_sha256,
            entry_mode=self.entry_mode,
        )

    def _phase_runtime_path(
        self,
        role: CampaignRoleV2,
        *,
        identity: CampaignIdentityV2 | None = None,
    ) -> Path:
        root = self.state_root if identity is None else Path(identity.state_root).parent.parent
        return root / role.value.lower() / "phase_runtime.json"

    def _submit_post_home_sidecars(self, result: Any) -> Mapping[str, Any]:
        """Fan out sealed evidence only after owner Home and durable accounting."""

        artifact = result.records[0].source_artifact
        return self.sidecars.try_submit_filter_shadow(
            chain_id=result.chain_id,
            artifact_path=artifact.artifact_path,
            lifecycle_receipt=artifact.receipt,
            event_bundle=artifact.event_bundle,
        )

    def _archive_role_raw(
        self,
        *,
        identity: CampaignIdentityV2,
        physical: V5PhysicalAdmissionLedgerV2,
    ) -> Mapping[str, Any]:
        """Compress sealed raw only after the resident writer is released."""

        archive_root = self.state_root / "raw-archives" / identity.role.value.lower()
        archive_root.mkdir(parents=True, exist_ok=True)
        entries: list[Mapping[str, Any]] = []
        seen: set[str] = set()
        for record in physical.records:
            raw_path = str(record.source_artifact.artifact_path)
            if raw_path in seen:
                continue
            seen.add(raw_path)
            destination = archive_root / (Path(raw_path).name + ".zst")
            receipt_path = destination.with_suffix(destination.suffix + ".json")
            if receipt_path.is_file():
                receipt = verify_raw_archive_receipt(
                    _read_json(receipt_path, "raw archive receipt")
                )
            else:
                try:
                    receipt = compress_r013life(
                        Path(raw_path),
                        output_path=destination,
                        retain_uncompressed=True,
                    )
                except V5RawArchiveError as exc:
                    raise V5ProductionBundleError(
                        f"sealed raw archive failed for {raw_path}: {exc}"
                    ) from exc
                _atomic_json(receipt_path, receipt)
            entries.append(receipt)
        body = {
            "schema": "step6.autotune/figure8-v5-raw-archive-manifest-v1",
            "version": 1,
            "role": identity.role.value,
            "campaign_fingerprint_sha256": identity.campaign_fingerprint,
            "physical_ledger_head_sha256": physical.head_sha256,
            "raw_count": len(entries),
            "entries": list(entries),
            "raw_deleted": False,
            "summary_only_fallback": False,
        }
        manifest = {**body, "manifest_sha256": canonical_sha256(body)}
        _atomic_json(archive_root / "manifest.json", manifest)
        return manifest

    def _sidecar_snapshot(self) -> Mapping[str, Any]:
        try:
            return self.sidecars.snapshot()
        except Exception as exc:  # noqa: BLE001 -- observation failure is isolated
            return {
                "schema": "step6.autotune/autotuner-v5-sidecar-status-v1",
                "version": 1,
                "status": "FAILED_ISOLATED",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "physical_campaign_dependency": False,
                "control_authority": False,
            }

    def _next_resident_dir(self, role: CampaignRoleV2) -> Path:
        live_root = self.state_root / "live"
        live_root.mkdir(parents=True, exist_ok=True)
        serial = 1
        while (live_root / f"{role.value.lower()}-resident-{serial:03d}").exists():
            serial += 1
        return live_root / f"{role.value.lower()}-resident-{serial:03d}"

    def _prepare_or_reuse_resident(
        self,
        *,
        role: CampaignRoleV2,
        fingerprint: str,
    ) -> tuple[Path, int]:
        phase_path = self._phase_runtime_path(role)
        if phase_path.is_file():
            phase = _read_json(phase_path, f"{role.value} phase runtime")
            if (
                phase.get("status") in {"resident_ready", "owner_open"}
                and phase.get("campaign_fingerprint") == fingerprint
            ):
                run_dir = Path(str(phase.get("run_dir", ""))).resolve()
                ready = _read_json(
                    run_dir / "r013_live_owner_ready.json",
                    f"{role.value} resident READY",
                )
                return run_dir, int(ready["session_epoch"])
        run_dir = self._next_resident_dir(role)
        phase_body = {
            "schema": "step6.autotune/autotuner-v5-phase-runtime-v1",
            "version": 1,
            "role": role.value,
            "status": "preparing",
            "campaign_fingerprint": fingerprint,
            "run_dir": str(run_dir),
        }
        _atomic_json(phase_path, phase_body)
        nonce = time.time_ns()
        prepare_figure8_live_run(
            run_dir,
            root=self.root,
            robot_host=self.inputs.robot_host,
            kunwei_host=self.inputs.kunwei_host,
            kunwei_port=int(self.inputs.kunwei_port),
            controller_readback_dir=self.inputs.controller_readback_dir,
            canary_dir=self.inputs.canary_dir,
            home_calibration_receipt_path=self.inputs.home_calibration_receipt,
            fingerprint=self.material.compatibility_fingerprint,
            campaign_id=f"autotuner-v5-{role.value.lower()}",
            run_id=f"autotuner-v5-{role.value.lower()}-{nonce}",
            attempt_id=f"r006-v5-{role.value.lower()}-{nonce}",
        )
        ready = _read_json(
            run_dir / "r013_live_owner_ready.json", f"{role.value} resident READY"
        )
        epoch = int(ready["session_epoch"])
        binding_body = {
            "schema": V5_LIVE_BINDING_SCHEMA,
            "version": V5_LIVE_OWNER_VERSION,
            "campaign_fingerprint": fingerprint,
            "release_identity_sha256": self.material.release[
                "release_identity_sha256"
            ],
            "role": role.value,
            "session_epoch": epoch,
            "run_dir": str(run_dir.resolve()),
            "compatibility_fingerprint_sha256": ready[
                "figure8_campaign_fingerprint_sha256"
            ],
        }
        _atomic_json(
            run_dir / "v5_live_binding.json",
            {**binding_body, "binding_sha256": canonical_sha256(binding_body)},
        )
        _atomic_json(
            phase_path,
            {
                **phase_body,
                "status": "resident_ready",
                "session_epoch": epoch,
                "v5_live_binding": str(run_dir / "v5_live_binding.json"),
            },
        )
        return run_dir, epoch

    def _campaign_components(
        self,
        *,
        identity: CampaignIdentityV2,
        session_epoch: int,
    ) -> tuple[
        V5CampaignV2,
        V5PhysicalAdmissionLedgerV2,
        V5OptimizerTellJournalV1,
        V5ExecutionJournalV1,
    ]:
        role_root = Path(identity.state_root).parent
        provider = SubprocessFigureEightProposalProviderV1(
            optimizer_python=self.inputs.optimizer_python,
            state_dir=role_root / "optimizer-proposals",
        )
        marker = Path(identity.state_root) / "namespace.json"
        campaign = (
            V5CampaignV2.resume(
                identity,
                qlognei_provider=provider,
                wire_session_epoch=session_epoch,
            )
            if marker.is_file()
            else V5CampaignV2.from_config(
                self.inputs.v5_campaign_config,
                identity,
                qlognei_provider=provider,
                wire_session_epoch=session_epoch,
            )
        )
        ledger_role = self._ledger_role(identity.role)
        physical = V5PhysicalAdmissionLedgerV2(
            role_root / "physical.jsonl",
            campaign_fingerprint=identity.campaign_fingerprint,
            release_identity_sha256=identity.release_identity_sha256,
            role=ledger_role,
        )
        optimizer = V5OptimizerTellJournalV1(
            role_root / "optimizer-tells.jsonl",
            campaign_fingerprint=identity.campaign_fingerprint,
            release_identity_sha256=identity.release_identity_sha256,
            role=ledger_role,
        )
        execution = V5ExecutionJournalV1(
            role_root / "execution.jsonl",
            campaign_fingerprint=identity.campaign_fingerprint,
            release_identity_sha256=identity.release_identity_sha256,
            role=ledger_role,
        )
        return campaign, physical, optimizer, execution

    def _campaign_components_for_resume(
        self,
        *,
        identity: CampaignIdentityV2,
        session_epoch: int,
    ) -> tuple[
        V5CampaignV2,
        V5PhysicalAdmissionLedgerV2,
        V5OptimizerTellJournalV1,
        V5ExecutionJournalV1,
    ]:
        """Open a stale Home-only ledger only to close its proven dispatch.

        A failed owner plan carries the old wire epoch, while the next
        resident must use a fresh epoch.  The temporary old-epoch campaign is
        used solely by the recovery adapter; it is replaced with the fresh
        epoch campaign immediately after the continuation row is durable.
        """

        try:
            return self._campaign_components(
                identity=identity,
                session_epoch=session_epoch,
            )
        except V5CampaignError as original:
            if (
                identity.entry_mode != ENTRY_MODE_HOME_ONLY_V1
                or "active plan belongs to another live session epoch"
                not in str(original)
            ):
                raise
            execution_path = Path(identity.state_root).parent / "execution.jsonl"
            probe = V5ExecutionJournalV1(
                execution_path,
                campaign_fingerprint=identity.campaign_fingerprint,
                release_identity_sha256=identity.release_identity_sha256,
                role=self._ledger_role(identity.role),
            )
            ambiguous = tuple(probe.ambiguous_chain_ids)
            if len(ambiguous) != 1:
                raise original
            plans = probe.dispatched_plans(ambiguous[0])
            if (
                len(plans) != 1
                or plans[0].entry_mode != ENTRY_MODE_HOME_ONLY_V1
                or not plans[0].requires_home
                or plans[0].packable
                or self._partial_connection_reset_receipt(
                    identity=identity,
                    chain_id=ambiguous[0],
                )
                is None
            ):
                raise original
            return self._campaign_components(
                identity=identity,
                session_epoch=plans[0].wire_epoch,
            )

    def _completed_role(
        self, identity: CampaignIdentityV2
    ) -> tuple[CampaignReportV2, V5PhysicalAdmissionLedgerV2, Mapping[str, Any]] | None:
        report_path = Path(identity.state_root) / "report.json"
        release_path = Path(identity.state_root).parent / "writer_release_receipt.json"
        phase_path = self._phase_runtime_path(identity.role, identity=identity)
        if not report_path.is_file() or not release_path.is_file() or not phase_path.is_file():
            return None
        phase = _read_json(phase_path, f"{identity.role.value} phase runtime")
        release = _read_json(release_path, f"{identity.role.value} writer release")
        if (
            phase.get("status") != "released_stopped_home"
            or release.get("home_verified") is not True
            or release.get("stopped") is not True
            or release.get("writer_released") is not True
        ):
            return None
        campaign, physical, _optimizer, _execution = self._campaign_components(
            identity=identity,
            session_epoch=1,
        )
        return campaign.report(), physical, release

    def _release_resident(
        self,
        *,
        identity: CampaignIdentityV2,
        phase_path: Path,
    ) -> Mapping[str, Any]:
        raw_release = _stop_exact_resident(
            robot_host=self.inputs.robot_host,
            expected_home_pose=self.material.compatibility_fingerprint.home_pose,
        )
        release = {
            **raw_release,
            "home_verified": raw_release.get("passed") is True,
            "stopped": raw_release.get("passed") is True,
        }
        release_path = (
            self.state_root
            / identity.role.value.lower()
            / "writer_release_receipt.json"
        )
        _atomic_json(release_path, release)
        phase = (
            _read_json(phase_path, f"{identity.role.value} phase runtime")
            if phase_path.is_file()
            else {
                "schema": "step6.autotune/autotuner-v5-phase-runtime-v1",
                "version": 1,
                "role": identity.role.value,
                "campaign_fingerprint": identity.campaign_fingerprint,
            }
        )
        _atomic_json(
            phase_path,
            {
                **phase,
                "status": "released_stopped_home",
                "writer_release_receipt": str(release_path),
            },
        )
        cold = _read_json(release_path, f"{identity.role.value} writer release")
        if (
            cold.get("home_verified") is not True
            or cold.get("stopped") is not True
            or cold.get("writer_released") is not True
        ):
            raise V5ProductionBundleError(
                f"{identity.role.value} release is not Home/STOPPED/writer-released"
            )
        return cold

    @staticmethod
    def _is_canonical_connection_reset(error: BaseException) -> bool:
        """Recognize only the mature RTDE reset owner failure."""

        text = str(error).lower()
        return (
            "canonical v5 rtde output read failed" in text
            and "connection reset by peer" in text
        )

    def _partial_connection_reset_receipt(
        self,
        *,
        identity: CampaignIdentityV2,
        chain_id: str,
    ) -> V5RecoverableFailureReceiptV1 | None:
        """Build typed continuation evidence from immutable post-cleanup files."""

        candidates = tuple(
            sorted(
                path
                for path in (self.state_root / "live").rglob(
                    f"*-{chain_id}.r013life.json"
                )
                if not path.is_symlink() and path.is_file()
            )
        )
        if len(candidates) != 1:
            return None
        partial_path = candidates[0].resolve()
        try:
            partial = _read_json(partial_path, "V5 partial lifecycle receipt")
        except (V5ProductionBundleError, V5CampaignRunnerError):
            return None
        errors = partial.get("errors")
        if not isinstance(errors, list) or not errors or not isinstance(errors[0], str):
            return None
        reason = str(errors[0])
        if not self._is_canonical_connection_reset(
            RuntimeError(reason)
        ):
            return None
        if (
            partial.get("status") != "incomplete"
            or partial.get("coverage_complete") is not False
            or partial.get("home_verified") is not False
            or partial.get("artifact_path") is None
        ):
            return None
        artifact_path = Path(str(partial["artifact_path"])).resolve()
        if artifact_path.parent != partial_path.parent or artifact_path.is_symlink():
            return None
        try:
            artifact_sha256 = _file_sha(artifact_path)
        except (V5ProductionBundleError, V5CampaignRunnerError):
            return None
        if partial.get("artifact_sha256") != artifact_sha256:
            return None
        release_path = (
            self.state_root
            / identity.role.value.lower()
            / "writer_release_receipt.json"
        ).resolve()
        try:
            release = _read_json(release_path, "V5 writer release receipt")
            release_sha256 = _file_sha(release_path)
            partial_sha256 = _file_sha(partial_path)
        except (V5ProductionBundleError, V5CampaignRunnerError):
            return None
        if (
            release.get("home_verified") is not True
            or release.get("stopped") is not True
            or release.get("writer_released") is not True
        ):
            return None
        try:
            return V5RecoverableFailureReceiptV1(
                chain_id=chain_id,
                failure_class=V5RecoverableFailureClass.CONNECTION_RESET,
                reason=reason,
                partial_receipt_path=str(partial_path),
                partial_receipt_sha256=partial_sha256,
                partial_artifact_path=str(artifact_path),
                partial_artifact_sha256=artifact_sha256,
                writer_release_receipt_path=str(release_path),
                writer_release_receipt_sha256=release_sha256,
                partial_receipt=partial,
                writer_release_receipt=release,
            )
        except (V5ProductionBundleError, V5CampaignRunnerError):
            return None

    def _repair_recoverable_home_only_dispatches(
        self,
        *,
        identity: CampaignIdentityV2,
        reconciler: V5PhysicalCampaignRunnerV1,
        execution: V5ExecutionJournalV1,
    ) -> tuple[V5RecoverableFailureReceiptV1, ...]:
        """Repair only proven serial Home-only resets before ambiguous check."""

        if identity.entry_mode != ENTRY_MODE_HOME_ONLY_V1:
            return ()
        chain_ids = tuple(execution.ambiguous_chain_ids)
        if not chain_ids:
            return ()
        receipts: list[V5RecoverableFailureReceiptV1] = []
        for chain_id in chain_ids:
            plans = execution.dispatched_plans(chain_id)
            if (
                len(plans) != 1
                or plans[0].entry_mode != ENTRY_MODE_HOME_ONLY_V1
                or not plans[0].requires_home
                or plans[0].packable
            ):
                return ()
            receipt = self._partial_connection_reset_receipt(
                identity=identity,
                chain_id=chain_id,
            )
            if receipt is None:
                return ()
            receipts.append(receipt)
        for receipt in receipts:
            execution.append_recoverable_failure(receipt.chain_id, receipt)
        reconciler.reconcile_durable_results()
        return tuple(receipts)

    def _publish_recoverable_continuation_marker(
        self,
        *,
        identity: CampaignIdentityV2,
        phase_path: Path,
        receipts: tuple[V5RecoverableFailureReceiptV1, ...],
    ) -> None:
        phase = _read_json(phase_path, f"{identity.role.value} phase runtime")
        marker = {
            "schema": V5_RECOVERABLE_CONTINUATION_SCHEMA,
            "version": 1,
            "marker": "V5_RECOVERABLE_CONTINUATION_V1",
            "status": "resume_required",
            "role": identity.role.value,
            "campaign_fingerprint": identity.campaign_fingerprint,
            "chain_ids": [receipt.chain_id for receipt in receipts],
            "receipt_sha256s": [
                canonical_sha256(receipt.as_dict()) for receipt in receipts
            ],
        }
        _atomic_json(
            phase_path,
            {**phase, "recoverable_continuation": marker},
        )

    def _run_role(
        self,
        *,
        identity: CampaignIdentityV2,
    ) -> tuple[CampaignReportV2, V5PhysicalAdmissionLedgerV2, Mapping[str, Any]]:
        completed = self._completed_role(identity)
        if completed is not None:
            self._archive_role_raw(
                identity=identity,
                physical=completed[1],
            )
            return completed
        report_path = Path(identity.state_root) / "report.json"
        phase_path = self._phase_runtime_path(identity.role, identity=identity)
        if report_path.is_file():
            campaign, physical, _optimizer, _execution = self._campaign_components(
                identity=identity,
                session_epoch=1,
            )
            report = campaign.report()
            release = self._release_resident(identity=identity, phase_path=phase_path)
            self._archive_role_raw(identity=identity, physical=physical)
            return report, physical, release
        run_dir, epoch = self._prepare_or_reuse_resident(
            role=identity.role,
            fingerprint=identity.campaign_fingerprint,
        )
        campaign, physical, optimizer, execution = self._campaign_components_for_resume(
            identity=identity,
            session_epoch=epoch,
        )
        class _ReconcileOnlyOwner:
            @staticmethod
            def execute_chain(_request: Any) -> Any:
                raise V5ProductionBundleError(
                    "reconciliation must never dispatch physical motion"
                )

        reconciler = V5PhysicalCampaignRunnerV1(
            campaign=campaign,
            owner=_ReconcileOnlyOwner(),
            physical_ledger=physical,
            optimizer_journal=optimizer,
            execution_journal=execution,
        )
        reconciler.reconcile_durable_results()
        # A previous owner may have reached verified Home after a canonical
        # RTDE connection reset but before appending the continuation row.  On
        # same-state resume, repair only the immutable Home-only evidence seam
        # before treating the dispatch as ambiguous.
        repaired_receipts = self._repair_recoverable_home_only_dispatches(
            identity=identity,
            reconciler=reconciler,
            execution=execution,
        )
        if repaired_receipts:
            # The old-epoch campaign above was only a recovery writer.  Open
            # the next plan under the fresh resident epoch before the
            # ambiguous check and before any new owner motion.
            campaign, physical, optimizer, execution = self._campaign_components(
                identity=identity,
                session_epoch=epoch,
            )
            reconciler = V5PhysicalCampaignRunnerV1(
                campaign=campaign,
                owner=_ReconcileOnlyOwner(),
                physical_ledger=physical,
                optimizer_journal=optimizer,
                execution_journal=execution,
            )
        for durable_result in execution.results:
            self._submit_post_home_sidecars(durable_result)
        if execution.ambiguous_chain_ids:
            ambiguous = ",".join(execution.ambiguous_chain_ids)
            try:
                self._release_resident(identity=identity, phase_path=phase_path)
            except BaseException as release_error:
                raise V5ProductionBundleError(
                    "ambiguous physical dispatch and exact resident release failed: "
                    f"chains={ambiguous}; release={release_error}"
                ) from release_error
            raise V5AmbiguousPhysicalDispatch(
                "motion dispatch has no owner-sealed result; resident is stopped at Home: "
                + ambiguous
            )
        if (Path(identity.state_root) / "report.json").is_file():
            report = campaign.report()
            release = self._release_resident(identity=identity, phase_path=phase_path)
            self._archive_role_raw(identity=identity, physical=physical)
            return report, physical, release
        context = build_v5_live_context(
            run_dir=run_dir,
            controller_host=self.inputs.robot_host,
            kunwei_host=self.inputs.kunwei_host,
            kunwei_port=int(self.inputs.kunwei_port),
            launch_profile=run_dir / "figure8_launch_profile.json",
            expected_campaign_fingerprint=identity.campaign_fingerprint,
            expected_release_identity_sha256=identity.release_identity_sha256,
            expected_role=self._ledger_role(identity.role),
            timing_scheduler_profile=self.inputs.timing_scheduler_profile,
        )
        phase = _read_json(phase_path, f"{identity.role.value} phase runtime")
        _atomic_json(phase_path, {**phase, "status": "owner_open"})
        error: BaseException | None = None
        report: CampaignReportV2 | None = None
        runner: V5PhysicalCampaignRunnerV1 | None = None
        try:
            runner = V5PhysicalCampaignRunnerV1(
                campaign=campaign,
                owner=V5SingleWriterOwnerV1(context),
                physical_ledger=physical,
                optimizer_journal=optimizer,
                execution_journal=execution,
            )
            while True:
                result = runner.run_next_chain()
                if result is not None:
                    self._submit_post_home_sidecars(result)
                snapshot = campaign.snapshot()
                _atomic_json(
                    phase_path,
                    {
                        **phase,
                        "status": "owner_open",
                        "session_epoch": epoch,
                        "campaign_snapshot": snapshot,
                        "physical_ledger_head_sha256": physical.head_sha256,
                        "optimizer_journal_head_sha256": optimizer.head_sha256,
                        "execution_journal_head_sha256": execution.head_sha256,
                    },
                )
                if result is None:
                    report = campaign.report()
                    break
        except BaseException as exc:
            error = exc
        cleanup_error: BaseException | None = None
        try:
            context.close()
            self._release_resident(
                identity=identity,
                phase_path=phase_path,
            )
        except BaseException as exc:
            cleanup_error = exc
        if error is not None:
            if cleanup_error is not None:
                raise V5ProductionBundleError(
                    f"{identity.role.value} failed and cleanup also failed: "
                    f"run={error}; cleanup={cleanup_error}"
                ) from error
            # The runner's generic V5LiveOwnerError is deliberately kept
            # ambiguous until cleanup has supplied the independent release
            # proof.  Only then may this exact canonical reset be converted
            # into the typed continuation marker consumed by the next run.
            if (
                self.entry_mode == ENTRY_MODE_HOME_ONLY_V1
                and not isinstance(error, V5RecoverableOwnerFailure)
                and self._is_canonical_connection_reset(error)
                and runner is not None
            ):
                receipts = self._repair_recoverable_home_only_dispatches(
                    identity=identity,
                    reconciler=runner,
                    execution=execution,
                )
                if receipts:
                    self._publish_recoverable_continuation_marker(
                        identity=identity,
                        phase_path=phase_path,
                        receipts=receipts,
                    )
                    raise V5RecoverableOwnerFailure(receipts[0]) from error
            raise error
        if cleanup_error is not None:
            raise cleanup_error
        if report is None:
            raise V5ProductionBundleError("V5 role exited without closeout")
        release_receipt = _read_json(
            self.state_root
            / identity.role.value.lower()
            / "writer_release_receipt.json",
            f"{identity.role.value} writer release",
        )
        self._archive_role_raw(identity=identity, physical=physical)
        return report, physical, release_receipt

    def _primary_identity(self) -> CampaignIdentityV2:
        return self._role_identity(
            role=CampaignRoleV2.PRIMARY,
            fingerprint=self.material.primary_fingerprint,
        )

    def _correction_identity(
        self,
        *,
        primary_report: CampaignReportV2,
        primary_physical: V5PhysicalAdmissionLedgerV2,
    ) -> CampaignIdentityV2:
        if primary_report.winner_candidate is None:
            raise V5ProductionBundleError("PRIMARY closeout has no physical winner")
        controller = dict(primary_report.winner_candidate["controller_path"])
        closeout_sha = canonical_sha256(primary_report.as_dict())
        parent = {
            "primary_campaign_fingerprint": self.material.primary_fingerprint,
            "primary_closeout_sha256": closeout_sha,
            "primary_physical_ledger_head_sha256": primary_physical.head_sha256,
            "primary_winner_controller_sha256": _controller_hash(controller),
        }
        fingerprint = derive_v5_campaign_fingerprint(
            role=CampaignRoleV2.CORRECTION,
            release_identity_sha256=str(
                self.material.release["release_identity_sha256"]
            ),
            home_calibration_receipt_sha256=str(
                self.material.home_calibration["receipt_sha256"]
            ),
            parent=parent,
        )
        receipt = {
            "schema": "step6.autotune/autotuner-v5-correction-parent-v1",
            "version": 1,
            "correction_campaign_fingerprint": fingerprint,
            "fixed_controller_path": controller,
            **parent,
        }
        _atomic_json(
            self.state_root / "correction_parent.json",
            {**receipt, "receipt_sha256": canonical_sha256(receipt)},
        )
        return self._role_identity(
            role=CampaignRoleV2.CORRECTION,
            fingerprint=fingerprint,
            fixed_controller_path=controller,
            primary_closeout_sha256=closeout_sha,
            primary_ledger_head_sha256=primary_physical.head_sha256,
        )

    def status(self) -> dict[str, Any]:
        roles: dict[str, Any] = {}
        for role in CampaignRoleV2:
            phase = self._phase_runtime_path(role)
            roles[role.value] = (
                None
                if not phase.is_file()
                else _read_json(phase, f"{role.value} phase runtime")
            )
        final_path = self.state_root / "final_receipt.json"
        return {
            "schema": V5_BUNDLE_SCHEMA,
            "version": V5_BUNDLE_VERSION,
            "state_root": str(self.state_root),
            "release_identity_sha256": self.material.release[
                "release_identity_sha256"
            ],
            "primary_campaign_fingerprint": self.material.primary_fingerprint,
            "roles": roles,
            "final_receipt": (
                None
                if not final_path.is_file()
                else _read_json(final_path, "V5 final receipt")
            ),
            "sidecars": dict(self._sidecar_snapshot()),
            "capability_acceptance": (
                None
                if not self.capability.receipt_path.is_file()
                else self.capability.verify_receipt()
            ),
        }

    def run(self) -> dict[str, Any]:
        capability_receipt = self.capability.run()
        primary_identity = self._primary_identity()
        primary_report, primary_physical, primary_release = self._run_role(
            identity=primary_identity
        )
        correction_identity = self._correction_identity(
            primary_report=primary_report,
            primary_physical=primary_physical,
        )
        correction_report, correction_physical, correction_release = self._run_role(
            identity=correction_identity
        )
        sidecar_snapshot = dict(self._sidecar_snapshot())
        evidence_report = build_v5_evidence_report(
            output_root=self.state_root / "reports" / "final",
            release_identity=self.material.release,
            primary_report=primary_report,
            correction_report=correction_report,
            primary_physical=primary_physical,
            correction_physical=correction_physical,
            primary_writer_release=primary_release,
            correction_writer_release=correction_release,
            capability_acceptance=capability_receipt,
            sidecars=sidecar_snapshot,
        )
        body = {
            "schema": V5_FINAL_RECEIPT_SCHEMA,
            "version": V5_BUNDLE_VERSION,
            "release_identity_sha256": self.material.release[
                "release_identity_sha256"
            ],
            "primary_campaign_fingerprint": primary_identity.campaign_fingerprint,
            "correction_campaign_fingerprint": correction_identity.campaign_fingerprint,
            "primary_report": primary_report.as_dict(),
            "correction_report": correction_report.as_dict(),
            "primary_physical_ledger_head_sha256": primary_physical.head_sha256,
            "correction_physical_ledger_head_sha256": correction_physical.head_sha256,
            "primary_writer_release": dict(primary_release),
            "correction_writer_release": dict(correction_release),
            "final_home": True,
            "final_stopped": True,
            "writer_released": True,
            "automatic_promotion": False,
            "capability_acceptance": capability_receipt,
            "sidecars": sidecar_snapshot,
            "evidence_report": evidence_report,
        }
        receipt = {**body, "receipt_sha256": canonical_sha256(body)}
        _atomic_json(self.state_root / "final_receipt.json", receipt)
        return receipt

    def run_primary_only(self) -> dict[str, Any]:
        """Run the Home-only Primary namespace and never open CORRECTION."""

        if self.entry_mode != ENTRY_MODE_HOME_ONLY_V1:
            raise V5ProductionBundleError(
                "primary-only execution requires the explicit HOME_ONLY_V1 config"
            )
        primary_identity = self._primary_identity()
        primary_report, primary_physical, primary_release = self._run_role(
            identity=primary_identity
        )
        body = {
            "schema": "step6.autotune/autotuner-v5-primary-only-receipt-v1",
            "version": 1,
            "entry_mode": self.entry_mode,
            "release_identity_sha256": self.material.release[
                "release_identity_sha256"
            ],
            "primary_campaign_fingerprint": primary_identity.campaign_fingerprint,
            "primary_report": primary_report.as_dict(),
            "primary_physical_ledger_head_sha256": primary_physical.head_sha256,
            "primary_writer_release": dict(primary_release),
            "correction_started": False,
            "correction_receipt": None,
            "capability_acceptance": {
                "status": "not_applicable_home_only",
                "rollover_rows_imported": 0,
                "equivalence_gate_used": False,
            },
            "final_home": True,
            "final_stopped": True,
            "writer_released": True,
            "automatic_promotion": False,
        }
        receipt = {**body, "receipt_sha256": canonical_sha256(body)}
        _atomic_json(self.state_root / "primary_only_receipt.json", receipt)
        return receipt

    def run_extension_epoch(self, epoch: V5ExtensionEpochV1) -> dict[str, Any]:
        """Run one Home-only extension epoch and seal its parent binding."""

        if self.material.primary_fingerprint != epoch.fingerprint:
            raise V5ProductionBundleError(
                "extension bundle fingerprint differs from epoch identity"
            )
        primary = self.run_primary_only()
        top = primary.get("primary_report", {}).get("top_candidates", ())
        if not isinstance(top, Sequence) or len(top) != 3 or any(
            item.get("total_n") != 5 for item in top if isinstance(item, Mapping)
        ):
            raise V5ProductionBundleError("extension Primary closeout lacks top-three n=5")
        closeout = epoch.closeout(
            physical_ledger_head_sha256=str(
                primary["primary_physical_ledger_head_sha256"]
            ),
            exact_novel_count=int(primary["primary_report"]["exact_novel_count"]),
            top3_total_n=5,
        )
        body = {
            "schema": "step6.autotune/figure8-v5-extension-run-receipt-v1",
            "version": 1,
            "epoch": epoch.inputs.epoch,
            "campaign_fingerprint_sha256": epoch.fingerprint,
            "primary_only_receipt": primary,
            "epoch_closeout": closeout,
            "final_home": True,
            "final_stopped": True,
            "writer_released": True,
        }
        receipt = {**body, "receipt_sha256": canonical_sha256(body)}
        _atomic_json(self.state_root / "extension_run_receipt.json", receipt)
        return receipt


__all__ = [
    "V5_BUNDLE_SCHEMA",
    "V5_BUNDLE_VERSION",
    "V5BundleMaterialV1",
    "V5ProductionBundleError",
    "V5ProductionBundleInputsV1",
    "V5ProductionBundleV1",
    "derive_v5_campaign_fingerprint",
    "derive_v5_release_identity",
]
