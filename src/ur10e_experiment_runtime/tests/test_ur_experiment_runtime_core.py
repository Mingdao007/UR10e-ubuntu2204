from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

from ur10e_experiment_runtime.cli import main  # noqa: E402
from ur10e_experiment_runtime.contracts import (  # noqa: E402
    AutotuneCandidate,
    OutputPathError,
    RegistryError,
    ResourceLockError,
    SpecValidationError,
    UnsupportedExecutionError,
)
from ur10e_experiment_runtime.identity import (  # noqa: E402
    StrictJSONError,
    canonical_json_bytes,
    canonical_sha256,
    load_strict_json,
    strict_json_loads,
)
from ur10e_experiment_runtime.registry import (  # noqa: E402
    ComponentKind,
    build_default_registry,
)
from ur10e_experiment_runtime.runtime import (  # noqa: E402
    HostResourceLock,
    append_run_state_event,
    create_exclusive_run_directory,
    plan_experiment,
    reconstruct_run_state,
    run_experiment,
    start_campaign,
    status,
    validate_experiment_spec,
    validate_run_manifest,
    verify_run_state_chain,
)


class _TestStageAdapter:
    component_id = "test_stage_adapter_v1"
    version = "1"

    def validate_spec(self, document):
        if document["stage"]["stage_key"] != "step5d":
            raise SpecValidationError("test adapter accepts only step5d")

    def plan(self, spec, lane_id):
        return {
            "adapter_id": self.component_id,
            "stage_id": spec.stage["stage_id"],
            "lane": lane_id,
        }


def _registry():
    registry = build_default_registry(include_stage_adapters=False)
    adapter = _TestStageAdapter()
    registry.register(
        ComponentKind.STAGE_ADAPTER,
        adapter.component_id,
        adapter.version,
        adapter,
    )
    return registry


def _spec_document():
    return {
        "schema_version": "ur10e.experiment_spec/v1",
        "experiment_id": "step5d_strict_rnn_autotune_v1",
        "stage": {
            "stage_key": "step5d",
            "stage_id": "step5d_strict_rnn_autotune_v1",
            "program_id": "step5d_rnn_autotune_v1",
            "source_stage_id": None,
        },
        "components": {
            "trajectory": {"id": "cycloid_v1", "version": "1"},
            "controller": {
                "id": "strict_rnn_speedj_autotune_v1",
                "version": "1",
            },
            "stage_adapter": {"id": "test_stage_adapter_v1", "version": "1"},
            "backend": {"id": "offline_scaffold_v1", "version": "1"},
            "observer": {"id": "outcome_classifier_v1", "version": "1"},
            "safety": {"id": "no_motion_v1", "version": "1"},
            "evidence": {"id": "jsonl_manifest_v1", "version": "1"},
        },
        "objective": {
            "target_force_n": 12.0,
            "window_s": [5.0, 60.0],
            "window_semantics": "half_open",
            "bins": 550,
            "parameter_semantics": "force_pi_damping_v1",
        },
        "trajectory_contract": {
            "duration_s": 60.0,
            "reference_frame": "task",
            "parameters_sha256": "d" * 64,
        },
        "frame_contract": {
            "base_frame": "base",
            "task_frame": "task",
            "sensor_frame": "kunwei_sensor",
            "tool_frame": "tool0",
            "contract_sha256": "e" * 64,
        },
        "surface_contract": {
            "surface_id": "step5_surface_v1",
            "calibration_sha256": None,
            "workspace_cage_sha256": None,
        },
        "safety_contract": {
            "policy_id": "step5d_offline_safety_v1",
            "policy_sha256": None,
            "fail_closed": True,
        },
        "bindings": {
            "git_sha": "a" * 40,
            "source_sha256": "b" * 64,
            "config_sha256": "c" * 64,
            "model_sha256": None,
            "tp_sha256": {"program": None, "script": None, "txt": None},
            "controller": {
                "id": "strict_rnn_speedj_autotune_v1",
                "sha256": None,
            },
        },
        "lanes": {
            "offline": {
                "backend_id": "offline_scaffold_v1",
                "backend_version": "1",
                "motion_ceiling": "none",
                "resources": ["cpu_throughput"],
            },
            "gazebo": {
                "backend_id": "gazebo_scaffold_v1",
                "backend_version": "1",
                "motion_ceiling": "simulation",
                "resources": ["cpu_throughput", "visible_gazebo"],
            },
            "hil": {
                "backend_id": "hil_scaffold_v1",
                "backend_version": "1",
                "motion_ceiling": "no_motion",
                "resources": ["cpu_throughput"],
            },
            "live_autotune": {
                "backend_id": "live_autotune_scaffold_v1",
                "backend_version": "1",
                "motion_ceiling": "live_motion",
                "resources": ["live_writer"],
            },
        },
        "resource_contract": {
            "id": "ur10e_concurrency_contract_v1",
            "source_sha256": "e3dc1886750ff500c17f4277fad0b90fea90e5ed6ca3f4bd1dbec1ad9f1e80d2",
            "exclusive_resources": [
                "live_writer",
                "visible_gazebo",
                "formal_timing",
            ],
            "serial_fallback_supported": True,
        },
        "readiness": {
            "calibration_required": True,
            "calibration_complete": False,
            "evidence_sha256": None,
        },
        "legacy_provenance": [],
        "warm_start": None,
    }


class ExperimentRuntimeCoreTest(unittest.TestCase):
    def test_strict_json_rejects_duplicate_keys_and_nonfinite_numbers(self):
        for payload in (
            '{"a":1,"a":2}',
            '{"value":NaN}',
            '{"value":Infinity}',
            '{"value":1e999}',
            '{"value":-1e999}',
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(StrictJSONError):
                    strict_json_loads(payload)

    def test_jcs_number_utf16_and_ijson_golden_fixtures(self):
        self.assertEqual(
            canonical_json_bytes(
                {
                    "numbers": [
                        333333333.33333329,
                        1e30,
                        4.50,
                        2e-3,
                        1e-27,
                        -0.0,
                        1e-6,
                        1e-7,
                        1e20,
                        1e21,
                    ]
                }
            ),
            b'{"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27,0,0.000001,1e-7,100000000000000000000,1e+21]}',
        )
        self.assertEqual(
            canonical_json_bytes({"\ue000": 2, "\U00010000": 1}),
            '{"\U00010000":1,"\ue000":2}'.encode("utf-8"),
        )
        self.assertNotEqual(canonical_sha256("é"), canonical_sha256("e\u0301"))
        for value in (2**53, -(2**53), "\ud800", {"\ud800": "bad"}):
            with self.subTest(value=repr(value)):
                with self.assertRaises(StrictJSONError):
                    canonical_json_bytes(value)

    def test_autotune_candidate_requires_positive_p_and_damping(self):
        self.assertEqual(
            AutotuneCandidate(0.1, 0.0, 0.2).to_dict(),
            {"p_gain": 0.1, "i_gain": 0.0, "damping": 0.2},
        )
        for candidate in ((0.0, 0.0, 0.2), (0.1, -0.1, 0.2), (0.1, 0.0, 0.0)):
            with self.subTest(candidate=candidate):
                with self.assertRaises(SpecValidationError):
                    AutotuneCandidate(*candidate)

    def test_load_strict_json_rejects_duplicate_keys_from_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "duplicate.json"
            path.write_text('{"stage":"a","stage":"b"}', encoding="utf-8")
            with self.assertRaises(StrictJSONError):
                load_strict_json(path)

    def test_schema_rejects_unknown_fields_and_bad_objective_window(self):
        unknown = _spec_document()
        unknown["hostname"] = "must-not-enter-identity"
        with self.assertRaises(SpecValidationError):
            validate_experiment_spec(unknown, registry=_registry())

        reversed_window = _spec_document()
        reversed_window["objective"]["window_s"] = [60.0, 5.0]
        with self.assertRaises(SpecValidationError):
            validate_experiment_spec(reversed_window, registry=_registry())

        stale_contract = _spec_document()
        stale_contract["resource_contract"]["source_sha256"] = "0" * 64
        with self.assertRaises(SpecValidationError):
            validate_experiment_spec(stale_contract, registry=_registry())

    def test_registry_is_allowlisted_and_uses_real_outcome_classifier(self):
        registry = _registry()
        unknown = _spec_document()
        unknown["components"]["trajectory"]["id"] = "dynamic.import.Payload"
        with self.assertRaises((RegistryError, SpecValidationError)):
            validate_experiment_spec(unknown, registry=registry)

        classifier = registry.resolve(ComponentKind.OBSERVER, "outcome_classifier_v1", "1")
        result = classifier.classify(
            {
                "outcome_class": "infrastructure_failure",
                "objective": 1.0,
                "metric_role": "diagnostic_only",
                "oracle_status": "failed",
                "observer_status": "incomplete",
                "fingerprint_verified": True,
                "exact_ack_consumed": False,
                "post_ack_closure_verified": False,
                "publication_unique": True,
            }
        )
        self.assertFalse(result["optimizer_eligible"])
        self.assertIsNone(result["objective"])

    def test_canonical_fingerprint_ignores_mapping_order_and_is_immutable(self):
        document = _spec_document()
        reordered = {key: document[key] for key in reversed(tuple(document))}
        first = validate_experiment_spec(document, registry=_registry())
        second = validate_experiment_spec(reordered, registry=_registry())
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(first.fingerprint, canonical_sha256(document))

        mutable_copy = first.document
        mutable_copy["experiment_id"] = "mutated"
        self.assertEqual(first.experiment_id, "step5d_strict_rnn_autotune_v1")

    def test_plan_is_declarative_and_hil_live_are_blocked(self):
        registry = _registry()
        spec = validate_experiment_spec(_spec_document(), registry=registry)
        offline = plan_experiment(spec, "offline", registry=registry)
        self.assertTrue(offline["executable"])
        self.assertEqual(offline["external_actions"], [])
        self.assertEqual(
            offline["adapter_plan"]["adapter_id"], "test_stage_adapter_v1"
        )

        hil = plan_experiment(spec, "hil", registry=registry)
        live = plan_experiment(spec, "live_autotune", registry=registry)
        self.assertFalse(hil["executable"])
        self.assertIn("hil_execution_not_implemented", hil["blocked_reasons"])
        self.assertFalse(live["executable"])
        self.assertIn("live_execution_not_implemented", live["blocked_reasons"])
        self.assertIn("calibration_incomplete", live["blocked_reasons"])

    def test_cli_plan_generates_change_contract_from_governance_inputs(self):
        registry = _registry()
        spec = validate_experiment_spec(_spec_document(), registry=registry)
        governance_root = (
            PACKAGE_ROOT.parents[1]
            / "experiments/tase-contact-reproduction/config/failure_to_guard"
        )
        stdout = StringIO()
        with patch(
            "ur10e_experiment_runtime.cli.load_experiment_spec", return_value=spec
        ), patch(
            "ur10e_experiment_runtime.cli.plan_experiment",
            return_value={"schema": "ur10e.experiment_plan/v1"},
        ), redirect_stdout(stdout):
            result = main(
                [
                    "plan",
                    "unused.json",
                    "--lane",
                    "offline",
                    "--touched-path",
                    "src/ur10e_experiment_runtime/ur10e_experiment_runtime/runtime.py",
                    "--governance-root",
                    str(governance_root),
                ]
            )
        self.assertEqual(result, 0)
        contract = json.loads(stdout.getvalue())["change_contract"]
        self.assertEqual(contract["parameter_impact"], "replay_required")
        self.assertIn("f2g.actual_overlay_identity", contract["invariant_ids"])
        self.assertIn("f2g.metric_role_optimizer_gate", contract["invariant_ids"])

    def test_offline_run_writes_append_only_state_and_write_once_manifest(self):
        registry = _registry()
        spec = validate_experiment_spec(_spec_document(), registry=registry)
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest = run_experiment(
                spec,
                "offline",
                output_root=temp_dir,
                candidate={"p_gain": 0.1, "i_gain": 0.01, "damping": 0.2},
                registry=registry,
            )
            run_root = Path(temp_dir) / spec.fingerprint / manifest.run_uid
            self.assertEqual(len(run_root.name), 64)
            self.assertTrue((run_root / "run_state.jsonl").is_file())
            self.assertTrue((run_root / "run_manifest.json").is_file())
            payload = json.loads((run_root / "run_manifest.json").read_text())
            self.assertFalse(payload["optimizer_eligible"])
            self.assertIsNone(payload["objective"])
            self.assertEqual(payload["authorization_ref"], None)
            self.assertEqual(payload["fingerprints"]["pre"], spec.fingerprint)
            self.assertEqual(payload["fingerprints"]["post"], spec.fingerprint)
            self.assertEqual(payload["run_uid"], payload["run_identity_sha256"])
            self.assertEqual(payload["resource_locks"]["requested"], [])
            self.assertEqual(payload["resource_locks"]["acquired"], [])
            parallel = json.loads((run_root / "parallel_run_manifest.json").read_text())
            self.assertEqual(
                parallel["contract"],
                {
                    "id": "ur10e_concurrency_contract_v1",
                    "source_sha256": spec.document["resource_contract"][
                        "source_sha256"
                    ],
                },
            )
            self.assertEqual(parallel["source_fingerprints"]["pre"], "b" * 64)
            self.assertEqual(parallel["source_fingerprints"]["post"], "b" * 64)
            self.assertEqual(parallel["resource_receipts"].keys(), {
                "acquisition_sha256", "release_sha256"
            })
            self.assertEqual(
                {path.name for path in run_root.iterdir()},
                {
                    "run_state.jsonl",
                    "parallel_run_manifest.json",
                    "run_manifest.json",
                },
            )
            with self.assertRaises(OutputPathError):
                append_run_state_event(
                    run_root,
                    {"schema": "ur10e.run_state_event/v1", "event": "too_late"},
                    evidence_lock_root=Path(temp_dir) / "locks",
                )
            self.assertEqual(
                status(manifest.run_uid, output_root=temp_dir)["kind"], "run"
            )

    def test_exclusive_run_directory_refuses_collision(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fingerprint = "a" * 64
            run_uid = "b" * 64
            create_exclusive_run_directory(temp_dir, fingerprint, run_uid)
            with self.assertRaises(OutputPathError):
                create_exclusive_run_directory(temp_dir, fingerprint, run_uid)

    def test_host_resource_lock_refuses_a_held_live_writer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            first = HostResourceLock("live_writer", lock_root=temp_dir)
            second = HostResourceLock("live_writer", lock_root=temp_dir)
            with first:
                self.assertTrue(first.held)
                with self.assertRaises(ResourceLockError):
                    second.acquire()
            self.assertFalse(first.held)
            with second:
                self.assertTrue(second.held)

    def test_concurrent_state_appends_are_complete_and_serialized(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "run"
            root.mkdir()
            lock_root = Path(temp_dir) / "locks"

            def append(sequence):
                append_run_state_event(
                    root,
                    {
                        "schema": "ur10e.run_state_event/v1",
                        "event": "concurrent_test",
                        "sequence": sequence,
                    },
                    evidence_lock_root=lock_root,
                )

            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(append, range(32)))
            records = [
                json.loads(line)
                for line in (root / "run_state.jsonl").read_text().splitlines()
            ]
            self.assertEqual(len(records), 32)
            self.assertEqual([record["sequence"] for record in records], list(range(32)))
            self.assertEqual(
                {record["event"]["sequence"] for record in records}, set(range(32))
            )
            self.assertEqual(verify_run_state_chain(root), tuple(records))
            self.assertNotIn(".evidence.lock", {path.name for path in root.iterdir()})

    def test_state_chain_reconstructs_and_tampering_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "run"
            root.mkdir()
            for sequence in range(3):
                append_run_state_event(
                    root,
                    {"event": "crash_cut", "cut": sequence},
                    evidence_lock_root=Path(temp_dir) / "locks",
                )
            reconstructed = reconstruct_run_state(root)
            self.assertEqual(reconstructed["record_count"], 3)
            self.assertEqual(
                [event["cut"] for event in reconstructed["events"]], [0, 1, 2]
            )
            self.assertFalse(reconstructed["final_manifest_present"])

            path = root / "run_state.jsonl"
            records = path.read_text().splitlines()
            middle = json.loads(records[1])
            middle["event"]["cut"] = 99
            records[1] = json.dumps(middle, sort_keys=True, separators=(",", ":"))
            path.write_text("\n".join(records) + "\n")
            with self.assertRaisesRegex(OutputPathError, "record hash"):
                verify_run_state_chain(root)

    def test_gazebo_scaffold_acquires_and_records_exclusive_resource(self):
        registry = _registry()
        spec = validate_experiment_spec(_spec_document(), registry=registry)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lock_root = root / "locks"
            output_root = root / "runs"
            with HostResourceLock("visible_gazebo", lock_root=lock_root):
                with self.assertRaises(ResourceLockError):
                    run_experiment(
                        spec,
                        "gazebo",
                        output_root=output_root,
                        registry=registry,
                        resource_lock_root=lock_root,
                    )
            self.assertFalse(output_root.exists())

            manifest = run_experiment(
                spec,
                "gazebo",
                output_root=output_root,
                registry=registry,
                resource_lock_root=lock_root,
            )
            payload = manifest.document
            self.assertEqual(payload["resource_locks"]["requested"], ["visible_gazebo"])
            self.assertEqual(payload["resource_locks"]["acquired"], ["visible_gazebo"])
            state = [
                json.loads(line)
                for line in (
                    manifest.source_path.parent / "run_state.jsonl"
                ).read_text().splitlines()
            ]
            self.assertEqual(
                state[0]["event"]["resource_locks_acquired"], ["visible_gazebo"]
            )
            self.assertEqual(
                state[1]["event"]["resource_locks_released"], ["visible_gazebo"]
            )
            with HostResourceLock("visible_gazebo", lock_root=lock_root):
                pass

    def test_serial_fallback_is_recorded_without_changing_run_semantics(self):
        registry = _registry()
        spec = validate_experiment_spec(_spec_document(), registry=registry)
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ", {"UR10E_PARALLEL": "0"}
        ):
            manifest = run_experiment(
                spec,
                "offline",
                output_root=temp_dir,
                registry=registry,
            )
            parallel = json.loads(
                (manifest.source_path.parent / "parallel_run_manifest.json").read_text()
            )
            self.assertTrue(parallel["serial_fallback"])
            self.assertEqual(parallel["claim_class"], "offline_scaffold")

    def test_hil_run_and_campaign_entrypoints_fail_closed(self):
        registry = _registry()
        spec = validate_experiment_spec(_spec_document(), registry=registry)
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(UnsupportedExecutionError):
                run_experiment(
                    spec,
                    "hil",
                    output_root=temp_dir,
                    registry=registry,
                )
            self.assertEqual(list(Path(temp_dir).iterdir()), [])
        with self.assertRaises(UnsupportedExecutionError):
            start_campaign(spec, "live_autotune", "must-not-be-consumed")

        stderr = StringIO()
        with patch(
            "ur10e_experiment_runtime.cli.load_experiment_spec", return_value=spec
        ), redirect_stderr(stderr):
            result = main(
                [
                    "campaign",
                    "start",
                    "unused.json",
                    "--lane",
                    "live_autotune",
                    "--authorization-ref",
                    "must-not-be-consumed",
                ]
            )
        self.assertEqual(result, 2)
        self.assertIn("no external action was attempted", stderr.getvalue())

    def test_run_manifest_schema_and_optimizer_gate_fail_closed(self):
        registry = _registry()
        spec = validate_experiment_spec(_spec_document(), registry=registry)
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest = run_experiment(
                spec,
                "offline",
                output_root=temp_dir,
                registry=registry,
            )
            unknown = copy.deepcopy(manifest.document)
            unknown["output_path"] = "/tmp/not-identity"
            with self.assertRaises(SpecValidationError):
                validate_run_manifest(unknown)

            polluted = copy.deepcopy(manifest.document)
            polluted["objective"] = 9.5
            with self.assertRaises(SpecValidationError):
                validate_run_manifest(polluted)


if __name__ == "__main__":
    unittest.main()
