from __future__ import annotations

import csv
import importlib
import sys
from pathlib import Path
from typing import Any, Iterator, Mapping


FEATURE_KEYS = (
    "force_mae_n", "tracking_nrmse", "tracking_correlation", "tracking_lag_s",
    "orientation_error_max_rad", "orientation_error_p95_rad",
    "orientation_saturation_duty", "host_slew_saturation_duty",
    "tp_accel_saturation_duty", "capture_rows", "stage25_rows",
    "feedback_age_p99_s", "cadence_ok", "feedback_fresh", "safe_closure",
)


def _nested(value: Mapping[str, Any], *keys: str) -> Any:
    current: Any = value
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def features_from_bundle(bundle: Mapping[str, Any]) -> dict[str, Any]:
    evaluation = bundle.get("evaluation", {})
    metrics = evaluation.get("metrics", {}) if isinstance(evaluation, Mapping) else {}
    capture = bundle.get("capture", {})
    evidence = capture.get("evidence", {}) if isinstance(capture, Mapping) else {}
    values = {
        "force_mae_n": _nested(metrics, "governor", "nontrainable_profile_diagnostic", "force_mae_n"),
        "tracking_nrmse": _nested(metrics, "governor", "tracking", "nrmse"),
        "tracking_correlation": _nested(metrics, "governor", "tracking", "correlation"),
        "tracking_lag_s": _nested(metrics, "governor", "tracking", "lag_s"),
        "orientation_error_max_rad": _nested(metrics, "profile", "orientation_error_max_rad"),
        "orientation_error_p95_rad": _nested(metrics, "profile", "orientation_error_p95_rad"),
        "orientation_saturation_duty": _nested(metrics, "profile", "angular_saturation_duty"),
        "host_slew_saturation_duty": _nested(metrics, "profile", "host_slew_saturation_duty"),
        "tp_accel_saturation_duty": _nested(metrics, "profile", "tp_accel_saturation_duty"),
        "capture_rows": evidence.get("capture_rows") if isinstance(evidence, Mapping) else None,
        "stage25_rows": evidence.get("stage25_rows") if isinstance(evidence, Mapping) else None,
        "feedback_age_p99_s": evidence.get("feedback_age_p99_s") if isinstance(evidence, Mapping) else None,
        "cadence_ok": capture.get("cadence_ok") if isinstance(capture, Mapping) else None,
        "feedback_fresh": capture.get("feedback_fresh") if isinstance(capture, Mapping) else None,
        "safe_closure": evaluation.get("safe_closure") if isinstance(evaluation, Mapping) else None,
    }
    return {key: values.get(key) for key in FEATURE_KEYS}


def stream_csv_rows(path: Path) -> Iterator[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as stream:
        yield from csv.DictReader(stream)


def recompute_fixed_f0_features(path: Path, reaction_normal_base: tuple[float, float, float]) -> dict[str, Any]:
    tools_dir = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(tools_dir))
    try:
        evaluator = importlib.import_module("step5d_autotune_evaluator")
        metrics = evaluator.fixed_f0_bin_metrics(
            stream_csv_rows(path), reaction_normal_base=reaction_normal_base
        )
    finally:
        if sys.path and sys.path[0] == str(tools_dir):
            sys.path.pop(0)
    return metrics
