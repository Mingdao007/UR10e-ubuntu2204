#!/usr/bin/env python3
"""Validate that a Markdown visual report exposes image evidence readably."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote

from PIL import Image


IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
RAW_IMG_RE = re.compile(r"\bIMG_\d{4}\.(?:HEIC|HEIF|JPE?G|PNG|WEBP)\b", re.I)
PACKOUT_HEADING_RE = re.compile(
    r"^##\s+.*(?:包装盒.*编号对照|packout.*crosswalk|scope[- ]?of[- ]?delivery).*",
    re.I,
)
BAD_ORIENTATION_VALUES = {3, 6, 8}
ORIENTATION_TAG = 274


def strip_angle(path: str) -> str:
    path = path.strip()
    if path.startswith("<") and path.endswith(">"):
        return path[1:-1]
    return path


def is_remote(path: str) -> bool:
    return re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", path) is not None


def resolve_image_path(target: str, report_dir: Path) -> Path | None:
    target = strip_angle(target)
    if is_remote(target):
        return None
    target = unquote(target.split("#", 1)[0])
    p = Path(target)
    if not p.is_absolute():
        p = report_dir / p
    return p


def residual_orientation(path: Path) -> int | None:
    if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
        return None
    with Image.open(path) as im:
        value = im.getexif().get(ORIENTATION_TAG)
    if value is None:
        return None
    return int(value)


def detect_large_file_first_table(lines: list[str]) -> bool:
    table_run = []
    for line in lines:
        if line.lstrip().startswith("|") and "|" in line.rstrip()[1:]:
            table_run.append(line)
        else:
            if _table_is_bad(table_run):
                return True
            table_run = []
    return _table_is_bad(table_run)


def _table_is_bad(table: list[str]) -> bool:
    if len(table) < 12:
        return False
    raw_refs = sum(1 for line in table if RAW_IMG_RE.search(line))
    image_embeds = sum(1 for line in table if IMAGE_RE.search(line))
    header = "\n".join(table[:2]).lower()
    fileish_header = "file" in header or "文件" in header or "photo" in header
    return fileish_header and raw_refs >= 10 and image_embeds == 0


def find_packout_crosswalk(lines: list[str]) -> tuple[list[str] | None, list[int]]:
    start = None
    for idx, line in enumerate(lines):
        if PACKOUT_HEADING_RE.search(line):
            start = idx
            break
    if start is None:
        return None, list(range(1, 11))

    end = len(lines)
    for idx in range(start + 1, len(lines)):
        if lines[idx].startswith("## "):
            end = idx
            break

    section = lines[start:end]
    section_text = "\n".join(section)
    missing = [
        number
        for number in range(1, 11)
        if not re.search(rf"(?m)^\|\s*{number}\s*\|", section_text)
    ]
    return section, missing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--min-images", type=int, default=4)
    parser.add_argument(
        "--require-top-overview",
        action="store_true",
        help="Require an embedded overview/contact-sheet image in the first 100 lines.",
    )
    parser.add_argument(
        "--require-packout-crosswalk",
        action="store_true",
        help="Require a numbered packout/scope-of-delivery crosswalk covering items 1-10.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    report = args.report.expanduser().resolve()
    if not report.is_file():
        print(f"report not found: {report}", file=sys.stderr)
        return 2

    text = report.read_text()
    lines = text.splitlines()
    report_dir = report.parent
    errors: list[str] = []
    warnings: list[str] = []

    images = IMAGE_RE.findall(text)
    if len(images) < args.min_images:
        errors.append(f"expected at least {args.min_images} embedded images, found {len(images)}")

    if not any("contact_sheet" in img or "contact_sheets" in img for img in images):
        errors.append("no embedded contact sheet found")

    if args.require_top_overview:
        first_visual_line = None
        for idx, line in enumerate(lines[:100], start=1):
            if IMAGE_RE.search(line) and (
                "contact_sheet" in line or "contact_sheets" in line or "visual overview" in line.lower()
            ):
                first_visual_line = idx
                break
        if first_visual_line is None:
            errors.append("no contact-sheet/visual-overview image in first 100 lines")
        elif first_visual_line > 45:
            warnings.append(f"first embedded image appears late at line {first_visual_line}")

    for img in images:
        clean = strip_angle(img).split("#", 1)[0]
        if clean.lower().endswith((".heic", ".heif")):
            errors.append(f"direct HEIC/HEIF embed is not Markdown-safe: {img}")
        resolved = resolve_image_path(img, report_dir)
        if resolved is not None and not resolved.exists():
            errors.append(f"embedded image does not exist: {img}")
        if resolved is not None and resolved.exists():
            try:
                orientation = residual_orientation(resolved)
            except Exception as exc:
                errors.append(f"embedded image cannot be opened for orientation check: {img} ({exc})")
            else:
                if orientation in BAD_ORIENTATION_VALUES:
                    errors.append(
                        f"embedded image has residual EXIF Orientation={orientation}; "
                        f"burn rotation into pixels: {img}"
                    )

    if detect_large_file_first_table(lines):
        errors.append("large file-first image table detected; use visual cards/grouped thumbnails instead")

    if args.require_packout_crosswalk:
        packout_section, missing_packout_numbers = find_packout_crosswalk(lines)
        if packout_section is None:
            errors.append("no numbered packout/scope-of-delivery crosswalk section found")
        elif missing_packout_numbers:
            missing = ", ".join(str(n) for n in missing_packout_numbers)
            errors.append(f"packout crosswalk missing numbered items: {missing}")

    text_without_image_links = IMAGE_RE.sub("", text)
    raw_ref_count = len(RAW_IMG_RE.findall(text_without_image_links))
    if raw_ref_count > 80:
        warnings.append(f"many raw camera filenames remain in prose/tables: {raw_ref_count}")

    result = {
        "ok": not errors,
        "report": str(report),
        "embedded_image_count": len(images),
        "require_top_overview": args.require_top_overview,
        "require_packout_crosswalk": args.require_packout_crosswalk,
        "errors": errors,
        "warnings": warnings,
    }
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        if errors:
            print("FAIL visual report check")
            for error in errors:
                print(f"- {error}")
        else:
            print("PASS visual report check")
        for warning in warnings:
            print(f"warning: {warning}")
        print(f"embedded_image_count={len(images)}")

    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
