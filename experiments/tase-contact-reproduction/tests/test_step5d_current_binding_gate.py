#!/usr/bin/env python3
"""Tests for the Step5d current binding gate."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import verify_step5d_current_binding as gate  # noqa: E402
import step5d_liveprep_readiness as liveprep  # noqa: E402


PROGRAM = "step5d_strict_rnn_liveprep_v99"
V29_PROGRAM = "step5d_strict_rnn_ablation_v29"
TARGET_DIR = "/programs/andyl/kunwei/step5"
V29_PROFILE = {
    "backend": "cupy",
    "inner_iterations": 1024,
    "epsilon": 0.01,
    "sigr_exponent_r": 0.8,
    "qdot_cap_rad_s": 0.05,
    "control_mode": "speedj_rnn_live",
    "joint_layout_code": 524.0,
}


def _v29_benchmark_contract() -> dict:
    return {
        "schema_version": "step5d_v29_liveprep_benchmark_v1",
        "program": V29_PROGRAM,
        "calibration_hash": "fixture_calibration_hash",
        "calibrated_source": {"run_csv": "fixture.csv", "write_index": 1},
        "runtime_profile": V29_PROFILE,
        "jacobian_base_tcp": np.eye(6).tolist(),
        "approach_normal_base": [1.0, 0.0, 0.0],
        "reaction_normal_base": [-1.0, 0.0, 0.0],
        "representative_xdot_c": [0.001, 0.0, 0.0, 0.0, 0.0, 0.0],
        "thresholds": {
            "solver_samples": 10000,
            "first_post_warm_max_ms": 1.75,
            "solver_p99_max_ms": 1.5,
            "deadline_ms": 2.0,
            "synthetic_duration_s": 60.0,
            "synthetic_frequency_hz": 500.0,
            "synthetic_p99_max_ms": 1.8,
            "safe_hold_samples": 10000,
            "safe_hold_p99_max_ms": 2.0,
        },
        "safety_boundary": [
            "offline compute only",
            "no bridge start",
            "no controller write",
            "no TP Play",
            "no zero_ftsensor",
            "no robot motion",
        ],
    }


def _valid_v29_timing() -> dict:
    return {
        "ok": True,
        "quick_mode": False,
        "microbenchmark": {
            "pass": True,
            "samples": 10000,
            "precompile_ms": 400.0,
            "precompile_outside_loop": True,
            "first_post_warm_ms": 1.2,
            "p50_ms": 1.1,
            "p99_ms": 1.4,
            "max_ms": 1.9,
            "accepted_count": 10000,
            "rejected_count": 0,
            "rejection_reasons": [],
            "deadline_miss_count": 0,
        },
        "synthetic_tick": {
            "pass": True,
            "samples": 30000,
            "duration_s": 60.001,
            "requested_duration_s": 60.0,
            "frequency_hz": 500.0,
            "deadline_paced": True,
            "accepted_count": 30000,
            "p50_ms": 1.2,
            "p99_ms": 1.7,
            "max_ms": 1.9,
            "compute_deadline_miss_count": 0,
            "schedule_overrun_count": 0,
            "max_schedule_lateness_ms": 0.0,
            "deadline_miss_count": 0,
        },
        "safe_hold": {
            "pass": True,
            "samples": 10000,
            "zero_qdot_count": 10000,
            "p99_ms": 0.1,
            "max_ms": 0.2,
            "deadline_miss_count": 0,
        },
    }


def _rebind_v29_readiness_sha(root: Path, readiness_path: Path) -> None:
    readiness_sha256 = _sha256(readiness_path.read_bytes())
    current_path = root / "config" / "current_stage.json"
    table_path = root / "config" / "step5_stage_table.json"
    current = json.loads(current_path.read_text(encoding="utf-8"))
    table = json.loads(table_path.read_text(encoding="utf-8"))
    current["liveprep_status"]["readiness_sha256"] = readiness_sha256
    table["stages"][0]["liveprep_status"]["readiness_sha256"] = readiness_sha256
    current_path.write_text(json.dumps(current), encoding="utf-8")
    table_path.write_text(json.dumps(table), encoding="utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_binding_fixture(
    root: Path,
    *,
    stage_active: bool = True,
    stage_blocked: bool = False,
    live_motion_authorized: bool = True,
    strict_rnn_p0_required: bool = False,
    strict_rnn_p0_passed: bool = False,
) -> None:
    local_stem = root / "programs" / "step5" / PROGRAM
    local_stem.parent.mkdir(parents=True)
    contents = {
        ".script": b"def codex_step5d_strict_rnn_liveprep_v99():\nend\n",
        ".txt": b"step5d fixture\n",
        ".urp": b"fixture urp bytes\n",
    }
    sha = {}
    for ext, data in contents.items():
        local_stem.with_suffix(ext).write_bytes(data)
        sha[ext] = _sha256(data)

    manifest_rel = f"runs/controller_readback_{PROGRAM}_fixture/manifest.json"
    manifest_path = root / manifest_rel
    manifest_path.parent.mkdir(parents=True)
    (root / "config").mkdir(parents=True)
    current = {
        "current_stage_id": PROGRAM,
        "program": PROGRAM,
        "stage_table_path": "config/step5_stage_table.json",
        "controller_target": f"{TARGET_DIR}/{PROGRAM}.urp",
        "controller_script": f"{TARGET_DIR}/{PROGRAM}.script",
        "local_triplet": f"programs/step5/{PROGRAM}",
        "status": f"{PROGRAM}_controller_readback_verified_pending_live_bridge_run",
        "delivery_manifest": manifest_rel,
        "sha256": sha,
        "bridge_trigger": {
            "bridge_has_started": False,
            "live_motion_authorized": live_motion_authorized,
            "zero_ftsensor_authorized": False,
            "required_before_live": [],
        },
    }
    manifest = {
        "status": "controller read-back verified",
        "target_dir": TARGET_DIR,
        "validation": {
            "program": PROGRAM,
            "target_dir": TARGET_DIR,
            "installation_relative_path": "../../../default",
            "script_node_path": f"{TARGET_DIR}/{PROGRAM}.script",
            "script_sha256": sha[".script"],
            "txt_sha256": sha[".txt"],
            "urp_sha256": sha[".urp"],
        },
        "sha256": {
            "local": sha,
            "controller": sha,
            "readback": sha,
        },
    }
    stage_table = {
        "stages": [
            {
                "id": PROGRAM,
                "active": stage_active,
                "blocked": stage_blocked,
                "acceptance": {
                    "strict_rnn_no_contact_p0_required_before_live": strict_rnn_p0_required,
                    "strict_rnn_no_contact_p0_passed": strict_rnn_p0_passed,
                },
            }
        ]
    }
    (root / "config" / "current_stage.json").write_text(json.dumps(current), encoding="utf-8")
    (root / "config" / "step5_stage_table.json").write_text(json.dumps(stage_table), encoding="utf-8")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def _write_v29_authorization_fixture(
    root: Path,
    *,
    stage_blocked: bool = False,
    readiness_ready: bool = True,
    readiness_blockers: list[str] | None = None,
    reviewed_source_sha256: str = "a" * 64,
    liveprep_state: str | None = None,
    readiness_workflow_state: str | None = None,
) -> None:
    (root / "config").mkdir(parents=True)
    benchmark = _v29_benchmark_contract()
    benchmark_path = root / "config" / "step5d_v29_liveprep_benchmark.json"
    benchmark_path.write_text(json.dumps(benchmark), encoding="utf-8")
    readiness_rel = "runs/step5d_v29_liveprep_fixture/liveprep_readiness.json"
    readback_rel = "runs/controller_readback_v29_fixture/manifest.json"
    readiness_path = root / readiness_rel
    readback_path = root / readback_rel
    readiness_path.parent.mkdir(parents=True)
    readback_path.parent.mkdir(parents=True)
    readback_path.write_text('{"status":"controller read-back verified"}\n', encoding="utf-8")
    package_sha = {
        extension: character * 64
        for extension, character in ((".script", "1"), (".txt", "2"), (".urp", "3"))
    }
    state = liveprep_state or ("blocked" if stage_blocked or not readiness_ready else "awaiting_live_authorization")
    current = {
        "current_stage_id": V29_PROGRAM,
        "program": V29_PROGRAM,
        "sha256": package_sha,
        "controller_readback_manifest": readback_rel,
        "liveprep_status": {"state": state, "readiness_artifact": readiness_rel},
        "bridge_trigger": {
            "bridge_has_started": False,
            "live_motion_authorized": True,
        },
    }
    stage_table = {
        "stages": [
            {
                "id": V29_PROGRAM,
                "active": True,
                "blocked": stage_blocked,
                "runtime_profile": V29_PROFILE,
                "acceptance": {
                    "strict_rnn_no_contact_p0_required_before_live": False,
                    "strict_rnn_no_contact_p0_passed": False,
                },
                "liveprep_status": {"state": state, "readiness_artifact": readiness_rel},
            }
        ]
    }
    (root / "config" / "current_stage.json").write_text(json.dumps(current), encoding="utf-8")
    (root / "config" / "step5_stage_table.json").write_text(json.dumps(stage_table), encoding="utf-8")
    blockers = list(readiness_blockers or [])
    review_lanes = []
    for lane_id in ("control_claim", "timing_runtime", "physical_operator_safety"):
        lane_artifact = readiness_path.parent / f"{lane_id}.md"
        lane_runtime = readiness_path.parent / f"{lane_id}.json"
        lane_artifact.write_text(f"# {lane_id}\n\nACCEPTED\n", encoding="utf-8")
        lane_artifact_sha = _sha256(lane_artifact.read_bytes())
        lane_runtime.write_text(
            json.dumps(
                {
                    "schema_version": "step5d_reviewer_runtime_evidence_v1",
                    "model": "gpt-5.6-sol",
                    "reasoning_effort": "max",
                    "sandbox": "read-only",
                    "exit_code": 0,
                    "artifact": lane_artifact.name,
                    "artifact_sha256": lane_artifact_sha,
                }
            ),
            encoding="utf-8",
        )
        review_lanes.append(
            {
                "id": lane_id,
                "result": "accepted",
                "artifact": lane_artifact.name,
                "artifact_sha256": lane_artifact_sha,
                "model": "gpt-5.6-sol",
                "reasoning_effort": "max",
                "runtime_evidence": lane_runtime.name,
                "runtime_evidence_sha256": _sha256(lane_runtime.read_bytes()),
            }
        )
    qdot_rnn = np.array([0.001, 0.0, 0.0, 0.0, 0.0, 0.0])
    dls_shadow = liveprep.build_dls_shadow(
        jacobian=np.asarray(benchmark["jacobian_base_tcp"], dtype=float),
        xdot_c=np.asarray(benchmark["representative_xdot_c"], dtype=float),
        qdot_rnn=qdot_rnn,
        omega_minus=np.full(6, -V29_PROFILE["qdot_cap_rad_s"]),
        omega_plus=np.full(6, V29_PROFILE["qdot_cap_rad_s"]),
        approach_normal=np.asarray(benchmark["approach_normal_base"], dtype=float),
    )
    readiness = {
        "schema_version": "step5d_liveprep_readiness_v1",
        "program": V29_PROGRAM,
        "profile": V29_PROGRAM,
        "workflow_state": readiness_workflow_state
        or ("awaiting_live_authorization" if readiness_ready else "liveprep_blocked"),
        "ready_for_explicit_live_authorization": readiness_ready,
        "blockers": blockers,
        "benchmark_contract_sha256": _sha256(benchmark_path.read_bytes()),
        "workflow_binding_sha256": liveprep.workflow_binding_sha256(root),
        "calibration_hash": benchmark["calibration_hash"],
        "calibrated_source": benchmark["calibrated_source"],
        "runtime_profile": V29_PROFILE,
        "runtime_profile_match": True,
        "timing": _valid_v29_timing(),
        "dls_shadow": dls_shadow,
        "review": {
            "schema_version": "step5d_liveprep_milestone_review_v1",
            "ok": True,
            "reviewed_source_sha256": reviewed_source_sha256,
            "expected_source_sha256": reviewed_source_sha256,
            "lanes": review_lanes,
        },
        "package_sha256": package_sha,
        "controller_readback_verified": True,
        "controller_readback_manifest": readback_rel,
        "controller_readback_manifest_sha256": _sha256(readback_path.read_bytes()),
        "package_hashes_match": True,
        "live_motion_authorized": False,
        "bridge_has_started": False,
        "claims": {
            "package_accepted": True,
            "live_run_accepted": False,
            "reproduction_complete": False,
        },
        "safety_boundary": benchmark["safety_boundary"],
    }
    readiness_path.write_text(json.dumps(readiness), encoding="utf-8")
    readiness_sha256 = _sha256(readiness_path.read_bytes())
    current["liveprep_status"]["readiness_sha256"] = readiness_sha256
    stage_table["stages"][0]["liveprep_status"]["readiness_sha256"] = readiness_sha256
    (root / "config" / "current_stage.json").write_text(json.dumps(current), encoding="utf-8")
    (root / "config" / "step5_stage_table.json").write_text(json.dumps(stage_table), encoding="utf-8")


def _verify_v29_authorization(root: Path, **overrides):  # noqa: ANN003
    profile = {
        "stage25_control_mode": "speedj_rnn_live",
        "rnn_backend": "cupy",
        "rnn_inner_iterations": 1024,
        "epsilon": 0.01,
        "sigr_exponent_r": 0.8,
        "qdot_cap_rad_s": 0.05,
    }
    profile.update(overrides)
    with mock.patch.object(gate, "verify_binding", return_value={"ok": True, "program": V29_PROGRAM}):
        return gate.verify_live_bridge_authorization(root, V29_PROGRAM, **profile)


class Step5dCurrentBindingGateTest(unittest.TestCase):
    def test_current_stage_readback_and_runtime_binding_pass(self) -> None:
        result = gate.verify_binding(ROOT)

        self.assertTrue(result["ok"])
        self.assertTrue(
            result["program"].startswith(("step5d_strict_rnn_liveprep_", "step5d_strict_rnn_ablation_"))
        )
        self.assertIn("controller_readback_step5d_strict_rnn_", result["manifest"])
        self.assertEqual(result["runtime_interface"]["program"], result["program"])
        self.assertEqual(len(result["local_triplet"]), 3)
        self.assertEqual(result["stage_table"]["id"], result["program"])
        self.assertTrue(result["stage_table"]["active"])
        self.assertTrue(result["stage_table"]["blocked"])

    def test_local_triplet_sha_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_binding_fixture(root)
            result = gate.verify_binding(root, PROGRAM)
            self.assertTrue(result["ok"])

            (root / "programs" / "step5" / f"{PROGRAM}.script").write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "local triplet sha256"):
                gate.verify_binding(root, PROGRAM)

    def test_inactive_stage_table_row_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_binding_fixture(root, stage_active=False)
            with self.assertRaisesRegex(RuntimeError, "stage table row .* is not active"):
                gate.verify_binding(root, PROGRAM)

    def test_blocked_stage_table_row_remains_valid_package_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_binding_fixture(root, stage_blocked=True)

            result = gate.verify_binding(root, PROGRAM)

        self.assertTrue(result["ok"])
        self.assertTrue(result["stage_table"]["blocked"])

    def test_current_blocked_v29_artifact_cannot_authorize_live_bridge(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_v29_authorization_fixture(
                root,
                stage_blocked=True,
                readiness_ready=False,
                readiness_blockers=["synthetic_tick_timing_failed"],
            )

            with self.assertRaisesRegex(RuntimeError, "blocked"):
                _verify_v29_authorization(root)

    def test_v29_authorization_requires_ready_bound_readiness_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_v29_authorization_fixture(
                root,
                readiness_ready=False,
                readiness_blockers=["milestone_review_not_accepted"],
                liveprep_state="awaiting_live_authorization",
                readiness_workflow_state="awaiting_live_authorization",
            )

            with self.assertRaisesRegex(RuntimeError, "ready_for_explicit_live_authorization"):
                _verify_v29_authorization(root)

    def test_v29_authorization_rejects_stale_review_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_v29_authorization_fixture(root, reviewed_source_sha256="a" * 64)

            with mock.patch.object(gate, "reviewed_source_sha256", return_value="b" * 64):
                with self.assertRaisesRegex(RuntimeError, "reviewed source"):
                    _verify_v29_authorization(root)

    def test_v29_authorization_requires_current_and_stage_bound_readiness_sha(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_v29_authorization_fixture(root)
            current_path = root / "config" / "current_stage.json"
            current = json.loads(current_path.read_text(encoding="utf-8"))
            current["liveprep_status"]["readiness_sha256"] = "f" * 64
            current_path.write_text(json.dumps(current), encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "readiness artifact hash"):
                _verify_v29_authorization(root)

    def test_v29_authorization_rejects_nonempty_blockers_and_stale_hash_bindings(self) -> None:
        mutations = {
            "blockers": lambda root, payload: payload.update({"blockers": ["timing_failed"]}),
            "package": lambda root, payload: payload.update({"package_sha256": {".script": "9" * 64}}),
            "readback": lambda root, payload: (root / "runs/controller_readback_v29_fixture/manifest.json").write_text(
                '{"status":"tampered"}\n', encoding="utf-8"
            ),
            "review": lambda root, payload: payload["review"].update({"ok": False}),
        }
        patterns = {
            "blockers": "blockers must be empty",
            "package": "package hash binding",
            "readback": "manifest hash is stale",
            "review": "milestone review is invalid",
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                _write_v29_authorization_fixture(root)
                readiness_path = root / "runs/step5d_v29_liveprep_fixture/liveprep_readiness.json"
                readiness = json.loads(readiness_path.read_text(encoding="utf-8"))
                mutate(root, readiness)
                readiness_path.write_text(json.dumps(readiness), encoding="utf-8")
                _rebind_v29_readiness_sha(root, readiness_path)
                with mock.patch.object(gate, "reviewed_source_sha256", return_value="a" * 64):
                    with self.assertRaisesRegex(RuntimeError, patterns[name]):
                        _verify_v29_authorization(root)

    def test_v29_authorization_recomputes_recorded_timing_dls_and_claim_boundaries(self) -> None:
        mutations = {
            "missing_timing": lambda payload: payload.pop("timing"),
            "quick_mode": lambda payload: payload["timing"].update({"quick_mode": True}),
            "micro_first": lambda payload: payload["timing"]["microbenchmark"].update(
                {"first_post_warm_ms": 1.80}
            ),
            "synthetic_schedule": lambda payload: payload["timing"]["synthetic_tick"].update(
                {"schedule_overrun_count": 1, "deadline_miss_count": 1}
            ),
            "safe_hold_nonzero": lambda payload: payload["timing"]["safe_hold"].update(
                {"zero_qdot_count": 9999}
            ),
            "dls_recompute": lambda payload: payload["dls_shadow"]["twist_rnn"].__setitem__(0, 0.02),
            "runtime_fallback": lambda payload: payload["dls_shadow"].update(
                {"runtime_fallback_allowed": True}
            ),
            "live_claim": lambda payload: payload["claims"].update({"live_run_accepted": True}),
            "artifact_live": lambda payload: payload.update({"live_motion_authorized": True}),
            "benchmark_hash": lambda payload: payload.update({"benchmark_contract_sha256": "f" * 64}),
            "workflow_hash": lambda payload: payload.update({"workflow_binding_sha256": "f" * 64}),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                _write_v29_authorization_fixture(root)
                readiness_path = root / "runs/step5d_v29_liveprep_fixture/liveprep_readiness.json"
                readiness = json.loads(readiness_path.read_text(encoding="utf-8"))
                mutate(readiness)
                readiness_path.write_text(json.dumps(readiness), encoding="utf-8")
                _rebind_v29_readiness_sha(root, readiness_path)
                with mock.patch.object(gate, "reviewed_source_sha256", return_value="a" * 64):
                    with self.assertRaisesRegex(RuntimeError, "recorded offline evidence"):
                        _verify_v29_authorization(root)

    def test_v29_authorization_rejects_runtime_profile_overrides(self) -> None:
        overrides = {
            "stage25_control_mode": "speedj_dls_oracle",
            "rnn_backend": "numpy",
            "rnn_inner_iterations": 512,
            "epsilon": 0.02,
            "sigr_exponent_r": 1.0,
            "qdot_cap_rad_s": 0.15,
        }
        for key, value in overrides.items():
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                _write_v29_authorization_fixture(root)
                with mock.patch.object(gate, "reviewed_source_sha256", return_value="a" * 64):
                    with self.assertRaisesRegex(RuntimeError, "exact runtime profile"):
                        _verify_v29_authorization(root, **{key: value})

    def test_v29_authorization_accepts_exact_profile_and_bound_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_v29_authorization_fixture(root)

            with mock.patch.object(gate, "reviewed_source_sha256", return_value="a" * 64):
                result = _verify_v29_authorization(root)

        self.assertTrue(result["ok"])
        self.assertEqual(result["readiness"]["workflow_state"], "awaiting_live_authorization")
        self.assertEqual(result["runtime_profile"], V29_PROFILE)

    def test_live_bridge_authorization_requires_current_stage_live_motion_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_binding_fixture(root, live_motion_authorized=False)

            with self.assertRaisesRegex(RuntimeError, "live motion is not authorized"):
                gate.verify_live_bridge_authorization(root, PROGRAM, "speedl_cartesian_oracle")

    def test_speedj_rnn_live_authorization_requires_no_contact_p0_passed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_binding_fixture(
                root,
                live_motion_authorized=True,
                strict_rnn_p0_required=True,
                strict_rnn_p0_passed=False,
            )

            with self.assertRaisesRegex(RuntimeError, "no-contact P0"):
                gate.verify_live_bridge_authorization(root, PROGRAM, "speedj_rnn_live")

    def test_speedj_rnn_live_authorization_passes_after_no_contact_p0(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_binding_fixture(
                root,
                live_motion_authorized=True,
                strict_rnn_p0_required=True,
                strict_rnn_p0_passed=True,
            )

            result = gate.verify_live_bridge_authorization(root, PROGRAM, "speedj_rnn_live")

        self.assertTrue(result["ok"])
        self.assertEqual(result["stage25_control_mode"], "speedj_rnn_live")
        self.assertTrue(result["live_motion_authorized"])
        self.assertTrue(result["strict_rnn_no_contact_p0_passed"])

    def test_speedl_live_authorization_does_not_require_strict_rnn_p0(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_binding_fixture(
                root,
                live_motion_authorized=True,
                strict_rnn_p0_required=True,
                strict_rnn_p0_passed=False,
            )

            result = gate.verify_live_bridge_authorization(root, PROGRAM, "speedl_cartesian_oracle")

        self.assertTrue(result["ok"])
        self.assertEqual(result["stage25_control_mode"], "speedl_cartesian_oracle")


if __name__ == "__main__":
    unittest.main()
