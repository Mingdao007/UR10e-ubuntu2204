from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from ur10e_artifact_store import (  # noqa: E402
    ArtifactStoreError,
    artifact_store,
    publish_artifact,
)
from step5d_workflow_state import (  # noqa: E402
    WorkflowStateError,
    assert_transition,
    classify_risk,
    resolve_artifacts,
    verify_current,
)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(
    root: Path, *, state: str = "controller_verified", transaction: bool | str = False,
) -> Path:
    store = root / "artifact-store"
    package_blobs = {
        ".script": b"script",
        ".txt": b"txt",
        ".urp": b"urp",
    }
    package_sha = {
        extension: hashlib.sha256(blob).hexdigest()
        for extension, blob in package_blobs.items()
    }
    readback_payload = {
        "status": "controller read-back verified",
        "delivery_mode": "full_upload_readback",
        "readback_source": "fresh_controller_get",
        "controller": "root@fixture-controller",
        "target_dir": "/programs/fixture",
        "validation": {
            "program": "p",
            "target_dir": "/programs/fixture",
            "script_node_path": "/programs/fixture/p.script",
            "script_sha256": package_sha[".script"],
            "txt_sha256": package_sha[".txt"],
            "urp_sha256": package_sha[".urp"],
        },
        "sha256": {
            "local": package_sha,
            "controller": package_sha,
            "readback": package_sha,
        },
    }
    transaction_token = (
        "1" * 32 if transaction is True else transaction if isinstance(transaction, str) else None
    )
    if transaction_token is not None:
        readback_payload["upload_transaction_id"] = transaction_token
    role_blobs = {
        "controller_readback_manifest": (
            json.dumps(readback_payload, sort_keys=True) + "\n"
        ).encode(),
        "controller_readback_script": package_blobs[".script"],
        "controller_readback_txt": package_blobs[".txt"],
        "controller_readback_urp": package_blobs[".urp"],
    }
    rows = []
    role_hashes = {}
    for role, blob in role_blobs.items():
        digest = hashlib.sha256(blob).hexdigest()
        object_path = store / "sha256" / digest
        object_path.parent.mkdir(parents=True, exist_ok=True)
        object_path.write_bytes(blob)
        rows.append({
            "logical_role": role,
            "sha256": digest,
            "size": len(blob),
            "store_key": f"sha256/{digest}",
        })
        role_hashes[role] = digest
    locator_path = root / "config/step5d/artifact_locators/step5d_v35_retained_inputs.json"
    _write_json(
        locator_path,
        {
            "schema_version": "ur10e_artifact_locator_v1",
            "program": "p",
            "artifacts": rows,
        },
    )
    candidate_path = root / "config/step5d/manifests/p/candidate.json"
    _write_json(candidate_path, {
        "schema_version": "step5d_candidate_manifest_v1",
        "program": "p",
        "state": "candidate",
        "decision_digest": "d" * 64,
        "package_sha256": package_sha,
    })
    controller_path = root / "config/step5d/manifests/p/controller.json"
    _write_json(controller_path, {
        "schema_version": "step5d_controller_verification_manifest_v1",
        "program": "p",
        "state": "controller_verified",
        "decision_digest": "d" * 64,
        "candidate_manifest": {"sha256": _sha(candidate_path)},
        "fresh_readback_verified": True,
        "controller_identity": "root@fixture-controller",
        "controller_target": "/programs/fixture/p.urp",
        "upload_transaction": ({"id": transaction_token} if transaction_token is not None else {}),
        "readback": {"artifact_roles": role_hashes},
    })
    manifests = {
        "candidate": {
            "path": str(candidate_path.relative_to(root)),
            "sha256": _sha(candidate_path),
        },
        "controller_verification": {
            "path": str(controller_path.relative_to(root)),
            "sha256": _sha(controller_path),
        },
        "live_ready": None,
    }
    if state == "candidate":
        manifests["controller_verification"] = None
    _write_json(root / "config/step5d/current.json", {
        "schema_version": "step5d_current_stage_v2",
        "program": "p",
        "state": state,
        "decision_digest": "d" * 64,
        "manifests": manifests,
        "artifact_locator": (
            None if state == "candidate" else {
                "path": str(locator_path.relative_to(root)),
                "sha256": _sha(locator_path),
            }
        ),
        "execution": {
            "live_authorized": False,
            "live_running": False,
        },
        "claims": {
            "new_version_complete": state != "candidate",
            "controller_verified": state != "candidate",
            "live_ready": False,
            "live_accepted": False,
        },
    })
    return store


class Step5dWorkflowStateTest(unittest.TestCase):
    def test_default_store_is_in_git_common_dir_with_explicit_override(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            self.assertEqual(artifact_store(root), (root / ".git/ur10e-artifacts").resolve())
            override = root / "explicit-store"
            self.assertEqual(artifact_store(root, override), override.resolve())
            with mock.patch.dict("os.environ", {"UR10E_ARTIFACT_STORE": str(root / "env-store")}):
                self.assertEqual(artifact_store(root), (root / "env-store").resolve())

    def test_default_store_uses_common_dir_from_nested_linked_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fixture = Path(td)
            repository = fixture / "repository"
            linked = fixture / "linked"
            repository.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=repository,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "UR10e test"],
                cwd=repository,
                check=True,
            )
            (repository / "tracked.txt").write_text("fixture\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repository, check=True)
            subprocess.run(
                ["git", "worktree", "add", "-q", "--detach", str(linked)],
                cwd=repository,
                check=True,
            )
            nested = linked / "experiments/tase-contact-reproduction"
            nested.mkdir(parents=True)

            with mock.patch.dict("os.environ", {"UR10E_ARTIFACT_STORE": ""}):
                self.assertEqual(
                    artifact_store(nested),
                    (repository / ".git/ur10e-artifacts").resolve(),
                )

    def test_artifact_ref_sha_is_authoritative_over_store_key(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = _fixture(root)
            locator = root / "config/step5d/artifact_locators/step5d_v35_retained_inputs.json"
            payload = json.loads(locator.read_text())
            payload["artifacts"][0]["store_key"] = f"sha256/{'0' * 64}"
            _write_json(locator, payload)
            with self.assertRaisesRegex(WorkflowStateError, "derived from sha256"):
                resolve_artifacts(root=root, locator_path=locator, store=store)

    def test_concurrent_artifact_publish_is_atomic_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source.bin"
            source.write_bytes((b"artifact-payload" * 4096) + b"end")
            store = root / "store"
            with ThreadPoolExecutor(max_workers=8) as pool:
                refs = list(pool.map(lambda _: publish_artifact(source, store=store), range(24)))
            self.assertTrue(all(ref == refs[0] for ref in refs))
            destination = store / refs[0].store_key
            self.assertEqual(destination.read_bytes(), source.read_bytes())
            self.assertEqual(list((store / "sha256").glob("*.tmp")), [])
            destination.chmod(0o644)
            destination.write_bytes(b"tampered")
            with self.assertRaises(ArtifactStoreError):
                publish_artifact(source, store=store)

    def test_v35_tracked_locator_resolves_from_content_addressed_store(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = _fixture(root)
            resolved = resolve_artifacts(root=root, store=store)
            manifest = json.loads(resolved["controller_readback_manifest"].read_text())
            self.assertEqual(manifest["status"], "controller read-back verified")

    def test_controller_verified_is_not_live_ready_or_live_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = _fixture(root)
            result = verify_current(root=root, store=store)
            self.assertTrue(result["controller_verified"])
            self.assertFalse(result["live_ready"])
            self.assertFalse(result["live_accepted"])
            self.assertFalse(result["live_authorized"])
            self.assertFalse(result["live_running"])

    def test_evidence_maturity_and_authorization_are_orthogonal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = _fixture(root)
            current_path = root / "config/step5d/current.json"
            current = json.loads(current_path.read_text())
            current["execution"]["live_authorized"] = True
            _write_json(current_path, current)
            result = verify_current(root=root, store=store)
            self.assertTrue(result["controller_verified"])
            self.assertFalse(result["live_ready"])
            self.assertTrue(result["live_authorized"])
            self.assertFalse(result["live_running"])

            current["execution"]["live_running"] = True
            _write_json(current_path, current)
            with self.assertRaisesRegex(WorkflowStateError, "live-ready evidence"):
                verify_current(root=root, store=store)

    def test_explicit_no_upload_shape_stays_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = _fixture(root, state="candidate")
            result = verify_current(root=root, store=store)
            self.assertEqual(result["state"], "candidate")
            self.assertFalse(result["controller_verified"])

    def test_hash_tamper_decision_drift_and_readback_mismatch_fail_closed(self) -> None:
        mutations = {
            "pointer": lambda root: (root / "config/step5d/manifests/p/candidate.json").write_text("{}\n"),
            "decision": lambda root: _mutate(root, "decision_digest", "x" * 64),
            "readback": lambda root: _mutate_controller(root, "fresh_readback_verified", False),
            "claims": lambda root: _mutate(root, "claims", {}),
            "manifest_state": lambda root: _mutate_controller(root, "state", "candidate"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                store = _fixture(root)
                mutate(root)
                with self.assertRaises(WorkflowStateError):
                    verify_current(root=root, store=store)

    def test_controller_verified_requires_complete_artifact_roles(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = _fixture(root)
            locator_path = root / "config/step5d/artifact_locators/step5d_v35_retained_inputs.json"
            locator = json.loads(locator_path.read_text())
            locator["artifacts"] = [
                row for row in locator["artifacts"]
                if row["logical_role"] != "controller_readback_urp"
            ]
            _write_json(locator_path, locator)
            current_path = root / "config/step5d/current.json"
            current = json.loads(current_path.read_text())
            current["artifact_locator"]["sha256"] = _sha(locator_path)
            _write_json(current_path, current)
            with self.assertRaisesRegex(WorkflowStateError, "artifact closure is incomplete"):
                verify_current(root=root, store=store)

    def test_artifact_locator_program_must_match_current_program(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = _fixture(root)
            locator_path = root / "config/step5d/artifact_locators/step5d_v35_retained_inputs.json"
            locator = json.loads(locator_path.read_text())
            locator["program"] = "different-program"
            _write_json(locator_path, locator)
            current_path = root / "config/step5d/current.json"
            current = json.loads(current_path.read_text())
            current["artifact_locator"]["sha256"] = _sha(locator_path)
            _write_json(current_path, current)
            with self.assertRaisesRegex(WorkflowStateError, "locator program identity"):
                verify_current(root=root, store=store)

    def test_locator_and_controller_cannot_rebind_triplet_away_from_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = _fixture(root)
            replacement = b"different-script-bytes"
            replacement_sha = hashlib.sha256(replacement).hexdigest()
            replacement_path = store / "sha256" / replacement_sha
            replacement_path.write_bytes(replacement)

            locator_path = root / "config/step5d/artifact_locators/step5d_v35_retained_inputs.json"
            locator = json.loads(locator_path.read_text())
            script_row = next(
                row for row in locator["artifacts"]
                if row["logical_role"] == "controller_readback_script"
            )
            script_row.update({
                "sha256": replacement_sha,
                "size": len(replacement),
                "store_key": f"sha256/{replacement_sha}",
            })
            _write_json(locator_path, locator)

            controller_path = root / "config/step5d/manifests/p/controller.json"
            controller = json.loads(controller_path.read_text())
            controller["readback"]["artifact_roles"]["controller_readback_script"] = replacement_sha
            _write_json(controller_path, controller)

            current_path = root / "config/step5d/current.json"
            current = json.loads(current_path.read_text())
            current["artifact_locator"]["sha256"] = _sha(locator_path)
            current["manifests"]["controller_verification"]["sha256"] = _sha(controller_path)
            _write_json(current_path, current)
            with self.assertRaisesRegex(WorkflowStateError, "differs from candidate package"):
                verify_current(root=root, store=store)

    def test_artifact_store_tamper_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = _fixture(root)
            next((store / "sha256").iterdir()).write_bytes(b"tampered")
            with self.assertRaisesRegex(WorkflowStateError, "artifact size mismatch"):
                verify_current(root=root, store=store)

    def test_durable_verifier_binds_new_upload_transaction_identity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = _fixture(root, transaction=True)
            controller_path = root / "config/step5d/manifests/p/controller.json"
            controller = json.loads(controller_path.read_text())
            controller["upload_transaction"]["id"] = "2" * 32
            _write_json(controller_path, controller)
            current_path = root / "config/step5d/current.json"
            current = json.loads(current_path.read_text())
            current["manifests"]["controller_verification"]["sha256"] = _sha(controller_path)
            _write_json(current_path, current)
            with self.assertRaisesRegex(WorkflowStateError, "transaction identity"):
                verify_current(root=root, store=store)

    def test_durable_verifier_rejects_matching_but_malformed_transaction_ids(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = _fixture(root, transaction="x")
            with self.assertRaisesRegex(WorkflowStateError, "transaction identity"):
                verify_current(root=root, store=store)

    def test_deterministic_risk_classifier(self) -> None:
        low = classify_risk(changed_contracts=["reporting"], owners=["bridge"])
        high = classify_risk(changed_contracts=["force_frame"], owners=["bridge"])
        unknown = classify_risk(changed_contracts=[], owners=["bridge"], impact_known=False)
        self.assertEqual(low["review_gate"], "0+0")
        self.assertEqual(high["review_gate"], "1+1_once_or_1+0_unavailable")
        self.assertEqual(unknown["risk_class"], "high")
        self.assertEqual(high["remediation_gate"], "deterministic_closure_only")

    def test_state_machine_rejects_skips(self) -> None:
        assert_transition("candidate", "controller_verified")
        assert_transition("controller_verified", "live_ready")
        with self.assertRaises(WorkflowStateError):
            assert_transition("candidate", "live_ready")

    def test_future_live_states_fail_closed_until_evidence_schemas_exist(self) -> None:
        for state in ("live_ready", "live_accepted"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                store = _fixture(root)
                current_path = root / "config/step5d/current.json"
                current = json.loads(current_path.read_text())
                current["state"] = state
                current["claims"].update({
                    "new_version_complete": True,
                    "controller_verified": True,
                    "live_ready": True,
                    "live_accepted": state == "live_accepted",
                })
                _write_json(current_path, current)
                with self.assertRaisesRegex(WorkflowStateError, "not implemented"):
                    verify_current(root=root, store=store)


def _mutate(root: Path, key: str, value: object) -> None:
    path = root / "config/step5d/current.json"
    payload = json.loads(path.read_text())
    payload[key] = value
    _write_json(path, payload)


def _mutate_controller(root: Path, key: str, value: object) -> None:
    controller_path = root / "config/step5d/manifests/p/controller.json"
    payload = json.loads(controller_path.read_text())
    payload[key] = value
    _write_json(controller_path, payload)
    current_path = root / "config/step5d/current.json"
    current = json.loads(current_path.read_text())
    current["manifests"]["controller_verification"]["sha256"] = _sha(controller_path)
    _write_json(current_path, current)


if __name__ == "__main__":
    unittest.main()
