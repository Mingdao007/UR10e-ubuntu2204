"""Ledger-authoritative, causal, streaming R011 early-end backtest."""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from step5d_force_objective import FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT

from .censor import CensorProtocol, evaluate_censor_prefix
from .common import canonical_bytes, finite
from .offline_checksums import digest


DEFAULT_TRACE_PATH = Path("/home/andy/.codex-worktrees/step5d-v4-r004-20260801/experiments/tase-contact-reproduction/runs/step5d_autotune_v4_r008/live_20260807_025535_b3_phase5_raw2/r008-state25-path-trace.jsonl")
DEFAULT_ADMISSION_PATH = DEFAULT_TRACE_PATH.with_name("r006-observations.jsonl")
BACKTEST_SCHEMA = "step5d.autotune-v4/r011-early-end-backtest-v3"
FORMAL_BINS = 550
TARGET_FORCE_N = 5.0
CONFIRMATION_REPEATS = 3


def _physical_key(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(candidate.get(name) for name in ("force_p_gain", "force_damping", "force_i_gain", "i_off", "normal_filter_tau_s", "orientation_ko", "motion_kp", "target_force_n"))


@dataclass(frozen=True)
class TraceAudit:
    present: bool
    sample_count: int
    duration_s: float | None


@dataclass(frozen=True)
class ReplayAttempt:
    attempt_ordinal: int
    absolute_errors_n: tuple[float, ...]
    candidate_key: Any
    run_kind: str = "BO_TRIAL"
    eligible: bool = True
    duration_s: float | None = None
    full_mean_n: float | None = None
    total_bin_count: int | None = FORMAL_BINS
    missing_trace: bool = False
    candidate: Mapping[str, Any] | None = None
    objective_semantic_fingerprint: str = FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT
    trace_sample_count: int = 0

    def __post_init__(self) -> None:
        if self.attempt_ordinal <= 0 or (not self.absolute_errors_n and not self.missing_trace):
            raise ValueError("replay attempt is empty")
        values = tuple(finite(value, "formal bin absolute error") for value in self.absolute_errors_n)
        if any(value < 0.0 for value in values):
            raise ValueError("formal bin absolute error cannot be negative")
        if values and len(values) != FORMAL_BINS:
            raise ValueError("ledger-authoritative replay requires 550 formal bins")
        if self.objective_semantic_fingerprint != FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT:
            raise ValueError("replay objective fingerprint differs")
        if self.full_mean_n is not None and finite(self.full_mean_n, "full_mean_n") < 0.0:
            raise ValueError("full_mean_n cannot be negative")
        if self.total_bin_count is not None and self.total_bin_count != FORMAL_BINS:
            raise ValueError("formal denominator must be 550")
        object.__setattr__(self, "absolute_errors_n", values)


@dataclass(frozen=True)
class EarlyEndBacktestReceipt:
    attempt_count: int
    eligible_attempts: int
    eligible_exact_attempts: int
    trigger_count: int
    trigger_rate: float
    trigger_bins: tuple[int, ...]
    median_trigger_progress: float | None
    estimated_time_saved_s: float
    saved_time_bound_s: float
    false_new_best_count: int
    false_confirmed_challenger_count: int
    final_confirmed_incumbent_with_early_end_n: float | None
    final_confirmed_incumbent_without_early_end_n: float | None
    seed_confirmed_incumbent_n: float | None
    exact_agreement: bool
    trace_sha256: str
    trace_audit_sha256: str
    trace_ordinals_present: int
    trace_sample_count: int
    trace_attempt_duration_count: int
    schema: str = BACKTEST_SCHEMA

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "objective_fingerprint": FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT,
            "attempt_count": self.attempt_count, "eligible_attempts": self.eligible_attempts,
            "eligible_exact_attempts": self.eligible_exact_attempts, "trigger_count": self.trigger_count,
            "trigger_rate": self.trigger_rate, "trigger_bins": list(self.trigger_bins),
            "median_trigger_progress": self.median_trigger_progress,
            "estimated_time_saved_s": self.estimated_time_saved_s, "saved_time_bound_s": self.saved_time_bound_s,
            "false_new_best_count": self.false_new_best_count,
            "false_confirmed_challenger_count": self.false_confirmed_challenger_count,
            "final_confirmed_incumbent_with_early_end_n": self.final_confirmed_incumbent_with_early_end_n,
            "final_confirmed_incumbent_without_early_end_n": self.final_confirmed_incumbent_without_early_end_n,
            "seed_confirmed_incumbent_n": self.seed_confirmed_incumbent_n,
            "exact_agreement": self.exact_agreement, "trace_sha256": self.trace_sha256,
            "trace_audit_sha256": self.trace_audit_sha256, "trace_ordinals_present": self.trace_ordinals_present,
            "trace_sample_count": self.trace_sample_count, "trace_attempt_duration_count": self.trace_attempt_duration_count,
        }


def _read_ledger(path: Path) -> tuple[dict[int, ReplayAttempt], tuple[int, ...]]:
    rows: dict[int, ReplayAttempt] = {}
    ineligible: list[int] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            raw = json.loads(line)
            if raw.get("record_type") == "header":
                continue
            if raw.get("record_type") != "observation":
                raise ValueError("R006 ledger contains an unknown record type")
            ordinal = int(raw["attempt_sequence"])
            candidate = dict(raw["candidate"])
            eligible = raw.get("eligible") is True and raw.get("sealed") is True
            force_objective = raw.get("force_objective")
            if not eligible and not isinstance(force_objective, Mapping):
                rows[ordinal] = ReplayAttempt(ordinal, (), _physical_key(candidate), str(raw["kind"]), False, raw.get("duration_s"), None, None, True, candidate, FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT, 0)
                ineligible.append(ordinal)
                continue
            if not isinstance(force_objective, Mapping) or force_objective.get("semantic_fingerprint") != FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT:
                raise ValueError(f"attempt {ordinal} lacks the sealed R005 objective fingerprint")
            sums = force_objective.get("formal_bin_sum_n")
            counts = force_objective.get("formal_bin_count")
            if not isinstance(sums, list) or not isinstance(counts, list) or len(sums) != FORMAL_BINS or len(counts) != FORMAL_BINS or any(int(count) <= 0 for count in counts):
                raise ValueError(f"attempt {ordinal} lacks 550 complete formal sufficient-statistic bins")
            errors = tuple(abs(float(total) / int(count) - TARGET_FORCE_N) for total, count in zip(sums, counts))
            objective = float(force_objective["v2_mae_n"])
            computed = math_fsum(errors) / FORMAL_BINS
            if abs(computed - objective) > 1e-12 or abs(float(raw.get("mae_n", objective)) - objective) > 1e-12:
                raise ValueError(f"attempt {ordinal} sealed objective is not byte-semantic equivalent to formal bins")
            if not eligible:
                ineligible.append(ordinal)
            rows[ordinal] = ReplayAttempt(
                ordinal, errors, _physical_key(candidate), str(raw["kind"]), eligible,
                float(raw["duration_s"]) if raw.get("duration_s") is not None else None,
                objective, FORMAL_BINS, False, candidate, FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT,
            )
    if len(rows) != 583:
        raise ValueError(f"R006 authority row count is {len(rows)}, expected 583")
    return rows, tuple(sorted(ineligible))


def _stream_trace(path: Path) -> tuple[str, dict[int, TraceAudit], int, int]:
    """Audit raw trace bytes in one pass, retaining only one record per attempt."""

    sha = hashlib.sha256()
    audits: dict[int, TraceAudit] = {}
    current: int | None = None
    count = 0
    first_time: float | None = None
    last_time: float | None = None
    total_samples = 0

    def flush() -> None:
        nonlocal count, first_time, last_time, current
        if current is not None:
            audits[current] = TraceAudit(True, count, None if first_time is None or last_time is None else max(0.0, last_time - first_time))
        count = 0; first_time = None; last_time = None

    try:
        import orjson
        decode = orjson.loads
    except ImportError:
        decode = json.loads
    with Path(path).open("rb", buffering=1024 * 1024) as handle:
        for line in handle:
            sha.update(line)
            if not line.strip():
                continue
            row = decode(line)
            ordinal = int(row["attempt_ordinal"])
            if current is None:
                current = ordinal
            elif ordinal != current:
                flush(); current = ordinal
            count += 1; total_samples += 1
            value = row.get("wall_time_s")
            if value is not None:
                timestamp = float(value)
                first_time = timestamp if first_time is None else min(first_time, timestamp)
                last_time = timestamp if last_time is None else max(last_time, timestamp)
    flush()
    return sha.hexdigest(), audits, len(audits), total_samples


def math_fsum(values: Sequence[float]) -> float:
    return math.fsum(values)


def _assemble_attempts(ledger: Mapping[int, ReplayAttempt], audits: Mapping[int, TraceAudit]) -> tuple[ReplayAttempt, ...]:
    attempts = []
    for ordinal in sorted(ledger):
        item = ledger[ordinal]
        audit = audits.get(ordinal, TraceAudit(False, 0, None))
        attempts.append(ReplayAttempt(item.attempt_ordinal, item.absolute_errors_n, item.candidate_key, item.run_kind, item.eligible, audit.duration_s if audit.present else item.duration_s, item.full_mean_n, FORMAL_BINS if not item.missing_trace else None, item.missing_trace or not audit.present, item.candidate, item.objective_semantic_fingerprint, audit.sample_count))
    return tuple(attempts)


def load_r008_path_trace(path: Path = DEFAULT_TRACE_PATH, *, target_force_n: float = TARGET_FORCE_N, admission_path: Path | None = DEFAULT_ADMISSION_PATH) -> tuple[ReplayAttempt, ...]:
    del target_force_n  # The sealed R005 authority is always target 5 N.
    if Path(path).is_symlink() or not Path(path).is_file() or admission_path is None:
        raise FileNotFoundError(path)
    ledger, _ = _read_ledger(Path(admission_path))
    _trace_sha, audits, _present_count, _sample_count = _stream_trace(Path(path))
    return _assemble_attempts(ledger, audits)


def _confirmed_mean(groups: Mapping[Any, list[float]]) -> float | None:
    means = [statistics.fmean(values) for values in groups.values() if len(values) >= 3]
    return min(means) if means else None


def _first_trigger_primary(values: Sequence[float], attempt: ReplayAttempt, confirmed: float | None, protocol: CensorProtocol) -> int | None:
    if not attempt.eligible or attempt.run_kind not in {"BO_TRIAL", "NOVEL_BO"} or confirmed is None:
        return None
    running = math.fsum(values[: protocol.guard_bins - 1])
    for count in range(protocol.guard_bins, FORMAL_BINS + 1):
        running += values[count - 1]
        if running / count > protocol.kappa * confirmed:
            # Bind the optimized replay to the production decision primitive
            # at the first crossing without repeatedly rescanning prefixes.
            decision = evaluate_censor_prefix(
                values[:count],
                run_kind=attempt.run_kind,
                confirmed_incumbent_mean_n=confirmed,
                protocol=protocol,
                novel_bo=True,
            )
            if not decision.triggered:
                raise ValueError("optimized trigger differs from censor primitive")
            return count
    return None


def _first_trigger_independent(values: Sequence[float], attempt: ReplayAttempt, confirmed: float | None, protocol: CensorProtocol) -> int | None:
    if not attempt.eligible or attempt.run_kind not in {"BO_TRIAL", "NOVEL_BO"} or confirmed is None:
        return None
    for count, running in enumerate(itertools.accumulate(values), start=1):
        if count >= protocol.guard_bins and running / count > protocol.kappa * confirmed:
            return count
    return None


def _replay(attempts: Sequence[ReplayAttempt], *, early_end: bool, initial_confirmed_incumbent_n: float | None, protocol: CensorProtocol) -> tuple[float | None, tuple[int, ...], int, float, tuple[dict[str, Any], ...], Mapping[Any, tuple[float, ...]]]:
    groups: dict[Any, list[float]] = defaultdict(list)
    confirmed = initial_confirmed_incumbent_n
    trigger_bins: list[int] = []
    false_new_best = 0
    saved = 0.0
    decisions: list[dict[str, Any]] = []
    for attempt in attempts:
        if attempt.missing_trace:
            continue
        full_mean = float(attempt.full_mean_n if attempt.full_mean_n is not None else math_fsum(attempt.absolute_errors_n) / FORMAL_BINS)
        primary = _first_trigger_primary(attempt.absolute_errors_n, attempt, confirmed, protocol)
        independent = _first_trigger_independent(attempt.absolute_errors_n, attempt, confirmed, protocol)
        decisions.append({"attempt_ordinal": attempt.attempt_ordinal, "primary_trigger_bin": primary, "independent_trigger_bin": independent, "confirmed_before_n": confirmed})
        triggered = early_end and primary is not None
        if triggered:
            trigger_bins.append(primary)
            saved += (FORMAL_BINS - primary) * 0.1
            if confirmed is not None and full_mean < confirmed:
                false_new_best += 1
            continue
        if attempt.eligible:
            groups[attempt.candidate_key].append(full_mean)
            candidate_confirmed = _confirmed_mean(groups)
            if candidate_confirmed is not None:
                # A confirmed incumbent is monotone: a later, worse n>=3 group
                # cannot erase the externally confirmed seed or an earlier
                # better group.
                confirmed = candidate_confirmed if confirmed is None else min(confirmed, candidate_confirmed)
    return confirmed, tuple(trigger_bins), false_new_best, saved, tuple(decisions), {
        key: tuple(values) for key, values in groups.items()
    }


def _missed_confirmed_challengers(
    *,
    full_groups: Mapping[Any, tuple[float, ...]],
    early_groups: Mapping[Any, tuple[float, ...]],
    early_confirmed_n: float | None,
) -> int:
    """Count physical candidates whose n>=3 winning mean censoring removed."""

    if early_confirmed_n is None:
        return 0
    missed = 0
    for key, values in full_groups.items():
        if len(values) < CONFIRMATION_REPEATS:
            continue
        full_mean = statistics.fmean(values)
        early_values = early_groups.get(key, ())
        early_mean = statistics.fmean(early_values) if len(early_values) >= CONFIRMATION_REPEATS else None
        if full_mean < early_confirmed_n and (early_mean is None or early_mean >= early_confirmed_n):
            missed += 1
    return missed


def backtest_early_end(attempts: Sequence[ReplayAttempt], *, initial_confirmed_incumbent_n: float | None = 0.7347, protocol: CensorProtocol = CensorProtocol(), trace_sha256: str = "", trace_audits: Mapping[int, TraceAudit] | None = None, trace_sample_count: int = 0) -> EarlyEndBacktestReceipt:
    rows = tuple(attempts)
    with_confirmed, bins, false_new_best, saved, decisions_early, early_groups = _replay(rows, early_end=True, initial_confirmed_incumbent_n=initial_confirmed_incumbent_n, protocol=protocol)
    without_confirmed, _unused, _fnb, _saved, decisions_full, full_groups = _replay(rows, early_end=False, initial_confirmed_incumbent_n=initial_confirmed_incumbent_n, protocol=protocol)
    false_challenger = _missed_confirmed_challengers(
        full_groups=full_groups,
        early_groups=early_groups,
        early_confirmed_n=with_confirmed,
    )
    independent_early = tuple((item["attempt_ordinal"], item["primary_trigger_bin"], item["independent_trigger_bin"]) for item in decisions_early)
    del decisions_full
    exact_agreement = (
        all(item["primary_trigger_bin"] == item["independent_trigger_bin"] for item in decisions_early)
        and with_confirmed == without_confirmed
    )
    eligible_count = sum(1 for row in rows if row.eligible)
    saved_bound = len(bins) * 49.5
    audit_map = trace_audits or {}
    audit_sha = digest({str(key): {"present": value.present, "sample_count": value.sample_count, "duration_s": value.duration_s} for key, value in sorted(audit_map.items())}) if audit_map else digest([])
    return EarlyEndBacktestReceipt(
        len(rows), eligible_count, sum(1 for row in rows if row.eligible and not row.missing_trace), len(bins), len(bins) / max(1, eligible_count), bins,
        None if not bins else statistics.median(value / FORMAL_BINS for value in bins), saved, saved_bound,
        false_new_best, false_challenger, with_confirmed, without_confirmed, initial_confirmed_incumbent_n,
        exact_agreement and 0.0 <= saved <= saved_bound, trace_sha256, audit_sha, len(audit_map), trace_sample_count, sum(value.duration_s is not None for value in audit_map.values()),
    )


def backtest_r008_trace(path: Path = DEFAULT_TRACE_PATH, *, admission_path: Path | None = DEFAULT_ADMISSION_PATH) -> EarlyEndBacktestReceipt:
    if admission_path is None:
        raise FileNotFoundError(admission_path)
    ledger, _ = _read_ledger(Path(admission_path))
    trace_sha, audits, present_count, sample_count = _stream_trace(Path(path))
    attempts = _assemble_attempts(ledger, audits)
    receipt = backtest_early_end(attempts, trace_sha256=trace_sha, trace_audits=audits, trace_sample_count=sample_count)
    if receipt.trace_ordinals_present != present_count:
        raise ValueError("trace audit ordinal count changed between streaming passes")
    return receipt


__all__ = ["BACKTEST_SCHEMA", "DEFAULT_ADMISSION_PATH", "DEFAULT_TRACE_PATH", "EarlyEndBacktestReceipt", "ReplayAttempt", "TraceAudit", "backtest_early_end", "backtest_r008_trace", "load_r008_path_trace"]
