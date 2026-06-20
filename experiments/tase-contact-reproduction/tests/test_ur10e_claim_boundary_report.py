#!/usr/bin/env python3
from __future__ import annotations

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
- simulated_ft: simulated wrench/FT topics or synthetic force logs only when
  stamp, frame_id, source, status, baseline, and log evidence are present.
- physical Gazebo collision/contact physics: blocked/not proven unless EOAT
  collision evidence, contact pair/log evidence, and wrench/contact correlation
  all exist. If `eoat_collision_count=0` or
  `force_contact_physics_proven=false`, this tier is blocked/not proven.
- real Kunwei read-only: retained logs or explicitly read-only Kunwei evidence
  only; no live contact, no device zero/tare/config writes.
- real bench/live contact: not authorized in this goal; no claim may upgrade
  simulated_ft or Gazebo evidence into real bench/live contact.
"""


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
                "`force_contact_physics_proven=false` remains blocked/not proven. "
                "`eoat_collision_count=0` remains blocked/not proven. "
                "The simulated_ft artifact includes stamp, frame_id, source, "
                "status, baseline, and log evidence."
            ),
        )
        returncode, payload = self.run_verifier(text)
        self.assertEqual(returncode, 0, payload)
        self.assertTrue(payload["ok"])

    def test_missing_claim_boundary_section_fails_closed(self) -> None:
        text = _base_report("", extra_body="No claim boundary section.")
        self.assertFailsWith(text, "claim_boundary_section")

    def test_missing_required_tier_fails_closed(self) -> None:
        gate = GOOD_CLAIM_GATE.replace("- simulated_ft:", "- simulated sensor:")
        text = _base_report(gate)
        self.assertFailsWith(text, "required_tier:simulated_ft")

    def test_missing_real_kunwei_read_only_tier_fails_closed(self) -> None:
        gate = GOOD_CLAIM_GATE.replace("- real Kunwei read-only:", "- retained sensor replay:")
        text = _base_report(gate)
        self.assertFailsWith(text, "required_tier:real Kunwei read-only")

    def test_simulated_ft_claim_without_required_fields_fails_closed(self) -> None:
        gate = GOOD_CLAIM_GATE.replace(
            "stamp, frame_id, source, status, baseline, and log evidence",
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


if __name__ == "__main__":
    unittest.main()
