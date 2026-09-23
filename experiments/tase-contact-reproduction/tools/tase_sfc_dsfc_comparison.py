"""Matched offline TASE-native-SFC versus TASE-native-DSFC model comparison.

This is a model-only campaign. It runs the frozen A normal/posture loop with
source-bound native SFC and DSFC parameters under the same deterministic
surface and disturbance seeds. Each per-attempt trace is retained separately
from its summary so the comparison can be replayed and audited.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

import numpy as np

import tase_research_campaign as research


SCHEMA = "ur10e.tase-sfc-dsfc-matched-offline-comparison-v1"
METHODS = ("TASE_RNN_MATURE+SFC_YIELD_V1", "TASE_RNN_MATURE+DSFC_YIELD_V1")
SCENARIOS = ("plane", "tangent_pulse", "normal_pulse")
BLOCKS = 5
DEFAULT_HORIZON_TICKS = 30_000  # 60 s at the shared 500 Hz simulation step.
PULSE_START_FRACTION = 0.35
PULSE_END_FRACTION = 0.55
PULSE_MAGNITUDE_N = 0.6  # Reuses the existing offline proxy pulse amplitude.
NORMAL_A_INTEGRAL_LIMIT_N_S = 1.0
OUTER_PROFILE_ID = "r013_60_rate400_A_incumbent_integral_1p0"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_frozen_profiles() -> dict[str, Any]:
    root = _repo_root()
    route_path = root / "config" / "yield_native_route_v1.json"
    route = json.loads(route_path.read_text(encoding="utf-8"))
    source_path = root / str(route["law_parameters_source"])
    source_bytes = source_path.read_bytes()
    source_sha = hashlib.sha256(source_bytes).hexdigest()
    if source_sha != route.get("law_parameters_source_sha256"):
        raise ValueError("frozen native law source digest differs from route config")
    source = json.loads(source_bytes.decode("utf-8"))
    law_parameters = route.get("law_parameters")
    if not isinstance(law_parameters, dict):
        raise ValueError("route config has no frozen native law parameters")
    if source.get("parameters") != law_parameters:
        raise ValueError("frozen native law parameters differ from retained source")
    if not {"SFC", "DSFC"}.issubset(law_parameters):
        raise ValueError("route config must freeze both native SFC and DSFC")

    incumbent_path = root / "config" / "tase_figure8_integral_0p1_rate400.json"
    incumbent = json.loads(incumbent_path.read_text(encoding="utf-8"))
    frozen = incumbent["frozen"]
    outer = {
        "kp": float(frozen["kp"]),
        "ko": float(frozen["ko"]),
        "kf": float(frozen["kf"]),
        "Md_scalar": float(incumbent["Md_scalar"]),
        "Bd_scalar": float(incumbent["Bd_scalar"]),
        "force_target_n": float(frozen["force_target_n"]),
        "force_integral_limit_n_s": NORMAL_A_INTEGRAL_LIMIT_N_S,
        "force_integral_policy": "legacy-clamp-v1",
        "force_integral_authority_error_n": float(frozen["force_integral_authority_error_n"]),
        "force_sign_convention": str(frozen["force_sign_convention"]),
        "delay_T_s": 0.004,
    }
    expected_start = (9.565272137974492, 693.6559295653944)
    if not np.allclose(
        (outer["Md_scalar"], outer["Bd_scalar"]), expected_start,
        rtol=0.0, atol=1e-12,
    ):
        raise ValueError("A comparison must start from the accepted RNN incumbent")
    normal = research._candidate("TASE_RNN_MATURE", 0)
    normal_config = {
        key: normal[key]
        for key in ("epsilon", "sigr_exponent_r", "lambda_update_sign")
    }
    return {
        "normal_controller": "TASE_RNN_MATURE",
        "normal_config": normal_config,
        "normal_outer_config": outer,
        "outer_profile_id": OUTER_PROFILE_ID,
        "tangential_parameters": {name: dict(law_parameters[name]) for name in ("SFC", "DSFC")},
        "law_parameter_source": str(route["law_parameters_source"]),
        "law_parameter_source_sha256": source_sha,
        "route_config": "config/yield_native_route_v1.json",
        "route_config_sha256": hashlib.sha256(route_path.read_bytes()).hexdigest(),
        "observer_profile_id": "yield_normal_observer_v3",
        "observer_parameters": research._normal_observer_profile()[1],
        "path_stiffness_n_per_m": 120.0,
        "path_stiffness_source": "existing TaseSfcComposedOfflineAdapter default",
        "dt_s": research.DT_S,
        "target_force_n": research.FORCE_TARGET_N,
        "joint_velocity_bound_rad_s": research.QDOT_LIMIT_RAD_S,
    }


def _candidate(profile: dict[str, Any], law: str) -> dict[str, Any]:
    return {
        "normal_controller": profile["normal_controller"],
        "normal_config": dict(profile["normal_config"]),
        "normal_outer_config": dict(profile["normal_outer_config"]),
        "outer_profile_id": profile["outer_profile_id"],
        "tangential_controller": law,
        "tangential_parameters": dict(profile["tangential_parameters"][law]),
        "tangential_parameter_source": f"config/yield_native_route_v1.json#law_parameters.{law}",
        "path_stiffness_n_per_m": profile["path_stiffness_n_per_m"],
        "initial_normal": [0.0, 0.0, 1.0],
        "dt_s": profile["dt_s"],
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git_context() -> dict[str, Any]:
    root = _repo_root()
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--short"], cwd=root, check=True,
        capture_output=True, text=True,
    ).stdout.splitlines()
    return {"head": head, "modified_paths_at_run": status}


def _aggregate(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for scenario in SCENARIOS:
        result[scenario] = {}
        for method in METHODS:
            rows = [
                item for item in attempts
                if item["case"] == scenario and item["method"] == method
            ]
            valid = [
                float(item["metrics"]["normal_force_mae_n"])
                for item in rows
                if not item["metrics"]["failed"]
                and item["metrics"]["normal_force_mae_n"] is not None
            ]
            result[scenario][method] = {
                "attempts": len(rows),
                "valid": len(valid),
                "failed": len(rows) - len(valid),
                "model_normal_force_mae_mean_n": float(np.mean(valid)) if valid else None,
                "model_normal_force_mae_values_n": valid,
                "path_error_rms_mean_m": float(np.mean([
                    float(item["metrics"]["path_error_rms_m"])
                    for item in rows
                    if not item["metrics"]["failed"]
                    and item["metrics"]["path_error_rms_m"] is not None
                ])) if valid else None,
                "max_force_norm_n": max((
                    float(item["metrics"]["force_norm_max_n"])
                    for item in rows if item["metrics"]["force_norm_max_n"] is not None
                ), default=None),
                "saturation_ticks_total": sum(int(item["metrics"]["saturation_ticks"]) for item in rows),
            }
    paired: dict[str, Any] = {}
    sfc_method, dsfc_method = METHODS
    for scenario in SCENARIOS:
        deltas = []
        for block in range(BLOCKS):
            key = f"sfc-dsfc:block:{block:02d}:scenario:{scenario}"
            by_method = {
                item["method"]: item for item in attempts
                if item["case"] == scenario and item["trial_key"] == key
            }
            if set(by_method) != set(METHODS):
                deltas.append({"block": block, "valid_pair": False})
                continue
            left, right = by_method[sfc_method], by_method[dsfc_method]
            if left["metrics"]["failed"] or right["metrics"]["failed"]:
                deltas.append({"block": block, "valid_pair": False})
                continue
            sfc_mae = float(left["metrics"]["normal_force_mae_n"])
            dsfc_mae = float(right["metrics"]["normal_force_mae_n"])
            deltas.append({
                "block": block,
                "valid_pair": True,
                "dsfc_minus_sfc_model_mae_n": dsfc_mae - sfc_mae,
                "sfc_model_mae_n": sfc_mae,
                "dsfc_model_mae_n": dsfc_mae,
            })
        valid_deltas = [row["dsfc_minus_sfc_model_mae_n"] for row in deltas if row["valid_pair"]]
        paired[scenario] = {
            "pairs": deltas,
            "valid_pairs": len(valid_deltas),
            "descriptive_mean_dsfc_minus_sfc_n": float(np.mean(valid_deltas)) if valid_deltas else None,
            "interpretation": "descriptive model comparison only; no physical or statistical superiority claim",
        }
    return {"by_scenario_method": result, "paired_descriptive_deltas": paired}


def _render_report(summary: dict[str, Any]) -> str:
    lines = [
        "# TASE + native SFC/DSFC matched offline comparison",
        "",
        "Evidence level: **offline closed-loop model proxy only**. These force values are generated by the proxy plant; they are neither UR10e feedback nor independent task-force truth. This report does not qualify either combination for live use.",
        "",
        f"Frozen normal controller: `{summary['profile']['normal_controller']}`, A outer profile `{summary['profile']['outer_profile_id']}` with Md={summary['profile']['normal_outer_config']['Md_scalar']}, Bd={summary['profile']['normal_outer_config']['Bd_scalar']}, integral cap={summary['profile']['normal_outer_config']['force_integral_limit_n_s']} N·s.",
        "",
        "Both laws use the same normal observer, reference trajectory, 120 N/m path-restoring term, 0.002 s simulation step, 5 N target, and ±0.05 rad/s joint-velocity bounds. Each tangent output is projected and composed before exactly one TASE final realization. No surface geometry is provided to the controller.",
        "",
        "The 0.6 N tangent/normal pulses are software inputs reused from the existing offline proxy. Each SFC/DSFC pair shares the deterministic disturbance/noise seed; because this is closed-loop simulation, subsequent sensed state is allowed to diverge with the controller response. This is not a recorded-input counterfactual replay.",
        "",
        "| Scenario | SFC valid / planned | SFC model MAE (N) | DSFC valid / planned | DSFC model MAE (N) | DSFC − SFC (N) |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    stats = summary["results"]["by_scenario_method"]
    pairs = summary["results"]["paired_descriptive_deltas"]
    sfc_method, dsfc_method = METHODS
    for scenario in SCENARIOS:
        sfc = stats[scenario][sfc_method]
        dsfc = stats[scenario][dsfc_method]
        delta = pairs[scenario]["descriptive_mean_dsfc_minus_sfc_n"]
        lines.append(
            f"| {scenario} | {sfc['valid']}/{sfc['attempts']} | {sfc['model_normal_force_mae_mean_n']} | "
            f"{dsfc['valid']}/{dsfc['attempts']} | {dsfc['model_normal_force_mae_mean_n']} | {delta} |"
        )
    lines.extend([
        "",
        "| Scenario | SFC path RMS (m) | DSFC path RMS (m) | SFC joint-bound-hit ticks (%) | DSFC joint-bound-hit ticks (%) | SFC/DSFC max force norm (N) |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    horizon_ticks = int(summary["protocol"]["horizon_ticks"])
    for scenario in SCENARIOS:
        sfc = stats[scenario][sfc_method]
        dsfc = stats[scenario][dsfc_method]
        sfc_total = max(1, int(sfc["valid"]) * horizon_ticks)
        dsfc_total = max(1, int(dsfc["valid"]) * horizon_ticks)
        sfc_bound_pct = 100.0 * int(sfc["saturation_ticks_total"]) / sfc_total
        dsfc_bound_pct = 100.0 * int(dsfc["saturation_ticks_total"]) / dsfc_total
        norm_text = f"{sfc['max_force_norm_n']} / {dsfc['max_force_norm_n']}"
        lines.append(
            f"| {scenario} | {sfc['path_error_rms_mean_m']} | {dsfc['path_error_rms_mean_m']} | "
            f"{sfc_bound_pct:.2f} | {dsfc_bound_pct:.2f} | {norm_text} |"
        )
    lines.extend([
        "",
        "## Identity and limits",
        "",
        f"- SFC composition: `{METHODS[0]}` / `{summary['profile']['native_composition_ids']['SFC']}`.",
        f"- DSFC composition: `{METHODS[1]}` / `{summary['profile']['native_composition_ids']['DSFC']}`.",
        f"- Native-law source: `{summary['profile']['law_parameter_source']}` (source SHA recorded in `summary.json`).",
        "- Five matched blocks per scenario; failed/incomplete attempts remain in the denominator. The displayed difference is descriptive and does not establish a winner.",
        "- Joint-bound-hit percentages count proxy samples whose realized qdot reached the ±0.05 rad/s box. They are not physical actuator saturation measurements; high fractions indicate that this simulation is operating at its speed boundary and should not be used to rank force quality without a better plant model.",
        "- The simulation is a diagnostic proxy with identity Jacobian and simplified plant. Model prediction, historical recorded-feedback diagnostics, and future live results must remain separate.",
        "- Raw command traces are stored in `traces/*.jsonl.gz`; each row separates controller inputs, realized `Jqdot`, and scoring-only model state.",
    ])
    return "\n".join(lines) + "\n"


def run_comparison(
    *,
    output_dir: Path,
    qp_library: Path | None = None,
    blocks: int = BLOCKS,
    horizon_ticks: int = DEFAULT_HORIZON_TICKS,
    seed: int = 20260924,
) -> dict[str, Any]:
    if isinstance(blocks, bool) or not isinstance(blocks, int) or blocks < 1:
        raise ValueError("blocks must be a positive integer")
    if isinstance(horizon_ticks, bool) or not isinstance(horizon_ticks, int) or horizon_ticks < 8:
        raise ValueError("horizon_ticks must be an integer >= 8")
    out = Path(output_dir).resolve()
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"refusing to overwrite non-empty output: {out}")
    out.mkdir(parents=True, exist_ok=True)
    trace_dir = out / "traces"
    trace_dir.mkdir()
    profile = _load_frozen_profiles()
    if blocks != BLOCKS:
        planned_blocks = blocks
    else:
        planned_blocks = BLOCKS
    config = research.CampaignConfig(
        horizon_ticks=horizon_ticks,
        seed=seed,
        initial_units=1,
        bo_units=1,
        repeat_units=1,
        holdout_rounds=1,
    )
    library = Path(qp_library or ".").resolve()
    attempts: list[dict[str, Any]] = []
    attempts_path = out / "attempts.jsonl"
    with attempts_path.open("w", encoding="utf-8") as attempt_stream:
        for scenario in SCENARIOS:
            for block in range(planned_blocks):
                trial_key = f"sfc-dsfc:block:{block:02d}:scenario:{scenario}"
                for method in METHODS:
                    law = "SFC" if method == METHODS[0] else "DSFC"
                    candidate = _candidate(profile, law)
                    attempt_id = f"{method}-block-{block:02d}-{scenario}"
                    result = research.run_attempt(
                        method=method,
                        candidate=candidate,
                        case_name=scenario,
                        attempt_id=attempt_id,
                        config=config,
                        qp_library=library,
                        trial_key=trial_key,
                        include_trace=True,
                    )
                    trace_rows = result.pop("trace_rows")
                    trace_path = trace_dir / f"{attempt_id}.jsonl.gz"
                    with gzip.open(trace_path, "wt", encoding="utf-8", newline="\n") as trace_stream:
                        for row in trace_rows:
                            trace_stream.write(json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
                    result["trace_path"] = str(trace_path.relative_to(out))
                    result["trace_sha256"] = _sha256(trace_path)
                    attempts.append(result)
                    attempt_stream.write(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
                    attempt_stream.flush()

    source_fields = {
        "normal_controller": profile["normal_controller"],
        "normal_config": profile["normal_config"],
        "normal_outer_config": profile["normal_outer_config"],
        "outer_profile_id": profile["outer_profile_id"],
        "native_composition_ids": {
            "SFC": "TASE_NORMAL_ORIENTATION+SFC_TANGENTIAL_YIELD_V1",
            "DSFC": "TASE_NORMAL_ORIENTATION+DSFC_TANGENTIAL_YIELD_V1",
        },
        "law_parameter_source": profile["law_parameter_source"],
        "law_parameter_source_sha256": profile["law_parameter_source_sha256"],
        "route_config": profile["route_config"],
        "route_config_sha256": profile["route_config_sha256"],
        "observer_profile_id": profile["observer_profile_id"],
        "observer_parameters": profile["observer_parameters"],
        "path_stiffness_n_per_m": profile["path_stiffness_n_per_m"],
        "path_stiffness_source": profile["path_stiffness_source"],
        "dt_s": profile["dt_s"],
        "target_force_n": profile["target_force_n"],
        "joint_velocity_bound_rad_s": profile["joint_velocity_bound_rad_s"],
        "tangential_parameters": profile["tangential_parameters"],
    }
    summary: dict[str, Any] = {
        "schema": SCHEMA,
        "evidence_class": "offline_closed_loop_model_proxy_only",
        "physical_evidence": False,
        "blocks": planned_blocks,
        "planned_attempts": planned_blocks * len(SCENARIOS) * len(METHODS),
        "attempts": len(attempts),
        "failed_attempts": sum(bool(item["metrics"]["failed"]) for item in attempts),
        "protocol": {
            "methods": list(METHODS),
            "scenarios": list(SCENARIOS),
            "horizon_ticks": horizon_ticks,
            "simulated_duration_s": horizon_ticks * research.DT_S,
            "matched_trial_key_contract": "same block and scenario share exogenous random seed across SFC and DSFC",
            "disturbance_pulse_n": PULSE_MAGNITUDE_N,
            "disturbance_active_fraction": [PULSE_START_FRACTION, PULSE_END_FRACTION],
            "note": "closed-loop simulated sensor inputs diverge after commands affect the proxy plant; this is not a same-input replay",
        },
        "profile": source_fields,
        "results": _aggregate(attempts),
        "git_context": _git_context(),
        "artifacts": {},
    }
    report_path = out / "report.md"
    report_path.write_text(_render_report(summary), encoding="utf-8")
    summary["artifacts"] = {
        "attempts_sha256": _sha256(attempts_path),
        "report_sha256": _sha256(report_path),
        "trace_count": len(list(trace_dir.glob("*.jsonl.gz"))),
    }
    summary_path = out / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--blocks", type=int, default=BLOCKS)
    parser.add_argument("--horizon-ticks", type=int, default=DEFAULT_HORIZON_TICKS)
    parser.add_argument("--seed", type=int, default=20260924)
    args = parser.parse_args(argv)
    summary = run_comparison(
        output_dir=args.output_dir,
        blocks=args.blocks,
        horizon_ticks=args.horizon_ticks,
        seed=args.seed,
    )
    print(json.dumps({
        "schema": summary["schema"],
        "output_dir": str(args.output_dir.resolve()),
        "attempts": summary["attempts"],
        "failed_attempts": summary["failed_attempts"],
        "evidence_class": summary["evidence_class"],
    }, sort_keys=True))
    return 0 if summary["failed_attempts"] == 0 else 2


__all__ = [
    "SCHEMA", "METHODS", "SCENARIOS", "BLOCKS", "DEFAULT_HORIZON_TICKS",
    "run_comparison",
]


if __name__ == "__main__":
    raise SystemExit(main())
