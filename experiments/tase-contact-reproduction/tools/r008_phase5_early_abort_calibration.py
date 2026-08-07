#!/usr/bin/env python3
"""Offline backtest for Andy's early-abort idea (2026-08-07 design round).

Andy's proposal (v1): partway through a PATH60 attempt, if the running
partial MAE is already >= kappa x the best-known sealed MAE, abort back to
HOME instead of running the full 60s. Round 2 (2026-08-07, this file):
compare several trigger *policies*, not just one fixed checkpoint --

  - fixed:  check once at a fixed progress fraction through the formal
            window (Andy's original "50% + kappa" framing).
  - any:    monitor continuously from a guard fraction onward, fire at the
            first crossing (Andy's "any time >= kappa" framing). A guard
            fraction and an optional "sustain" (must stay above threshold
            for N consecutive trace points, not just one noisy sample) avoid
            killing on a transient onset spike before the filter settles.
  - decay:  Claude's proposed third option -- a time-varying threshold that
            starts loose (early samples are noisier, fewer averaged) and
            tightens toward the end of the window, instead of one constant
            kappa for the whole attempt.

This replays the REAL raw PATH60 samples of every already-sealed attempt in
a run directory, using the causal (chronological, attempt_sequence-ordered)
rolling-best MAE that would actually have been known at decision time.

Read-only. Never touches the live host, never writes into the run directory.
Uses a calibration-local running mean of |force - target| over formal-window
samples (same statistic as the live early-abort hook) for partial MAE traces.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_force_objective import FORMAL_END_S, FORMAL_START_S, TARGET_FORCE_N  # noqa: E402
from step5d_autotune_v4_r008.early_abort_kappa import sigmoid_kappa  # noqa: E402
from step5d_autotune_v4_r008.raw_force_binary_v2 import detect_and_decode  # noqa: E402

# A real outcome this much worse than best-so-far still counts as "decent"
# (not just strictly-better-than-best) when judging false-abort harm.
DECENT_MULTIPLE = 1.2
TRACE_STEP_FRACTION = 0.02  # 2% of the formal window per trace point (~1.1s)


def _load_ledger_rows(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "r006-observations.jsonl"
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        fo = row.get("force_objective")
        if not isinstance(fo, dict):
            continue
        mae = fo.get("force_mae_v2")
        if not isinstance(mae, (int, float)):
            continue
        rows.append(row)
    rows.sort(key=lambda r: int(r["attempt_sequence"]))
    return rows


def _load_sidecar_index(run_dir: Path) -> dict[int, dict[str, Any]]:
    path = run_dir / "r006-observations-r006-objectives.jsonl"
    index: dict[int, dict[str, Any]] = {}
    if not path.is_file():
        return index
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        seq = row.get("attempt_sequence")
        if isinstance(seq, int):
            index[seq] = row
    return index


def _raw_samples_for(run_dir: Path, sidecar_row: dict[str, Any]) -> list[dict[str, Any]] | None:
    artifact_name = sidecar_row.get("artifact_name")
    if not isinstance(artifact_name, str):
        return None
    stub = run_dir / "r006_raw_objectives" / artifact_name
    raw_path = stub.with_suffix(".r008raw")
    if not raw_path.is_file():
        return None
    payload = raw_path.read_bytes()
    try:
        return detect_and_decode(payload, include_audit=False)
    except Exception:  # noqa: BLE001 -- skip unreadable artifacts, don't crash the backtest
        return None


def _running_partial_mae_n(partial_abs_error_sum: float, partial_count: int) -> float | None:
    if partial_count <= 0:
        return None
    return partial_abs_error_sum / float(partial_count)


def _partial_mae_trace(
    samples: list[dict[str, Any]], *, step_fraction: float = TRACE_STEP_FRACTION
) -> list[tuple[float, float | None]]:
    """[(progress_fraction, partial_mae_n as of that point), ...] at regular steps.

    One pass over the raw samples regardless of how many policies later read
    this trace -- decoding+replaying 500+ attempts is the expensive part.
    """

    samples = sorted(samples, key=lambda s: s["path_time_s"])
    window_s = FORMAL_END_S - FORMAL_START_S
    n_steps = int(round(1.0 / step_fraction))
    fractions = [round((i + 1) * step_fraction, 6) for i in range(n_steps)]
    checkpoints_s = [FORMAL_START_S + frac * window_s for frac in fractions]

    trace: list[tuple[float, float | None]] = []
    partial_abs_error_sum = 0.0
    partial_count = 0
    step_idx = 0
    for row in samples:
        t = row["path_time_s"]
        while step_idx < len(checkpoints_s) and t >= checkpoints_s[step_idx]:
            trace.append(
                (
                    fractions[step_idx],
                    _running_partial_mae_n(partial_abs_error_sum, partial_count),
                )
            )
            step_idx += 1
        if step_idx >= len(checkpoints_s):
            break
        if FORMAL_START_S <= float(t) < FORMAL_END_S:
            try:
                force_n = float(row["filtered_normal_n"])
            except (KeyError, TypeError, ValueError):
                force_n = float("nan")
            if math.isfinite(force_n):
                partial_abs_error_sum += abs(force_n - TARGET_FORCE_N)
                partial_count += 1
    while step_idx < len(checkpoints_s):
        trace.append(
            (
                fractions[step_idx],
                _running_partial_mae_n(partial_abs_error_sum, partial_count),
            )
        )
        step_idx += 1
    return trace


# --- policies -----------------------------------------------------------
# Each policy takes (trace, best_so_far_mae_n) and returns the progress
# fraction at which it would fire, or None if it never fires.

def policy_fixed(frac: float, kappa: float) -> Callable[..., float | None]:
    def _fn(trace: list[tuple[float, float | None]], best: float) -> float | None:
        for f, partial in trace:
            if f >= frac and partial is not None:
                return f if partial >= kappa * best else None
        return None

    return _fn


def policy_any_time(
    guard_frac: float, kappa: float, *, sustain_points: int = 1
) -> Callable[..., float | None]:
    def _fn(trace: list[tuple[float, float | None]], best: float) -> float | None:
        streak = 0
        for f, partial in trace:
            if f < guard_frac or partial is None:
                streak = 0
                continue
            if partial >= kappa * best:
                streak += 1
                if streak >= sustain_points:
                    return f
            else:
                streak = 0
        return None

    return _fn


def policy_decay(
    guard_frac: float, kappa_start: float, kappa_end: float
) -> Callable[..., float | None]:
    """Linear ramp from kappa_start (right after guard) to kappa_end (end of window)."""

    def _fn(trace: list[tuple[float, float | None]], best: float) -> float | None:
        for f, partial in trace:
            if f < guard_frac or partial is None:
                continue
            span = max(1e-9, 1.0 - guard_frac)
            progress_in_span = min(1.0, (f - guard_frac) / span)
            kappa = kappa_start + (kappa_end - kappa_start) * progress_in_span
            if partial >= kappa * best:
                return f
        return None

    return _fn


def policy_decay_sigmoid(
    guard_frac: float,
    kappa_start: float,
    kappa_end: float,
    *,
    midpoint: float = 0.5,
    steepness: float = 10.0,
) -> Callable[..., float | None]:
    """Inverse-sigmoid ramp: stays near kappa_start, drops sharply around
    ``midpoint`` (fraction of the post-guard span), flattens near kappa_end.

    ``steepness`` controls how sharp the drop is (bigger = sharper); 8-12 is
    a fairly sharp transition over roughly the middle third of the span.
    """

    def _fn(trace: list[tuple[float, float | None]], best: float) -> float | None:
        for f, partial in trace:
            if f < guard_frac or partial is None:
                continue
            kappa = sigmoid_kappa(
                f,
                guard_frac=guard_frac,
                kappa_start=kappa_start,
                kappa_end=kappa_end,
                midpoint=midpoint,
                steepness=steepness,
            )
            if partial >= kappa * best:
                return f
        return None

    return _fn


def run_backtest(
    run_dir: Path, *, policies: dict[str, Callable[..., float | None]]
) -> dict[str, Any]:
    ledger_rows = _load_ledger_rows(run_dir)
    sidecar_index = _load_sidecar_index(run_dir)

    evaluated: list[dict[str, Any]] = []
    best_so_far: float | None = None
    skipped_no_artifact = 0

    for row in ledger_rows:
        seq = int(row["attempt_sequence"])
        real_mae = float(row["force_objective"]["force_mae_v2"])
        sidecar_row = sidecar_index.get(seq)
        if best_so_far is not None and sidecar_row is not None:
            samples = _raw_samples_for(run_dir, sidecar_row)
            if samples:
                trace = _partial_mae_trace(samples)
                evaluated.append(
                    {
                        "attempt_sequence": seq,
                        "kind": row.get("kind"),
                        "real_mae_n": real_mae,
                        "best_so_far_mae_n": best_so_far,
                        "trace": trace,
                    }
                )
            else:
                skipped_no_artifact += 1
        best_so_far = real_mae if best_so_far is None else min(best_so_far, real_mae)

    window_s = FORMAL_END_S - FORMAL_START_S
    results: dict[str, Any] = {}
    for name, policy in policies.items():
        would_abort = 0
        false_new_best = 0
        false_decent = 0
        correct_bad = 0
        time_saved_s_sum = 0.0
        for item in evaluated:
            trigger_frac = policy(item["trace"], item["best_so_far_mae_n"])
            if trigger_frac is None:
                continue
            would_abort += 1
            time_saved_s_sum += (1.0 - trigger_frac) * window_s
            if item["real_mae_n"] < item["best_so_far_mae_n"]:
                false_new_best += 1
            elif item["real_mae_n"] <= DECENT_MULTIPLE * item["best_so_far_mae_n"]:
                false_decent += 1
            else:
                correct_bad += 1
        n = len(evaluated)
        results[name] = {
            "n_evaluated": n,
            "would_abort": would_abort,
            "would_abort_rate": (would_abort / n) if n else None,
            "false_abort_new_best": false_new_best,
            "false_abort_decent": false_decent,
            "correctly_skipped_bad": correct_bad,
            "mean_time_saved_s_per_abort": (
                time_saved_s_sum / would_abort if would_abort else None
            ),
        }

    return {
        "run_dir": str(run_dir),
        "n_ledger_rows_with_force_objective": len(ledger_rows),
        "n_evaluated_with_raw_artifact": len(evaluated),
        "n_skipped_no_artifact": skipped_no_artifact,
        "policies": results,
    }


def _default_policies() -> dict[str, Callable[..., float | None]]:
    return {
        "fixed_50pct_k2.0 (Andy v1)": policy_fixed(0.5, 2.0),
        "fixed_50pct_k1.5 (Andy variant2)": policy_fixed(0.5, 1.5),
        "any_time_guard10%_k2.0_1sample (Andy variant1, no debounce)": policy_any_time(
            0.10, 2.0, sustain_points=1
        ),
        "any_time_guard10%_k2.0_sustain3 (debounced ~3s)": policy_any_time(
            0.10, 2.0, sustain_points=3
        ),
        "decay_guard10%_k3.0to1.5 (Claude proposal)": policy_decay(0.10, 3.0, 1.5),
        "decay_guard10%_k2.5to1.3 (Claude proposal, tighter)": policy_decay(0.10, 2.5, 1.3),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    result = run_backtest(args.run_dir.resolve(), policies=_default_policies())

    if args.json:
        print(json.dumps(result, sort_keys=True, default=str))
        return 0

    print(f"run_dir: {result['run_dir']}")
    print(
        f"sealed rows: {result['n_ledger_rows_with_force_objective']}  "
        f"evaluated (has causal best + raw artifact): {result['n_evaluated_with_raw_artifact']}  "
        f"skipped (no artifact): {result['n_skipped_no_artifact']}"
    )
    print()
    header = (
        f"{'policy':<58} {'n':>4} {'abort%':>7} "
        f"{'false=best':>10} {'false=decent':>12} {'correct_bad':>11} {'~saved_s':>9}"
    )
    print(header)
    for name, g in result["policies"].items():
        rate = f"{g['would_abort_rate'] * 100:.1f}%" if g["would_abort_rate"] is not None else "n/a"
        saved = (
            f"{g['mean_time_saved_s_per_abort']:.1f}"
            if g["mean_time_saved_s_per_abort"] is not None
            else "n/a"
        )
        print(
            f"{name:<58} {g['n_evaluated']:>4} {rate:>7} "
            f"{g['false_abort_new_best']:>10} {g['false_abort_decent']:>12} "
            f"{g['correctly_skipped_bad']:>11} {saved:>9}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
