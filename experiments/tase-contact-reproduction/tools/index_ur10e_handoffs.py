#!/usr/bin/env python3
"""Index UR10e-related handoffs without modifying historical files."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


NAME_PATTERN = re.compile(r"ur10e|step[456]|tase|kunwei|bridge|force", re.I)
CONTENT_PATTERN = re.compile(rb"UR10e|Step[456]|TASE|Kunwei", re.I)


def build(root: Path) -> tuple[str, str]:
    rows = []
    for path in sorted(root.glob("*.md")):
        if path.name in {"UR10E_HANDOFF_INDEX.md"}:
            continue
        content = path.read_bytes()
        if NAME_PATTERN.search(path.name) is None and CONTENT_PATTERN.search(content[:8192]) is None:
            continue
        rows.append(
            {
                "path": str(path.resolve()),
                "sha256": hashlib.sha256(content).hexdigest(),
                "lines": len(content.splitlines()),
                "bytes": len(content),
            }
        )
    payload = {
        "schema": "ur10e.handoff-index/v1",
        "policy": "historical_files_are_immutable; new_handoffs_max_120_lines",
        "document_count": len(rows),
        "total_lines": sum(row["lines"] for row in rows),
        "documents": rows,
    }
    encoded_json = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    largest = sorted(rows, key=lambda row: (-row["lines"], row["path"]))[:20]
    newest = list(reversed(rows))[:20]
    md = [
        "# UR10e handoff index",
        "",
        "Historical handoffs keep their original paths and SHA-256 identities.",
        "The full searchable inventory is `UR10E_HANDOFF_INDEX.json`.",
        "",
        f"- Documents: {len(rows)}",
        f"- Total historical lines: {sum(row['lines'] for row in rows)}",
        "- New handoff hard cap: 120 lines",
        "",
        "## Largest retained files",
        "",
        "| File | Lines | SHA-256 |",
        "|---|---:|---|",
    ]
    md.extend(
        f"| `{Path(row['path']).name}` | {row['lines']} | `{row['sha256']}` |"
        for row in largest
    )
    md.extend(
        [
            "",
            "## Latest indexed files",
            "",
            "| File | Lines | SHA-256 |",
            "|---|---:|---|",
        ]
    )
    md.extend(
        f"| `{Path(row['path']).name}` | {row['lines']} | `{row['sha256']}` |"
        for row in newest
    )
    return encoded_json, "\n".join(md) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/home/andy/codex_handoffs"))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    json_path = root / "UR10E_HANDOFF_INDEX.json"
    md_path = root / "UR10E_HANDOFF_INDEX.md"
    encoded_json, encoded_md = build(root)
    if args.check:
        if json_path.read_text(encoding="utf-8") != encoded_json:
            raise SystemExit("UR10E_HANDOFF_INDEX.json is stale")
        if md_path.read_text(encoding="utf-8") != encoded_md:
            raise SystemExit("UR10E_HANDOFF_INDEX.md is stale")
        return 0
    json_path.write_text(encoded_json, encoding="utf-8")
    md_path.write_text(encoded_md, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
