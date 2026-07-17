#!/usr/bin/env python3
"""Build Step5d autotune v2 from one strict controller snapshot receipt.

The builder is offline-only.  It never probes, uploads, reads back, starts a
bridge, loads a program, or authorizes live execution.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import os
import posixpath
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SOURCE_PROGRAM = "step5d_strict_rnn_autotune_v1"
CANDIDATE_PROGRAM = "step5d_strict_rnn_autotune_v2"
CONTROLLER_HOST = "192.168.1.18"
ALLOWED_CONTROLLER_ROOT = PurePosixPath("/programs/andyl")
EXTENSIONS = (".script", ".txt", ".urp")
RECEIPT_SCHEMA = "ur10e.controller.snapshot-triplet-receipt/v1"
ATTESTATION_SCHEMA = "step5d.autotune.tp-watchdog-diff/v2"
STALE_DEPLOY_SCHEMA = "step5d.autotune.non-promotable-deploy-manifest/v1"
WATCHDOG_MANIFEST = ROOT / "config/step5/tp_watchdog_v2.json"
SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
V2_STAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{4}HKT_"
    r"STEP5D_STRICT_RNN_AUTOTUNE_V2$"
)
SOURCE_VERSION_RE = re.compile(
    rb"^# VERSION: (?P<stamp>[^\r\n]+)(?:\r?\n)"
)


class BuildError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReceiptFile:
    filename: str
    path: Path
    sha256: str


@dataclass(frozen=True)
class SnapshotReceipt:
    path: Path
    sha256: str
    host: str
    controller_directory: PurePosixPath
    basename: str
    triplet_dir: Path
    captured_at: str
    source_class: str
    files: dict[str, ReceiptFile]


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_absolute_path(path: Path, *, label: str) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        raise BuildError(f"{label} must be an absolute path")
    if ".." in expanded.parts:
        raise BuildError(f"{label} must not contain '..'")
    current = Path(expanded.anchor)
    for part in expanded.parts[1:]:
        current /= part
        if current.is_symlink():
            raise BuildError(f"{label} must not traverse a symlink: {current}")
    return expanded.resolve()


def _validate_controller_directory(raw: object) -> PurePosixPath:
    if not isinstance(raw, str) or not raw.startswith("/"):
        raise BuildError("controller_directory must be an absolute POSIX path")
    path = PurePosixPath(raw)
    if ".." in path.parts:
        raise BuildError("controller_directory must not contain '..'")
    if path != ALLOWED_CONTROLLER_ROOT and ALLOWED_CONTROLLER_ROOT not in path.parents:
        raise BuildError(
            f"controller_directory must stay under {ALLOWED_CONTROLLER_ROOT}"
        )
    if any(SAFE_COMPONENT.fullmatch(part) is None for part in path.parts[1:]):
        raise BuildError("controller_directory contains an unsafe component")
    return path


def _parse_timestamp(raw: object) -> str:
    if not isinstance(raw, str) or not raw:
        raise BuildError("captured_at must be a non-empty ISO-8601 timestamp")
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BuildError("captured_at must be valid ISO-8601") from exc
    if value.tzinfo is None or value.utcoffset() is None:
        raise BuildError("captured_at must include a timezone")
    return raw


def load_snapshot_receipt(
    receipt_path: Path, triplet_dir: Path
) -> SnapshotReceipt:
    receipt_path = _validate_absolute_path(receipt_path, label="snapshot receipt")
    triplet_dir = _validate_absolute_path(
        triplet_dir, label="snapshot triplet directory"
    )
    if not receipt_path.is_file() or receipt_path.is_symlink():
        raise BuildError("snapshot receipt must be a regular non-symlink file")
    if not triplet_dir.is_dir() or triplet_dir.is_symlink():
        raise BuildError("snapshot triplet directory must be a non-symlink directory")
    try:
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildError(f"cannot read snapshot receipt: {type(exc).__name__}") from exc
    expected_keys = {
        "schema",
        "host",
        "controller_directory",
        "basename",
        "output_dir",
        "receipt_path",
        "captured_at",
        "source_class",
        "files",
    }
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise BuildError("snapshot receipt contains missing or unsupported fields")
    if payload.get("schema") != RECEIPT_SCHEMA:
        raise BuildError("unknown snapshot receipt schema")
    if payload.get("host") != CONTROLLER_HOST:
        raise BuildError("snapshot receipt host mismatch")
    source_class = payload.get("source_class")
    if source_class not in {"fresh_controller_snapshot", "stale_source_fixture"}:
        raise BuildError("snapshot receipt has an unsupported source_class")
    controller_directory = _validate_controller_directory(
        payload.get("controller_directory")
    )
    basename = payload.get("basename")
    if (
        not isinstance(basename, str)
        or SAFE_COMPONENT.fullmatch(basename) is None
        or "." in basename
    ):
        raise BuildError("snapshot receipt basename is unsafe")
    if basename != SOURCE_PROGRAM:
        raise BuildError(
            f"v2 builder requires source basename {SOURCE_PROGRAM}, got {basename}"
        )
    output_raw = payload.get("output_dir")
    bound_receipt_raw = payload.get("receipt_path")
    if not isinstance(output_raw, str) or not isinstance(bound_receipt_raw, str):
        raise BuildError("snapshot receipt path bindings must be strings")
    bound_output = _validate_absolute_path(
        Path(output_raw), label="receipt output_dir"
    )
    bound_receipt = _validate_absolute_path(
        Path(bound_receipt_raw), label="receipt receipt_path"
    )
    if bound_output != triplet_dir:
        raise BuildError("snapshot receipt output_dir does not bind triplet directory")
    if bound_receipt != receipt_path:
        raise BuildError("snapshot receipt_path does not bind the receipt being read")
    captured_at = _parse_timestamp(payload.get("captured_at"))
    raw_files = payload.get("files")
    if not isinstance(raw_files, list) or len(raw_files) != len(EXTENSIONS):
        raise BuildError("snapshot receipt must bind exactly one triplet")
    files: dict[str, ReceiptFile] = {}
    for row in raw_files:
        if not isinstance(row, dict) or set(row) != {"filename", "path", "sha256"}:
            raise BuildError("snapshot receipt file row contains unsupported fields")
        filename = row.get("filename")
        path_raw = row.get("path")
        digest = row.get("sha256")
        if not isinstance(filename, str) or not isinstance(path_raw, str):
            raise BuildError("snapshot receipt file name/path must be strings")
        suffix = Path(filename).suffix
        if suffix not in EXTENSIONS or filename != f"{basename}{suffix}":
            raise BuildError("snapshot receipt filenames do not form one triplet")
        if suffix in files:
            raise BuildError(f"duplicate snapshot receipt suffix: {suffix}")
        path = _validate_absolute_path(
            Path(path_raw), label=f"snapshot file {filename}"
        )
        if path != triplet_dir / filename:
            raise BuildError(f"snapshot receipt file path escapes triplet_dir: {filename}")
        if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
            raise BuildError(f"snapshot receipt has invalid SHA256: {filename}")
        if not path.is_file() or path.is_symlink():
            raise BuildError(f"snapshot member must be a regular non-symlink file: {filename}")
        actual = sha256_path(path)
        if actual != digest:
            raise BuildError(f"snapshot receipt SHA256 drift: {filename}")
        files[suffix] = ReceiptFile(filename, path, digest)
    if set(files) != set(EXTENSIONS):
        raise BuildError("snapshot receipt triplet suffix set is incomplete")
    return SnapshotReceipt(
        path=receipt_path,
        sha256=sha256_path(receipt_path),
        host=CONTROLLER_HOST,
        controller_directory=controller_directory,
        basename=basename,
        triplet_dir=triplet_dir,
        captured_at=captured_at,
        source_class=source_class,
        files=files,
    )


def write_stale_fixture_receipt(
    *, triplet_dir: Path, receipt_path: Path, captured_at: str
) -> Path:
    """Bind retained v1 bytes as an explicitly non-promotable offline fixture."""

    triplet_dir = _validate_absolute_path(
        triplet_dir, label="stale fixture triplet directory"
    )
    receipt_path = _validate_absolute_path(
        receipt_path, label="stale fixture receipt"
    )
    if receipt_path.exists() or receipt_path.is_symlink():
        raise BuildError("stale fixture receipt must not already exist")
    if not receipt_path.parent.is_dir() or receipt_path.parent.is_symlink():
        raise BuildError("stale fixture receipt parent must be an existing directory")
    _parse_timestamp(captured_at)
    files = []
    for suffix in EXTENSIONS:
        path = triplet_dir / f"{SOURCE_PROGRAM}{suffix}"
        if not path.is_file() or path.is_symlink():
            raise BuildError(f"stale fixture member is missing or a symlink: {path}")
        files.append(
            {
                "filename": path.name,
                "path": str(path),
                "sha256": sha256_path(path),
            }
        )
    payload = {
        "schema": RECEIPT_SCHEMA,
        "host": CONTROLLER_HOST,
        "controller_directory": "/programs/andyl/kunwei/step5",
        "basename": SOURCE_PROGRAM,
        "output_dir": str(triplet_dir),
        "receipt_path": str(receipt_path),
        "captured_at": captured_at,
        "source_class": "stale_source_fixture",
        "files": files,
    }
    _write_json_exclusive(receipt_path, payload)
    load_snapshot_receipt(receipt_path, triplet_dir)
    return receipt_path


def _replace_once(payload: bytes, old: bytes, new: bytes, *, label: str) -> bytes:
    count = payload.count(old)
    if count != 1:
        raise BuildError(f"{label} count is {count}, expected 1")
    return payload.replace(old, new, 1)


def _extract_source_stamp(script: bytes) -> str:
    match = SOURCE_VERSION_RE.match(script)
    if match is None:
        raise BuildError("source script must start with one # VERSION line")
    try:
        stamp = match.group("stamp").decode("ascii")
    except UnicodeDecodeError as exc:
        raise BuildError("source stamp must be ASCII") from exc
    if not stamp.endswith("_STEP5D_STRICT_RNN_AUTOTUNE_V1"):
        raise BuildError("source script first stamp is not the current autotune v1 identity")
    return stamp


def _load_canonical_watchdog() -> tuple[str, bytes, str]:
    try:
        manifest: dict[str, Any] = json.loads(
            WATCHDOG_MANIFEST.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildError(f"cannot read watchdog manifest: {type(exc).__name__}") from exc
    gate = manifest.get("watchdog_diff_gate")
    rows = gate.get("required_blocks") if isinstance(gate, dict) else None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise BuildError("watchdog manifest must bind exactly one canonical block")
    row = rows[0]
    if set(row) != {"block_id", "source", "sha256", "required_in"}:
        raise BuildError("canonical watchdog manifest row has unsupported fields")
    block_id = row.get("block_id")
    source_raw = row.get("source")
    digest = row.get("sha256")
    if (
        not isinstance(block_id, str)
        or not isinstance(source_raw, str)
        or not isinstance(digest, str)
        or SHA256_RE.fullmatch(digest) is None
        or row.get("required_in") != [".script", ".urp"]
    ):
        raise BuildError("canonical watchdog manifest binding is invalid")
    source = (ROOT / source_raw).resolve()
    try:
        source.relative_to(ROOT)
    except ValueError as exc:
        raise BuildError("canonical watchdog source escapes repository") from exc
    if not source.is_file() or source.is_symlink():
        raise BuildError("canonical watchdog source must be a regular file")
    payload = source.read_bytes()
    if sha256_bytes(payload) != digest:
        raise BuildError("canonical watchdog source SHA256 drift")
    return block_id, payload, digest


def _transform_script(
    source: bytes, *, source_stamp: str, candidate_stamp: str, watchdog: bytes
) -> bytes:
    try:
        source.decode("utf-8")
        watchdog.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BuildError("script/watchdog sources must be UTF-8") from exc
    transformed = _replace_once(
        source,
        f"# VERSION: {source_stamp}".encode("ascii"),
        f"# VERSION: {candidate_stamp}".encode("ascii"),
        label="source stamp identity",
    )
    transformed = _replace_once(
        transformed,
        f"# STEP5_STAGE_ID: {SOURCE_PROGRAM}".encode("ascii"),
        f"# STEP5_STAGE_ID: {CANDIDATE_PROGRAM}".encode("ascii"),
        label="stage program identity",
    )
    transformed = _replace_once(
        transformed,
        f"def codex_{SOURCE_PROGRAM}():".encode("ascii"),
        f"def codex_{CANDIDATE_PROGRAM}():".encode("ascii"),
        label="main function identity",
    )
    transformed = _replace_once(
        transformed,
        f"\ncodex_{SOURCE_PROGRAM}()\n".encode("ascii"),
        f"\ncodex_{CANDIDATE_PROGRAM}()\n".encode("ascii"),
        label="main invocation identity",
    )
    anchor = b"  codex_autotune_write_state(0, 0, 10, 0, 0, 0, 0)\n"
    transformed = _replace_once(
        transformed,
        anchor,
        anchor + watchdog,
        label="watchdog executable anchor",
    )
    return transformed


def _transform_txt(
    source: bytes, *, source_stamp: str, candidate_stamp: str, controller_dir: str
) -> bytes:
    try:
        source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BuildError("source .txt must be UTF-8") from exc
    transformed = _replace_once(
        source,
        source_stamp.encode("ascii"),
        candidate_stamp.encode("ascii"),
        label="TP documentation source stamp",
    )
    transformed = _replace_once(
        transformed,
        f"{controller_dir}/{SOURCE_PROGRAM}.urp".encode("ascii"),
        f"{controller_dir}/{CANDIDATE_PROGRAM}.urp".encode("ascii"),
        label="TP documentation program path",
    )
    return transformed


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def _validate_urp_xml(
    xml_bytes: bytes,
    *,
    program: str,
    controller_dir: str,
    script: bytes,
) -> str:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise BuildError(f"URP_INTERNAL_CONTENT_GATE XML parse failed: {exc}") from exc
    if _xml_local_name(root.tag) not in {"Program", "URProgram"}:
        raise BuildError("URP_INTERNAL_CONTENT_GATE root is not Program/URProgram")
    expected_install = posixpath.relpath("/programs/default", controller_dir)
    checks = {
        "root name": root.get("name") == program,
        "directory": root.get("directory") == controller_dir,
        "installationRelativePath": root.get("installationRelativePath")
        == expected_install,
        "numeric crcValue": re.fullmatch(r"[0-9]+", root.get("crcValue") or "")
        is not None,
    }
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        raise BuildError(f"URP_INTERNAL_CONTENT_GATE metadata failed: {failed}")
    cached_nodes = [
        node for node in root.iter() if _xml_local_name(node.tag) == "cachedContents"
    ]
    file_nodes = [
        node
        for node in root.iter()
        if _xml_local_name(node.tag) == "file"
        and node.get("resolves-to") == "file"
    ]
    if len(cached_nodes) != 1 or list(cached_nodes[0]):
        raise BuildError("URP_INTERNAL_CONTENT_GATE requires one text-only cachedContents")
    if len(file_nodes) != 1:
        raise BuildError("URP_INTERNAL_CONTENT_GATE requires one Script file node")
    try:
        expected_script = script.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BuildError("candidate script is not UTF-8") from exc
    if (cached_nodes[0].text or "") != expected_script:
        raise BuildError("URP_INTERNAL_CONTENT_GATE cachedContents/script mismatch")
    expected_file = f"{controller_dir}/{program}.script"
    if (file_nodes[0].text or "") != expected_file:
        raise BuildError("URP_INTERNAL_CONTENT_GATE Script node path mismatch")
    return root.get("crcValue") or ""


def _replace_root_name(xml_bytes: bytes) -> bytes:
    pattern = re.compile(
        rb"(?P<prefix><(?:URProgram|Program)\b[^>]*\bname\s*=\s*)(?P<quote>[\"'])(?P<value>[^\"']*)(?P=quote)"
    )
    matches = list(pattern.finditer(xml_bytes))
    if len(matches) != 1 or matches[0].group("value") != SOURCE_PROGRAM.encode(
        "ascii"
    ):
        raise BuildError("URP root program name attribute is missing or ambiguous")
    match = matches[0]
    start, end = match.span("value")
    return xml_bytes[:start] + CANDIDATE_PROGRAM.encode("ascii") + xml_bytes[end:]


def _transform_urp(
    source_urp: bytes,
    *,
    source_script: bytes,
    candidate_script: bytes,
    controller_dir: str,
) -> tuple[bytes, str, str]:
    try:
        source_xml = gzip.decompress(source_urp)
    except (OSError, gzip.BadGzipFile) as exc:
        raise BuildError(f"URP_INTERNAL_CONTENT_GATE gzip failed: {exc}") from exc
    source_crc = _validate_urp_xml(
        source_xml,
        program=SOURCE_PROGRAM,
        controller_dir=controller_dir,
        script=source_script,
    )
    old_cached = html.escape(source_script.decode("utf-8"), quote=True).encode(
        "utf-8"
    )
    new_cached = html.escape(candidate_script.decode("utf-8"), quote=True).encode(
        "utf-8"
    )
    transformed = _replace_root_name(source_xml)
    transformed = _replace_once(
        transformed,
        old_cached,
        new_cached,
        label="URP cachedContents bytes",
    )
    transformed = _replace_once(
        transformed,
        f"{controller_dir}/{SOURCE_PROGRAM}.script".encode("ascii"),
        f"{controller_dir}/{CANDIDATE_PROGRAM}.script".encode("ascii"),
        label="URP Script node identity",
    )
    candidate_crc = _validate_urp_xml(
        transformed,
        program=CANDIDATE_PROGRAM,
        controller_dir=controller_dir,
        script=candidate_script,
    )
    return gzip.compress(transformed, mtime=0), source_crc, candidate_crc


def _write_bytes_exclusive(path: Path, payload: bytes, *, mode: int = 0o644) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, mode)
    try:
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(fd)


def _write_json_exclusive(path: Path, payload: dict[str, Any]) -> None:
    _write_bytes_exclusive(
        path,
        (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        mode=0o600,
    )


def _prepare_outputs(output_dir: Path, attestation_path: Path) -> tuple[Path, Path]:
    output_dir = _validate_absolute_path(output_dir, label="candidate output directory")
    attestation_path = _validate_absolute_path(
        attestation_path, label="diff attestation path"
    )
    if output_dir.exists() or output_dir.is_symlink():
        raise BuildError("candidate output directory must not already exist")
    if attestation_path.exists() or attestation_path.is_symlink():
        raise BuildError("diff attestation path must not already exist")
    if not output_dir.parent.is_dir() or output_dir.parent.is_symlink():
        raise BuildError("candidate output parent must be an existing directory")
    if output_dir not in attestation_path.parents and (
        not attestation_path.parent.is_dir() or attestation_path.parent.is_symlink()
    ):
        raise BuildError(
            "external diff attestation parent must be an existing non-symlink directory"
        )
    return output_dir, attestation_path


def _deploy_manifest(
    *,
    receipt: SnapshotReceipt,
    output_dir: Path,
    candidate_sha: dict[str, str],
) -> tuple[Path, str]:
    bound = {
        "schema_version": 1,
        "basename": CANDIDATE_PROGRAM,
        "controller_directory": str(receipt.controller_directory),
        "artifacts": [
            {
                "filename": f"{CANDIDATE_PROGRAM}{suffix}",
                "source": f"{CANDIDATE_PROGRAM}{suffix}",
                "sha256": candidate_sha[suffix],
            }
            for suffix in EXTENSIONS
        ],
    }
    path = output_dir / f"{CANDIDATE_PROGRAM}.deploy-manifest.json"
    if receipt.source_class == "fresh_controller_snapshot":
        payload = bound
        schema = "ur10e.controller.deployment-manifest/v1"
    else:
        payload = {
            "schema": STALE_DEPLOY_SCHEMA,
            "promotable": False,
            "blocked_reason": "stale_source_fixture",
            "would_deploy": bound,
        }
        schema = STALE_DEPLOY_SCHEMA
    _write_json_exclusive(path, payload)
    return path, schema


def build_package(
    *,
    snapshot_receipt: Path,
    snapshot_triplet_dir: Path,
    output_dir: Path,
    v2_source_stamp: str,
    diff_attestation: Path,
) -> dict[str, Any]:
    if V2_STAMP_RE.fullmatch(v2_source_stamp) is None:
        raise BuildError(
            "v2 source stamp must match YYYY-MM-DDTHHMMHKT_STEP5D_STRICT_RNN_AUTOTUNE_V2"
        )
    output_dir, diff_attestation = _prepare_outputs(output_dir, diff_attestation)
    receipt = load_snapshot_receipt(snapshot_receipt, snapshot_triplet_dir)
    source_script = receipt.files[".script"].path.read_bytes()
    source_txt = receipt.files[".txt"].path.read_bytes()
    source_urp = receipt.files[".urp"].path.read_bytes()
    source_stamp = _extract_source_stamp(source_script)
    block_id, watchdog, canonical_sha = _load_canonical_watchdog()
    candidate_script = _transform_script(
        source_script,
        source_stamp=source_stamp,
        candidate_stamp=v2_source_stamp,
        watchdog=watchdog,
    )
    candidate_txt = _transform_txt(
        source_txt,
        source_stamp=source_stamp,
        candidate_stamp=v2_source_stamp,
        controller_dir=str(receipt.controller_directory),
    )
    candidate_urp, source_crc, candidate_crc = _transform_urp(
        source_urp,
        source_script=source_script,
        candidate_script=candidate_script,
        controller_dir=str(receipt.controller_directory),
    )

    output_dir.mkdir(mode=0o700)
    candidate_payloads = {
        ".script": candidate_script,
        ".txt": candidate_txt,
        ".urp": candidate_urp,
    }
    candidate_paths: dict[str, Path] = {}
    for suffix, payload in candidate_payloads.items():
        path = output_dir / f"{CANDIDATE_PROGRAM}{suffix}"
        _write_bytes_exclusive(path, payload)
        candidate_paths[suffix] = path
    candidate_sha = {
        suffix: sha256_path(path) for suffix, path in candidate_paths.items()
    }
    deploy_path, deploy_schema = _deploy_manifest(
        receipt=receipt,
        output_dir=output_dir,
        candidate_sha=candidate_sha,
    )

    from verify_step5d_tp_watchdog_diff import (  # noqa: PLC0415
        inspect_content,
    )

    normalized_sha: dict[str, str] = {}
    watchdog_counts: dict[str, int] = {}
    watchdog_sha: dict[str, dict[str, str]] = {}
    for suffix in EXTENSIONS:
        source_inspection = inspect_content(
            receipt.files[suffix].path,
            current_program=SOURCE_PROGRAM,
            candidate_program=CANDIDATE_PROGRAM,
            source_stamp=source_stamp,
            candidate_stamp=v2_source_stamp,
            controller_directory=str(receipt.controller_directory),
        )
        candidate_inspection = inspect_content(
            candidate_paths[suffix],
            current_program=SOURCE_PROGRAM,
            candidate_program=CANDIDATE_PROGRAM,
            source_stamp=source_stamp,
            candidate_stamp=v2_source_stamp,
            controller_directory=str(receipt.controller_directory),
        )
        if source_inspection.blocks:
            raise BuildError(f"source snapshot unexpectedly contains watchdog block: {suffix}")
        if source_inspection.normalized != candidate_inspection.normalized:
            raise BuildError(
                f"candidate changes bytes outside allowed identities/watchdog: {suffix}"
            )
        normalized_sha[suffix] = sha256_bytes(candidate_inspection.normalized)
        watchdog_counts[suffix] = len(candidate_inspection.blocks)
        watchdog_sha[suffix] = {
            block.block_id: sha256_bytes(block.payload)
            for block in candidate_inspection.blocks
        }

    promotable = receipt.source_class == "fresh_controller_snapshot"
    attestation: dict[str, Any] = {
        "schema": ATTESTATION_SCHEMA,
        "source_class": receipt.source_class,
        "promotable": promotable,
        "blocked_reason": None if promotable else "stale_source_fixture",
        "source_receipt": {
            "schema": RECEIPT_SCHEMA,
            "path": str(receipt.path),
            "sha256": receipt.sha256,
            "host": receipt.host,
            "controller_directory": str(receipt.controller_directory),
            "basename": receipt.basename,
            "output_dir": str(receipt.triplet_dir),
            "captured_at": receipt.captured_at,
            "source_class": receipt.source_class,
        },
        "source_program": SOURCE_PROGRAM,
        "candidate_program": CANDIDATE_PROGRAM,
        "controller_directory": str(receipt.controller_directory),
        "source_stamp": source_stamp,
        "candidate_stamp": v2_source_stamp,
        "allowed_changes": [
            "program_identity",
            "source_stamp_identity",
            "urp_crcValue",
            "canonical_watchdog_block",
        ],
        "control_math_changed": False,
        "trajectory_changed": False,
        "waypoint_changed": False,
        "command_order_changed": False,
        "fetched_controller_sha256": {
            suffix: receipt.files[suffix].sha256 for suffix in EXTENSIONS
        },
        "candidate_sha256": candidate_sha,
        "normalized_content_sha256": normalized_sha,
        "watchdog_block_count": watchdog_counts,
        "watchdog_block_sha256": watchdog_sha,
        "canonical_watchdog": {
            "block_id": block_id,
            "sha256": canonical_sha,
        },
        "urp_crcValue": {
            "source": source_crc,
            "candidate": candidate_crc,
        },
        "deploy_manifest": {
            "path": str(deploy_path),
            "sha256": sha256_path(deploy_path),
            "schema": deploy_schema,
            "promotable": promotable,
        },
    }
    _write_json_exclusive(diff_attestation, attestation)

    from verify_step5d_tp_watchdog_diff import verify_triplet  # noqa: PLC0415

    verify_triplet(
        current=receipt.triplet_dir,
        candidate=output_dir,
        program=CANDIDATE_PROGRAM,
        attestation_path=diff_attestation,
    )
    return {
        "schema": "step5d.autotune.tp-build-result/v2",
        "status": (
            "local_candidate_unuploaded"
            if promotable
            else "stale_source_fixture_non_promotable"
        ),
        "promotable": promotable,
        "blocked_reason": None if promotable else "stale_source_fixture",
        "program": CANDIDATE_PROGRAM,
        "output_dir": str(output_dir),
        "diff_attestation": str(diff_attestation),
        "deploy_manifest": str(deploy_path),
        "sha256": candidate_sha,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-receipt", type=Path, required=True)
    parser.add_argument("--snapshot-triplet-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--v2-source-stamp", required=True)
    parser.add_argument("--diff-attestation", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = build_package(
            snapshot_receipt=args.snapshot_receipt,
            snapshot_triplet_dir=args.snapshot_triplet_dir,
            output_dir=args.output_dir,
            v2_source_stamp=args.v2_source_stamp,
            diff_attestation=args.diff_attestation,
        )
    except (BuildError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {
                    "schema": "step5d.autotune.tp-build-result/v2",
                    "status": "failed_closed",
                    "error": str(exc),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
