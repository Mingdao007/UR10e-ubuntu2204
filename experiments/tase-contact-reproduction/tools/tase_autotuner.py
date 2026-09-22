"""Automatic TASE outer-loop campaign runner.

The tuner owns only candidate state and scheduling. Each candidate is executed
through the single ``figure8.sh`` owner, which performs the no-admittance
contact lifecycle, one explicit 60 s R013-compatible PATH, automatic Home,
and immutable run receipts. A failed physical attempt consumes its ordinal and
never becomes a successful BO observation. The 62.831853 s full-period route
remains a separate protocol and is never mixed into this campaign.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, asdict
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Mapping

import numpy as np
from scipy.special import ndtr
from scipy.stats import qmc, t as student_t
from tase_figure8_protocol import (
    DURATION_S as R013_COMPAT60_DURATION_S,
    FORMAL_END_S as R013_COMPAT60_FORMAL_END_S,
    FORMAL_START_S as R013_COMPAT60_FORMAL_START_S,
    PROTOCOL_ID as R013_COMPAT60_PROTOCOL_ID,
    REQUIRED_BINS as R013_COMPAT60_REQUIRED_BINS,
)
from tase_r013_timing_ledger import ledger_from_receipts


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "tase_autotuner_v1.json"
SCHEMA = "tase.autotuner-v1"
PARAMETER_SCHEMA = "tase.outer-parameters-v1"


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    stage: str
    index: int
    Md_scalar: float
    Bd_scalar: float

    def parameters(self) -> dict[str, Any]:
        return {
            "schema": PARAMETER_SCHEMA,
            "protocol_id": R013_COMPAT60_PROTOCOL_ID,
            "duration_token": "r013_60",
            "candidate_id": self.candidate_id,
            "stage": self.stage,
            "index": self.index,
            "Md_scalar": self.Md_scalar,
            "Bd_scalar": self.Bd_scalar,
            "frozen": {
                "kp": 4.0,
                "ko": 5.0,
                "kf": 1.0,
                "force_target_n": 5.0,
                "force_integral_limit_n_s": 1.0,
                "force_sign_convention": "step5_step6_positive_normal_load",
            },
        }


def _load_config(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != SCHEMA:
        raise ValueError("autotuner config schema differs")
    if payload.get("method") != "TASE_RNN_MATURE":
        raise ValueError("this runner only executes TASE_RNN_MATURE")
    if payload.get("protocol_id") != R013_COMPAT60_PROTOCOL_ID:
        raise ValueError("autotuner must use the explicit R013-compatible 60 s protocol")
    if payload.get("duration_token") not in {"r013_60", "compat60"}:
        raise ValueError("autotuner duration token must be r013_60")
    if tuple(float(value) for value in payload.get("formal_window_s", ())) != (
        R013_COMPAT60_FORMAL_START_S,
        R013_COMPAT60_FORMAL_END_S,
    ):
        raise ValueError("autotuner formal window must be [5,60)")
    budget = payload.get("budget", {})
    if budget != {"initial": 8, "bo": 12, "repeats": 4}:
        raise ValueError("autotuner budget must remain 8+12+4")
    control_cpu = payload.get("control_cpu", 3)
    if isinstance(control_cpu, bool) or not isinstance(control_cpu, int) or control_cpu < 0:
        raise ValueError("control_cpu must be a nonnegative CPU index")
    payload["control_cpu"] = control_cpu
    video_policy = payload.get("video_policy", "required")
    if video_policy not in {"required", "evidence-only"}:
        raise ValueError("video_policy must be required or evidence-only")
    payload["video_policy"] = video_policy
    if payload.get("protection_parameters_frozen") is not True or payload.get("integral_policy_frozen") is not True:
        raise ValueError("autotuner protection/integral freeze is missing")
    noise = float(payload.get("observation_noise_n", 0.05))
    if not math.isfinite(noise) or noise <= 0.0:
        raise ValueError("observation_noise_n must be finite and positive")
    payload["observation_noise_n"] = noise
    return payload


def _scale_unit(config: Mapping[str, Any], point: np.ndarray) -> tuple[float, float]:
    center = config["center"]
    bounds = config["log2_bounds"]
    values = []
    for name, value in zip(("Md_scalar", "Bd_scalar"), point, strict=True):
        lo, hi = map(float, bounds[name])
        exponent = lo + float(value) * (hi - lo)
        values.append(float(center[name]) * (2.0 ** exponent))
    return values[0], values[1]


def _initial_candidates(config: Mapping[str, Any]) -> list[Candidate]:
    sampler = qmc.Sobol(d=2, scramble=True, seed=int(config["seed"]))
    # Generate a power-of-two Sobol block and retain seven non-centre points;
    # this keeps the requested 8 initial candidates without the balance warning
    # emitted by scipy for a non-power-of-two draw.
    points = sampler.random_base2(m=3)[:7]
    candidates = [Candidate("initial-00", "initial", 0,
                            float(config["center"]["Md_scalar"]),
                            float(config["center"]["Bd_scalar"]))]
    for index, point in enumerate(points, start=1):
        md, bd = _scale_unit(config, point)
        candidates.append(Candidate(f"initial-{index:02d}", "initial", index, md, bd))
    return candidates


def _key(md: float, bd: float) -> tuple[float, float]:
    return (round(float(md), 12), round(float(bd), 12))


def _observations(records: list[dict[str, Any]]) -> tuple[np.ndarray, np.ndarray]:
    rows = [row for row in records if row.get("status") == "complete" and row.get("mae_n") is not None]
    if not rows:
        return np.empty((0, 2)), np.empty((0,))
    x = np.asarray([[float(row["Md_scalar"]), float(row["Bd_scalar"])] for row in rows], dtype=float)
    y = np.asarray([float(row["mae_n"]) for row in rows], dtype=float)
    return x, y


def _normalized(config: Mapping[str, Any], values: np.ndarray) -> np.ndarray:
    center = config["center"]
    return np.asarray([
        math.log2(float(values[0]) / float(center["Md_scalar"])),
        math.log2(float(values[1]) / float(center["Bd_scalar"])),
    ], dtype=float)


def _rbf_kernel(left: np.ndarray, right: np.ndarray, *, length_scale: float,
                signal_variance: float) -> np.ndarray:
    delta = left[:, None, :] - right[None, :, :]
    return signal_variance * np.exp(-0.5 * np.sum((delta / length_scale) ** 2, axis=2))


def _gp_posterior(config: Mapping[str, Any], observed_x: np.ndarray,
                  observed_y: np.ndarray, query_x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return a deterministic noisy-GP posterior in log2 parameter space.

    The objective has a non-zero force-error baseline, so the constant mean is
    fit from the observations and restored after conditioning.  ``noise_n``
    is a standard deviation in the config; only its square enters the
    covariance diagonal.  The returned variance is the latent posterior
    variance (observation noise is kept in the fit, not double-counted in EI).
    """
    if len(observed_y) == 0:
        return np.zeros(len(query_x)), np.ones(len(query_x))
    if observed_x.ndim != 2 or observed_x.shape[0] != len(observed_y) or observed_x.shape[1] != 2:
        raise ValueError("GP observations must have shape (n,2) matching y")
    if query_x.ndim != 2 or query_x.shape[1] != 2:
        raise ValueError("GP query points must have shape (m,2)")
    train = np.asarray([_normalized(config, row) for row in observed_x], dtype=float)
    query = np.asarray(query_x, dtype=float)
    prior_mean = float(np.mean(observed_y))
    centered_y = np.asarray(observed_y, dtype=float) - prior_mean
    noise_std = float(config["observation_noise_n"])
    noise_variance = noise_std * noise_std
    signal = max(float(np.var(centered_y)), noise_variance, 1e-6)
    length = float(config.get("gp_length_scale", 0.25))
    kernel = _rbf_kernel(train, train, length_scale=length, signal_variance=signal)
    kernel.flat[:: len(kernel) + 1] += noise_variance + max(1e-9, signal * 1e-9)
    cross = _rbf_kernel(train, query, length_scale=length, signal_variance=signal)
    prior = np.full(len(query), signal, dtype=float)
    try:
        chol = np.linalg.cholesky(kernel)
        alpha = np.linalg.solve(chol.T, np.linalg.solve(chol, centered_y))
        mean = prior_mean + cross.T @ alpha
        projected = np.linalg.solve(chol, cross)
        variance = prior - np.sum(projected * projected, axis=0)
    except np.linalg.LinAlgError:
        inverse = np.linalg.pinv(kernel)
        mean = prior_mean + cross.T @ inverse @ centered_y
        variance = prior - np.einsum("ij,ji->i", cross.T @ inverse, cross)
    return np.asarray(mean, dtype=float), np.maximum(np.asarray(variance, dtype=float), 1e-12)


def _expected_improvement(
    mean: np.ndarray,
    variance: np.ndarray,
    incumbent: float,
    *,
    exploration_n: float = 0.0,
) -> np.ndarray:
    """Expected improvement for minimization, with stable zero-variance handling."""
    if not math.isfinite(float(incumbent)) or exploration_n < 0.0:
        raise ValueError("EI incumbent/exploration values are invalid")
    sigma = np.sqrt(np.maximum(np.asarray(variance, dtype=float), 1e-12))
    improvement = float(incumbent) - np.asarray(mean, dtype=float) - float(exploration_n)
    z = np.divide(improvement, sigma, out=np.zeros_like(improvement), where=sigma > 0.0)
    phi = np.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)
    cdf = ndtr(z)
    return np.maximum(improvement * cdf + sigma * phi, 0.0)


def _next_bo_candidate(config: Mapping[str, Any], records: list[dict[str, Any]], index: int) -> Candidate:
    observed_x, observed_y = _observations(records)
    sampler = qmc.Sobol(d=2, scramble=True, seed=int(config["seed"]) + 1009 + index)
    pool = sampler.random(int(config["bo_pool_size"]))
    tried = {_key(float(row["Md_scalar"]), float(row["Bd_scalar"])) for row in records}
    normalized_pool = np.asarray([
        [float(config["log2_bounds"][name][0]) + point[j] *
         (float(config["log2_bounds"][name][1]) - float(config["log2_bounds"][name][0]))
         for j, name in enumerate(("Md_scalar", "Bd_scalar"))]
        for point in pool
    ])
    if len(observed_y) < 2:
        order = np.roll(np.arange(len(pool), dtype=int), -index)
    else:
        mean, variance = _gp_posterior(config, observed_x, observed_y, normalized_pool)
        incumbent = float(np.min(observed_y))
        ei = _expected_improvement(mean, variance, incumbent)
        order = np.argsort(-ei)
    for pool_index in order:
        point = pool[int(pool_index)]
        md, bd = _scale_unit(config, point)
        if _key(md, bd) not in tried:
            return Candidate(f"bo-{index:02d}", "bo", index, md, bd)
    raise RuntimeError("bounded GP proposal pool was exhausted without a new candidate")


def _best_candidate(config: Mapping[str, Any], records: list[dict[str, Any]]) -> Candidate:
    complete = [row for row in records if row.get("status") == "complete" and row.get("mae_n") is not None]
    if not complete:
        return _initial_candidates(config)[0]
    row = min(complete, key=lambda item: float(item["mae_n"]))
    return Candidate(str(row["candidate_id"]), "repeat", int(row["index"]),
                     float(row["Md_scalar"]), float(row["Bd_scalar"]))


def _read_records(ledger: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not ledger.exists():
        return [], []
    by_ordinal: dict[int, dict[str, Any]] = {}
    for line in ledger.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            ordinal = int(row["ordinal"])
            # A terminal row supersedes its durable started marker.  Repeated
            # terminal rows are resolved to the latest one so a resumed process
            # cannot count one physical attempt twice.
            if ordinal not in by_ordinal or row.get("status") != "started":
                by_ordinal[ordinal] = row
    recovered: list[dict[str, Any]] = []
    for ordinal, row in sorted(by_ordinal.items()):
        if row.get("status") == "started":
            row = {
                **row,
                "status": "failed",
                "mae_n": None,
                "complete_path": False,
                "failure": "interrupted_attempt_recovered_as_failed",
                "recovery_required": True,
                "recovery_reason": "started_without_terminal_receipt_unknown_dispatch_state",
                "finished_at": time.time(),
            }
            recovered.append(row)
            by_ordinal[ordinal] = row
    return [by_ordinal[key] for key in sorted(by_ordinal)], recovered


def _assert_r013_records(records: list[dict[str, Any]]) -> None:
    """Refuse to mix another duration or metric protocol into this campaign."""

    for row in records:
        if row.get("protocol_id") != R013_COMPAT60_PROTOCOL_ID:
            raise ValueError(
                "campaign ledger contains a non-R013 record; refusing to mix "
                "full-period or legacy observations into the 60 s campaign"
            )
        if row.get("duration_token") not in {"r013_60", "compat60"}:
            raise ValueError("campaign ledger duration token differs from r013_60")


def _recovery_is_closed(row: Mapping[str, Any]) -> bool:
    if row.get("home_verified") is True:
        return True
    recovery = row.get("recovery")
    return isinstance(recovery, Mapping) and recovery.get("success") is True


def _needs_recovery_pause(row: Mapping[str, Any]) -> bool:
    """A dispatched failure may not advance the campaign without Home proof."""

    if row.get("status") != "failed":
        return False
    dispatch = row.get("dispatch")
    if isinstance(dispatch, Mapping) and dispatch.get("opened") is True:
        return not _recovery_is_closed(row)
    if str(row.get("failure", "")).startswith("receipt_parse:"):
        return not _recovery_is_closed(row)
    # A missing receipt without the typed neutral-hold proof is an unknown
    # dispatch state. It may have died after motion began; keep the campaign
    # at the recovery owner until Home is closed.
    return (
        row.get("failure") == "owner_failed_or_missing_receipt"
        and not _is_preflight_only_failure(row)
    )


def _append(ledger: Path, row: Mapping[str, Any]) -> None:
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(dict(row), sort_keys=True, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _is_preflight_only_failure(row: Mapping[str, Any]) -> bool:
    """Identify an owner failure that never dispatched a physical attempt."""

    if (
        row.get("status") != "failed"
        or row.get("failure") != "owner_failed_or_missing_receipt"
    ):
        return False
    run_dir = row.get("run_dir")
    if not isinstance(run_dir, str) or not run_dir:
        return False
    if (Path(run_dir) / "dispatch_receipt.json").is_file():
        return False
    neutral = Path(run_dir) / "terminal-no-dispatch-receipt.json"
    if not neutral.is_file() or neutral.is_symlink():
        return False
    try:
        payload = json.loads(neutral.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if (
        payload.get("attempt_dispatched") is not False
        or payload.get("terminal_no_dispatch") is not True
        or payload.get("writer_artifacts_absent") is not True
    ):
        return False
    forbidden = {
        "dispatch_receipt.json",
        "owner.json",
        "raw_sensor.jsonl",
        "robot_frames.jsonl",
        "published_packets.jsonl",
        "admission_robot_frames.jsonl",
        "rejected_robot_frames.jsonl",
        "supervisor-result.json",
    }
    return not any((Path(run_dir) / name).exists() for name in forbidden)


def _read_confirmation_records(ledger: Path) -> list[dict[str, Any]]:
    """Read confirmation rows by their (round, arm) identity.

    The tuning ledger uses integer ordinals; confirmation deliberately uses a
    two-arm paired key, so it cannot be parsed by ``_read_records`` without
    losing the randomised order or collapsing the pair.
    """
    if not ledger.exists():
        return []
    by_key: dict[tuple[int, str], dict[str, Any]] = {}
    for line in ledger.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        key = (int(row["round"]), str(row["arm"]))
        if key not in by_key or row.get("status") != "started":
            by_key[key] = row
    recovered: list[dict[str, Any]] = []
    for key, row in sorted(by_key.items()):
        if row.get("status") == "started":
            row = {
                **row,
                "status": "failed",
                "mae_n": None,
                "complete_path": False,
                "failure": "interrupted_confirmation_recovered_as_failed",
                "finished_at": time.time(),
            }
            recovered.append(row)
            by_key[key] = row
    for row in recovered:
        _append(ledger, row)
    return [by_key[key] for key in sorted(by_key)]


def _confirmation_needs_recovery(row: Mapping[str, Any]) -> bool:
    if row.get("status") not in {"started", "failed"}:
        return False
    if row.get("home_verified") is True:
        return False
    recovery = row.get("recovery")
    if isinstance(recovery, Mapping) and recovery.get("success") is True:
        return False
    return True


def _extract_mae(run_dir: Path) -> tuple[float | None, dict[str, Any]]:
    dispatch = json.loads((run_dir / "dispatch_receipt.json").read_text(encoding="utf-8"))
    supervisor = {}
    supervisor_path = run_dir / "supervisor-result.json"
    if supervisor_path.is_file():
        try:
            supervisor = json.loads(supervisor_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            supervisor = {}
    path = dispatch.get("live_path") or {}
    recovery = (
        dispatch.get("autonomous_home_recovery")
        or dispatch.get("recovery")
        or supervisor.get("autonomous_home_recovery")
        or supervisor.get("recovery")
    )
    attempts = dispatch.get("attempts", [])
    pilot = next((item for item in attempts if item.get("phase") == "pilot"), None)
    pilot_evidence = pilot.get("evidence", {}) if isinstance(pilot, dict) else {}
    home_proof = pilot_evidence.get("home_proof", {}) if isinstance(pilot_evidence, dict) else {}
    home_verified = bool(
        isinstance(home_proof, dict)
        and home_proof.get("fixed_home_route") is True
        and home_proof.get("stationary") is True
        and pilot_evidence.get("return_gate_passed") is True
        and dispatch.get("stop", {}).get("observed_stationary") is True
    )
    if isinstance(recovery, dict) and recovery.get("success") is True:
        home_verified = True
    lifecycle_events = dispatch.get("lifecycle_events") or dispatch.get("timing_events") or []
    if not isinstance(lifecycle_events, list):
        lifecycle_events = []
    try:
        timing_ledger = ledger_from_receipts(
            str(dispatch.get("attempt_id") or run_dir.name),
            lifecycle_events=lifecycle_events,
            dispatch_receipt=dispatch,
            supervisor_result=supervisor,
        )
        timing_ledger_payload = timing_ledger.as_dict()
        home_verified = home_verified or bool(
            timing_ledger.recovery_outcome().get("home_verified")
        )
    except (TypeError, ValueError):
        timing_ledger_payload = None
    path_kind = path.get("kind")
    if path_kind == "full_period":
        requested_duration_s = 62.83185307179586
        required_bins = 629
        protocol_id = "contact_yield_full_period_v1"
        incomplete_reason = "full_period_metrics_incomplete"
    elif path_kind == "r013_compat_60" and path.get("protocol_id") == R013_COMPAT60_PROTOCOL_ID:
        requested_duration_s = R013_COMPAT60_DURATION_S
        required_bins = R013_COMPAT60_REQUIRED_BINS
        protocol_id = R013_COMPAT60_PROTOCOL_ID
        incomplete_reason = "r013_compat_60_metrics_incomplete"
    else:
        requested_duration_s = None
        required_bins = None
        protocol_id = None
        incomplete_reason = "unknown_path_protocol"
    common = {
        "dispatch": dispatch,
        "recovery": recovery,
        "home_verified": home_verified,
        "protocol_id": protocol_id,
        "timing_ledger": timing_ledger_payload,
    }
    if (
        dispatch.get("command") != "pilot"
        or requested_duration_s is None
        or dispatch.get("evidence_eligible") is not True
        or dispatch.get("error")
    ):
        return None, {**common, "complete_path": False,
                      "reason": incomplete_reason}
    metric_sources = [dispatch.get("evidence_metrics", {})]
    if isinstance(pilot, dict):
        evidence = pilot.get("evidence", {})
        if isinstance(evidence, dict):
            metric_sources.append(evidence.get("metrics", {}))
    def _legacy_path_closure(metrics: Mapping[str, Any]) -> bool:
        """Recognize one sealed PATH metric envelope for the selected protocol.

        Older live receipts predate the generic ``ContactMetrics`` flags and
        therefore expose the same closure proof as the timing/bin fields.  A
        metric is accepted through this compatibility path only when every
        formal bin is present, the owner reports a successful timing gate, and
        the measured metric interval is the complete requested PATH.  This is
        deliberately narrower than accepting a non-null MAE, so qualification
        and censored/short PATHs remain failed denominators.
        """
        try:
            full_bins = int(metrics.get("full_path_bin_count", metrics.get("complete_bins")))
            metric_required_bins = int(
                metrics.get("required_full_path_bin_count", metrics.get("required_bins"))
            )
            path_duration = float(metrics["path_duration_s"])
            metric_duration = float(
                metrics.get(
                    "full_force_metric_duration_s",
                    metrics.get("formal_metric_duration_s", path_duration),
                )
            )
        except (KeyError, TypeError, ValueError):
            return False
        timing = metrics.get("timing_evidence")
        timing_ok = bool(
            metrics.get("timing_gate_passed") is True
            and isinstance(timing, Mapping)
            and timing.get("successful") is True
        )
        metric_required_duration = (
            requested_duration_s
            if protocol_id == "contact_yield_full_period_v1"
            else R013_COMPAT60_FORMAL_END_S - R013_COMPAT60_FORMAL_START_S
        )
        return bool(
            full_bins > 0
            and full_bins == metric_required_bins == required_bins
            and path_duration >= requested_duration_s
            and metric_duration >= metric_required_duration
            and timing_ok
            and (
                protocol_id == "contact_yield_full_period_v1"
                or metrics.get("protocol_id") == R013_COMPAT60_PROTOCOL_ID
            )
            and metrics.get("interrupted") is not True
        )

    for metrics in metric_sources:
        if not isinstance(metrics, dict):
            continue
        # ContactMetrics is the only live source accepted here.  Requiring all
        # three flags prevents a short qualification or censored path from
        # becoming a successful BO observation.
        generic_complete = (
            metrics.get("complete") is True
            and metrics.get("objective_eligible") is True
            and metrics.get("coverage_complete") is True
            and metrics.get("interrupted") is False
        )
        generic_protocol_ok = (
            protocol_id == "contact_yield_full_period_v1"
            or metrics.get("protocol_id") == R013_COMPAT60_PROTOCOL_ID
        )
        if not generic_complete and not _legacy_path_closure(metrics):
            continue
        if protocol_id == R013_COMPAT60_PROTOCOL_ID and not generic_protocol_ok:
            continue
        for key in (
            "normal_force_mae_n",
            "force_mae_n",
            "full_force_mae_n",
            "mae_n",
            "mae",
        ):
            if key in metrics and metrics[key] is not None:
                mae = float(metrics[key])
                if math.isfinite(mae):
                    return mae, {**common, "complete_path": True,
                                  "metrics": metrics}
    return None, {**common, "complete_path": False,
                  "reason": incomplete_reason}


def _complete_r013_result(
    evidence: Mapping[str, Any],
    mae: float | None,
    returncode: int,
    home_verified: bool,
) -> bool:
    return bool(
        mae is not None
        and returncode == 0
        and home_verified
        and evidence.get("protocol_id") == R013_COMPAT60_PROTOCOL_ID
        and evidence.get("complete_path") is True
    )


def _execute_preflight_recovery(
    config: Mapping[str, Any],
    campaign_dir: Path,
    script: Path,
    source_row: Mapping[str, Any],
    recovery_index: int,
) -> dict[str, Any]:
    """Retry only a candidate that failed before owner dispatch.

    The original row and run directory stay immutable.  A recovery directory
    carries the same candidate identity and parameter values, so a restored
    prerequisite can be re-tested without overwriting the first failure.
    """

    ordinal = int(source_row["ordinal"])
    candidate = Candidate(
        str(source_row["candidate_id"]),
        str(source_row["stage"]),
        int(source_row["index"]),
        float(source_row["Md_scalar"]),
        float(source_row["Bd_scalar"]),
    )
    stem = f"{ordinal:02d}-{candidate.candidate_id}-recovery-{recovery_index:02d}"
    candidate_file = campaign_dir / "candidates" / f"{stem}.json"
    run_dir = campaign_dir / "runs" / stem
    _write_candidate(candidate_file, candidate)
    row = {
        "schema": "tase.autotuner-attempt-v1",
        "protocol_id": R013_COMPAT60_PROTOCOL_ID,
        "duration_token": "r013_60",
        "ordinal": ordinal,
        "candidate_id": candidate.candidate_id,
        "stage": candidate.stage,
        "index": candidate.index,
        "Md_scalar": candidate.Md_scalar,
        "Bd_scalar": candidate.Bd_scalar,
        "parameter_file": str(candidate_file),
        "run_dir": str(run_dir),
        "recovery_of": source_row.get("run_dir"),
        "recovery_index": recovery_index,
        "started_at": time.time(),
    }
    ledger = campaign_dir / "ledger.jsonl"
    _append(ledger, {**row, "status": "started"})
    command = [
        str(script),
        "--method", "TASE_RNN_MATURE",
        "--duration", "r013_60",
        "--control-cpu", str(config["control_cpu"]),
        "--video-policy", str(config["video_policy"]),
        "--run-dir", str(run_dir),
        "--parameter-file", str(candidate_file),
    ]
    completed = subprocess.run(command, cwd=str(ROOT), check=False)
    receipt_path = run_dir / "dispatch_receipt.json"
    if not receipt_path.is_file():
        result = {
            **row,
            "status": "failed",
            "mae_n": None,
            "complete_path": False,
            "returncode": completed.returncode,
            "failure": "owner_failed_or_missing_receipt",
            "preflight_failed": True,
            "finished_at": time.time(),
        }
        _append(ledger, result)
        return result
    try:
        mae, evidence = _extract_mae(run_dir)
        home_verified = bool(evidence.get("home_verified"))
        status = (
            "complete"
            if _complete_r013_result(evidence, mae, completed.returncode, home_verified)
            else "failed"
        )
        result = {
            **row,
            "status": status,
            "mae_n": mae,
            "complete_path": bool(evidence.get("complete_path")),
            "dispatch": evidence.get("dispatch"),
            "home_verified": home_verified,
            "recovery": evidence.get("recovery"),
            "metrics": evidence.get("metrics"),
            "failure": (
                None
                if status == "complete"
                else evidence.get("reason") or (
                    "owner_returncode_nonzero" if completed.returncode != 0
                    else "home_not_verified"
                )
            ),
            "returncode": completed.returncode,
            "finished_at": time.time(),
        }
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result = {
            **row,
            "status": "failed",
            "mae_n": None,
            "failure": f"receipt_parse:{type(exc).__name__}:{exc}",
            "finished_at": time.time(),
        }
    _append(ledger, result)
    return result


def _write_candidate(path: Path, candidate: Candidate) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(candidate.parameters(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _candidate_for(config: Mapping[str, Any], records: list[dict[str, Any]], ordinal: int) -> Candidate:
    initials = _initial_candidates(config)
    if ordinal < len(initials):
        return initials[ordinal]
    bo_end = len(initials) + int(config["budget"]["bo"])
    if ordinal < bo_end:
        return _next_bo_candidate(config, records, ordinal - len(initials))
    best = _best_candidate(config, records)
    repeat_index = ordinal - bo_end
    return Candidate(f"repeat-{repeat_index:02d}", "repeat", repeat_index,
                     best.Md_scalar, best.Bd_scalar)


def run_campaign(
    config_path: Path,
    campaign_dir: Path,
    *,
    execute: bool,
    dry_run: bool = False,
    resume: bool = False,
) -> dict[str, Any]:
    config = _load_config(config_path)
    campaign_dir = campaign_dir.expanduser().resolve()
    campaign_dir.mkdir(parents=True, exist_ok=True)
    ledger = campaign_dir / "ledger.jsonl"
    records, recovered = _read_records(ledger)
    _assert_r013_records(records)
    unresolved_recovery = any(
        row.get("recovery_required") is True or _needs_recovery_pause(row)
        for row in records
    )
    resume_paused = unresolved_recovery
    # A dry plan is a schedule, not a consumed physical budget.  Reusing its
    # directory for ``--execute`` must start those ordinals instead of treating
    # the planned rows as completed attempts.
    if execute:
        records = [row for row in records if row.get("status") != "planned"]
    for row in recovered:
        # Preserve the original attempt identity while making an interrupted
        # attempt an explicit failed denominator entry before scheduling more.
        _append(ledger, row)
    total = sum(int(value) for value in config["budget"].values())
    script = ROOT / "scripts" / "figure8.sh"
    if execute and not script.is_file():
        raise RuntimeError(f"figure8 owner is missing: {script}")
    if unresolved_recovery:
        # A started row without a terminal receipt does not prove that motion
        # never started.  Do not reinterpret it as a cheap preflight retry;
        # the recovery owner must close Home before any new candidate.
        records = [
            {**row, "campaign_state": "PAUSED_RECOVERY_BLOCKED"}
            if row.get("recovery_required") is True or _needs_recovery_pause(row)
            else row
            for row in records
        ]
        for row in records:
            if row.get("campaign_state") == "PAUSED_RECOVERY_BLOCKED":
                _append(ledger, row)
    elif resume:
        if not execute:
            raise ValueError("preflight recovery requires execute mode")
        preflight_rows = [row for row in records if _is_preflight_only_failure(row)]
        for source_row in preflight_rows:
            recovery_root = campaign_dir / "runs"
            prefix = f"{int(source_row['ordinal']):02d}-{source_row['candidate_id']}-recovery-"
            recovery_index = sum(1 for path in recovery_root.glob(prefix + "*"))
            result = _execute_preflight_recovery(
                config,
                campaign_dir,
                script,
                source_row,
                recovery_index,
            )
            if _is_preflight_only_failure(result):
                # A shared prerequisite is still absent. Preserve the new
                # failed row and stop before spending another candidate.
                _append(
                    ledger,
                    {**result, "campaign_state": "PAUSED_PRECHECK_BLOCKED"},
                )
                resume_paused = True
                break
            if _needs_recovery_pause(result):
                result = {**result, "campaign_state": "PAUSED_RECOVERY_BLOCKED"}
                _append(ledger, result)
                resume_paused = True
                break
        records, _ = _read_records(ledger)
        _assert_r013_records(records)
    while len(records) < total and not resume_paused:
        ordinal = len(records)
        candidate = _candidate_for(config, records, ordinal)
        candidate_file = campaign_dir / "candidates" / f"{ordinal:02d}-{candidate.candidate_id}.json"
        _write_candidate(candidate_file, candidate)
        run_dir = campaign_dir / "runs" / f"{ordinal:02d}-{candidate.candidate_id}"
        row = {
            "schema": "tase.autotuner-attempt-v1",
            "protocol_id": R013_COMPAT60_PROTOCOL_ID,
            "duration_token": "r013_60",
            "ordinal": ordinal,
            "candidate_id": candidate.candidate_id,
            "stage": candidate.stage,
            "index": candidate.index,
            "Md_scalar": candidate.Md_scalar,
            "Bd_scalar": candidate.Bd_scalar,
            "parameter_file": str(candidate_file),
            "run_dir": str(run_dir),
            "started_at": time.time(),
        }
        if not execute:
            planned = {**row, "status": "planned", "mae_n": None,
                       "complete_path": False, "failure": None}
            _append(ledger, planned)
            records.append(planned)
            continue
        _append(ledger, {**row, "status": "started"})
        command = [str(script), "--method", "TASE_RNN_MATURE", "--duration", "r013_60",
                   "--control-cpu", str(config["control_cpu"]),
                   "--video-policy", str(config["video_policy"]),
                   "--run-dir", str(run_dir), "--parameter-file", str(candidate_file)]
        completed = subprocess.run(command, cwd=str(ROOT), check=False)
        receipt_path = run_dir / "dispatch_receipt.json"
        if not receipt_path.is_file():
            failure = {**row, "status": "failed", "mae_n": None,
                       "returncode": completed.returncode,
                       "failure": "owner_failed_or_missing_receipt",
                       "preflight_failed": True,
                       "finished_at": time.time()}
            _append(ledger, failure)
            records.append(failure)
            failure["campaign_state"] = "PAUSED_PRECHECK_BLOCKED"
            _append(ledger, failure)
            break
            continue
        try:
            mae, evidence = _extract_mae(run_dir)
            home_verified = bool(evidence.get("home_verified"))
            status = (
                "complete"
                if _complete_r013_result(evidence, mae, completed.returncode, home_verified)
                else "failed"
            )
            result = {**row, "status": status, "mae_n": mae,
                      "complete_path": bool(evidence.get("complete_path")),
                      "dispatch": evidence.get("dispatch"),
                      "home_verified": home_verified,
                      "recovery": evidence.get("recovery"),
                      "metrics": evidence.get("metrics"),
                      "failure": (
                          None
                          if status == "complete"
                          else evidence.get("reason") or (
                              "owner_returncode_nonzero" if completed.returncode != 0
                              else "home_not_verified"
                          )
                      ),
                      "returncode": completed.returncode,
                      "finished_at": time.time()}
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            result = {**row, "status": "failed", "mae_n": None,
                      "failure": f"receipt_parse:{type(exc).__name__}:{exc}",
                      "finished_at": time.time()}
        _append(ledger, result)
        records.append(result)
        # A physical writer may not advance the campaign until its failed
        # attempt has reached verified Home. Preserve the failed denominator,
        # then pause for a recovery owner that did not close the route.
        if _needs_recovery_pause(result):
            result["campaign_state"] = "PAUSED_RECOVERY_BLOCKED"
            _append(ledger, result)
            break
    complete = [row for row in records if row.get("status") == "complete" and row.get("mae_n") is not None]
    failed = [row for row in records if row.get("status") == "failed"]
    summary = {
        "schema": "tase.autotuner-summary-v1",
        "protocol_id": R013_COMPAT60_PROTOCOL_ID,
        "duration_token": "r013_60",
        "video_policy": config["video_policy"],
        "config": str(config_path),
        "campaign_dir": str(campaign_dir),
        "budget": config["budget"],
        "attempts": len(records),
        "complete_paths": len(complete),
        "failed_attempts": len(failed),
        "planned_attempts": sum(row.get("status") == "planned" for row in records),
        "best": None if not complete else min(complete, key=lambda row: float(row["mae_n"])),
        "state": (
            "PLANNED"
            if not execute
            else "PAUSED_PRECHECK_BLOCKED"
            if any(
                _is_preflight_only_failure(row)
                or row.get("campaign_state") == "PAUSED_PRECHECK_BLOCKED"
                for row in records
            )
            else "PAUSED_RECOVERY_BLOCKED"
            if any(row.get("campaign_state") == "PAUSED_RECOVERY_BLOCKED" for row in records)
            else "COMPLETE" if len(records) >= total else "PAUSED_RECOVERY_BLOCKED"
        ),
        "claim_scope": "complete-path receipts only; no controller promotion or manuscript claim",
    }
    (campaign_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    return summary


def run_confirmation(config_path: Path, campaign_dir: Path, *, execute: bool) -> dict[str, Any]:
    """Run the frozen five-round randomized baseline/incumbent confirmation.

    Confirmation rows live in a separate ledger and never become BO
    observations. A physical failure still consumes its confirmation cell; a
    recovery that does not close at Home pauses the paired sequence.
    """
    config = _load_config(config_path)
    campaign_dir = campaign_dir.expanduser().resolve()
    source_summary = json.loads((campaign_dir / "summary.json").read_text(encoding="utf-8"))
    if source_summary.get("protocol_id") != R013_COMPAT60_PROTOCOL_ID:
        raise RuntimeError("confirmation source summary belongs to another protocol")
    if source_summary.get("state") != "COMPLETE" or not source_summary.get("best"):
        raise RuntimeError("confirmation requires a complete 24-unit tuning campaign")
    incumbent = Candidate(
        "frozen-incumbent", "confirmation", 0,
        float(source_summary["best"]["Md_scalar"]),
        float(source_summary["best"]["Bd_scalar"]),
    )
    baseline = Candidate(
        "frozen-baseline", "confirmation", 0,
        float(config["center"]["Md_scalar"]),
        float(config["center"]["Bd_scalar"]),
    )
    confirmation_dir = campaign_dir / "confirmation"
    ledger = confirmation_dir / "ledger.jsonl"
    existing = _read_confirmation_records(ledger)
    if any(_confirmation_needs_recovery(row) for row in existing):
        return {
            "schema": "tase.autotuner-confirmation-summary-v1",
            "state": "PAUSED_RECOVERY_BLOCKED",
            "rounds": 0,
            "rows": len(existing),
            "ledger": str(ledger),
            "reason": "existing confirmation attempt lacks verified Home closure",
        }
    rounds = int(config.get("confirmation_rounds", 5))
    rng = np.random.default_rng(int(config["seed"]) + 7001)
    order = [tuple(rng.permutation(["baseline", "incumbent"]).tolist()) for _ in range(rounds)]
    script = ROOT / "scripts" / "figure8.sh"
    existing_keys = {(int(row["round"]), str(row["arm"])) for row in existing}
    for ordinal in range(rounds):
        for arm in order[ordinal]:
            if (ordinal, arm) in existing_keys:
                continue
            candidate = baseline if arm == "baseline" else incumbent
            row = {
                "schema": "tase.autotuner-confirmation-v1",
                "protocol_id": R013_COMPAT60_PROTOCOL_ID,
                "duration_token": "r013_60",
                "round": ordinal,
                "arm": arm,
                "candidate_id": candidate.candidate_id,
                "Md_scalar": candidate.Md_scalar,
                "Bd_scalar": candidate.Bd_scalar,
                "started_at": time.time(),
            }
            candidate_file = confirmation_dir / "candidates" / f"round-{ordinal:02d}-{arm}.json"
            run_dir = confirmation_dir / "runs" / f"round-{ordinal:02d}-{arm}"
            _write_candidate(candidate_file, candidate)
            row.update({"parameter_file": str(candidate_file), "run_dir": str(run_dir)})
            _append(ledger, {**row, "status": "started"})
            if not execute:
                result = {**row, "status": "planned", "mae_n": None}
            else:
                completed = subprocess.run(
                    [str(script), "--method", "TASE_RNN_MATURE", "--duration", "r013_60",
                     "--control-cpu", str(config["control_cpu"]),
                     "--video-policy", str(config["video_policy"]),
                     "--run-dir", str(run_dir), "--parameter-file", str(candidate_file)],
                    cwd=str(ROOT), check=False,
                )
                if not (run_dir / "dispatch_receipt.json").is_file():
                    result = {**row, "status": "failed", "mae_n": None,
                              "returncode": completed.returncode,
                              "failure": "owner_failed_or_missing_receipt"}
                else:
                    mae, evidence = _extract_mae(run_dir)
                    home_verified = bool(evidence.get("home_verified"))
                    status = (
                        "complete"
                        if _complete_r013_result(evidence, mae, completed.returncode, home_verified)
                        else "failed"
                    )
                    result = {**row, "status": status,
                              "mae_n": mae, "complete_path": bool(evidence.get("complete_path")),
                              "home_verified": home_verified,
                              "recovery": evidence.get("recovery"),
                              "returncode": completed.returncode,
                              "failure": (
                                  None
                                  if status == "complete"
                                  else evidence.get("reason") or (
                                      "owner_returncode_nonzero" if completed.returncode != 0
                                      else "home_not_verified"
                                  )
                              )}
            result["finished_at"] = time.time()
            _append(ledger, result)
            existing.append(result)
            existing_keys.add((ordinal, arm))
            if execute and result.get("status") != "complete":
                recovery = result.get("recovery")
                if not result.get("home_verified") and not (
                    isinstance(recovery, dict) and recovery.get("success") is True
                ):
                    return {"schema": "tase.autotuner-confirmation-summary-v1",
                            "state": "PAUSED_RECOVERY_BLOCKED", "rounds": ordinal,
                            "rows": len(existing), "ledger": str(ledger)}
    pairs: list[dict[str, Any]] = []
    for round_index in range(rounds):
        pair = {row.get("arm"): row for row in existing if row.get("round") == round_index}
        if "baseline" in pair and "incumbent" in pair:
            def valid(row: Mapping[str, Any]) -> bool:
                return bool(
                    row.get("status") == "complete"
                    and row.get("protocol_id") == R013_COMPAT60_PROTOCOL_ID
                    and row.get("duration_token") in {"r013_60", "compat60"}
                    and row.get("mae_n") is not None
                    and row.get("complete_path") is True
                    and row.get("home_verified") is True
                )
            if valid(pair["baseline"]) and valid(pair["incumbent"]):
                pairs.append({"round": round_index,
                              "baseline_mae_n": pair["baseline"]["mae_n"],
                              "incumbent_mae_n": pair["incumbent"]["mae_n"],
                              "delta_incumbent_minus_baseline_n": pair["incumbent"]["mae_n"] - pair["baseline"]["mae_n"]})
    deltas = np.asarray([row["delta_incumbent_minus_baseline_n"] for row in pairs], dtype=float)
    if len(deltas):
        mean_delta = float(np.mean(deltas))
        if len(deltas) > 1:
            standard_error = float(np.std(deltas, ddof=1) / math.sqrt(len(deltas)))
            margin = float(student_t.ppf(0.975, len(deltas) - 1) * standard_error)
        else:
            margin = None
        delta_ci95 = None if margin is None else [mean_delta - margin, mean_delta + margin]
        improvement_ci95 = None if delta_ci95 is None else [-delta_ci95[1], -delta_ci95[0]]
        partial_improvement_supported = bool(
            improvement_ci95 is not None
            and -mean_delta >= float(config["claim_threshold_n"])
            and improvement_ci95[0] >= float(config["claim_threshold_n"])
        )
        improvement_supported = bool(partial_improvement_supported and len(pairs) == rounds)
    else:
        mean_delta = None
        delta_ci95 = None
        improvement_ci95 = None
        partial_improvement_supported = False
        improvement_supported = False
    summary = {
        "schema": "tase.autotuner-confirmation-summary-v1",
        "protocol_id": R013_COMPAT60_PROTOCOL_ID,
        "video_policy": config["video_policy"],
        "state": "COMPLETE" if len(pairs) == rounds else "INCOMPLETE",
        "rounds": rounds,
        "paired_complete": len(pairs),
        "pairs": pairs,
        "mean_delta_n": mean_delta,
        "mean_improvement_n": None if mean_delta is None else -mean_delta,
        "paired_delta_ci95_n": delta_ci95,
        "improvement_ci95_n": improvement_ci95,
        "partial_improvement_supported": partial_improvement_supported,
        "improvement_supported": improvement_supported,
        "claim_threshold_n": float(config["claim_threshold_n"]),
        "claim_confidence": float(config["claim_confidence"]),
        "holdout_excluded_from_tuning": True,
        "ledger": str(ledger),
    }
    confirmation_dir.mkdir(parents=True, exist_ok=True)
    (confirmation_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--campaign-dir", type=Path, required=True)
    parser.add_argument("--plan", action="store_true", help="write the schedule without touching hardware")
    parser.add_argument("--execute", action="store_true", help="run the complete sequential campaign")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="retry only prior preflight failures that never dispatched motion",
    )
    parser.add_argument("--confirm", action="store_true", help="run the frozen five-round paired confirmation")
    args = parser.parse_args(argv)
    if args.confirm and (args.plan or args.execute or args.resume):
        parser.error("--confirm is a separate post-tuning action")
    if not args.confirm and sum(bool(value) for value in (args.plan, args.execute, args.resume)) != 1:
        parser.error("choose exactly one of --plan, --execute, or --resume")
    summary = (run_confirmation(args.config, args.campaign_dir, execute=True)
               if args.confirm else run_campaign(
                   args.config,
                   args.campaign_dir,
                   execute=args.execute or args.resume,
                   resume=args.resume,
               ))
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
