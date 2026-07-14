#!/usr/bin/env python3
"""Stable evidence identity, fingerprints, and quarantine for Step5b autotune."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ID = "step5b_tp_local_autotune_bridge_v2"
EVALUATION_SCHEMA = "step5b_autotune_evaluation_v4"
OBJECTIVE_NAME = "force_mae_n"
OBJECTIVE_UNIT = "N"
FINGERPRINT_SCHEMA = "step5b_autotune_source_config_fingerprint_v1"
TRIAL_UID_SCHEMA = "step5b_autotune_trial_uid_v1"
TRIAL_SPEC_SCHEMA = "step5b_autotune_trial_spec_v1"
QUARANTINE_SCHEMA = "step5b_autotune_evidence_quarantine_v1"

SOURCE_FINGERPRINT_PATHS = (
    "programs/step5/autotune/step5b_contact_cycloid_bayes_loop_v2.script",
    "programs/step5/autotune/step5b_contact_cycloid_bayes_loop_v2.txt",
    "programs/step5/autotune/step5b_contact_cycloid_bayes_loop_v2.urp",
    "tools/contact_semantics.py",
    "tools/kunwei_rtde_bridge.py",
    "tools/step5b_autotune_contract.py",
    "tools/step5b_autotune_evaluator.py",
    "tools/step5b_autotune_evidence.py",
    "tools/step5b_autotune_optimizer.py",
    "tools/step5b_autotune_promotion.py",
    "tools/step5b_autotune_supervisor.py",
)

CONFIG_FINGERPRINT_PATHS = (
    "UR_FORCE_FRAME_CONTRACT.md",
    "config/local_control_textbook_spec.json",
    "config/step5b_autotune_delivery_v2.json",
    "config/step5b_autotune_loop_v2.json",
    "config/step5b_autotune_numeric_sanity.json",
    "config/step5b_autotune_stage_table.json",
    "config/step5b_tp_autotune_authorization_v2.json",
    "config/step5_stage_table.json",
)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str | None:
    try:
        if not path.is_file() or path.is_symlink():
            return None
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(serialized)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _fingerprint_group(relative_paths: Iterable[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for relative in relative_paths:
        path = EXPERIMENT_ROOT / relative
        digest = sha256_file(path)
        if digest is None:
            raise FileNotFoundError(f"fingerprint source missing or unsafe: {path}")
        result[relative] = digest
    return result


def source_config_fingerprint() -> dict[str, Any]:
    source_files = _fingerprint_group(SOURCE_FINGERPRINT_PATHS)
    config_files = _fingerprint_group(CONFIG_FINGERPRINT_PATHS)
    source_sha256 = sha256_bytes(canonical_bytes(source_files))
    config_sha256 = sha256_bytes(canonical_bytes(config_files))
    core = {
        "schema_version": FINGERPRINT_SCHEMA,
        "backend_id": BACKEND_ID,
        "source_files": source_files,
        "config_files": config_files,
        "source_sha256": source_sha256,
        "config_sha256": config_sha256,
    }
    return {**core, "combined_sha256": sha256_bytes(canonical_bytes(core))}


def fingerprint_record(phase: str) -> dict[str, Any]:
    if phase not in {"pre", "post"}:
        raise ValueError(f"unsupported fingerprint phase: {phase}")
    return {
        "schema_version": FINGERPRINT_SCHEMA,
        "phase": phase,
        "captured_at": now_iso(),
        "fingerprint": source_config_fingerprint(),
    }


def fingerprint_core(record: dict[str, Any], *, expected_phase: str) -> dict[str, Any]:
    if record.get("schema_version") != FINGERPRINT_SCHEMA:
        raise ValueError("fingerprint record schema mismatch")
    if record.get("phase") != expected_phase:
        raise ValueError(f"fingerprint record phase mismatch: expected {expected_phase}")
    payload = record.get("fingerprint")
    if not isinstance(payload, dict):
        raise ValueError("fingerprint payload missing")
    required = {
        "schema_version",
        "backend_id",
        "source_files",
        "config_files",
        "source_sha256",
        "config_sha256",
        "combined_sha256",
    }
    if set(payload) != required:
        raise ValueError("fingerprint payload fields mismatch")
    if payload.get("schema_version") != FINGERPRINT_SCHEMA or payload.get("backend_id") != BACKEND_ID:
        raise ValueError("fingerprint payload identity mismatch")
    source_files = payload.get("source_files")
    config_files = payload.get("config_files")
    if not isinstance(source_files, dict) or not isinstance(config_files, dict):
        raise ValueError("fingerprint file maps missing")
    if set(source_files) != set(SOURCE_FINGERPRINT_PATHS):
        raise ValueError("fingerprint source file set mismatch")
    if set(config_files) != set(CONFIG_FINGERPRINT_PATHS):
        raise ValueError("fingerprint config file set mismatch")
    source_sha256 = sha256_bytes(canonical_bytes(source_files))
    config_sha256 = sha256_bytes(canonical_bytes(config_files))
    core = {
        "schema_version": FINGERPRINT_SCHEMA,
        "backend_id": BACKEND_ID,
        "source_files": source_files,
        "config_files": config_files,
        "source_sha256": source_sha256,
        "config_sha256": config_sha256,
    }
    combined_sha256 = sha256_bytes(canonical_bytes(core))
    if payload.get("source_sha256") != source_sha256:
        raise ValueError("fingerprint source digest mismatch")
    if payload.get("config_sha256") != config_sha256:
        raise ValueError("fingerprint config digest mismatch")
    if payload.get("combined_sha256") != combined_sha256:
        raise ValueError("fingerprint combined digest mismatch")
    return payload


def _require_sha256(value: str, *, label: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{label} must be 64 lowercase hexadecimal characters")


def candidate_uid_from_payload(candidate: dict[str, Any]) -> str:
    return sha256_bytes(canonical_bytes(candidate))


def trial_uid_from_identity(session_uid: str, trial_id: int, candidate_uid: str) -> str:
    _require_sha256(session_uid, label="session_uid")
    _require_sha256(candidate_uid, label="candidate_uid")
    if int(trial_id) <= 0:
        raise ValueError("trial_id must be positive")
    core = {
        "schema_version": TRIAL_UID_SCHEMA,
        "backend_id": BACKEND_ID,
        "session_uid": session_uid,
        "trial_id": int(trial_id),
        "candidate_uid": candidate_uid,
    }
    return sha256_bytes(canonical_bytes(core))


def physical_capture_uid_from_sha256(capture_sha256: str) -> str:
    _require_sha256(capture_sha256, label="capture_sha256")
    core = {
        "schema_version": "step5b_autotune_physical_capture_uid_v1",
        "backend_id": BACKEND_ID,
        "capture_sha256": capture_sha256,
    }
    return sha256_bytes(canonical_bytes(core))


def trial_spec_payload(
    *,
    session_uid: str,
    trial_id: int,
    candidate: dict[str, Any],
    candidate_token_low31: int,
    fingerprint_pre_sha256: str,
) -> dict[str, Any]:
    candidate_uid = candidate_uid_from_payload(candidate)
    trial_uid = trial_uid_from_identity(session_uid, trial_id, candidate_uid)
    _require_sha256(fingerprint_pre_sha256, label="fingerprint_pre_sha256")
    if int(candidate_token_low31) <= 0:
        raise ValueError("candidate_token_low31 must be positive")
    return {
        "schema_version": TRIAL_SPEC_SCHEMA,
        "backend_id": BACKEND_ID,
        "session_uid": session_uid,
        "trial_id": int(trial_id),
        "candidate_uid": candidate_uid,
        "trial_uid": trial_uid,
        "candidate": candidate,
        "candidate_token_low31": int(candidate_token_low31),
        "fingerprint_pre_sha256": fingerprint_pre_sha256,
    }


def quarantine_evidence(
    root: Path,
    *,
    reasons: Iterable[str],
    artifact_paths: Iterable[Path] = (),
    context: dict[str, Any] | None = None,
) -> Path:
    normalized_reasons = sorted({str(reason) for reason in reasons if str(reason)})
    artifacts = {
        path.name: {"path": str(path), "sha256": sha256_file(path)}
        for path in sorted({path.resolve() for path in artifact_paths}, key=str)
    }
    core = {
        "schema_version": QUARANTINE_SCHEMA,
        "backend_id": BACKEND_ID,
        "reasons": normalized_reasons,
        "artifacts": artifacts,
        "context": context or {},
    }
    quarantine_id = sha256_bytes(canonical_bytes(core))
    payload = {
        **core,
        "quarantine_id": quarantine_id,
        "quarantined_at": now_iso(),
        "training_eligible": False,
    }
    path = root / "evidence_quarantine" / f"{quarantine_id}.json"
    if not path.exists():
        try:
            atomic_write_json(path, payload)
        except OSError as exc:
            raise RuntimeError(
                f"durable evidence quarantine write failed for {path}: {exc}"
            ) from exc
    try:
        stored = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant is forbidden: {value}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(
            f"durable evidence quarantine verification failed for {path}: {exc}"
        ) from exc
    if (
        not isinstance(stored, dict)
        or stored.get("quarantine_id") != quarantine_id
        or stored.get("training_eligible") is not False
        or any(stored.get(key) != value for key, value in core.items())
    ):
        raise RuntimeError(f"durable evidence quarantine content mismatch for {path}")
    return path


@dataclass(frozen=True)
class JsonlRecord:
    payload: dict[str, Any]
    line_number: int
    raw_line: str


def quarantine_jsonl_record(path: Path, line_number: int, raw_line: str, reason: str) -> Path:
    return quarantine_evidence(
        path.parent,
        reasons=[reason],
        artifact_paths=[path],
        context={
            "source_jsonl": str(path.resolve()),
            "line_number": line_number,
            "raw_line_sha256": sha256_bytes(raw_line.encode("utf-8", errors="replace")),
            "raw_line": raw_line,
        },
    )


def read_evaluation_jsonl(path: Path) -> list[JsonlRecord]:
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        quarantine_jsonl_record(path, 0, "", f"observation_history_unreadable:{type(exc).__name__}:{exc}")
        return []
    records: list[JsonlRecord] = []
    for line_number, raw_line in enumerate(lines, start=1):
        if not raw_line.strip():
            continue
        try:
            payload = json.loads(
                raw_line,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"non-finite JSON constant is forbidden: {value}")
                ),
            )
            if not isinstance(payload, dict):
                raise ValueError("evaluation line is not a JSON object")
            if payload.get("schema_version") != EVALUATION_SCHEMA:
                raise ValueError("evaluation schema mismatch")
            if payload.get("objective_name") != OBJECTIVE_NAME or payload.get("objective_unit") != OBJECTIVE_UNIT:
                raise ValueError("evaluation objective identity mismatch")
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            quarantine_jsonl_record(
                path,
                line_number,
                raw_line,
                f"malformed_observation:{type(exc).__name__}:{exc}",
            )
            continue
        records.append(JsonlRecord(payload=payload, line_number=line_number, raw_line=raw_line))
    return records
