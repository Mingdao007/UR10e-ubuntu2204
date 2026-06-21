#!/usr/bin/env python3
"""Audit local Step5d strict-RNN adaptations without enabling final acceptance.

This gate stays offline. It does not run ROS, Gazebo, a bridge, a controller, or
any live bench surface. Its job is to bind each currently pending paper-truth
field to local evidence where possible, and to keep final strict-RNN acceptance
blocked when the evidence is local-only, synthetic-only, or contradicted by a
targeted discrete sanity check.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import tempfile
from typing import Any

import numpy as np

from step5c_strict_rnn import StrictRnnConfig, StrictTaseRnnSolver, sigr


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = EXPERIMENT_ROOT.parents[1]
PAPER_TRUTH = EXPERIMENT_ROOT / "config" / "step5c_tase_paper_truth.json"
PDF_AUDIT = (
    EXPERIMENT_ROOT
    / "runs"
    / "ur10e_gazebo_17h_sim_ft_rnn_20260621_0905_step5c_pdf_truth_audit"
    / "step5c_paper_truth_pdf_audit.json"
)
NUMERIC_SANITY = (
    EXPERIMENT_ROOT
    / "runs"
    / "step5d_numeric_sanity_20260614_215555"
    / "step5d_numeric_sanity.json"
)
MIDRUN_GATE_FREEZE_AT = "2026-06-21T10:33:50+08:00"
PENDING_FIELDS = [
    "Eq23_nonzero_command_stability",
    "alpha_escape_velocity_gain",
    "communication_delay_T_mapping",
    "local_qdot_bound_rad_s",
    "production_sigr_exponent_r",
    "step5c_strict_contact.filtered-live normal compatibility",
    "step5c_strict_contact.force ladder parameters",
    "step5c_strict_dryrun.mapping from paper task variable to UR10e 6dof qdot",
    "step5c_strict_dryrun.which paper equations remain active in no-contact dry-run",
]


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def rel(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return str(path.resolve().relative_to(WORKSPACE.resolve()))
    except (OSError, ValueError):
        return str(path)


def verified_truth_file() -> Path:
    payload = {"strict_rnn_enabled": True, "pending_pdf_verify": [], "sections": {}}
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False)
    json.dump(payload, handle)
    handle.close()
    return Path(handle.name)


def eq23_discrete_sign_sensitivity_probe(*, steps: int = 2000) -> dict[str, Any]:
    variants = [
        {
            "variant": "current_positive_projection_positive_lambda_update",
            "implementation_current": True,
            "projection_input_form": "+J.T @ lambda_state",
            "lambda_update_form": "lambda_state += (dt / epsilon) * (J @ theta_dot_state - xdot_c)",
            "projection_sign": 1.0,
            "lambda_update_sign": 1.0,
        },
        {
            "variant": "shadow_positive_projection_negative_lambda_update",
            "implementation_current": False,
            "projection_input_form": "+J.T @ lambda_state",
            "lambda_update_form": "lambda_state -= (dt / epsilon) * (J @ theta_dot_state - xdot_c)",
            "projection_sign": 1.0,
            "lambda_update_sign": -1.0,
        },
        {
            "variant": "shadow_negative_projection_positive_lambda_update",
            "implementation_current": False,
            "projection_input_form": "-J.T @ lambda_state",
            "lambda_update_form": "lambda_state += (dt / epsilon) * (J @ theta_dot_state - xdot_c)",
            "projection_sign": -1.0,
            "lambda_update_sign": 1.0,
        },
        {
            "variant": "shadow_negative_projection_negative_lambda_update",
            "implementation_current": False,
            "projection_input_form": "-J.T @ lambda_state",
            "lambda_update_form": "lambda_state -= (dt / epsilon) * (J @ theta_dot_state - xdot_c)",
            "projection_sign": -1.0,
            "lambda_update_sign": -1.0,
        },
    ]
    xdot = np.array([0.05, 0.0, 0.0, 0.0, 0.0, 0.0], dtype=float)
    dt_s = 0.002
    epsilon = 0.022
    r = 0.2
    lower = np.full(6, -0.15)
    upper = np.full(6, 0.15)
    rows: list[dict[str, Any]] = []
    for variant in variants:
        theta = np.zeros(6, dtype=float)
        lambda_state = np.zeros(6, dtype=float)
        residuals: list[float] = []
        hit_bound = False
        for _ in range(steps):
            proj_input = float(variant["projection_sign"]) * lambda_state
            projected = np.clip(proj_input, lower, upper)
            sigr_arg = theta - projected
            theta_delta = -(dt_s / epsilon) * np.asarray(sigr(sigr_arg, r), dtype=float)
            crosses_projection = np.abs(theta_delta) > np.abs(sigr_arg)
            theta = np.where(crosses_projection, projected, theta + theta_delta)
            residual = theta - xdot
            lambda_state = lambda_state + float(variant["lambda_update_sign"]) * (dt_s / epsilon) * residual
            residuals.append(float(np.linalg.norm(residual)))
            hit_bound = hit_bound or bool(np.any(np.isclose(projected, lower) | np.isclose(projected, upper)))
        residual_nonincreasing = all(
            residuals[index + 1] <= residuals[index] + 1e-12
            for index in range(len(residuals) - 1)
        )
        stable = bool(residuals[-1] < residuals[0] and residuals[-1] < 1e-3 and not hit_bound)
        rows.append(
            {
                **{key: value for key, value in variant.items() if not key.endswith("_sign")},
                "steps": steps,
                "dt_s": dt_s,
                "epsilon": epsilon,
                "r": r,
                "qdot_bound_rad_s": 0.15,
                "initial_residual_norm": residuals[0],
                "final_residual_norm": residuals[-1],
                "residual_nonincreasing": residual_nonincreasing,
                "hit_velocity_bound": hit_bound,
                "final_theta_dot_state": [float(value) for value in theta],
                "final_lambda_state": [float(value) for value in lambda_state],
                "stable_for_final_acceptance": stable,
            }
        )
    current = next(row for row in rows if row["implementation_current"])
    shadow_stable = [
        row["variant"]
        for row in rows
        if not row["implementation_current"] and row["stable_for_final_acceptance"]
    ]
    return {
        "probe": "identity_J_zero_initial_lambda_nonzero_xdot_c_sign_sensitivity",
        "claim_tier": "virtual/software force-loop",
        "status": "blocked_current_sign_unstable_shadow_variants_not_acceptance",
        "current_variant": current,
        "shadow_stable_variants": shadow_stable,
        "variants": rows,
        "acceptance_effect": "diagnostic_only_does_not_change_solver_or_clear_strict_rnn_final_acceptance",
    }


def nonzero_command_stability_probe(*, steps: int = 2000) -> dict[str, Any]:
    sign_sensitivity = eq23_discrete_sign_sensitivity_probe(steps=steps)
    truth_path = verified_truth_file()
    try:
        solver = StrictTaseRnnSolver(
            StrictRnnConfig(
                paper_truth_path=truth_path,
                qdot_limit_rad_s=0.15,
                epsilon=0.022,
                sigr_exponent_r=0.2,
            )
        )
        residuals: list[float] = []
        hit_bound = False
        last_diag = None
        for _ in range(steps):
            last_diag = solver.step(
                J=np.eye(6),
                xdot_c=np.array([0.05, 0.0, 0.0, 0.0, 0.0, 0.0]),
                omega_minus=np.full(6, -0.15),
                omega_plus=np.full(6, 0.15),
                dt=0.002,
                epsilon=0.022,
                r=0.2,
            )
            residuals.append(float(last_diag.constraint_residual_norm))
            hit_bound = hit_bound or any(last_diag.active_bounds_mask)
        assert last_diag is not None
        residual_nonincreasing = all(
            residuals[index + 1] <= residuals[index] + 1e-12
            for index in range(len(residuals) - 1)
        )
        stable = bool(residuals[-1] < residuals[0] and residuals[-1] < 1e-3 and not hit_bound)
        return {
            "probe": "identity_J_zero_initial_lambda_nonzero_xdot_c",
            "claim_tier": "virtual/software force-loop",
            "steps": steps,
            "dt_s": 0.002,
            "epsilon": 0.022,
            "r": 0.2,
            "qdot_bound_rad_s": 0.15,
            "initial_residual_norm": residuals[0],
            "final_residual_norm": residuals[-1],
            "residual_nonincreasing": residual_nonincreasing,
            "hit_velocity_bound": hit_bound,
            "final_theta_dot_state": [float(value) for value in last_diag.theta_dot_state],
            "final_lambda_state": [float(value) for value in last_diag.lambda_state],
            "stable_for_final_acceptance": stable,
            "sign_sensitivity": sign_sensitivity,
            "status": "passed" if stable else "blocked_discrete_printed_sign_nonzero_command_not_stable",
        }
    finally:
        truth_path.unlink(missing_ok=True)


def field_rows(
    *,
    paper_truth: dict[str, Any],
    pdf_audit: dict[str, Any],
    numeric_sanity: dict[str, Any],
    nonzero_probe: dict[str, Any],
) -> list[dict[str, Any]]:
    assumptions = numeric_sanity.get("assumptions", {}) if isinstance(numeric_sanity.get("assumptions"), dict) else {}
    gates = numeric_sanity.get("gates", {}) if isinstance(numeric_sanity.get("gates"), dict) else {}
    metrics = numeric_sanity.get("metrics", {}) if isinstance(numeric_sanity.get("metrics"), dict) else {}
    local_qdot_bound = assumptions.get("qdot_limit_rad_s")
    qdot_matches_pdf_anchor = (
        isinstance(local_qdot_bound, (int, float))
        and math.isclose(float(local_qdot_bound), 0.15, rel_tol=0.0, abs_tol=1e-9)
        and bool(gates.get("qdot_within_nominal_limit_pass"))
        and bool(numeric_sanity.get("overall_pass"))
    )
    qdot_status = (
        "local_pdf_anchor_bound_sanity_passed_not_final_acceptance"
        if qdot_matches_pdf_anchor
        else "blocked_local_bound_differs_from_pdf_anchor"
    )
    qdot_blocker = (
        "PDF Section VI +/-0.15 rad/s bound is matched by offline no-contact structural sanity, "
        "but strict RNN final acceptance remains blocked by paper-truth/local-adaptation gates"
        if qdot_matches_pdf_anchor
        else "current local full-chain sanity uses 0.30 rad/s while PDF Section VI anchor is +/-0.15 rad/s"
    )
    pdf_sign_consistency = pdf_audit.get("eq23_sign_consistency", {})
    return [
        {
            "field": "Eq23_nonzero_command_stability",
            "claim_tier": "virtual/software force-loop",
            "status": nonzero_probe["status"],
            "supports_strict_rnn_final_acceptance": False,
            "evidence": {
                **nonzero_probe,
                "paper_pdf_sign_consistency": pdf_sign_consistency,
            },
            "blocker": "local discrete zero-initial-lambda nonzero-command probe does not prove stable convergence",
        },
        {
            "field": "alpha_escape_velocity_gain",
            "claim_tier": "virtual/software force-loop",
            "status": "blocked_local_default_not_pdf_verified",
            "supports_strict_rnn_final_acceptance": False,
            "evidence": {
                "local_default_alpha_s_inv": assumptions.get("alpha_s_inv"),
                "pdf_audit_note": pdf_audit.get("partial_or_unresolved_evidence", {}).get("alpha_escape_velocity_gain"),
            },
            "blocker": "PDF gives alpha > 0 but no UR10e production numeric alpha",
        },
        {
            "field": "communication_delay_T_mapping",
            "claim_tier": "virtual/software force-loop",
            "status": "blocked_local_dt_assumption_not_pdf_mapping",
            "supports_strict_rnn_final_acceptance": False,
            "evidence": {
                "local_T_s": assumptions.get("T_s"),
                "pdf_audit_note": pdf_audit.get("partial_or_unresolved_evidence", {}).get("communication_delay_T_mapping"),
            },
            "blocker": "PDF T is robot-controller communication delay; repo maps it to dt_s only as a local assumption",
        },
        {
            "field": "local_qdot_bound_rad_s",
            "claim_tier": "virtual/software force-loop",
            "status": qdot_status,
            "supports_strict_rnn_final_acceptance": False,
            "evidence": {
                "pdf_section_vi_bound_rad_s": 0.15,
                "local_numeric_sanity_bound_rad_s": local_qdot_bound,
                "numeric_qdot_max_abs_rad_s": metrics.get("qdot_max_abs_rad_s"),
                "qdot_within_nominal_limit_pass": gates.get("qdot_within_nominal_limit_pass"),
                "numeric_sanity_overall_pass": numeric_sanity.get("overall_pass"),
                "outputs": numeric_sanity.get("outputs", {}),
            },
            "blocker": qdot_blocker,
        },
        {
            "field": "production_sigr_exponent_r",
            "claim_tier": "virtual/software force-loop",
            "status": "blocked_local_default_matches_example_not_production_selection",
            "supports_strict_rnn_final_acceptance": False,
            "evidence": {
                "local_sigr_exponent_r": assumptions.get("sigr_exponent_r"),
                "pdf_verified_fields": pdf_audit.get("verified_fields", []),
                "pdf_audit_note": pdf_audit.get("partial_or_unresolved_evidence", {}).get("production_sigr_exponent_r"),
            },
            "blocker": "PDF supports r domain/examples but no UR10e production selection",
        },
        {
            "field": "step5c_strict_contact.filtered-live normal compatibility",
            "claim_tier": "virtual/software force-loop",
            "status": "local_semantics_gate_required_not_pdf_verified",
            "supports_strict_rnn_final_acceptance": False,
            "evidence": {
                "force_sign_convention": assumptions.get("force_sign_convention"),
                "force_sign_evidence": assumptions.get("force_sign_evidence"),
            },
            "blocker": "filtered-live normal policy is repo-local and not a direct paper parameter",
        },
        {
            "field": "step5c_strict_contact.force ladder parameters",
            "claim_tier": "virtual/software force-loop",
            "status": "blocked_repo_safety_policy_not_paper_parameter",
            "supports_strict_rnn_final_acceptance": False,
            "evidence": {
                "contact_evidence": assumptions.get("contact_evidence"),
                "force_target_n": assumptions.get("force_target_n"),
            },
            "blocker": "force ladder/contact-entry parameters are local safety policy",
        },
        {
            "field": "step5c_strict_dryrun.mapping from paper task variable to UR10e 6dof qdot",
            "claim_tier": "virtual/software force-loop",
            "status": "local_structural_only_not_final_acceptance",
            "supports_strict_rnn_final_acceptance": False,
            "evidence": {
                "numeric_sanity_overall_pass": numeric_sanity.get("overall_pass"),
                "register_order_pass": gates.get("register_order_pass"),
                "finite_outputs_pass": gates.get("finite_outputs_pass"),
                "constraint_residual_norm_max": metrics.get("constraint_residual_norm_max"),
                "residuals_csv": numeric_sanity.get("outputs", {}).get("residuals_csv"),
            },
            "blocker": "recorded pose/q -> outer loop -> calibrated J -> qdot -> registers is structural-only and synthetic/no-contact",
        },
        {
            "field": "step5c_strict_dryrun.which paper equations remain active in no-contact dry-run",
            "claim_tier": "virtual/software force-loop",
            "status": "blocked_no_contact_dryrun_is_repo_adaptation",
            "supports_strict_rnn_final_acceptance": False,
            "evidence": {
                "numeric_sanity_force_input": assumptions.get("force_input"),
                "position_error_mode": assumptions.get("position_error_mode"),
                "contact_evidence": assumptions.get("contact_evidence"),
            },
            "blocker": "no-contact dry-run is not a direct paper mode",
        },
    ]


def pending_fields(payload: dict[str, Any]) -> list[str]:
    pending = [str(field) for field in payload.get("pending_pdf_verify", [])]
    for section_name, section in payload.get("sections", {}).items():
        if not isinstance(section, dict):
            continue
        for field in section.get("pending_pdf_verify", []):
            pending.append(f"{section_name}.{field}")
    return sorted(pending)


def build_audit(
    *,
    generated_at: str | None = None,
    numeric_sanity_path: Path = NUMERIC_SANITY,
) -> dict[str, Any]:
    generated = generated_at or datetime.now().isoformat(timespec="seconds")
    paper_truth = load_json(PAPER_TRUTH)
    pdf_audit = load_json(PDF_AUDIT) if PDF_AUDIT.exists() else {}
    numeric_sanity = load_json(numeric_sanity_path) if numeric_sanity_path.exists() else {}
    nonzero_probe = nonzero_command_stability_probe()
    rows = field_rows(
        paper_truth=paper_truth,
        pdf_audit=pdf_audit,
        numeric_sanity=numeric_sanity,
        nonzero_probe=nonzero_probe,
    )
    observed_fields = sorted(row["field"] for row in rows)
    pending = pending_fields(paper_truth)
    missing_pending_rows = sorted(set(pending) - set(observed_fields))
    unexpected_rows = sorted(set(observed_fields) - set(PENDING_FIELDS))
    supports_final_count = sum(1 for row in rows if row["supports_strict_rnn_final_acceptance"])
    blocked_or_local_only_count = sum(1 for row in rows if not row["supports_strict_rnn_final_acceptance"])
    audit_ok = not missing_pending_rows and not unexpected_rows and bool(pdf_audit.get("audit_ok")) and bool(
        numeric_sanity.get("overall_pass")
    )
    return {
        "schema": "ur10e_strict_rnn_local_adaptation_audit_v1",
        "generated_at": generated,
        "mode": "offline_no_live_strict_rnn_local_adaptation_audit",
        "status": "blocked_local_adaptation_fields_not_final_acceptance",
        "evidence_window": "post-checkpoint gated audit of pre-gate source artifacts",
        "checkpoint_boundary": {
            "freeze_at": MIDRUN_GATE_FREEZE_AT,
            "source_artifacts_evidence_window": "pre-gate evidence",
            "audit_artifact_evidence_window": "post-checkpoint gated evidence",
            "final_acceptance_effect": "does_not_clear_strict_rnn_final_acceptance",
        },
        "claim_tier": "virtual/software force-loop",
        "target_claim_tier": "virtual/software force-loop",
        "strict_rnn_final_acceptance_allowed": False,
        "audit_ok": audit_ok,
        "allowed_claim": (
            "post-checkpoint fail-closed report/verifier audit of local strict-RNN "
            "adaptation blockers at virtual/software force-loop tier only"
        ),
        "paper_truth": rel(PAPER_TRUTH),
        "paper_truth_strict_rnn_enabled": bool(paper_truth.get("strict_rnn_enabled")),
        "paper_truth_pending_fields": pending,
        "pdf_audit": rel(PDF_AUDIT) if PDF_AUDIT.exists() else None,
        "pdf_audit_ok": bool(pdf_audit.get("audit_ok")),
        "numeric_sanity": rel(numeric_sanity_path) if numeric_sanity_path.exists() else None,
        "numeric_sanity_overall_pass": bool(numeric_sanity.get("overall_pass")),
        "field_rows": rows,
        "supports_final_acceptance_count": supports_final_count,
        "blocked_or_local_only_count": blocked_or_local_only_count,
        "missing_pending_rows": missing_pending_rows,
        "unexpected_rows": unexpected_rows,
        "blockers": [
            "paper_truth:strict_rnn_disabled",
            "paper_truth:pending_pdf_verify",
            "local_adaptation:not_final_acceptance",
            "eq23_nonzero_command_stability:not_proven",
        ],
        "forbidden_claim": (
            "strict RNN final acceptance; simulated_ft; physical Gazebo collision/contact physics; "
            "real bench/live contact; live bridge/TP/URScript/motion"
        ),
    }


def write_audit(
    output_dir: Path,
    *,
    generated_at: str | None = None,
    numeric_sanity_path: Path = NUMERIC_SANITY,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "strict_rnn_local_adaptation_audit.json"
    payload = build_audit(generated_at=generated_at, numeric_sanity_path=numeric_sanity_path)
    payload["artifact_path"] = str(path)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--generated-at", default=None)
    parser.add_argument("--numeric-sanity", type=Path, default=NUMERIC_SANITY)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    print(
        write_audit(
            args.output_dir,
            generated_at=args.generated_at,
            numeric_sanity_path=args.numeric_sanity,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
