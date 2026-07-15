"""Post-closure metrics and optional PNG generation for immutable captures."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


class PostprocessError(RuntimeError):
    """Raised for structural evidence errors after the physical ACK boundary."""


def metrics_from_bundle(
    bundle: Mapping[str, Any]
) -> tuple[dict[str, Any], bool, bool]:
    evaluation = bundle.get("evaluation")
    if not isinstance(evaluation, Mapping):
        raise PostprocessError("immutable capture lacks an evaluation object")
    metrics = evaluation.get("metrics") or {}
    governor = metrics.get("governor") or {}
    diagnostic = governor.get("nontrainable_profile_diagnostic") or {}
    tracking = governor.get("tracking") or {}
    orientation = governor.get("orientation") or {}
    burden = governor.get("burden_by_layer") or {}
    force_mae = diagnostic.get("force_mae_n")
    complete_bins = evaluation.get("complete_bins")
    safe_closure = evaluation.get("safe_closure") is True
    if not isinstance(complete_bins, int) or isinstance(complete_bins, bool):
        raise PostprocessError("immutable capture lacks an integer complete_bins")
    if force_mae is None:
        raise PostprocessError("immutable capture lacks diagnostic force MAE")
    result = {
        "force_mae_n": force_mae,
        "objective_mae_n": evaluation.get("objective_mae_n"),
        "complete_bins": complete_bins,
        "correlation": tracking.get("correlation"),
        "lag_s": tracking.get("lag_s"),
        "nrmse": tracking.get("nrmse"),
        "orientation_p95_rad": orientation.get("p95_error_rad"),
        "orientation_max_rad": orientation.get("max_error_rad"),
        "normal_filter_saturation": burden.get("normal_filter_rate"),
        "tp_accel_saturation": burden.get("tp_speedj_acceleration"),
        "host_slew_saturation": burden.get("host_qdot_slew"),
        "safe_closure": safe_closure,
        "eligible_objective": evaluation.get("eligible") is True,
    }
    diagnostic_eligible = safe_closure and complete_bins >= 550
    return result, diagnostic_eligible, evaluation.get("eligible") is True


def analyze_capture(
    *, capture: Path, trial_id: str, output_dir: Path
) -> dict[str, Any]:
    if not capture.is_file() or capture.is_symlink():
        raise PostprocessError("immutable capture is missing or unsafe")
    try:
        bundle = json.loads(capture.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PostprocessError(f"immutable capture JSON is invalid: {exc}") from exc
    if not isinstance(bundle, dict):
        raise PostprocessError("immutable capture must be a JSON object")
    metrics, diagnostic_eligible, objective_eligible = metrics_from_bundle(bundle)
    artifacts: list[dict[str, str]] = []
    warnings: list[str] = []
    try:
        from publish_step5d_autotune_plot import build_plot

        png = build_plot(capture, output_dir)
        artifacts.append(
            {
                "role": "parameter_named_png",
                "path": str(png.resolve()),
                "sha256": hashlib.sha256(png.read_bytes()).hexdigest(),
            }
        )
    except Exception as exc:
        warnings.append(f"png:{type(exc).__name__}:{exc}")
    return {
        "schema": "step5d.autotune.analysis/v2",
        "trial_id": trial_id,
        "metrics": metrics,
        "diagnostic_eligible": diagnostic_eligible,
        "objective_eligible": objective_eligible,
        "artifacts": artifacts,
        "warnings": warnings,
    }
