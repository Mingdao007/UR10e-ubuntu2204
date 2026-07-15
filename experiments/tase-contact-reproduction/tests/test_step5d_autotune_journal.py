#!/usr/bin/env python3
"""Offline durability and recovery tests for the Step5d autotune journal."""

from __future__ import annotations

import json
import multiprocessing
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import step5d_autotune_journal as journal_module  # noqa: E402
from step5d_autotune_journal import (  # noqa: E402
    CampaignIdentity,
    GovernorProbe,
    HighWaterMarks,
    JournalConflictError,
    JournalIntegrityError,
    JournalReference,
    JournalState,
    PendingAck,
    PendingRetry,
    ReconcileAction,
    SupervisorJournal,
    TpSnapshot,
    TrialCursor,
    reconcile_tp_snapshot,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
CANDIDATE_UID = "d" * 64
TRIAL_UID = "e" * 64
BUNDLE_UID = "f" * 64


def campaign() -> CampaignIdentity:
    return CampaignIdentity(
        campaign_id="step5d-native-journal-fixture",
        campaign_epoch=7,
        campaign_fingerprint=SHA_A,
        backend_id="step5d_v35_native",
        source_fingerprint=SHA_B,
        config_fingerprint=SHA_C,
    )


def cursor() -> TrialCursor:
    return TrialCursor(
        trial_uid=TRIAL_UID,
        candidate_uid=CANDIDATE_UID,
        trial_id=1,
        candidate_token=1,
        arm_command_seq=1,
        execution_profile_integer_id=111,
        trial_spec=JournalReference(
            reference_id=TRIAL_UID,
            path="/tmp/step5d-autotune-journal-fixture/trial_spec.json",
            sha256="4" * 64,
        ),
    )


def reference(root: Path, *, reference_id: str = BUNDLE_UID) -> JournalReference:
    return JournalReference(
        reference_id=reference_id,
        path=str(root / reference_id / "immutable_trial_bundle.json"),
        sha256="1" * 64,
    )


def home_state(**updates) -> JournalState:
    payload = {
        "campaign": campaign(),
        "phase": "home",
        "high_water": HighWaterMarks(),
        "candidate_tokens": {},
        "plant_epoch": 1,
        "execution_profile_id": "nf010-slew010-a010",
        "execution_profile_integer_id": 111,
    }
    payload.update(updates)
    return JournalState(**payload)


def active_state(**updates) -> JournalState:
    payload = {
        "campaign": campaign(),
        "phase": "trial_active",
        "high_water": HighWaterMarks(trial_id=1, command_seq=1, candidate_token=1),
        "candidate_tokens": {CANDIDATE_UID: 1},
        "plant_epoch": 1,
        "execution_profile_id": "nf010-slew010-a010",
        "execution_profile_integer_id": 111,
        "active_trial": cursor(),
    }
    payload.update(updates)
    return JournalState(**payload)


def wait_ack_state(root: Path, **updates) -> JournalState:
    payload = {
        "campaign": campaign(),
        "phase": "wait_ack",
        "high_water": HighWaterMarks(trial_id=1, command_seq=2, candidate_token=1),
        "candidate_tokens": {CANDIDATE_UID: 1},
        "plant_epoch": 1,
        "execution_profile_id": "nf010-slew010-a010",
        "execution_profile_integer_id": 111,
        "pending_ack": PendingAck(
            trial=cursor(),
            ack_command_seq=2,
            immutable_bundle=reference(root),
            post_ack_phase="home",
        ),
    }
    payload.update(updates)
    return JournalState(**payload)


def wait_infra_state(**updates) -> JournalState:
    payload = {
        "campaign": campaign(),
        "phase": "wait_infra_ready",
        "high_water": HighWaterMarks(trial_id=1, command_seq=2, candidate_token=1),
        "candidate_tokens": {CANDIDATE_UID: 1},
        "plant_epoch": 1,
        "execution_profile_id": "nf010-slew010-a010",
        "execution_profile_integer_id": 111,
        "pending_retry": PendingRetry(
            origin_trial=cursor(),
            kind="infrastructure",
            last_consumed_command_seq=2,
        ),
    }
    payload.update(updates)
    return JournalState(**payload)


def tp_snapshot(
    state: str,
    *,
    consumed: int,
    trial_id: int = 1,
    token: int = 1,
    epoch: int = 7,
    profile_id: int = 111,
) -> TpSnapshot:
    return TpSnapshot(
        campaign_epoch_echo=epoch,
        trial_id_echo=trial_id,
        state=state,
        candidate_token_echo=token,
        terminal_reason=1,
        execution_profile_integer_id_echo=profile_id,
        consumed_command_seq=consumed,
    )


def _concurrent_append(path: str, queue) -> None:
    try:
        SupervisorJournal(Path(path)).append(home_state())
    except BaseException as exc:  # pragma: no cover - reported to parent process
        queue.put(f"{type(exc).__name__}: {exc}")
    else:
        queue.put(None)


class JournalDurabilityTest(unittest.TestCase):
    def test_round_trip_hash_chain_and_complete_supervisor_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = SupervisorJournal(root / "journal")
            first = store.append(home_state(), expected_revision=0)
            observation = reference(root, reference_id="2" * 64)
            history = reference(root, reference_id="3" * 64)
            second_state = active_state(
                cooldown_remaining=3,
                observation_references=(observation,),
                history_references=(history,),
            )
            second = store.append(second_state, expected_revision=1)
            loaded = store.load_latest(expected_campaign=campaign())
            self.assertEqual(first.revision, 1)
            self.assertEqual(second.revision, 2)
            self.assertEqual(second.previous_record_sha256, first.record_sha256)
            self.assertEqual(loaded, second)
            self.assertEqual(loaded.state.cooldown_remaining, 3)
            self.assertEqual(dict(loaded.state.candidate_tokens), {CANDIDATE_UID: 1})
            self.assertEqual(loaded.state.observation_references, (observation,))
            self.assertEqual(loaded.state.history_references, (history,))

    def test_governor_probe_and_profile_change_require_new_plant_epoch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = SupervisorJournal(Path(td) / "journal")
            probe = GovernorProbe(
                layer="normal_filter_rate",
                profile_before_id="nf010-slew010-a010",
                profile_after_id="nf015-slew010-a010",
                stage="b",
                force_candidate_uid=CANDIDATE_UID,
                plant_epoch=1,
                trial_a_uid="8" * 64,
            )
            retained = {
                "high_water": HighWaterMarks(1, 2, 1),
                "candidate_tokens": {CANDIDATE_UID: 1},
            }
            store.append(
                home_state(
                    **retained,
                    governor_probe=probe,
                )
            )
            with self.assertRaisesRegex(JournalConflictError, "new plant epoch"):
                store.append(
                    home_state(
                        **retained,
                        execution_profile_id="nf015-slew010-a010",
                        execution_profile_integer_id=211,
                    )
                )
            kept = store.append(
                home_state(
                    **retained,
                    plant_epoch=2,
                    execution_profile_id="nf015-slew010-a010",
                    execution_profile_integer_id=211,
                )
            )
            self.assertEqual(kept.state.plant_epoch, 2)

    def test_high_water_and_candidate_token_mapping_never_regress(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = SupervisorJournal(Path(td) / "journal")
            store.append(active_state())
            with self.assertRaisesRegex(JournalConflictError, "high-water regressed"):
                store.append(home_state())
            with self.assertRaisesRegex(ValueError, "reuses a token"):
                active_state(
                    high_water=HighWaterMarks(1, 1, 2),
                    candidate_tokens={CANDIDATE_UID: 1, "9" * 64: 1},
                )

    def test_stale_expected_revision_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = SupervisorJournal(Path(td) / "journal")
            store.append(home_state())
            with self.assertRaisesRegex(JournalConflictError, "stale journal revision"):
                store.append(home_state(), expected_revision=0)

    def test_fsync_is_used_and_atomic_publish_leaves_no_temporary_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = SupervisorJournal(Path(td) / "journal")
            with mock.patch.object(
                journal_module.os, "fsync", wraps=os.fsync
            ) as fsync:
                store.append(home_state())
            self.assertGreaterEqual(fsync.call_count, 5)
            names = {path.name for path in store.root.rglob("*")}
            self.assertTrue(all(not name.endswith(".tmp") for name in names))
            self.assertIn("latest.json", names)
            self.assertIn("00000000000000000001.json", names)

    def test_process_lock_serializes_concurrent_writers(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "journal")
            SupervisorJournal(Path(path)).append(home_state())
            context = multiprocessing.get_context("fork")
            queue = context.Queue()
            workers = [
                context.Process(target=_concurrent_append, args=(path, queue))
                for _ in range(2)
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=10)
                self.assertFalse(worker.is_alive())
                self.assertEqual(worker.exitcode, 0)
            self.assertEqual([queue.get(timeout=2) for _ in workers], [None, None])
            self.assertEqual(SupervisorJournal(Path(path)).load_latest().revision, 3)

    def test_missing_or_orphan_journal_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = SupervisorJournal(Path(td) / "journal")
            with self.assertRaisesRegex(JournalIntegrityError, "missing"):
                store.load_latest()
            store.append(home_state())
            orphan = store.entries_dir / "00000000000000000002.json"
            orphan.write_bytes(
                (store.entries_dir / "00000000000000000001.json").read_bytes()
            )
            with self.assertRaisesRegex(JournalIntegrityError, "filename/revision"):
                store.load_latest()

    def test_single_valid_orphan_repairs_head_after_publish_crash(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = SupervisorJournal(Path(td) / "journal")
            first = store.append(home_state())
            original_publish = journal_module._atomic_publish

            def crash_before_head(path, encoded, *, replace):
                if path == store.head_path:
                    raise OSError("simulated crash before head publish")
                return original_publish(path, encoded, replace=replace)

            with mock.patch.object(
                journal_module,
                "_atomic_publish",
                side_effect=crash_before_head,
            ):
                with self.assertRaisesRegex(OSError, "simulated crash"):
                    store.append(home_state(), expected_revision=first.revision)

            self.assertEqual(json.loads(store.head_path.read_text())["revision"], 1)
            repaired = store.load_latest()
            self.assertEqual(repaired.revision, 2)
            repaired_head = json.loads(store.head_path.read_text())
            self.assertEqual(repaired_head["revision"], 2)
            self.assertEqual(repaired_head["record_sha256"], repaired.record_sha256)

    def test_multiple_orphans_remain_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = SupervisorJournal(Path(td) / "journal")
            first = store.append(home_state())
            original_publish = journal_module._atomic_publish

            def crash_before_head(path, encoded, *, replace):
                if path == store.head_path:
                    raise OSError("simulated crash before head publish")
                return original_publish(path, encoded, replace=replace)

            with mock.patch.object(
                journal_module,
                "_atomic_publish",
                side_effect=crash_before_head,
            ):
                with self.assertRaises(OSError):
                    store.append(home_state(), expected_revision=first.revision)
            second_payload = json.loads(
                (store.entries_dir / "00000000000000000002.json").read_text()
            )
            third_envelope = {
                key: value
                for key, value in second_payload.items()
                if key != "record_sha256"
            }
            third_envelope["revision"] = 3
            third_envelope["previous_record_sha256"] = second_payload[
                "record_sha256"
            ]
            third_digest = journal_module._sha(
                journal_module._canonical(third_envelope)
            )
            third = {**third_envelope, "record_sha256": third_digest}
            (store.entries_dir / "00000000000000000003.json").write_bytes(
                journal_module._canonical(third)
            )
            with self.assertRaisesRegex(JournalIntegrityError, "multiple orphans"):
                store.load_latest()


class JournalStrictInputTest(unittest.TestCase):
    @staticmethod
    def _canonical(payload: dict) -> bytes:
        return (
            json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode()

    def test_record_tamper_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = SupervisorJournal(Path(td) / "journal")
            store.append(home_state())
            record = store.entries_dir / "00000000000000000001.json"
            payload = json.loads(record.read_text())
            payload["state"]["cooldown_remaining"] = 1
            # The key is nested under governor; adding it also proves unknown
            # fields fail before an optimizer can trust a repaired digest.
            record.write_bytes(self._canonical(payload))
            with self.assertRaises(JournalIntegrityError):
                store.load_latest()

    def test_duplicate_json_keys_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = SupervisorJournal(Path(td) / "journal")
            store.append(home_state())
            original = store.head_path.read_text().rstrip("\n}")
            store.head_path.write_text(original + ',"revision":1}\n')
            with self.assertRaisesRegex(JournalIntegrityError, "duplicate JSON"):
                store.load_latest()

    def test_nonfinite_json_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = SupervisorJournal(Path(td) / "journal")
            store.append(home_state())
            encoded = store.head_path.read_text().replace('"revision":1', '"revision":NaN')
            store.head_path.write_text(encoded)
            with self.assertRaisesRegex(JournalIntegrityError, "non-finite"):
                store.load_latest()

    def test_journal_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = SupervisorJournal(root / "journal")
            store.append(home_state())
            target = root / "head-copy.json"
            target.write_bytes(store.head_path.read_bytes())
            store.head_path.unlink()
            store.head_path.symlink_to(target)
            with self.assertRaisesRegex(JournalIntegrityError, "links"):
                store.load_latest()

    def test_reference_and_state_contracts_are_strict(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(ValueError, "normalized and absolute"):
                JournalReference(BUNDLE_UID, "relative/path", "1" * 64)
            with self.assertRaisesRegex(ValueError, "wait_ack"):
                home_state(phase="wait_ack")
            with self.assertRaisesRegex(ValueError, "unlimited"):
                PendingRetry(cursor(), "evidence", 1, unlimited=False)


class TpRecoveryReconcileTest(unittest.TestCase):
    def _entry(self, root: Path, state: JournalState):
        return SupervisorJournal(root / "journal").append(state)

    def test_ready_home_resumes_only_from_durable_home(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            entry = self._entry(root, home_state())
            decision = reconcile_tp_snapshot(
                entry,
                tp_snapshot(
                    "READY_HOME",
                    consumed=0,
                    trial_id=0,
                    token=0,
                    epoch=0,
                    profile_id=0,
                ),
            )
            self.assertEqual(decision.action, ReconcileAction.RESUME_HOME)
            self.assertFalse(decision.fail_closed)

    def test_ready_home_can_send_only_a_previously_persisted_arm(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            entry = self._entry(Path(td), active_state())
            decision = reconcile_tp_snapshot(
                entry,
                tp_snapshot(
                    "READY_HOME",
                    consumed=0,
                    trial_id=0,
                    token=0,
                    epoch=0,
                    profile_id=0,
                ),
            )
            self.assertEqual(decision.action, ReconcileAction.SEND_PERSISTED_ARM)
            self.assertEqual(decision.command_seq, 1)
            self.assertTrue(decision.command_permitted)

    def test_arm_before_persist_ambiguity_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            entry = self._entry(Path(td), home_state())
            decision = reconcile_tp_snapshot(entry, tp_snapshot("ARMED", consumed=1))
            self.assertTrue(decision.fail_closed)
            self.assertIn("before_durable_active_persist", decision.reason)

    def test_wait_ack_can_send_only_the_persisted_ack_after_bundle_reference(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            entry = self._entry(root, wait_ack_state(root))
            decision = reconcile_tp_snapshot(entry, tp_snapshot("WAIT_ACK", consumed=1))
            self.assertEqual(decision.action, ReconcileAction.SEND_PERSISTED_ACK)
            self.assertEqual(decision.command_seq, 2)
            self.assertTrue(decision.command_permitted)

    def test_ack_before_post_ack_persist_ambiguity_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            entry = self._entry(root, wait_ack_state(root))
            ready = reconcile_tp_snapshot(entry, tp_snapshot("READY_HOME", consumed=2))
            still_waiting = reconcile_tp_snapshot(
                entry, tp_snapshot("WAIT_ACK", consumed=2)
            )
            self.assertTrue(ready.fail_closed)
            self.assertIn("ack_before_post_ack_persist", ready.reason)
            self.assertTrue(still_waiting.fail_closed)
            self.assertIn("without_durable_post_ack", still_waiting.reason)

    def test_wait_infra_is_held_without_automatic_retry_command(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            entry = self._entry(Path(td), wait_infra_state())
            decision = reconcile_tp_snapshot(
                entry, tp_snapshot("WAIT_INFRA_READY", consumed=2)
            )
            self.assertEqual(decision.action, ReconcileAction.HOLD_WAIT_INFRA)
            self.assertFalse(decision.command_permitted)

    def test_fault_always_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            entry = self._entry(Path(td), active_state())
            decision = reconcile_tp_snapshot(entry, tp_snapshot("FAULT", consumed=1))
            self.assertTrue(decision.fail_closed)
            self.assertIn("manual_recovery", decision.reason)

    def test_matching_transient_trial_is_monitor_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            entry = self._entry(Path(td), active_state())
            decision = reconcile_tp_snapshot(entry, tp_snapshot("RUN", consumed=1))
            self.assertEqual(decision.action, ReconcileAction.MONITOR_ACTIVE)
            self.assertFalse(decision.command_permitted)


if __name__ == "__main__":
    unittest.main()
