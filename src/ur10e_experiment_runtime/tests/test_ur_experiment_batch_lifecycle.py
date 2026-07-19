from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

from ur10e_experiment_runtime.batch import (  # noqa: E402
    BatchFate,
    BatchIdentity,
    BatchJournal,
    BatchRow,
    ExactAckReceipt,
    ReturnReferenceKind,
    SafeClosureReceipt,
    return_reference_for_row,
)
from ur10e_experiment_runtime.contracts import (  # noqa: E402
    OutputPathError,
    SpecValidationError,
)
from ur10e_experiment_runtime.stage_adapters import (  # noqa: E402
    control_candidate_uid,
)


def _overlay(index: int) -> dict:
    candidate = {
        "force_p_gain": 0.001 * (1.0 + index / 100.0),
        "force_i_gain": 0.00001,
        "force_damping": 7.0,
        "orientation_ko": 0.4 + index / 100.0,
    }
    return {
        **candidate,
        "control_candidate_uid": control_candidate_uid(candidate),
        "execution_profile_id": "nf050-slew050-a050",
        "step5d_preload_filtered_min_n": 7.5,
        "step5d_preload_filtered_max_n": 14.0,
        "step5d_preload_raw_min_n": 7.0,
        "step5d_preload_raw_max_n": 15.0,
        "step5d_preload_force_norm_max_n": 25.0,
        "step5d_preload_hold_s": 0.1,
        "step5d_preload_timeout_s": 10.0,
    }


def _identity() -> BatchIdentity:
    rows = []
    for index in range(1, 11):
        overlay = _overlay(index)
        rows.append(
            BatchRow(
                row_index=index,
                control_candidate={
                    name: overlay[name]
                    for name in (
                        "force_p_gain",
                        "force_i_gain",
                        "force_damping",
                        "orientation_ko",
                    )
                },
                trial_overlay=overlay,
            )
        )
    return BatchIdentity(
        experiment_fingerprint="a" * 64,
        launch_fingerprint="b" * 64,
        plant_epoch=2,
        rows=tuple(rows),
    )


def _complete_row(journal: BatchJournal, identity: BatchIdentity, row_index: int):
    trial_uid = f"{row_index:064x}"
    journal.start_attempt(row_index, trial_uid)
    journal.record_bundle(row_index, trial_uid, f"{100 + row_index:064x}")
    ack = ExactAckReceipt(
        batch_uid=identity.batch_uid,
        row_index=row_index,
        trial_uid=trial_uid,
        control_candidate_uid=identity.rows[row_index - 1].control_candidate_uid,
        arm_command_seq=row_index * 2 - 1,
        ack_command_seq=row_index * 2,
        consumed_command_seq=row_index * 2,
    )
    journal.record_ack_consumed(ack)
    closure = SafeClosureReceipt(
        batch_uid=identity.batch_uid,
        row_index=row_index,
        trial_uid=trial_uid,
        ack_uid=ack.ack_uid,
        return_reference=return_reference_for_row(row_index),
    )
    journal.record_safe_closure(closure)
    return trial_uid, ack, closure


class BatchLifecycleTest(unittest.TestCase):
    def test_batch_identity_requires_exact_ten_unique_ordered_rows(self):
        identity = _identity()
        self.assertEqual(len(identity.rows), 10)
        self.assertEqual(
            BatchIdentity.from_document(identity.document()).batch_uid,
            identity.batch_uid,
        )
        with self.assertRaisesRegex(SpecValidationError, "exact ordered"):
            BatchIdentity(
                experiment_fingerprint="a" * 64,
                launch_fingerprint="b" * 64,
                plant_epoch=1,
                rows=identity.rows[:-1],
            )
        duplicate = list(identity.rows)
        duplicate[-1] = BatchRow(
            row_index=10,
            control_candidate=identity.rows[0].control_candidate,
            trial_overlay=identity.rows[0].trial_overlay,
        )
        with self.assertRaisesRegex(SpecValidationError, "unique"):
            BatchIdentity(
                experiment_fingerprint="a" * 64,
                launch_fingerprint="b" * 64,
                plant_epoch=1,
                rows=tuple(duplicate),
            )

    def test_bundle_and_ack_are_not_completion_until_post_ack_safe_closure(self):
        identity = _identity()
        with tempfile.TemporaryDirectory() as directory:
            journal = BatchJournal.create(Path(directory) / "batch", identity)
            trial_uid = "1" * 64
            journal.start_attempt(1, trial_uid)
            self.assertIs(
                journal.state().rows[0].fate, BatchFate.ATTEMPTED_INCOMPLETE
            )
            journal.record_bundle(1, trial_uid, "2" * 64)
            self.assertIs(
                journal.state().rows[0].fate, BatchFate.ATTEMPTED_INCOMPLETE
            )
            ack = ExactAckReceipt(
                batch_uid=identity.batch_uid,
                row_index=1,
                trial_uid=trial_uid,
                control_candidate_uid=identity.rows[0].control_candidate_uid,
                arm_command_seq=3,
                ack_command_seq=4,
                consumed_command_seq=4,
            )
            journal.record_ack_consumed(ack)
            self.assertIs(
                journal.state().rows[0].fate, BatchFate.ATTEMPTED_INCOMPLETE
            )
            journal.record_safe_closure(
                SafeClosureReceipt(
                    batch_uid=identity.batch_uid,
                    row_index=1,
                    trial_uid=trial_uid,
                    ack_uid=ack.ack_uid,
                    return_reference=ReturnReferenceKind.NEAR_READY,
                )
            )
            state = journal.state()
            self.assertIs(state.rows[0].fate, BatchFate.ACK_COMPLETED)
            self.assertEqual(state.next_row_index, 2)

    def test_ack_and_closure_fail_closed_on_wrong_identity_or_order(self):
        identity = _identity()
        with self.assertRaisesRegex(SpecValidationError, "exact sequence"):
            ExactAckReceipt(
                batch_uid=identity.batch_uid,
                row_index=1,
                trial_uid="1" * 64,
                control_candidate_uid=identity.rows[0].control_candidate_uid,
                arm_command_seq=1,
                ack_command_seq=2,
                consumed_command_seq=3,
            )
        with tempfile.TemporaryDirectory() as directory:
            journal = BatchJournal.create(Path(directory) / "batch", identity)
            trial_uid = "1" * 64
            journal.start_attempt(1, trial_uid)
            with self.assertRaisesRegex(SpecValidationError, "active bundle"):
                journal.record_ack_consumed(
                    ExactAckReceipt(
                        batch_uid=identity.batch_uid,
                        row_index=1,
                        trial_uid=trial_uid,
                        control_candidate_uid=identity.rows[0].control_candidate_uid,
                        arm_command_seq=1,
                        ack_command_seq=2,
                        consumed_command_seq=2,
                    )
                )
            journal.record_bundle(1, trial_uid, "2" * 64)
            with self.assertRaisesRegex(SpecValidationError, "active incomplete"):
                journal.record_bundle(1, trial_uid, "2" * 64)
            with self.assertRaisesRegex(SpecValidationError, "active bundle"):
                journal.record_ack_consumed(
                    ExactAckReceipt(
                        batch_uid=identity.batch_uid,
                        row_index=1,
                        trial_uid=trial_uid,
                        control_candidate_uid=identity.rows[1].control_candidate_uid,
                        arm_command_seq=1,
                        ack_command_seq=2,
                        consumed_command_seq=2,
                    )
                )
            self.assertEqual(journal.state().journal_record_count, 2)

    def test_crash_cut_matrix_reconstructs_only_non_completed_rows(self):
        identity = _identity()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for cut in range(5):
                root = base / f"cut-{cut}"
                journal = BatchJournal.create(root, identity)
                trial_uid = "1" * 64
                if cut >= 1:
                    journal.start_attempt(1, trial_uid)
                if cut >= 2:
                    journal.record_bundle(1, trial_uid, "2" * 64)
                ack = ExactAckReceipt(
                    batch_uid=identity.batch_uid,
                    row_index=1,
                    trial_uid=trial_uid,
                    control_candidate_uid=identity.rows[0].control_candidate_uid,
                    arm_command_seq=1,
                    ack_command_seq=2,
                    consumed_command_seq=2,
                )
                if cut >= 3:
                    journal.record_ack_consumed(ack)
                if cut >= 4:
                    journal.record_safe_closure(
                        SafeClosureReceipt(
                            batch_uid=identity.batch_uid,
                            row_index=1,
                            trial_uid=trial_uid,
                            ack_uid=ack.ack_uid,
                            return_reference=ReturnReferenceKind.NEAR_READY,
                        )
                    )
                restored = BatchJournal.open(root).state()
                self.assertEqual(restored.next_row_index, 2 if cut == 4 else 1)
                self.assertEqual(
                    restored.rows[0].fate,
                    BatchFate.ACK_COMPLETED
                    if cut == 4
                    else (
                        BatchFate.UNATTEMPTED
                        if cut == 0
                        else BatchFate.ATTEMPTED_INCOMPLETE
                    ),
                )

    def test_exact_ten_rows_require_final_home_and_durable_result_before_exit_zero(self):
        identity = _identity()
        with tempfile.TemporaryDirectory() as directory:
            journal = BatchJournal.create(Path(directory) / "batch", identity)
            for row_index in range(1, 11):
                _complete_row(journal, identity, row_index)
            state = journal.state()
            self.assertTrue(state.complete)
            self.assertEqual(state.resume_row_indices, ())
            self.assertTrue(
                all(
                    row.return_reference is ReturnReferenceKind.NEAR_READY
                    for row in state.rows[:9]
                )
            )
            self.assertIs(
                state.rows[9].return_reference, ReturnReferenceKind.CAMPAIGN_HOME
            )
            with self.assertRaisesRegex(SpecValidationError, "BatchResult"):
                journal.verified_exit_code()
            result = journal.finalize()
            self.assertEqual(result["row_count"], 10)
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(journal.verified_exit_code(), 0)
            self.assertEqual(journal.finalize(), result)
            with self.assertRaisesRegex(OutputPathError, "already final"):
                journal.start_attempt(10, "f" * 64)

    def test_tampered_journal_or_result_fails_closed(self):
        identity = _identity()
        with tempfile.TemporaryDirectory() as directory:
            journal = BatchJournal.create(Path(directory) / "batch", identity)
            journal.start_attempt(1, "1" * 64)
            path = journal.journal_path
            record = json.loads(path.read_text())
            record["event"]["row_index"] = 2
            path.write_text(json.dumps(record) + "\n")
            with self.assertRaisesRegex(OutputPathError, "hash"):
                BatchJournal.open(journal.root)

    def test_identity_symlink_is_rejected_without_following_it(self):
        identity = _identity()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = BatchJournal.create(root / "batch", identity)
            target = root / "replacement.json"
            target.write_text(json.dumps(identity.document()))
            journal.identity_path.unlink()
            journal.identity_path.symlink_to(target)
            with self.assertRaisesRegex(OutputPathError, "batch identity"):
                BatchJournal.open(journal.root)


if __name__ == "__main__":
    unittest.main()
