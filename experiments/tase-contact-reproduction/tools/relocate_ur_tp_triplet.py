#!/usr/bin/env python3
"""Relocate a complete UR TP triplet while preserving its script identity."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath


ALLOWED_CONTROLLER_ROOT = PurePosixPath("/programs/andyl")
SUFFIXES = (".script", ".txt", ".urp")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_controller_dir(raw: str) -> PurePosixPath:
    path = PurePosixPath(raw)
    if not raw.startswith("/") or ".." in path.parts:
        raise ValueError("controller directory must be an absolute normalized path")
    if path != ALLOWED_CONTROLLER_ROOT and ALLOWED_CONTROLLER_ROOT not in path.parents:
        raise ValueError(f"controller directory must stay under {ALLOWED_CONTROLLER_ROOT}")
    return path


def relocate(source_dir: Path, output_dir: Path, basename: str, controller_dir: str) -> dict[str, object]:
    destination = validate_controller_dir(controller_dir)
    sources = {suffix: source_dir / f"{basename}{suffix}" for suffix in SUFFIXES}
    if any(not path.is_file() or path.is_symlink() for path in sources.values()):
        raise ValueError("source must contain one regular non-symlink .script/.txt/.urp triplet")

    script = sources[".script"].read_text(encoding="utf-8")
    txt = sources[".txt"].read_text(encoding="utf-8")
    xml_text = gzip.decompress(sources[".urp"].read_bytes()).decode("utf-8")
    root = ET.fromstring(xml_text)
    old_directory = root.attrib.get("directory")
    if not old_directory or old_directory == str(destination):
        raise ValueError("URP must identify a different source controller directory")
    expected_old_script = f"{old_directory}/{basename}.script"
    old_installation_relative = root.attrib.get("installationRelativePath")
    file_nodes = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "file"]
    cached_nodes = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "cachedContents"]
    if len(file_nodes) != 1 or (file_nodes[0].text or "").strip() != expected_old_script:
        raise ValueError("URP Script-node path does not match basename and source directory")
    if len(cached_nodes) != 1 or (cached_nodes[0].text or "") != script:
        raise ValueError("URP cachedContents does not exactly match companion script")

    relocated_txt = txt.replace(old_directory, str(destination))
    destination_depth = len(destination.relative_to(PurePosixPath("/programs")).parts)
    installation_relative = "../" * destination_depth + "default"
    relocated_xml = xml_text.replace(old_directory, str(destination))
    if old_installation_relative:
        relocated_xml = relocated_xml.replace(
            f'installationRelativePath="{old_installation_relative}"',
            f'installationRelativePath="{installation_relative}"',
            1,
        )
    relocated_root = ET.fromstring(relocated_xml)
    relocated_file_nodes = [
        node for node in relocated_root.iter() if node.tag.rsplit("}", 1)[-1] == "file"
    ]
    if relocated_root.attrib.get("directory") != str(destination):
        raise ValueError("relocated URP directory binding failed")
    if relocated_root.attrib.get("installationRelativePath") != installation_relative:
        raise ValueError("relocated URP installation path binding failed")
    if (relocated_file_nodes[0].text or "").strip() != f"{destination}/{basename}.script":
        raise ValueError("relocated URP Script-node path binding failed")

    output_dir.mkdir(parents=True, exist_ok=True)
    outputs = {suffix: output_dir / f"{basename}{suffix}" for suffix in SUFFIXES}
    outputs[".script"].write_text(script, encoding="utf-8")
    outputs[".txt"].write_text(relocated_txt, encoding="utf-8")
    outputs[".urp"].write_bytes(gzip.compress(relocated_xml.encode("utf-8"), mtime=0))
    return {
        "basename": basename,
        "source_controller_directory": old_directory,
        "controller_directory": str(destination),
        "installation_relative_path": installation_relative,
        "sha256": {suffix: sha256(path) for suffix, path in outputs.items()},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--basename", required=True)
    parser.add_argument("--controller-dir", required=True)
    args = parser.parse_args()
    result = relocate(args.source_dir, args.output_dir, args.basename, args.controller_dir)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
