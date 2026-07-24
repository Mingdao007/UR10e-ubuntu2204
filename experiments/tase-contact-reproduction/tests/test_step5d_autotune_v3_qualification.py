#!/usr/bin/env python3
"""No-network behavior tests for Step5d V3 production qualification."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import inspect
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = ROOT.parents[1]
RUNTIME_SOURCE = REPOSITORY_ROOT / "src/ur10e_experiment_runtime"
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(RUNTIME_SOURCE))

from step5d_autotune_contract import (  # noqa: E402
    CampaignSpec,
    ExecutionProfile,
    ForceCandidate,
    TrialTransition,
    TrialTransitionKind,
    TrialSpec,
)
from step5d_autotune_backend import PreparedFingerprint, PreparedTrial  # noqa: E402
from step5d_autotune_live_driver import (  # noqa: E402
    AtomicCommandMailbox,
    BridgeMailboxRuntime,
    HostClosureCollector,
    execution_profile_id_for,
)
from step5d_autotune_state_machine import (  # noqa: E402
    HostCommand,
    HostPacket,
    TpLoopState,
)
from step5d_autotune_v3.qualification import (  # noqa: E402
    BRIDGE_EXITED_AFTER_FIRST_TRIAL,
    BRIDGE_EXITED_BEFORE_FIRST_ARM_ACK,
    BRIDGE_EXITED_DURING_FIRST_TRIAL,
    BRIDGE_EXITED_IMMEDIATELY,
    BRIDGE_NOT_ALIVE_AFTER_NEXT_ACK,
    CANONICAL_LAUNCH_ENV,
    ENDPOINT_INJECTION_UNAVAILABLE,
    NEXT_ARM_ACK_MISSING,
    PROCESS_TREE_BINDING_INCOMPLETE,
    QualificationError,
    QualificationLifecycle,
    capture_content_binding,
    read_process_starttime,
    require_canonical_launcher,
    run_endpoint_qualification,
    validate_content_binding,
    write_qualification_evidence,
)
from step5d_autotune_v3.qualification_endpoints import (  # noqa: E402
    default_r012_runtime_identity,
)
import step5d_autotune_v3.qualification as qualification  # noqa: E402
import run_step5d_autotune_v3_qualification as worker  # noqa: E402


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def runtime_process_binding_fixture() -> dict[str, object]:
    return {
        "schema": "step5d.autotune-v3/runtime-process-binding-v1",
        "bundle_id": SHA_A,
        "contract_sha256": SHA_B,
        "lock_sha256": SHA_C,
        "runtime_attestation_sha256": SHA_A,
        "gpu_uuid": "GPU-qualification-fixture",
        "profiles": {
            "control": {
                "environment_id": SHA_B,
                "python_executable": "/runtime/control/bin/python",
                "record_tree_sha256": SHA_C,
                "profile_tree_sha256": SHA_A,
            },
            "optimizer": {
                "environment_id": SHA_C,
                "python_executable": "/runtime/optimizer/bin/python",
                "record_tree_sha256": SHA_A,
                "profile_tree_sha256": SHA_B,
            },
        },
    }


def runtime_pointer_fixture() -> dict[str, object]:
    process = runtime_process_binding_fixture()
    return {
        "bundle_id": process["bundle_id"],
        "contract_sha256": process["contract_sha256"],
        "lock_sha256": process["lock_sha256"],
        "attestation_sha256": SHA_C,
        "attestation_path": "/runtime/attestation/attestation.json",
        "profiles": {
            "control": {
                "root": "/runtime/control",
                "environment_id": process["profiles"]["control"]["environment_id"],
                "python_executable": process["profiles"]["control"]["python_executable"],
                "record_tree_sha256": process["profiles"]["control"]["record_tree_sha256"],
                "profile_tree_sha256": process["profiles"]["control"]["profile_tree_sha256"],
            },
            "optimizer": {
                "root": "/runtime/optimizer",
                "environment_id": process["profiles"]["optimizer"]["environment_id"],
                "python_executable": process["profiles"]["optimizer"]["python_executable"],
                "record_tree_sha256": process["profiles"]["optimizer"]["record_tree_sha256"],
                "profile_tree_sha256": process["profiles"]["optimizer"]["profile_tree_sha256"],
            },
        },
    }


def make_trial(
    *,
    trial_id: int = 1,
    command_seq: int = 10,
    candidate_token: int = 20,
    profile: ExecutionProfile | None = None,
) -> TrialSpec:
    campaign = CampaignSpec(
        campaign_id="qualification-behavior-campaign",
        campaign_epoch=7,
        campaign_fingerprint=SHA_A,
    )
    return TrialSpec(
        campaign=campaign,
        trial_id=trial_id,
        candidate_token=candidate_token,
        command_seq=command_seq,
        plant_epoch=1,
        candidate=ForceCandidate(),
        execution_profile=profile or ExecutionProfile("nf010-slew010-a010", 0.010),
        backend_id="step5d-v35-qualification-test",
        source_fingerprint=SHA_B,
        config_fingerprint=SHA_C,
        transition=TrialTransition(TrialTransitionKind.BASELINE),
    )


def make_prepared(trial: TrialSpec) -> PreparedTrial:
    candidate = trial.candidate
    profile = trial.execution_profile
    return PreparedTrial(
        trial=trial,
        frozen=PreparedFingerprint(
            source_fingerprint=trial.source_fingerprint,
            config_fingerprint=trial.config_fingerprint,
            composite_fingerprint=trial.campaign.campaign_fingerprint,
        ),
        environment={
            "STEP5D_AUTOTUNE_FORCE_P": f"{candidate.force_p_gain:.12g}",
            "STEP5D_AUTOTUNE_FORCE_I": f"{candidate.force_i_gain:.12g}",
            "STEP5D_AUTOTUNE_FORCE_DAMPING": f"{candidate.force_damping:.12g}",
            "STEP5D_AUTOTUNE_NORMAL_RATE_RAD_S": f"{profile.normal_max_rate_rad_s:.12g}",
            "STEP5D_AUTOTUNE_HOST_SLEW_RAD_S2": f"{profile.host_qdot_slew_rad_s2:.12g}",
            "STEP5D_AUTOTUNE_SPEEDJ_ACCELERATION_RAD_S2": (
                f"{profile.tp_speedj_accel_rad_s2:.12g}"
            ),
        },
        runner_arguments=(),
    )


def packet_for(trial: TrialSpec, command: HostCommand, *, command_seq: int | None = None) -> HostPacket:
    return HostPacket(
        campaign_epoch=trial.campaign.campaign_epoch,
        trial_id=trial.trial_id,
        command=command,
        candidate_token=trial.candidate_token,
        execution_profile_id=execution_profile_id_for(
            trial.execution_profile, network_mode=True
        ),
        command_seq=trial.command_seq if command_seq is None else command_seq,
    )


def fake_rtde(
    state: TpLoopState,
    packet: HostPacket | None,
    *,
    consumed_seq: int,
    reason: int = 0,
) -> dict[str, object]:
    if packet is None:
        epoch = trial_id = token = profile_id = 0
    else:
        epoch = packet.campaign_epoch
        trial_id = packet.trial_id
        token = packet.candidate_token
        profile_id = packet.execution_profile_id
    return {
        "timestamp": 1.0,
        "output_int_register_24": epoch,
        "output_int_register_25": trial_id,
        "output_int_register_26": int(state),
        "output_int_register_27": token,
        "output_int_register_28": reason,
        "output_int_register_29": profile_id,
        "output_int_register_30": consumed_seq,
        "output_double_register_36": 0.0002,
        "output_double_register_37": 0.001,
        "output_double_register_38": 0.0005,
        "actual_TCP_pose": [0.4, -0.2, 0.3, 0.0, 0.0, 0.0],
        "actual_q": [0.0] * 6,
        "actual_TCP_speed": [0.0] * 6,
        "actual_qd": [0.0] * 6,
        "safety_mode": "NORMAL",
    }


def fake_bridge_args() -> SimpleNamespace:
    return SimpleNamespace(
        step5d_autotune_handshake={
            "campaign_epoch": 0,
            "trial_id": 0,
            "command": 0,
            "candidate_token": 0,
            "execution_profile_id": 0,
            "command_seq": 0,
        },
        bridge_normal_max_rate_rad_s=0.010,
        step4e_normal_max_rate_rad_s=0.010,
    )


def production_ready_payload(pid: int, nonce: str) -> dict[str, object]:
    return {
        "ready_schema": "step5d_bridge_ready_v2",
        "ok": True,
        "pid": pid,
        "launch_nonce": nonce,
        "bridge_profile": "step5d_strict_rnn_autotune_v1",
        "rtde_hz": 500.0,
        "runtime_scheduler": {},
        "runtime_scheduler_lifecycle": {},
        "prewarm_status": "ok",
        "v30_runtime_complete": True,
        "rtde_connected": True,
        "rtde_send_succeeded": True,
        "sensor_stream_ready": True,
        "sensor_samples": 1,
        "baseline_ready": True,
        "sensor_age_s": 0.0,
        "sensor_stale_s": 0.1,
        "parse_errors": 0,
        "output_dir": "/qualification/fixture",
    }


def runner_ready_payload(bridge_run: Path, pid: int = 12345) -> dict[str, object]:
    return {
        "schema_version": "step5d_autotune_runner_ready_v1",
        "ok": True,
        "pid": pid,
        "bridge_run": str(bridge_run),
        "campaign_root": str(bridge_run.parent / "campaign"),
        "campaign_epoch": 7,
        "campaign_fingerprint": SHA_A,
        "selection_policy": "codex_batches",
        "state": "ready_home",
        "durable_state_ready": True,
    }


class Step5dQualificationTest(unittest.TestCase):
    def binding(self) -> dict[str, object]:
        source = ROOT / "scripts/step5d-autotune-v3.sh"
        identity = SimpleNamespace(
            manifest_sha256=SHA_A,
            source_fingerprints={
                "scripts/step5d-autotune-v3.sh": hashlib.sha256(source.read_bytes()).hexdigest()
            },
        )
        snapshot = SimpleNamespace(
            valid=True,
            manifest_sha256=SHA_A,
            source_fingerprint=SHA_B,
            error=None,
        )
        with patch(
            "step5d_autotune_v3.governance.load_current_release_snapshot",
            return_value=snapshot,
        ), patch(
            "step5d_autotune_v3.release_identity.load_current_release",
            return_value=identity,
        ), patch.object(
            qualification,
            "load_runtime_pointer",
            return_value=runtime_pointer_fixture(),
        ), patch.object(
            qualification,
            "runtime_binding",
            return_value=runtime_process_binding_fixture(),
        ):
            return capture_content_binding(
                ROOT,
                manifest_sha256=SHA_A,
                environment={
                    CANONICAL_LAUNCH_ENV: str(ROOT / "scripts/step5d-autotune-v3.sh")
                },
            )

    def test_canonical_guard_requires_exact_non_symlink_path_string(self) -> None:
        canonical = ROOT / "scripts/step5d-autotune-v3.sh"
        self.assertEqual(
            require_canonical_launcher(
                ROOT, {CANONICAL_LAUNCH_ENV: str(canonical)}
            ),
            canonical,
        )
        for observed in (None, "scripts/step5d-autotune-v3.sh", str(canonical) + "/"):
            environment = {} if observed is None else {CANONICAL_LAUNCH_ENV: observed}
            with self.assertRaisesRegex(QualificationError, "not a public entrypoint"):
                require_canonical_launcher(ROOT, environment)
        with tempfile.TemporaryDirectory() as directory:
            alias = Path(directory) / "launcher"
            alias.symlink_to(canonical)
            with self.assertRaisesRegex(QualificationError, "not a public entrypoint"):
                require_canonical_launcher(ROOT, {CANONICAL_LAUNCH_ENV: str(alias)})

    def test_launch_attempt_id_does_not_invalidate_offline_environment(self) -> None:
        pointer = runtime_pointer_fixture()
        common = {
            CANONICAL_LAUNCH_ENV: str(ROOT / "scripts/step5d-autotune-v3.sh"),
            "PYTHONNOUSERSITE": "1",
        }
        with patch.object(
            qualification,
            "load_runtime_contract",
            return_value={"python": {"version": "3.10.12"}},
        ), patch.object(
            qualification,
            "runtime_binding",
            return_value=runtime_process_binding_fixture(),
        ):
            first = qualification._environment_binding(
                {**common, "STEP5D_V3_LAUNCH_ATTEMPT_ID": "attempt-a"},
                runtime_pointer=pointer,
            )
            second = qualification._environment_binding(
                {**common, "STEP5D_V3_LAUNCH_ATTEMPT_ID": "attempt-b"},
                runtime_pointer=pointer,
            )
        self.assertEqual(first, second)
        self.assertNotIn(
            "STEP5D_V3_LAUNCH_ATTEMPT_ID",
            first["values"],
        )

    def test_release_scope_ignores_optimizer_deployment_but_binds_control(self) -> None:
        first = runtime_pointer_fixture()
        optimizer_changed = json.loads(json.dumps(first))
        optimizer_changed["bundle_id"] = SHA_B
        optimizer_changed["contract_sha256"] = SHA_C
        optimizer_changed["lock_sha256"] = SHA_A
        optimizer_changed["profiles"]["optimizer"]["environment_id"] = SHA_A
        optimizer_changed["profiles"]["optimizer"]["record_tree_sha256"] = SHA_B
        optimizer_changed["profiles"]["optimizer"]["profile_tree_sha256"] = SHA_C

        self.assertEqual(
            qualification._control_environment_fingerprint(first),
            qualification._control_environment_fingerprint(optimizer_changed),
        )

        control_changed = json.loads(json.dumps(first))
        control_changed["profiles"]["control"]["profile_tree_sha256"] = SHA_B
        self.assertNotEqual(
            qualification._control_environment_fingerprint(first),
            qualification._control_environment_fingerprint(control_changed),
        )

    def test_release_scope_process_tree_excludes_optimizer_runner(self) -> None:
        observed_paths: list[Path] = []

        def hash_path(path: Path) -> str:
            observed_paths.append(path)
            return hashlib.sha256(str(path).encode("utf-8")).hexdigest()

        with patch.object(
            qualification,
            "_sha256_file",
            side_effect=hash_path,
        ):
            fingerprint = (
                qualification.qualification_safety_process_tree_fingerprint(ROOT)
            )

        self.assertEqual(len(fingerprint), 64)
        observed = {path.name for path in observed_paths}
        self.assertNotIn("run_step5d_autotune_campaign.py", observed)
        self.assertIn("run_step5d_autotune_v3_live.py", observed)
        self.assertIn("run_step5d_autotune_v3_bridge.py", observed)

    def test_worker_reuse_only_never_runs_full_runtime_gate(self) -> None:
        payload = {
            "ok": True,
            "reason_code": "QUALIFIED",
            "remaining_integration_seam": None,
            "binding": {"manifest_sha256": SHA_A},
        }
        evidence = {"path": "/qualification/evidence.json", "sha256": SHA_B}
        with tempfile.TemporaryDirectory() as directory, patch.object(
            worker, "require_canonical_launcher"
        ), patch.object(
            worker, "require_runtime_profile"
        ) as runtime_gate, patch.object(
            worker,
            "run_endpoint_qualification",
            return_value=(payload, evidence),
        ) as qualify:
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                rc = worker.main(
                    ["--output-root", directory, "--reuse-only"]
                )
        self.assertEqual(rc, 0)
        runtime_gate.assert_not_called()
        self.assertTrue(qualify.call_args.kwargs["reuse_only"])
        self.assertEqual(json.loads(stream.getvalue())["reason_code"], "QUALIFIED")

    def test_worker_reuse_only_reports_missing_release_certificate(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(
            worker, "require_canonical_launcher"
        ), patch.object(
            worker,
            "run_endpoint_qualification",
            side_effect=qualification.QualificationBlocked(
                qualification.RELEASE_CERTIFICATE_MISSING
            ),
        ):
            stream = io.StringIO()
            with contextlib.redirect_stderr(stream):
                rc = worker.main(
                    ["--output-root", directory, "--reuse-only"]
                )
        self.assertEqual(rc, 64)
        self.assertEqual(
            json.loads(stream.getvalue())["reason_code"],
            qualification.RELEASE_CERTIFICATE_MISSING,
        )

    def test_reuse_only_certificate_miss_stops_before_full_runtime_gate(self) -> None:
        release = SimpleNamespace(manifest_sha256=SHA_A)
        prebinding = {
            "source": {
                "fingerprint": SHA_A,
                "files_fingerprint": SHA_B,
            },
            "launcher": {"sha256": SHA_C},
            "environment": {"fingerprint": SHA_A},
            "process_tree": {
                "complete": False,
                "fingerprint": SHA_C,
                "processes": [],
            },
        }
        environment = {
            CANONICAL_LAUNCH_ENV: str(ROOT / "scripts/step5d-autotune-v3.sh")
        }
        with tempfile.TemporaryDirectory() as directory, patch.object(
            qualification,
            "_qualification_release",
            return_value=(release, release),
        ), patch.object(
            qualification,
            "_source_binding",
            return_value=prebinding["source"],
        ), patch.object(
            qualification,
            "load_runtime_pointer_identity",
            return_value=runtime_pointer_fixture(),
        ), patch.object(
            qualification,
            "production_runtime_environment",
            return_value=environment,
        ) as environment_builder, patch.object(
            qualification,
            "capture_content_binding",
            return_value=prebinding,
        ), patch.object(
            qualification,
            "qualification_safety_process_tree_fingerprint",
            return_value=SHA_C,
        ), patch.object(
            qualification,
            "_read_release_certificate_for_scope",
            return_value=None,
        ), patch(
            "step5d_autotune_v3.runtime_gate.release_runtime_contract",
            return_value={
                "tp_runtime_identity": default_r012_runtime_identity().as_dict(),
                "expected_loaded_program": (
                    "/programs/andyl/kunwei/step5/"
                    "step5d_strict_rnn_autotune_v3_r012.urp"
                ),
            },
        ), patch.object(
            qualification,
            "load_runtime_pointer",
        ) as full_runtime:
            with self.assertRaisesRegex(
                qualification.QualificationBlocked,
                qualification.RELEASE_CERTIFICATE_MISSING,
            ):
                run_endpoint_qualification(
                    ROOT,
                    Path(directory),
                    environment=environment,
                    release_identity=release,
                    reuse_only=True,
                )
        full_runtime.assert_not_called()
        environment_builder.assert_called_once()

    def test_production_shaped_endpoint_ports_require_one_cross_process_lease(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            environment = {
                **os.environ,
                "UR10E_LOCK_ROOT": str(root / "locks"),
            }
            with qualification.qualification_endpoint_lease(
                first,
                environment=environment,
            ):
                active = json.loads(
                    (first / "qualification_endpoint_lease.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(active["status"], "ACTIVE")
                self.assertEqual(
                    active["ports"], qualification.QUALIFICATION_ENDPOINT_PORTS
                )
                with self.assertRaisesRegex(
                    QualificationError,
                    qualification.QUALIFICATION_ENDPOINT_LEASE_BUSY,
                ):
                    with qualification.qualification_endpoint_lease(
                        second,
                        environment=environment,
                    ):
                        self.fail("a second qualification endpoint owner was admitted")
            released = json.loads(
                (first / "qualification_endpoint_lease.json").read_text(
                    encoding="utf-8"
                )
            )
            blocked = json.loads(
                (second / "qualification_endpoint_lease.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(released["status"], "RELEASED")
            self.assertLess(
                released["acquired_at_unix_ns"], released["released_at_unix_ns"]
            )
            self.assertEqual(blocked["status"], "BLOCKED")
            self.assertEqual(
                blocked["reason_code"],
                qualification.QUALIFICATION_ENDPOINT_LEASE_BUSY,
            )
    def test_direct_worker_refuses_before_creating_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory).resolve()
            environment = dict(os.environ)
            environment.pop(CANONICAL_LAUNCH_ENV, None)
            environment["PYTHONPATH"] = os.pathsep.join(
                (str(ROOT / "tools"), str(RUNTIME_SOURCE))
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools/run_step5d_autotune_v3_qualification.py"),
                    "--experiment-root",
                    str(ROOT),
                    "--output-root",
                    str(output),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(completed.returncode, 64, completed.stderr)
            self.assertIn("CANONICAL_QUALIFICATION_REFUSED", completed.stderr)
            self.assertFalse((output / "qualification").exists())

    def test_hidden_shell_mode_is_absent_from_help_and_fails_closed(self) -> None:
        help_text = worker._parser().format_help()
        self.assertNotIn("exec-live-from-shell-contract", help_text)
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory).resolve() / "missing-contract.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools/run_step5d_autotune_v3_qualification.py"),
                    "--_exec-live-from-shell-contract",
                    str(missing),
                ],
                check=False,
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "PYTHONPATH": os.pathsep.join(
                        (str(ROOT / "tools"), str(RUNTIME_SOURCE))
                    ),
                },
            )
        self.assertEqual(completed.returncode, 64, completed.stderr)
        self.assertIn("CANONICAL_QUALIFICATION_REFUSED", completed.stderr)
        self.assertIn("contract path is unsafe", completed.stderr)

    def test_canonical_worker_reports_named_blocker_not_fake_success(self) -> None:
        payload = {
            "ok": False,
            "reason_code": ENDPOINT_INJECTION_UNAVAILABLE,
            "remaining_integration_seam": "missing endpoint-only seam",
        }
        evidence = {"path": "/tmp/evidence", "sha256": SHA_A}
        stream = io.StringIO()
        with patch.object(
            worker, "require_canonical_launcher"
        ), patch.object(
            worker, "require_runtime_profile"
        ), patch.object(
            worker,
            "run_endpoint_qualification",
            return_value=(payload, evidence),
        ):
            with contextlib.redirect_stdout(stream):
                rc = worker.main(["--output-root", "/tmp"])
        self.assertEqual(rc, 78)
        result = json.loads(stream.getvalue())
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], ENDPOINT_INJECTION_UNAVAILABLE)
        self.assertIsNone(result["certificate"])
        self.assertEqual(result["qualification_evidence"], evidence)

    def test_invalid_release_binding_stops_before_endpoints_and_never_writes_current(self) -> None:
        canonical = ROOT / "scripts/step5d-autotune-v3.sh"
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory).resolve()
            with patch(
                "step5d_autotune_v3.release_identity.load_current_release",
                return_value=SimpleNamespace(
                    manifest_sha256=SHA_A,
                    source_fingerprints={
                        "scripts/step5d-autotune-v3.sh": hashlib.sha256(
                            canonical.read_bytes()
                        ).hexdigest()
                    },
                ),
            ):
                with self.assertRaisesRegex(
                    QualificationError, "qualification release source binding is invalid"
                ):
                    run_endpoint_qualification(
                        ROOT,
                        output,
                        environment={CANONICAL_LAUNCH_ENV: str(canonical)},
                    )
            self.assertEqual(list(output.rglob("certificate.json")), [])
            self.assertEqual(list(output.rglob("bridge_ready.json")), [])

    def test_synthetic_delivery_observation_is_endpoint_only_and_loadable(self) -> None:
        from step5d_autotune_v3.delivery_observation import (
            load_delivery_observation,
        )

        with tempfile.TemporaryDirectory() as directory:
            experiment_root = Path(directory).resolve()
            run_root = experiment_root / "runs/qualification"
            run_root.mkdir(parents=True)
            release = SimpleNamespace(
                manifest_sha256=SHA_A,
                program_id="step5d_strict_rnn_autotune_v3_r009",
                controller_target=(
                    "/programs/step5d_strict_rnn_autotune_v3_r009.urp"
                ),
                artifact_sha256={
                    ".script": SHA_A,
                    ".txt": SHA_B,
                    ".urp": SHA_C,
                },
            )
            observation_path = qualification._write_synthetic_delivery_observation(
                experiment_root,
                run_root,
                release=release,
                endpoint_content_sha256=SHA_B,
            )
            observation = load_delivery_observation(
                experiment_root,
                observation_path,
                release=release,
            )
            receipt_path = experiment_root / observation["receipt"]["path"]
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(
                receipt["qualification_endpoint_substitution"],
                {
                    "schema": qualification.SYNTHETIC_DELIVERY_RECEIPT_SCHEMA,
                    "controller_contacted": False,
                    "endpoint_only": True,
                    "motion_capable": False,
                    "endpoint_content_sha256": SHA_B,
                },
            )
            self.assertEqual(observation["triplet_sha256"], release.artifact_sha256)

    def test_preflight_and_live_commands_share_delivery_observation(self) -> None:
        source = inspect.getsource(
            qualification._run_production_endpoint_qualification
        )
        self.assertEqual(source.count('"--delivery-observation"'), 1)
        preflight_source = source[
            source.index("preflight_command = [") : source.index(
                "with preflight_log.open"
            )
        ]
        self.assertIn('"--delivery-observation"', preflight_source)
        self.assertIn("str(delivery_observation_path)", preflight_source)
        self.assertIn(
            "delivery_observation_path=delivery_observation_path",
            source,
        )
        hidden_shell_source = inspect.getsource(
            qualification.exec_internal_shell_contract
        )
        self.assertIn('"--delivery-observation"', hidden_shell_source)
        self.assertIn('contract["delivery_observation"]["path"]', hidden_shell_source)

    def test_binding_tamper_and_non_production_process_are_rejected(self) -> None:
        binding = self.binding()
        binding["source"]["files"]["invented.py"] = SHA_B
        with self.assertRaisesRegex(QualificationError, "source-file fingerprint"):
            validate_content_binding(binding)
        with patch.object(
            qualification,
            "load_runtime_pointer",
            return_value=runtime_pointer_fixture(),
        ), patch.object(
            qualification,
            "runtime_binding",
            return_value=runtime_process_binding_fixture(),
        ):
            with self.assertRaisesRegex(
                QualificationError, "does not execute production script"
            ):
                capture_content_binding(
                    ROOT,
                    manifest_sha256=SHA_A,
                    process_pids={
                        "canonical_launcher": os.getpid(),
                        "launcher_supervisor": os.getpid(),
                        "bridge_wrapper": os.getpid(),
                        "campaign_runner": os.getpid(),
                    },
                )

    def test_process_tree_requires_exact_role_runtime_binding(self) -> None:
        binding = self.binding()
        runtime_profiles = binding["environment"]["values"]["runtime_binding"][
            "profiles"
        ]
        role_profiles = {
            "canonical_launcher": None,
            "launcher_supervisor": "control",
            "bridge_wrapper": "control",
            "campaign_runner": "optimizer",
        }
        pids = {
            "canonical_launcher": 101,
            "launcher_supervisor": 102,
            "bridge_wrapper": 103,
            "campaign_runner": 104,
        }
        processes = []
        for role, relative in sorted(qualification.production_process_role_paths().items()):
            profile = role_profiles[role]
            argv0 = (
                "/usr/bin/bash"
                if profile is None
                else runtime_profiles[profile]["python_executable"]
            )
            script = (ROOT / relative).resolve(strict=True)
            argv = [argv0, str(script)]
            processes.append(
                {
                    "role": role,
                    "pid": pids[role],
                    "ppid": (
                        1
                        if role == "canonical_launcher"
                        else pids["canonical_launcher"]
                        if role == "launcher_supervisor"
                        else pids["launcher_supervisor"]
                    ),
                    "starttime": pids[role] + 1000,
                    "executable": argv0,
                    "argv": argv,
                    "argv0": argv0,
                    "argv_sha256": qualification._sha256_bytes(
                        qualification._canonical_bytes(argv)
                    ),
                    "runtime_profile": profile,
                    "environment_id": (
                        None
                        if profile is None
                        else runtime_profiles[profile]["environment_id"]
                    ),
                    "script": str(script),
                    "script_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
                }
            )
        binding["process_tree"] = {
            "complete": True,
            "processes": processes,
            "fingerprint": qualification._process_tree_shape_fingerprint(processes),
        }
        binding["endpoint_substitution"].update(
            {
                "production_processes_retained": True,
                "ready_writer": "production_bridge",
            }
        )
        validate_content_binding(binding)

        by_role = {process["role"]: process for process in processes}
        self.assertEqual(by_role["launcher_supervisor"]["runtime_profile"], "control")
        self.assertEqual(by_role["bridge_wrapper"]["environment_id"], SHA_B)
        self.assertEqual(by_role["campaign_runner"]["argv0"], "/runtime/optimizer/bin/python")

        wrong_argv0 = copy.deepcopy(binding)
        runner = next(
            process
            for process in wrong_argv0["process_tree"]["processes"]
            if process["role"] == "campaign_runner"
        )
        runner["argv0"] = runner["argv"][0] = "/runtime/control/bin/python"
        runner["argv_sha256"] = qualification._sha256_bytes(
            qualification._canonical_bytes(runner["argv"])
        )
        with self.assertRaisesRegex(QualificationError, "raw argv0 differs"):
            validate_content_binding(wrong_argv0)

        wrong_profile = copy.deepcopy(binding)
        supervisor = next(
            process
            for process in wrong_profile["process_tree"]["processes"]
            if process["role"] == "launcher_supervisor"
        )
        supervisor["runtime_profile"] = "optimizer"
        wrong_profile["process_tree"]["fingerprint"] = (
            qualification._process_tree_shape_fingerprint(
                wrong_profile["process_tree"]["processes"]
            )
        )
        with self.assertRaisesRegex(QualificationError, "runtime profile binding differs"):
            validate_content_binding(wrong_profile)

        wrong_environment = copy.deepcopy(binding)
        bridge = next(
            process
            for process in wrong_environment["process_tree"]["processes"]
            if process["role"] == "bridge_wrapper"
        )
        bridge["environment_id"] = SHA_C
        with self.assertRaisesRegex(QualificationError, "environment ID differs"):
            validate_content_binding(wrong_environment)

    def test_process_capture_uses_raw_argv0_and_process_runtime_environment(self) -> None:
        runtime_binding = runtime_process_binding_fixture()
        runtime_binding["profiles"]["optimizer"]["python_executable"] = sys.executable
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory).resolve() / "campaign_runner.py"
            script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
            environment = {
                **os.environ,
                "STEP5D_V3_RUNTIME_PROFILE": "optimizer",
                "STEP5D_V3_OPTIMIZER_ENVIRONMENT_ID": SHA_C,
            }
            process = subprocess.Popen(
                [sys.executable, str(script)],
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            try:
                deadline = time.monotonic() + 2.0
                while str(script) not in qualification._process_cmdline(process.pid):
                    if process.poll() is not None or time.monotonic() >= deadline:
                        self.fail("fixture process did not expose its script argv")
                    time.sleep(0.005)
                observed = qualification._capture_process(
                    process.pid,
                    "campaign_runner",
                    script,
                    runtime_binding,
                )
            finally:
                process.terminate()
                process.wait(timeout=5)
        self.assertEqual(observed["argv0"], sys.executable)
        self.assertEqual(observed["runtime_profile"], "optimizer")
        self.assertEqual(observed["environment_id"], SHA_C)

    def test_fake_ready_and_print_only_ready_are_rejected(self) -> None:
        starttime = read_process_starttime(os.getpid())
        assert starttime is not None
        lifecycle = QualificationLifecycle(
            self.binding(), os.getpid(), starttime, "nonce"
        )
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory).resolve() / "bridge_ready.json"
            with self.assertRaisesRegex(QualificationError, "unavailable"):
                lifecycle.observe_bridge_ready_file(missing)
            fake = production_ready_payload(os.getpid(), "nonce")
            fake.update(
                {
                    "transport": "fake_no_network_no_motion",
                    "motion_capable": False,
                    "controller_connected": False,
                }
            )
            missing.write_text(json.dumps(fake), encoding="utf-8")
            with self.assertRaisesRegex(QualificationError, "may not manufacture"):
                lifecycle.observe_bridge_ready_file(missing)

    def test_play_requires_waiting_barrier_and_reference_rejects_original_symlink(self) -> None:
        starttime = read_process_starttime(os.getpid())
        assert starttime is not None
        lifecycle = QualificationLifecycle(
            self.binding(), os.getpid(), starttime, "nonce"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ready = root / "bridge_ready.json"
            ready.write_text(
                json.dumps(production_ready_payload(os.getpid(), "nonce")),
                encoding="utf-8",
            )
            lifecycle.observe_bridge_ready_file(ready)
            with self.assertRaisesRegex(QualificationError, "out of order"):
                lifecycle.observe_play(
                    {
                        "ur_runtime_state": "2",
                        "ur_safety_mode": "1",
                        "rtde_connected": "1",
                    }
                )

            target = root / "target.json"
            target.write_text("{}\n", encoding="ascii")
            alias = root / "alias.json"
            alias.symlink_to(target)
            with self.assertRaisesRegex(QualificationError, "unsafe"):
                qualification._reference_file(alias)

    def test_bridge_readiness_snapshot_survives_runtime_sentinel_cleanup(self) -> None:
        starttime = read_process_starttime(os.getpid())
        assert starttime is not None
        lifecycle = QualificationLifecycle(
            self.binding(), os.getpid(), starttime, "nonce"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ready = root / "bridge_ready.json"
            snapshot = root / "qualification_bridge_ready.json"
            encoded = json.dumps(
                production_ready_payload(os.getpid(), "nonce")
            ).encode("utf-8")
            ready.write_bytes(encoded)

            lifecycle.observe_bridge_ready_file(
                ready,
                evidence_path=snapshot,
            )
            ready.unlink()

            self.assertEqual(snapshot.read_bytes(), encoded)
            self.assertEqual(
                lifecycle.bridge_ready_ref,
                {
                    "path": str(snapshot),
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                },
            )

    def test_real_runtime_contract_reaches_next_arm_and_detects_exit_modes(self) -> None:
        alive = {"value": True}
        starttime = 424242
        reader = lambda _pid: starttime if alive["value"] else None
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ready_path = root / "bridge_ready.json"
            ready_path.write_text(
                json.dumps(production_ready_payload(54321, "nonce")),
                encoding="utf-8",
            )
            lifecycle = QualificationLifecycle(
                self.binding(),
                54321,
                starttime,
                "nonce",
                process_starttime_reader=reader,
            )
            alive["value"] = False
            self.assertEqual(lifecycle.result()["reason_code"], BRIDGE_EXITED_IMMEDIATELY)
            alive["value"] = True
            lifecycle.observe_bridge_ready_file(ready_path)
            alive["value"] = False
            self.assertEqual(
                lifecycle.result()["reason_code"],
                BRIDGE_EXITED_BEFORE_FIRST_ARM_ACK,
            )
            alive["value"] = True
            runner_ready = root / "campaign_runner_ready.json"
            runner_ready.write_text(
                json.dumps(runner_ready_payload(root)), encoding="utf-8"
            )
            supervisor_log = root / "launcher_supervisor.log"
            supervisor_log.write_text(
                "V3_QUALIFICATION_SIMULATED_PLAY_BARRIER\n",
                encoding="ascii",
            )
            lifecycle.observe_waiting_barrier(runner_ready, supervisor_log)
            lifecycle.observe_play(
                {"ur_runtime_state": "2", "ur_safety_mode": "1", "rtde_connected": "1"}
            )

            mailbox_path = root / "command.json"
            sink = AtomicCommandMailbox(mailbox_path)
            runtime = BridgeMailboxRuntime(mailbox_path)
            args = fake_bridge_args()
            trial1 = make_trial()
            prepared1 = make_prepared(trial1)
            arm1 = packet_for(trial1, HostCommand.ARM)
            sink.send_command(arm1, prepared_trial=prepared1)
            self.assertTrue(
                runtime.poll(args, fake_rtde(TpLoopState.READY_HOME, None, consumed_seq=0))
            )
            arm1_run = fake_rtde(TpLoopState.RUN, arm1, consumed_seq=arm1.command_seq)
            self.assertFalse(runtime.poll(args, arm1_run))
            lifecycle.observe_arm_ack(runtime, arm1_run)
            alive["value"] = False
            self.assertEqual(
                lifecycle.result()["reason_code"], BRIDGE_EXITED_DURING_FIRST_TRIAL
            )
            alive["value"] = True

            wait_ack = fake_rtde(
                TpLoopState.WAIT_ACK,
                arm1,
                consumed_seq=arm1.command_seq,
                reason=1,
            )
            collector = HostClosureCollector.for_test_fixture(
                expected_arm=arm1,
                campaign_home_pose=[0.4, -0.2, 0.3, 0.0, 0.0, 0.0],
                campaign_home_q=[0.0] * 6,
                max_sample_gap_s=0.02,
            )
            for index in range(51):
                collector.observe(wait_ack, monotonic_s=index * 0.01)
            closure = collector.finalize(
                capture_hashes_complete=True,
                terminal_manifest_complete=True,
                fingerprint_closed=True,
            )
            trial_evidence = root / "trial-evidence.json"
            trial_evidence.write_text(json.dumps(closure.payload()), encoding="utf-8")
            lifecycle.observe_trial_complete(closure, trial_evidence)
            self.assertEqual(lifecycle.result()["reason_code"], NEXT_ARM_ACK_MISSING)
            alive["value"] = False
            self.assertEqual(
                lifecycle.result()["reason_code"], BRIDGE_EXITED_AFTER_FIRST_TRIAL
            )
            alive["value"] = True

            ack1 = packet_for(trial1, HostCommand.ACK_BUNDLE, command_seq=11)
            sink.send_command(ack1, prepared_trial=prepared1)
            self.assertTrue(runtime.poll(args, wait_ack))
            ready = fake_rtde(TpLoopState.READY_HOME, None, consumed_seq=11)
            self.assertFalse(runtime.poll(args, ready))
            trial2 = make_trial(trial_id=2, command_seq=12, candidate_token=21)
            prepared2 = make_prepared(trial2)
            arm2 = packet_for(trial2, HostCommand.ARM)
            sink.send_command(arm2, prepared_trial=prepared2)
            self.assertTrue(runtime.poll(args, ready))
            arm2_run = fake_rtde(TpLoopState.RUN, arm2, consumed_seq=arm2.command_seq)
            self.assertFalse(runtime.poll(args, arm2_run))
            lifecycle.observe_arm_ack(runtime, arm2_run)
            alive["value"] = False
            self.assertEqual(
                lifecycle.result()["reason_code"], BRIDGE_NOT_ALIVE_AFTER_NEXT_ACK
            )
            alive["value"] = True
            result = lifecycle.result()
            self.assertTrue(result["lifecycle_complete"])
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason_code"], PROCESS_TREE_BINDING_INCOMPLETE)
            self.assertEqual(result["first_arm_seq"], 10)
            self.assertEqual(result["next_arm_seq"], 12)

    def test_content_addressed_writer_is_idempotent_and_detects_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            reference1 = write_qualification_evidence(root, {"ok": False, "x": 1})
            reference2 = write_qualification_evidence(root, {"ok": False, "x": 1})
            self.assertEqual(reference1, reference2)
            path = Path(reference1["path"])
            path.write_text("different\n", encoding="ascii")
            with self.assertRaisesRegex(QualificationError, "conflicts"):
                write_qualification_evidence(root, {"ok": False, "x": 1})


if __name__ == "__main__":
    unittest.main()
