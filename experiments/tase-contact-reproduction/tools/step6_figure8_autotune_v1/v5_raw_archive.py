"""Cold-verified local compression for new V5 raw lifecycle artifacts.

The uncompressed ``.r013life`` remains the source while a dependent ledger or
report is live.  The receipt proves that a zstd copy can be decompressed back
to the exact byte SHA; no deletion or silent summary-only fallback is allowed.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any, Mapping, Sequence


RAW_ARCHIVE_SCHEMA = "step6.autotune/figure8-v5-raw-archive-v1"
RAW_ARCHIVE_VERSION = 1


class V5RawArchiveError(RuntimeError):
    """The raw lifecycle compression or cold verification failed."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _receipt_hash(body: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(dict(body))).hexdigest()


def _require_raw(path: Path) -> Path:
    target = Path(path).resolve()
    if target.is_symlink() or not target.is_file() or target.suffix != ".r013life":
        raise V5RawArchiveError("raw lifecycle source must be a regular .r013life file")
    return target


def _zstd() -> str:
    executable = shutil.which("zstd")
    if executable is None:
        raise V5RawArchiveError("zstd executable is unavailable")
    return executable


def compress_r013life(
    raw_path: Path | str,
    *,
    output_path: Path | str | None = None,
    level: int = 19,
    retain_uncompressed: bool = True,
) -> dict[str, Any]:
    """Create and cold-verify a zstd copy without mutating the source."""

    source = _require_raw(Path(raw_path))
    if type(level) is not int or not 1 <= level <= 22:
        raise V5RawArchiveError("zstd level must be between 1 and 22")
    destination = Path(output_path).resolve() if output_path is not None else source.with_name(source.name + ".zst")
    if destination.suffix != ".zst" or destination == source:
        raise V5RawArchiveError("compressed raw destination must end in .zst")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise V5RawArchiveError("compressed raw destination already exists")
    raw_size = source.stat().st_size
    raw_sha = _sha256(source)
    temporary = destination.with_name(destination.name + f".tmp-{os.getpid()}")
    try:
        with temporary.open("xb") as output:
            completed = subprocess.run(
                [_zstd(), f"-{level}", "--quiet", "--stdout", str(source)],
                stdout=output,
                stderr=subprocess.PIPE,
                check=False,
            )
            output.flush()
            os.fsync(output.fileno())
        if completed.returncode != 0:
            raise V5RawArchiveError(f"zstd compression failed: {completed.stderr.decode(errors='replace').strip()}")
        os.replace(temporary, destination)
        with tempfile.TemporaryDirectory(prefix="v5-raw-verify-", dir=str(destination.parent)) as directory:
            restored = Path(directory) / source.name
            with restored.open("xb") as output:
                completed = subprocess.run(
                    [_zstd(), "--quiet", "--decompress", "--stdout", str(destination)],
                    stdout=output,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                output.flush()
                os.fsync(output.fileno())
            if completed.returncode != 0 or restored.stat().st_size != raw_size or _sha256(restored) != raw_sha:
                raise V5RawArchiveError("decompressed raw SHA/size does not match source")
        compressed_sha = _sha256(destination)
        compressed_size = destination.stat().st_size
        body = {
            "schema": RAW_ARCHIVE_SCHEMA,
            "version": RAW_ARCHIVE_VERSION,
            "raw_path": str(source),
            "compressed_path": str(destination),
            "raw_sha256": raw_sha,
            "compressed_sha256": compressed_sha,
            "raw_size_bytes": raw_size,
            "compressed_size_bytes": compressed_size,
            "compression_ratio_raw_over_compressed": raw_size / compressed_size if compressed_size else None,
            "compression": "zstd",
            "level": level,
            "decompression_cold_verified": True,
            "retain_uncompressed": bool(retain_uncompressed),
            "summary_only_fallback": False,
        }
        return {**body, "receipt_sha256": _receipt_hash(body)}
    finally:
        if temporary.exists():
            temporary.unlink()


def verify_raw_archive_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(receipt, Mapping):
        raise V5RawArchiveError("raw archive receipt is not a mapping")
    value = dict(receipt)
    receipt_sha = value.pop("receipt_sha256", None)
    if value.get("schema") != RAW_ARCHIVE_SCHEMA or value.get("version") != RAW_ARCHIVE_VERSION or receipt_sha != _receipt_hash(value):
        raise V5RawArchiveError("raw archive receipt hash/schema differs")
    raw = _require_raw(Path(str(value.get("raw_path", ""))))
    compressed = Path(str(value.get("compressed_path", ""))).resolve()
    if compressed.suffix != ".zst" or not compressed.is_file():
        raise V5RawArchiveError("compressed raw artifact is unavailable")
    if _sha256(raw) != value.get("raw_sha256") or raw.stat().st_size != value.get("raw_size_bytes"):
        raise V5RawArchiveError("raw source changed after archive receipt")
    if _sha256(compressed) != value.get("compressed_sha256") or compressed.stat().st_size != value.get("compressed_size_bytes"):
        raise V5RawArchiveError("compressed raw artifact changed after archive receipt")
    if value.get("decompression_cold_verified") is not True or value.get("summary_only_fallback") is not False:
        raise V5RawArchiveError("raw archive lacks cold verification")
    return dict(receipt)


__all__ = [
    "RAW_ARCHIVE_SCHEMA",
    "RAW_ARCHIVE_VERSION",
    "V5RawArchiveError",
    "compress_r013life",
    "verify_raw_archive_receipt",
]
