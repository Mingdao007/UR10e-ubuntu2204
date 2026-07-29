"""Best-effort derived postprocess queue after the immutable ACK boundary.

Failures are recorded per job and never alter v1 trial disposition, safe
closure, ACK state, or the next candidate.  Safety/structural evaluation does
not belong in this worker.
"""

from __future__ import annotations

import csv
import hashlib
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .state import atomic_json, read_strict_json, utc_now


JOB_SCHEMA = "step5d.autotune-v3.derived-job/v1"
RESULT_SCHEMA = "step5d.autotune-v3.derived-result/v1"
_TRIAL_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")


class PostprocessError(RuntimeError):
    """A derived-only postprocess request is unsafe or malformed."""


@dataclass(frozen=True)
class PostprocessRun:
    total: int
    succeeded: int
    failed: int


Analyzer = Callable[[Path, str, Path], Mapping[str, Any]]


def _finite_row(row: Mapping[str, Any], key: str) -> float | None:
    try:
        value = float(row.get(key, ""))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _orientation_mae(
    rows: list[Mapping[str, Any]], *, start_s: float, end_s: float
) -> tuple[float | None, int]:
    errors: list[float] = []
    for row in rows:
        stage = _finite_row(row, "ur_output_double_register_35")
        time_s = _finite_row(row, "_step4e_path_time_s")
        if time_s is None:
            time_s = _finite_row(row, "_step4e_line_stage_s")
        if stage is None or not math.isclose(stage, 25.0, abs_tol=0.005):
            continue
        if time_s is None or not start_s <= time_s < end_s:
            continue
        error = _finite_row(row, "_step5d_contact_orientation_error_rad")
        if error is None:
            error = _finite_row(row, "_step5d_outer_orientation_error_rad")
        if error is not None:
            errors.append(abs(error))
    return (sum(errors) / len(errors), len(errors)) if errors else (None, 0)


def _same_identity(rows: list[Mapping[str, Any]], key: str) -> str | None:
    values = {str(row.get(key, "")).strip() for row in rows}
    values.discard("")
    return next(iter(values)) if len(values) == 1 else None


def pareto_frontier(
    rows: list[Mapping[str, Any]], *, round_id: str = "round_a"
) -> dict[str, Any]:
    """Return deterministic non-dominated force/orientation candidates."""

    eligible: list[dict[str, Any]] = []
    for row in rows:
        rounds = row.get("rounds")
        metrics = rounds.get(round_id) if isinstance(rounds, Mapping) else None
        if row.get("optimizer_eligible") is not True or not isinstance(metrics, Mapping):
            continue
        force = metrics.get("force_mae_n")
        orientation = metrics.get("orientation_mae_rad")
        if not all(
            isinstance(value, (int, float)) and math.isfinite(float(value))
            for value in (force, orientation)
        ):
            continue
        eligible.append(
            {
                "control_candidate_uid": row.get("control_candidate_uid"),
                "force_mae_n": float(force),
                "orientation_mae_rad": float(orientation),
            }
        )
    eligible.sort(
        key=lambda row: (
            row["force_mae_n"],
            row["orientation_mae_rad"],
            str(row["control_candidate_uid"]),
        )
    )
    frontier = [
        row
        for row in eligible
        if not any(
            other is not row
            and other["force_mae_n"] <= row["force_mae_n"]
            and other["orientation_mae_rad"] <= row["orientation_mae_rad"]
            and (
                other["force_mae_n"] < row["force_mae_n"]
                or other["orientation_mae_rad"] < row["orientation_mae_rad"]
            )
            for other in eligible
        )
    ]
    knee = None
    if frontier:
        force_values = [row["force_mae_n"] for row in frontier]
        orientation_values = [row["orientation_mae_rad"] for row in frontier]
        force_span = max(force_values) - min(force_values)
        orientation_span = max(orientation_values) - min(orientation_values)
        knee = min(
            frontier,
            key=lambda row: (
                math.hypot(
                    0.0
                    if force_span == 0.0
                    else (row["force_mae_n"] - min(force_values)) / force_span,
                    0.0
                    if orientation_span == 0.0
                    else (
                        row["orientation_mae_rad"] - min(orientation_values)
                    )
                    / orientation_span,
                ),
                str(row["control_candidate_uid"]),
            ),
        )
    return {
        "schema": "step5d.autotune-v3/pareto-frontier/v1",
        "round_id": round_id,
        "eligible_candidate_count": len(eligible),
        "frontier": frontier,
        "recommended_knee": knee,
    }


def derive_bundle_summary(capture: Path, trial_id: str, output_dir: Path) -> Mapping[str, Any]:
    """Produce a small derived summary; never recompute acceptance or ACK."""

    del output_dir
    bundle = read_strict_json(capture, role="immutable trial bundle")
    if not isinstance(bundle, dict):
        raise PostprocessError("immutable trial bundle must be an object")
    evaluation = bundle.get("evaluation")
    if not isinstance(evaluation, Mapping):
        raise PostprocessError("immutable trial bundle lacks evaluation")
    failures = evaluation.get("structural_failures")
    if failures is not None and not isinstance(failures, list):
        raise PostprocessError("structural_failures must be a list when present")
    provenance = bundle.get("artifact_provenance")
    csv_provenance = provenance.get("csv") if isinstance(provenance, Mapping) else None
    if not isinstance(csv_provenance, Mapping):
        raise PostprocessError("immutable trial bundle lacks CSV provenance")
    csv_path = Path(str(csv_provenance.get("path", "")))
    if csv_path.is_symlink() or not csv_path.is_file():
        raise PostprocessError("immutable trial CSV is missing or unsafe")
    csv_bytes = csv_path.read_bytes()
    if hashlib.sha256(csv_bytes).hexdigest() != csv_provenance.get("sha256"):
        raise PostprocessError("immutable trial CSV hash differs")
    try:
        rows = list(csv.DictReader(csv_bytes.decode("utf-8").splitlines()))
    except (UnicodeError, csv.Error) as exc:
        raise PostprocessError(f"immutable trial CSV is malformed: {exc}") from exc
    if not rows:
        raise PostprocessError("immutable trial CSV is empty")
    from step5d_autotune_evaluator import fixed_f0_bin_metrics

    trial = bundle.get("trial")
    campaign = trial.get("campaign") if isinstance(trial, Mapping) else None
    if not isinstance(campaign, Mapping):
        raise PostprocessError("immutable trial bundle lacks campaign contract")
    normal = campaign.get("f0_shadow_reaction_normal_base")
    target_force = campaign.get("target_force_n")
    if not isinstance(normal, list) or not isinstance(target_force, (int, float)):
        raise PostprocessError("immutable trial campaign objective differs")
    rounds: dict[str, Any] = {}
    for round_id, start_s in (("round_a", 5.0), ("round_b", 0.0)):
        force = fixed_f0_bin_metrics(
            rows,
            reaction_normal_base=normal,
            target_force_n=float(target_force),
            start_s=start_s,
            end_s=60.0,
            bin_s=0.1,
        )
        orientation_mae, orientation_rows = _orientation_mae(
            rows, start_s=start_s, end_s=60.0
        )
        rounds[round_id] = {
            "window_s": [start_s, 60.0],
            "force_mae_n": force.get("mae_n"),
            "force_complete_bins": force["complete_bins"],
            "force_required_bins": force["required_bins"],
            "orientation_mae_rad": orientation_mae,
            "orientation_rows": orientation_rows,
        }
    capture_summary = bundle.get("capture")
    capture_summary = capture_summary if isinstance(capture_summary, Mapping) else {}
    allowed_derived_only_failures = {"orientation_profile_unqualified"}
    blocking_failures = sorted(set(failures or []) - allowed_derived_only_failures)
    control_uid = _same_identity(rows, "autotune_control_candidate_uid")
    exact_candidate = {
        "force_p_gain": _same_identity(rows, "autotune_force_p_gain"),
        "force_i_gain": _same_identity(rows, "autotune_force_i_gain"),
        "force_damping": _same_identity(rows, "autotune_force_damping"),
        "applied_force_p_gain": _same_identity(
            rows, "_step5d_applied_force_p_gain"
        ),
        "applied_force_i_gain": _same_identity(
            rows, "_step5d_applied_force_i_gain"
        ),
        "applied_force_damping": _same_identity(
            rows, "_step5d_applied_force_damping"
        ),
        "orientation_ko": _same_identity(rows, "autotune_orientation_ko"),
        "motion_kp": _same_identity(rows, "autotune_motion_kp"),
        "normal_filter_tau_s": _same_identity(
            rows, "autotune_normal_filter_tau_s"
        ),
        "applied_normal_filter_tau_s": _same_identity(
            rows, "_step5d_normal_filter_tau_s"
        ),
        "applied_orientation_ko": _same_identity(
            rows, "_step5d_applied_orientation_ko"
        ),
        "applied_motion_kp": _same_identity(
            rows, "_step5d_applied_motion_kp"
        ),
    }
    applied_identity_matches = all(
        requested is not None
        and applied is not None
        and math.isclose(
            float(requested),
            float(applied),
            rel_tol=0.0,
            abs_tol=1e-12,
        )
        for requested, applied in (
            (
                exact_candidate["force_p_gain"],
                exact_candidate["applied_force_p_gain"],
            ),
            (
                exact_candidate["force_i_gain"],
                exact_candidate["applied_force_i_gain"],
            ),
            (
                exact_candidate["force_damping"],
                exact_candidate["applied_force_damping"],
            ),
            (
                exact_candidate["orientation_ko"],
                exact_candidate["applied_orientation_ko"],
            ),
            (
                exact_candidate["motion_kp"],
                exact_candidate["applied_motion_kp"],
            ),
            (
                exact_candidate["normal_filter_tau_s"],
                exact_candidate["applied_normal_filter_tau_s"],
            ),
        )
    )
    round_a = rounds["round_a"]
    optimizer_eligible = bool(
        control_uid
        and all(value is not None for value in exact_candidate.values())
        and applied_identity_matches
        and not blocking_failures
        and capture_summary.get("terminal_reason") == 1
        and capture_summary.get("cadence_ok") is True
        and capture_summary.get("feedback_fresh") is True
        and capture_summary.get("rnn_oracle_aligned") is True
        and capture_summary.get("safety_normal") is True
        and capture_summary.get("returned_safe") is True
        and round_a["force_mae_n"] is not None
        and round_a["orientation_mae_rad"] is not None
    )
    return {
        "trial_id": trial_id,
        "core_eligible": evaluation.get("eligible") is True,
        "optimizer_eligible": optimizer_eligible,
        "control_candidate_uid": control_uid,
        "control_candidate": exact_candidate,
        "applied_identity_matches": applied_identity_matches,
        "rounds": rounds,
        "blocking_structural_failures": blocking_failures,
        "diagnostic_only_failures": sorted(
            set(failures or []) & allowed_derived_only_failures
        ),
        "structural_failure_count": len(failures or []),
        "safe_closure": evaluation.get("safe_closure") is True,
    }


class DerivedPostprocessQueue:
    def __init__(self, root: Path, *, allowed_capture_root: Path) -> None:
        self.root = root.expanduser().absolute()
        self.allowed_capture_root = allowed_capture_root.expanduser().absolute()
        self.pending = self.root / "pending"
        self.complete = self.root / "complete"
        self.failed = self.root / "failed"
        self.outputs = self.root / "outputs"

    def _ensure_layout(self) -> None:
        if self.root.exists() and (self.root.is_symlink() or not self.root.is_dir()):
            raise PostprocessError("postprocess root must be a real directory")
        for path in (self.pending, self.complete, self.failed, self.outputs):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            if path.is_symlink():
                raise PostprocessError("postprocess directories must not be symlinks")

    def submit(self, *, capture: Path, trial_id: str) -> str:
        if _TRIAL_ID.fullmatch(trial_id) is None:
            raise PostprocessError("trial_id is invalid")
        capture_input = capture.expanduser().absolute()
        if capture_input.is_symlink() or not capture_input.is_file():
            raise PostprocessError("immutable capture must be a real regular file")
        capture_path = capture_input.resolve(strict=True)
        try:
            capture_path.relative_to(self.allowed_capture_root.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise PostprocessError("immutable capture is outside the campaign root") from exc
        capture_sha = hashlib.sha256(capture_path.read_bytes()).hexdigest()
        job_id = hashlib.sha256(
            f"{trial_id}\0{capture_path}\0{capture_sha}".encode("utf-8")
        ).hexdigest()
        self._ensure_layout()
        payload = {
            "schema": JOB_SCHEMA,
            "job_id": job_id,
            "trial_id": trial_id,
            "capture": str(capture_path),
            "capture_sha256": capture_sha,
            "submitted_at": utc_now(),
        }
        terminal = (self.complete / f"{job_id}.json", self.failed / f"{job_id}.json")
        if any(path.exists() for path in terminal):
            return job_id
        pending = self.pending / f"{job_id}.json"
        if pending.exists():
            existing = read_strict_json(pending, role="derived postprocess job")
            comparable = dict(existing)
            comparable.pop("submitted_at", None)
            expected = dict(payload)
            expected.pop("submitted_at")
            if comparable != expected:
                raise PostprocessError("existing postprocess job identity differs")
            return job_id
        atomic_json(pending, payload)
        return job_id

    def _load_job(self, path: Path) -> Mapping[str, Any]:
        job = read_strict_json(path, role="derived postprocess job")
        required = {
            "schema",
            "job_id",
            "trial_id",
            "capture",
            "capture_sha256",
            "submitted_at",
        }
        if not isinstance(job, dict) or set(job) != required or job["schema"] != JOB_SCHEMA:
            raise PostprocessError("derived postprocess job schema differs")
        if path.name != f"{job['job_id']}.json":
            raise PostprocessError("derived postprocess filename differs from job id")
        capture = Path(str(job["capture"]))
        if capture.is_symlink() or not capture.is_file():
            raise PostprocessError("derived postprocess capture is missing or unsafe")
        if hashlib.sha256(capture.read_bytes()).hexdigest() != job["capture_sha256"]:
            raise PostprocessError("immutable capture changed after queueing")
        return job

    def run_pending(
        self,
        *,
        analyzer: Analyzer = derive_bundle_summary,
        limit: int | None = None,
    ) -> PostprocessRun:
        self._ensure_layout()
        paths = sorted(self.pending.glob("*.json"))
        if limit is not None:
            if type(limit) is not int or limit < 0:
                raise PostprocessError("postprocess limit must be non-negative")
            paths = paths[:limit]
        succeeded = failed = 0
        for path in paths:
            job_id = path.stem
            complete_path = self.complete / path.name
            failed_path = self.failed / path.name
            if complete_path.exists() or failed_path.exists():
                path.unlink(missing_ok=True)
                continue
            try:
                job = self._load_job(path)
                output_dir = self.outputs / job_id
                output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                result = analyzer(Path(job["capture"]), str(job["trial_id"]), output_dir)
                if not isinstance(result, Mapping):
                    raise PostprocessError("derived analyzer must return a mapping")
                atomic_json(
                    complete_path,
                    {
                        "schema": RESULT_SCHEMA,
                        "job_id": job_id,
                        "status": "complete",
                        "finished_at": utc_now(),
                        "result": dict(result),
                    },
                )
                succeeded += 1
            except Exception as exc:
                atomic_json(
                    failed_path,
                    {
                        "schema": RESULT_SCHEMA,
                        "job_id": job_id,
                        "status": "analysis_failed",
                        "finished_at": utc_now(),
                        "error": f"{type(exc).__name__}:{exc}",
                    },
                )
                failed += 1
            finally:
                path.unlink(missing_ok=True)
        if paths:
            directory_fd = os.open(self.pending, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        return PostprocessRun(total=len(paths), succeeded=succeeded, failed=failed)
