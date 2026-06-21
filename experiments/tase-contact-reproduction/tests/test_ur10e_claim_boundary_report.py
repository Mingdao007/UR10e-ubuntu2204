#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "verify_ur10e_claim_boundary_report.py"


def _base_report(claim_gate: str, extra_body: str = "") -> str:
    return f"""# UR10e Claim Boundary Test Report

```yaml
handoff_direction: implementor_to_auditor
status: ready_for_audit
```

{claim_gate}

## Evidence summary

{extra_body}

## Next action — user

Wait for Auditor review.

## Open decisions for user

None for this checkpoint.
"""


GOOD_CLAIM_GATE = """## Claim Boundary Gate

This report is fail-closed. Every checkpoint/report/final claim must label
evidence using these tiers:

- visual_only: Gazebo/RViz screenshots, EOAT visibility, TCP marker, model pose,
  and observer-view evidence only.
- virtual/software force-loop: `gazebo_joint_state_fk_virtual_surface_model`
  and other software-only force-loop artifacts only.
- simulated_ft: simulated wrench/FT topics, Gazebo FT plugin output, or
  synthetic force logs only when stamp, frame_id, source, status, baseline, and
  log evidence are present.
- physical Gazebo collision/contact physics: blocked/not proven unless EOAT
  collision evidence, contact pair/log evidence, contact normal/surface
  relation, total contact wrench, and wrench/contact correlation all exist.
  If `eoat_collision_count=0` or
  `force_contact_physics_proven=false`, this tier is blocked/not proven.
- real bench/live contact: not authorized in this goal; no claim may upgrade
  simulated_ft or Gazebo evidence into real bench/live contact.
"""


GOOD_CLAIM_TIER_TABLE = """## Current Claim Tier Table

| Evidence surface | Current status | Claim tier |
|---|---|---|
| P2 EOAT inventory artifact | EOAT collision bodies present; contact pair/log evidence and wrench/contact correlation missing | visual_only |
| P1 canonical simulated FT artifact | stamp, frame_id, source, status, baseline, and log evidence present | simulated_ft |
| Physical Gazebo contact | `force_contact_physics_proven=false`; target tier blocked/not proven | visual_only |
"""


def _write_json_artifact(path: Path, payload: dict[str, object]) -> str:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _standalone_total_contact_wrench_artifact() -> dict[str, object]:
    return {
        "schema": "ur10e_gazebo_contact_wrench_adapter_report_v1",
        "claim_tier": "physical Gazebo collision/contact physics",
        "observation_scope": "standalone_p2_contact_witness",
        "force_source": "gazebo_contact",
        "trace_written": True,
        "total_contact_wrench_proven": True,
        "wrench_aggregation_policy": "total_contact_wrench",
        "total_contact_wrench_blockers": [],
    }


def _per_stage_contact_artifact() -> dict[str, object]:
    return {
        "schema": "ur10e_per_stage_dual_sensor_contact_audit_v1",
        "claim_tier": "physical Gazebo collision/contact physics",
        "stage_wrench_contact_correlation": {
            "evidence": True,
            "status": "correlated",
        },
        "per_stage_physical_gazebo_contact": {
            "per_stage_physical_gazebo_contact_proven": True,
        },
        "blockers": [],
        "validation_issues": [],
    }


class Ur10eClaimBoundaryReportVerifierTest(unittest.TestCase):
    def run_verifier(self, text: str) -> tuple[int, dict[str, object]]:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "report.md"
            report.write_text(text, encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--json", str(report)],
                check=False,
                text=True,
                capture_output=True,
            )
        payload = json.loads(result.stdout) if result.stdout else {"findings": []}
        return result.returncode, payload

    def assertFailsWith(self, text: str, check: str) -> None:
        returncode, payload = self.run_verifier(text)
        self.assertNotEqual(returncode, 0, payload)
        failed = [finding["check"] for finding in payload["findings"] if not finding["ok"]]
        self.assertIn(check, failed)

    def test_compliant_report_passes(self) -> None:
        text = _base_report(
            GOOD_CLAIM_GATE,
            extra_body=(
                GOOD_CLAIM_TIER_TABLE
                + "\n"
                "`force_contact_physics_proven=false` remains blocked/not proven. "
                "The simulated_ft artifact includes stamp, frame_id, source, "
                "status, baseline, and log evidence."
            ),
        )
        returncode, payload = self.run_verifier(text)
        self.assertEqual(returncode, 0, payload)
        self.assertTrue(payload["ok"])

    def test_missing_current_claim_tier_table_fails_closed(self) -> None:
        text = _base_report(
            GOOD_CLAIM_GATE,
            extra_body=(
                "`force_contact_physics_proven=false` remains blocked/not proven. "
                "`eoat_collision_count=0` remains blocked/not proven."
            ),
        )
        self.assertFailsWith(text, "current_claim_tier_table")

    def test_claim_tier_table_without_evidence_surface_column_fails_closed(self) -> None:
        table = """## Current Claim Tier Table

| Claim | Status | Claim tier |
|---|---|---|
| P2 | blocked | visual_only |
"""
        text = _base_report(GOOD_CLAIM_GATE, extra_body=table)
        self.assertFailsWith(text, "current_claim_tier_table")

    def test_missing_claim_boundary_section_fails_closed(self) -> None:
        text = _base_report("", extra_body="No claim boundary section.")
        self.assertFailsWith(text, "claim_boundary_section")

    def test_missing_required_tier_fails_closed(self) -> None:
        gate = GOOD_CLAIM_GATE.replace("- simulated_ft:", "- simulated sensor:")
        text = _base_report(gate)
        self.assertFailsWith(text, "required_tier:simulated_ft")

    def test_simulated_ft_claim_without_required_fields_fails_closed(self) -> None:
        gate = GOOD_CLAIM_GATE.replace(
            "stamp, frame_id, source, status, baseline, and\n"
            "  log evidence",
            "complete synthetic samples",
        )
        text = _base_report(gate, extra_body="simulated_ft topic evidence is accepted.")
        self.assertFailsWith(text, "simulated_ft_evidence_fields")

    def test_physical_gazebo_claim_is_blocked_when_contact_physics_not_proven(self) -> None:
        text = _base_report(
            GOOD_CLAIM_GATE,
            extra_body=(
                "`force_contact_physics_proven=false`; nevertheless physical "
                "Gazebo collision/contact physics is accepted as complete."
            ),
        )
        self.assertFailsWith(text, "physical_contact_claim_boundary")

    def test_physical_gazebo_claim_is_blocked_when_eoat_collision_count_zero(self) -> None:
        text = _base_report(
            GOOD_CLAIM_GATE,
            extra_body=(
                "`eoat_collision_count=0`; physical Gazebo collision/contact "
                "physics is proven."
            ),
        )
        self.assertFailsWith(text, "physical_contact_claim_boundary")

    def test_positive_physical_gazebo_prose_without_evidence_row_fails_closed(self) -> None:
        weak_gate = GOOD_CLAIM_GATE.replace(
            "blocked/not proven unless EOAT",
            "requires EOAT",
        ).replace(
            "  all exist. If `eoat_collision_count=0` or\n"
            "  `force_contact_physics_proven=false`, this tier is blocked/not proven.",
            "  all exist.",
        )
        table = """## Current Claim Tier Table

| Evidence surface | Current status | Claim tier |
|---|---|---|
| Gazebo observer matrix | screenshots and EOAT visibility only | visual_only |
| P1 canonical simulated FT artifact | stamp, frame_id, source, status, baseline, and log evidence present | simulated_ft |
"""
        text = _base_report(
            weak_gate,
            extra_body=table + "\nPhysical Gazebo collision/contact physics is verified.",
        )
        self.assertFailsWith(text, "physical_contact_claim_boundary")

    def test_real_bench_claim_without_explicit_authorization_fails_closed(self) -> None:
        text = _base_report(
            GOOD_CLAIM_GATE,
            extra_body=(
                "The report upgrades simulated_ft evidence into real bench/live "
                "contact accepted evidence."
            ),
        )
        self.assertFailsWith(text, "real_bench_claim_boundary")

    def test_visual_evidence_requires_visual_only_tier(self) -> None:
        gate = GOOD_CLAIM_GATE.replace("- visual_only:", "- visual observer:")
        text = _base_report(gate, extra_body="Gazebo/RViz screenshot and TCP marker evidence.")
        self.assertFailsWith(text, "required_tier:visual_only")

    def test_visual_only_gate_must_name_required_visual_sources(self) -> None:
        gate = GOOD_CLAIM_GATE.replace(
            "Gazebo/RViz screenshots, EOAT visibility, TCP marker, model pose,\n"
            "  and observer-view evidence only.",
            "observer artifacts only.",
        )
        text = _base_report(gate, extra_body=GOOD_CLAIM_TIER_TABLE)
        self.assertFailsWith(text, "visual_only_source_boundaries")

    def test_simulated_ft_gate_must_include_gazebo_ft_plugin_output(self) -> None:
        gate = GOOD_CLAIM_GATE.replace(", Gazebo FT plugin output,", "")
        text = _base_report(gate, extra_body=GOOD_CLAIM_TIER_TABLE)
        self.assertFailsWith(text, "simulated_ft_source_boundaries")

    def test_visual_only_sources_cannot_be_upgraded_in_claim_table(self) -> None:
        table = """## Current Claim Tier Table

| Evidence surface | Current status | Claim tier |
|---|---|---|
| Gazebo/RViz screenshot and TCP marker | EOAT visibility only | simulated_ft |
"""
        text = _base_report(GOOD_CLAIM_GATE, extra_body=table)
        self.assertFailsWith(text, "claim_tier_table_source_boundaries")

    def test_virtual_force_loop_source_cannot_be_upgraded_in_claim_table(self) -> None:
        table = """## Current Claim Tier Table

| Evidence surface | Current status | Claim tier |
|---|---|---|
| `gazebo_joint_state_fk_virtual_surface_model` | software-only force source | physical Gazebo collision/contact physics |
"""
        text = _base_report(GOOD_CLAIM_GATE, extra_body=table)
        self.assertFailsWith(text, "claim_tier_table_source_boundaries")

    def test_simulated_ft_row_without_required_metadata_fails_closed(self) -> None:
        table = """## Current Claim Tier Table

| Evidence surface | Current status | Claim tier |
|---|---|---|
| Simulated wrench topic | source and log evidence present, status unknown | simulated_ft |
"""
        text = _base_report(GOOD_CLAIM_GATE, extra_body=table)
        self.assertFailsWith(text, "claim_tier_table_source_boundaries")

    def test_physical_gazebo_contact_row_requires_blocked_label_when_blocked(self) -> None:
        table = """## Current Claim Tier Table

| Evidence surface | Current status | Claim tier |
|---|---|---|
| Physical Gazebo contact | `eoat_collision_count=0`; `force_contact_physics_proven=false` | physical Gazebo collision/contact physics |
"""
        text = _base_report(GOOD_CLAIM_GATE, extra_body=table)
        self.assertFailsWith(text, "claim_tier_table_source_boundaries")

    def test_generic_blocked_physical_gazebo_row_must_downgrade_to_visual_only(self) -> None:
        table = """## Current Claim Tier Table

| Evidence surface | Current status | Claim tier |
|---|---|---|
| Physical Gazebo contact | blocked/not proven pending wrench/contact correlation | physical Gazebo collision/contact physics |
"""
        text = _base_report(GOOD_CLAIM_GATE, extra_body=table)
        self.assertFailsWith(text, "claim_tier_table_source_boundaries")

    def test_standalone_physical_gazebo_row_requires_total_wrench_artifact_and_hash(self) -> None:
        table = """## Current Claim Tier Table

| Evidence surface | Current status | Claim tier |
|---|---|---|
| Standalone P2 contact witness artifact `/tmp/p2.json` sha256=abc123 | standalone P2 witness has EOAT collision evidence, contact pair/log evidence, contact normal/surface relation, and wrench/contact correlation, but only a single contact-point wrench | physical Gazebo collision/contact physics |
"""
        text = _base_report(GOOD_CLAIM_GATE, extra_body=table)
        self.assertFailsWith(text, "claim_tier_table_source_boundaries")

    def test_physical_gazebo_row_requires_artifact_hash_freshness(self) -> None:
        table = """## Current Claim Tier Table

| Evidence surface | Current status | Claim tier |
|---|---|---|
| Standalone P2 contact witness artifact `/tmp/p2.json` | standalone P2 witness has EOAT collision evidence, contact pair/log evidence, contact normal/surface relation, total contact wrench, and wrench/contact correlation | physical Gazebo collision/contact physics |
"""
        text = _base_report(GOOD_CLAIM_GATE, extra_body=table)
        self.assertFailsWith(text, "claim_tier_table_source_boundaries")

    def test_per_stage_physical_gazebo_row_requires_per_stage_audit_artifact(self) -> None:
        table = """## Current Claim Tier Table

| Evidence surface | Current status | Claim tier |
|---|---|---|
| Step5b per-stage contact artifact `/tmp/step5b.json` sha256=abc123 | per-stage Step5b has EOAT collision evidence, contact pair/log evidence, contact normal/surface relation, total contact wrench, and wrench/contact correlation | physical Gazebo collision/contact physics |
"""
        text = _base_report(GOOD_CLAIM_GATE, extra_body=table)
        self.assertFailsWith(text, "claim_tier_table_source_boundaries")

    def test_source_backed_standalone_physical_gazebo_row_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "p2_total_contact_wrench.json"
            sha256 = _write_json_artifact(artifact, _standalone_total_contact_wrench_artifact())
            table = f"""## Current Claim Tier Table

| Evidence surface | Current status | Claim tier |
|---|---|---|
| Standalone P2 total-contact-wrench witness artifact `{artifact}` sha256={sha256} | standalone P2 witness only; EOAT collision evidence, contact pair/log evidence, contact normal/surface relation, total contact wrench, and wrench/contact correlation are present | physical Gazebo collision/contact physics |
"""
            text = _base_report(GOOD_CLAIM_GATE, extra_body=table)
            returncode, payload = self.run_verifier(text)
        self.assertEqual(returncode, 0, payload)
        self.assertTrue(payload["ok"])

    def test_source_backed_per_stage_physical_gazebo_row_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "step5b_per_stage_dual_sensor_contact_audit.json"
            sha256 = _write_json_artifact(artifact, _per_stage_contact_artifact())
            table = f"""## Current Claim Tier Table

| Evidence surface | Current status | Claim tier |
|---|---|---|
| Step5b per-stage dual-sensor contact audit artifact `{artifact}` sha256={sha256} | per-stage audit artifact; EOAT collision evidence, contact pair/log evidence, contact normal/surface relation, total contact wrench, and wrench/contact correlation are present | physical Gazebo collision/contact physics |
"""
            text = _base_report(GOOD_CLAIM_GATE, extra_body=table)
            returncode, payload = self.run_verifier(text)
        self.assertEqual(returncode, 0, payload)
        self.assertTrue(payload["ok"])

    def test_hybrid_blocked_claim_tier_cells_fail_closed(self) -> None:
        table = """## Current Claim Tier Table

| Evidence surface | Current status | Claim tier |
|---|---|---|
| Physical Gazebo contact | `force_contact_physics_proven=false`; blocked/not proven | physical Gazebo collision/contact physics blocked/not proven |
| Real bench/live contact | not authorized | real bench/live contact not authorized |
"""
        text = _base_report(GOOD_CLAIM_GATE, extra_body=table)
        self.assertFailsWith(text, "claim_tier_table_source_boundaries")

    def test_real_bench_authorization_status_is_not_visual_only_evidence_row(self) -> None:
        table = """## Current Claim Tier Table

| Evidence surface | Current status | Claim tier |
|---|---|---|
| Real bench/live contact | not authorized; no live bench evidence may be claimed | visual_only |
"""
        text = _base_report(GOOD_CLAIM_GATE, extra_body=table)
        self.assertFailsWith(text, "claim_tier_table_source_boundaries")


if __name__ == "__main__":
    unittest.main()
