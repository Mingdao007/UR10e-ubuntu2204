#!/usr/bin/env python3
"""Backend seam for Step5d-native autotune without implicit live execution."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from jsonschema import Draft202012Validator

from build_step5d_autotune_tp import render_script as render_autotune_tp_script

from step5d_autotune_contract import (
    CAMPAIGN_STAGE_ID,
    SOURCE_STAGE_ID,
    CaptureManifest,
    Evaluation,
    ForceCandidate,
    SearchAttestation,
    TrialSpec,
    sha256_json,
)
from step5d_autotune_evaluator import evaluate_csv
from step5d_autotune_replay import (
    build_candidate_bound_search_attestation,
    verify_candidate_bound_search_attestation,
)
from step5d_autotune_supervisor import execution_profile_integer_id
from step5d_workflow_state import WorkflowStateError, resolve_artifacts, verify_current
from ur10e_artifact_store import artifact_store


BACKEND_ID = "step5d_v35_native_backend_v1"


class LiveAuthorizationRequired(RuntimeError):
    pass


@dataclass(frozen=True)
class FrozenFingerprint:
    backend_id: str
    git_commit: str
    source_fingerprint: str
    config_fingerprint: str
    composite_fingerprint: str
    source_files: Mapping[str, str]
    config_files: Mapping[str, str]
    v35_package_sha256: Mapping[str, str]
    controller_readback_manifest: str
    controller_readback_manifest_sha256: str
    rnn_contract: Mapping[str, Any]
    force_frame_contract_sha256: str


@dataclass(frozen=True)
class BackendPreflight:
    ok: bool
    offline_only: bool
    source_stage_current: bool
    controller_readback_verified: bool
    controller_readback_sha_closed: bool
    cuda_available: bool
    live_authorized: bool
    blockers: tuple[str, ...]
    evidence: Mapping[str, Any]


@dataclass(frozen=True)
class PreparedTrial:
    trial: TrialSpec
    frozen: FrozenFingerprint
    environment: Mapping[str, str]
    runner_arguments: tuple[str, ...]


@dataclass(frozen=True)
class CampaignAuthorization:
    campaign_id: str
    campaign_fingerprint: str
    bounded_baseline_and_loop: bool
    live_authorized: bool
    controller_readback_verified: bool


class AutotuneBackend(Protocol):
    def freeze_fingerprint(self) -> FrozenFingerprint:
        ...

    def preflight(self, *, offline: bool, authorization: CampaignAuthorization | None = None) -> BackendPreflight:
        ...

    def prepare_trial(self, trial: TrialSpec, frozen: FrozenFingerprint) -> PreparedTrial:
        ...

    def execute_trial(
        self,
        prepared: PreparedTrial,
        *,
        authorization: CampaignAuthorization,
        runner: Callable[[PreparedTrial], Path],
    ) -> Path:
        ...

    def evaluate_trial(
        self,
        trial: TrialSpec,
        manifest: CaptureManifest,
        csv_path: Path,
    ) -> Evaluation:
        ...

    def diagnostic_jobs(self, prepared: PreparedTrial) -> Sequence[Mapping[str, Any]]:
        ...


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


class Step5dV35Backend:
    """Bind exact-v35 bytes while exposing a separate autotune campaign identity."""

    SOURCE_PATHS = (
        "programs/step5/step5d/step5d_strict_rnn_ablation_v35.script",
        "tools/build_step5d_autotune_tp.py",
        "tools/kunwei_rtde_bridge.py",
        "tools/step5d_autotune_backend.py",
        "tools/step5d_autotune_contract.py",
        "tools/step5d_autotune_evaluator.py",
        "tools/step5d_autotune_governor.py",
        "tools/step5d_autotune_journal.py",
        "tools/step5d_autotune_live_driver.py",
        "tools/step5d_autotune_optimizer.py",
        "tools/step5d_autotune_replay.py",
        "tools/step5d_autotune_state_machine.py",
        "tools/step5d_autotune_store.py",
        "tools/step5d_autotune_supervisor.py",
        "tools/step5d_autotune_coordinator.py",
        "tools/run_step5d_autotune_campaign.py",
        "tools/step5d_paper_outer_loop.py",
        "tools/step5d_control_contract.py",
        "tools/step5c_strict_rnn.py",
        "tools/step5d_runtime_interface.py",
        "tools/step5d_workflow_state.py",
        "tools/ur10e_artifact_store.py",
    )
    CONFIG_PATHS = (
        "config/step5d_autotune_campaign_v1.json",
        "config/schemas/step5d_autotune_campaign_v1.schema.json",
        "config/reviews/step5d_autotune_campaign_launcher_fable5_unavailable.json",
        "config/step5_stage_table.json",
        "config/current_stage.json",
        "config/step5_safe_frame.json",
        "config/step_pose_contract_table.json",
        "config/step5d/current.json",
        "config/step5d/artifact_locators/step5d_v35_retained_inputs.json",
        "config/step5d/manifests/step5d_strict_rnn_ablation_v35/candidate.json",
        "config/step5d/manifests/step5d_strict_rnn_ablation_v35/controller_verification.json",
        "config/ur10e_test_dependency_map_v1.json",
        "UR_FORCE_FRAME_CONTRACT.md",
    )

    def __init__(self, experiment_root: Path) -> None:
        self.root = experiment_root.resolve()

    def _git_commit(self) -> str:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    def _stage_row(self, stage_id: str) -> dict[str, Any]:
        table = _json(self.root / "config" / "step5_stage_table.json")
        for row in table.get("stages", []):
            if isinstance(row, dict) and row.get("id") == stage_id:
                return row
        raise ValueError(f"missing stage table row: {stage_id}")

    def _validate_campaign_source_contract(self) -> None:
        config = _json(self.root / "config" / "step5d_autotune_campaign_v1.json")
        schema = _json(
            self.root / "config" / "schemas" / "step5d_autotune_campaign_v1.schema.json"
        )
        Draft202012Validator.check_schema(schema)
        errors = sorted(
            Draft202012Validator(schema).iter_errors(config),
            key=lambda error: tuple(str(item) for item in error.absolute_path),
        )
        if errors:
            detail = "; ".join(
                f"{'/'.join(str(item) for item in error.absolute_path) or '<root>'}: "
                f"{error.message}"
                for error in errors[:8]
            )
            raise ValueError(f"Step5d autotune campaign schema validation failed: {detail}")

    def _campaign_readback_closure(
        self,
        delivery: Mapping[str, Any],
    ) -> tuple[bool, dict[str, Any]]:
        evidence: dict[str, Any] = {"campaign_readback_sha_closed": False}
        required_flags = (
            delivery.get("controller_uploaded") is True
            and delivery.get("controller_readback_verified") is True
            and delivery.get("status")
            in {"controller_readback_verified", "controller_readback_verified_current"}
        )
        if not required_flags:
            evidence["campaign_readback_failure"] = "delivery_flags_not_verified"
            return False, evidence
        locator = delivery.get("artifact_locator")
        if not isinstance(locator, Mapping) or set(locator) != {"path", "sha256"}:
            evidence["campaign_readback_failure"] = "artifact_locator_missing_or_invalid"
            return False, evidence
        locator_path = self.root / str(locator["path"])
        try:
            if _sha256_file(locator_path) != str(locator["sha256"]):
                raise ValueError("artifact locator digest mismatch")
            artifacts = resolve_artifacts(
                root=self.root,
                locator_path=locator_path,
                store=artifact_store(self.root),
            )
            required_roles = {
                "controller_readback_manifest",
                "controller_readback_script",
                "controller_readback_txt",
                "controller_readback_urp",
            }
            if not required_roles.issubset(artifacts):
                raise ValueError("campaign artifact locator lacks readback closure")
            manifest_path = artifacts["controller_readback_manifest"]
            manifest_sha = _sha256_file(manifest_path)
            if manifest_sha != delivery.get("controller_readback_manifest_sha256"):
                raise ValueError("campaign readback manifest digest differs from stage table")
            manifest = _json(manifest_path)
            readback_sha = (manifest.get("sha256") or {}).get("readback")
            actual_sha = {
                ".script": _sha256_file(artifacts["controller_readback_script"]),
                ".txt": _sha256_file(artifacts["controller_readback_txt"]),
                ".urp": _sha256_file(artifacts["controller_readback_urp"]),
            }
            if readback_sha != actual_sha or delivery.get("sha256") != actual_sha:
                raise ValueError("campaign readback manifest/blob SHA triplet is not closed")
            if (
                manifest.get("fresh_controller_sha_verified") is not True
                or manifest.get("readback_source") != "fresh_controller_get"
                or (manifest.get("validation") or {}).get("program") != CAMPAIGN_STAGE_ID
            ):
                raise ValueError("campaign readback is not a fresh exact-package capture")
        except (OSError, ValueError, WorkflowStateError) as exc:
            evidence["campaign_readback_failure"] = f"{type(exc).__name__}:{exc}"
            return False, evidence
        evidence.update(
            {
                "campaign_readback_sha_closed": True,
                "campaign_readback_manifest_sha256": manifest_sha,
                "campaign_package_sha256": actual_sha,
            }
        )
        return True, evidence

    def freeze_fingerprint(self) -> FrozenFingerprint:
        self._validate_campaign_source_contract()
        source_hashes = {path: _sha256_file(self.root / path) for path in self.SOURCE_PATHS}
        config_hashes = {path: _sha256_file(self.root / path) for path in self.CONFIG_PATHS}
        workflow = verify_current(root=self.root, store=artifact_store(self.root))
        if workflow.get("program") not in {SOURCE_STAGE_ID, CAMPAIGN_STAGE_ID} or not workflow.get(
            "controller_verified"
        ):
            raise ValueError(
                "current Step5d binding must be controller-verified v35 or autotune"
            )
        row = self._stage_row(SOURCE_STAGE_ID)
        campaign_row = self._stage_row(CAMPAIGN_STAGE_ID)
        outer = row.get("stage25_outer_profile", {})
        runtime = row.get("runtime_profile", {})
        if not isinstance(outer, dict) or not isinstance(runtime, dict):
            raise ValueError("v35 stage row lacks runtime/outer profile")
        if float(outer.get("tangential_kp", 0.0)) != 1.5:
            raise ValueError("Step5dOuterLoopConfig.kp must remain tangential gain 1.5")
        expected_runtime = {
            "backend": "cupy",
            "inner_iterations": 512,
            "epsilon": 0.01,
            "sigr_exponent_r": 0.8,
            "qdot_cap_rad_s": 0.5,
            "command_slew_rad_s2": 0.1,
            "tp_speedj_acceleration_rad_s2": 0.1,
            "control_mode": "speedj_rnn_live",
        }
        for key, expected in expected_runtime.items():
            if runtime.get(key) != expected:
                raise ValueError(f"v35 runtime contract mismatch for {key}: {runtime.get(key)!r}")
        promoted = _json(self.root / "config" / "current_stage.json")
        campaign_is_current = promoted.get("program") == CAMPAIGN_STAGE_ID
        source_binding = campaign_row.get("source_binding") or {}
        rendered_script = render_autotune_tp_script()
        rendered_script_bytes = rendered_script.encode("utf-8")
        rendered_script_sha256 = hashlib.sha256(rendered_script_bytes).hexdigest()
        if (
            row.get("active") is campaign_is_current
            or (row.get("current_binding") or {}).get("is_current") is campaign_is_current
            or campaign_row.get("active") is not campaign_is_current
            or (campaign_row.get("current_binding") or {}).get("is_current")
            is not campaign_is_current
            or source_binding.get("stage_id") != SOURCE_STAGE_ID
            or source_binding.get("program_source_sha256")
            != source_hashes[
                "programs/step5/step5d/step5d_strict_rnn_ablation_v35.script"
            ]
            or source_binding.get("autotune_tp_builder")
            != "tools/build_step5d_autotune_tp.py"
            or source_binding.get("autotune_tp_builder_sha256")
            != source_hashes["tools/build_step5d_autotune_tp.py"]
            or source_binding.get("autotune_rendered_script_sha256")
            != rendered_script_sha256
            or source_binding.get("autotune_rendered_script_bytes")
            != len(rendered_script_bytes)
        ):
            raise ValueError("autotune stage current/active/source binding is inconsistent")
        retained = resolve_artifacts(
            root=self.root,
            locator_path=(
                self.root
                / "config"
                / "step5d"
                / "artifact_locators"
                / "step5d_v35_retained_inputs.json"
            ),
            store=artifact_store(self.root),
        )
        required_roles = {
            "controller_readback_manifest",
            "controller_readback_script",
            "controller_readback_txt",
            "controller_readback_urp",
        }
        if not required_roles.issubset(retained):
            raise ValueError("canonical v35 retained artifact closure is incomplete")
        readback_path = Path(retained["controller_readback_manifest"])
        readback_sha = _sha256_file(readback_path)
        readback = _json(readback_path)
        package_sha = (readback.get("sha256") or {}).get("readback")
        if not isinstance(package_sha, dict) or set(package_sha) != {".script", ".txt", ".urp"}:
            raise ValueError("v35 package SHA triplet is incomplete")
        retained_sha = {
            extension: _sha256_file(
                Path(retained[f"controller_readback_{extension[1:]}"])
            )
            for extension in (".script", ".txt", ".urp")
        }
        if package_sha != retained_sha:
            raise ValueError("v35 read-back manifest differs from canonical retained bytes")
        source_fingerprint = sha256_json(source_hashes)
        config_fingerprint = sha256_json(config_hashes)
        force_frame_sha = config_hashes["UR_FORCE_FRAME_CONTRACT.md"]
        git_commit = self._git_commit()
        composite = sha256_json(
            {
                "backend_id": BACKEND_ID,
                "git_commit": git_commit,
                "source": source_fingerprint,
                "config": config_fingerprint,
                "package": package_sha,
                "readback": readback_sha,
                "rnn": expected_runtime,
                "campaign_stage": CAMPAIGN_STAGE_ID,
            }
        )
        return FrozenFingerprint(
            backend_id=BACKEND_ID,
            git_commit=git_commit,
            source_fingerprint=source_fingerprint,
            config_fingerprint=config_fingerprint,
            composite_fingerprint=composite,
            source_files=source_hashes,
            config_files=config_hashes,
            v35_package_sha256={str(key): str(value) for key, value in package_sha.items()},
            controller_readback_manifest=str(readback_path),
            controller_readback_manifest_sha256=readback_sha,
            rnn_contract=expected_runtime,
            force_frame_contract_sha256=force_frame_sha,
        )

    def preflight(
        self,
        *,
        offline: bool,
        authorization: CampaignAuthorization | None = None,
    ) -> BackendPreflight:
        blockers: list[str] = []
        evidence: dict[str, Any] = {}
        frozen: FrozenFingerprint | None = None
        try:
            frozen = self.freeze_fingerprint()
            evidence["composite_fingerprint"] = frozen.composite_fingerprint
            evidence["baseline_controller_readback_manifest_sha256"] = (
                frozen.controller_readback_manifest_sha256
            )
            source_current = True
            evidence["baseline_controller_readback_sha_closed"] = True
        except (
            OSError,
            ValueError,
            WorkflowStateError,
            subprocess.CalledProcessError,
        ) as exc:
            source_current = False
            evidence["baseline_controller_readback_sha_closed"] = False
            blockers.append(f"fingerprint_freeze_failed:{type(exc).__name__}:{exc}")
        try:
            campaign_row = self._stage_row(CAMPAIGN_STAGE_ID)
            campaign_delivery = campaign_row.get("package_delivery", {})
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            campaign_row = {}
            campaign_delivery = {}
            blockers.append(
                f"campaign_stage_delivery_unreadable:{type(exc).__name__}:{exc}"
            )
        readback_verified, readback_evidence = self._campaign_readback_closure(
            campaign_delivery if isinstance(campaign_delivery, Mapping) else {}
        )
        evidence.update(readback_evidence)
        evidence["campaign_controller_readback_verified"] = readback_verified
        try:
            current = _json(self.root / "config" / "current_stage.json")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            current = {}
            blockers.append(f"current_stage_unreadable:{type(exc).__name__}:{exc}")
        campaign_current_active = bool(
            current.get("program") == CAMPAIGN_STAGE_ID
            and isinstance(campaign_row, Mapping)
            and campaign_row.get("active") is True
            and (campaign_row.get("current_binding") or {}).get("is_current") is True
        )
        evidence["campaign_current_active"] = campaign_current_active
        cuda_available = False
        try:
            import cupy

            cupy_devices = int(cupy.cuda.runtime.getDeviceCount())
            evidence["cupy_version"] = cupy.__version__
            evidence["cupy_device_count"] = cupy_devices
            cuda_available = cupy_devices > 0
            if cuda_available:
                properties = cupy.cuda.runtime.getDeviceProperties(0)
                name = properties.get("name", "unknown")
                evidence["gpu_name"] = (
                    name.decode("utf-8", errors="replace")
                    if isinstance(name, bytes)
                    else str(name)
                )
        except (ImportError, RuntimeError):
            evidence["cupy_device_count"] = 0
        try:
            import torch

            evidence["torch_version"] = torch.__version__
            evidence["torch_cuda_available"] = bool(torch.cuda.is_available())
        except ImportError:
            evidence["torch_cuda_available"] = False
        live_authorized = bool(
            authorization
            and authorization.live_authorized
            and authorization.controller_readback_verified
            and authorization.bounded_baseline_and_loop
            and readback_verified
            and campaign_current_active
            and evidence.get("composite_fingerprint") == authorization.campaign_fingerprint
        )
        if not offline and not cuda_available:
            blockers.append("cuda_required_for_live_no_cpu_fallback")
        if not offline and not live_authorized:
            blockers.append("bounded_campaign_live_authorization_missing_or_mismatched")
        if not offline and not readback_verified:
            blockers.append("autotune_controller_delivery_and_fresh_readback_required")
        if not offline and not campaign_current_active:
            blockers.append("autotune_campaign_must_be_current_and_active")
        return BackendPreflight(
            ok=not blockers,
            offline_only=offline,
            source_stage_current=source_current,
            controller_readback_verified=readback_verified,
            controller_readback_sha_closed=readback_verified,
            cuda_available=cuda_available,
            live_authorized=live_authorized,
            blockers=tuple(blockers),
            evidence=evidence,
        )

    def prepare_trial(self, trial: TrialSpec, frozen: FrozenFingerprint) -> PreparedTrial:
        if trial.backend_id != BACKEND_ID or frozen.backend_id != BACKEND_ID:
            raise ValueError("backend identity mismatch")
        if trial.source_fingerprint != frozen.source_fingerprint:
            raise ValueError("trial source fingerprint does not match frozen backend")
        if trial.config_fingerprint != frozen.config_fingerprint:
            raise ValueError("trial config fingerprint does not match frozen backend")
        if trial.campaign.campaign_fingerprint != frozen.composite_fingerprint:
            raise ValueError("campaign fingerprint does not match frozen backend")
        if trial.campaign.f0_shadow_reaction_normal_base is None:
            raise ValueError("campaign F0 shadow reaction normal must be frozen before trial preparation")
        if trial.search_attestation is not None:
            verify_candidate_bound_search_attestation(
                trial.search_attestation,
                root=self.root,
                execution_profile=trial.execution_profile,
            )
        profile = trial.execution_profile
        candidate = trial.candidate
        environment = {
            "BRIDGE_PROFILE": CAMPAIGN_STAGE_ID,
            "BRIDGE_TARGET_FORCE_N": "12.0",
            "STEP5D_AUTOTUNE_FORCE_P": f"{candidate.force_p_gain:.12g}",
            "STEP5D_AUTOTUNE_FORCE_I": f"{candidate.force_i_gain:.12g}",
            "STEP5D_AUTOTUNE_FORCE_DAMPING": f"{candidate.force_damping:.12g}",
            "BRIDGE_NORMAL_FILTER_TAU_S": f"{profile.normal_filter_tau_s:.12g}",
            "STEP5D_AUTOTUNE_NORMAL_RATE_RAD_S": f"{profile.normal_max_rate_rad_s:.12g}",
            "STEP5D_QDOT_LIMIT_RAD_S": "0.5",
            "STEP5D_AUTOTUNE_HOST_SLEW_RAD_S2": f"{profile.host_qdot_slew_rad_s2:.12g}",
            "STEP5D_AUTOTUNE_SPEEDJ_ACCELERATION_RAD_S2": f"{profile.tp_speedj_accel_rad_s2:.12g}",
            "STEP5D_AUTOTUNE_CAMPAIGN_EPOCH": str(trial.campaign.campaign_epoch),
            "STEP5D_AUTOTUNE_TRIAL_ID": str(trial.trial_id),
            "STEP5D_AUTOTUNE_COMMAND": "1",
            "STEP5D_AUTOTUNE_TRIAL_UID": trial.trial_uid,
            "STEP5D_AUTOTUNE_CANDIDATE_TOKEN": str(trial.candidate_token),
            "STEP5D_AUTOTUNE_EXECUTION_PROFILE_ID": str(
                execution_profile_integer_id(profile)
            ),
            "STEP5D_AUTOTUNE_COMMAND_SEQ": str(trial.command_seq),
        }
        if trial.search_attestation is not None:
            environment.update(
                {
                    "STEP5D_AUTOTUNE_SEARCH_ATTESTATION_UID": (
                        trial.search_attestation.attestation_uid
                    ),
                    "STEP5D_AUTOTUNE_REPLAY_SOURCE_TRIAL_UID": (
                        trial.search_attestation.source_trial_uid
                    ),
                    "STEP5D_AUTOTUNE_REPLAY_TRACE_SHA256": (
                        trial.search_attestation.latest_trace_sha256
                    ),
                    "STEP5D_AUTOTUNE_REPLAY_REPORT_SHA256": (
                        trial.search_attestation.replay_evidence.report_artifact_ref.sha256
                    ),
                }
            )
        if "BRIDGE_NORMAL_FILTER_ALPHA" in environment:
            raise AssertionError("normal_filter_alpha must not enter Step5d autotune runtime")
        arguments = (
            "--bridge-profile",
            CAMPAIGN_STAGE_ID,
            "--step5d-autotune-force-p",
            environment["STEP5D_AUTOTUNE_FORCE_P"],
            "--step5d-autotune-force-i",
            environment["STEP5D_AUTOTUNE_FORCE_I"],
            "--step5d-autotune-force-damping",
            environment["STEP5D_AUTOTUNE_FORCE_DAMPING"],
            "--bridge-normal-filter-tau-s",
            environment["BRIDGE_NORMAL_FILTER_TAU_S"],
            "--step5d-autotune-normal-rate-rad-s",
            environment["STEP5D_AUTOTUNE_NORMAL_RATE_RAD_S"],
            "--step5d-qdot-limit-rad-s",
            environment["STEP5D_QDOT_LIMIT_RAD_S"],
            "--step5d-autotune-host-slew-rad-s2",
            environment["STEP5D_AUTOTUNE_HOST_SLEW_RAD_S2"],
            "--step5d-autotune-speedj-acceleration-rad-s2",
            environment["STEP5D_AUTOTUNE_SPEEDJ_ACCELERATION_RAD_S2"],
            "--step5d-autotune-campaign-epoch",
            str(trial.campaign.campaign_epoch),
            "--step5d-autotune-trial-id",
            str(trial.trial_id),
            "--step5d-autotune-command",
            "1",
            "--step5d-autotune-candidate-token",
            str(trial.candidate_token),
            "--step5d-autotune-execution-profile-id",
            str(execution_profile_integer_id(profile)),
            "--step5d-autotune-command-sequence",
            str(trial.command_seq),
        )
        if not profile.live_eligible:
            arguments = (*arguments, "--step5d-autotune-offline-only-profile")
        return PreparedTrial(trial=trial, frozen=frozen, environment=environment, runner_arguments=arguments)

    def execute_trial(
        self,
        prepared: PreparedTrial,
        *,
        authorization: CampaignAuthorization,
        runner: Callable[[PreparedTrial], Path],
    ) -> Path:
        preflight = self.preflight(offline=False, authorization=authorization)
        if not preflight.ok:
            raise LiveAuthorizationRequired(";".join(preflight.blockers))
        if authorization.campaign_id != prepared.trial.campaign.campaign_id:
            raise LiveAuthorizationRequired("campaign authorization id mismatch")
        if not prepared.trial.execution_profile.live_eligible:
            raise LiveAuthorizationRequired("offline_only execution profile cannot run live")
        if (
            authorization.campaign_fingerprint
            != prepared.trial.campaign.campaign_fingerprint
            or prepared.frozen.composite_fingerprint
            != prepared.trial.campaign.campaign_fingerprint
        ):
            raise LiveAuthorizationRequired("campaign authorization fingerprint mismatch")
        if self.freeze_fingerprint() != prepared.frozen:
            raise LiveAuthorizationRequired("backend fingerprint changed after trial preparation")
        return runner(prepared)

    def evaluate_trial(
        self,
        trial: TrialSpec,
        manifest: CaptureManifest,
        csv_path: Path,
    ) -> Evaluation:
        return evaluate_csv(trial, manifest, csv_path)

    def attest_search_candidate(
        self,
        *,
        source_trial: TrialSpec,
        source_manifest: CaptureManifest,
        trace_path: Path,
        to_candidate: ForceCandidate,
        artifact_store_override: Path | None = None,
    ) -> SearchAttestation:
        return build_candidate_bound_search_attestation(
            root=self.root,
            source_trial=source_trial,
            source_manifest=source_manifest,
            trace_path=trace_path,
            to_candidate=to_candidate,
            artifact_store_override=artifact_store_override,
        )

    def diagnostic_jobs(self, prepared: PreparedTrial) -> Sequence[Mapping[str, Any]]:
        jobs: list[Mapping[str, Any]] = [
            {
                "task": "step5d_autotune_contract_tests",
                "resource_lane": "cpu_throughput",
                "claim_class": "diagnostic_only",
                "command": ["python3", "-m", "pytest", "-q", "tests/test_step5d_autotune.py"],
            },
            {
                "task": "step5d_contact_semantic_gate",
                "resource_lane": "cpu_throughput",
                "claim_class": "offline_semantic",
                "command": ["python3", "tools/ur_contact_semantic_gate.py"],
            },
        ]
        proof = prepared.trial.search_attestation
        if proof is not None:
            jobs.append(
                {
                    "task": "step5d_candidate_bound_replay_attestation",
                    "resource_lane": "gpu_functional",
                    "claim_class": "verified_preparation_evidence",
                    "attestation_uid": proof.attestation_uid,
                    "trace_artifact_ref": (
                        proof.replay_evidence.trace_artifact_ref.as_dict()
                    ),
                    "report_artifact_ref": (
                        proof.replay_evidence.report_artifact_ref.as_dict()
                    ),
                }
            )
        return tuple(jobs)

    def frozen_payload(self, frozen: FrozenFingerprint) -> dict[str, Any]:
        return asdict(frozen)


def default_backend(root: Path | None = None) -> Step5dV35Backend:
    return Step5dV35Backend(root or Path(__file__).resolve().parents[1])
