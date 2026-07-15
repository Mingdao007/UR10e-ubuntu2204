#!/usr/bin/env python3
"""Verify the immutable v1 tag and its archived file/evidence indexes."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "config/step5/v1_freeze_manifest.json"


def git(*args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args],
        check=True,
        capture_output=True,
    ).stdout


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if payload.get("schema") != "step5d.autotune.v1-freeze/v1":
        raise SystemExit("v1 freeze manifest schema differs")
    tag = payload["tag"]
    target = git("rev-parse", f"{tag}^{{}}").decode().strip()
    if target != payload["tag_target_commit"]:
        raise SystemExit("v1 archive tag target differs")
    for path, expected in payload["files"]:
        actual = hashlib.sha256(git("show", f"{tag}:{path}")).hexdigest()
        if actual != expected:
            raise SystemExit(f"v1 tagged content differs: {path}")
    for relative, expected in payload["evidence_indexes"].items():
        actual = sha(ROOT / relative)
        if actual != expected:
            raise SystemExit(f"v1 evidence index differs: {relative}")
    archive = json.loads(
        (ROOT / "docs/archive/step5/index.json").read_text(encoding="utf-8")
    )
    if archive.get("schema") != "step5.doc-archive-index/v1":
        raise SystemExit("Step5 archive index schema differs")
    for row in archive.get("documents", []):
        if sha(ROOT / row["path"]) != row["sha256"]:
            raise SystemExit(f"Step5 archived document differs: {row['path']}")
    stage_index = json.loads(
        (ROOT / "config/step5/stages/index.json").read_text(encoding="utf-8")
    )
    stages = stage_index.get("stages") or []
    if (
        stage_index.get("schema") != "step5.stage-index/v1"
        or stage_index.get("stage_count") != len(stages)
    ):
        raise SystemExit("Step5 stage index identity or count differs")
    for row in stages:
        if sha(ROOT / row["path"]) != row["sha256"]:
            raise SystemExit(f"Step5 stage manifest differs: {row['path']}")
    if payload["preserved_uncommitted_worktree"].get("mixed_into_v2") is not False:
        raise SystemExit("uncommitted v1 mailbox work was not preserved separately")
    print("step5d_v1_freeze=pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
