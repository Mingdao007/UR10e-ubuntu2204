#!/usr/bin/env python3
"""Tests for the Step5c paper-truth PDF audit."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "build_step5c_paper_truth_pdf_audit.py"


def import_audit_module():
    spec = importlib.util.spec_from_file_location("build_step5c_paper_truth_pdf_audit", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


FIXTURE_TEXT = """
finite-time convergent recurrent neural network
(23a)
(23b)
where the parameter epsilon > 0, r ∈ [0, 1] are tunable parameter
where ε = κ > 0 and % = 1
r ∈ [0, 1]
0 < r < 1
r = 0.2
Md (xddot_pd - xddot_p)
e p = xpd - x p, e f = Fd − F
ẍ p = e f + k f integral - Bd ẋ p /Md
ẋ p (t) = ΦO (...) + Φ̄O (...)
cross-product matrix
Rd = I + sin(u)S
equa = Q−1
ẋo = x˙od + ko eo
ẋod = 0
min θ̇ θ̇/2
L = θ̇ θ̇/2 + λT
Karush-Kuhn-Tucker
PΩ
parameters in the controller are set
Md = Diag(12, ..., 12), Bd = Diag(550, ..., 550)
0.022, k p = 4, ko = 5 and k f = 1
0.15 rad/s
"""


class Step5cPaperTruthPdfAuditTest(unittest.TestCase):
    def test_fixture_proves_core_pdf_fields_but_keeps_local_blockers(self) -> None:
        audit = import_audit_module()
        payload = audit.build_audit(generated_at="2026-06-21T09:05:00+08:00", paper_text=FIXTURE_TEXT)

        self.assertTrue(payload["audit_ok"])
        self.assertEqual(payload["claim_tier"], "virtual/software force-loop")
        self.assertIn("finite_time_rnn_state_equation", payload["verified_fields"])
        self.assertIn("rnn_gain_scalar_not_matrix", payload["verified_fields"])
        self.assertIn("paper_section_vi_experimental_parameters", payload["verified_fields"])
        self.assertEqual(payload["unverified_expected_fields"], [])
        self.assertIn("alpha_escape_velocity_gain", payload["remaining_pending_fields"])
        self.assertIn(
            "step5c_strict_dryrun.mapping from paper task variable to UR10e 6dof qdot",
            payload["remaining_pending_fields"],
        )
        self.assertFalse(payload["strict_rnn_enabled"])
        self.assertIn("simulated_ft", payload["forbidden_claim"])

    def test_current_config_splits_verified_fields_from_pending_fields(self) -> None:
        truth = json.loads((ROOT / "config" / "step5c_tase_paper_truth.json").read_text(encoding="utf-8"))

        verified = truth["pdf_text_audit"]["verified_fields"]
        pending = truth["pending_pdf_verify"]
        self.assertIn("finite_time_rnn_state_equation", verified)
        self.assertIn("constraint_qp_or_kkt_form", verified)
        self.assertNotIn("finite_time_rnn_state_equation", pending)
        self.assertIn("alpha_escape_velocity_gain", pending)
        self.assertFalse(truth["strict_rnn_enabled"])

    def test_write_audit_creates_machine_readable_artifact(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="step5c_pdf_truth_audit_test_") as tmp:
            path = audit.write_audit(Path(tmp), generated_at="2026-06-21T09:05:00+08:00")
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(path.name, "step5c_paper_truth_pdf_audit.json")
        self.assertEqual(payload["artifact_path"], str(path))
        self.assertEqual(payload["schema"], "ur10e_step5c_paper_truth_pdf_audit_v1")
        self.assertTrue(payload["audit_ok"])


if __name__ == "__main__":
    unittest.main()
