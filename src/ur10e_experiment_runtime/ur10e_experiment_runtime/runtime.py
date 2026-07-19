"""Validation, planning, and offline-only run scaffolding."""

from __future__ import annotations

import hashlib
import fcntl
import os
import secrets
import stat
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping

from jsonschema import Draft202012Validator

from .contracts import (
    AutotuneCandidate,
    ExperimentSpec,
    OutputPathError,
    ResourceLockError,
    RunManifest,
    SpecValidationError,
    UnsupportedExecutionError,
)
from .identity import (
    canonical_json_bytes,
    canonical_sha256,
    load_strict_json,
    strict_json_loads,
)
from .registry import ComponentKind, ComponentRegistry, build_default_registry


_SCHEMA_DIRECTORY = Path(__file__).with_name("schemas")
_SHA256_LENGTH = 64
_ZERO_SHA256 = "0" * _SHA256_LENGTH
_RUNNABLE_SCAFFOLD_CEILINGS = frozenset({"none", "simulation"})
_EXCLUSIVE_RESOURCE_IDS = frozenset(
    {"live_writer", "visible_gazebo", "formal_timing"}
)


def _default_host_lock_root() -> Path:
    return Path("/tmp") / f"ur10e-experiment-runtime-locks-{os.getuid()}"


class HostResourceLock:
    """Non-blocking host-local ``flock`` for governed exclusive resources."""

    def __init__(self, resource_id: str, *, lock_root: str | Path | None = None):
        if resource_id not in _EXCLUSIVE_RESOURCE_IDS:
            raise ResourceLockError(f"unknown exclusive resource: {resource_id!r}")
        self.resource_id = resource_id
        self.lock_root = (
            Path(lock_root)
            if lock_root is not None
            else _default_host_lock_root()
        )
        self.path = self.lock_root / f"{resource_id}.lock"
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> "HostResourceLock":
        if self.held:
            raise ResourceLockError(f"resource lock is already held: {self.resource_id}")
        if self.lock_root.is_symlink():
            raise ResourceLockError("resource lock root must not be a symlink")
        try:
            self.lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(self.path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                fd = os.open(self.path, flags)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                os.close(fd)
                raise ResourceLockError(
                    f"exclusive resource is already locked: {self.resource_id}"
                ) from exc
        except ResourceLockError:
            raise
        except OSError as exc:
            raise ResourceLockError(
                f"cannot acquire resource lock {self.resource_id}: {exc}"
            ) from exc
        self._fd = fd
        return self

    def release(self) -> None:
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> "HostResourceLock":
        return self.acquire()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self.release()


def _load_schema(filename: str) -> Mapping[str, Any]:
    schema = load_strict_json(_SCHEMA_DIRECTORY / filename)
    if not isinstance(schema, Mapping):
        raise SpecValidationError(f"schema {filename} must be a JSON object")
    Draft202012Validator.check_schema(schema)
    return schema


def _format_json_path(path: Any) -> str:
    rendered = "$"
    for part in path:
        rendered += f"[{part}]" if isinstance(part, int) else f".{part}"
    return rendered


def _validate_schema(
    document: Mapping[str, Any],
    schema_filename: str,
    document_name: str,
) -> None:
    validator = Draft202012Validator(_load_schema(schema_filename))
    errors = sorted(
        validator.iter_errors(document),
        key=lambda error: (tuple(str(part) for part in error.absolute_path), error.message),
    )
    if errors:
        details = "; ".join(
            f"{_format_json_path(error.absolute_path)}: {error.message}"
            for error in errors[:8]
        )
        if len(errors) > 8:
            details += f"; ... {len(errors) - 8} more error(s)"
        raise SpecValidationError(f"invalid {document_name}: {details}")


def _is_relative_artifact_path(value: str) -> bool:
    path = PurePosixPath(value.replace("\\", "/"))
    return not path.is_absolute() and ".." not in path.parts and bool(path.parts)


def _validate_experiment_semantics(document: Mapping[str, Any]) -> None:
    objective = document["objective"]
    start_s, end_s = objective["window_s"]
    if end_s <= start_s:
        raise SpecValidationError("objective.window_s end must be greater than start")

    readiness = document["readiness"]
    if readiness["calibration_complete"] and readiness["evidence_sha256"] is None:
        raise SpecValidationError(
            "completed calibration requires readiness.evidence_sha256"
        )
    if (
        readiness["calibration_complete"]
        and document["surface_contract"]["calibration_sha256"] is None
    ):
        raise SpecValidationError(
            "completed calibration requires surface_contract.calibration_sha256"
        )

    exclusive_resources = set(document["resource_contract"]["exclusive_resources"])
    for lane_id, lane in document["lanes"].items():
        resources = set(lane["resources"])
        motion_ceiling = lane["motion_ceiling"]
        backend_id = lane["backend_id"]
        if motion_ceiling == "live_motion" and "live_writer" not in resources:
            raise SpecValidationError(
                f"lane {lane_id!r} permits live motion without live_writer"
            )
        for resource in resources & {"visible_gazebo", "formal_timing", "live_writer"}:
            if resource not in exclusive_resources:
                raise SpecValidationError(
                    f"lane {lane_id!r} uses {resource!r} without declaring it exclusive"
                )
        if backend_id == "hil_scaffold_v1" and motion_ceiling != "no_motion":
            raise SpecValidationError("hil_scaffold_v1 requires motion_ceiling=no_motion")
        if backend_id == "live_autotune_scaffold_v1":
            if motion_ceiling != "live_motion" or "live_writer" not in resources:
                raise SpecValidationError(
                    "live_autotune_scaffold_v1 requires live_motion and live_writer"
                )
        if backend_id in {"gazebo_scaffold_v1", "ursim_scaffold_v1"}:
            if motion_ceiling != "simulation":
                raise SpecValidationError(
                    f"{backend_id} requires motion_ceiling=simulation"
                )

    for record in document["legacy_provenance"]:
        if not _is_relative_artifact_path(record["artifact_path"]):
            raise SpecValidationError(
                "legacy_provenance artifact_path must be relative and traversal-free"
            )

    warm_start = document["warm_start"]
    if warm_start is not None:
        if warm_start["source_experiment_id"] == document["experiment_id"]:
            raise SpecValidationError("warm_start must name a different experiment")


def validate_experiment_spec(
    document: Mapping[str, Any],
    *,
    registry: ComponentRegistry | None = None,
    source_path: str | Path | None = None,
) -> ExperimentSpec:
    """Validate a mapping and return an immutable, identity-bound spec."""

    if not isinstance(document, Mapping):
        raise SpecValidationError("ExperimentSpec must be a JSON object")
    canonical_document = canonical_json_bytes(document)
    normalized = strict_json_loads(canonical_document)
    _validate_schema(normalized, "experiment_spec.schema.json", "ExperimentSpec")
    _validate_experiment_semantics(normalized)
    active_registry = registry or build_default_registry()
    active_registry.validate_spec(normalized)
    return ExperimentSpec(
        _canonical_document=canonical_document,
        fingerprint=canonical_sha256(normalized),
        source_path=Path(source_path) if source_path is not None else None,
    )


def load_experiment_spec(
    path: str | Path,
    *,
    registry: ComponentRegistry | None = None,
) -> ExperimentSpec:
    source = Path(path)
    document = load_strict_json(source)
    if not isinstance(document, Mapping):
        raise SpecValidationError("ExperimentSpec must be a JSON object")
    return validate_experiment_spec(
        document,
        registry=registry,
        source_path=source,
    )


def validate_run_manifest(
    document: Mapping[str, Any],
    *,
    source_path: str | Path | None = None,
) -> RunManifest:
    if not isinstance(document, Mapping):
        raise SpecValidationError("RunManifest must be a JSON object")
    canonical_document = canonical_json_bytes(document)
    normalized = strict_json_loads(canonical_document)
    _validate_schema(normalized, "run_manifest.schema.json", "RunManifest")
    if normalized["run_uid"] != normalized["run_identity_sha256"]:
        raise SpecValidationError("run_uid must equal run_identity_sha256")
    expected_run_uid = canonical_sha256(
        {
            "schema": "ur10e.run_identity/v1",
            "experiment_fingerprint": normalized["experiment_fingerprint"],
            "lane": normalized["lane"],
            "candidate": normalized["candidate"],
            "nonce": normalized["run_nonce"],
        }
    )
    if normalized["run_uid"] != expected_run_uid:
        raise SpecValidationError("run_uid does not match its canonical identity payload")
    if normalized["optimizer_eligible"]:
        required_optimizer_facts = {
            "valid_outcome": normalized["outcome"]["classification"]
            == "valid_parameter_observation",
            "trainable_metric": normalized["metric_role"]
            == "trainable_objective",
            "objective_present": normalized["objective"] is not None,
            "overlay_present": normalized["trial_overlay"] is not None,
            "candidate_uid_present": normalized["control_candidate_uid"] is not None,
            "fingerprint_closed": normalized["fingerprints"]["pre"]
            == normalized["fingerprints"]["post"],
            "controller_readback": normalized["controller_readback"]["verified"]
            and normalized["controller_readback"]["sha256"] is not None,
            "credible_oracle": normalized["oracle_status"] == "credible",
            "complete_observer": normalized["observer_status"] == "complete",
            "exact_ack": normalized["ack"]["consumed"]
            and normalized["ack"]["ack_uid"] is not None
            and normalized["ack"]["receipt_sha256"] is not None,
            "post_ack_closure": normalized["closure"]["post_ack_verified"]
            and normalized["closure"]["receipt_sha256"] is not None,
            "unique_publication": normalized["publication"]["unique"]
            and normalized["publication"]["identity_sha256"] is not None,
        }
        missing = sorted(
            name for name, satisfied in required_optimizer_facts.items() if not satisfied
        )
        if missing:
            raise SpecValidationError(
                "optimizer-eligible manifest lacks required facts: " + ", ".join(missing)
            )
    elif normalized["objective"] is not None:
        raise SpecValidationError(
            "optimizer-ineligible manifest must set objective to null"
        )
    if normalized["metric_role"] != "trainable_objective" and normalized["objective"] is not None:
        raise SpecValidationError("non-trainable metric role must set objective to null")
    if normalized["ack"]["consumed"] != (
        normalized["ack"]["ack_uid"] is not None
        and normalized["ack"]["receipt_sha256"] is not None
    ):
        raise SpecValidationError("ACK fields must be all present only when consumed")
    if normalized["closure"]["post_ack_verified"] != (
        normalized["closure"]["receipt_sha256"] is not None
    ):
        raise SpecValidationError("closure receipt must match post-ACK verification")
    if normalized["publication"]["unique"] != (
        normalized["publication"]["identity_sha256"] is not None
    ):
        raise SpecValidationError("publication identity must match uniqueness state")
    for artifact in normalized["artifacts"]:
        if not _is_relative_artifact_path(artifact["path"]):
            raise SpecValidationError(
                "RunManifest artifact path must be relative and traversal-free"
            )
    return RunManifest(
        _canonical_document=canonical_document,
        source_path=Path(source_path) if source_path is not None else None,
    )


def load_run_manifest(path: str | Path) -> RunManifest:
    source = Path(path)
    document = load_strict_json(source)
    if not isinstance(document, Mapping):
        raise SpecValidationError("RunManifest must be a JSON object")
    return validate_run_manifest(document, source_path=source)


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def validate_parallel_run_manifest(document: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one concurrency receipt without adding it to run identity."""

    normalized = strict_json_loads(canonical_json_bytes(document))
    _validate_schema(
        normalized,
        "parallel_run_manifest.schema.json",
        "ParallelRunManifest",
    )
    if normalized["ended_at"] < normalized["started_at"]:
        raise SpecValidationError("parallel run ended_at precedes started_at")
    for output_path in normalized["output_paths"]:
        if not _is_relative_artifact_path(output_path):
            raise SpecValidationError(
                "parallel run output paths must be relative and traversal-free"
            )
    return normalized


def plan_experiment(
    spec: ExperimentSpec,
    lane_id: str,
    *,
    registry: ComponentRegistry | None = None,
) -> dict[str, Any]:
    """Return a deterministic plan; never start a backend or contact hardware."""

    active_registry = registry or build_default_registry()
    active_registry.validate_spec(spec.document)
    lane = spec.lane(lane_id)
    backend_id = str(lane["backend_id"])
    motion_ceiling = str(lane["motion_ceiling"])
    reasons: list[str] = []
    if lane_id == "hil" or backend_id == "hil_scaffold_v1":
        reasons.append("hil_execution_not_implemented")
    if lane_id == "live_autotune" or backend_id == "live_autotune_scaffold_v1":
        reasons.append("live_execution_not_implemented")
    if motion_ceiling not in _RUNNABLE_SCAFFOLD_CEILINGS:
        reasons.append(f"motion_ceiling_not_offline:{motion_ceiling}")
    readiness = spec.readiness
    if (
        motion_ceiling == "live_motion"
        and readiness["calibration_required"]
        and not readiness["calibration_complete"]
    ):
        reasons.append("calibration_incomplete")
    if motion_ceiling == "live_motion":
        if spec.document["surface_contract"]["calibration_sha256"] is None:
            reasons.append("surface_calibration_unbound")
        if spec.document["surface_contract"]["workspace_cage_sha256"] is None:
            reasons.append("workspace_cage_unbound")
        if spec.document["safety_contract"]["policy_sha256"] is None:
            reasons.append("safety_policy_unbound")

    adapter_ref = spec.components["stage_adapter"]
    adapter = active_registry.resolve(
        ComponentKind.STAGE_ADAPTER,
        str(adapter_ref["id"]),
        str(adapter_ref["version"]),
    )
    adapter_plan_fn = getattr(adapter, "plan", None)
    adapter_plan: Mapping[str, Any] = {}
    if callable(adapter_plan_fn):
        try:
            candidate_plan = adapter_plan_fn(spec, lane_id)
        except Exception as exc:
            raise SpecValidationError(f"stage adapter plan failed: {exc}") from exc
        if not isinstance(candidate_plan, Mapping):
            raise SpecValidationError("stage adapter plan must return a mapping")
        adapter_plan = candidate_plan

    return {
        "schema": "ur10e.experiment_plan/v1",
        "experiment_id": spec.experiment_id,
        "experiment_fingerprint": spec.fingerprint,
        "lane": lane_id,
        "backend_id": backend_id,
        "motion_ceiling": motion_ceiling,
        "resources": list(lane["resources"]),
        "mode": "offline_scaffold_only",
        "executable": not reasons,
        "blocked_reasons": reasons,
        "adapter_plan": dict(adapter_plan),
        "external_actions": [],
    }


def create_exclusive_run_directory(
    output_root: str | Path,
    experiment_fingerprint: str,
    run_uid: str,
) -> Path:
    """Claim one full-digest output path without overwriting prior evidence."""

    for name, value in (
        ("experiment_fingerprint", experiment_fingerprint),
        ("run_uid", run_uid),
    ):
        if len(value) != _SHA256_LENGTH or any(ch not in "0123456789abcdef" for ch in value):
            raise OutputPathError(f"{name} must be a lowercase full SHA256 digest")
    root = Path(output_root)
    if root.is_symlink():
        raise OutputPathError("output root must not be a symlink")
    try:
        root.mkdir(parents=True, exist_ok=True)
        experiment_root = root / experiment_fingerprint
        if experiment_root.is_symlink():
            raise OutputPathError("experiment output root must not be a symlink")
        experiment_root.mkdir(exist_ok=True)
        run_root = experiment_root / run_uid
        run_root.mkdir(exist_ok=False)
        _fsync_directory(run_root.parent)
        _fsync_directory(experiment_root.parent)
    except FileExistsError as exc:
        raise OutputPathError(
            f"run output directory already exists: {experiment_fingerprint}/{run_uid}"
        ) from exc
    except OutputPathError:
        raise
    except OSError as exc:
        raise OutputPathError(f"cannot create exclusive run output: {exc}") from exc
    return run_root


def _candidate_document(
    candidate: AutotuneCandidate | Mapping[str, Any] | None,
) -> dict[str, float] | None:
    if candidate is None:
        return None
    if isinstance(candidate, AutotuneCandidate):
        return candidate.to_dict()
    try:
        normalized = AutotuneCandidate(
            p_gain=candidate["p_gain"],
            i_gain=candidate["i_gain"],
            damping=candidate["damping"],
        )
    except (KeyError, TypeError) as exc:
        raise SpecValidationError(
            "candidate must contain only p_gain, i_gain, and damping"
        ) from exc
    if set(candidate) != {"p_gain", "i_gain", "damping"}:
        raise SpecValidationError(
            "candidate must contain only p_gain, i_gain, and damping"
        )
    return normalized.to_dict()


def _write_exclusive_json(path: Path, document: Mapping[str, Any]) -> None:
    payload = canonical_json_bytes(document) + b"\n"
    try:
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            _write_all(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        _fsync_directory(path.parent)
    except FileExistsError as exc:
        raise OutputPathError(f"refusing to overwrite immutable artifact: {path.name}") from exc
    except OSError as exc:
        raise OutputPathError(f"cannot write immutable artifact {path.name}: {exc}") from exc


def _write_all(fd: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise OSError("short write made no progress")
        remaining = remaining[written:]


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def _run_evidence_lock(
    root: Path,
    lock_root: str | Path | None = None,
) -> Iterator[None]:
    namespace = (
        Path(lock_root) if lock_root is not None else _default_host_lock_root()
    ) / "evidence"
    if namespace.is_symlink():
        raise OutputPathError("evidence lock namespace must not be a symlink")
    namespace.mkdir(parents=True, exist_ok=True, mode=0o700)
    run_path_digest = hashlib.sha256(
        os.fsencode(str(root.resolve(strict=True)))
    ).hexdigest()
    lock_path = namespace / f"{run_path_digest}.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock_path, flags, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
    except OutputPathError:
        raise
    except OSError as exc:
        raise OutputPathError(f"cannot lock run evidence: {exc}") from exc


def append_run_state_event(
    run_root: str | Path,
    event: Mapping[str, Any],
    *,
    evidence_lock_root: str | Path | None = None,
) -> Path:
    """Append one sequence-numbered, hash-chained event until finalization."""

    root = Path(run_root)
    if root.is_symlink() or not root.is_dir():
        raise OutputPathError("run root must be a real directory")
    state_path = root / "run_state.jsonl"
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        with _run_evidence_lock(root, evidence_lock_root):
            if (root / "run_manifest.json").exists():
                raise OutputPathError(
                    "run_state.jsonl is closed after final manifest publication"
                )
            records = _verify_state_path(state_path, missing_ok=True)
            previous_sha256 = (
                records[-1]["record_sha256"] if records else _ZERO_SHA256
            )
            body = {
                "schema": "ur10e.run_state_record/v1",
                "sequence": len(records),
                "previous_sha256": previous_sha256,
                "event": dict(event),
            }
            record = {**body, "record_sha256": canonical_sha256(body)}
            payload = canonical_json_bytes(record) + b"\n"
            existed = state_path.exists()
            fd = os.open(state_path, flags, 0o600)
            try:
                _write_all(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
            if not existed:
                _fsync_directory(root)
    except OutputPathError:
        raise
    except OSError as exc:
        raise OutputPathError(f"cannot append run state event: {exc}") from exc
    return state_path


def _verify_state_path(
    state_path: Path, *, missing_ok: bool = False
) -> tuple[dict[str, Any], ...]:
    if not state_path.exists():
        if missing_ok:
            return ()
        raise OutputPathError("run_state.jsonl is missing")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(state_path, flags)
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise OutputPathError("run_state.jsonl must be a no-follow regular file")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                raw_lines = tuple(stream)
        finally:
            os.close(fd)
    except OutputPathError:
        raise
    except OSError as exc:
        raise OutputPathError(f"cannot read run_state.jsonl: {exc}") from exc

    records: list[dict[str, Any]] = []
    previous_sha256 = _ZERO_SHA256
    required_keys = {
        "schema",
        "sequence",
        "previous_sha256",
        "event",
        "record_sha256",
    }
    for line_number, raw_line in enumerate(raw_lines, start=1):
        if not raw_line.strip():
            raise OutputPathError(
                f"run_state.jsonl contains an empty record at line {line_number}"
            )
        try:
            record = strict_json_loads(raw_line)
        except Exception as exc:
            raise OutputPathError(
                f"invalid run_state.jsonl record at line {line_number}: {exc}"
            ) from exc
        if not isinstance(record, dict) or set(record) != required_keys:
            raise OutputPathError(
                f"invalid run_state.jsonl record shape at line {line_number}"
            )
        if record["schema"] != "ur10e.run_state_record/v1":
            raise OutputPathError(
                f"unknown run_state.jsonl schema at line {line_number}"
            )
        if record["sequence"] != len(records):
            raise OutputPathError(
                f"non-contiguous run_state.jsonl sequence at line {line_number}"
            )
        if record["previous_sha256"] != previous_sha256:
            raise OutputPathError(
                f"broken run_state.jsonl previous hash at line {line_number}"
            )
        if not isinstance(record["event"], dict):
            raise OutputPathError(
                f"run_state.jsonl event must be an object at line {line_number}"
            )
        body = {key: record[key] for key in required_keys - {"record_sha256"}}
        expected_sha256 = canonical_sha256(body)
        if record["record_sha256"] != expected_sha256:
            raise OutputPathError(
                f"broken run_state.jsonl record hash at line {line_number}"
            )
        previous_sha256 = expected_sha256
        records.append(record)
    return tuple(records)


def verify_run_state_chain(run_root: str | Path) -> tuple[dict[str, Any], ...]:
    """Verify and return the canonical state chain for crash-safe resume."""

    root = Path(run_root)
    if root.is_symlink() or not root.is_dir():
        raise OutputPathError("run root must be a real directory")
    return _verify_state_path(root / "run_state.jsonl")


def reconstruct_run_state(run_root: str | Path) -> dict[str, Any]:
    """Reconstruct durable state without consulting mutable pointers."""

    root = Path(run_root)
    records = verify_run_state_chain(root)
    return {
        "schema": "ur10e.run_state_reconstruction/v1",
        "record_count": len(records),
        "head_sha256": records[-1]["record_sha256"] if records else _ZERO_SHA256,
        "events": [record["event"] for record in records],
        "final_manifest_present": (root / "run_manifest.json").is_file(),
    }


def finalize_run_manifest(
    run_root: str | Path,
    document: Mapping[str, Any],
    *,
    evidence_lock_root: str | Path | None = None,
) -> RunManifest:
    """Close append-only state and publish one manifest under one evidence lock."""

    root = Path(run_root)
    if root.is_symlink() or not root.is_dir():
        raise OutputPathError("run root must be a real directory")
    manifest_path = root / "run_manifest.json"
    state_path = root / "run_state.jsonl"
    with _run_evidence_lock(root, evidence_lock_root):
        if manifest_path.exists():
            raise OutputPathError("final run manifest already exists")
        if state_path.is_symlink() or not state_path.is_file():
            raise OutputPathError("run_state.jsonl must exist before finalization")
        records = _verify_state_path(state_path)
        state_sha256 = hashlib.sha256(state_path.read_bytes()).hexdigest()
        normalized = strict_json_loads(canonical_json_bytes(document))
        normalized["state_chain"] = {
            "record_count": len(records),
            "head_sha256": records[-1]["record_sha256"],
        }
        artifacts = normalized.get("artifacts", [])
        state_artifacts = [
            artifact
            for artifact in artifacts
            if artifact.get("kind") == "run_state_jsonl"
            and artifact.get("path") == "run_state.jsonl"
        ]
        if len(state_artifacts) != 1:
            raise SpecValidationError(
                "RunManifest requires exactly one run_state_jsonl artifact"
            )
        state_artifacts[0]["sha256"] = state_sha256
        manifest = validate_run_manifest(normalized)
        _write_exclusive_json(manifest_path, manifest.document)
    return RunManifest(
        _canonical_document=manifest._canonical_document,
        source_path=manifest_path,
    )


def run_experiment(
    spec: ExperimentSpec,
    lane_id: str,
    *,
    output_root: str | Path,
    candidate: AutotuneCandidate | Mapping[str, Any] | None = None,
    registry: ComponentRegistry | None = None,
    resource_lock_root: str | Path | None = None,
) -> RunManifest:
    """Create offline scaffold evidence without invoking any backend."""

    active_registry = registry or build_default_registry()
    plan = plan_experiment(spec, lane_id, registry=active_registry)
    if not plan["executable"]:
        reasons = ", ".join(plan["blocked_reasons"])
        raise UnsupportedExecutionError(
            f"lane {lane_id!r} is fail-closed in offline scaffolding: {reasons}"
        )

    lane = spec.lane(lane_id)
    requested_resources = sorted(
        set(lane["resources"]) & _EXCLUSIVE_RESOURCE_IDS
    )
    acquired_resources: list[str] = []
    started_at = _utc_timestamp()
    with ExitStack() as resource_locks:
        for resource_id in requested_resources:
            resource_locks.enter_context(
                HostResourceLock(resource_id, lock_root=resource_lock_root)
            )
            acquired_resources.append(resource_id)

        candidate_document = _candidate_document(candidate)
        run_nonce = secrets.token_hex(32)
        run_identity = {
            "schema": "ur10e.run_identity/v1",
            "experiment_fingerprint": spec.fingerprint,
            "lane": lane_id,
            "candidate": candidate_document,
            "nonce": run_nonce,
        }
        run_uid = canonical_sha256(run_identity)
        run_root = create_exclusive_run_directory(
            output_root, spec.fingerprint, run_uid
        )

        acquisition_receipt = {
            "schema": "ur10e.resource_lock_receipt/v1",
            "phase": "acquired",
            "run_uid": run_uid,
            "resources": acquired_resources,
        }
        acquisition_sha256 = canonical_sha256(acquisition_receipt)
        state_event = {
            "schema": "ur10e.run_state_event/v1",
            "event": "offline_scaffold_created",
            "experiment_fingerprint": spec.fingerprint,
            "run_uid": run_uid,
            "lane": lane_id,
            "resource_locks_acquired": acquired_resources,
            "resource_lock_acquisition_sha256": acquisition_sha256,
            "optimizer_eligible": False,
            "external_actions": [],
        }
        append_run_state_event(
            run_root,
            state_event,
            evidence_lock_root=resource_lock_root,
        )

    release_receipt = {
        "schema": "ur10e.resource_lock_receipt/v1",
        "phase": "released",
        "run_uid": run_uid,
        "resources": acquired_resources,
        "acquisition_sha256": acquisition_sha256,
    }
    release_sha256 = canonical_sha256(release_receipt)
    state_path = append_run_state_event(
        run_root,
        {
            "schema": "ur10e.run_state_event/v1",
            "event": "resource_locks_released",
            "run_uid": run_uid,
            "lane": lane_id,
            "resource_locks_released": acquired_resources,
            "resource_lock_release_sha256": release_sha256,
            "external_actions": [],
        },
        evidence_lock_root=resource_lock_root,
    )
    ended_at = _utc_timestamp()
    source_fingerprint = str(spec.bindings["source_sha256"])
    parallel_document = validate_parallel_run_manifest(
        {
            "schema": "ur10e.parallel_run_manifest/v1",
            "contract": {
                "id": str(spec.document["resource_contract"]["id"]),
                "source_sha256": str(
                    spec.document["resource_contract"]["source_sha256"]
                ),
            },
            "task": run_uid,
            "dependencies": [],
            "resource_lane": lane_id,
            "claim_class": "offline_scaffold",
            "started_at": started_at,
            "ended_at": ended_at,
            "exit_code": 0,
            "output_paths": [
                "run_state.jsonl",
                "parallel_run_manifest.json",
                "run_manifest.json",
            ],
            "serial_fallback": os.environ.get("UR10E_PARALLEL") == "0",
            "source_fingerprints": {
                "pre": source_fingerprint,
                "post": source_fingerprint,
            },
            "resource_receipts": {
                "acquisition_sha256": acquisition_sha256,
                "release_sha256": release_sha256,
            },
        }
    )
    parallel_path = run_root / "parallel_run_manifest.json"
    _write_exclusive_json(parallel_path, parallel_document)
    parallel_sha256 = hashlib.sha256(parallel_path.read_bytes()).hexdigest()
    state_sha256 = hashlib.sha256(state_path.read_bytes()).hexdigest()

    manifest_document = {
        "schema_version": "ur10e.run_manifest/v1",
        "experiment_id": spec.experiment_id,
        "experiment_fingerprint": spec.fingerprint,
        "run_uid": run_uid,
        "run_identity_sha256": run_uid,
        "run_nonce": run_nonce,
        "campaign_uid": None,
        "lane": lane_id,
        "candidate": candidate_document,
        "trial_overlay": candidate_document,
        "control_candidate_uid": (
            canonical_sha256(
                {
                    "schema": "ur10e.control_candidate/v1",
                    "candidate": candidate_document,
                }
            )
            if candidate_document is not None
            else None
        ),
        "authorization_ref": None,
        "fingerprints": {"pre": spec.fingerprint, "post": spec.fingerprint},
        "resource_locks": {
            "requested": requested_resources,
            "acquired": acquired_resources,
            "release_policy": "before_final_manifest_after_execution_receipt",
        },
        "controller_readback": {
            "controller_id": str(spec.bindings["controller"]["id"]),
            "verified": False,
            "sha256": None,
        },
        "outcome": {
            "classification": "offline_scaffold",
            "reason": "offline_scaffolding_only_no_backend_invoked",
        },
        "metric_role": "unavailable",
        "oracle_status": "unavailable",
        "observer_status": "unavailable",
        "ack": {"ack_uid": None, "consumed": False, "receipt_sha256": None},
        "closure": {"post_ack_verified": False, "receipt_sha256": None},
        "publication": {"unique": False, "identity_sha256": None},
        "optimizer_eligible": False,
        "objective": None,
        "artifacts": [
            {
                "kind": "run_state_jsonl",
                "path": "run_state.jsonl",
                "sha256": state_sha256,
            },
            {
                "kind": "parallel_run_manifest",
                "path": "parallel_run_manifest.json",
                "sha256": parallel_sha256,
            },
        ],
    }
    return finalize_run_manifest(
        run_root,
        manifest_document,
        evidence_lock_root=resource_lock_root,
    )


def status(identifier: str, *, output_root: str | Path) -> dict[str, Any]:
    """Read one locally scaffolded run without consulting mutable pointers."""

    if len(identifier) != _SHA256_LENGTH or any(
        ch not in "0123456789abcdef" for ch in identifier
    ):
        raise SpecValidationError("status identifier must be a full lowercase SHA256")
    root = Path(output_root)
    matches = sorted(root.glob(f"*/{identifier}/run_manifest.json"))
    if not matches:
        raise SpecValidationError(f"no local run or campaign found for {identifier}")
    if len(matches) != 1:
        raise SpecValidationError(f"ambiguous local status identifier: {identifier}")
    manifest = load_run_manifest(matches[0])
    return {
        "schema": "ur10e.status/v1",
        "kind": "run",
        "identifier": identifier,
        "manifest": manifest.document,
    }


def start_campaign(
    spec: ExperimentSpec,
    lane_id: str,
    authorization_ref: str,
) -> None:
    """Fail closed: campaign execution belongs to the live bench owner."""

    del spec, lane_id, authorization_ref
    raise UnsupportedExecutionError(
        "campaign start is unavailable in offline scaffolding; no authorization "
        "was consumed and no external action was attempted"
    )


def resume_campaign(campaign_uid: str, authorization_ref: str) -> None:
    """Fail closed: campaign resume belongs to the live bench owner."""

    del campaign_uid, authorization_ref
    raise UnsupportedExecutionError(
        "campaign resume is unavailable in offline scaffolding; no authorization "
        "was consumed and no external action was attempted"
    )
