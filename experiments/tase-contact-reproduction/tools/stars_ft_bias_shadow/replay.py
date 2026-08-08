"""Offline, sealed replay for the STARS FT bias shadow.

The implementation is historical science only.  It has no live-control
imports, no sensor-write path, no optimizer/ledger integration, and never
overwrites an output directory.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

from .adapters.r008_run_dir import (
    HistoricalRunSnapshot,
    R008RunDirError,
    canonical_json_bytes,
    iter_trace_samples,
    parse_strict_json_bytes,
    preflight_run_dir,
    sha256_bytes,
    verify_inputs_unchanged,
)
from .contact_gate import (
    BIAS_ESTIMATE_FIELDS,
    BIAS_RATE_ESTIMATE_FIELDS,
    decide_gate,
    residual_wrench_from_sample,
)
from .estimator import GatedEmaBiasEstimator


DEFAULT_CONFIG_NAME = "stars_shadow_overnight_v0.json"
CONFIG_SCHEMA = "stars_ft_bias_shadow/overnight_v0"
HARDENED_POLICY_VERSION = "stars_ft_bias_shadow/hardened_v1"
EXECUTION_BEHAVIOR_VERSION = "stars_ft_bias_shadow/execution_behavior_v1"
EXECUTION_BEHAVIOR = {
    "version": EXECUTION_BEHAVIOR_VERSION,
    "trace_merge": "two_source_one_row_lookahead_global_monotonic",
    "time_credit": "positive_global_contiguous_dt_only",
    "first_row_credit": "zero",
    "gap_credit": "zero_and_count_full_gap_seconds",
    "coverage_denominator": "credited_observed_time_s",
    "pose_speed_timebase": "elapsed_time_from_previous_row_of_same_source",
    "state20_static_update": "eligible_when_all_gate_evidence_passes",
    "host_log_terminal": "final_nonblank_strict_json_object_prior_non_json_counted",
    "terminal_formal_policy": "incomplete_status_implies_false_completed_requires_explicit_boolean",
    "output_sealing": "exclusive_files_receipt_cold_read",
}

EXPECTED_BIAS_FIELDS = [
    "bias_est_fx_n",
    "bias_est_fy_n",
    "bias_est_fz_n",
    "bias_est_mx_nm",
    "bias_est_my_nm",
    "bias_est_mz_nm",
]
EXPECTED_RATE_FIELDS = [
    "bias_rate_est_fx_n_per_s",
    "bias_rate_est_fy_n_per_s",
    "bias_rate_est_fz_n_per_s",
    "bias_rate_est_mx_nm_per_s",
    "bias_rate_est_my_nm_per_s",
    "bias_rate_est_mz_nm_per_s",
]


def _config_error(code: str, message: str, **details: Any) -> R008RunDirError:
    return R008RunDirError(message, code=code, details=details)


def _positive_float(
    value: Any,
    *,
    label: str,
    upper: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _config_error("config_type_invalid", f"{label} must be a finite positive number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise _config_error("config_type_invalid", f"{label} must be a finite positive number") from exc
    if not math.isfinite(number) or number <= 0.0:
        raise _config_error("config_value_invalid", f"{label} must be finite and positive")
    if number > upper:
        raise _config_error("config_value_out_of_bounds", f"{label} exceeds hardened bound", upper=upper)
    return number


def _positive_int(value: Any, *, label: str, upper: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _config_error("config_type_invalid", f"{label} must be a positive integer")
    if value <= 0 or value > upper:
        raise _config_error("config_value_out_of_bounds", f"{label} is outside hardened bounds", upper=upper)
    return value


def _strict_keys(value: Any, expected: set[str], *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        actual = sorted(value) if isinstance(value, dict) else type(value).__name__
        raise _config_error(
            "config_schema_invalid",
            f"{label} must contain exactly the hardened fields",
            expected=sorted(expected),
            actual=actual,
        )
    return value


def validate_config(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the exact v1 config shape and all bounded numeric controls."""

    top = _strict_keys(
        raw,
        {
            "schema",
            "policy_version",
            "science_not_promoted",
            "wrench_authority",
            "sensor_zero_tare_or_config_allowed",
            "source_coverage",
            "estimator",
            "gates",
            "limits",
            "outputs",
        },
        label="config",
    )
    if top["schema"] != CONFIG_SCHEMA:
        raise _config_error("config_schema_invalid", "config.schema is not the STARS overnight schema")
    if top["policy_version"] != HARDENED_POLICY_VERSION:
        raise _config_error("config_policy_invalid", "config.policy_version is not the hardened policy")
    if top["science_not_promoted"] is not True:
        raise _config_error("science_promotion_forbidden", "science_not_promoted must be true")
    if top["wrench_authority"] != "kunwei_only":
        raise _config_error("wrench_authority_invalid", "wrench_authority must be kunwei_only")
    if top["sensor_zero_tare_or_config_allowed"] is not False:
        raise _config_error(
            "tare_or_config_write_forbidden",
            "sensor_zero_tare_or_config_allowed must be false",
        )

    source_coverage = _strict_keys(
        top["source_coverage"], {"allow_missing_state25"}, label="config.source_coverage"
    )
    if source_coverage["allow_missing_state25"] is not True:
        raise _config_error("config_source_coverage_invalid", "allow_missing_state25 must be true")

    estimator = _strict_keys(
        top["estimator"], {"kind", "ema_tau_s", "rate_finite_diff_min_dt_s"}, label="config.estimator"
    )
    if estimator["kind"] != "gated_ema":
        raise _config_error("config_estimator_invalid", "only gated_ema is allowed")
    _positive_float(estimator["ema_tau_s"], label="estimator.ema_tau_s", upper=86400.0)
    _positive_float(
        estimator["rate_finite_diff_min_dt_s"],
        label="estimator.rate_finite_diff_min_dt_s",
        upper=86400.0,
    )

    gates = _strict_keys(
        top["gates"],
        {
            "normal_load_threshold_n",
            "force_norm_threshold_n",
            "torque_norm_threshold_nm",
            "tcp_linear_max_m_s",
            "tcp_angular_max_rad_s",
            "require_sensor_fresh",
            "known_tp_states",
            "update_tp_states",
            "freeze_tp_states",
            "search_tp_states",
        },
        label="config.gates",
    )
    _positive_float(gates["normal_load_threshold_n"], label="gates.normal_load_threshold_n", upper=1000.0)
    _positive_float(gates["force_norm_threshold_n"], label="gates.force_norm_threshold_n", upper=1000.0)
    _positive_float(gates["torque_norm_threshold_nm"], label="gates.torque_norm_threshold_nm", upper=1000.0)
    _positive_float(gates["tcp_linear_max_m_s"], label="gates.tcp_linear_max_m_s", upper=10.0)
    _positive_float(gates["tcp_angular_max_rad_s"], label="gates.tcp_angular_max_rad_s", upper=100.0)
    if gates["require_sensor_fresh"] is not True:
        raise _config_error("config_gate_invalid", "gates.require_sensor_fresh must be true")

    def state_list(value: Any, label: str) -> list[int]:
        if not isinstance(value, list) or not value:
            raise _config_error("config_gate_invalid", f"{label} must be a non-empty integer list")
        if any(isinstance(item, bool) or not isinstance(item, int) or item < 0 or item > 1000 for item in value):
            raise _config_error("config_gate_invalid", f"{label} contains an invalid tp_state")
        if len(set(value)) != len(value):
            raise _config_error("config_gate_invalid", f"{label} contains duplicates")
        return list(value)

    known = set(state_list(gates["known_tp_states"], "gates.known_tp_states"))
    update = set(state_list(gates["update_tp_states"], "gates.update_tp_states"))
    freeze = set(state_list(gates["freeze_tp_states"], "gates.freeze_tp_states"))
    search = set(state_list(gates["search_tp_states"], "gates.search_tp_states"))
    # ``search`` is a motion-sensitive subset: state20 is update-eligible
    # only while static, and moving state20 freezes.  It therefore overlaps
    # the update class by design; the freeze class remains disjoint.
    if known != update | freeze or update & freeze or not search <= update:
        raise _config_error(
            "config_gate_invalid",
            "tp_state classes must cover known states with search as an update subset",
        )

    limits = _strict_keys(
        top["limits"],
        {
            "max_contiguous_dt_s",
            "min_input_age_s",
            "max_input_file_bytes",
            "max_total_input_bytes",
            "max_output_rows",
            "max_output_file_bytes",
        },
        label="config.limits",
    )
    _positive_float(limits["max_contiguous_dt_s"], label="limits.max_contiguous_dt_s", upper=60.0)
    _positive_float(limits["min_input_age_s"], label="limits.min_input_age_s", upper=604800.0)
    _positive_int(
        limits["max_input_file_bytes"],
        label="limits.max_input_file_bytes",
        upper=64 * 1024 * 1024,
    )
    _positive_int(
        limits["max_total_input_bytes"],
        label="limits.max_total_input_bytes",
        upper=64 * 1024 * 1024,
    )
    _positive_int(limits["max_output_rows"], label="limits.max_output_rows", upper=10_000_000)
    _positive_int(
        limits["max_output_file_bytes"],
        label="limits.max_output_file_bytes",
        upper=256 * 1024 * 1024,
    )
    if limits["max_total_input_bytes"] < limits["max_input_file_bytes"]:
        raise _config_error("config_limits_invalid", "total input cap cannot be below per-file cap")

    outputs = _strict_keys(
        top["outputs"], {"bias_est_fields", "bias_rate_est_fields"}, label="config.outputs"
    )
    if outputs["bias_est_fields"] != EXPECTED_BIAS_FIELDS or outputs["bias_rate_est_fields"] != EXPECTED_RATE_FIELDS:
        raise _config_error("config_outputs_invalid", "output field names do not match the stable schema")
    return dict(top)


def _load_config_document(
    path: Path | None,
    experiment_root: Path,
) -> tuple[dict[str, Any], bytes, str]:
    config_path = (
        Path(path)
        if path is not None
        else Path(experiment_root) / "config" / "ft_bias" / DEFAULT_CONFIG_NAME
    )
    if config_path.is_symlink() or not config_path.is_file():
        raise _config_error("config_input_invalid", "config must be an existing regular non-symlink file")
    raw_bytes = config_path.read_bytes()
    raw = parse_strict_json_bytes(raw_bytes, label="shadow config")
    if not isinstance(raw, dict):
        raise _config_error("config_schema_invalid", "shadow config must be an object")
    validated = validate_config(raw)
    return validated, raw_bytes, sha256_bytes(raw_bytes)


def load_config(path: Path | None, experiment_root: Path) -> dict[str, Any]:
    """Load the default-named strict config while preserving the old API."""

    return _load_config_document(path, experiment_root)[0]


def _mapping_config_document(config: Mapping[str, Any]) -> tuple[dict[str, Any], bytes, str]:
    try:
        raw_bytes = canonical_json_bytes(dict(config))
        raw = parse_strict_json_bytes(raw_bytes, label="mapping shadow config")
    except R008RunDirError:
        raise
    if not isinstance(raw, dict):
        raise _config_error("config_schema_invalid", "mapping shadow config must be an object")
    validated = validate_config(raw)
    return validated, raw_bytes, sha256_bytes(raw_bytes)


@dataclass(frozen=True)
class ShadowReplayResult:
    run_dir: Path
    out_dir: Path
    bias_est_path: Path
    summary_path: Path
    summary: dict[str, Any]
    rows_written: int
    input_manifest_path: Path
    completion_receipt_path: Path


def default_shadow_out_dir(
    experiment_root: Path,
    run_dir: Path,
    execution_identity_sha256: str | None = None,
) -> Path:
    """Return the immutable default output location for an execution."""

    run_id = Path(run_dir).name
    if execution_identity_sha256 is None:
        raise R008RunDirError(
            "execution identity is required for the default output path",
            code="execution_identity_required",
        )
    return (
        Path(experiment_root)
        / "runs"
        / "step5d_autotune_v4_r008"
        / "_shadow_out"
        / run_id
        / execution_identity_sha256
    )


def _tool_closure() -> tuple[str, list[dict[str, Any]]]:
    tool_root = Path(__file__).resolve().parent
    entries: list[dict[str, Any]] = []
    for path in sorted(tool_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        if path.is_symlink() or not path.is_file():
            raise R008RunDirError(
                f"tool source closure contains a non-regular file: {path.name}",
                code="tool_closure_invalid",
            )
        relative = path.relative_to(tool_root).as_posix()
        data = path.read_bytes()
        entries.append({"path": relative, "bytes": len(data), "sha256": sha256_bytes(data)})
    if not entries:
        raise R008RunDirError("tool source closure is empty", code="tool_closure_invalid")
    return sha256_bytes(canonical_json_bytes(entries)), entries


def _execution_identity(
    *,
    config_identity_sha256: str,
    tool_closure_sha256: str,
    input_manifest_sha256: str,
) -> tuple[str, str, dict[str, Any]]:
    behavior_sha256 = sha256_bytes(canonical_json_bytes(EXECUTION_BEHAVIOR))
    payload: dict[str, Any] = {
        "schema": "stars_ft_bias_shadow/execution_identity_v1",
        "policy_version": HARDENED_POLICY_VERSION,
        "config_bytes_sha256": config_identity_sha256,
        "tool_python_closure_sha256": tool_closure_sha256,
        "input_manifest_sha256": input_manifest_sha256,
        "execution_behavior_sha256": behavior_sha256,
    }
    return sha256_bytes(canonical_json_bytes(payload)), behavior_sha256, payload


def _json_bytes(value: Any, *, label: str) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise R008RunDirError(f"{label} cannot be serialized strictly: {exc}", code="output_not_json") from exc


def _exclusive_json(path: Path, value: Any, *, label: str, max_bytes: int) -> int:
    data = _json_bytes(value, label=label) + b"\n"
    if len(data) > max_bytes:
        raise R008RunDirError(f"{label} exceeds output size limit", code="output_file_too_large")
    try:
        with path.open("xb") as handle:
            handle.write(data)
    except FileExistsError as exc:
        raise R008RunDirError(f"refusing to overwrite {label}", code="output_exists") from exc
    return len(data)


def _file_digest(path: Path) -> tuple[int, str]:
    if path.is_symlink() or not path.is_file():
        raise R008RunDirError(f"output {path.name} is not a regular file", code="output_invalid")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _count_strict_jsonl(path: Path, *, expected_schema: str | None = None) -> int:
    count = 0
    with path.open("rb") as handle:
        for line_no, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                raise R008RunDirError(f"{path.name} line {line_no} is blank", code="output_invalid")
            row = parse_strict_json_bytes(raw_line.rstrip(b"\r\n"), label=f"{path.name} line {line_no}")
            if not isinstance(row, dict):
                raise R008RunDirError(f"{path.name} line {line_no} is not an object", code="output_invalid")
            if expected_schema is not None and row.get("schema") != expected_schema:
                raise R008RunDirError(f"{path.name} has an unexpected schema", code="output_invalid")
            count += 1
    return count


def _manifest_document(snapshot: HistoricalRunSnapshot, identity: Mapping[str, Any]) -> dict[str, Any]:
    document = dict(snapshot.input_manifest)
    document.update(
        {
            "input_manifest_sha256": snapshot.input_manifest_sha256,
            "config_identity_sha256": identity["config_bytes_sha256"],
            "tool_closure_sha256": identity["tool_python_closure_sha256"],
            "execution_identity_sha256": identity["execution_identity_sha256"],
        }
    )
    return document


def _row_with_identity(
    sample: Any,
    *,
    estimator_row: Mapping[str, Any],
    residual: tuple[float, float, float, float, float, float] | None,
    identity: Mapping[str, Any],
    terminal_status: str,
    invalid_fields: list[str],
    source_coverage: Mapping[str, Any],
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "schema": "stars_ft_bias_shadow/bias_est_v1",
        "source": sample.source,
        "line": sample.line_no,
        "source_line": sample.line_no,
        "packet_sequence": sample.packet_sequence,
        "packet": sample.packet_sequence,
        "monotonic_s": sample.t_s,
        "time_s": sample.t_s,
        "wall_time_s": sample.wall_time_s,
        "tp_state": sample.row.get("tp_state"),
        "sensor_fresh": sample.row.get("sensor_fresh"),
        "force_norm_n": sample.row.get("force_norm_n"),
        "normal_load_n": sample.row.get("normal_load_n"),
        "torque_norm_nm": sample.row.get("torque_norm_nm"),
        "tcp_speed_m_s": sample.tcp_speed_m_s,
        "tcp_omega_rad_s": sample.tcp_omega_rad_s,
        "gate": estimator_row["gate"],
        "reason": estimator_row["bias_contact_reason"],
        "reason_code": estimator_row["bias_contact_reason"],
        "bias_contact_reason": estimator_row["bias_contact_reason"],
        "update_allowed": estimator_row["update_allowed"],
        "input_valid": estimator_row["input_valid"],
        "bias_estimation_contact_mask": estimator_row["bias_estimation_contact_mask"],
        "contact_mask": estimator_row["contact_mask"],
        "invalid_fields": invalid_fields,
        "dt_raw_s": sample.dt_raw_s,
        "dt_credited_s": sample.dt_credited_s,
        "dt_raw": sample.dt_raw_s,
        "dt_credited": sample.dt_credited_s,
        "source_dt_raw_s": sample.source_dt_raw_s,
        "gap": sample.gap,
        "gap_s": sample.gap_s,
        "gap_seconds": sample.gap_s,
        "source_first": sample.source_first,
        "source_coverage": {
            "state20_present": source_coverage["state20_present"],
            "state25_present": source_coverage["state25_present"],
            "partial_source_coverage": source_coverage["partial_source_coverage"],
        },
        "residual_valid": residual is not None,
        "dropped_count": 0,
        "malformed_count": 0,
        "dropped_malformed_counters": {"dropped": 0, "malformed": 0},
        "terminal_status": terminal_status,
        "config_identity_sha256": identity["config_bytes_sha256"],
        "tool_closure_sha256": identity["tool_python_closure_sha256"],
        "input_manifest_sha256": identity["input_manifest_sha256"],
        "execution_identity_sha256": identity["execution_identity_sha256"],
    }
    if residual is not None:
        for name, value in zip(
            ("residual_fx_n", "residual_fy_n", "residual_fz_n", "residual_mx_nm", "residual_my_nm", "residual_mz_nm"),
            residual,
            strict=True,
        ):
            row[name] = value
    for field in BIAS_ESTIMATE_FIELDS + BIAS_RATE_ESTIMATE_FIELDS:
        row[field] = estimator_row[field]
    return row


def _validate_no_absolute_identity(value: Any) -> None:
    if isinstance(value, str):
        if value.startswith("/") or "\\" in value:
            raise R008RunDirError(
                "absolute path found in execution identity",
                code="identity_contains_absolute_path",
            )
    elif isinstance(value, dict):
        for nested in value.values():
            _validate_no_absolute_identity(nested)
    elif isinstance(value, list):
        for nested in value:
            _validate_no_absolute_identity(nested)


def validate_completion_receipt(out_dir: Path) -> dict[str, Any]:
    """Cold-read and verify the four-file sealed output bundle."""

    output = Path(out_dir)
    if output.is_symlink() or not output.is_dir():
        raise R008RunDirError("completion output is not a regular directory", code="receipt_invalid")
    required = {
        "input_manifest.json",
        "bias_est.jsonl",
        "summary.json",
        "completion_receipt.json",
    }
    actual = {item.name for item in output.iterdir() if item.is_file() or item.is_symlink()}
    if actual != required:
        raise R008RunDirError("sealed output contains unexpected or missing files", code="receipt_invalid")
    for name in required:
        path = output / name
        if path.is_symlink() or not path.is_file():
            raise R008RunDirError(f"missing sealed output {name}", code="receipt_invalid")
    receipt_raw = parse_strict_json_bytes(
        (output / "completion_receipt.json").read_bytes(), label="completion_receipt.json"
    )
    if not isinstance(receipt_raw, dict) or receipt_raw.get("schema") != "stars_ft_bias_shadow/completion_receipt_v1":
        raise R008RunDirError("completion receipt schema is invalid", code="receipt_invalid")
    if receipt_raw.get("sealed") is not True or receipt_raw.get("partial_failure") is not False:
        raise R008RunDirError("completion receipt is not sealed", code="receipt_invalid")
    payload_hash = receipt_raw.get("receipt_payload_sha256")
    if not isinstance(payload_hash, str):
        raise R008RunDirError("completion receipt payload hash is missing", code="receipt_invalid")
    payload = dict(receipt_raw)
    payload.pop("receipt_payload_sha256", None)
    if sha256_bytes(canonical_json_bytes(payload)) != payload_hash:
        raise R008RunDirError("completion receipt payload hash mismatch", code="receipt_tampered")

    identity_payload = receipt_raw.get("execution_identity_payload")
    if not isinstance(identity_payload, dict):
        raise R008RunDirError("execution identity payload is missing", code="receipt_invalid")
    _validate_no_absolute_identity(identity_payload)
    execution_identity = receipt_raw.get("execution_identity_sha256")
    if execution_identity != sha256_bytes(canonical_json_bytes(identity_payload)):
        raise R008RunDirError("execution identity hash mismatch", code="receipt_tampered")

    output_files = receipt_raw.get("output_files")
    if not isinstance(output_files, dict) or set(output_files) != {
        "input_manifest.json",
        "bias_est.jsonl",
        "summary.json",
    }:
        raise R008RunDirError("completion receipt output binding is invalid", code="receipt_invalid")
    row_counts: dict[str, int] = {}
    for name, binding in output_files.items():
        if not isinstance(binding, dict):
            raise R008RunDirError("output binding is not an object", code="receipt_invalid")
        size, digest = _file_digest(output / name)
        if binding.get("bytes") != size or binding.get("sha256") != digest:
            raise R008RunDirError(f"sealed output {name} was tampered", code="receipt_tampered")
        if name == "bias_est.jsonl":
            rows = _count_strict_jsonl(output / name, expected_schema="stars_ft_bias_shadow/bias_est_v1")
        else:
            document = parse_strict_json_bytes((output / name).read_bytes(), label=name)
            if not isinstance(document, dict):
                raise R008RunDirError(f"{name} is not an object", code="receipt_invalid")
            rows = 1
        if binding.get("rows") != rows:
            raise R008RunDirError(f"sealed row count for {name} is invalid", code="receipt_tampered")
        row_counts[name] = rows
    if receipt_raw.get("rows_written") != row_counts["bias_est.jsonl"]:
        raise R008RunDirError("receipt row count is inconsistent", code="receipt_tampered")

    manifest = parse_strict_json_bytes((output / "input_manifest.json").read_bytes(), label="input_manifest.json")
    summary = parse_strict_json_bytes((output / "summary.json").read_bytes(), label="summary.json")
    if not isinstance(manifest, dict) or not isinstance(summary, dict):
        raise R008RunDirError("sealed manifest or summary is not an object", code="receipt_invalid")
    manifest_payload = dict(manifest)
    manifest_hash = manifest_payload.pop("input_manifest_sha256", None)
    for field in (
        "config_identity_sha256",
        "tool_closure_sha256",
        "execution_identity_sha256",
    ):
        manifest_payload.pop(field, None)
    if manifest_hash != receipt_raw.get("input_manifest_sha256") or sha256_bytes(canonical_json_bytes(manifest_payload)) != manifest_hash:
        raise R008RunDirError("input manifest identity mismatch", code="receipt_tampered")
    for field in (
        "config_identity_sha256",
        "tool_closure_sha256",
        "input_manifest_sha256",
        "execution_identity_sha256",
        "terminal_status",
        "formal_live_complete",
        "host_non_json_line_count",
        "host_non_object_line_count",
        "host_blank_line_count",
        "terminal_evidence",
    ):
        if summary.get(field) != receipt_raw.get(field):
            raise R008RunDirError(f"summary identity field {field} is inconsistent", code="receipt_tampered")
    for field in (
        "host_non_json_line_count",
        "host_non_object_line_count",
        "host_blank_line_count",
        "terminal_evidence",
    ):
        if manifest.get(field) != summary.get(field):
            raise R008RunDirError(f"manifest terminal evidence field {field} is inconsistent", code="receipt_tampered")
    terminal_evidence = summary.get("terminal_evidence")
    if not isinstance(terminal_evidence, dict) or terminal_evidence.get(
        "terminal_line_policy"
    ) != "final_nonblank_line_strict_json_object":
        raise R008RunDirError("terminal evidence semantics are invalid", code="receipt_invalid")
    if summary.get("rows_written") != row_counts["bias_est.jsonl"]:
        raise R008RunDirError("summary row count is inconsistent", code="receipt_tampered")
    if summary.get("science_not_promoted") is not True or summary.get("sensor_zero_tare_or_config_allowed") is not False:
        raise R008RunDirError("science/tare invariants are not sealed", code="receipt_invalid")
    coverage = summary.get("update_coverage")
    if isinstance(coverage, bool) or not isinstance(coverage, (int, float)) or coverage < 0.0 or coverage > 1.0:
        raise R008RunDirError("update coverage is outside [0,1]", code="receipt_invalid")
    return summary


def validate_cold_read(out_dir: Path) -> dict[str, Any]:
    """Alias with the terminology used by downstream offline validators."""

    return validate_completion_receipt(out_dir)


cold_read_validate = validate_completion_receipt


def _write_bias_rows(
    path: Path,
    *,
    run: HistoricalRunSnapshot,
    cfg: Mapping[str, Any],
    identity: Mapping[str, Any],
    estimator: GatedEmaBiasEstimator,
    terminal_status: str,
    max_rows: int,
    max_bytes: int,
) -> tuple[int, dict[str, int], float, float, float, dict[str, int]]:
    rows_written = 0
    source_counts = {"state20": 0, "state25": 0}
    credited_observed_time_s = 0.0
    gap_seconds = 0.0
    gap_count = 0
    invalid_fields_count: dict[str, int] = {}
    gates = cfg["gates"]
    max_dt = float(cfg["limits"]["max_contiguous_dt_s"])
    with path.open("xb") as handle:
        for sample in iter_trace_samples(run.run_dir, max_contiguous_dt_s=max_dt):
            if rows_written >= max_rows:
                raise R008RunDirError("output row cap exceeded", code="output_row_limit")
            decision = decide_gate(
                sample.row,
                gates=gates,
                tcp_speed_m_s=sample.tcp_speed_m_s,
                tcp_omega_rad_s=sample.tcp_omega_rad_s,
                dt_s=sample.dt_credited_s,
            )
            residual = residual_wrench_from_sample(sample.row)
            estimator_row = estimator.step(
                t_s=sample.t_s,
                residual=residual,
                decision=decision,
                dt_s=sample.dt_credited_s,
            )
            invalid_fields = list(decision.invalid_fields)
            for field in invalid_fields:
                invalid_fields_count[field] = invalid_fields_count.get(field, 0) + 1
            row = _row_with_identity(
                sample,
                estimator_row=estimator_row,
                residual=residual,
                identity=identity,
                terminal_status=terminal_status,
                invalid_fields=invalid_fields,
                source_coverage=run.source_coverage,
            )
            data = _json_bytes(row, label="bias_est row") + b"\n"
            if handle.tell() + len(data) > max_bytes:
                raise R008RunDirError("bias_est.jsonl exceeds output size limit", code="output_file_too_large")
            handle.write(data)
            rows_written += 1
            source_counts[sample.source] += 1
            credited_observed_time_s += sample.dt_credited_s
            if sample.gap:
                gap_count += 1
                gap_seconds += sample.gap_s
    return (
        rows_written,
        source_counts,
        credited_observed_time_s,
        gap_seconds,
        float(gap_count),
        invalid_fields_count,
    )


def replay_run_dir(
    run_dir: Path,
    *,
    experiment_root: Path,
    config: Mapping[str, Any] | None = None,
    config_path: Path | None = None,
    out_dir: Path | None = None,
) -> ShadowReplayResult:
    """Replay one immutable historical snapshot and seal four outputs."""

    if config is not None and config_path is not None:
        raise R008RunDirError("choose config or config_path, not both", code="config_input_invalid")
    root = Path(experiment_root).resolve()
    config_file_path: Path | None = None
    config_file_stat: tuple[int, int, int, int] | None = None
    if config is None:
        config_file_path = (
            Path(config_path)
            if config_path is not None
            else root / "config" / "ft_bias" / DEFAULT_CONFIG_NAME
        )
        config_file_path = config_file_path.resolve()
        cfg, config_bytes, config_identity_sha256 = _load_config_document(config_file_path, root)
        info = config_file_path.stat()
        config_file_stat = (info.st_size, info.st_mtime_ns, info.st_ino, info.st_dev)
    else:
        cfg, config_bytes, config_identity_sha256 = _mapping_config_document(config)
    del config_bytes
    limits = cfg["limits"]
    run = preflight_run_dir(
        Path(run_dir),
        experiment_root=root,
        min_input_age_s=float(limits["min_input_age_s"]),
        max_input_file_bytes=int(limits["max_input_file_bytes"]),
        max_total_input_bytes=int(limits["max_total_input_bytes"]),
        max_contiguous_dt_s=float(limits["max_contiguous_dt_s"]),
        allow_missing_state25=bool(cfg["source_coverage"]["allow_missing_state25"]),
    )
    tool_closure_sha256, tool_entries = _tool_closure()
    execution_identity_sha256, behavior_sha256, execution_payload = _execution_identity(
        config_identity_sha256=config_identity_sha256,
        tool_closure_sha256=tool_closure_sha256,
        input_manifest_sha256=run.input_manifest_sha256,
    )
    identity: dict[str, Any] = {
        "config_bytes_sha256": config_identity_sha256,
        "tool_python_closure_sha256": tool_closure_sha256,
        "input_manifest_sha256": run.input_manifest_sha256,
        "execution_identity_sha256": execution_identity_sha256,
        "execution_identity_payload": execution_payload,
    }
    _validate_no_absolute_identity(execution_payload)

    output = (
        Path(out_dir)
        if out_dir is not None
        else default_shadow_out_dir(root, run.run_dir, execution_identity_sha256)
    )
    if output.exists() or output.is_symlink():
        raise R008RunDirError("output directory already exists; overwrite is forbidden", code="output_exists")
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise R008RunDirError("output directory already exists; overwrite is forbidden", code="output_exists") from exc

    manifest_document = _manifest_document(
        run,
        {
            "config_bytes_sha256": config_identity_sha256,
            "tool_python_closure_sha256": tool_closure_sha256,
            "execution_identity_sha256": execution_identity_sha256,
        },
    )
    manifest_path = output / "input_manifest.json"
    _exclusive_json(
        manifest_path,
        manifest_document,
        label="input_manifest.json",
        max_bytes=int(limits["max_output_file_bytes"]),
    )
    bias_path = output / "bias_est.jsonl"
    estimator = GatedEmaBiasEstimator(
        ema_tau_s=float(cfg["estimator"]["ema_tau_s"]),
        rate_finite_diff_min_dt_s=float(cfg["estimator"]["rate_finite_diff_min_dt_s"]),
    )
    (
        rows_written,
        source_counts,
        credited_observed_time_s,
        gap_seconds,
        gap_count_float,
        invalid_fields_count,
    ) = _write_bias_rows(
        bias_path,
        run=run,
        cfg=cfg,
        identity=identity,
        estimator=estimator,
        terminal_status=run.terminal_status,
        max_rows=int(limits["max_output_rows"]),
        max_bytes=int(limits["max_output_file_bytes"]),
    )

    # A second closure read makes a source edit during replay a hard failure.
    post_tool_closure_sha256, _ = _tool_closure()
    if post_tool_closure_sha256 != tool_closure_sha256:
        raise R008RunDirError("tool source closure changed during replay", code="tool_mutated_after_replay")
    verify_inputs_unchanged(
        run,
        min_input_age_s=float(limits["min_input_age_s"]),
        max_input_file_bytes=int(limits["max_input_file_bytes"]),
        max_total_input_bytes=int(limits["max_total_input_bytes"]),
    )
    if config_file_path is not None and config_file_stat is not None:
        if config_file_path.is_symlink() or not config_file_path.is_file():
            raise R008RunDirError("config changed during replay", code="config_mutated_after_replay")
        current_info = config_file_path.stat()
        current_stat = (current_info.st_size, current_info.st_mtime_ns, current_info.st_ino, current_info.st_dev)
        if current_stat != config_file_stat or sha256_bytes(config_file_path.read_bytes()) != config_identity_sha256:
            raise R008RunDirError("config changed during replay", code="config_mutated_after_replay")

    counts = estimator.summary_counts()
    if abs(
        credited_observed_time_s
        - (float(counts["update_time_s"]) + float(counts["freeze_time_s"]) + float(counts["predict_time_s"]))
    ) > 1e-9:
        raise R008RunDirError("credited time accounting is inconsistent", code="time_accounting_invalid")
    update_time_s = float(counts["update_time_s"])
    update_coverage = 0.0 if credited_observed_time_s <= 0.0 else update_time_s / credited_observed_time_s
    if update_coverage < 0.0 or update_coverage > 1.0 + 1e-12:
        raise R008RunDirError("update coverage exceeds credited observed time", code="coverage_invalid")
    update_coverage = min(1.0, max(0.0, update_coverage))

    source_coverage = dict(run.source_coverage)
    source_coverage["rows_by_source"] = source_counts
    source_coverage["credited_observed_time_s"] = credited_observed_time_s
    source_coverage["gap_seconds"] = gap_seconds
    source_coverage["gap_count"] = int(gap_count_float)
    first_t = source_coverage.get("first_monotonic_s")
    last_t = source_coverage.get("last_monotonic_s")
    first_wall_t = source_coverage.get("first_wall_time_s")
    last_wall_t = source_coverage.get("last_wall_time_s")
    monotonic_span_s = 0.0 if first_t is None or last_t is None else max(0.0, float(last_t) - float(first_t))
    wall_span_s = (
        0.0
        if first_wall_t is None or last_wall_t is None
        else max(0.0, float(last_wall_t) - float(first_wall_t))
    )
    summary: dict[str, Any] = {
        "schema": "stars_ft_bias_shadow/summary_v1",
        "policy_version": HARDENED_POLICY_VERSION,
        "science_not_promoted": True,
        "promotion_status": "offline_historical_shadow_only",
        "wrench_authority": "kunwei_only",
        "sensor_zero_tare_or_config_allowed": False,
        "run_id": run.run_id,
        "run_dir": str(run.run_dir),
        "out_dir": str(output),
        "terminal_status": run.terminal_status,
        "formal_live_complete": run.formal_live_complete,
        "host_non_json_line_count": run.host_non_json_line_count,
        "host_non_object_line_count": run.host_non_object_line_count,
        "host_blank_line_count": run.host_blank_line_count,
        "terminal_evidence": run.terminal_evidence,
        "config_schema": cfg["schema"],
        "config_identity_sha256": config_identity_sha256,
        "tool_closure_sha256": tool_closure_sha256,
        "tool_closure_files": tool_entries,
        "input_manifest_sha256": run.input_manifest_sha256,
        "execution_behavior_sha256": behavior_sha256,
        "execution_identity_sha256": execution_identity_sha256,
        "execution_identity_payload": execution_payload,
        "launch_context_identity_sha256": run.launch_context_identity_sha256,
        "launch_context_identity_anchor": run.launch_context_identity_anchor,
        "baseline": {
            "schema": run.baseline.schema,
            "sample_count": run.baseline.sample_count,
            "mean_wrench_n_nm": list(run.baseline.mean_wrench_n_nm),
            "zero_tare_config_write": False,
        },
        "rows_written": rows_written,
        "source_counts": source_counts,
        "source_coverage": source_coverage,
        "credited_observed_time_s": credited_observed_time_s,
        "wall_span_s": wall_span_s,
        "monotonic_span_s": monotonic_span_s,
        "update_time_s": update_time_s,
        "freeze_time_s": float(counts["freeze_time_s"]),
        "predict_time_s": float(counts["predict_time_s"]),
        "update_s": update_time_s,
        "freeze_s": float(counts["freeze_time_s"]),
        "predict_s": float(counts["predict_time_s"]),
        "update_coverage": update_coverage,
        "gap_seconds": gap_seconds,
        "gap_count": int(gap_count_float),
        "dropped_count": 0,
        "malformed_count": 0,
        "dropped_malformed_counters": {"dropped": 0, "malformed": 0},
        "invalid_fields_count": dict(sorted(invalid_fields_count.items())),
        "estimator_counts": counts,
        "final_bias_est": {
            field: estimator.bias[index] for index, field in enumerate(BIAS_ESTIMATE_FIELDS)
        },
        "phase_timing_rows": run.phase_timing_rows,
        "notes": [
            "Historical offline shadow only; no tare, zero, or configuration write is permitted.",
            "UPDATE requires exact fresh sensor evidence, known update-eligible state, finite wrench/load/pose speed, and credited contiguous dt.",
            "formal_live_complete is reported only and never promotes this science artifact.",
        ],
    }
    summary_path = output / "summary.json"
    _exclusive_json(
        summary_path,
        summary,
        label="summary.json",
        max_bytes=int(limits["max_output_file_bytes"]),
    )

    output_bindings: dict[str, dict[str, Any]] = {}
    for name in ("input_manifest.json", "bias_est.jsonl", "summary.json"):
        size, digest = _file_digest(output / name)
        rows = 1 if name != "bias_est.jsonl" else rows_written
        output_bindings[name] = {"bytes": size, "sha256": digest, "rows": rows}
    receipt_payload: dict[str, Any] = {
        "schema": "stars_ft_bias_shadow/completion_receipt_v1",
        "sealed": True,
        "partial_failure": False,
        "terminal_status": run.terminal_status,
        "formal_live_complete": run.formal_live_complete,
        "host_non_json_line_count": run.host_non_json_line_count,
        "host_non_object_line_count": run.host_non_object_line_count,
        "host_blank_line_count": run.host_blank_line_count,
        "terminal_evidence": run.terminal_evidence,
        "science_not_promoted": True,
        "sensor_zero_tare_or_config_allowed": False,
        "config_identity_sha256": config_identity_sha256,
        "tool_closure_sha256": tool_closure_sha256,
        "input_manifest_sha256": run.input_manifest_sha256,
        "execution_identity_sha256": execution_identity_sha256,
        "execution_identity_payload": execution_payload,
        "rows_written": rows_written,
        "output_files": output_bindings,
    }
    receipt = dict(receipt_payload)
    receipt["receipt_payload_sha256"] = sha256_bytes(canonical_json_bytes(receipt_payload))
    receipt_path = output / "completion_receipt.json"
    _exclusive_json(
        receipt_path,
        receipt,
        label="completion_receipt.json",
        max_bytes=int(limits["max_output_file_bytes"]),
    )
    validate_completion_receipt(output)
    return ShadowReplayResult(
        run_dir=run.run_dir,
        out_dir=output,
        bias_est_path=bias_path,
        summary_path=summary_path,
        summary=summary,
        rows_written=rows_written,
        input_manifest_path=manifest_path,
        completion_receipt_path=receipt_path,
    )


__all__ = [
    "CONFIG_SCHEMA",
    "DEFAULT_CONFIG_NAME",
    "EXECUTION_BEHAVIOR_VERSION",
    "HARDENED_POLICY_VERSION",
    "ShadowReplayResult",
    "default_shadow_out_dir",
    "load_config",
    "replay_run_dir",
    "validate_cold_read",
    "validate_completion_receipt",
    "validate_config",
    "cold_read_validate",
]
