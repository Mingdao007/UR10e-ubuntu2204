"""Durable post-processing tasks for the parameter receiver hot path.

The live receiver only enqueues immutable work.  This module also provides a
one-shot, offline consumer that validates capture identity before producing
immutable force metrics and a bounded diagnostic plot.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from step5d_autotune_v3.atomic_io import atomic_bytes
from step5d_autotune_v3.state import atomic_json, read_strict_json


OUTBOX_SCHEMA = "step5d.parameter-receiver/postprocess-task-v1"
RESULT_SCHEMA = "step5d.parameter-receiver/postprocess-result-v1"
EXPECTED_OPERATIONS = ("sha256", "analysis", "png")


class PostprocessError(RuntimeError):
    """An outbox task or one of its immutable inputs is inconsistent."""


def enqueue_postprocess_task(
    root: Path,
    *,
    dispatch_sequence: int,
    dispatch_identity: str | None = None,
    trial_uid: str,
    capture_path: Path,
    result_path: Path,
) -> Path:
    """Atomically enqueue SHA/analysis/PNG work without doing it inline."""

    tasks = root / "tasks"
    tasks.mkdir(parents=True, exist_ok=True, mode=0o700)
    identity = dispatch_identity or f"sequence:{int(dispatch_sequence)}"
    identity_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    path = tasks / f"{identity_key}.json"
    payload: dict[str, Any] = {
        "schema": OUTBOX_SCHEMA,
        "dispatch_sequence": int(dispatch_sequence),
        "dispatch_identity": identity,
        "trial_uid": str(trial_uid),
        "capture_path": str(capture_path),
        "result_path": str(result_path),
        "operations": ["sha256", "analysis", "png"],
        "optimizer_required": False,
        "status": "PENDING",
    }
    if path.exists() or path.is_symlink():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("immutable postprocess task is unreadable") from exc
        if path.is_symlink() or existing != payload:
            raise RuntimeError("immutable postprocess task differs")
    else:
        atomic_json(path, payload)
    return path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _strict_object(path: Path, role: str) -> dict[str, Any]:
    payload = read_strict_json(path, role=role)
    if not isinstance(payload, dict):
        raise PostprocessError(f"{role} must be a JSON object")
    return payload


def _required_string(payload: Mapping[str, Any], key: str, role: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise PostprocessError(f"{role} {key} must be a non-empty string")
    return value


def _required_positive_int(
    payload: Mapping[str, Any], key: str, role: str
) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PostprocessError(f"{role} {key} must be a positive integer")
    return value


def _regular_file_within(path: Path, root: Path, role: str) -> Path:
    if not path.is_absolute():
        raise PostprocessError(f"{role} path must be absolute")
    if path.is_symlink() or not path.is_file():
        raise PostprocessError(f"{role} must be a real regular file")
    if root.is_symlink() or not root.is_dir():
        raise PostprocessError(f"{role} allowed root must be a real directory")
    resolved = path.resolve(strict=True)
    resolved_root = root.resolve(strict=True)
    if not resolved.is_relative_to(resolved_root):
        raise PostprocessError(f"{role} escapes its allowed root")
    return resolved


def _uniform_string(
    rows: Sequence[Mapping[str, str]], field: str, role: str
) -> str:
    values = {row.get(field, "") for row in rows}
    values.discard("")
    if len(values) != 1:
        raise PostprocessError(
            f"{role} {field} must have exactly one non-empty value"
        )
    return next(iter(values))


def _uniform_int(
    rows: Sequence[Mapping[str, str]], field: str, role: str
) -> int:
    raw = _uniform_string(rows, field, role)
    try:
        value = float(raw)
    except ValueError as exc:
        raise PostprocessError(f"{role} {field} is not numeric") from exc
    if not math.isfinite(value) or not value.is_integer():
        raise PostprocessError(f"{role} {field} is not an integer")
    return int(value)


def _uniform_float(
    rows: Sequence[Mapping[str, str]], field: str, role: str
) -> float:
    raw = _uniform_string(rows, field, role)
    try:
        value = float(raw)
    except ValueError as exc:
        raise PostprocessError(f"{role} {field} is not numeric") from exc
    if not math.isfinite(value):
        raise PostprocessError(f"{role} {field} is not finite")
    return value


def _matching_float(
    rows: Sequence[Mapping[str, str]],
    *,
    field: str,
    expected: Any,
) -> float:
    if isinstance(expected, bool) or not isinstance(expected, (int, float)):
        raise PostprocessError(f"dispatch overlay {field} is not numeric")
    expected_value = float(expected)
    observed = _uniform_float(rows, field, "capture")
    if not math.isclose(
        observed,
        expected_value,
        rel_tol=1e-12,
        abs_tol=1e-15,
    ):
        raise PostprocessError(
            f"capture {field} differs from immutable dispatch overlay"
        )
    return observed


def _read_capture_rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                raise PostprocessError("capture CSV header is missing")
            rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise PostprocessError(f"cannot read capture CSV: {exc}") from exc
    if not rows:
        raise PostprocessError("capture CSV has no rows")
    return rows


def _candidate_and_identity(
    rows: Sequence[Mapping[str, str]],
    *,
    task: Mapping[str, Any],
    dispatch: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    packet = dispatch.get("packet")
    request = dispatch.get("request")
    if not isinstance(packet, dict) or not isinstance(request, dict):
        raise PostprocessError("dispatch packet and request must be objects")
    overlay = request.get("overlay")
    if not isinstance(overlay, dict):
        raise PostprocessError("dispatch request overlay must be an object")

    dispatch_sequence = _required_positive_int(
        task, "dispatch_sequence", "task"
    )
    trial_uid = _required_string(task, "trial_uid", "task")
    expected_candidate_uid = _required_string(
        request, "control_candidate_uid", "dispatch request"
    )
    identity_fields = {
        "campaign_epoch": "campaign_epoch",
        "trial_id": "trial_id",
        "candidate_token": "candidate_token",
        "execution_profile_id": "execution_profile_id",
        "command_seq": "command_seq",
        "logical_batch_sequence": "ur_output_int_register_34",
    }
    observed_identity: dict[str, Any] = {}
    for packet_key, csv_field in identity_fields.items():
        expected = _required_positive_int(packet, packet_key, "dispatch packet")
        observed = _uniform_int(rows, csv_field, "capture")
        if observed != expected:
            raise PostprocessError(
                f"capture {csv_field} differs from immutable dispatch packet"
            )
        observed_identity[packet_key] = observed
    if observed_identity["logical_batch_sequence"] != dispatch_sequence:
        raise PostprocessError(
            "capture logical batch sequence differs from task dispatch sequence"
        )
    if _uniform_string(rows, "autotune_trial_uid", "capture") != trial_uid:
        raise PostprocessError("capture trial UID differs from task")
    if (
        _uniform_string(rows, "autotune_control_candidate_uid", "capture")
        != expected_candidate_uid
    ):
        raise PostprocessError("capture candidate UID differs from dispatch")

    candidate = {
        "control_candidate_uid": expected_candidate_uid,
        "force_p_gain": _matching_float(
            rows,
            field="autotune_force_p_gain",
            expected=overlay.get("force_p_gain"),
        ),
        "force_i_gain": _matching_float(
            rows,
            field="autotune_force_i_gain",
            expected=overlay.get("force_i_gain"),
        ),
        "force_damping": _matching_float(
            rows,
            field="autotune_force_damping",
            expected=overlay.get("force_damping"),
        ),
        "applied_force_p_gain": _matching_float(
            rows,
            field="_step5d_applied_force_p_gain",
            expected=overlay.get("force_p_gain"),
        ),
        "applied_force_i_gain": _matching_float(
            rows,
            field="_step5d_applied_force_i_gain",
            expected=overlay.get("force_i_gain"),
        ),
        "applied_force_damping": _matching_float(
            rows,
            field="_step5d_applied_force_damping",
            expected=overlay.get("force_damping"),
        ),
        "orientation_ko": _matching_float(
            rows,
            field="autotune_orientation_ko",
            expected=overlay.get("orientation_ko"),
        ),
        "motion_kp": _matching_float(
            rows,
            field="autotune_motion_kp",
            expected=overlay.get("motion_kp", 1.5),
        ),
        "normal_filter_tau_s": _matching_float(
            rows,
            field="autotune_normal_filter_tau_s",
            expected=overlay.get("normal_filter_tau_s", 0.35),
        ),
        "applied_normal_filter_tau_s": _matching_float(
            rows,
            field="_step5d_normal_filter_tau_s",
            expected=overlay.get("normal_filter_tau_s", 0.35),
        ),
        "applied_orientation_ko": _matching_float(
            rows,
            field="_step5d_applied_orientation_ko",
            expected=overlay.get("orientation_ko"),
        ),
        "applied_motion_kp": _matching_float(
            rows,
            field="_step5d_applied_motion_kp",
            expected=overlay.get("motion_kp", 1.5),
        ),
        "source": request.get("source"),
    }
    observed_identity.update(
        {
            "trial_uid": trial_uid,
            "backend_id": _uniform_string(
                rows, "autotune_backend_id", "capture"
            ),
        }
    )
    return candidate, observed_identity


def _analysis_contracts(
    campaign_config_path: Path,
    control_contract_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    campaign = _strict_object(campaign_config_path, "campaign config")
    control = _strict_object(control_contract_path, "control contract")
    baseline = campaign.get("baseline")
    effective = control.get("effective_fields")
    if not isinstance(baseline, dict) or not isinstance(effective, dict):
        raise PostprocessError("analysis contracts lack baseline/effective fields")
    control_invariant = effective.get("control_invariant")
    safety_invariant = effective.get("safety_invariant")
    if not isinstance(control_invariant, dict) or not isinstance(
        safety_invariant, dict
    ):
        raise PostprocessError("control contract invariants are malformed")
    normal = baseline.get("f0_shadow_reaction_normal_base")
    if (
        not isinstance(normal, list)
        or len(normal) != 3
        or any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            for value in normal
        )
    ):
        raise PostprocessError("campaign fixed-F0 reaction normal is malformed")
    target = baseline.get("target_force_n")
    control_target = control_invariant.get("target_force_n")
    if (
        isinstance(target, bool)
        or not isinstance(target, (int, float))
        or isinstance(control_target, bool)
        or not isinstance(control_target, (int, float))
        or not math.isclose(
            float(target), float(control_target), rel_tol=0.0, abs_tol=1e-12
        )
    ):
        raise PostprocessError("campaign and control target force differ")
    analysis = {
        "reaction_normal_base": [float(value) for value in normal],
        "target_force_n": float(target),
        "start_s": 5.0,
        "end_s": 60.0,
        "bin_s": 0.1,
        "angular_rate_limit_rad_s": float(
            safety_invariant["bridge_normal_max_rate_rad_s"]
        ),
        "tp_accel_limit_rad_s2": float(
            safety_invariant[
                "step5d_autotune_speedj_acceleration_rad_s2"
            ]
        ),
        "execution_profile_id": _required_string(
            control, "execution_profile_id", "control contract"
        ),
    }
    provenance = {
        "campaign_config": str(campaign_config_path.resolve(strict=True)),
        "campaign_config_sha256": _sha256_file(campaign_config_path),
        "control_contract": str(control_contract_path.resolve(strict=True)),
        "control_contract_sha256": _sha256_file(control_contract_path),
    }
    return analysis, provenance


def _write_immutable_bytes(path: Path, encoded: bytes, role: str) -> None:
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != encoded:
            raise PostprocessError(f"immutable {role} differs")
        return
    atomic_bytes(path, encoded)


def _write_immutable_json(
    path: Path, payload: Mapping[str, Any], role: str
) -> None:
    encoded = (
        json.dumps(payload, allow_nan=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    _write_immutable_bytes(path, encoded, role)


def _diagnostic_plot(
    capture_path: Path,
    plot_path: Path,
) -> tuple[str, dict[str, Any]]:
    # Keep matplotlib and plot-tool imports off the live enqueue path.
    from plot_step5d_bridge_run import main as plot_main
    from plot_step5d_bridge_run import read_series

    series = read_series(capture_path, max_points=5000)
    series.pop("times")
    series.pop("stages")
    series.pop("forces")
    if not plot_path.exists():
        with tempfile.TemporaryDirectory(
            prefix=".postprocess-plot-", dir=plot_path.parent
        ) as temporary:
            temporary_root = Path(temporary)
            rendered = temporary_root / "diagnostic.png"
            metadata = temporary_root / "diagnostic.json"
            if (
                plot_main(
                    [
                        str(capture_path),
                        "--output",
                        str(rendered),
                        "--metadata-output",
                        str(metadata),
                        "--max-points",
                        "5000",
                    ]
                )
                != 0
            ):
                raise PostprocessError("diagnostic plot renderer failed")
            _write_immutable_bytes(
                plot_path, rendered.read_bytes(), "diagnostic plot"
            )
    if plot_path.is_symlink() or not plot_path.is_file():
        raise PostprocessError("diagnostic plot is not a real regular file")
    return _sha256_file(plot_path), {
        "schema_version": "step5d_bridge_diagnostic_plot_v1",
        "claim_class": "diagnostic_only",
        "source_csv": str(capture_path),
        "plot": str(plot_path),
        **series,
    }


def process_postprocess_task(
    task_path: Path,
    *,
    outbox_root: Path,
    capture_root: Path,
    campaign_config_path: Path,
    control_contract_path: Path,
) -> Path:
    """Validate and materialize one immutable parameter outbox result."""

    outbox_root = outbox_root.resolve(strict=True)
    tasks_root = (outbox_root / "tasks").resolve(strict=True)
    task_path = _regular_file_within(task_path, tasks_root, "task")
    task = _strict_object(task_path, "postprocess task")
    if task.get("schema") != OUTBOX_SCHEMA or task.get("status") != "PENDING":
        raise PostprocessError("unsupported postprocess task schema or status")
    if tuple(task.get("operations", ())) != EXPECTED_OPERATIONS:
        raise PostprocessError("unsupported postprocess task operations")
    if task.get("optimizer_required") is not False:
        raise PostprocessError("v1 outbox consumer requires optional optimizer")

    dispatch_identity = _required_string(
        task, "dispatch_identity", "postprocess task"
    )
    task_key = hashlib.sha256(dispatch_identity.encode("utf-8")).hexdigest()
    if task_path.name != f"{task_key}.json":
        raise PostprocessError("task filename differs from dispatch identity")
    dispatch_sequence = _required_positive_int(
        task, "dispatch_sequence", "postprocess task"
    )
    campaign_root = outbox_root.parent
    expected_result_path = (
        campaign_root / "parameter_results" / f"{task_key}.json"
    ).resolve(strict=True)
    declared_result_path = _regular_file_within(
        Path(_required_string(task, "result_path", "postprocess task")),
        campaign_root / "parameter_results",
        "parameter result",
    )
    if declared_result_path != expected_result_path:
        raise PostprocessError("parameter result path differs from task identity")
    trial_result = _strict_object(declared_result_path, "parameter result")
    if (
        trial_result.get("schema")
        != "step5d.parameter-receiver/trial-result-v3"
        or trial_result.get("status") != "SUCCEEDED"
        or trial_result.get("dispatch_identity") != dispatch_identity
        or trial_result.get("dispatch_sequence") != dispatch_sequence
        or trial_result.get("trial_uid") != task.get("trial_uid")
    ):
        raise PostprocessError("parameter result identity/status differs from task")

    capture_path = _regular_file_within(
        Path(_required_string(task, "capture_path", "postprocess task")),
        capture_root,
        "capture",
    )
    if capture_path.suffix != ".csv" or capture_path.name.endswith(".part"):
        raise PostprocessError("capture must be a sealed CSV")
    result_capture = trial_result.get("capture")
    if not isinstance(result_capture, str) or Path(result_capture).resolve(
        strict=True
    ) != capture_path:
        raise PostprocessError("parameter result capture differs from task")

    dispatch_path = _regular_file_within(
        campaign_root
        / "control"
        / "parameter_receiver"
        / "dispatches"
        / f"{dispatch_sequence:012d}.json",
        campaign_root / "control" / "parameter_receiver" / "dispatches",
        "dispatch",
    )
    dispatch = _strict_object(dispatch_path, "parameter dispatch")
    if (
        dispatch.get("schema") != "step5d.parameter-receiver/dispatch-v1"
        or dispatch.get("dispatch_identity") != dispatch_identity
        or dispatch.get("dispatch_sequence") != dispatch_sequence
    ):
        raise PostprocessError("dispatch identity differs from task")
    request = dispatch.get("request")
    receipt = trial_result.get("receipt")
    if (
        not isinstance(request, dict)
        or not isinstance(receipt, dict)
        or request.get("request_uid") != trial_result.get("request_uid")
        or receipt.get("request_uid") != request.get("request_uid")
    ):
        raise PostprocessError("request/receipt identity chain is inconsistent")

    rows = _read_capture_rows(capture_path)
    candidate, observed_identity = _candidate_and_identity(
        rows, task=task, dispatch=dispatch
    )
    analysis, contract_provenance = _analysis_contracts(
        campaign_config_path, control_contract_path
    )
    overlay = request.get("overlay")
    if (
        not isinstance(overlay, dict)
        or overlay.get("execution_profile_id")
        != analysis["execution_profile_id"]
    ):
        raise PostprocessError(
            "dispatch execution profile differs from control contract"
        )

    from step5d_autotune_evaluator import fixed_f0_bin_metrics, profile_metrics

    objective = fixed_f0_bin_metrics(
        rows,
        reaction_normal_base=analysis["reaction_normal_base"],
        target_force_n=analysis["target_force_n"],
        start_s=analysis["start_s"],
        end_s=analysis["end_s"],
        bin_s=analysis["bin_s"],
    )
    profile = profile_metrics(
        rows,
        angular_rate_limit_rad_s=analysis["angular_rate_limit_rad_s"],
        tp_accel_limit_rad_s2=analysis["tp_accel_limit_rad_s2"],
    )
    plot_path = outbox_root / "plots" / f"{task_key}.png"
    plot_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    plot_sha256, plot_metadata = _diagnostic_plot(capture_path, plot_path)

    payload = {
        "schema": RESULT_SCHEMA,
        "status": "SUCCEEDED",
        "task": str(task_path),
        "task_sha256": _sha256_file(task_path),
        "dispatch_identity": dispatch_identity,
        "dispatch_sequence": dispatch_sequence,
        "dispatch": str(dispatch_path),
        "dispatch_file_sha256": _sha256_file(dispatch_path),
        "request_uid": request.get("request_uid"),
        "trial_uid": task.get("trial_uid"),
        "capture": str(capture_path),
        "capture_sha256": _sha256_file(capture_path),
        "capture_rows": len(rows),
        "parameter_result": str(declared_result_path),
        "parameter_result_sha256": _sha256_file(declared_result_path),
        "identity": observed_identity,
        "candidate": candidate,
        "analysis_contract": {**analysis, **contract_provenance},
        "objective": objective,
        "profile": profile,
        "diagnostic_plot": {
            "path": str(plot_path),
            "sha256": plot_sha256,
            "metadata": plot_metadata,
        },
        "optimizer": {
            "required": False,
            "status": "NOT_REQUESTED",
        },
    }
    result_path = outbox_root / "results" / f"{task_key}.json"
    _write_immutable_json(result_path, payload, "postprocess result")
    return result_path


def process_pending_tasks(
    *,
    outbox_root: Path,
    capture_root: Path,
    campaign_config_path: Path,
    control_contract_path: Path,
    limit: int | None = None,
) -> dict[str, Any]:
    outbox_root = outbox_root.expanduser().absolute()
    capture_root = capture_root.expanduser().absolute()
    campaign_config_path = campaign_config_path.expanduser().absolute()
    control_contract_path = control_contract_path.expanduser().absolute()
    tasks_root = outbox_root / "tasks"
    if tasks_root.is_symlink() or not tasks_root.is_dir():
        raise PostprocessError("outbox tasks root must be a real directory")
    completed: list[str] = []
    failures: list[dict[str, str]] = []
    task_paths = sorted(tasks_root.glob("*.json"))
    if limit is not None:
        task_paths = task_paths[:limit]
    for task_path in task_paths:
        try:
            completed.append(
                str(
                    process_postprocess_task(
                        task_path,
                        outbox_root=outbox_root,
                        capture_root=capture_root,
                        campaign_config_path=campaign_config_path,
                        control_contract_path=control_contract_path,
                    )
                )
            )
        except Exception as exc:
            failures.append({"task": str(task_path), "error": str(exc)})
    return {
        "schema": "step5d.parameter-receiver/postprocess-run-v1",
        "status": "SUCCEEDED" if not failures else "FAILED",
        "tasks_seen": len(task_paths),
        "completed": completed,
        "failures": failures,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outbox-root", type=Path, required=True)
    parser.add_argument("--capture-root", type=Path, required=True)
    parser.add_argument("--campaign-config", type=Path, required=True)
    parser.add_argument("--control-contract", type=Path, required=True)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be positive")
    try:
        result = process_pending_tasks(
            outbox_root=args.outbox_root,
            capture_root=args.capture_root,
            campaign_config_path=args.campaign_config,
            control_contract_path=args.control_contract,
            limit=args.limit,
        )
    except Exception as exc:
        result = {
            "schema": "step5d.parameter-receiver/postprocess-run-v1",
            "status": "FAILED",
            "tasks_seen": 0,
            "completed": [],
            "failures": [{"task": None, "error": str(exc)}],
        }
    print(json.dumps(result, allow_nan=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "SUCCEEDED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
