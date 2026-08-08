"""Durable r006 objective artifacts and their append-only binding ledger.

The r005 observation ledger remains the mature lifecycle ledger.  This
sidecar owns only the changed r006 objective semantic.  A row becomes
trainable only after a fresh interpreter has read the immutable artifact and
recomputed the scalar from its raw PATH samples.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .contracts import canonical_bytes
from .objective import R006ObjectiveError, R006ObjectiveReceipt


SIDECAR_SCHEMA = "step5d.autotune-v4/r006-objective-sidecar-v1"
GENESIS_SHA256 = "0" * 64


class R006SidecarError(RuntimeError):
    """The r006 raw objective store is not durable or content-bound."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _row_sha(row: Mapping[str, Any]) -> str:
    return _sha(canonical_bytes({key: value for key, value in row.items() if key != "row_sha256"}))


def _read_rows(path: Path) -> tuple[dict[str, Any], ...]:
    if path.is_symlink() or not path.is_file():
        raise R006SidecarError("r006 sidecar must be a regular file")
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise R006SidecarError(f"r006 sidecar row {number} is invalid") from exc
        if not isinstance(value, dict):
            raise R006SidecarError(f"r006 sidecar row {number} is not an object")
        rows.append(value)
    return tuple(rows)


def _fresh_verify_artifact(path: Path, campaign_fingerprint: str) -> R006ObjectiveReceipt:
    tools_root = str(Path(__file__).resolve().parents[1])
    code = r'''
import json, pathlib, sys
sys.path.insert(0, sys.argv[1])
from step5d_autotune_v4_r006.objective import R006ObjectiveReceipt, cold_read_verify
path = pathlib.Path(sys.argv[2])
if path.is_symlink() or not path.is_file():
    raise ValueError("artifact is not a regular file")
receipt = R006ObjectiveReceipt.from_mapping(json.loads(path.read_text(encoding="utf-8")))
verified = cold_read_verify(receipt, expected_campaign_fingerprint=sys.argv[3])
print(json.dumps(verified.as_dict(), sort_keys=True, separators=(",", ":")))
'''
    try:
        completed = subprocess.run(
            [sys.executable, "-c", code, tools_root, str(path.resolve(strict=True)), campaign_fingerprint],
            check=True,
            capture_output=True,
            text=True,
            timeout=20.0,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip().splitlines()[-1:] or ["child exited nonzero"]
        raise R006SidecarError(f"r006 fresh artifact verification failed: {detail[0]}") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise R006SidecarError("r006 fresh artifact verification failed") from exc
    try:
        value = json.loads(completed.stdout)
        receipt = R006ObjectiveReceipt.from_mapping(value)
    except (UnicodeError, json.JSONDecodeError, R006ObjectiveError) as exc:
        raise R006SidecarError("r006 fresh verifier returned an invalid receipt") from exc
    if not receipt.trainable:
        raise R006SidecarError("r006 fresh verifier did not grant trainable capability")
    return receipt


class R006ObjectiveSidecar:
    """One immutable raw receipt per attempt plus a hash-chain index."""

    def __init__(self, path: Path, *, campaign_fingerprint: str) -> None:
        self.path = Path(path)
        self.campaign_fingerprint = str(campaign_fingerprint)
        if len(self.campaign_fingerprint) != 64:
            raise R006SidecarError("r006 sidecar campaign fingerprint is invalid")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_root = self.path.parent / "r006_raw_objectives"
        if self.artifact_root.is_symlink():
            raise R006SidecarError("r006 artifact root must not be a symlink")
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        if self.path.exists() or self.path.is_symlink():
            self._verify_rows(cold_read=True)
        else:
            header = {
                "schema": SIDECAR_SCHEMA,
                "record_type": "header",
                "campaign_fingerprint": self.campaign_fingerprint,
                "objective_authority": "fresh_subprocess_raw_recompute_only",
            }
            with self.path.open("xb") as stream:
                stream.write(canonical_bytes(header) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            _fsync_dir(self.path.parent)
        self._cached = self._verify_rows(cold_read=True)

    def _artifact_path(self, artifact_name: str) -> Path:
        if not artifact_name.endswith(".json") or "/" in artifact_name or "\\" in artifact_name:
            raise R006SidecarError("r006 artifact name is unsafe")
        candidate = self.artifact_root / artifact_name
        if candidate.is_symlink():
            raise R006SidecarError("r006 artifact is a symlink")
        return candidate

    def _verify_rows(self, *, cold_read: bool) -> tuple[Mapping[str, Any], ...]:
        rows = _read_rows(self.path)
        if not rows or rows[0] != {
            "schema": SIDECAR_SCHEMA,
            "record_type": "header",
            "campaign_fingerprint": self.campaign_fingerprint,
            "objective_authority": "fresh_subprocess_raw_recompute_only",
        }:
            raise R006SidecarError("r006 sidecar header differs")
        previous = GENESIS_SHA256
        seen: set[tuple[int, str]] = set()
        verified_rows: list[Mapping[str, Any]] = []
        for number, raw in enumerate(rows[1:], 2):
            row = dict(raw)
            if row.get("schema") != SIDECAR_SCHEMA or row.get("record_type") != "objective_artifact":
                raise R006SidecarError(f"r006 sidecar row {number} schema differs")
            if row.get("campaign_fingerprint") != self.campaign_fingerprint:
                raise R006SidecarError(f"r006 sidecar row {number} campaign differs")
            if row.get("previous_sha256") != previous or row.get("row_sha256") != _row_sha(row):
                raise R006SidecarError(f"r006 sidecar row {number} hash chain differs")
            identity = (int(row.get("attempt_sequence", 0)), str(row.get("execution_id", "")))
            if identity[0] <= 0 or not identity[1] or identity in seen:
                raise R006SidecarError(f"r006 sidecar row {number} identity differs")
            seen.add(identity)
            artifact = self._artifact_path(str(row.get("artifact_name", "")))
            if not artifact.is_file():
                raise R006SidecarError(f"r006 sidecar row {number} artifact is missing")
            encoded = artifact.read_bytes()
            if _sha(encoded) != row.get("artifact_sha256") or len(encoded) != row.get("artifact_size"):
                raise R006SidecarError(f"r006 sidecar row {number} artifact bytes differ")
            receipt = (
                _fresh_verify_artifact(artifact, self.campaign_fingerprint)
                if cold_read
                else R006ObjectiveReceipt.from_mapping(json.loads(encoded))
            )
            if (
                receipt.attempt_sequence != identity[0]
                or receipt.execution_id != identity[1]
                or receipt.metadata.get("candidate_uid") != row.get("candidate_uid")
                or receipt.builder_seal_sha256 != row.get("builder_seal_sha256")
            ):
                raise R006SidecarError(f"r006 sidecar row {number} receipt binding differs")
            enriched = {
                **row,
                "receipt": receipt.as_dict(),
                "trainable": receipt.trainable,
                "objective_mae_n": receipt.objective,
                "artifact_path": str(artifact.resolve(strict=True)),
            }
            verified_rows.append(MappingProxyType(enriched))
            previous = str(row["row_sha256"])
        return tuple(verified_rows)

    def append(
        self,
        receipt: R006ObjectiveReceipt,
        *,
        epoch: int,
        candidate_uid: str,
        kind: str,
        point_key: list[Any],
    ) -> Mapping[str, Any]:
        if not isinstance(receipt, R006ObjectiveReceipt):
            raise R006SidecarError("r006 append requires a typed builder receipt")
        if receipt.verification_state not in {"builder_sealed", "verified_raw_artifact"}:
            raise R006SidecarError("r006 receipt state is invalid")
        if receipt.campaign_fingerprint != self.campaign_fingerprint:
            raise R006SidecarError("r006 receipt campaign differs")
        if receipt.metadata.get("candidate_uid") != candidate_uid:
            raise R006SidecarError("r006 receipt candidate binding differs")
        matches = [
            row for row in self._cached
            if row["attempt_sequence"] == receipt.attempt_sequence
        ]
        if matches:
            existing = matches[0]
            if (
                existing["execution_id"] == receipt.execution_id
                and existing["builder_seal_sha256"] == receipt.builder_seal_sha256
                and existing["candidate_uid"] == candidate_uid
            ):
                return existing
            raise R006SidecarError("r006 attempt sequence already has different evidence")
        identity = {
            "campaign_fingerprint": self.campaign_fingerprint,
            "attempt_sequence": receipt.attempt_sequence,
            "execution_id": receipt.execution_id,
            "candidate_uid": candidate_uid,
            "builder_seal_sha256": receipt.builder_seal_sha256,
        }
        artifact_name = _sha(canonical_bytes(identity)) + ".json"
        target = self._artifact_path(artifact_name)
        encoded = canonical_bytes(receipt.as_dict()) + b"\n"
        if target.exists():
            if not target.is_file() or target.read_bytes() != encoded:
                raise R006SidecarError("r006 immutable artifact identity collision")
        else:
            descriptor, temporary_name = tempfile.mkstemp(prefix=".r006-", suffix=".tmp", dir=self.artifact_root)
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
                _fsync_dir(self.artifact_root)
                _fsync_dir(self.path.parent)
            finally:
                temporary.unlink(missing_ok=True)
        verified = _fresh_verify_artifact(target, self.campaign_fingerprint)
        previous = str(self._cached[-1]["row_sha256"]) if self._cached else GENESIS_SHA256
        row: dict[str, Any] = {
            "schema": SIDECAR_SCHEMA,
            "record_type": "objective_artifact",
            "campaign_fingerprint": self.campaign_fingerprint,
            "epoch": int(epoch),
            "attempt_sequence": receipt.attempt_sequence,
            "execution_id": receipt.execution_id,
            "candidate_uid": candidate_uid,
            "kind": str(kind),
            "point_key": list(point_key),
            "artifact_name": artifact_name,
            "artifact_sha256": _sha(encoded),
            "artifact_size": len(encoded),
            "builder_seal_sha256": verified.builder_seal_sha256,
            "previous_sha256": previous,
        }
        row["row_sha256"] = _row_sha(row)
        with self.path.open("ab") as stream:
            stream.write(canonical_bytes(row) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_dir(self.path.parent)
        self._cached = self._verify_rows(cold_read=True)
        return self._cached[-1]

    def fresh_process_verify(self) -> tuple[Mapping[str, Any], ...]:
        self._cached = self._verify_rows(cold_read=True)
        return self._cached

    @property
    def rows(self) -> tuple[Mapping[str, Any], ...]:
        return self._cached


__all__ = ["R006ObjectiveSidecar", "R006SidecarError", "SIDECAR_SCHEMA"]
