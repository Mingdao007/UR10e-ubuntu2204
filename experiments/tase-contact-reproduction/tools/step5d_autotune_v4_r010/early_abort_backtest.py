"""Formal-bin R010 early-abort shadow backtest over sealed raw artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from step5d_force_objective import (
    FORMAL_END_S,
    FORMAL_START_S,
    REQUIRED_BINS,
    TARGET_FORCE_N,
    ForceObjectiveBuilder,
)
from step5d_autotune_v4_r008.raw_force_binary_v2 import detect_and_decode

from .behavior import EARLY_ABORT_SIGMOID_SEMANTICS, early_abort_kappa
from .gp_calibration import AdmissionResult, CalibrationRow, admit_phase5_ledger, sha256_file


EARLY_ABORT_BACKTEST_SCHEMA = "step5d.autotune-v4/r010-early-abort-backtest-v1"
TRACE_STEP_FRACTION = 0.02
DECENT_MULTIPLE = 1.2


class R010EarlyAbortBacktestError(ValueError):
    """Raw historical evidence cannot support the formal-bin backtest."""


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise R010EarlyAbortBacktestError(
            f"early-abort report is not canonical JSON: {exc}"
        ) from exc


def _strict_sidecar_index(path: Path, campaign_fingerprint: str) -> dict[int, Mapping[str, Any]]:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise R010EarlyAbortBacktestError("objective sidecar is missing or unsafe")
    index: dict[int, Mapping[str, Any]] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            raise R010EarlyAbortBacktestError(f"objective sidecar blank line {line_number}")
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise R010EarlyAbortBacktestError(
                f"objective sidecar line {line_number} is invalid"
            ) from exc
        if row.get("record_type") == "header":
            continue
        sequence = row.get("attempt_sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
            raise R010EarlyAbortBacktestError("objective sidecar sequence is invalid")
        if sequence in index:
            raise R010EarlyAbortBacktestError("objective sidecar sequence repeats")
        if row.get("campaign_fingerprint") != campaign_fingerprint:
            raise R010EarlyAbortBacktestError("objective sidecar campaign differs")
        artifact_name = row.get("artifact_name")
        if (
            not isinstance(artifact_name, str)
            or Path(artifact_name).name != artifact_name
            or not artifact_name.endswith(".json")
        ):
            raise R010EarlyAbortBacktestError("objective artifact name is unsafe")
        index[sequence] = row
    return index


def formal_partial_trace(
    samples: Sequence[Mapping[str, Any]],
    *,
    step_fraction: float = TRACE_STEP_FRACTION,
) -> list[dict[str, Any]]:
    if not math.isclose(step_fraction, TRACE_STEP_FRACTION, rel_tol=0.0, abs_tol=0.0):
        raise R010EarlyAbortBacktestError("R010 backtest trace step differs")
    bins: list[list[float]] = [[] for _ in range(REQUIRED_BINS)]
    last_path_time_s: float | None = None
    for sample in samples:
        try:
            path_time_s = float(sample["path_time_s"])
            force_n = float(sample["filtered_normal_n"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise R010EarlyAbortBacktestError("decoded raw sample fields differ") from exc
        if not math.isfinite(path_time_s) or not math.isfinite(force_n):
            raise R010EarlyAbortBacktestError("decoded raw sample is nonfinite")
        if last_path_time_s is not None and path_time_s < last_path_time_s:
            raise R010EarlyAbortBacktestError("decoded raw PATH time regressed")
        last_path_time_s = path_time_s
        if any(sample.get(key) != 25 for key in ("path_phase", "stage", "path_stage")):
            raise R010EarlyAbortBacktestError("decoded raw sample is not PATH stage 25")
        bin_index = ForceObjectiveBuilder._bin(  # noqa: SLF001 - canonical objective formula
            path_time_s,
            FORMAL_START_S,
            FORMAL_END_S,
        )
        if bin_index is not None:
            bins[bin_index].append(force_n)
    bin_errors = [
        abs(math.fsum(values) / len(values) - TARGET_FORCE_N) if values else None
        for values in bins
    ]
    trace: list[dict[str, Any]] = []
    steps = int(round(1.0 / step_fraction))
    formal_span = FORMAL_END_S - FORMAL_START_S
    for index in range(steps):
        progress = round((index + 1) * step_fraction, 12)
        watermark = FORMAL_START_S + progress * formal_span
        open_bin = ForceObjectiveBuilder._bin(  # noqa: SLF001 - canonical watermark formula
            watermark,
            FORMAL_START_S,
            FORMAL_END_S,
        )
        closed = REQUIRED_BINS if watermark >= FORMAL_END_S else int(open_bin or 0)
        observed = sum(1 for value in bin_errors[:closed] if value is not None)
        numerator = math.fsum(
            value for value in bin_errors[:closed] if value is not None
        )
        final_mae = (
            math.fsum(float(value) for value in bin_errors) / REQUIRED_BINS
            if closed == REQUIRED_BINS and all(value is not None for value in bin_errors)
            else None
        )
        trace.append(
            {
                "progress_fraction": progress,
                "partial_mae_n": numerator / REQUIRED_BINS,
                "closed_bin_count": closed,
                "observed_closed_bins": observed,
                "formal_mae_n": final_mae,
            }
        )
    return trace


def evaluate_formal_trace(
    trace: Sequence[Mapping[str, Any]],
    *,
    best_so_far_n: float,
    real_mae_n: float,
) -> dict[str, Any]:
    if min(best_so_far_n, real_mae_n) < 0.0 or not all(
        math.isfinite(value) for value in (best_so_far_n, real_mae_n)
    ):
        raise R010EarlyAbortBacktestError("early-abort objective values are invalid")
    guard = float(EARLY_ABORT_SIGMOID_SEMANTICS["guard_fraction"])
    minimum_bins = int(EARLY_ABORT_SIGMOID_SEMANTICS["minimum_complete_bins"])
    trigger: Mapping[str, Any] | None = None
    for point in trace:
        progress = float(point["progress_fraction"])
        partial = float(point["partial_mae_n"])
        observed = int(point["observed_closed_bins"])
        if progress < guard or observed < minimum_bins:
            continue
        kappa = early_abort_kappa(progress)
        if partial >= kappa * best_so_far_n:
            trigger = {
                "progress_fraction": progress,
                "partial_mae_n": partial,
                "kappa": kappa,
                "threshold_n": kappa * best_so_far_n,
                "observed_closed_bins": observed,
            }
            break
    would_abort = trigger is not None
    return {
        "would_abort": would_abort,
        "trigger": None if trigger is None else dict(trigger),
        "false_abort_new_best": would_abort and real_mae_n < best_so_far_n,
        "false_abort_decent": (
            would_abort
            and real_mae_n >= best_so_far_n
            and real_mae_n <= DECENT_MULTIPLE * best_so_far_n
        ),
        "correctly_skipped_bad": would_abort and real_mae_n > DECENT_MULTIPLE * best_so_far_n,
        "time_saved_s": (
            0.0
            if trigger is None
            else (1.0 - float(trigger["progress_fraction"]))
            * (FORMAL_END_S - FORMAL_START_S)
        ),
    }


def build_early_abort_backtest(
    run_dir: Path,
    admission: AdmissionResult,
) -> dict[str, Any]:
    run_dir = Path(run_dir)
    if run_dir.is_symlink() or not run_dir.is_dir():
        raise R010EarlyAbortBacktestError("Phase5 run directory is missing or unsafe")
    if not isinstance(admission, AdmissionResult) or not admission.rows:
        raise R010EarlyAbortBacktestError("early-abort backtest requires admitted rows")
    campaign = admission.rows[0].campaign_fingerprint
    sidecar_path = run_dir / "r006-observations-r006-objectives.jsonl"
    sidecar = _strict_sidecar_index(sidecar_path, campaign)
    raw_root = run_dir / "r006_raw_objectives"
    evaluated: list[dict[str, Any]] = []
    best_so_far: float | None = None
    raw_bytes = 0
    for row in sorted(admission.rows, key=lambda item: item.attempt_sequence):
        if best_so_far is None:
            best_so_far = row.objective_n
            continue
        sidecar_row = sidecar.get(row.attempt_sequence)
        if sidecar_row is None:
            raise R010EarlyAbortBacktestError(
                f"admitted attempt {row.attempt_sequence} lacks an objective artifact"
            )
        raw_path = (raw_root / str(sidecar_row["artifact_name"])).with_suffix(".r008raw")
        if raw_path.is_symlink() or not raw_path.is_file():
            raise R010EarlyAbortBacktestError(
                f"admitted attempt {row.attempt_sequence} raw artifact is missing"
            )
        raw_bytes += raw_path.stat().st_size
        samples = detect_and_decode(raw_path.read_bytes(), include_audit=False)
        trace = formal_partial_trace(samples)
        final_mae = trace[-1]["formal_mae_n"]
        if final_mae is None or not math.isclose(
            float(final_mae), row.objective_n, rel_tol=0.0, abs_tol=1e-12
        ):
            raise R010EarlyAbortBacktestError(
                f"admitted attempt {row.attempt_sequence} formal objective differs"
            )
        outcome = evaluate_formal_trace(
            trace,
            best_so_far_n=best_so_far,
            real_mae_n=row.objective_n,
        )
        evaluated.append(
            {
                "attempt_sequence": row.attempt_sequence,
                "kind": row.kind,
                "real_mae_n": row.objective_n,
                "best_so_far_n": best_so_far,
                **outcome,
            }
        )
        best_so_far = min(best_so_far, row.objective_n)

    aborted = [row for row in evaluated if row["would_abort"]]
    report: dict[str, Any] = {
        "schema": EARLY_ABORT_BACKTEST_SCHEMA,
        "source": {
            "ledger_sha256": admission.input_sha256,
            "dataset_sha256": admission.dataset_sha256,
            "objective_sidecar_sha256": sha256_file(sidecar_path),
            "campaign_fingerprint": campaign,
            "admitted_rows": len(admission.rows),
            "evaluated_rows": len(evaluated),
            "raw_artifact_bytes_read": raw_bytes,
        },
        "policy": {
            **dict(EARLY_ABORT_SIGMOID_SEMANTICS),
            "metric": "causal_0.1s_equal_bin_formal_denominator",
            "trace_step_fraction": TRACE_STEP_FRACTION,
            "decent_multiple": DECENT_MULTIPLE,
        },
        "result": {
            "would_abort": len(aborted),
            "would_abort_rate": len(aborted) / len(evaluated) if evaluated else 0.0,
            "false_abort_new_best": sum(row["false_abort_new_best"] for row in evaluated),
            "false_abort_decent": sum(row["false_abort_decent"] for row in evaluated),
            "correctly_skipped_bad": sum(row["correctly_skipped_bad"] for row in evaluated),
            "total_time_saved_s": math.fsum(row["time_saved_s"] for row in evaluated),
            "mean_time_saved_s_per_abort": (
                math.fsum(row["time_saved_s"] for row in aborted) / len(aborted)
                if aborted
                else None
            ),
        },
        "triggered_attempts": [
            {
                "attempt_sequence": row["attempt_sequence"],
                "kind": row["kind"],
                "real_mae_n": row["real_mae_n"],
                "best_so_far_n": row["best_so_far_n"],
                "trigger": row["trigger"],
                "false_abort_new_best": row["false_abort_new_best"],
                "false_abort_decent": row["false_abort_decent"],
            }
            for row in aborted
        ],
        "boundary": {
            "shadow_only": True,
            "enters_control": False,
            "enters_gp_training": False,
            "enters_ledger": False,
            "active_promotion_allowed": False,
        },
    }
    report["report_sha256"] = hashlib.sha256(_canonical_bytes(report)).hexdigest()
    return report


def analyze_phase5_early_abort(run_dir: Path) -> dict[str, Any]:
    ledger = Path(run_dir) / "r006-observations.jsonl"
    admission = admit_phase5_ledger(ledger)
    return build_early_abort_backtest(run_dir, admission)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    print(json.dumps(analyze_phase5_early_abort(args.run_dir), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EARLY_ABORT_BACKTEST_SCHEMA",
    "R010EarlyAbortBacktestError",
    "analyze_phase5_early_abort",
    "build_early_abort_backtest",
    "evaluate_formal_trace",
    "formal_partial_trace",
]
