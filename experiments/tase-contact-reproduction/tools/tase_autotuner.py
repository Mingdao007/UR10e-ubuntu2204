"""Automatic TASE outer-loop campaign runner.

The tuner owns only candidate state and scheduling. Each candidate is executed
through the single ``figure8.sh`` owner, which performs one ten-second joint
qualification, one complete 62.831853 s PATH, automatic Home, and immutable
run receipts. A failed physical attempt consumes its ordinal and never becomes
a successful BO observation.
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
from scipy.stats import qmc


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
                "force_integral_limit_n_s": 5.0,
                "force_sign_convention": "step5_step6_positive_normal_load",
            },
        }


def _load_config(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != SCHEMA:
        raise ValueError("autotuner config schema differs")
    if payload.get("method") != "TASE_RNN_MATURE":
        raise ValueError("this runner only executes TASE_RNN_MATURE")
    budget = payload.get("budget", {})
    if budget != {"initial": 8, "bo": 12, "repeats": 4}:
        raise ValueError("autotuner budget must remain 8+12+4")
    if payload.get("protection_parameters_frozen") is not True or payload.get("integral_policy_frozen") is not True:
        raise ValueError("autotuner protection/integral freeze is missing")
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


def _next_bo_candidate(config: Mapping[str, Any], records: list[dict[str, Any]], index: int) -> Candidate:
    observed_x, observed_y = _observations(records)
    sampler = qmc.Sobol(d=2, scramble=True, seed=int(config["seed"]) + 1009 + index)
    pool = sampler.random(int(config["bo_pool_size"]))
    tried = {_key(float(row["Md_scalar"]), float(row["Bd_scalar"])) for row in records}
    if len(observed_y) < 2:
        choice = pool[index % len(pool)]
    else:
        normalized_observed = np.asarray([_normalized(config, row) for row in observed_x])
        length = 0.22
        distances = normalized_observed[:, None, :] - normalized_observed[None, :, :]
        kernel = np.exp(-0.5 * np.sum((distances / length) ** 2, axis=2))
        kernel += 1e-8 * np.eye(len(kernel))
        try:
            weights = np.linalg.solve(kernel, observed_y)
        except np.linalg.LinAlgError:
            weights = np.linalg.lstsq(kernel, observed_y, rcond=None)[0]
        normalized_pool = np.asarray([
            [float(config["log2_bounds"][name][0]) + point[j] *
             (float(config["log2_bounds"][name][1]) - float(config["log2_bounds"][name][0]))
             for j, name in enumerate(("Md_scalar", "Bd_scalar"))]
            for point in pool
        ])
        delta = normalized_pool[:, None, :] - normalized_observed[None, :, :]
        pool_kernel = np.exp(-0.5 * np.sum((delta / length) ** 2, axis=2))
        mean = pool_kernel @ weights
        variance = np.maximum(1e-12, 1.0 - np.sum(pool_kernel * pool_kernel, axis=1))
        sigma = np.sqrt(variance)
        incumbent = float(np.min(observed_y))
        improvement = incumbent - mean
        z = improvement / sigma
        ei = improvement * ndtr(z) + sigma * np.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)
        choice = pool[int(np.argmax(ei))]
    for offset in range(len(pool)):
        point = pool[(int(np.argmax(pool, axis=0)[0]) + index + offset) % len(pool)] if len(observed_y) < 2 else pool[(int(np.argmax(ei)) + offset) % len(pool)]
        md, bd = _scale_unit(config, point)
        if _key(md, bd) not in tried:
            return Candidate(f"bo-{index:02d}", "bo", index, md, bd)
    md, bd = _scale_unit(config, choice)
    return Candidate(f"bo-{index:02d}", "bo", index, md, bd)


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
                "finished_at": time.time(),
            }
            recovered.append(row)
            by_ordinal[ordinal] = row
    return [by_ordinal[key] for key in sorted(by_ordinal)], recovered


def _append(ledger: Path, row: Mapping[str, Any]) -> None:
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(dict(row), sort_keys=True, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _extract_mae(run_dir: Path) -> tuple[float | None, dict[str, Any]]:
    dispatch = json.loads((run_dir / "dispatch_receipt.json").read_text(encoding="utf-8"))
    path = dispatch.get("live_path") or {}
    if (
        dispatch.get("command") != "pilot"
        or path.get("kind") != "full_period"
        or dispatch.get("evidence_eligible") is not True
        or dispatch.get("error")
    ):
        return None, {"dispatch": dispatch, "complete_path": False,
                      "reason": "not_a_clean_full_period_receipt"}
    attempts = dispatch.get("attempts", [])
    pilot = next((item for item in attempts if item.get("phase") == "pilot"), None)
    metric_sources = [dispatch.get("evidence_metrics", {})]
    if isinstance(pilot, dict):
        evidence = pilot.get("evidence", {})
        if isinstance(evidence, dict):
            metric_sources.append(evidence.get("metrics", {}))
    for metrics in metric_sources:
        if not isinstance(metrics, dict):
            continue
        # ContactMetrics is the only live source accepted here.  Requiring all
        # three flags prevents a short qualification or censored path from
        # becoming a successful BO observation.
        if not (
            metrics.get("complete") is True
            and metrics.get("objective_eligible") is True
            and metrics.get("coverage_complete") is True
            and metrics.get("interrupted") is False
        ):
            continue
        for key in ("normal_force_mae_n", "force_mae_n", "mae_n", "mae"):
            if key in metrics and metrics[key] is not None:
                mae = float(metrics[key])
                if math.isfinite(mae):
                    return mae, {"dispatch": dispatch, "complete_path": True,
                                  "metrics": metrics}
    return None, {"dispatch": dispatch, "complete_path": False,
                  "reason": "full_period_metrics_incomplete"}


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


def run_campaign(config_path: Path, campaign_dir: Path, *, execute: bool, dry_run: bool = False) -> dict[str, Any]:
    config = _load_config(config_path)
    campaign_dir = campaign_dir.expanduser().resolve()
    campaign_dir.mkdir(parents=True, exist_ok=True)
    ledger = campaign_dir / "ledger.jsonl"
    records, recovered = _read_records(ledger)
    for row in recovered:
        # Preserve the original attempt identity while making an interrupted
        # attempt an explicit failed denominator entry before scheduling more.
        _append(ledger, row)
    total = sum(int(value) for value in config["budget"].values())
    script = ROOT / "scripts" / "figure8.sh"
    if execute and not script.is_file():
        raise RuntimeError(f"figure8 owner is missing: {script}")
    while len(records) < total:
        ordinal = len(records)
        candidate = _candidate_for(config, records, ordinal)
        candidate_file = campaign_dir / "candidates" / f"{ordinal:02d}-{candidate.candidate_id}.json"
        _write_candidate(candidate_file, candidate)
        run_dir = campaign_dir / "runs" / f"{ordinal:02d}-{candidate.candidate_id}"
        row = {
            "schema": "tase.autotuner-attempt-v1",
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
        _append(ledger, {**row, "status": "started"})
        if not execute:
            records.append({**row, "status": "planned", "mae_n": None})
            continue
        command = [str(script), "--method", "TASE_RNN_MATURE", "--control-cpu", "2",
                   "--run-dir", str(run_dir), "--parameter-file", str(candidate_file)]
        completed = subprocess.run(command, cwd=str(ROOT), check=False)
        if completed.returncode != 0 or not (run_dir / "dispatch_receipt.json").is_file():
            failure = {**row, "status": "failed", "mae_n": None,
                       "returncode": completed.returncode, "failure": "owner_failed_or_missing_receipt",
                       "finished_at": time.time()}
            _append(ledger, failure)
            records.append(failure)
            continue
        try:
            mae, evidence = _extract_mae(run_dir)
            status = "complete" if mae is not None else "failed"
            result = {**row, "status": status, "mae_n": mae,
                      "complete_path": bool(evidence.get("complete_path")),
                      "finished_at": time.time()}
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            result = {**row, "status": "failed", "mae_n": None,
                      "failure": f"receipt_parse:{type(exc).__name__}:{exc}",
                      "finished_at": time.time()}
        _append(ledger, result)
        records.append(result)
    complete = [row for row in records if row.get("status") == "complete" and row.get("mae_n") is not None]
    summary = {
        "schema": "tase.autotuner-summary-v1",
        "config": str(config_path),
        "campaign_dir": str(campaign_dir),
        "budget": config["budget"],
        "attempts": len(records),
        "complete_paths": len(complete),
        "failed_attempts": len(records) - len(complete),
        "best": None if not complete else min(complete, key=lambda row: float(row["mae_n"])),
        "claim_scope": "complete-path receipts only; no controller promotion or manuscript claim",
    }
    (campaign_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--campaign-dir", type=Path, required=True)
    parser.add_argument("--plan", action="store_true", help="write the schedule without touching hardware")
    parser.add_argument("--execute", action="store_true", help="run the complete sequential campaign")
    args = parser.parse_args(argv)
    if args.plan == args.execute:
        parser.error("choose exactly one of --plan or --execute")
    summary = run_campaign(args.config, args.campaign_dir, execute=args.execute)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
