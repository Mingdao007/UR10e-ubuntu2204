"""Typed, content-addressed contract for Autotune V4 r006.

This module deliberately does not mutate a parent release.  r004/r005 bytes
are read only and their digests are part of the new r006 parent binding.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from step5d_managed_runtime import ManagedRuntimeError, ManagedRuntimeManifest, load_runtime_manifest
from step5d_autotune_v4_r004.path_reference import path_reference_binding


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = ROOT / "config/step5d/autotune_v4_r006.json"
RUNTIME_MANIFEST_PATH = ROOT / "config/step5d/autotune_v4_r006_runtime_manifest.json"
PROGRAM = "step5d_strict_rnn_autotune_v4_r006"
LINEAGE = PROGRAM
REVISION = 6
TARGET_FORCE_N = 5.0
STEP_OCTAVE = 0.25
FORMAL_WINDOW_S = (5.0, 60.0)
LEGACY_SHADOW_WINDOW_S = (0.0, 55.0)
BIN_WIDTH_S = 0.1
REQUIRED_BINS = 550
PATH_STAGE = 25
PACKET_LEASE_S = 0.080
NOMINAL_HZ = 500.0
MINIMUM_HZ = 460.0
PATH_DURATION_S = 60.0
PATH_OMEGA_RAD_S = 0.1
PATH_AMPLITUDE_M = 0.015

# These values are the named anchors only; target force is intentionally not a
# coordinate in the r006 graph.
P_ANCHOR = 0.0003535533906
I_ON_ANCHOR = 0.00001
D_ANCHOR = 28.0
TAU_ANCHOR = 0.35
KO_ANCHOR = 0.1
KP_ANCHOR = 1.5

R006_CONTRACT_SCHEMA = "step5d.autotune-v4/r006-release-contract-v1"
R006_RUNTIME_MANIFEST_SCHEMA = "step5d.managed-runtime/manifest-v1"
OBJECTIVE_SCHEMA = "step5d.force-objective/v3-r006"
OBJECTIVE_SEMANTIC_FINGERPRINT = (
    "r006.force-mae-v3|stage=25|formal=[5,60)|legacy-r004-shadow=[0,55)|"
    "r005-shadow=abs(bin-mean-5)|bin=0.1s|bins=550|target=5N|target-not-dimension"
)
OBJECTIVE_RECEIPT_VERSION = "r006-sealed-raw-bundle-sufficient-statistics-v1"


class R006ContractError(RuntimeError):
    """A hash-bound r006 contract or parent identity is invalid."""


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise R006ContractError(f"source is not a regular file: {path}")
    return sha256_bytes(path.read_bytes())


def _digest(value: Any, role: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise R006ContractError(f"{role} must be a lowercase SHA-256")
    return value


def _finite(value: Any, role: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise R006ContractError(f"{role} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise R006ContractError(f"{role} must be finite")
    return result


def _strict_document(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise R006ContractError(f"contract is missing or unsafe: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise R006ContractError(f"contract is not strict JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise R006ContractError("r006 contract must be an object")
    return document


@dataclass(frozen=True)
class R006Contract:
    """Immutable loaded r006 contract and its managed-runtime binding."""

    path: Path
    raw: Mapping[str, Any]
    sha256: str
    campaign_fingerprint: str
    runtime_manifest: ManagedRuntimeManifest

    @property
    def program(self) -> str:
        return PROGRAM

    @property
    def target_force_n(self) -> float:
        return TARGET_FORCE_N

    @property
    def parent_hashes(self) -> Mapping[str, str]:
        return dict(self.raw["parent_identity"]["sha256"])

    @property
    def source_closure_path(self) -> Path:
        return ROOT / str(self.raw["source_closure"]["path"])

    def require_offline_only(self) -> None:
        boundary = self.raw["offline_boundary"]
        if any(bool(value) for value in boundary.values()):
            raise R006ContractError("r006 offline boundary unexpectedly enables a live action")


def _validate_parent_identity(document: Mapping[str, Any]) -> dict[str, str]:
    parent = document.get("parent_identity")
    if not isinstance(parent, Mapping) or set(parent) != {"lineage", "sha256"}:
        raise R006ContractError("r006 parent identity fields differ")
    if parent.get("lineage") != "step5d_strict_rnn_autotune_v4_r005":
        raise R006ContractError("r006 parent lineage is not r005")
    rows = parent.get("sha256")
    if not isinstance(rows, Mapping) or not rows:
        raise R006ContractError("r006 parent digest map is missing")
    result: dict[str, str] = {}
    for raw_path, raw_digest in rows.items():
        if not isinstance(raw_path, str) or not raw_path or Path(raw_path).is_absolute():
            raise R006ContractError("r006 parent path is invalid")
        source = (ROOT / raw_path).resolve()
        try:
            source.relative_to(ROOT.resolve())
        except ValueError as exc:
            raise R006ContractError("r006 parent path escapes the experiment root") from exc
        expected = _digest(raw_digest, f"r006 parent {raw_path}")
        actual = sha256_file(source)
        if actual != expected:
            raise R006ContractError(f"r006 parent digest differs: {raw_path}")
        result[raw_path] = expected
    return result


def _validate_runtime_manifest(document: Mapping[str, Any]) -> ManagedRuntimeManifest:
    declaration = document.get("runtime_manifest")
    if not isinstance(declaration, Mapping) or set(declaration) != {"path", "sha256"}:
        raise R006ContractError("r006 runtime manifest declaration is missing")
    if declaration.get("path") != "config/step5d/autotune_v4_r006_runtime_manifest.json":
        raise R006ContractError("r006 runtime manifest owner differs")
    try:
        manifest = load_runtime_manifest(
            ROOT / str(declaration["path"]),
            root=ROOT,
            expected_sha256=_digest(declaration["sha256"], "r006 runtime manifest digest"),
        )
    except ManagedRuntimeError as exc:
        raise R006ContractError(f"r006 runtime manifest is invalid: {exc}") from exc
    if (
        manifest.release_contract_path != "config/step5d/autotune_v4_r006.json"
        or manifest.release_contract_schema != R006_CONTRACT_SCHEMA
        or manifest.release_program != PROGRAM
        or manifest.release_revision != REVISION
    ):
        raise R006ContractError("r006 runtime manifest release identity differs")
    return manifest


def _validate_source_closure(
    document: Mapping[str, Any],
    *,
    campaign_fingerprint: str,
) -> None:
    """Verify the closure's stable basis and every listed immutable byte.

    The generated TP triplet contains the release-contract digest, so a raw
    hash of the entire closure document would create a circular fixed-point
    requirement.  r006 therefore content-addresses a declared source-only
    basis and records contract-bound package hashes separately.
    """

    declaration = document.get("source_closure")
    if not isinstance(declaration, Mapping):
        raise R006ContractError("r006 source closure declaration is missing")
    closure_path = ROOT / str(declaration.get("path", ""))
    if closure_path.is_symlink() or not closure_path.is_file():
        raise R006ContractError("r006 source closure is missing or unsafe")
    try:
        closure = json.loads(closure_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise R006ContractError("r006 source closure is not strict JSON") from exc
    if not isinstance(closure, Mapping):
        raise R006ContractError("r006 source closure must be an object")
    release = closure.get("release_contract")
    if (
        not isinstance(release, Mapping)
        or release.get("path") != "config/step5d/autotune_v4_r006.json"
        or release.get("campaign_fingerprint") != campaign_fingerprint
    ):
        raise R006ContractError("r006 source closure release binding differs")
    content_address = closure.get("content_address")
    if not isinstance(content_address, Mapping):
        raise R006ContractError("r006 source closure content address is missing")
    payload = content_address.get("payload")
    expected = content_address.get("sha256")
    if not isinstance(payload, Mapping) or _digest(expected, "r006 source closure basis digest") != sha256_bytes(canonical_bytes(payload)):
        raise R006ContractError("r006 source closure basis digest differs")
    if expected != declaration.get("sha256"):
        raise R006ContractError("r006 source closure contract binding differs")
    if payload.get("release_contract", {}).get("campaign_fingerprint") != campaign_fingerprint:
        raise R006ContractError("r006 source closure basis campaign differs")
    rows = closure.get("source_closure")
    if not isinstance(rows, Mapping) or not isinstance(rows.get("sha256"), Mapping):
        raise R006ContractError("r006 source closure hash rows are missing")
    for relative, digest in rows["sha256"].items():
        if not isinstance(relative, str) or Path(relative).is_absolute():
            raise R006ContractError("r006 source closure path is invalid")
        source = (ROOT / relative).resolve()
        try:
            source.relative_to(ROOT.resolve())
        except ValueError as exc:
            raise R006ContractError("r006 source closure path escapes root") from exc
        if sha256_file(source) != _digest(digest, f"r006 source closure {relative}"):
            raise R006ContractError(f"r006 source closure byte digest differs: {relative}")
    artifacts = closure.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise R006ContractError("r006 package artifact rows are missing")
    for role, row in artifacts.items():
        if not isinstance(row, Mapping):
            raise R006ContractError(f"r006 artifact row is invalid: {role}")
        relative = row.get("path")
        if not isinstance(relative, str) or Path(relative).is_absolute():
            raise R006ContractError(f"r006 artifact path is invalid: {role}")
        source = (ROOT / relative).resolve()
        try:
            source.relative_to(ROOT.resolve())
        except ValueError as exc:
            raise R006ContractError("r006 artifact path escapes root") from exc
        if sha256_file(source) != _digest(row.get("sha256"), f"r006 artifact {role}"):
            raise R006ContractError(f"r006 artifact byte digest differs: {role}")


def load_contract(
    path: Path = CONTRACT_PATH,
    *,
    verify_source_closure: bool = True,
) -> R006Contract:
    """Load and fail closed on all content-addressed r006 bindings."""

    path = Path(path)
    document = _strict_document(path)
    required = {
        "schema",
        "lineage_id",
        "program",
        "revision",
        "parent_identity",
        "motion",
        "objective",
        "lattice",
        "warm_start",
        "runtime",
        "optimizer",
        "completion",
        "runtime_manifest",
        "source_closure",
        "offline_boundary",
    }
    if set(document) != required:
        raise R006ContractError("r006 contract fields differ")
    if document["schema"] != R006_CONTRACT_SCHEMA:
        raise R006ContractError("r006 contract schema differs")
    if document["lineage_id"] != LINEAGE or document["program"] != PROGRAM:
        raise R006ContractError("r006 release identity differs")
    if document["revision"] != REVISION:
        raise R006ContractError("r006 revision differs")
    _validate_parent_identity(document)

    objective = document["objective"]
    if (
        objective.get("schema") != OBJECTIVE_SCHEMA
        or objective.get("version") != OBJECTIVE_RECEIPT_VERSION
        or _finite(objective.get("target_force_n"), "objective target") != TARGET_FORCE_N
        or objective.get("target_is_optimizer_dimension") is not False
        or tuple(objective.get("formal_window_s", ())) != FORMAL_WINDOW_S
        or tuple(objective.get("legacy_r004_shadow_window_s", ())) != LEGACY_SHADOW_WINDOW_S
        or objective.get("required_bins") != REQUIRED_BINS
        or objective.get("semantic_fingerprint") != OBJECTIVE_SEMANTIC_FINGERPRINT
    ):
        raise R006ContractError("r006 objective contract differs")
    motion = document["motion"]
    snapshot = motion.get("path_snapshot") if isinstance(motion, Mapping) else None
    expected_snapshot = dict(path_reference_binding())
    if not isinstance(snapshot, Mapping) or dict(snapshot) != expected_snapshot:
        raise R006ContractError("r006 path snapshot binding differs")
    runtime = document["runtime"]
    if (
        runtime.get("host_hz") != NOMINAL_HZ
        or runtime.get("rtde_hz") != NOMINAL_HZ
        or runtime.get("rtde_request_hz") != NOMINAL_HZ
        or runtime.get("kunwei_hz") != NOMINAL_HZ
        or runtime.get("tp_hz") != NOMINAL_HZ
        or runtime.get("minimum_rate_hz") != MINIMUM_HZ
        or runtime.get("active_lease_s") != PACKET_LEASE_S
        or runtime.get("path_duration_s") != PATH_DURATION_S
        or runtime.get("path_omega_rad_s") != PATH_OMEGA_RAD_S
        or runtime.get("path_amplitude_m") != PATH_AMPLITUDE_M
        or runtime.get("attempt_sequence") != "positive_monotonically_increasing_unbounded"
        or runtime.get("production_stop_after_ordinal") is not False
    ):
        raise R006ContractError("r006 runtime contract differs")
    queue = runtime.get("queue")
    if not isinstance(queue, Mapping) or queue.get("pending_max") != 2 or queue.get("physical_inflight_max") != 1:
        raise R006ContractError("r006 queue bound differs")
    if document["optimizer"].get("cuda_required") is not True or document["optimizer"].get("degraded_fallback") is not False:
        raise R006ContractError("r006 optimizer fallback policy differs")
    completion = document["completion"]
    if (
        "pac_epsilon_default_n" in completion
        or "application_mae_threshold_default_n" in completion
        or completion.get("threshold_receipt_required") is not True
    ):
        raise R006ContractError("r006 completion cannot contain production threshold defaults")
    if completion.get("retests") != 3 or completion.get("minimum_retest_passes") != 2:
        raise R006ContractError("r006 retest contract differs")
    threshold_input = document["warm_start"].get("threshold_input")
    if (
        not isinstance(threshold_input, Mapping)
        or threshold_input.get("schema") != "step5d.autotune-v4/r006-runtime-thresholds-v1"
        or threshold_input.get("version") != "r006-v1"
        or threshold_input.get("values_are_required_before_arm") is not True
        or threshold_input.get("missing_values_arm") is not False
    ):
        raise R006ContractError("r006 Home threshold input gate differs")
    source = document["source_closure"]
    if not isinstance(source, Mapping) or source.get("path") != "config/step5d/autotune_v4_r006_offline_closure.json":
        raise R006ContractError("r006 source closure owner differs")
    manifest = _validate_runtime_manifest(document)
    boundary = document["offline_boundary"]
    if not isinstance(boundary, Mapping) or not boundary or any(value is not False for value in boundary.values()):
        raise R006ContractError("r006 offline boundary is not fail-closed")
    fingerprint = sha256_bytes(canonical_bytes({
        "lineage_id": document["lineage_id"],
        "program": document["program"],
        "revision": document["revision"],
        "motion": document["motion"],
        "objective": document["objective"],
        "lattice": document["lattice"],
        "warm_start": document["warm_start"],
        "runtime": document["runtime"],
        "optimizer": document["optimizer"],
        "completion": document["completion"],
    }))
    if verify_source_closure:
        _validate_source_closure(document, campaign_fingerprint=fingerprint)
    encoded = path.read_bytes()
    contract = R006Contract(
        path=path,
        raw=document,
        sha256=sha256_bytes(encoded),
        campaign_fingerprint=fingerprint,
        runtime_manifest=manifest,
    )
    contract.require_offline_only()
    return contract


__all__ = [
    "BIN_WIDTH_S",
    "CONTRACT_PATH",
    "D_ANCHOR",
    "FORMAL_WINDOW_S",
    "I_ON_ANCHOR",
    "KP_ANCHOR",
    "KO_ANCHOR",
    "LEGACY_SHADOW_WINDOW_S",
    "LINEAGE",
    "MINIMUM_HZ",
    "NOMINAL_HZ",
    "OBJECTIVE_RECEIPT_VERSION",
    "OBJECTIVE_SCHEMA",
    "OBJECTIVE_SEMANTIC_FINGERPRINT",
    "PACKET_LEASE_S",
    "PATH_AMPLITUDE_M",
    "PATH_DURATION_S",
    "PATH_OMEGA_RAD_S",
    "PATH_STAGE",
    "P_ANCHOR",
    "PROGRAM",
    "REQUIRED_BINS",
    "R006Contract",
    "R006ContractError",
    "R006_CONTRACT_SCHEMA",
    "RUNTIME_MANIFEST_PATH",
    "STEP_OCTAVE",
    "TARGET_FORCE_N",
    "TAU_ANCHOR",
    "canonical_bytes",
    "load_contract",
    "sha256_bytes",
    "sha256_file",
]
