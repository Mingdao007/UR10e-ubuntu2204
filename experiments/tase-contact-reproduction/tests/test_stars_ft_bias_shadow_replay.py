#!/usr/bin/env python3
"""Hermetic contract tests for the offline STARS FT bias shadow."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from stars_ft_bias_shadow.adapters.r008_run_dir import (  # noqa: E402
    R008RunDirError,
    iter_trace_samples,
    preflight_run_dir,
)
from stars_ft_bias_shadow.contact_gate import (  # noqa: E402
    BIAS_ESTIMATE_FIELDS,
    BIAS_RATE_ESTIMATE_FIELDS,
    GateMode,
    decide_gate,
)
from stars_ft_bias_shadow.replay import (  # noqa: E402
    HARDENED_POLICY_VERSION,
    load_config,
    replay_run_dir,
    validate_completion_receipt,
)


CONFIG_PATH = ROOT / "config" / "ft_bias" / "stars_shadow_overnight_v0.json"
CLI_PATH = ROOT / "tools" / "stars_ft_bias_shadow" / "cli.py"
TRACE_SCHEMAS = {
    "state20": "step5d.autotune-v4/r008-state20-search-trace-v1",
    "state25": "step5d.autotune-v4/r008-state25-path-trace-v1",
}
_OMIT = object()


def _config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _old(path: Path, age_s: float = 120.0) -> None:
    timestamp = time.time() - age_s
    os.utime(path, (timestamp, timestamp))


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, separators=(",", ":")) + "\n", encoding="utf-8")


def _row(
    source: str,
    t_s: float,
    sequence: int,
    *,
    tp_state: int = 5,
    sensor_fresh: object = True,
    force_norm_n: object = 0.1,
    normal_load_n: object = 0.1,
    torque_norm_nm: object = 0.1,
    wrench: object = (0.2, 0.0, 0.0, 0.0, 0.0, 0.0),
    pose: object = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
    schema: str | None = None,
    explicit_source: object = _OMIT,
) -> dict:
    row = {
        "schema": TRACE_SCHEMAS[source] if schema is None else schema,
        "monotonic_s": t_s,
        "wall_time_s": 1_000_000.0 + t_s,
        "packet_sequence": sequence,
        "tp_state": tp_state,
        "sensor_fresh": sensor_fresh,
        "force_norm_n": force_norm_n,
        "normal_load_n": normal_load_n,
        "torque_norm_nm": torque_norm_nm,
        "tcp_pose_m_rad": list(pose) if isinstance(pose, tuple) else pose,
        "wrench": list(wrench) if isinstance(wrench, tuple) else wrench,
    }
    if explicit_source is not _OMIT:
        row["source"] = explicit_source
    return row


def _make_run(
    root: Path,
    *,
    state20_rows: list[dict] | None = None,
    state25_rows: list[dict] | None = None,
    terminal_status: str = "incomplete_stopped",
    formal_live_complete: bool = False,
) -> Path:
    r008_root = root / "runs" / "step5d_autotune_v4_r008"
    r008_root.mkdir(parents=True, exist_ok=True)
    run_dir = r008_root / "live_fixture"
    run_dir.mkdir()
    _write_json(
        run_dir / "software_baseline.json",
        {
            "schema": "step5d.autotune-v4/r005-software-baseline-v1",
            "zero_tare_config_write": False,
            "mean_wrench_n_nm": [1.0, 2.0, 3.0, 0.1, 0.2, 0.3],
            "stdev_wrench_n_nm": [0.01] * 6,
            "sample_count": 1000,
            "parse_errors": 0,
            "dropped_bytes": 0,
        },
    )
    _write_json(
        run_dir / "launch_context.json",
        {
            "run_id": run_dir.name,
            "identity": "fixture-launch-identity",
            "formal_live_complete": formal_live_complete,
        },
    )
    (run_dir / "host.log").write_text(
        "WARNING fixture host log contains deterministic pre-terminal text\n"
        + json.dumps({"status": "running"})
        + "\n"
        + json.dumps(
            {"status": terminal_status, "formal_live_complete": formal_live_complete}
        )
        + "\n",
        encoding="utf-8",
    )
    if state20_rows is None:
        state20_rows = [_row("state20", 0.0, 0), _row("state20", 0.1, 1), _row("state20", 0.2, 2)]
    with (run_dir / "r008-state20-search-trace.jsonl").open("w", encoding="utf-8") as handle:
        for row in state20_rows:
            handle.write(json.dumps(row, separators=(",", ":")) + "\n")
    if state25_rows is not None:
        with (run_dir / "r008-state25-path-trace.jsonl").open("w", encoding="utf-8") as handle:
            for row in state25_rows:
                handle.write(json.dumps(row, separators=(",", ":")) + "\n")
    for path in run_dir.iterdir():
        _old(path)
    return run_dir


def _replay(run_dir: Path, root: Path, *, config: dict | None = None, name: str = "out"):
    kwargs = {"config": config} if config is not None else {"config_path": CONFIG_PATH}
    return replay_run_dir(run_dir, experiment_root=root, out_dir=root / name, **kwargs)


def _assert_code(test: unittest.TestCase, code: str, callable_obj, *args, **kwargs) -> None:
    if callable_obj is replay_run_dir and "config" not in kwargs and "config_path" not in kwargs:
        kwargs["config_path"] = CONFIG_PATH
    with test.assertRaises(R008RunDirError) as context:
        callable_obj(*args, **kwargs)
    test.assertEqual(context.exception.code, code)


class ContactGateFailClosedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.gates = _config()["gates"]
        self.base = {
            "tp_state": 5,
            "sensor_fresh": True,
            "force_norm_n": 0.1,
            "normal_load_n": 0.1,
            "torque_norm_nm": 0.1,
            "tcp_pose_m_rad": [0.0] * 6,
            "wrench": [0.2, 0.0, 0.0, 0.0, 0.0, 0.0],
        }

    def gate(self, *, tcp_speed_m_s: float = 0.0, tcp_omega_rad_s: float = 0.0, dt_s: float = 0.1, **changes):
        sample = dict(self.base)
        sample.update(changes)
        return decide_gate(
            sample,
            gates=self.gates,
            tcp_speed_m_s=tcp_speed_m_s,
            tcp_omega_rad_s=tcp_omega_rad_s,
            dt_s=dt_s,
        )

    def test_only_complete_free_static_evidence_updates(self) -> None:
        decision = self.gate()
        self.assertEqual(decision.mode, GateMode.UPDATE)
        self.assertTrue(decision.update_allowed)
        self.assertTrue(decision.input_valid)
        self.assertEqual(decision.reason_code, "free_static")

    def test_missing_or_invalid_sensor_fresh_never_updates(self) -> None:
        for value in (None, False, 1, "true"):
            decision = self.gate(sensor_fresh=value)
            self.assertNotEqual(decision.mode, GateMode.UPDATE)
            self.assertFalse(decision.update_allowed)

    def test_missing_or_invalid_loads_never_update(self) -> None:
        for field in ("force_norm_n", "normal_load_n", "torque_norm_nm"):
            invalid_values = (None, "bad", float("nan"))
            if field != "normal_load_n":
                invalid_values = (*invalid_values, -1.0)
            for value in invalid_values:
                decision = self.gate(**{field: value})
                self.assertNotEqual(decision.mode, GateMode.UPDATE)
                self.assertIn(field, decision.invalid_fields)

    def test_signed_normal_load_uses_magnitude(self) -> None:
        self.assertEqual(self.gate(normal_load_n=-0.5).mode, GateMode.UPDATE)
        large = self.gate(normal_load_n=-self.gates["normal_load_threshold_n"])
        self.assertEqual(large.mode, GateMode.FREEZE)
        self.assertEqual(large.reason_code, "normal_load_threshold")

    def test_missing_unknown_and_invalid_state_never_update(self) -> None:
        for value in (None, 99, 5.0, "5"):
            decision = self.gate(tp_state=value)
            self.assertNotEqual(decision.mode, GateMode.UPDATE)
            self.assertFalse(decision.update_allowed)

    def test_missing_invalid_wrench_pose_speed_and_dt_never_update(self) -> None:
        cases = (
            {"wrench": None},
            {"wrench": [0.0] * 5},
            {"wrench": [float("nan")] + [0.0] * 5},
            {"remove": "tcp_pose_m_rad"},
            {"tcp_pose_m_rad": [0.0] * 5},
            {"tcp_speed_m_s": None},
            {"tcp_speed_m_s": float("nan")},
            {"dt_s": None},
            {"dt_s": 0.0},
            {"dt_s": -0.1},
        )
        for changes in cases:
            sample = dict(self.base)
            speed = changes.pop("tcp_speed_m_s", 0.0)
            omega = changes.pop("tcp_omega_rad_s", 0.0)
            dt = changes.pop("dt_s", 0.1)
            remove = changes.pop("remove", None)
            if remove is not None:
                sample.pop(remove, None)
            sample.update(changes)
            decision = decide_gate(sample, gates=self.gates, tcp_speed_m_s=speed, tcp_omega_rad_s=omega, dt_s=dt)
            self.assertNotEqual(decision.mode, GateMode.UPDATE, changes)

    def test_state25_contact_and_search_motion_freeze(self) -> None:
        self.assertEqual(self.gate(tp_state=25).mode, GateMode.FREEZE)
        self.assertEqual(self.gate(contact=True).mode, GateMode.FREEZE)
        self.assertEqual(
            self.gate(force_norm_n=self.gates["force_norm_threshold_n"], wrench=None).mode,
            GateMode.FREEZE,
        )
        search = dict(self.base, tp_state=20)
        decision = decide_gate(
            search,
            gates=self.gates,
            tcp_speed_m_s=self.gates["tcp_linear_max_m_s"],
            tcp_omega_rad_s=0.0,
            dt_s=0.1,
        )
        self.assertEqual(decision.mode, GateMode.FREEZE)
        self.assertEqual(decision.reason_code, "search_motion")

    def test_static_state20_updates_but_motion_is_freeze(self) -> None:
        search = self.gate(tp_state=20)
        self.assertEqual(search.mode, GateMode.UPDATE)
        moving = self.gate(tp_state=20, tcp_speed_m_s=self.gates["tcp_linear_max_m_s"])
        self.assertEqual(moving.mode, GateMode.FREEZE)
        self.assertEqual(moving.reason_code, "search_motion")
        state5_motion = self.gate(tcp_speed_m_s=self.gates["tcp_linear_max_m_s"])
        self.assertEqual(state5_motion.mode, GateMode.PREDICT_ONLY)

    def test_threshold_equality_is_not_below(self) -> None:
        for field, reason in (
            ("normal_load_n", "normal_load_threshold"),
            ("force_norm_n", "force_norm_threshold"),
            ("torque_norm_nm", "torque_norm_threshold"),
        ):
            threshold_field = {
                "normal_load_n": "normal_load_threshold_n",
                "force_norm_n": "force_norm_threshold_n",
                "torque_norm_nm": "torque_norm_threshold_nm",
            }[field]
            decision = self.gate(**{field: self.gates[threshold_field]})
            self.assertEqual(decision.mode, GateMode.FREEZE)
            self.assertEqual(decision.reason_code, reason)
        motion = decide_gate(
            self.base,
            gates=self.gates,
            tcp_speed_m_s=self.gates["tcp_linear_max_m_s"],
            tcp_omega_rad_s=0.0,
            dt_s=0.1,
        )
        self.assertEqual(motion.mode, GateMode.PREDICT_ONLY)


class EstimatorContractTest(unittest.TestCase):
    def test_output_fields_and_credited_clock(self) -> None:
        gates = _config()["gates"]
        sample = _row("state20", 0.0, 0)
        decision = decide_gate(sample, gates=gates, tcp_speed_m_s=None, tcp_omega_rad_s=None, dt_s=0.0)
        estimator = __import__("stars_ft_bias_shadow.estimator", fromlist=["GatedEmaBiasEstimator"]).GatedEmaBiasEstimator()
        row = estimator.step(t_s=73.566, residual=[0.1] * 6, decision=decision, dt_s=0.0)
        self.assertEqual(row["gate"], "PREDICT_ONLY")
        self.assertEqual(estimator.update_time_s, 0.0)
        for name in BIAS_ESTIMATE_FIELDS + BIAS_RATE_ESTIMATE_FIELDS:
            self.assertIn(name, row)

    def test_contradictory_update_decision_is_neutralized(self) -> None:
        from stars_ft_bias_shadow.contact_gate import GateDecision
        from stars_ft_bias_shadow.estimator import GatedEmaBiasEstimator

        estimator = GatedEmaBiasEstimator()
        decision = GateDecision(GateMode.UPDATE, False, "forged", True, input_valid=False)
        row = estimator.step(t_s=0.0, residual=[1.0] * 6, decision=decision, dt_s=1.0)
        self.assertEqual(row["gate"], "PREDICT_ONLY")
        self.assertEqual(estimator.update_count, 0)


class StrictTraceAdapterTest(unittest.TestCase):
    def test_strict_json_duplicate_nonfinite_and_malformed_fail_preflight(self) -> None:
        cases = (
            (b"{\"source\":\"state20\",\"source\":\"state20\"}\n", "invalid_json"),
            (b"NaN\n", "invalid_json"),
            (b"[]\n", "trace_row_not_object"),
            (b"not-json\n", "invalid_json"),
            (b"\n", "blank_trace_line"),
        )
        for index, (payload, code) in enumerate(cases):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                run = _make_run(root)
                trace = run / "r008-state20-search-trace.jsonl"
                trace.write_bytes(payload)
                _old(trace)
                _assert_code(self, code, replay_run_dir, run, experiment_root=root, out_dir=root / f"out-{index}")

    def test_schema_source_binding_and_per_file_monotonic_time_sequence_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = _make_run(root)
            trace = run / "r008-state20-search-trace.jsonl"
            rows = [_row("state25", 0.0, 0), _row("state25", 0.1, 1)]
            trace.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            _old(trace)
            _assert_code(self, "trace_schema_mismatch", replay_run_dir, run, experiment_root=root, out_dir=root / "a")

            rows = [
                _row("state20", 0.0, 0, explicit_source="state25"),
                _row("state20", 0.1, 1, explicit_source="state25"),
            ]
            trace.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            _old(trace)
            _assert_code(self, "source_binding_mismatch", replay_run_dir, run, experiment_root=root, out_dir=root / "source")

            rows = [_row("state20", 0.1, 1), _row("state20", 0.1, 2)]
            trace.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            _old(trace)
            _assert_code(self, "trace_time_not_monotonic", replay_run_dir, run, experiment_root=root, out_dir=root / "b")

            rows = [_row("state20", 0.1, 2), _row("state20", 0.2, 2)]
            trace.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            _old(trace)
            _assert_code(self, "trace_sequence_not_monotonic", replay_run_dir, run, experiment_root=root, out_dir=root / "c")

    def test_chronological_merge_is_streamed_and_source_bound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = _make_run(
                root,
                state20_rows=[
                    _row("state20", 0.0, 0, pose=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)),
                    _row("state20", 0.2, 1, pose=(0.02, 0.0, 0.0, 0.0, 0.0, 0.0)),
                ],
                state25_rows=[_row("state25", 0.1, 0, tp_state=25), _row("state25", 0.3, 1, tp_state=25)],
            )
            samples = list(iter_trace_samples(run, max_contiguous_dt_s=1.0))
            self.assertEqual([(sample.source, sample.t_s) for sample in samples], [
                ("state20", 0.0),
                ("state25", 0.1),
                ("state20", 0.2),
                ("state25", 0.3),
            ])
            self.assertTrue(samples[0].source_first)
            self.assertTrue(samples[1].source_first)
            self.assertAlmostEqual(samples[2].source_dt_raw_s, 0.2, places=9)
            self.assertAlmostEqual(samples[2].tcp_speed_m_s, 0.1, places=9)
            self.assertAlmostEqual(samples[2].dt_raw_s, 0.1, places=9)

    def test_73_566_second_gap_has_zero_credit_and_is_counted_separately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = _make_run(
                root,
                state20_rows=[_row("state20", 0.0, 0), _row("state20", 0.1, 1), _row("state20", 73.666, 2)],
            )
            result = _replay(run, root)
            rows = [json.loads(line) for line in result.bias_est_path.read_text().splitlines()]
            self.assertEqual(rows[-1]["dt_raw_s"], 73.566)
            self.assertEqual(rows[-1]["dt_credited_s"], 0.0)
            self.assertTrue(rows[-1]["gap"])
            self.assertEqual(result.summary["gap_seconds"], 73.566)
            self.assertEqual(result.summary["credited_observed_time_s"], 0.1)
            self.assertLessEqual(result.summary["update_coverage"], 1.0)
            self.assertAlmostEqual(result.summary["wall_span_s"], 73.666, places=9)


class HistoricalSnapshotAndSealingTest(unittest.TestCase):
    def test_missing_state25_is_explicit_partial_source_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = _make_run(root, state25_rows=None)
            result = _replay(run, root)
            coverage = result.summary["source_coverage"]
            self.assertTrue(coverage["partial_source_coverage"])
            self.assertTrue(coverage["missing_state25_allowed"])
            manifest = json.loads(result.input_manifest_path.read_text())
            self.assertFalse(next(item for item in manifest["files"] if item["path"].endswith("state25-path-trace.jsonl"))["present"])

    def test_terminal_status_and_formal_flag_are_reported_without_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = _make_run(root, terminal_status="completed", formal_live_complete=True)
            result = _replay(run, root)
            self.assertEqual(result.summary["terminal_status"], "completed")
            self.assertTrue(result.summary["formal_live_complete"])
            self.assertEqual(result.summary["host_non_json_line_count"], 1)
            self.assertEqual(result.summary["terminal_evidence"]["terminal_line_policy"], "final_nonblank_line_strict_json_object")
            manifest = json.loads(result.input_manifest_path.read_text())
            self.assertEqual(manifest["host_non_json_line_count"], 1)
            self.assertEqual(manifest["terminal_evidence"]["terminal_status"], "completed")
            self.assertTrue(result.summary["science_not_promoted"])
            self.assertEqual(result.summary["promotion_status"], "offline_historical_shadow_only")

    def test_host_log_trailing_non_json_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = _make_run(root)
            host = run / "host.log"
            host.write_text(
                "WARNING deterministic historical warning\n"
                + json.dumps({"status": "incomplete_stopped", "formal_live_complete": False})
                + "\nCORRUPTED TRAILING TEXT\n",
                encoding="utf-8",
            )
            _old(host)
            _assert_code(
                self,
                "host_log_trailing_corruption",
                replay_run_dir,
                run,
                experiment_root=root,
                out_dir=root / "trailing",
            )

    def test_incomplete_status_implies_false_and_completed_requires_explicit_formal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = _make_run(root)
            launch_context = run / "launch_context.json"
            context = json.loads(launch_context.read_text(encoding="utf-8"))
            context.pop("formal_live_complete")
            _write_json(launch_context, context)
            _old(launch_context)
            host = run / "host.log"
            host.write_text(
                "WARNING deterministic historical warning\n"
                + json.dumps(
                    {
                        "b3_two_stage": True,
                        "reason": "r004 resident program is not running",
                        "status": "incomplete_stopped",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            _old(host)

            implied = _replay(run, root, name="implied-false")
            self.assertFalse(implied.summary["formal_live_complete"])
            evidence = implied.summary["terminal_evidence"]
            self.assertEqual(evidence["formal_live_complete_field"], "terminal_status_implies_false")
            self.assertEqual(evidence["formal_live_complete_provenance"], "terminal_status_implies_false")
            self.assertIsNone(evidence["formal_live_complete_explicit_value"])
            manifest = json.loads(implied.input_manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["terminal_evidence"], evidence)
            self.assertEqual(validate_completion_receipt(implied.out_dir)["formal_live_complete"], False)

            host.write_text(
                "WARNING deterministic historical warning\n"
                + json.dumps({"status": "incomplete_stopped", "formal_live_complete": True})
                + "\n",
                encoding="utf-8",
            )
            _old(host)
            overridden = _replay(run, root, name="explicit-true-overridden")
            override_evidence = overridden.summary["terminal_evidence"]
            self.assertFalse(overridden.summary["formal_live_complete"])
            self.assertTrue(override_evidence["formal_live_complete_explicit_value"])
            self.assertEqual(override_evidence["formal_live_complete_field"], "host.log.formal_live_complete")
            self.assertEqual(
                override_evidence["formal_live_complete_provenance"],
                "explicit_host_json_status_override_terminal_status_implies_false",
            )

            host.write_text(
                "WARNING deterministic historical warning\n"
                + json.dumps({"status": "incomplete_stopped", "formal_live_complete": "false"})
                + "\n",
                encoding="utf-8",
            )
            _old(host)
            _assert_code(
                self,
                "invalid_formal_live_complete",
                replay_run_dir,
                run,
                experiment_root=root,
                out_dir=root / "nonboolean-formal",
            )

            host.write_text(
                "WARNING deterministic historical warning\n"
                + json.dumps({"status": "completed"})
                + "\n",
                encoding="utf-8",
            )
            _old(host)
            _assert_code(
                self,
                "invalid_formal_live_complete",
                replay_run_dir,
                run,
                experiment_root=root,
                out_dir=root / "completed-without-formal",
            )

            context["formal_live_complete"] = False
            _write_json(launch_context, context)
            _old(launch_context)
            completed_from_launch = _replay(run, root, name="completed-from-launch-formal")
            self.assertFalse(completed_from_launch.summary["formal_live_complete"])
            self.assertEqual(
                completed_from_launch.summary["terminal_evidence"]["formal_live_complete_field"],
                "launch_context.formal_live_complete",
            )

    def test_age_size_and_symlink_gates_are_strict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = _make_run(root)
            fresh = run / "host.log"
            os.utime(fresh, None)
            _assert_code(
                self,
                "input_too_new",
                replay_run_dir,
                run,
                experiment_root=root,
                out_dir=root / "fresh",
            )

            run = _make_run(root / "size", state25_rows=None)
            cfg = _config()
            cfg["limits"]["max_input_file_bytes"] = 10
            cfg["limits"]["max_total_input_bytes"] = 1000
            _assert_code(self, "input_file_too_large", replay_run_dir, run, experiment_root=root / "size", config=cfg, out_dir=root / "size-out")

            huge_root = root / "huge-stat-only"
            huge_run = _make_run(huge_root, state25_rows=None)
            huge_trace = huge_run / "r008-state20-search-trace.jsonl"
            with huge_trace.open("ab") as handle:
                handle.truncate(64 * 1024 * 1024 + 1)
            _old(huge_trace)
            original_open = Path.open

            def reject_huge_content(path: Path, *args, **kwargs):
                if path.name == "r008-state20-search-trace.jsonl":
                    raise AssertionError("oversized state20 content was opened")
                return original_open(path, *args, **kwargs)

            with mock.patch.object(Path, "open", new=reject_huge_content):
                _assert_code(
                    self,
                    "input_file_too_large",
                    replay_run_dir,
                    huge_run,
                    experiment_root=huge_root,
                    out_dir=huge_root / "huge-out",
                )

            run = _make_run(root / "link", state25_rows=None)
            target = run / "r008-state20-search-trace.jsonl"
            target.unlink()
            os.symlink(run / "software_baseline.json", target)
            _assert_code(self, "symlink_input", replay_run_dir, run, experiment_root=root / "link", out_dir=root / "link-out")

    def test_config_is_exact_hardened_and_tare_invariant(self) -> None:
        cfg = load_config(CONFIG_PATH, ROOT)
        self.assertEqual(cfg["policy_version"], HARDENED_POLICY_VERSION)
        self.assertEqual(cfg["gates"]["update_tp_states"], [5, 20])
        self.assertEqual(cfg["limits"]["max_input_file_bytes"], 64 * 1024 * 1024)
        self.assertEqual(cfg["limits"]["max_total_input_bytes"], 64 * 1024 * 1024)
        self.assertEqual(cfg["limits"]["max_output_file_bytes"], 256 * 1024 * 1024)
        for mutate in (
            lambda value: value.update({"unexpected": 1}),
            lambda value: value["gates"].update({"normal_load_threshold_n": 0.0}),
            lambda value: value["gates"].update({"normal_load_threshold_n": "0.75"}),
            lambda value: value.update({"policy_version": "old"}),
            lambda value: value.update({"sensor_zero_tare_or_config_allowed": True}),
            lambda value: value["limits"].update({"max_input_file_bytes": 64 * 1024 * 1024 + 1}),
            lambda value: value["limits"].update({"max_total_input_bytes": 64 * 1024 * 1024 + 1}),
            lambda value: value["limits"].update({"max_output_file_bytes": 256 * 1024 * 1024 + 1}),
        ):
            candidate = copy.deepcopy(cfg)
            mutate(candidate)
            with self.assertRaises(R008RunDirError):
                from stars_ft_bias_shadow.replay import validate_config

                validate_config(candidate)

    def test_config_and_tool_identity_drift_are_visible_or_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = _make_run(root)
            first = _replay(run, root, name="one")
            changed = _config()
            changed["gates"]["force_norm_threshold_n"] = 2.1
            second = _replay(run, root, config=changed, name="two")
            self.assertNotEqual(first.summary["config_identity_sha256"], second.summary["config_identity_sha256"])
            self.assertNotEqual(first.summary["execution_identity_sha256"], second.summary["execution_identity_sha256"])

            from stars_ft_bias_shadow import replay as replay_module

            with mock.patch.object(replay_module, "_tool_closure", side_effect=[("a", []), ("b", [])]):
                _assert_code(self, "tool_mutated_after_replay", replay_run_dir, run, experiment_root=root, out_dir=root / "tool-drift")

    def test_input_mutation_after_replay_prevents_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = _make_run(root)
            from stars_ft_bias_shadow import replay as replay_module

            original = replay_module._write_bias_rows

            def write_then_mutate(*args, **kwargs):
                result = original(*args, **kwargs)
                trace = run / "r008-state20-search-trace.jsonl"
                trace.write_text(trace.read_text() + json.dumps(_row("state20", 0.3, 3)) + "\n", encoding="utf-8")
                _old(trace)
                return result

            with mock.patch.object(replay_module, "_write_bias_rows", side_effect=write_then_mutate):
                _assert_code(self, "input_mutated_after_replay", replay_run_dir, run, experiment_root=root, out_dir=root / "mutated")
            self.assertFalse((root / "mutated" / "completion_receipt.json").exists())

    def test_overwrite_protection_and_cold_receipt_tamper_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = _make_run(root)
            result = _replay(run, root, name="sealed")
            original_bytes = result.bias_est_path.read_bytes()
            _assert_code(self, "output_exists", replay_run_dir, run, experiment_root=root, out_dir=result.out_dir)
            self.assertEqual(result.bias_est_path.read_bytes(), original_bytes)
            self.assertEqual(validate_completion_receipt(result.out_dir)["rows_written"], result.rows_written)

            result.bias_est_path.write_bytes(original_bytes + b"\n")
            _assert_code(self, "receipt_tampered", validate_completion_receipt, result.out_dir)

    def test_default_output_is_full_execution_identity_and_identity_has_no_absolute_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_copy = root / "config" / "ft_bias" / CONFIG_PATH.name
            config_copy.parent.mkdir(parents=True)
            config_copy.write_bytes(CONFIG_PATH.read_bytes())
            run = _make_run(root)
            result = replay_run_dir(run, experiment_root=root)
            self.assertEqual(result.out_dir.parent.name, run.name)
            self.assertEqual(result.out_dir.name, result.summary["execution_identity_sha256"])
            self.assertEqual(result.out_dir.parent.parent.name, "_shadow_out")
            payload = json.dumps(result.summary["execution_identity_payload"])
            self.assertNotIn(str(root), payload)

    def test_output_schema_contains_identity_gate_time_and_science_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = _make_run(root)
            result = _replay(run, root)
            row = json.loads(result.bias_est_path.read_text().splitlines()[0])
            for field in (
                "source", "line", "packet_sequence", "monotonic_s", "wall_time_s",
                "gate", "reason", "dt_raw_s", "dt_credited_s", "source_dt_raw_s", "gap", "input_valid",
                "dropped_count", "malformed_count", "terminal_status", "config_identity_sha256",
                "tool_closure_sha256", "input_manifest_sha256", "execution_identity_sha256",
            ):
                self.assertIn(field, row)
            self.assertNotIn("residual_fx_n", {"residual_fx_n": None} if not row["residual_valid"] else {})
            self.assertTrue(result.summary["science_not_promoted"])
            self.assertFalse(result.summary["sensor_zero_tare_or_config_allowed"])
            self.assertNotIn("ledger", json.dumps(result.summary).lower())
            self.assertNotIn("bo", json.dumps(result.summary).lower())


class CliOfflineTest(unittest.TestCase):
    def test_cli_help_inventory_replay_and_cold_read_are_offline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = _make_run(root)
            env = dict(os.environ, PYTHONPATH=str(ROOT / "tools"))
            help_result = subprocess.run(
                [sys.executable, str(CLI_PATH), "--help"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(help_result.returncode, 0)
            inventory = subprocess.run(
                [
                    sys.executable,
                    str(CLI_PATH),
                    "inventory",
                    "--run-dir",
                    str(run),
                    "--experiment-root",
                    str(root),
                    "--config",
                    str(CONFIG_PATH),
                ],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(inventory.returncode, 0, inventory.stderr)
            inventory_doc = json.loads(inventory.stdout)
            self.assertTrue(inventory_doc["science_not_promoted"])
            self.assertEqual(inventory_doc["host_non_json_line_count"], 1)
            self.assertEqual(
                inventory_doc["terminal_evidence"]["terminal_line_policy"],
                "final_nonblank_line_strict_json_object",
            )
            replay = subprocess.run(
                [
                    sys.executable,
                    str(CLI_PATH),
                    "replay",
                    "--run-dir",
                    str(run),
                    "--experiment-root",
                    str(root),
                    "--config",
                    str(CONFIG_PATH),
                    "--out-dir",
                    str(root / "cli-out"),
                ],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(replay.returncode, 0, replay.stderr)
            receipt = subprocess.run(
                [sys.executable, str(CLI_PATH), "validate-receipt", "--out-dir", str(root / "cli-out")],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(receipt.returncode, 0, receipt.stderr)


if __name__ == "__main__":
    unittest.main()
