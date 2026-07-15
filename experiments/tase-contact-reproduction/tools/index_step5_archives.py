#!/usr/bin/env python3
"""Build SHA-backed indexes for retained long-form Step5 documents."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def render(root: Path) -> tuple[str, str]:
    archive = root / "docs/archive/step5"
    rows = []
    for path in sorted(archive.glob("*.md")):
        if path.name == "INDEX.md":
            continue
        content = path.read_bytes()
        rows.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(content).hexdigest(),
                "lines": len(content.splitlines()),
            }
        )
    payload = {"schema": "step5.doc-archive-index/v1", "documents": rows}
    encoded_json = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    lines = [
        "# Step5 archived documents",
        "",
        "These files retain pre-v2 long-form context. They are immutable evidence,",
        "not current runtime configuration.",
        "",
        "| Path | Lines | SHA-256 |",
        "|---|---:|---|",
    ]
    lines.extend(
        f"| `{row['path']}` | {row['lines']} | `{row['sha256']}` |" for row in rows
    )
    return encoded_json, "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    archive = root / "docs/archive/step5"
    encoded_json, encoded_markdown = render(root)
    json_path = archive / "index.json"
    markdown_path = archive / "INDEX.md"
    if args.check:
        if json_path.read_text(encoding="utf-8") != encoded_json:
            raise SystemExit("Step5 archive JSON index is stale")
        if markdown_path.read_text(encoding="utf-8") != encoded_markdown:
            raise SystemExit("Step5 archive Markdown index is stale")
        return 0
    json_path.write_text(encoded_json, encoding="utf-8")
    markdown_path.write_text(encoded_markdown, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
