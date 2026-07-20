from __future__ import annotations

import fcntl
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_offline_trial_rl.cli import assert_no_runtime_contention  # noqa: E402


class OfflineTrialCliTest(unittest.TestCase):
    def test_runtime_lock_blocks_heavy_offline_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "live_writer.lock"
            path.touch()
            with path.open("rb") as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(RuntimeError, "live_writer.lock"):
                    assert_no_runtime_contention(Path(directory))

    def test_unheld_runtime_lock_allows_offline_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "formal_timing.lock"
            path.touch()
            assert_no_runtime_contention(Path(directory))


if __name__ == "__main__":
    unittest.main()
