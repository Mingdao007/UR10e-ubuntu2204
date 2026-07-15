#!/usr/bin/env python3
"""Evidence-integrity tests for the Step5d-native autotune store."""

from __future__ import annotations

import hashlib
import json
import math
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_contract import (  # noqa: E402
    CampaignSpec,
    CaptureArtifactPaths,
    CaptureManifest,
    Evaluation,
    ExecutionProfile,
    ForceCandidate,
    SafeClosureEvidence,
    TrialDisposition,
    TrialSpec,
    TrialTransition,
    TrialTransitionKind,
    trial_source_from_trial,
)
from step5d_autotune_store import (  # noqa: E402
    CampaignStore,
    DuplicateTrialError,
    EvidenceIntegrityError,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
BACKEND_ID = "step5d-test-backend-v1"


def safe_closure(**updates) -> SafeClosureEvidence:
    payload = {
        "tp_position_error_m": 0.001,
        "tp_orientation_error_rad": 0.01,
        "tp_joint_error_max_rad": 0.005,
        "host_position_error_m": 0.001,
        "host_orientation_error_rad": 0.01,
        "host_joint_error_max_rad": 0.005,
        "host_tcp_linear_speed_m_s": 0.0005,
        "host_tcp_angular_speed_rad_s": 0.005,
        "host_qd_max_rad_s": 0.005,
        "host_safety_mode": "NORMAL",
        "host_dwell_s": 0.5,
        "trial_token_match": True,
        "capture_hashes_complete": True,
        "terminal_manifest_complete": True,
        "fingerprint_closed": True,
    }
    payload.update(updates)
    return SafeClosureEvidence(**payload)


def _sha256(encoded: bytes) -> str:
    return hashlib.sha256(encoded).hexdigest()


def campaign() -> CampaignSpec:
    return CampaignSpec(
        campaign_id="store-hardening",
        campaign_epoch=1,
        campaign_fingerprint=SHA_A,
        f0_shadow_reaction_normal_base=(0.0, 0.0, 1.0),
    )


def trial(*, trial_id: int = 1, token: int = 101) -> TrialSpec:
    transition = TrialTransition(TrialTransitionKind.BASELINE)
    if trial_id != 1:
        transition = TrialTransition(
            TrialTransitionKind.REPLICATION,
            source=trial_source_from_trial(trial()),
        )
    return TrialSpec(
        campaign=campaign(),
        trial_id=trial_id,
        candidate_token=token,
        command_seq=trial_id,
        plant_epoch=1,
        candidate=ForceCandidate(),
        execution_profile=ExecutionProfile("nf010-slew010-a010", 0.010),
        backend_id=BACKEND_ID,
        source_fingerprint=SHA_B,
        config_fingerprint=SHA_C,
        transition=transition,
    )


def evaluation(spec: TrialSpec, objective: float = 0.25) -> Evaluation:
    return Evaluation(
        trial_uid=spec.trial_uid,
        backend_id=spec.backend_id,
        eligible=True,
        disposition=TrialDisposition.OBJECTIVE,
        objective_mae_n=objective,
        force_bias_n=0.0,
        force_std_n=0.1,
        coverage_12_plus_minus_1_ratio=0.99,
        complete_bins=550,
        safe_closure=True,
    )


def write_artifacts(
    root: Path,
    *,
    spec: TrialSpec,
    csv_bytes: bytes = b"stage,path_time_s,force_b_z_n\n25,5.05,12.0\n",
    metadata_bytes: bytes | None = None,
    terminal_bytes: bytes | None = None,
    terminal_reason: int = 1,
) -> tuple[CaptureArtifactPaths, dict[str, str]]:
    if metadata_bytes is None:
        metadata_bytes = (
            json.dumps(
                {
                    "schema_version": "metadata/v1",
                    "trial_uid": spec.trial_uid,
                    "backend_id": spec.backend_id,
                    "candidate_token": spec.candidate_token,
                },
                allow_nan=False,
                sort_keys=True,
            )
            + "\n"
        ).encode()
    if terminal_bytes is None:
        terminal_bytes = (
            json.dumps(
                {
                    "schema_version": "terminal/v1",
                    "trial_uid": spec.trial_uid,
                    "backend_id": spec.backend_id,
                    "candidate_token": spec.candidate_token,
                    "terminal_reason": terminal_reason,
                    "safe_closure_evidence": safe_closure().payload(),
                },
                allow_nan=False,
                sort_keys=True,
            )
            + "\n"
        ).encode()
    root.mkdir(parents=True, exist_ok=True)
    paths = CaptureArtifactPaths(
        csv_path=root / "capture.csv",
        metadata_path=root / "metadata.json",
        terminal_manifest_path=root / "terminal_manifest.json",
    )
    paths.csv_path.write_bytes(csv_bytes)
    paths.metadata_path.write_bytes(metadata_bytes)
    paths.terminal_manifest_path.write_bytes(terminal_bytes)
    return paths, {
        "csv": _sha256(csv_bytes),
        "metadata": _sha256(metadata_bytes),
        "terminal_manifest": _sha256(terminal_bytes),
    }


def manifest(spec: TrialSpec, digests: dict[str, str]) -> CaptureManifest:
    return CaptureManifest(
        trial_uid=spec.trial_uid,
        backend_id=spec.backend_id,
        source_fingerprint_pre=spec.source_fingerprint,
        source_fingerprint_post=spec.source_fingerprint,
        config_fingerprint_pre=spec.config_fingerprint,
        config_fingerprint_post=spec.config_fingerprint,
        candidate_token=spec.candidate_token,
        terminal_reason=1,
        host_cause=None,
        csv_sha256=digests["csv"],
        metadata_sha256=digests["metadata"],
        terminal_manifest_sha256=digests["terminal_manifest"],
        completion_marker=True,
        cadence_ok=True,
        feedback_fresh=True,
        rnn_oracle_aligned=True,
        safety_normal=True,
        returned_safe=True,
        immutable_bundle_written=True,
        stage25_complete_s=60.0,
        safe_closure_evidence=safe_closure(),
    )


class ContractInvariantTest(unittest.TestCase):
    def test_sha256_fields_require_full_lowercase_hex(self) -> None:
        with self.assertRaisesRegex(ValueError, "lowercase hexadecimal"):
            CampaignSpec(
                campaign_id="bad-sha",
                campaign_epoch=1,
                campaign_fingerprint="A" * 64,
            )
        with self.assertRaisesRegex(ValueError, "source_fingerprint"):
            replace(trial(), source_fingerprint="g" * 64)

    def test_evaluation_rejects_nonfinite_values_at_contract_boundary(self) -> None:
        spec = trial()
        with self.assertRaisesRegex(ValueError, "objective_mae_n must be finite"):
            replace(evaluation(spec), objective_mae_n=math.nan)
        with self.assertRaises(ValueError):
            replace(evaluation(spec), metrics={"nested": {"value": math.inf}})

    def test_derived_native_mapping_and_campaign_thresholds_are_fixed(self) -> None:
        with self.assertRaisesRegex(ValueError, "Md/kf/Bd mapping must remain finite"):
            ForceCandidate(force_p_gain=0.001 * (2.0**-1020))
        with self.assertRaisesRegex(ValueError, "thresholds are fixed"):
            replace(campaign(), success_mae_n=0.31)

    def test_trial_identity_is_stable_and_has_no_path_input(self) -> None:
        first = trial()
        second = trial()
        self.assertEqual(first.trial_uid, second.trial_uid)
        self.assertEqual(first.candidate.candidate_uid, second.candidate.candidate_uid)
        serialized = json.dumps(first.payload(), sort_keys=True)
        self.assertNotIn("run_dir", serialized)
        self.assertNotIn("artifact", serialized)

    def test_numeric_identity_is_canonical_across_int_and_float_inputs(self) -> None:
        integer_candidate = ForceCandidate(force_damping=7)
        float_candidate = ForceCandidate(force_damping=7.0)
        self.assertEqual(integer_candidate.candidate_uid, float_candidate.candidate_uid)
        integer_trial = replace(trial(), candidate=integer_candidate)
        float_trial = replace(trial(), candidate=float_candidate)
        self.assertEqual(integer_trial.trial_uid, float_trial.trial_uid)
        self.assertEqual(integer_trial.payload(), float_trial.payload())
        with self.assertRaisesRegex(ValueError, "force_damping must be a finite float"):
            ForceCandidate(force_damping="7")  # type: ignore[arg-type]

    def test_comparison_key_binds_the_complete_execution_profile(self) -> None:
        first = replace(
            trial(),
            execution_profile=ExecutionProfile("nf010-slew010-a010", 0.010),
        )
        second = replace(
            trial(),
            execution_profile=ExecutionProfile("nf020-slew010-a010", 0.020),
        )
        self.assertNotEqual(first.comparison_key, second.comparison_key)

    def test_capture_summary_cannot_spoof_detailed_safe_closure(self) -> None:
        spec = trial()
        digests = {"csv": SHA_A, "metadata": SHA_B, "terminal_manifest": SHA_C}
        with self.assertRaisesRegex(ValueError, "detailed safe closure proof"):
            replace(manifest(spec, digests), returned_safe=False)


class StoreIntegrityTest(unittest.TestCase):
    def initialized_store(self, root: Path) -> CampaignStore:
        store = CampaignStore(root / "campaign-store")
        store.initialize({"schema_version": "test/v1", "campaign_id": "store-hardening"})
        return store

    def test_bundle_rehashes_all_artifacts_and_rejects_any_changed_role(self) -> None:
        for role in ("csv", "metadata", "terminal_manifest"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                spec = trial()
                paths, digests = write_artifacts(root / "capture", spec=spec)
                capture = manifest(spec, digests)
                path = paths.by_role()[role]
                path.write_bytes(path.read_bytes() + b"changed")
                store = self.initialized_store(root)
                with self.assertRaisesRegex(EvidenceIntegrityError, role):
                    store.write_trial_bundle(
                        spec,
                        capture,
                        evaluation(spec),
                        artifact_paths=paths,
                )
                self.assertFalse(store.history_path.exists())
                self.assertEqual(len(list(store.quarantine_dir.glob("*.json"))), 1)

    def test_eligible_bundle_cannot_bypass_capture_closure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            spec = trial()
            paths, digests = write_artifacts(root / "capture", spec=spec)
            capture = replace(manifest(spec, digests), cadence_ok=False)
            store = self.initialized_store(root)
            with self.assertRaisesRegex(EvidenceIntegrityError, "complete safe capture closure"):
                store.write_trial_bundle(
                    spec,
                    capture,
                    evaluation(spec),
                    artifact_paths=paths,
                )

    def test_matching_hash_does_not_allow_non_strict_metadata_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            spec = trial()
            paths, digests = write_artifacts(
                root / "capture",
                spec=spec,
                metadata_bytes=b'{"value":NaN}\n',
            )
            store = self.initialized_store(root)
            with self.assertRaisesRegex(EvidenceIntegrityError, "non-finite JSON"):
                store.write_trial_bundle(
                    spec,
                    manifest(spec, digests),
                    evaluation(spec),
                    artifact_paths=paths,
                )

    def test_matching_hash_rejects_overflowed_json_number(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            spec = trial()
            metadata_bytes = (
                '{"trial_uid":"%s","backend_id":"%s",'
                '"candidate_token":%d,"overflow":1e400}\n'
                % (spec.trial_uid, spec.backend_id, spec.candidate_token)
            ).encode()
            paths, digests = write_artifacts(
                root / "capture",
                spec=spec,
                metadata_bytes=metadata_bytes,
            )
            store = self.initialized_store(root)
            with self.assertRaisesRegex(EvidenceIntegrityError, "non-finite JSON number"):
                store.write_trial_bundle(
                    spec,
                    manifest(spec, digests),
                    evaluation(spec),
                    artifact_paths=paths,
                )

    def test_metadata_and_terminal_identity_fields_are_mandatory(self) -> None:
        for role in ("metadata", "terminal_manifest"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                spec = trial()
                overrides = (
                    {"metadata_bytes": b'{"schema_version":"metadata/v1"}\n'}
                    if role == "metadata"
                    else {"terminal_bytes": b'{"schema_version":"terminal/v1"}\n'}
                )
                paths, digests = write_artifacts(
                    root / "capture",
                    spec=spec,
                    **overrides,
                )
                store = self.initialized_store(root)
                with self.assertRaisesRegex(EvidenceIntegrityError, "identity fields missing"):
                    store.write_trial_bundle(
                        spec,
                        manifest(spec, digests),
                        evaluation(spec),
                        artifact_paths=paths,
                    )

    def test_terminal_manifest_must_bind_the_exact_safe_closure_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            spec = trial()
            paths, _ = write_artifacts(root / "capture", spec=spec)
            terminal = json.loads(paths.terminal_manifest_path.read_text())
            terminal["safe_closure_evidence"]["host_dwell_s"] = 0.75
            paths.terminal_manifest_path.write_text(
                json.dumps(terminal, sort_keys=True) + "\n", encoding="utf-8"
            )
            digests = {
                "csv": _sha256(paths.csv_path.read_bytes()),
                "metadata": _sha256(paths.metadata_path.read_bytes()),
                "terminal_manifest": _sha256(paths.terminal_manifest_path.read_bytes()),
            }
            store = self.initialized_store(root)
            with self.assertRaisesRegex(
                EvidenceIntegrityError, "safe_closure_evidence conflicts"
            ):
                store.write_trial_bundle(
                    spec,
                    manifest(spec, digests),
                    evaluation(spec),
                    artifact_paths=paths,
                )

    def test_artifact_identity_requires_type_exact_integer_fields(self) -> None:
        cases = (
            (
                "metadata candidate_token float",
                {"metadata_override": {"candidate_token": 101.0}},
            ),
            (
                "terminal reason boolean",
                {"terminal_override": {"terminal_reason": True}},
            ),
        )
        for label, overrides in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                spec = trial()
                identity = {
                    "trial_uid": spec.trial_uid,
                    "backend_id": spec.backend_id,
                    "candidate_token": spec.candidate_token,
                }
                metadata_payload = {
                    **identity,
                    **overrides.get("metadata_override", {}),
                }
                terminal_payload = {
                    **identity,
                    "terminal_reason": 1,
                    **overrides.get("terminal_override", {}),
                }
                paths, digests = write_artifacts(
                    root / "capture",
                    spec=spec,
                    metadata_bytes=(json.dumps(metadata_payload) + "\n").encode(),
                    terminal_bytes=(json.dumps(terminal_payload) + "\n").encode(),
                )
                store = self.initialized_store(root)
                with self.assertRaisesRegex(EvidenceIntegrityError, "conflicts"):
                    store.write_trial_bundle(
                        spec,
                        manifest(spec, digests),
                        evaluation(spec),
                        artifact_paths=paths,
                    )

    def test_symlink_artifact_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            spec = trial()
            paths, digests = write_artifacts(root / "capture", spec=spec)
            real_csv = paths.csv_path.with_name("real.csv")
            paths.csv_path.rename(real_csv)
            paths.csv_path.symlink_to(real_csv)
            store = self.initialized_store(root)
            with self.assertRaisesRegex(EvidenceIntegrityError, "missing or unsafe"):
                store.write_trial_bundle(
                    spec,
                    manifest(spec, digests),
                    evaluation(spec),
                    artifact_paths=paths,
                )

    def test_hardlinked_artifact_roles_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            spec = trial()
            paths, digests = write_artifacts(root / "capture", spec=spec)
            paths.metadata_path.unlink()
            paths.metadata_path.hardlink_to(paths.csv_path)
            digests["metadata"] = digests["csv"]
            store = self.initialized_store(root)
            with self.assertRaisesRegex(EvidenceIntegrityError, "distinct files"):
                store.write_trial_bundle(
                    spec,
                    manifest(spec, digests),
                    evaluation(spec),
                    artifact_paths=paths,
                )

    def test_same_trial_is_idempotent_across_provenance_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            spec = trial()
            first_paths, first_digests = write_artifacts(root / "capture-a", spec=spec)
            second_paths, second_digests = write_artifacts(root / "capture-b", spec=spec)
            self.assertEqual(first_digests, second_digests)
            store = self.initialized_store(root)
            first_bundle = store.write_trial_bundle(
                spec,
                manifest(spec, first_digests),
                evaluation(spec),
                artifact_paths=first_paths,
            )
            second_bundle = store.write_trial_bundle(
                spec,
                manifest(spec, second_digests),
                evaluation(spec),
                artifact_paths=second_paths,
            )
            self.assertEqual(first_bundle, second_bundle)
            rows = store.read_history()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["trial_uid"], spec.trial_uid)

    def test_same_csv_is_deduped_cross_trial_even_without_index_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            first = trial()
            second = trial(trial_id=2, token=202)
            first_paths, first_digests = write_artifacts(root / "capture-a", spec=first)
            second_paths, second_digests = write_artifacts(
                root / "capture-b",
                spec=second,
            )
            self.assertEqual(first_digests["csv"], second_digests["csv"])
            store = self.initialized_store(root)
            store.write_trial_bundle(
                first,
                manifest(first, first_digests),
                evaluation(first),
                artifact_paths=first_paths,
            )
            store.index_path.unlink()
            with self.assertRaisesRegex(DuplicateTrialError, "physical capture"):
                store.write_trial_bundle(
                    second,
                    manifest(second, second_digests),
                    evaluation(second),
                    artifact_paths=second_paths,
                )
            self.assertEqual(len(store.read_history()), 1)

    def test_orphan_bundle_reserves_capture_after_history_append_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            first = trial()
            first_paths, first_digests = write_artifacts(root / "capture-a", spec=first)
            store = self.initialized_store(root)
            with mock.patch.object(
                store,
                "_append_history_locked",
                side_effect=RuntimeError("synthetic append failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "synthetic append failure"):
                    store.write_trial_bundle(
                        first,
                        manifest(first, first_digests),
                        evaluation(first),
                        artifact_paths=first_paths,
                    )
            orphan_bundle = store.root / "trials" / first.trial_uid / "immutable_trial_bundle.json"
            self.assertTrue(orphan_bundle.is_file())

            second = trial(trial_id=2, token=202)
            second_paths, second_digests = write_artifacts(root / "capture-b", spec=second)
            with self.assertRaisesRegex(DuplicateTrialError, "reserved"):
                store.write_trial_bundle(
                    second,
                    manifest(second, second_digests),
                    evaluation(second),
                    artifact_paths=second_paths,
                )

    def test_capture_evidence_is_bound_into_history_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            spec = trial()
            paths, digests = write_artifacts(root / "capture", spec=spec)
            store = self.initialized_store(root)
            bundle_path = store.write_trial_bundle(
                spec,
                replace(manifest(spec, digests), evidence={"lag_ms": 11.9}),
                evaluation(spec),
                artifact_paths=paths,
            )
            bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
            bundle["capture"]["evidence"]["lag_ms"] = 0.0
            bundle_path.write_text(json.dumps(bundle) + "\n", encoding="utf-8")
            with self.assertRaises(EvidenceIntegrityError):
                store.read_promotion_history()

    def test_promotion_excludes_verified_ineligible_infra_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            spec = trial()
            paths, digests = write_artifacts(
                root / "capture",
                spec=spec,
                terminal_reason=12,
            )
            capture = replace(manifest(spec, digests), terminal_reason=12)
            ineligible = Evaluation(
                trial_uid=spec.trial_uid,
                backend_id=spec.backend_id,
                eligible=False,
                disposition=TrialDisposition.WAIT_INFRA_READY,
                objective_mae_n=None,
                force_bias_n=None,
                force_std_n=None,
                coverage_12_plus_minus_1_ratio=None,
                complete_bins=0,
                safe_closure=True,
            )
            store = self.initialized_store(root)
            store.write_trial_bundle(
                spec,
                capture,
                ineligible,
                artifact_paths=paths,
            )
            self.assertEqual(len(store.read_history()), 1)
            self.assertEqual(len(store.read_resume_history()), 1)
            self.assertEqual(store.read_promotion_history(), [])

    def test_promotion_fails_closed_when_any_history_row_is_malformed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            spec = trial()
            paths, digests = write_artifacts(root / "capture", spec=spec)
            store = self.initialized_store(root)
            store.write_trial_bundle(
                spec,
                manifest(spec, digests),
                evaluation(spec),
                artifact_paths=paths,
            )
            with store.history_path.open("a", encoding="utf-8") as handle:
                handle.write('{"overflow":1e400}\n')
            with self.assertRaisesRegex(EvidenceIntegrityError, "quarantined evidence"):
                store.read_promotion_history()

    def test_promotion_reapplies_current_semantics_to_legacy_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            spec = trial()
            paths, digests = write_artifacts(
                root / "capture",
                spec=spec,
                metadata_bytes=b'{"schema_version":"legacy-metadata/v0"}\n',
                terminal_bytes=b'{"schema_version":"legacy-terminal/v0"}\n',
            )
            store = self.initialized_store(root)
            with mock.patch.object(store, "_verify_artifact_semantics"):
                store.write_trial_bundle(
                    spec,
                    manifest(spec, digests),
                    evaluation(spec),
                    artifact_paths=paths,
                )
            with self.assertRaisesRegex(EvidenceIntegrityError, "quarantined evidence"):
                store.read_promotion_history()

    def test_atomic_history_rewrite_recovers_complete_row_without_final_newline(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            first = trial()
            first_paths, first_digests = write_artifacts(root / "capture-a", spec=first)
            store = self.initialized_store(root)
            store.write_trial_bundle(
                first,
                manifest(first, first_digests),
                evaluation(first),
                artifact_paths=first_paths,
            )
            store.history_path.write_bytes(store.history_path.read_bytes().rstrip(b"\n"))

            second = trial(trial_id=2, token=202)
            second_paths, second_digests = write_artifacts(
                root / "capture-b",
                spec=second,
                csv_bytes=b"stage,path_time_s,force_b_z_n\n25,5.05,12.2\n",
            )
            store.write_trial_bundle(
                second,
                manifest(second, second_digests),
                evaluation(second),
                artifact_paths=second_paths,
            )
            self.assertTrue(store.history_path.read_bytes().endswith(b"\n"))
            self.assertEqual(len(store.read_promotion_history()), 2)

    def test_promotion_read_rehashes_current_bytes_and_quarantines_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            spec = trial()
            paths, digests = write_artifacts(root / "capture", spec=spec)
            store = self.initialized_store(root)
            store.write_trial_bundle(
                spec,
                manifest(spec, digests),
                evaluation(spec),
                artifact_paths=paths,
            )
            # Source paths are no longer mutable provenance after commit: the
            # bundle points at campaign-owned retained byte snapshots.
            paths.terminal_manifest_path.write_bytes(b'{"reason":2}\n')
            self.assertEqual(len(store.read_promotion_history()), 1)
            row = store.read_history()[0]
            retained_terminal = Path(row["artifact_provenance"]["terminal_manifest"]["path"])
            retained_terminal.chmod(0o644)
            retained_terminal.write_bytes(b'{"reason":2}\n')
            with self.assertRaises(EvidenceIntegrityError):
                store.read_promotion_history()
            quarantines = list(store.quarantine_dir.glob("*.json"))
            self.assertEqual(len(quarantines), 1)
            with self.assertRaises(EvidenceIntegrityError):
                store.read_promotion_history()
            self.assertEqual(len(list(store.quarantine_dir.glob("*.json"))), 1)

    def test_promotion_requires_the_immutable_bundle_and_next_write_stops(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            first = trial()
            first_paths, first_digests = write_artifacts(root / "capture-a", spec=first)
            store = self.initialized_store(root)
            bundle_path = store.write_trial_bundle(
                first,
                manifest(first, first_digests),
                evaluation(first),
                artifact_paths=first_paths,
            )
            bundle_path.unlink()
            with self.assertRaises(EvidenceIntegrityError):
                store.read_promotion_history()
            second = trial(trial_id=2, token=202)
            second_paths, second_digests = write_artifacts(
                root / "capture-b",
                spec=second,
                csv_bytes=b"stage,path_time_s,force_b_z_n\n25,5.05,12.1\n",
            )
            with self.assertRaisesRegex(EvidenceIntegrityError, "quarantined evidence"):
                store.write_trial_bundle(
                    second,
                    manifest(second, second_digests),
                    evaluation(second),
                    artifact_paths=second_paths,
                )

    def test_history_identity_and_objective_invariants_are_revalidated(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            spec = trial()
            paths, digests = write_artifacts(root / "capture", spec=spec)
            store = self.initialized_store(root)
            store.write_trial_bundle(
                spec,
                manifest(spec, digests),
                evaluation(spec),
                artifact_paths=paths,
            )
            row = json.loads(store.history_path.read_text(encoding="utf-8"))
            row["evaluation"]["objective_mae_n"] = -1.0
            store.history_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            self.assertEqual(store.read_history(), [])
            self.assertEqual(len(list(store.quarantine_dir.glob("*.json"))), 1)

    def test_history_identity_binds_evaluation_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            spec = trial()
            paths, digests = write_artifacts(root / "capture", spec=spec)
            store = self.initialized_store(root)
            store.write_trial_bundle(
                spec,
                manifest(spec, digests),
                evaluation(spec),
                artifact_paths=paths,
            )
            row = json.loads(store.history_path.read_text(encoding="utf-8"))
            row["evaluation"]["metrics"] = {"governor_burden": 0.5}
            store.history_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            with self.assertRaises(EvidenceIntegrityError):
                store.read_promotion_history()
            self.assertEqual(len(list(store.quarantine_dir.glob("*.json"))), 1)

    def test_nan_and_duplicate_json_keys_are_durably_quarantined(self) -> None:
        malformed_rows = (
            '{"objective":NaN}\n',
            '{"trial_uid":"a","trial_uid":"b"}\n',
        )
        for raw_line in malformed_rows:
            with self.subTest(raw_line=raw_line), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                store = self.initialized_store(root)
                store.history_path.write_text(raw_line, encoding="utf-8")
                self.assertEqual(store.read_history(), [])
                quarantines = list(store.quarantine_dir.glob("*.json"))
                self.assertEqual(len(quarantines), 1)
                payload = json.loads(quarantines[0].read_text(encoding="utf-8"))
                self.assertIs(payload["training_eligible"], False)
                self.assertIn("malformed_history", payload["reason"])

    def test_quarantine_and_history_write_failures_are_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = self.initialized_store(root)
            with mock.patch(
                "step5d_autotune_store._atomic_json",
                side_effect=OSError("synthetic quarantine failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "quarantine write failed"):
                    store.quarantine(
                        identity=SHA_A,
                        reason="synthetic",
                        evidence={"row": "bad"},
                    )
            with mock.patch(
                "step5d_autotune_store._atomic_jsonl",
                side_effect=OSError("synthetic history failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "history write failed"):
                    store._append_history_locked({"row": "synthetic"}, [])


if __name__ == "__main__":
    unittest.main()
