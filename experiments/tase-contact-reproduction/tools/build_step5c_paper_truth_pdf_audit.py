#!/usr/bin/env python3
"""Build a fail-closed PDF-text audit for Step5c/Step5d paper truth.

The audit verifies only what the local PDF text directly supports. It does not
enable strict RNN reproduction, does not run ROS/Gazebo, and does not authorize
any live robot path.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = EXPERIMENT_ROOT.parents[1]
PAPER_TRUTH = EXPERIMENT_ROOT / "config" / "step5c_tase_paper_truth.json"

VERIFIED_FIELD_RULES: dict[str, tuple[tuple[str, str], ...]] = {
    "finite_time_rnn_state_equation": (
        ("finite-time controller section", r"finite-time convergent"),
        ("recurrent neural network section", r"recurrent neural network"),
        ("Eq.23a state equation", r"\(23a\)"),
        ("Eq.23b lambda equation", r"\(23b\)"),
    ),
    "rnn_gain_scalar_not_matrix": (
        ("Eq.23 tunable epsilon/r", r"parameter\s+.*>\s*0.*r\s*∈\s*\[0,\s*1\]"),
        ("Eq.30 scalar gain binding", r"κ\s*>\s*0.*%\s*=\s*1"),
    ),
    "sigr_exponent_r_domain_and_examples": (
        ("r domain", r"r\s*∈\s*\[0,\s*1\]"),
        ("finite-time interval", r"0\s*<\s*r\s*<\s*1"),
        ("r example 0.2", r"r\s*=\s*0\.2"),
    ),
    "force_motion_task_equations": (
        ("impedance model Eq.11", r"Md\s*\("),
        ("force error Eq.15", r"e\s*p\s*=\s*xpd.*e\s*f\s*=\s*Fd\s*−\s*F"),
        ("force acceleration Eq.16", r"ẍ\s*p\s*=.*Bd\s*ẋ\s*p.*Md"),
        ("velocity-level Eq.17", r"ẋ\s*p\s*\(t\).*ΦO.*Φ̄O"),
    ),
    "orientation_compliance_equations": (
        ("orientation matrix Eq.9", r"cross-product matrix"),
        ("desired orientation Eq.10", r"Rd\s*=.*sin"),
        ("quaternion error Eq.13", r"Q−1"),
        ("orientation command Eq.14", r"ẋo\s*=\s*x˙od\s*\+\s*ko"),
        ("Remark 1 zero desired angular velocity", r"ẋod\s*=\s*0"),
    ),
    "constraint_qp_or_kkt_form": (
        ("dynamic programming Eq.20", r"min\s*θ̇\s*θ̇/2"),
        ("Lagrange function Eq.21", r"L\s*=\s*θ̇\s*θ̇/2\s*\+\s*λT"),
        ("KKT conditions", r"Karush-Kuhn-Tucker"),
        ("projection Eq.22", r"PΩ"),
    ),
    "paper_section_vi_experimental_parameter_anchors": (
        ("Section VI parameters", r"parameters in the controller are set"),
        ("Md/Bd values", r"Md\s*=\s*Diag\(12.*Bd\s*=\s*Diag\(550"),
        ("epsilon and gains", r"0\.022.*k\s*p\s*=\s*4.*ko\s*=\s*5.*k\s*f\s*=\s*1"),
        ("paper experimental qdot bounds", r"0\.15\s*rad/s"),
    ),
}

PARTIAL_OR_UNRESOLVED_FIELDS = {
    "alpha_escape_velocity_gain": "PDF states alpha > 0 and Eq.18/Eq.19 bounds, but no UR10e numeric alpha is specified.",
    "production_sigr_exponent_r": "PDF gives r domain and simulation examples; no single production r is selected for UR10e.",
    "local_qdot_bound_rad_s": "PDF Section VI uses +/-0.15 rad/s; the repo's 0.30 rad/s value is a local offline/live-prep adaptation until separately justified.",
    "communication_delay_T_mapping": "PDF defines T as robot-controller communication delay, but does not map it to this repo's dt_s assumption.",
    "Eq23_nonzero_command_stability": "PDF provides continuous finite-time proof, but the printed Eq.23 sign convention and local discrete zero-initial-lambda nonzero-command gate remain separate fail-closed checks.",
    "step5c_strict_dryrun.which paper equations remain active in no-contact dry-run": "No-contact dry-run is a repo adaptation, not a direct paper mode.",
    "step5c_strict_dryrun.mapping from paper task variable to UR10e 6dof qdot": "Paper is generic n-DOF/FRANKA evidence; UR10e 6DOF mapping needs local kinematics evidence.",
    "step5c_strict_contact.filtered-live normal compatibility": "Paper reports no force-signal filter in experiments; repo filtered-live normal policy is a local adaptation.",
    "step5c_strict_contact.force ladder parameters": "Force ladder/contact-entry parameters are repo safety policy, not direct paper parameters.",
}

SIGN_CONSISTENCY_RULES: tuple[tuple[str, str], ...] = (
    ("Eq.21 Lagrange sign", r"L\s*=\s*θ̇\s*θ̇/2\s*\+\s*λT\s*\(J\(θ\)θ̇\s*−\s*ẋc\s*\)"),
    ("Eq.23a printed projection sign", r"θ̇\s*−\s*PΩ\s*\(θ̇\s*−\s*\(θ̇\s*−\s*J\s*T\s*\(θ\)λ\)\)"),
    ("Eq.23b printed lambda residual", r"λ̇\s*=\s*J\(θ\)θ̇\s*−\s*ẋc?"),
    ("Appendix Lyapunov projection sign", r"kθ̇\s*−\s*PΩ\s*\(θ̇\s*−\s*\(θ̇\s*−\s*J\s*T\s*\(θ\)λ\)\)k"),
)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(WORKSPACE.resolve()))
    except ValueError:
        return str(path)


def extract_pdf_text(pdf_path: Path) -> str:
    if not pdf_path.exists():
        raise FileNotFoundError(f"paper PDF missing: {pdf_path}")
    with tempfile.TemporaryDirectory(prefix="step5c_pdf_text_") as tmpdir:
        text_path = Path(tmpdir) / "paper.txt"
        subprocess.run(["pdftotext", "-layout", str(pdf_path), str(text_path)], check=True)
        return text_path.read_text(encoding="utf-8", errors="replace")


def find_rule_hits(lines: list[str], rules: tuple[tuple[str, str], ...]) -> tuple[bool, list[dict[str, Any]]]:
    hits: list[dict[str, Any]] = []
    all_found = True
    for label, pattern in rules:
        compiled = re.compile(pattern, re.IGNORECASE)
        match_line = None
        for index, line in enumerate(lines, start=1):
            if compiled.search(line):
                match_line = index
                break
        if match_line is None:
            all_found = False
        hits.append({"label": label, "pattern": pattern, "line": match_line, "found": match_line is not None})
    return all_found, hits


def pending_fields(payload: dict[str, Any]) -> list[str]:
    pending = [str(field) for field in payload.get("pending_pdf_verify", [])]
    for section_name, section in payload.get("sections", {}).items():
        if not isinstance(section, dict):
            continue
        for field in section.get("pending_pdf_verify", []):
            pending.append(f"{section_name}.{field}")
    return sorted(pending)


def eq23_sign_consistency_audit(lines: list[str]) -> dict[str, Any]:
    _, hits = find_rule_hits(lines, SIGN_CONSISTENCY_RULES)
    found = {hit["label"]: bool(hit["found"]) for hit in hits}
    printed_eq23_sign_anchors_present = bool(
        found.get("Eq.23a printed projection sign")
        and found.get("Eq.23b printed lambda residual")
        and found.get("Appendix Lyapunov projection sign")
    )
    kkt_sign_tension = bool(found.get("Eq.21 Lagrange sign") and printed_eq23_sign_anchors_present)
    return {
        "status": "blocked_pdf_printed_sign_requires_local_discrete_gate",
        "claim_tier": "virtual/software force-loop",
        "line_anchors": hits,
        "printed_eq23_sign_anchors_present": printed_eq23_sign_anchors_present,
        "kkt_sign_tension_from_eq21_and_printed_eq23": kkt_sign_tension,
        "local_discrete_gate_required": True,
        "acceptance_effect": "does_not_clear_Eq23_nonzero_command_stability_or_strict_rnn_final_acceptance",
    }


def build_audit(
    *,
    generated_at: str | None = None,
    paper_truth_path: Path = PAPER_TRUTH,
    paper_text: str | None = None,
) -> dict[str, Any]:
    generated = generated_at or datetime.now().isoformat(timespec="seconds")
    truth = load_json(paper_truth_path)
    pdf_path = Path(truth["pdf_source"])
    text = paper_text if paper_text is not None else extract_pdf_text(pdf_path)
    lines = text.splitlines()

    verified: list[str] = []
    unverified: list[str] = []
    field_evidence: dict[str, Any] = {}
    for field, rules in VERIFIED_FIELD_RULES.items():
        proven, hits = find_rule_hits(lines, rules)
        field_evidence[field] = {
            "status": "pdf_text_proven" if proven else "missing_required_pdf_text_anchor",
            "claim_tier": "virtual/software force-loop",
            "line_anchors": hits,
        }
        if proven:
            verified.append(field)
        else:
            unverified.append(field)
    eq23_sign_consistency = eq23_sign_consistency_audit(lines)

    current_pending = pending_fields(truth)
    unresolved = sorted(field for field in current_pending if field in PARTIAL_OR_UNRESOLVED_FIELDS)
    unexpected_pending = sorted(field for field in current_pending if field not in PARTIAL_OR_UNRESOLVED_FIELDS)
    expected_pending = sorted(PARTIAL_OR_UNRESOLVED_FIELDS)
    missing_expected_pending = sorted(field for field in expected_pending if field not in current_pending)
    audit_ok = not unverified and not unexpected_pending and not missing_expected_pending and unresolved == expected_pending
    return {
        "schema": "ur10e_step5c_paper_truth_pdf_audit_v1",
        "generated_at": generated,
        "paper_truth": rel(paper_truth_path),
        "pdf_source": str(pdf_path),
        "claim_tier": "virtual/software force-loop",
        "status": "partial_pdf_truth_verified_strict_rnn_still_blocked",
        "strict_rnn_enabled": bool(truth.get("strict_rnn_enabled")),
        "audit_ok": audit_ok,
        "pdf_text_line_count": len(lines),
        "verified_fields": verified,
        "unverified_expected_fields": unverified,
        "remaining_pending_fields": current_pending,
        "remaining_pending_count": len(current_pending),
        "expected_remaining_pending_fields": expected_pending,
        "missing_expected_pending_fields": missing_expected_pending,
        "unexpected_pending_fields": unexpected_pending,
        "partial_or_unresolved_evidence": {field: PARTIAL_OR_UNRESOLVED_FIELDS[field] for field in unresolved},
        "field_evidence": field_evidence,
        "eq23_sign_consistency": eq23_sign_consistency,
        "acceptance_effect": (
            "Core paper equations and Section VI parameter anchors are text-verified, but strict RNN final acceptance "
            "remains blocked by local adaptation/stability fields and strict_rnn_enabled=false."
        ),
        "forbidden_claim": (
            "simulated_ft; physical Gazebo collision/contact physics; real bench/live contact; live bridge/TP/URScript/motion"
        ),
    }


def write_audit(output_dir: Path, *, generated_at: str | None = None) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "step5c_paper_truth_pdf_audit.json"
    payload = build_audit(generated_at=generated_at)
    payload["artifact_path"] = str(path)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--generated-at", default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    print(write_audit(args.output_dir, generated_at=args.generated_at))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
