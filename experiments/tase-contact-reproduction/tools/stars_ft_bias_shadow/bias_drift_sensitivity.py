"""Offline bias-drift sensitivity over a sealed STARS replay."""

from __future__ import annotations

import hashlib
import math
import statistics
from pathlib import Path
from typing import Any

from .adapters.r008_run_dir import R008RunDirError, canonical_json_bytes, parse_strict_json_bytes
from .contact_gate import BIAS_ESTIMATE_FIELDS
from .replay import validate_completion_receipt


SENSITIVITY_SCHEMA = "stars_ft_bias_shadow/bias-drift-sensitivity-v1"


def _linear_slope(times: list[float], values: list[float]) -> float:
    mean_t = statistics.fmean(times)
    mean_v = statistics.fmean(values)
    denominator = sum((time - mean_t) ** 2 for time in times)
    if denominator <= 0.0:
        return 0.0
    return sum((time - mean_t) * (value - mean_v) for time, value in zip(times, values, strict=True)) / denominator


def analyze_bias_drift(shadow_out_dir: Path) -> dict[str, Any]:
    out_dir = Path(shadow_out_dir)
    summary = validate_completion_receipt(out_dir)
    receipt_bytes = (out_dir / "completion_receipt.json").read_bytes()
    receipt = parse_strict_json_bytes(receipt_bytes, label="completion_receipt.json")
    if not isinstance(receipt, dict):
        raise R008RunDirError("completion receipt must be an object", code="sensitivity_invalid")
    times: list[float] = []
    values: dict[str, list[float]] = {field: [] for field in BIAS_ESTIMATE_FIELDS}
    with (out_dir / "bias_est.jsonl").open("rb") as handle:
        for line_number, line in enumerate(handle, 1):
            row = parse_strict_json_bytes(line.rstrip(b"\r\n"), label=f"bias_est line {line_number}")
            if not isinstance(row, dict):
                raise R008RunDirError("bias_est row is not an object", code="sensitivity_invalid")
            if row.get("gate") != "UPDATE" or row.get("update_allowed") is not True:
                continue
            time = row.get("monotonic_s")
            if isinstance(time, bool) or not isinstance(time, (int, float)) or not math.isfinite(float(time)):
                raise R008RunDirError("bias drift time is invalid", code="sensitivity_invalid")
            sample: list[float] = []
            for field in BIAS_ESTIMATE_FIELDS:
                value = row.get(field)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                    raise R008RunDirError("bias drift value is invalid", code="sensitivity_invalid")
                sample.append(float(value))
            times.append(float(time))
            for field, value in zip(BIAS_ESTIMATE_FIELDS, sample, strict=True):
                values[field].append(value)
    if len(times) < 2:
        raise R008RunDirError("bias drift sensitivity needs two UPDATE rows", code="sensitivity_insufficient")
    midpoint = len(times) // 2
    components = {}
    for field in BIAS_ESTIMATE_FIELDS:
        series = values[field]
        components[field] = {
            "first": series[0],
            "last": series[-1],
            "delta": series[-1] - series[0],
            "slope_per_s": _linear_slope(times, series),
            "second_half_minus_first_half_median": (
                statistics.median(series[midpoint:]) - statistics.median(series[:midpoint])
            ),
        }
    payload: dict[str, Any] = {
        "schema": SENSITIVITY_SCHEMA,
        "stars_execution_identity_sha256": receipt["execution_identity_sha256"],
        "stars_completion_receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
        "terminal_status": summary["terminal_status"],
        "update_rows": len(times),
        "time_span_s": max(times) - min(times),
        "components": components,
        "interpretation": "sensitivity_only_not_a_calibration_estimator",
        "calibration_result_modified": False,
        "science_not_promoted": True,
        "gp_observation": False,
        "force_correction": False,
        "zero_tare_or_config_write": False,
    }
    payload["sensitivity_sha256"] = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    return payload


def persist_bias_drift_sensitivity(path: Path, value: dict[str, Any]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        raise R008RunDirError("refusing to overwrite bias sensitivity", code="sensitivity_exists")
    with target.open("xb") as handle:
        handle.write(canonical_json_bytes(value) + b"\n")
    return target


__all__ = [
    "SENSITIVITY_SCHEMA",
    "analyze_bias_drift",
    "persist_bias_drift_sensitivity",
]
