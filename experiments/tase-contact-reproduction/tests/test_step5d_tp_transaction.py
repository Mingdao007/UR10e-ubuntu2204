#!/usr/bin/env python3
from __future__ import annotations
import sys, tempfile, unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import run_step5d_tp_transaction as coordinator  # noqa: E402

class Step5dTpTransactionTest(unittest.TestCase):
    def args(self, root: Path) -> list[str]:
        return ["step5d_x", "--root", str(root), "--local-dir", str(root / "programs")]

    def test_success_holds_one_lock_scope_for_all_three_stages(self) -> None:
        events: list[str] = []
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(coordinator, "acquire_controller_mutation_locks", side_effect=lambda: events.append("lock") or [object()]), \
             mock.patch.object(coordinator.upload, "_main", side_effect=lambda _: events.append("upload") or 0), \
             mock.patch.object(coordinator.promote, "_main", side_effect=lambda _: events.append("promote") or 0), \
             mock.patch.object(coordinator.readback, "main", side_effect=lambda _: events.append("readback") or 0), \
             mock.patch.object(coordinator, "release_controller_mutation_locks", side_effect=lambda _: events.append("release")):
            self.assertEqual(coordinator.main(self.args(Path(tmp))), 0)
        self.assertEqual(events, ["lock", "upload", "promote", "readback", "release"])

    def test_failure_stops_following_stages_and_releases(self) -> None:
        events: list[str] = []
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(coordinator, "acquire_controller_mutation_locks", return_value=[object()]), \
             mock.patch.object(coordinator.upload, "_main", return_value=7), \
             mock.patch.object(coordinator.promote, "_main") as promote, \
             mock.patch.object(coordinator.readback, "main") as readback, \
             mock.patch.object(coordinator, "release_controller_mutation_locks", side_effect=lambda _: events.append("release")):
            self.assertEqual(coordinator.main(self.args(Path(tmp))), 7)
        promote.assert_not_called()
        readback.assert_not_called()
        self.assertEqual(events, ["release"])

if __name__ == "__main__":
    unittest.main()
