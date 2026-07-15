#!/usr/bin/env python3
"""Generate immutable per-stage views from the frozen v1 compatibility table."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SAFE_ID = re.compile(r"[a-zA-Z0-9_.-]+\Z")


def encoded(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def generate(root: Path, *, check: bool) -> None:
    table_path = root / "config/step5_stage_table.json"
    table = json.loads(table_path.read_text(encoding="utf-8"))
    stages = table.get("stages")
    if not isinstance(stages, list):
        raise SystemExit("legacy Step5 table has no stages array")
    destination = root / "config/step5/stages"
    entries = []
    expected_paths: set[Path] = set()
    for stage in stages:
        stage_id = stage.get("id") if isinstance(stage, dict) else None
        if not isinstance(stage_id, str) or SAFE_ID.fullmatch(stage_id) is None:
            raise SystemExit(f"unsafe stage id: {stage_id!r}")
        path = destination / f"{stage_id}.json"
        content = encoded(
            {
                "schema": "step5.stage-manifest/v1",
                "source": "config/step5_stage_table.json",
                "stage": stage,
            }
        )
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        expected_paths.add(path)
        if check:
            if not path.is_file() or path.read_text(encoding="utf-8") != content:
                raise SystemExit(f"stale Step5 stage manifest: {path}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        entries.append(
            {
                "id": stage_id,
                "path": path.relative_to(root).as_posix(),
                "sha256": digest,
                "active": stage.get("active") is True,
                "retained": stage.get("retained") is True,
            }
        )
    index_path = destination / "index.json"
    actual_paths = set(destination.glob("*.json")) - {index_path}
    if actual_paths != expected_paths:
        raise SystemExit(
            "Step5 stage manifest set differs: "
            f"missing={sorted(str(path) for path in expected_paths - actual_paths)} "
            f"extra={sorted(str(path) for path in actual_paths - expected_paths)}"
        )
    index = encoded(
        {
            "schema": "step5.stage-index/v1",
            "legacy_source": "config/step5_stage_table.json",
            "legacy_source_sha256": hashlib.sha256(table_path.read_bytes()).hexdigest(),
            "stage_count": len(entries),
            "stages": entries,
        }
    )
    if check:
        if not index_path.is_file() or index_path.read_text(encoding="utf-8") != index:
            raise SystemExit("Step5 stage index is stale")
    else:
        destination.mkdir(parents=True, exist_ok=True)
        index_path.write_text(index, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    generate(args.root.resolve(), check=args.check)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
