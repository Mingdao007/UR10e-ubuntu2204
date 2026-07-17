"""One-way importer for immutable v1 evidence; never imports active state."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from pathlib import Path
from typing import Any, Mapping

from .model import (
    AttemptTupleSpec,
    BatchSpec,
    CandidateSpec,
    DeploymentSpec,
    canonical_profile_id,
)
from .postprocess import metrics_from_bundle
from .repository import Repository, RepositoryError
from .supervisor import AnalysisResult, ArtifactSeal, CampaignSupervisor


LEGACY_MANIFEST_SCHEMA = "step5d.autotune.legacy-map/v1"
IMPORT_RESULT_SCHEMA = "step5d.autotune.legacy-import/v2"
PROFILE = {
    "normal_max_rate_rad_s": "0.05",
    "host_qdot_slew_rad_s2": "0.5",
    "tp_speedj_accel_rad_s2": "0.5",
    "qdot_cap_rad_s": "0.5",
}


class LegacyImportError(RuntimeError):
    """Raised when old evidence cannot be bound without guessing."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LegacyImportError(f"cannot read legacy JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise LegacyImportError(f"legacy JSON is not an object: {path}")
    return value


def _candidate(row: Mapping[str, Any]) -> CandidateSpec:
    return CandidateSpec.from_mapping(
        {
            key: value
            for key, value in row.items()
            if key
            in {
                "group_id",
                "p",
                "i",
                "d",
                "profile_id",
                "log2_p",
                "log2_i",
                "log2_d",
                "i_multiplier",
            }
        }
    )


def _old_metrics(bundle: Mapping[str, Any]) -> tuple[dict[str, Any], bool, bool]:
    return metrics_from_bundle(bundle)


class _ImportedRuntimePort:
    """Deterministic evidence adapter used only while importing sealed v1 trials."""

    def __init__(self, *, bundle_path: Path, bundle: Mapping[str, Any]) -> None:
        self.bundle_path = bundle_path
        self.bundle = bundle
        self.bundle_sha256 = _sha256(bundle_path)

    def publish_arm(
        self, *, trial_id: str, sequence: int, candidate: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return {"sequence": sequence, "imported": True}

    def wait_tp_consumed(self, *, trial_id: str) -> Mapping[str, Any]:
        return {"imported": True, "legacy_consumed": True}

    def wait_run_started(self, *, trial_id: str) -> Mapping[str, Any]:
        return {"imported": True, "legacy_run": True}

    def wait_home_verified(self, *, trial_id: str) -> Mapping[str, Any]:
        return {"imported": True, "legacy_safe_home": True}

    def seal_raw(self, *, trial_id: str) -> ArtifactSeal:
        return ArtifactSeal(self.bundle_path, self.bundle_sha256)

    def publish_ack(
        self, *, trial_id: str, sequence: int, artifact: ArtifactSeal
    ) -> Mapping[str, Any]:
        return {"sequence": sequence, "imported": True, "legacy_ack": True}

    def wait_ready_home(self, *, trial_id: str) -> Mapping[str, Any]:
        return {"imported": True, "legacy_ready_home": True}

    def analyze(self, *, trial_id: str, artifact: ArtifactSeal) -> AnalysisResult:
        metrics, diagnostic_eligible, objective_eligible = _old_metrics(self.bundle)
        return AnalysisResult(
            metrics=metrics,
            diagnostic_eligible=diagnostic_eligible,
            objective_eligible=objective_eligible,
        )


class LegacyImporter:
    def __init__(
        self,
        repository: Repository,
        *,
        source_root: Path,
        mapping_path: Path,
    ) -> None:
        self.repository = repository
        self.source_root = source_root.resolve()
        self.mapping_path = mapping_path.resolve()

    def run(self, *, output_path: Path) -> dict[str, Any]:
        mapping = _json(self.mapping_path)
        if mapping.get("schema") != LEGACY_MANIFEST_SCHEMA:
            raise LegacyImportError("unknown legacy mapping schema")
        mapping_sha = _sha256(self.mapping_path)
        previous = self.repository.get_metadata("legacy_import_mapping_sha256")
        if previous is not None:
            if previous != mapping_sha:
                raise LegacyImportError("database was already imported with a different mapping")
            if not output_path.is_file():
                raise LegacyImportError(
                    "import is complete but its immutable result manifest is missing"
                )
            expected_result_sha = self.repository.get_metadata(
                "legacy_import_result_sha256"
            )
            if expected_result_sha is None or _sha256(output_path) != expected_result_sha:
                raise LegacyImportError("immutable import result checksum mismatch")
            return _json(output_path)
        batches: list[BatchSpec] = []
        source_rows: dict[str, Mapping[str, Any]] = {}
        for batch_payload in mapping.get("batches", []):
            candidates = tuple(_candidate(row) for row in batch_payload.get("candidates", []))
            batch = BatchSpec(
                batch_id=str(batch_payload.get("batch_id", "")),
                source=str(batch_payload.get("source", "")),
                candidates=candidates,
            )
            batches.append(batch)
            for raw, candidate in zip(batch_payload["candidates"], candidates):
                source_rows[candidate.group_id] = raw
        for batch in batches:
            self._ensure_batch(batch)
        imported_trials: list[dict[str, Any]] = []
        source_checksums: list[dict[str, str]] = []
        for batch in batches:
            for candidate in batch.candidates:
                source = source_rows[candidate.group_id]
                relative = source.get("trial_dir")
                if relative is None:
                    continue
                trial_dir = (self.source_root / str(relative)).resolve()
                try:
                    trial_dir.relative_to(self.source_root)
                except ValueError as exc:
                    raise LegacyImportError("legacy trial path escapes source root") from exc
                trial_path = trial_dir / "trial_spec.json"
                bundle_path = trial_dir / "immutable_trial_bundle.json"
                if not trial_path.is_file() or not bundle_path.is_file():
                    raise LegacyImportError(
                        f"completed legacy group lacks immutable trial files: {candidate.group_id}"
                    )
                trial_payload = _json(trial_path)
                bundle = _json(bundle_path)
                trial_uid = str(trial_payload.get("trial_uid", ""))
                if trial_uid != source.get("trial_uid"):
                    raise LegacyImportError(f"legacy trial identity drift: {candidate.group_id}")
                old_candidate = trial_payload.get("candidate") or {}
                old_spec = CandidateSpec(
                    group_id=candidate.group_id,
                    p=str(old_candidate.get("force_p_gain")),
                    i=str(old_candidate.get("force_i_gain")),
                    d=str(old_candidate.get("force_damping")),
                    profile_id=candidate.profile_id,
                )
                actual_tuple = (old_spec.p, old_spec.i, old_spec.d)
                if actual_tuple != (candidate.p, candidate.i, candidate.d):
                    raise LegacyImportError(
                        f"legacy candidate tuple drift for {candidate.group_id}: {actual_tuple}"
                    )
                source_fingerprint = str(trial_payload.get("source_fingerprint", ""))
                guard_fingerprint = str(trial_payload.get("config_fingerprint", ""))
                tp_fingerprint = str(mapping.get("tp_fingerprint", ""))
                deployment_id = (
                    f"legacy-{source_fingerprint[:12]}-{guard_fingerprint[:12]}"
                )
                deployment = DeploymentSpec(
                    deployment_id=deployment_id,
                    code_fingerprint=source_fingerprint,
                    tp_fingerprint=tp_fingerprint,
                    guard_fingerprint=guard_fingerprint,
                    profile=PROFILE,
                    deployment_authorized=True,
                    controller_readback_verified=True,
                )
                self.repository.register_deployment(deployment)
                bundle_sha = _sha256(bundle_path)
                try:
                    detail = self.repository.trial_detail(trial_uid)
                except RepositoryError:
                    trial_id = self.repository.create_trial(
                        candidate_id=candidate.candidate_id,
                        deployment_id=deployment_id,
                        trial_id=trial_uid,
                    )
                else:
                    trial_id = trial_uid
                    if (
                        detail["candidate_id"] != candidate.candidate_id
                        or detail["deployment_id"] != deployment_id
                    ):
                        raise LegacyImportError(
                            f"legacy trial already binds different identity: {trial_uid}"
                        )
                outcome = CampaignSupervisor(
                    self.repository,
                    _ImportedRuntimePort(bundle_path=bundle_path, bundle=bundle),
                ).resume_trial(trial_id)
                if outcome.state != "complete":
                    raise LegacyImportError(
                        f"legacy trial did not reach complete: {candidate.group_id}:{outcome.state}"
                    )
                source_checksums.extend(
                    (
                        {"path": str(trial_path), "sha256": _sha256(trial_path)},
                        {"path": str(bundle_path), "sha256": bundle_sha},
                    )
                )
                imported_trials.append(
                    {
                        "group_id": candidate.group_id,
                        "trial_id": trial_id,
                        "comparison_key": candidate.comparison_key,
                        "bundle_sha256": bundle_sha,
                    }
                )
        tombstones = self._import_partial_attempts()
        incumbent = self.repository.historical_diagnostic_reference(
            profile_id=canonical_profile_id(PROFILE), group_id=None
        )
        result = {
            "schema": IMPORT_RESULT_SCHEMA,
            "mapping": {"path": str(self.mapping_path), "sha256": mapping_sha},
            "source_root": str(self.source_root),
            "source_files": sorted(source_checksums, key=lambda row: row["path"]),
            "imported_trials": imported_trials,
            "attempt_tombstones": tombstones,
            "pending_groups": [
                row["group_id"]
                for row in self.repository.list_candidates()
                if row["status"] == "pending"
            ],
            "diagnostic_incumbent": (
                None
                if incumbent is None
                else {
                    "group_id": incumbent["group_id"],
                    "trial_id": incumbent["trial_id"],
                    "force_mae_n": incumbent["force_mae_n"],
                }
            ),
        }
        self._atomic_result(output_path, result)
        self.repository.set_metadata("legacy_import_result_sha256", _sha256(output_path))
        # This is the completion marker and therefore must be committed last.
        self.repository.set_metadata("legacy_import_mapping_sha256", mapping_sha)
        return result

    def _ensure_batch(self, batch: BatchSpec) -> None:
        detail = self.repository.batch_detail(batch.batch_id)
        if detail is None:
            self.repository.enqueue_batch(batch)
            return
        actual_candidates = self.repository.list_candidates(batch_id=batch.batch_id)
        actual_ids = {row["candidate_id"] for row in actual_candidates}
        expected_ids = {candidate.candidate_id for candidate in batch.candidates}
        if (
            detail["source"] != batch.source
            or detail["recovery"] is not batch.recovery
            or tuple(detail["completed_groups"]) != batch.completed_groups
            or actual_ids != expected_ids
            or len(actual_candidates) != len(batch.candidates)
        ):
            raise LegacyImportError(
                f"existing batch conflicts with legacy mapping: {batch.batch_id}"
            )

    def _import_partial_attempts(self) -> list[dict[str, str]]:
        partial_by_uid: dict[str, Path] = {}
        for name in ("capture.csv.part", "capture.csv"):
            for path in self.source_root.rglob(name):
                if path.parent.name and len(path.parent.name) == 64:
                    partial_by_uid.setdefault(path.parent.name, path)
        imported: list[dict[str, str]] = []
        seen: set[str] = set()
        for trial_path in self.source_root.rglob("trial_spec.json"):
            trial = _json(trial_path)
            uid = str(trial.get("trial_uid", ""))
            evidence = partial_by_uid.get(uid)
            if evidence is None:
                continue
            old = trial.get("candidate") or {}
            try:
                candidate = AttemptTupleSpec(
                    p=str(old["force_p_gain"]),
                    i=str(old["force_i_gain"]),
                    d=str(old["force_damping"]),
                )
            except (KeyError, ValueError):
                continue
            if candidate.comparison_key in seen:
                continue
            seen.add(candidate.comparison_key)
            digest = _sha256(evidence)
            retained = self.repository.add_attempt_tombstone(
                candidate=candidate,
                disposition="uncertain_attempt" if evidence.name.endswith(".part") else "physical_capture_without_bundle",
                evidence_path=evidence,
                evidence_sha256=digest,
            )
            if not retained:
                continue
            imported.append(
                {
                    "comparison_key": candidate.comparison_key,
                    "evidence_path": str(evidence),
                    "evidence_sha256": digest,
                }
            )
        return imported

    @staticmethod
    def _atomic_result(path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
        if path.exists() or path.is_symlink():
            if path.is_symlink() or path.read_text(encoding="utf-8") != encoded:
                raise LegacyImportError("immutable import result already differs")
            return
        temporary = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
