#!/usr/bin/env python3
"""Fail-closed claim-boundary verifier for UR10e Gazebo reports.

This gate enforces the current no-live UR10e/Gazebo reproduction evidence
boundary. It is intentionally conservative: missing or ambiguous evidence
language fails rather than being upgraded by prose.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(?P<text>.+?)\s*#*\s*$")

REQUIRED_TIERS = (
    "visual_only",
    "virtual/software force-loop",
    "simulated_ft",
    "physical Gazebo collision/contact physics",
    "real Kunwei read-only",
    "real bench/live contact",
)

SIMULATED_FT_REQUIRED_FIELDS = (
    "stamp",
    "frame_id",
    "source",
    "status",
    "baseline",
    "log evidence",
)

PHYSICAL_GAZEBO_REQUIRED_FIELDS = (
    "EOAT collision evidence",
    "contact pair/log evidence",
    "wrench/contact correlation",
)

PHYSICAL_BLOCKERS = (
    "eoat_collision_count=0",
    "force_contact_physics_proven=false",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify UR10e report evidence claims stay inside explicit claim-boundary tiers.",
    )
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    findings: list[dict[str, Any]] = []
    for report in args.reports:
        findings.extend(evaluate(report))
    ok_all = all(finding["ok"] for finding in findings)

    if args.json:
        print(json.dumps({"ok": ok_all, "findings": findings}, indent=2, sort_keys=True))
    else:
        for finding in findings:
            label = "PASS" if finding["ok"] else "FAIL"
            print(f"{label} {finding['report']} {finding['check']}: {finding['detail']}")
        print("OK" if ok_all else "FAILED")
    return 0 if ok_all else 2


def evaluate(report_path: Path) -> list[dict[str, Any]]:
    try:
        text = report_path.read_text(encoding="utf-8")
    except OSError as exc:
        return [_finding(report_path, "readable", False, f"{type(exc).__name__}: {exc}")]

    findings: list[dict[str, Any]] = []
    sections = parse_sections(text)
    gate = first_section(sections, "claim boundary gate")
    if gate is None or not gate.strip():
        return [_finding(report_path, "claim_boundary_section", False, "required section missing or empty")]

    gate_norm = normalize(gate)
    full_norm = normalize(text)

    findings.append(_finding(report_path, "claim_boundary_section", True, "present"))
    for tier in REQUIRED_TIERS:
        check = f"required_tier:{tier}"
        ok = tier_line_present(gate, tier)
        findings.append(_finding(report_path, check, ok, "present" if ok else "missing from Claim Boundary Gate"))

    missing_sim_fields = [field for field in SIMULATED_FT_REQUIRED_FIELDS if field.lower() not in gate_norm]
    findings.append(
        _finding(
            report_path,
            "simulated_ft_evidence_fields",
            not missing_sim_fields,
            "present" if not missing_sim_fields else "missing: " + ", ".join(missing_sim_fields),
        )
    )

    missing_physical_fields = [field for field in PHYSICAL_GAZEBO_REQUIRED_FIELDS if field.lower() not in gate_norm]
    findings.append(
        _finding(
            report_path,
            "physical_gazebo_evidence_fields",
            not missing_physical_fields,
            "present" if not missing_physical_fields else "missing: " + ", ".join(missing_physical_fields),
        )
    )

    missing_blocker_rules = [token for token in PHYSICAL_BLOCKERS if token not in gate_norm]
    blocker_text_present = "blocked" in gate_norm and "not proven" in gate_norm
    findings.append(
        _finding(
            report_path,
            "physical_gazebo_blocker_rules",
            not missing_blocker_rules and blocker_text_present,
            "present"
            if not missing_blocker_rules and blocker_text_present
            else "missing blocker downgrade wording",
        )
    )

    real_bench_not_authorized = (
        "real bench/live contact" in gate_norm
        and "not authorized" in gate_norm
        and "upgrade" in gate_norm
    )
    findings.append(
        _finding(
            report_path,
            "real_bench_not_authorized_rule",
            real_bench_not_authorized,
            "present" if real_bench_not_authorized else "missing not-authorized/no-upgrade rule",
        )
    )

    findings.append(
        _finding(
            report_path,
            "virtual_force_loop_source_boundary",
            "gazebo_joint_state_fk_virtual_surface_model" not in full_norm
            or "virtual/software force-loop" in gate_norm,
            "present" if "gazebo_joint_state_fk_virtual_surface_model" in full_norm else "not referenced",
        )
    )

    physical_positive = has_positive_claim(text, "physical Gazebo collision/contact physics")
    physical_blocker_seen = any(token in full_norm for token in PHYSICAL_BLOCKERS)
    findings.append(
        _finding(
            report_path,
            "physical_contact_claim_boundary",
            not (physical_blocker_seen and physical_positive),
            "blocked/not proven"
            if not (physical_blocker_seen and physical_positive)
            else "positive physical Gazebo contact claim appears with blocker evidence",
        )
    )

    real_positive = has_positive_claim(text, "real bench/live contact")
    findings.append(
        _finding(
            report_path,
            "real_bench_claim_boundary",
            not real_positive,
            "not authorized"
            if not real_positive
            else "positive real bench/live contact claim appears without authorization",
        )
    )

    return findings


def parse_sections(text: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    current_head: str | None = None
    current_body: list[str] = []
    for line in text.splitlines():
        match = HEADING_RE.match(line)
        if match:
            if current_head is not None:
                sections.append((current_head, "\n".join(current_body).strip()))
            current_head = match.group("text")
            current_body = []
        elif current_head is not None:
            current_body.append(line)
    if current_head is not None:
        sections.append((current_head, "\n".join(current_body).strip()))
    return sections


def first_section(sections: list[tuple[str, str]], wanted: str) -> str | None:
    wanted_norm = normalize(wanted)
    for heading, body in sections:
        if normalize(heading) == wanted_norm:
            return body
    return None


def tier_line_present(section: str, tier: str) -> bool:
    tier_norm = normalize(tier)
    for raw_line in section.splitlines():
        line = normalize(raw_line).lstrip("-*| ").strip()
        if line.startswith(tier_norm + ":") or line.startswith(tier_norm + " |"):
            return True
        if raw_line.strip().startswith("|") and re.search(rf"\|\s*{re.escape(tier_norm)}\s*\|", normalize(raw_line)):
            return True
    return False


def has_positive_claim(text: str, tier: str) -> bool:
    tier_norm = tier.lower()
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        sentence_norm = normalize(sentence)
        if tier_norm not in sentence_norm:
            continue
        sanitized = sentence_norm
        for allowed_negative in (
            "blocked/not proven",
            "not proven",
            "not accepted",
            "not authorized",
            "no claim may upgrade",
            "blocked",
        ):
            sanitized = sanitized.replace(allowed_negative, "")
        if re.search(r"\b(accepted|complete|success|verified|validated|pass(?:ed)?|proven)\b", sanitized):
            return True
    return False


def normalize(text: str) -> str:
    normalized = text.lower().replace("—", "-").replace("–", "-")
    return re.sub(r"\s+", " ", normalized).strip()


def _finding(report_path: Path, check: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"ok": ok, "report": str(report_path), "check": check, "detail": detail}


if __name__ == "__main__":
    raise SystemExit(main())
