"""Best-effort derived postprocess queue after the immutable ACK boundary.

Failures are recorded per job and never alter v1 trial disposition, safe
closure, ACK state, or the next candidate.  Safety/structural evaluation does
not belong in this worker.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .state import atomic_json, read_strict_json, utc_now


JOB_SCHEMA = "step5d.autotune-v3.derived-job/v1"
RESULT_SCHEMA = "step5d.autotune-v3.derived-result/v1"
_TRIAL_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")


class PostprocessError(RuntimeError):
    """A derived-only postprocess request is unsafe or malformed."""


@dataclass(frozen=True)
class PostprocessRun:
    total: int
    succeeded: int
    failed: int


Analyzer = Callable[[Path, str, Path], Mapping[str, Any]]


def derive_bundle_summary(capture: Path, trial_id: str, output_dir: Path) -> Mapping[str, Any]:
    """Produce a small derived summary; never recompute acceptance or ACK."""

    del output_dir
    bundle = read_strict_json(capture, role="immutable trial bundle")
    if not isinstance(bundle, dict):
        raise PostprocessError("immutable trial bundle must be an object")
    evaluation = bundle.get("evaluation")
    if not isinstance(evaluation, Mapping):
        raise PostprocessError("immutable trial bundle lacks evaluation")
    failures = evaluation.get("structural_failures")
    if failures is not None and not isinstance(failures, list):
        raise PostprocessError("structural_failures must be a list when present")
    return {
        "trial_id": trial_id,
        "eligible": evaluation.get("eligible") is True,
        "objective_mae_n": evaluation.get("objective_mae_n"),
        "structural_failure_count": len(failures or []),
        "safe_closure": evaluation.get("safe_closure") is True,
    }


class DerivedPostprocessQueue:
    def __init__(self, root: Path, *, allowed_capture_root: Path) -> None:
        self.root = root.expanduser().absolute()
        self.allowed_capture_root = allowed_capture_root.expanduser().absolute()
        self.pending = self.root / "pending"
        self.complete = self.root / "complete"
        self.failed = self.root / "failed"
        self.outputs = self.root / "outputs"

    def _ensure_layout(self) -> None:
        if self.root.exists() and (self.root.is_symlink() or not self.root.is_dir()):
            raise PostprocessError("postprocess root must be a real directory")
        for path in (self.pending, self.complete, self.failed, self.outputs):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            if path.is_symlink():
                raise PostprocessError("postprocess directories must not be symlinks")

    def submit(self, *, capture: Path, trial_id: str) -> str:
        if _TRIAL_ID.fullmatch(trial_id) is None:
            raise PostprocessError("trial_id is invalid")
        capture_input = capture.expanduser().absolute()
        if capture_input.is_symlink() or not capture_input.is_file():
            raise PostprocessError("immutable capture must be a real regular file")
        capture_path = capture_input.resolve(strict=True)
        try:
            capture_path.relative_to(self.allowed_capture_root.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise PostprocessError("immutable capture is outside the campaign root") from exc
        capture_sha = hashlib.sha256(capture_path.read_bytes()).hexdigest()
        job_id = hashlib.sha256(
            f"{trial_id}\0{capture_path}\0{capture_sha}".encode("utf-8")
        ).hexdigest()
        self._ensure_layout()
        payload = {
            "schema": JOB_SCHEMA,
            "job_id": job_id,
            "trial_id": trial_id,
            "capture": str(capture_path),
            "capture_sha256": capture_sha,
            "submitted_at": utc_now(),
        }
        terminal = (self.complete / f"{job_id}.json", self.failed / f"{job_id}.json")
        if any(path.exists() for path in terminal):
            return job_id
        pending = self.pending / f"{job_id}.json"
        if pending.exists():
            existing = read_strict_json(pending, role="derived postprocess job")
            comparable = dict(existing)
            comparable.pop("submitted_at", None)
            expected = dict(payload)
            expected.pop("submitted_at")
            if comparable != expected:
                raise PostprocessError("existing postprocess job identity differs")
            return job_id
        atomic_json(pending, payload)
        return job_id

    def _load_job(self, path: Path) -> Mapping[str, Any]:
        job = read_strict_json(path, role="derived postprocess job")
        required = {
            "schema",
            "job_id",
            "trial_id",
            "capture",
            "capture_sha256",
            "submitted_at",
        }
        if not isinstance(job, dict) or set(job) != required or job["schema"] != JOB_SCHEMA:
            raise PostprocessError("derived postprocess job schema differs")
        if path.name != f"{job['job_id']}.json":
            raise PostprocessError("derived postprocess filename differs from job id")
        capture = Path(str(job["capture"]))
        if capture.is_symlink() or not capture.is_file():
            raise PostprocessError("derived postprocess capture is missing or unsafe")
        if hashlib.sha256(capture.read_bytes()).hexdigest() != job["capture_sha256"]:
            raise PostprocessError("immutable capture changed after queueing")
        return job

    def run_pending(
        self,
        *,
        analyzer: Analyzer = derive_bundle_summary,
        limit: int | None = None,
    ) -> PostprocessRun:
        self._ensure_layout()
        paths = sorted(self.pending.glob("*.json"))
        if limit is not None:
            if type(limit) is not int or limit < 0:
                raise PostprocessError("postprocess limit must be non-negative")
            paths = paths[:limit]
        succeeded = failed = 0
        for path in paths:
            job_id = path.stem
            complete_path = self.complete / path.name
            failed_path = self.failed / path.name
            if complete_path.exists() or failed_path.exists():
                path.unlink(missing_ok=True)
                continue
            try:
                job = self._load_job(path)
                output_dir = self.outputs / job_id
                output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                result = analyzer(Path(job["capture"]), str(job["trial_id"]), output_dir)
                if not isinstance(result, Mapping):
                    raise PostprocessError("derived analyzer must return a mapping")
                atomic_json(
                    complete_path,
                    {
                        "schema": RESULT_SCHEMA,
                        "job_id": job_id,
                        "status": "complete",
                        "finished_at": utc_now(),
                        "result": dict(result),
                    },
                )
                succeeded += 1
            except Exception as exc:
                atomic_json(
                    failed_path,
                    {
                        "schema": RESULT_SCHEMA,
                        "job_id": job_id,
                        "status": "analysis_failed",
                        "finished_at": utc_now(),
                        "error": f"{type(exc).__name__}:{exc}",
                    },
                )
                failed += 1
            finally:
                path.unlink(missing_ok=True)
        if paths:
            directory_fd = os.open(self.pending, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        return PostprocessRun(total=len(paths), succeeded=succeeded, failed=failed)
