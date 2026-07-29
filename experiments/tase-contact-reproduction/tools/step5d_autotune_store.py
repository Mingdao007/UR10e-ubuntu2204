#!/usr/bin/python3.10
"""Strict, atomic campaign evidence store for Step5d-native autotune."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from step5d_autotune_contract import (
    SCHEMA_VERSION,
    CampaignSpec,
    CaptureArtifactPaths,
    CaptureManifest,
    Evaluation,
    ExecutionProfile,
    ForceCandidate,
    SearchAttestation,
    TrialSource,
    TrialTransition,
    TrialTransitionSourceScope,
    TrialDisposition,
    TrialSpec,
    canonical_json_bytes,
    require_sha256,
    sha256_json,
)


INDEX_SCHEMA_VERSION = "step5d.autotune.store-index/v1"
CAMPAIGN_IDENTITY_SCHEMA_VERSION = "step5d.autotune.store-campaign/v1"
BUNDLE_SCHEMA_VERSION = "step5d.autotune.immutable-bundle/v1"
HISTORY_SCHEMA_VERSION = "step5d.autotune.history/v1"
QUARANTINE_SCHEMA_VERSION = "step5d.autotune.quarantine/v1"
ARTIFACT_ROLES = ("csv", "metadata", "terminal_manifest")


class DuplicateTrialError(RuntimeError):
    pass


class ImmutableBundleError(RuntimeError):
    pass


class EvidenceIntegrityError(ImmutableBundleError):
    pass


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _parse_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite JSON number is forbidden: {value}")
    return parsed


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key is forbidden: {key}")
        result[key] = value
    return result


def _strict_json_loads(text: str, *, role: str) -> Any:
    try:
        return json.loads(
            text,
            parse_constant=_reject_json_constant,
            parse_float=_parse_json_float,
            object_pairs_hook=_unique_json_object,
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise EvidenceIntegrityError(f"{role} is not strict JSON: {exc}") from exc


def _strict_json_bytes(encoded: bytes, *, role: str) -> Any:
    try:
        text = encoded.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise EvidenceIntegrityError(f"{role} is not UTF-8 JSON: {exc}") from exc
    return _strict_json_loads(text, role=role)


def _read_strict_json(path: Path, *, role: str) -> Any:
    encoded, _, _, _ = _read_regular_file_once(path, role=role)
    return _strict_json_bytes(encoded, role=role)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _atomic_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = "".join(
        json.dumps(row, sort_keys=True, allow_nan=False) + "\n" for row in rows
    )
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _read_regular_file_once(
    path: Path,
    *,
    role: str,
) -> tuple[bytes, str, int, tuple[int, int]]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise EvidenceIntegrityError(f"{role} artifact is missing or unsafe at {path}: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise EvidenceIntegrityError(f"{role} artifact is not a regular file: {path}")
        chunks: list[bytes] = []
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
            digest.update(chunk)
        closed = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        current = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise EvidenceIntegrityError(f"{role} artifact disappeared during hashing: {path}") from exc
    identity_before = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
    identity_after = (closed.st_dev, closed.st_ino, closed.st_size, closed.st_mtime_ns)
    identity_current = (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
    if identity_before != identity_after or identity_after != identity_current:
        raise EvidenceIntegrityError(f"{role} artifact changed during hashing: {path}")
    return (
        b"".join(chunks),
        digest.hexdigest(),
        opened.st_size,
        (opened.st_dev, opened.st_ino),
    )


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _empty_index() -> dict[str, Any]:
    return {
        "schema_version": INDEX_SCHEMA_VERSION,
        "trial_uids": {},
        "capture_uids": {},
    }


def _artifact_digest_map(provenance: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    return {role: str(provenance[role]["sha256"]) for role in ARTIFACT_ROLES}


def _history_identity_core(row: Mapping[str, Any]) -> dict[str, Any]:
    evaluation = row.get("evaluation")
    if not isinstance(evaluation, Mapping):
        raise EvidenceIntegrityError("history evaluation is not an object")
    provenance = row.get("artifact_provenance")
    if not isinstance(provenance, Mapping):
        raise EvidenceIntegrityError("history artifact provenance is not an object")
    return {
        "schema_version": HISTORY_SCHEMA_VERSION,
        "trial_uid": row.get("trial_uid"),
        "trial_spec_sha256": row.get("trial_spec_sha256"),
        "trial": row.get("trial"),
        "candidate_uid": row.get("candidate_uid"),
        "comparison_key": row.get("comparison_key"),
        "backend_id": row.get("backend_id"),
        "candidate": row.get("candidate"),
        "execution_profile": row.get("execution_profile"),
        "plant_epoch": row.get("plant_epoch"),
        "campaign_id": row.get("campaign_id"),
        "campaign_epoch": row.get("campaign_epoch"),
        "campaign_fingerprint": row.get("campaign_fingerprint"),
        "source_fingerprint": row.get("source_fingerprint"),
        "config_fingerprint": row.get("config_fingerprint"),
        "physical_capture_uid": row.get("physical_capture_uid"),
        "capture_identity_sha256": row.get("capture_identity_sha256"),
        "artifact_sha256": _artifact_digest_map(provenance),
        "evaluation": dict(evaluation),
    }


class CampaignStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.lock_path = self.root / ".campaign.lock"
        self.campaign_identity_path = self.root / "campaign_identity.json"
        self.index_path = self.root / "trial_index.json"
        self.history_path = self.root / "history.jsonl"
        self.quarantine_dir = self.root / "quarantine"

    def initialize(self, campaign_payload: dict[str, Any]) -> None:
        if not isinstance(campaign_payload, dict):
            raise ImmutableBundleError("campaign payload must be an object")
        canonical_json_bytes(campaign_payload)
        with _exclusive_lock(self.lock_path):
            self.root.mkdir(parents=True, exist_ok=True)
            path = self.root / "campaign.json"
            if path.exists():
                existing = _read_strict_json(path, role="campaign.json")
                if canonical_json_bytes(existing) != canonical_json_bytes(campaign_payload):
                    raise ImmutableBundleError("campaign.json already exists with different bytes")
            else:
                _atomic_json(path, campaign_payload)
            if not self.index_path.exists():
                _atomic_json(self.index_path, _empty_index())
            else:
                self._index()

    def _index(self) -> dict[str, Any]:
        if not self.index_path.exists():
            return _empty_index()
        payload = _read_strict_json(self.index_path, role="trial index")
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "trial_uids",
            "capture_uids",
        }:
            raise ImmutableBundleError("trial index fields are invalid")
        if payload.get("schema_version") != INDEX_SCHEMA_VERSION:
            raise ImmutableBundleError("trial index schema mismatch")
        trial_uids = payload.get("trial_uids")
        capture_uids = payload.get("capture_uids")
        if not isinstance(trial_uids, dict) or not isinstance(capture_uids, dict):
            raise ImmutableBundleError("trial index maps are invalid")
        for trial_uid, reference in trial_uids.items():
            require_sha256("indexed trial_uid", trial_uid)
            if not isinstance(reference, dict) or set(reference) != {
                "relative_trial_dir",
                "trial_spec_sha256",
            }:
                raise ImmutableBundleError("indexed trial reference is invalid")
            require_sha256("indexed trial_spec_sha256", reference["trial_spec_sha256"])
            if reference["relative_trial_dir"] != f"trials/{trial_uid}":
                raise ImmutableBundleError("indexed trial path is not stable and relative")
        for capture_uid, reference in capture_uids.items():
            require_sha256("indexed physical_capture_uid", capture_uid)
            if not isinstance(reference, dict) or set(reference) != {"trial_uid", "csv_sha256"}:
                raise ImmutableBundleError("indexed capture reference is invalid")
            require_sha256("indexed capture trial_uid", reference["trial_uid"])
            require_sha256("indexed csv_sha256", reference["csv_sha256"])
        return payload

    @staticmethod
    def physical_capture_uid(*, backend_id: str, csv_sha256: str) -> str:
        require_sha256("csv_sha256", csv_sha256)
        if not isinstance(backend_id, str) or not backend_id.strip():
            raise ValueError("backend_id must be a non-empty string")
        return sha256_json(
            {
                "schema_version": "step5d.autotune.physical-capture/v1",
                "backend_id": backend_id,
                "csv_sha256": csv_sha256,
            }
        )

    @staticmethod
    def _capture_identity(manifest: CaptureManifest) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "trial_uid": manifest.trial_uid,
            "backend_id": manifest.backend_id,
            "source_fingerprint_pre": manifest.source_fingerprint_pre,
            "source_fingerprint_post": manifest.source_fingerprint_post,
            "config_fingerprint_pre": manifest.config_fingerprint_pre,
            "config_fingerprint_post": manifest.config_fingerprint_post,
            "candidate_token": manifest.candidate_token,
            "terminal_reason": manifest.terminal_reason,
            "host_cause": manifest.host_cause,
            "csv_sha256": manifest.csv_sha256,
            "metadata_sha256": manifest.metadata_sha256,
            "terminal_manifest_sha256": manifest.terminal_manifest_sha256,
            "completion_marker": manifest.completion_marker,
            "cadence_ok": manifest.cadence_ok,
            "feedback_fresh": manifest.feedback_fresh,
            "rnn_oracle_aligned": manifest.rnn_oracle_aligned,
            "safety_normal": manifest.safety_normal,
            "returned_safe": manifest.returned_safe,
            "immutable_bundle_written": manifest.immutable_bundle_written,
            "stage25_complete_s": manifest.stage25_complete_s,
            "safe_closure_evidence": manifest.safe_closure_evidence.payload(),
            "evidence": dict(manifest.evidence),
        }

    @staticmethod
    def _verify_trial_closure(
        trial: TrialSpec,
        manifest: CaptureManifest,
        evaluation: Evaluation,
    ) -> None:
        if manifest.trial_uid != trial.trial_uid or evaluation.trial_uid != trial.trial_uid:
            raise EvidenceIntegrityError("trial/capture/evaluation identity mismatch")
        if manifest.backend_id != trial.backend_id or evaluation.backend_id != trial.backend_id:
            raise EvidenceIntegrityError("trial/capture/evaluation backend mismatch")
        if manifest.candidate_token != trial.candidate_token:
            raise EvidenceIntegrityError("capture candidate_token mismatch")
        if manifest.source_fingerprint_pre != trial.source_fingerprint:
            raise EvidenceIntegrityError("capture pre source fingerprint mismatch")
        if manifest.config_fingerprint_pre != trial.config_fingerprint:
            raise EvidenceIntegrityError("capture pre config fingerprint mismatch")
        if not manifest.fingerprint_closed:
            raise EvidenceIntegrityError("capture pre/post fingerprint closure failed")
        if manifest.safe_closure_evidence.returned_safe is not manifest.returned_safe:
            raise EvidenceIntegrityError("capture detailed safe closure proof mismatch")
        if evaluation.safe_closure is not manifest.returned_safe:
            raise EvidenceIntegrityError("evaluation safe closure differs from capture proof")
        if evaluation.eligible and (
            manifest.terminal_reason != 1
            or manifest.host_cause is not None
            or not manifest.completion_marker
            or not manifest.immutable_bundle_written
            or not manifest.cadence_ok
            or not manifest.feedback_fresh
            or not manifest.rnn_oracle_aligned
            or not manifest.safety_normal
            or not manifest.returned_safe
            or manifest.stage25_complete_s < 60.0
            or evaluation.complete_bins != trial.campaign.required_bins
        ):
            raise EvidenceIntegrityError("eligible evaluation lacks complete safe capture closure")

    def _publish_artifact_snapshot(self, role: str, encoded: bytes, digest: str) -> Path:
        require_sha256(f"{role} snapshot digest", digest)
        path = self.root / "artifacts" / "sha256" / digest / role
        if path.exists():
            _, current_sha256, current_size, _ = _read_regular_file_once(
                path,
                role=f"retained {role} snapshot",
            )
            if current_sha256 != digest or current_size != len(encoded):
                raise EvidenceIntegrityError(f"retained {role} snapshot is corrupt")
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{role}.", dir=path.parent)
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            temporary_path.chmod(0o444)
            os.replace(temporary_path, path)
            _fsync_directory(path.parent)
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
        _, current_sha256, current_size, _ = _read_regular_file_once(
            path,
            role=f"retained {role} snapshot",
        )
        if current_sha256 != digest or current_size != len(encoded):
            raise EvidenceIntegrityError(f"retained {role} snapshot publish verification failed")
        return path

    def _verify_artifacts(
        self,
        manifest: CaptureManifest,
        artifact_paths: CaptureArtifactPaths,
    ) -> dict[str, dict[str, Any]]:
        expected = {
            "csv": manifest.csv_sha256,
            "metadata": manifest.metadata_sha256,
            "terminal_manifest": manifest.terminal_manifest_sha256,
        }
        role_paths = artifact_paths.by_role()
        provenance: dict[str, dict[str, Any]] = {}
        opened_file_identities: set[tuple[int, int]] = set()
        for role in ARTIFACT_ROLES:
            path = role_paths[role]
            encoded, current_sha256, size_bytes, file_identity = _read_regular_file_once(
                path,
                role=role,
            )
            if file_identity in opened_file_identities:
                raise EvidenceIntegrityError("capture artifact roles must reference distinct files")
            opened_file_identities.add(file_identity)
            if current_sha256 != expected[role]:
                raise EvidenceIntegrityError(f"{role} bytes differ from CaptureManifest SHA-256")
            if role != "csv":
                payload = _strict_json_bytes(encoded, role=f"{role} artifact")
                self._verify_artifact_semantics(role, payload, manifest)
            retained_path = self._publish_artifact_snapshot(role, encoded, current_sha256)
            provenance[role] = {
                "path": str(retained_path),
                "sha256": current_sha256,
                "size_bytes": size_bytes,
            }
        return provenance

    @staticmethod
    def _verify_artifact_semantics(
        role: str,
        payload: Any,
        manifest: CaptureManifest,
    ) -> None:
        if role not in {"metadata", "terminal_manifest"}:
            raise EvidenceIntegrityError(f"unsupported JSON artifact role: {role}")
        if not isinstance(payload, dict):
            raise EvidenceIntegrityError(f"{role} artifact must be a JSON object")
        expected_identity: dict[str, str | int] = {
            "trial_uid": manifest.trial_uid,
            "backend_id": manifest.backend_id,
            "candidate_token": manifest.candidate_token,
        }
        if role == "terminal_manifest":
            expected_identity["terminal_reason"] = manifest.terminal_reason
        missing_identity = set(expected_identity) - set(payload)
        if missing_identity:
            raise EvidenceIntegrityError(
                f"{role} artifact identity fields missing: {sorted(missing_identity)}"
            )
        for name, expected_value in expected_identity.items():
            actual_value = payload[name]
            if type(actual_value) is not type(expected_value) or actual_value != expected_value:
                raise EvidenceIntegrityError(
                    f"{role} artifact {name} conflicts with CaptureManifest"
                )
        if role == "terminal_manifest":
            closure = payload.get("safe_closure_evidence")
            if closure != manifest.safe_closure_evidence.payload():
                raise EvidenceIntegrityError(
                    "terminal_manifest safe_closure_evidence conflicts with CaptureManifest"
                )

    def _register_trial_locked(
        self,
        trial: TrialSpec,
        index: dict[str, Any],
        *,
        provenance_run_dir: Path | None = None,
    ) -> Path:
        self._bind_campaign_locked(trial.campaign)
        trial_dir = self.root / "trials" / trial.trial_uid
        payload = trial.payload()
        trial_spec_sha256 = sha256_json(payload)
        if provenance_run_dir is not None:
            payload["provenance_run_dir"] = str(provenance_run_dir.resolve())
        reference = {
            "relative_trial_dir": f"trials/{trial.trial_uid}",
            "trial_spec_sha256": trial_spec_sha256,
        }
        prior = index["trial_uids"].get(trial.trial_uid)
        if prior not in (None, reference):
            raise DuplicateTrialError("trial_uid already maps to a different immutable spec")
        path = trial_dir / "trial_spec.json"
        if path.exists():
            existing = _read_strict_json(path, role="trial_spec.json")
            if not isinstance(existing, dict):
                raise DuplicateTrialError("existing trial_spec.json is not an object")
            existing = dict(existing)
            existing.pop("provenance_run_dir", None)
            candidate = dict(payload)
            candidate.pop("provenance_run_dir", None)
            if canonical_json_bytes(existing) != canonical_json_bytes(candidate):
                raise DuplicateTrialError("trial_uid collision with different immutable spec")
        else:
            _atomic_json(path, payload)
        index["trial_uids"][trial.trial_uid] = reference
        return trial_dir

    def _bind_campaign_locked(self, campaign: CampaignSpec) -> None:
        campaign_payload = asdict(campaign)
        expected = {
            "schema_version": CAMPAIGN_IDENTITY_SCHEMA_VERSION,
            "campaign_uid": sha256_json(campaign_payload),
            "campaign": campaign_payload,
        }
        if self.campaign_identity_path.exists():
            existing = _read_strict_json(
                self.campaign_identity_path,
                role="campaign_identity.json",
            )
            if canonical_json_bytes(existing) != canonical_json_bytes(expected):
                raise ImmutableBundleError("campaign store is already bound to another campaign")
        else:
            _atomic_json(self.campaign_identity_path, expected)

    def register_trial(self, trial: TrialSpec, *, provenance_run_dir: Path | None = None) -> Path:
        with _exclusive_lock(self.lock_path):
            index = self._index()
            trial_dir = self._register_trial_locked(
                trial,
                index,
                provenance_run_dir=provenance_run_dir,
            )
            _atomic_json(self.index_path, index)
        return trial_dir

    def _history_row(
        self,
        trial: TrialSpec,
        manifest: CaptureManifest,
        evaluation: Evaluation,
        capture_uid: str,
        artifact_provenance: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        row: dict[str, Any] = {
            "history_schema_version": HISTORY_SCHEMA_VERSION,
            "trial_uid": trial.trial_uid,
            "trial_spec_sha256": sha256_json(trial.payload()),
            "trial": trial.payload(),
            "candidate_uid": trial.candidate.candidate_uid,
            "comparison_key": trial.comparison_key,
            "backend_id": trial.backend_id,
            "candidate": trial.candidate.payload(),
            "execution_profile": trial.execution_profile.payload(),
            "plant_epoch": trial.plant_epoch,
            "campaign_id": trial.campaign.campaign_id,
            "campaign_epoch": trial.campaign.campaign_epoch,
            "campaign_fingerprint": trial.campaign.campaign_fingerprint,
            "source_fingerprint": trial.source_fingerprint,
            "config_fingerprint": trial.config_fingerprint,
            "physical_capture_uid": capture_uid,
            "capture_identity_sha256": sha256_json(self._capture_identity(manifest)),
            "artifact_provenance": artifact_provenance,
            "evaluation": evaluation.history_payload(),
        }
        row["history_identity"] = sha256_json(_history_identity_core(row))
        canonical_json_bytes(row)
        return row

    @staticmethod
    def _bundle_identity(bundle: Mapping[str, Any]) -> dict[str, Any]:
        required = {
            "schema_version",
            "trial",
            "capture",
            "evaluation",
            "physical_capture_uid",
            "history_identity",
            "artifact_provenance",
        }
        if set(bundle) != required or bundle.get("schema_version") != BUNDLE_SCHEMA_VERSION:
            raise EvidenceIntegrityError("immutable bundle fields/schema mismatch")
        provenance = bundle.get("artifact_provenance")
        if not isinstance(provenance, Mapping) or set(provenance) != set(ARTIFACT_ROLES):
            raise EvidenceIntegrityError("immutable bundle provenance is missing")
        for role in ARTIFACT_ROLES:
            reference = provenance[role]
            if not isinstance(reference, Mapping) or set(reference) != {
                "path",
                "sha256",
                "size_bytes",
            }:
                raise EvidenceIntegrityError(f"immutable bundle {role} provenance is invalid")
        return {
            "schema_version": bundle.get("schema_version"),
            "trial": bundle.get("trial"),
            "capture": bundle.get("capture"),
            "evaluation": bundle.get("evaluation"),
            "physical_capture_uid": bundle.get("physical_capture_uid"),
            "history_identity": bundle.get("history_identity"),
            "artifact_sha256": _artifact_digest_map(provenance),
        }

    def _bundle_capture_reservations(self) -> dict[str, str]:
        trials_root = self.root / "trials"
        if not trials_root.exists():
            return {}
        reservations: dict[str, str] = {}
        for trial_dir in sorted(trials_root.iterdir(), key=lambda path: path.name):
            if trial_dir.is_symlink() or not trial_dir.is_dir():
                raise EvidenceIntegrityError(f"unsafe entry in trial store: {trial_dir}")
            require_sha256("trial directory identity", trial_dir.name)
            bundle_path = trial_dir / "immutable_trial_bundle.json"
            if not bundle_path.exists():
                continue
            bundle = _read_strict_json(bundle_path, role="capture reservation bundle")
            if not isinstance(bundle, dict):
                raise EvidenceIntegrityError("capture reservation bundle is not an object")
            self._bundle_identity(bundle)
            trial_payload = bundle["trial"]
            capture_payload = bundle["capture"]
            if not isinstance(trial_payload, dict) or not isinstance(capture_payload, dict):
                raise EvidenceIntegrityError("capture reservation identity payload is invalid")
            if trial_payload.get("trial_uid") != trial_dir.name:
                raise EvidenceIntegrityError("capture reservation trial directory mismatch")
            try:
                capture = CaptureManifest(**capture_payload)
            except (TypeError, ValueError) as exc:
                raise EvidenceIntegrityError(f"capture reservation manifest is invalid: {exc}") from exc
            if any(
                (
                    capture.trial_uid != trial_payload.get("trial_uid"),
                    capture.backend_id != trial_payload.get("backend_id"),
                    capture.candidate_token != trial_payload.get("candidate_token"),
                    capture.source_fingerprint_pre != trial_payload.get("source_fingerprint"),
                    capture.source_fingerprint_post != trial_payload.get("source_fingerprint"),
                    capture.config_fingerprint_pre != trial_payload.get("config_fingerprint"),
                    capture.config_fingerprint_post != trial_payload.get("config_fingerprint"),
                )
            ):
                raise EvidenceIntegrityError("capture reservation manifest/trial identity mismatch")
            provenance = bundle["artifact_provenance"]
            retained_paths = CaptureArtifactPaths(
                csv_path=Path(str(provenance["csv"]["path"])),
                metadata_path=Path(str(provenance["metadata"]["path"])),
                terminal_manifest_path=Path(str(provenance["terminal_manifest"]["path"])),
            )
            current_provenance = self._verify_artifacts(capture, retained_paths)
            if _artifact_digest_map(current_provenance) != _artifact_digest_map(provenance):
                raise EvidenceIntegrityError("capture reservation artifact digest mismatch")
            capture_uid = self.physical_capture_uid(
                backend_id=capture.backend_id,
                csv_sha256=current_provenance["csv"]["sha256"],
            )
            if capture_uid != bundle["physical_capture_uid"]:
                raise EvidenceIntegrityError("capture reservation physical UID mismatch")
            prior_trial = reservations.get(capture_uid)
            if prior_trial not in (None, capture.trial_uid):
                raise EvidenceIntegrityError("duplicate physical capture reservations in bundle store")
            reservations[capture_uid] = capture.trial_uid
        return reservations

    def write_trial_bundle(
        self,
        trial: TrialSpec,
        manifest: CaptureManifest,
        evaluation: Evaluation,
        *,
        artifact_paths: CaptureArtifactPaths,
    ) -> Path:
        if not isinstance(artifact_paths, CaptureArtifactPaths):
            raise EvidenceIntegrityError("artifact_paths must be CaptureArtifactPaths")
        try:
            self._verify_trial_closure(trial, manifest, evaluation)
        except EvidenceIntegrityError as exc:
            quarantine_path = self._quarantine_bundle_failure(
                trial,
                manifest,
                evaluation,
                artifact_paths,
                phase="capture_closure",
                error=exc,
            )
            raise EvidenceIntegrityError(
                f"capture closure failed and was quarantined at {quarantine_path}: {exc}"
            ) from exc
        with _exclusive_lock(self.lock_path):
            # Rehash all three source artifacts while holding the campaign
            # writer lock. No manifest claim is trusted as a byte proof.
            try:
                artifact_provenance = self._verify_artifacts(manifest, artifact_paths)
            except EvidenceIntegrityError as exc:
                quarantine_path = self._quarantine_bundle_failure(
                    trial,
                    manifest,
                    evaluation,
                    artifact_paths,
                    phase="artifact_rehash",
                    error=exc,
                )
                raise EvidenceIntegrityError(
                    f"artifact verification failed and was quarantined at {quarantine_path}: {exc}"
                ) from exc
            capture_uid = self.physical_capture_uid(
                backend_id=trial.backend_id,
                csv_sha256=artifact_provenance["csv"]["sha256"],
            )
            bundle_reservations = self._bundle_capture_reservations()
            prior_bundle_trial = bundle_reservations.get(capture_uid)
            if prior_bundle_trial not in (None, trial.trial_uid):
                raise DuplicateTrialError(
                    "same physical capture is already reserved by another immutable bundle"
                )
            index = self._index()
            prior_capture = index["capture_uids"].get(capture_uid)
            if prior_capture is not None and prior_capture["trial_uid"] != trial.trial_uid:
                raise DuplicateTrialError("same physical capture is already bound to another trial")

            history_rows = self._read_history_unlocked(fail_on_quarantined=True)
            for row in history_rows:
                if (
                    row["physical_capture_uid"] == capture_uid
                    and row["trial_uid"] != trial.trial_uid
                ):
                    raise DuplicateTrialError(
                        "same physical capture is already present under another trial in history"
                    )

            trial_dir = self._register_trial_locked(trial, index)
            history_row = self._history_row(
                trial,
                manifest,
                evaluation,
                capture_uid,
                artifact_provenance,
            )
            bundle = {
                "schema_version": BUNDLE_SCHEMA_VERSION,
                "trial": trial.payload(),
                "capture": asdict(manifest),
                "evaluation": evaluation.history_payload(),
                "physical_capture_uid": capture_uid,
                "history_identity": history_row["history_identity"],
                "artifact_provenance": artifact_provenance,
            }
            bundle_path = trial_dir / "immutable_trial_bundle.json"
            if bundle_path.exists():
                existing = _read_strict_json(bundle_path, role="immutable trial bundle")
                if not isinstance(existing, dict) or canonical_json_bytes(
                    self._bundle_identity(existing)
                ) != canonical_json_bytes(self._bundle_identity(bundle)):
                    raise ImmutableBundleError(
                        "immutable trial bundle already exists with different identity bytes"
                    )
            else:
                _atomic_json(bundle_path, bundle)
            self._append_history_locked(history_row, history_rows)
            index["capture_uids"][capture_uid] = {
                "trial_uid": trial.trial_uid,
                "csv_sha256": artifact_provenance["csv"]["sha256"],
            }
            _atomic_json(self.index_path, index)
        return bundle_path

    def _quarantine_bundle_failure(
        self,
        trial: TrialSpec,
        manifest: CaptureManifest,
        evaluation: Evaluation,
        artifact_paths: CaptureArtifactPaths,
        *,
        phase: str,
        error: Exception,
    ) -> Path:
        return self.quarantine(
            identity=trial.trial_uid,
            reason=f"bundle_{phase}_failed:{type(error).__name__}:{error}",
            evidence={
                "trial_uid": trial.trial_uid,
                "backend_id": trial.backend_id,
                "phase": phase,
                "artifact_paths": {
                    role: str(path.resolve(strict=False))
                    for role, path in artifact_paths.by_role().items()
                },
                "artifact_snapshot": self._artifact_snapshot_for_quarantine(artifact_paths),
                "capture_manifest": asdict(manifest),
                "evaluation": evaluation.history_payload(),
            },
        )

    @staticmethod
    def _artifact_snapshot_for_quarantine(
        artifact_paths: CaptureArtifactPaths,
    ) -> dict[str, dict[str, Any]]:
        snapshot: dict[str, dict[str, Any]] = {}
        for role, path in artifact_paths.by_role().items():
            record: dict[str, Any] = {"path": str(path.resolve(strict=False))}
            try:
                encoded, digest, size_bytes, _ = _read_regular_file_once(path, role=role)
                record.update({"sha256": digest, "size_bytes": size_bytes})
                if role != "csv":
                    try:
                        payload = _strict_json_bytes(encoded, role=f"{role} artifact")
                        record["strict_json_object"] = isinstance(payload, dict)
                    except EvidenceIntegrityError as exc:
                        record["strict_json_object"] = False
                        record["strict_json_error"] = str(exc)
            except EvidenceIntegrityError as exc:
                record["read_error"] = str(exc)
            snapshot[role] = record
        return snapshot

    def _append_history_locked(
        self,
        row: dict[str, Any],
        existing_rows: list[dict[str, Any]],
    ) -> None:
        for existing in existing_rows:
            if existing["trial_uid"] == row["trial_uid"]:
                if existing["history_identity"] != row["history_identity"]:
                    raise ImmutableBundleError("trial history identity changed for an existing trial_uid")
                return
        try:
            _atomic_jsonl(self.history_path, [*existing_rows, row])
        except (OSError, TypeError, ValueError) as exc:
            raise RuntimeError(f"durable history write failed for {self.history_path}: {exc}") from exc

    def quarantine(self, *, identity: str, reason: str, evidence: Any) -> Path:
        require_sha256("quarantine identity", identity)
        if not isinstance(reason, str) or not reason:
            raise ValueError("quarantine reason must be a non-empty string")
        core = {
            "schema_version": QUARANTINE_SCHEMA_VERSION,
            "identity": identity,
            "reason": reason,
            "evidence": evidence,
            "training_eligible": False,
        }
        quarantine_id = sha256_json(core)
        payload = {
            **core,
            "quarantine_id": quarantine_id,
            "quarantined_at": datetime.now(timezone.utc).isoformat(),
        }
        path = self.quarantine_dir / f"{quarantine_id}.json"
        if not path.exists():
            try:
                _atomic_json(path, payload)
            except (OSError, TypeError, ValueError) as exc:
                raise RuntimeError(f"durable evidence quarantine write failed for {path}: {exc}") from exc
        try:
            stored = _read_strict_json(path, role="durable evidence quarantine")
        except EvidenceIntegrityError as exc:
            raise RuntimeError(f"durable evidence quarantine verification failed for {path}: {exc}") from exc
        if (
            not isinstance(stored, dict)
            or set(stored) != {*core, "quarantine_id", "quarantined_at"}
            or stored.get("quarantine_id") != quarantine_id
            or not isinstance(stored.get("quarantined_at"), str)
            or any(stored.get(key) != value for key, value in core.items())
        ):
            raise RuntimeError(f"durable evidence quarantine content mismatch for {path}")
        return path

    def _quarantine_history_line(self, line_number: int, raw_line: str, reason: str) -> Path:
        identity = sha256_json(
            {
                "schema_version": HISTORY_SCHEMA_VERSION,
                "line_number": line_number,
                "raw_line_sha256": hashlib.sha256(
                    raw_line.encode("utf-8", errors="replace")
                ).hexdigest(),
            }
        )
        return self.quarantine(
            identity=identity,
            reason=reason,
            evidence={"line_number": line_number, "raw_line": raw_line},
        )

    def _verify_history_artifacts(
        self,
        row: Mapping[str, Any],
        manifest: CaptureManifest,
    ) -> None:
        provenance = row.get("artifact_provenance")
        if not isinstance(provenance, dict) or set(provenance) != set(ARTIFACT_ROLES):
            raise EvidenceIntegrityError("history artifact provenance roles mismatch")
        opened_file_identities: set[tuple[int, int]] = set()
        for role in ARTIFACT_ROLES:
            reference = provenance[role]
            if not isinstance(reference, dict) or set(reference) != {"path", "sha256", "size_bytes"}:
                raise EvidenceIntegrityError(f"history {role} provenance fields mismatch")
            require_sha256(f"history {role} sha256", reference["sha256"])
            size_bytes = reference["size_bytes"]
            if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
                raise EvidenceIntegrityError(f"history {role} size is invalid")
            path = Path(str(reference["path"]))
            canonical_retained_path = (
                self.root / "artifacts" / "sha256" / reference["sha256"] / role
            )
            if path != canonical_retained_path:
                raise EvidenceIntegrityError(
                    f"history {role} provenance is not the canonical retained snapshot"
                )
            encoded, current_sha256, current_size, file_identity = _read_regular_file_once(
                path,
                role=role,
            )
            if file_identity in opened_file_identities:
                raise EvidenceIntegrityError("history artifact roles do not reference distinct files")
            opened_file_identities.add(file_identity)
            if current_sha256 != reference["sha256"] or current_size != size_bytes:
                raise EvidenceIntegrityError(
                    f"history {role} artifact bytes changed: "
                    f"expected_sha256={reference['sha256']} actual_sha256={current_sha256} "
                    f"expected_size={size_bytes} actual_size={current_size}"
                )
            if role != "csv":
                payload = _strict_json_bytes(encoded, role=f"history {role} artifact")
                self._verify_artifact_semantics(role, payload, manifest)

    def _validate_history_row(self, row: dict[str, Any]) -> None:
        required = {
            "history_schema_version",
            "history_identity",
            "trial_uid",
            "trial_spec_sha256",
            "trial",
            "candidate_uid",
            "comparison_key",
            "backend_id",
            "candidate",
            "execution_profile",
            "plant_epoch",
            "campaign_id",
            "campaign_epoch",
            "campaign_fingerprint",
            "source_fingerprint",
            "config_fingerprint",
            "physical_capture_uid",
            "capture_identity_sha256",
            "artifact_provenance",
            "evaluation",
        }
        if set(row) != required:
            raise EvidenceIntegrityError(
                f"history fields mismatch: missing={sorted(required - set(row))} "
                f"extra={sorted(set(row) - required)}"
            )
        if row["history_schema_version"] != HISTORY_SCHEMA_VERSION:
            raise EvidenceIntegrityError("history schema mismatch")
        for name in (
            "history_identity",
            "trial_uid",
            "trial_spec_sha256",
            "candidate_uid",
            "comparison_key",
            "campaign_fingerprint",
            "source_fingerprint",
            "config_fingerprint",
            "physical_capture_uid",
            "capture_identity_sha256",
        ):
            require_sha256(f"history {name}", row[name])
        if not isinstance(row["backend_id"], str) or not row["backend_id"]:
            raise EvidenceIntegrityError("history backend_id is invalid")
        if not isinstance(row["campaign_id"], str) or not row["campaign_id"]:
            raise EvidenceIntegrityError("history campaign_id is invalid")
        for name in ("plant_epoch", "campaign_epoch"):
            value = row[name]
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise EvidenceIntegrityError(f"history {name} is invalid")
        if not isinstance(row["candidate"], dict) or not isinstance(
            row["execution_profile"], dict
        ):
            raise EvidenceIntegrityError("history candidate/profile is invalid")
        trial_payload = row["trial"]
        if not isinstance(trial_payload, dict):
            raise EvidenceIntegrityError("history trial payload is invalid")
        if sha256_json(trial_payload) != row["trial_spec_sha256"]:
            raise EvidenceIntegrityError("history trial_spec_sha256 mismatch")
        try:
            campaign_payload = dict(trial_payload["campaign"])
            if campaign_payload.get("f0_shadow_reaction_normal_base") is not None:
                campaign_payload["f0_shadow_reaction_normal_base"] = tuple(
                    campaign_payload["f0_shadow_reaction_normal_base"]
                )
            reconstructed_campaign = CampaignSpec(**campaign_payload)
            candidate_payload = trial_payload["candidate"]
            reconstructed_candidate = ForceCandidate(
                target_force_n=candidate_payload["target_force_n"],
                force_p_gain=candidate_payload["force_p_gain"],
                force_i_gain=candidate_payload["force_i_gain"],
                force_damping=candidate_payload["force_damping"],
                orientation_ko=candidate_payload.get("orientation_ko", 0.4),
                motion_kp=candidate_payload.get("motion_kp", 1.5),
                normal_filter_tau_s=candidate_payload.get(
                    "normal_filter_tau_s", 0.35
                ),
            )
            reconstructed_profile = ExecutionProfile(**trial_payload["execution_profile"])
            attestation_payload = trial_payload.get("search_attestation")
            reconstructed_attestation = (
                None
                if attestation_payload is None
                else SearchAttestation.from_payload(attestation_payload)
            )
            reconstructed_trial = TrialSpec(
                campaign=reconstructed_campaign,
                trial_id=trial_payload["trial_id"],
                candidate_token=trial_payload["candidate_token"],
                command_seq=trial_payload["command_seq"],
                plant_epoch=trial_payload["plant_epoch"],
                candidate=reconstructed_candidate,
                execution_profile=reconstructed_profile,
                backend_id=trial_payload["backend_id"],
                source_fingerprint=trial_payload["source_fingerprint"],
                config_fingerprint=trial_payload["config_fingerprint"],
                transition=TrialTransition.from_payload(
                    trial_payload["transition"]
                ),
                search_attestation=reconstructed_attestation,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise EvidenceIntegrityError(f"history trial payload is invalid: {exc}") from exc
        if canonical_json_bytes(reconstructed_trial.payload()) != canonical_json_bytes(trial_payload):
            raise EvidenceIntegrityError("history trial payload is not canonical")
        if any(
            (
                row["trial_uid"] != reconstructed_trial.trial_uid,
                row["candidate_uid"] != reconstructed_candidate.candidate_uid,
                row["comparison_key"] != reconstructed_trial.comparison_key,
                row["backend_id"] != reconstructed_trial.backend_id,
                row["candidate"] != reconstructed_candidate.payload(),
                row["execution_profile"] != reconstructed_profile.payload(),
                row["plant_epoch"] != reconstructed_trial.plant_epoch,
                row["campaign_id"] != reconstructed_campaign.campaign_id,
                row["campaign_epoch"] != reconstructed_campaign.campaign_epoch,
                row["campaign_fingerprint"] != reconstructed_campaign.campaign_fingerprint,
                row["source_fingerprint"] != reconstructed_trial.source_fingerprint,
                row["config_fingerprint"] != reconstructed_trial.config_fingerprint,
            )
        ):
            raise EvidenceIntegrityError("history row differs from its canonical trial identity")

        evaluation = row["evaluation"]
        if not isinstance(evaluation, dict) or evaluation.get("schema_version") != SCHEMA_VERSION:
            raise EvidenceIntegrityError("history evaluation schema mismatch")
        if evaluation.get("trial_uid") != row["trial_uid"]:
            raise EvidenceIntegrityError("history evaluation trial_uid mismatch")
        if evaluation.get("backend_id") != row["backend_id"]:
            raise EvidenceIntegrityError("history evaluation backend mismatch")
        try:
            reconstructed_evaluation = Evaluation(
                trial_uid=evaluation["trial_uid"],
                backend_id=evaluation["backend_id"],
                eligible=evaluation["eligible"],
                disposition=TrialDisposition(evaluation["disposition"]),
                objective_mae_n=evaluation["objective_mae_n"],
                force_bias_n=evaluation["force_bias_n"],
                force_std_n=evaluation["force_std_n"],
                coverage_12_plus_minus_1_ratio=evaluation[
                    "coverage_12_plus_minus_1_ratio"
                ],
                complete_bins=evaluation["complete_bins"],
                safe_closure=evaluation["safe_closure"],
                structural_failures=tuple(evaluation["structural_failures"]),
                metrics=evaluation["metrics"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise EvidenceIntegrityError(f"history evaluation is invalid: {exc}") from exc
        if canonical_json_bytes(reconstructed_evaluation.history_payload()) != canonical_json_bytes(
            evaluation
        ):
            raise EvidenceIntegrityError("history evaluation payload is not canonical")

        capture = self._verify_history_store_artifacts(row)
        self._verify_history_artifacts(row, capture)
        provenance = row["artifact_provenance"]
        expected_capture_uid = self.physical_capture_uid(
            backend_id=row["backend_id"],
            csv_sha256=provenance["csv"]["sha256"],
        )
        if row["physical_capture_uid"] != expected_capture_uid:
            raise EvidenceIntegrityError("history physical_capture_uid mismatch")
        if row["history_identity"] != sha256_json(_history_identity_core(row)):
            raise EvidenceIntegrityError("history_identity mismatch")

    def _verify_history_store_artifacts(self, row: Mapping[str, Any]) -> CaptureManifest:
        trial_dir = self.root / "trials" / str(row["trial_uid"])
        trial_spec = _read_strict_json(trial_dir / "trial_spec.json", role="history trial_spec.json")
        if not isinstance(trial_spec, dict):
            raise EvidenceIntegrityError("history trial_spec.json is not an object")
        trial_spec = dict(trial_spec)
        trial_spec.pop("provenance_run_dir", None)
        if canonical_json_bytes(trial_spec) != canonical_json_bytes(row["trial"]):
            raise EvidenceIntegrityError("history trial_spec.json differs from history trial identity")
        if sha256_json(trial_spec) != row["trial_spec_sha256"]:
            raise EvidenceIntegrityError("history trial_spec.json digest mismatch")

        bundle = _read_strict_json(
            trial_dir / "immutable_trial_bundle.json",
            role="history immutable trial bundle",
        )
        if not isinstance(bundle, dict):
            raise EvidenceIntegrityError("history immutable trial bundle is not an object")
        self._bundle_identity(bundle)
        if bundle["trial"] != row["trial"]:
            raise EvidenceIntegrityError("history immutable bundle trial identity mismatch")
        if bundle["evaluation"] != row["evaluation"]:
            raise EvidenceIntegrityError("history immutable bundle evaluation mismatch")
        if bundle["physical_capture_uid"] != row["physical_capture_uid"]:
            raise EvidenceIntegrityError("history immutable bundle capture UID mismatch")
        if bundle["history_identity"] != row["history_identity"]:
            raise EvidenceIntegrityError("history immutable bundle history identity mismatch")
        if _artifact_digest_map(bundle["artifact_provenance"]) != _artifact_digest_map(
            row["artifact_provenance"]
        ):
            raise EvidenceIntegrityError("history immutable bundle artifact digests mismatch")
        capture_payload = bundle["capture"]
        if not isinstance(capture_payload, dict):
            raise EvidenceIntegrityError("history immutable bundle capture is invalid")
        try:
            capture = CaptureManifest(**capture_payload)
        except (TypeError, ValueError) as exc:
            raise EvidenceIntegrityError(f"history immutable bundle capture is invalid: {exc}") from exc
        if sha256_json(self._capture_identity(capture)) != row["capture_identity_sha256"]:
            raise EvidenceIntegrityError("history immutable bundle capture identity mismatch")
        provenance_digests = _artifact_digest_map(row["artifact_provenance"])
        if any(
            (
                capture.trial_uid != row["trial_uid"],
                capture.backend_id != row["backend_id"],
                capture.candidate_token != row["trial"]["candidate_token"],
                capture.source_fingerprint_pre != row["source_fingerprint"],
                capture.source_fingerprint_post != row["source_fingerprint"],
                capture.config_fingerprint_pre != row["config_fingerprint"],
                capture.config_fingerprint_post != row["config_fingerprint"],
                capture.csv_sha256 != provenance_digests["csv"],
                capture.metadata_sha256 != provenance_digests["metadata"],
                capture.terminal_manifest_sha256 != provenance_digests["terminal_manifest"],
            )
        ):
            raise EvidenceIntegrityError("history immutable bundle capture closure mismatch")
        return capture

    def _read_history_unlocked(
        self,
        *,
        fail_on_quarantined: bool = False,
    ) -> list[dict[str, Any]]:
        if not self.history_path.exists():
            return []
        store_campaign_identity: tuple[str, int, str] | None = None
        if self.campaign_identity_path.exists():
            stored_campaign = _read_strict_json(
                self.campaign_identity_path,
                role="campaign_identity.json",
            )
            if not isinstance(stored_campaign, dict) or set(stored_campaign) != {
                "schema_version",
                "campaign_uid",
                "campaign",
            }:
                raise EvidenceIntegrityError("stored campaign identity fields are invalid")
            if stored_campaign["schema_version"] != CAMPAIGN_IDENTITY_SCHEMA_VERSION:
                raise EvidenceIntegrityError("stored campaign identity schema mismatch")
            require_sha256("stored campaign_uid", stored_campaign["campaign_uid"])
            campaign_payload = stored_campaign["campaign"]
            if not isinstance(campaign_payload, dict) or sha256_json(campaign_payload) != stored_campaign[
                "campaign_uid"
            ]:
                raise EvidenceIntegrityError("stored campaign identity digest mismatch")
            try:
                reconstructed_payload = dict(campaign_payload)
                if reconstructed_payload.get("f0_shadow_reaction_normal_base") is not None:
                    reconstructed_payload["f0_shadow_reaction_normal_base"] = tuple(
                        reconstructed_payload["f0_shadow_reaction_normal_base"]
                    )
                reconstructed_campaign = CampaignSpec(**reconstructed_payload)
            except (TypeError, ValueError) as exc:
                raise EvidenceIntegrityError(f"stored campaign identity is invalid: {exc}") from exc
            if canonical_json_bytes(asdict(reconstructed_campaign)) != canonical_json_bytes(
                campaign_payload
            ):
                raise EvidenceIntegrityError("stored campaign identity is not canonical")
            store_campaign_identity = (
                reconstructed_campaign.campaign_id,
                reconstructed_campaign.campaign_epoch,
                reconstructed_campaign.campaign_fingerprint,
            )
        try:
            encoded, _, _, _ = _read_regular_file_once(self.history_path, role="history.jsonl")
            lines = encoded.decode("utf-8", errors="strict").splitlines()
        except (EvidenceIntegrityError, UnicodeError) as exc:
            identity = sha256_json(
                {
                    "schema_version": HISTORY_SCHEMA_VERSION,
                    "history_file": self.history_path.name,
                    "error": f"{type(exc).__name__}:{exc}",
                }
            )
            quarantine_path = self.quarantine(
                identity=identity,
                reason=f"history_unreadable:{type(exc).__name__}:{exc}",
                evidence={"history_file": self.history_path.name},
            )
            if fail_on_quarantined:
                raise EvidenceIntegrityError(
                    f"history is unreadable and was quarantined at {quarantine_path}"
                ) from exc
            return []

        rows: list[dict[str, Any]] = []
        seen_trial_uids: set[str] = set()
        seen_capture_uids: set[str] = set()
        seen_trial_sources: dict[str, TrialSource] = {}
        campaign_identity: tuple[str, int, str] | None = None
        backend_identity: str | None = None
        quarantined_paths: list[Path] = []
        for line_number, raw_line in enumerate(lines, 1):
            if not raw_line.strip():
                continue
            try:
                payload = _strict_json_loads(raw_line, role=f"history line {line_number}")
                if not isinstance(payload, dict):
                    raise EvidenceIntegrityError("history row is not a JSON object")
                self._validate_history_row(payload)
                trial_payload = payload["trial"]
                transition = TrialTransition.from_payload(
                    trial_payload["transition"]
                )
                transition_policy = transition.policy
                if transition_policy.campaign_start_only:
                    if seen_trial_sources:
                        raise EvidenceIntegrityError(
                            f"{transition.kind.value} transition may occur only at "
                            "campaign history start"
                        )
                elif (
                    transition_policy.source_scope
                    is TrialTransitionSourceScope.CURRENT_HISTORY
                ):
                    if transition.source is None:
                        raise EvidenceIntegrityError(
                            "current-history transition lacks its required source"
                        )
                    if (
                        seen_trial_sources.get(transition.source.trial_uid)
                        != transition.source
                    ):
                        raise EvidenceIntegrityError(
                            "trial transition source is absent or differs from prior history"
                        )
                current_campaign = (
                    payload["campaign_id"],
                    payload["campaign_epoch"],
                    payload["campaign_fingerprint"],
                )
                if store_campaign_identity is None:
                    raise EvidenceIntegrityError("history lacks durable campaign identity binding")
                if current_campaign != store_campaign_identity:
                    raise EvidenceIntegrityError("history differs from stored campaign identity")
                if campaign_identity is not None and current_campaign != campaign_identity:
                    raise EvidenceIntegrityError("history crosses campaign identity/epoch")
                if backend_identity is not None and payload["backend_id"] != backend_identity:
                    raise EvidenceIntegrityError("history crosses backend identity")
                if payload["trial_uid"] in seen_trial_uids:
                    raise EvidenceIntegrityError("duplicate trial_uid in history")
                if payload["physical_capture_uid"] in seen_capture_uids:
                    raise EvidenceIntegrityError("duplicate physical_capture_uid in history")
                campaign_identity = campaign_identity or current_campaign
                backend_identity = backend_identity or payload["backend_id"]
                seen_trial_uids.add(payload["trial_uid"])
                seen_capture_uids.add(payload["physical_capture_uid"])
                candidate_payload = payload["candidate"]
                seen_trial_sources[payload["trial_uid"]] = TrialSource(
                    trial_uid=payload["trial_uid"],
                    candidate=ForceCandidate(
                        target_force_n=candidate_payload["target_force_n"],
                        force_p_gain=candidate_payload["force_p_gain"],
                        force_i_gain=candidate_payload["force_i_gain"],
                        force_damping=candidate_payload["force_damping"],
                        orientation_ko=candidate_payload.get("orientation_ko", 0.4),
                        motion_kp=candidate_payload.get("motion_kp", 1.5),
                        normal_filter_tau_s=candidate_payload.get(
                            "normal_filter_tau_s", 0.35
                        ),
                    ),
                    profile_id=payload["execution_profile"]["profile_id"],
                    plant_epoch=payload["plant_epoch"],
                    campaign_id=payload["campaign_id"],
                    campaign_epoch=payload["campaign_epoch"],
                    campaign_fingerprint=payload["campaign_fingerprint"],
                    backend_id=payload["backend_id"],
                    source_fingerprint=payload["source_fingerprint"],
                    config_fingerprint=payload["config_fingerprint"],
                )
                rows.append(payload)
            except (EvidenceIntegrityError, KeyError, TypeError, ValueError) as exc:
                quarantined_paths.append(
                    self._quarantine_history_line(
                        line_number,
                        raw_line,
                        f"malformed_history:{type(exc).__name__}:{exc}",
                    )
                )
        if fail_on_quarantined and quarantined_paths:
            raise EvidenceIntegrityError(
                "history contains quarantined evidence; refusing another bundle write: "
                + ",".join(str(path) for path in quarantined_paths)
            )
        return rows

    def read_history(self) -> list[dict[str, Any]]:
        """Return only current-byte-verified rows; invalid rows are durably quarantined."""

        with _exclusive_lock(self.lock_path):
            return self._read_history_unlocked()

    def read_resume_history(self) -> list[dict[str, Any]]:
        """Return every verified outcome for recovery; any quarantine fails closed."""

        with _exclusive_lock(self.lock_path):
            return self._read_history_unlocked(fail_on_quarantined=True)

    def read_promotion_history(self) -> list[dict[str, Any]]:
        """Return only verified objective rows; any quarantined row fails closed."""

        with _exclusive_lock(self.lock_path):
            rows = self._read_history_unlocked(fail_on_quarantined=True)
        return [
            row
            for row in rows
            if row["evaluation"]["eligible"] is True
            and row["evaluation"]["disposition"] == TrialDisposition.OBJECTIVE.value
        ]


def cold_read_resume_history_subprocess(
    store_root: Path,
    *,
    timeout_s: float = 30.0,
) -> list[dict[str, Any]]:
    """Verify committed store bytes in a fresh interpreter process."""

    if not isinstance(store_root, Path) or not store_root.is_absolute():
        raise ValueError("store_root must be an absolute pathlib.Path")
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--cold-read-resume-history",
            str(store_root),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    if result.returncode != 0:
        raise EvidenceIntegrityError(
            "independent CampaignStore cold-read failed: "
            + result.stderr.strip()
        )
    try:
        payload = json.loads(result.stdout, parse_constant=_reject_json_constant)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise EvidenceIntegrityError(
            "independent CampaignStore cold-read returned malformed JSON"
        ) from exc
    if not isinstance(payload, list) or any(
        not isinstance(row, dict) for row in payload
    ):
        raise EvidenceIntegrityError(
            "independent CampaignStore cold-read did not return history rows"
        )
    return payload


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="step5d-autotune-store")
    parser.add_argument("--cold-read-resume-history", type=Path)
    args = parser.parse_args(argv)
    if args.cold_read_resume_history is None:
        parser.error("--cold-read-resume-history is required")
    rows = CampaignStore(
        args.cold_read_resume_history.expanduser().absolute()
    ).read_resume_history()
    sys.stdout.write(
        json.dumps(rows, allow_nan=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
