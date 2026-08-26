"""Deterministic source closure for the Figure-eight physical function."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


CONFIG_PATH = Path("config/step6/r013_figure8_direct_campaign_v1.json")
GOVERNANCE_PATHS = (
    Path("STEP6_FLOW.md"),
)
SOURCE_PATHS = (
    Path("tools/step6_figure8_autotune_v1/source_identity.py"),
    Path("tools/step6_figure8_autotune_v1/core.py"),
    Path("tools/step6_figure8_autotune_v1/live_composition.py"),
    Path("tools/step6_figure8_autotune_v1/physical_candidate.py"),
    Path("tools/step6_figure8_autotune_v1/physical_censor.py"),
    Path("tools/step6_figure8_autotune_v1/physical_ledger.py"),
    Path("tools/step6_figure8_autotune_v1/prepare_live.py"),
    Path("tools/step6_figure8_autotune_v1/report.py"),
    Path("tools/step6_figure8_autotune_v1/optimizer_worker.py"),
    Path("tools/step6_figure8_autotune_v1/optimizer_bridge.py"),
    Path("tools/step6_figure8_autotune_v1/v5_design_domain.py"),
    Path("tools/step6_figure8_autotune_v1/v5_kernel_selection.py"),
    Path("tools/step6_figure8_autotune_v1/v4_isaturation_plan.py"),
    Path("tools/step6_figure8_autotune_v1/campaign_runner.py"),
    Path("tools/step6_figure8_autotune_v1/v5_composition_contract.py"),
    Path("tools/step6_figure8_autotune_v1/v5_rollover.py"),
    Path("tools/step6_figure8_autotune_v1/v5_register_transport.py"),
    Path("tools/step6_figure8_autotune_v1/v5_resident_protocol.py"),
    Path("tools/step6_figure8_autotune_v1/v5_live_runtime.py"),
    Path("tools/step6_figure8_autotune_v1/v5_live_owner.py"),
    Path("tools/step6_figure8_autotune_v1/v5_optimizer_journal.py"),
    Path("tools/step6_figure8_autotune_v1/v5_lifecycle_ledger.py"),
    Path("tools/step6_figure8_autotune_v1/v5_campaign.py"),
    Path("tools/step6_figure8_autotune_v1/v5_campaign_runner.py"),
    Path("tools/step6_figure8_autotune_v1/v5_capability_acceptance.py"),
    Path("tools/step6_figure8_autotune_v1/v5_production_bundle.py"),
    Path("tools/step6_figure8_autotune_v1/v5_filter_shadow.py"),
    Path("tools/step6_figure8_autotune_v1/v5_camera_observer.py"),
    Path("tools/step6_figure8_autotune_v1/v5_ros2_observation_mirror.py"),
    Path("tools/step6_figure8_autotune_v1/v5_sidecar_bundle.py"),
    Path("tools/step6_figure8_autotune_v1/v5_extension.py"),
    Path("tools/step6_figure8_autotune_v1/v5_raw_archive.py"),
    Path("tools/step6_figure8_autotune_v1/v5_extension_runner.py"),
    Path("tools/step6_figure8_autotune_v1/v5_report.py"),
    Path("tools/run_autotuner_v5_sidecars.py"),
    Path("tools/run_step6_figure8_autotune_v1_live.py"),
    Path("tools/build_autotuner_combined_report.py"),
    Path("tools/run_step6_figure8_no_contact_canary_v1_live.py"),
    Path("tools/build_step6_figure8_autotune_v1.py"),
    Path("tools/build_step6_figure8_no_contact_canary_v1.py"),
    Path("tools/step5d_managed_runtime.py"),
    Path("tools/step5d_optimizer_runtime.py"),
    Path("tools/step5d_autotune_v3/runtime_installation.py"),
    Path("tools/step5d_autotune_v3/runtime_environment.py"),
    Path("tools/step5d_autotune_v4_r013/live_owner.py"),
    Path("tools/step5d_autotune_v4_r013/live_runtime.py"),
    Path("tools/step5d_autotune_v4_r013/handoff.py"),
    Path("tools/step5d_autotune_v4_r013/metrics.py"),
    Path("tools/step5d_autotune_v4_r013/path_context.py"),
    Path("tools/step5d_autotune_v4_r013/lifecycle_trace.py"),
    Path("tools/step5d_autotune_v4_r013/floor_coordinator.py"),
    Path("tools/step5d_autotune_v4_r013/v4_two_stage_campaign.py"),
    Path("tools/step5d_autotune_v4_r013/v4_stage_optimizer.py"),
    Path("tools/step5d_autotune_v4_r013/v4_stage_optimizer_worker.py"),
    Path("tools/step5d_autotune_v4_r013/v4_stage_live_adapter.py"),
    Path("tools/step5d_autotune_v4_r013/v4_stage_censor.py"),
    Path("tools/step5d_autotune_v4_r013/recovery.py"),
    Path("tools/run_v4_two_stage_campaign.py"),
    Path("tools/run_v4_stage_live.py"),
    Path("tools/run_v4_stage_recovery_supervisor.py"),
    Path("tools/run_v4_timing_characterization.py"),
    Path("tools/capture_v4_timing_host_snapshot.py"),
    Path("tools/step5d_autotune_v4_r013/timing_characterization.py"),
    Path("tools/step5d_autotune_v4_r006/live_adapter.py"),
    Path("tools/step5d_autotune_v4_live_writer.py"),
    Path("tools/step5d_autotune_v4_r004/transport.py"),
    Path("tools/step5d_autotune_v4_r004/arm_transition.py"),
    Path("tools/step5d_autotune_v4_r004/wire.py"),
    Path("tools/step5d_autotune_v4_r004/calibrated_runtime.py"),
    Path("tools/step5d_autotune_v4_r004/path_controller.py"),
    Path("tools/step5d_autotune_v4_r004/policies.py"),
    Path("tools/step5d_autotune_v4_r004/runtime.py"),
    Path("tools/step5d_autotune_v4_r004/home.py"),
    Path("tools/step5d_autotune_v4_r004/motion_profile.py"),
    Path("tools/step5d_autotune_v4_r004/qualification.py"),
    Path("tools/step5d_autotune_v4_r004_live_writer.py"),
    Path("tools/step5d_autotune_v4/contracts.py"),
    Path("tools/step5d_paper_outer_loop.py"),
    Path("tools/step5c_strict_rnn.py"),
    Path("tools/contact_semantics.py"),
    Path("tools/step5d_autotune_v4_r012/path_cbf_live.py"),
    Path("tools/step5d_autotune_v4_r012/safety_filter.py"),
    Path("tools/step5d_autotune_v4_r012/register_transport.py"),
    CONFIG_PATH,
    Path("config/step6/autotuner_v5_composition_contract_v2.json"),
    Path("config/step6/autotuner_v5_campaign_v2.json"),
    Path("config/step6/autotuner_v5_home_only_primary_v1.json"),
    Path("config/step6/v4_isaturation_validation_plan_v1.json"),
    Path("config/step5d/v4_two_stage_campaign_v1.json"),
    Path("config/step6/autotuner_v5_sidecars_v1.json"),
    Path("config/step6/autotuner_v5_extension_policy_v1.json"),
    Path("config/step6/step6_figure8_autotune_v1_runtime_release.json"),
    Path("config/step6/step6_figure8_runtime_manifest.json"),
    *GOVERNANCE_PATHS,
)


class FigureEightSourceIdentityError(RuntimeError):
    """The explicit physical-function source closure is incomplete."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _content(root: Path, relative: Path) -> tuple[bytes, str]:
    path = Path(root) / relative
    if path.is_symlink() or not path.is_file():
        raise FigureEightSourceIdentityError(
            f"Figure-eight source path is unavailable: {relative}"
        )
    if relative != CONFIG_PATH:
        return path.read_bytes(), "raw_bytes"
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise FigureEightSourceIdentityError("Figure-eight config is not an object")
    normalized = json.loads(json.dumps(value))
    controller = normalized.get("controller")
    if not isinstance(controller, dict):
        raise FigureEightSourceIdentityError("Figure-eight config controller is missing")
    normalized["controller"]["source_parent_sha256"] = "0" * 64
    return _canonical(normalized), "canonical_json_source_sha_zeroed"


def build_source_identity(root: Path) -> dict[str, Any]:
    base = Path(root).resolve()
    files = []
    for relative in SOURCE_PATHS:
        content, normalization = _content(base, relative)
        files.append({
            "path": relative.as_posix(),
            "sha256": hashlib.sha256(content).hexdigest(),
            "byte_size": len(content),
            "normalization": normalization,
        })
    payload = {
        "schema": "step6.autotune/figure8-source-identity-v1",
        "version": 1,
        "source_paths": [item["path"] for item in files],
        "files": files,
        "config_self_reference_policy": "source_parent_sha256_zeroed_before_hash",
    }
    return {
        **payload,
        "source_sha256": hashlib.sha256(_canonical(payload)).hexdigest(),
    }


def require_source_identity(root: Path, expected_sha256: str) -> dict[str, Any]:
    identity = build_source_identity(root)
    if identity["source_sha256"] != expected_sha256:
        raise FigureEightSourceIdentityError(
            "Figure-eight configured source SHA differs from the current source closure"
        )
    return identity


__all__ = [
    "CONFIG_PATH",
    "SOURCE_PATHS",
    "FigureEightSourceIdentityError",
    "build_source_identity",
    "require_source_identity",
]
