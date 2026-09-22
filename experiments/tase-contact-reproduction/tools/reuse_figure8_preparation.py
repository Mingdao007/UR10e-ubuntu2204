"""Copy a completed Figure-eight preparation into a new attempt directory.

The embedded observation timestamps and hashes are preserved. This utility
copies only preparation artifacts and refuses symlinks or live attempt output.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil


PREPARATION_FILES = (
    "readback-results.json",
    "software_baseline_receipt.json",
    "baseline-frames.json",
    "neutral-hold-receipt.json",
)


def reuse_preparation(source: Path, destination: Path) -> Path:
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if not source.is_dir():
        raise ValueError(f"preparation source is not a directory: {source}")
    if destination.exists() and any(destination.iterdir()):
        raise FileExistsError(f"attempt directory is not empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    for name in PREPARATION_FILES:
        path = source / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"preparation file is missing or symlinked: {path}")
        shutil.copy2(path, destination / name)
    readback = source / "readback"
    if readback.is_dir() and not readback.is_symlink():
        shutil.copytree(readback, destination / "readback")
    else:
        raise ValueError(f"preparation readback directory is missing: {readback}")
    return destination


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    print(reuse_preparation(args.source, args.destination))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
