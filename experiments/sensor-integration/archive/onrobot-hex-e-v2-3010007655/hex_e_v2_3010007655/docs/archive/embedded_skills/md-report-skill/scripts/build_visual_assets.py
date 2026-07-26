#!/usr/bin/env python3
"""Build report-local thumbnails and contact sheets for Markdown reports."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps


IMAGE_EXTS = {".heic", ".heif", ".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}


def natural_key(path: Path) -> tuple:
    stem = path.stem
    parts: list[int | str] = []
    buf = ""
    for ch in stem:
        if ch.isdigit():
            buf += ch
        else:
            if buf:
                parts.append(int(buf))
                buf = ""
            parts.append(ch.lower())
    if buf:
        parts.append(int(buf))
    return tuple(parts)


def load_font(size: int) -> ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except Exception:
            pass
    return ImageFont.load_default()


def convert_with_sips(src: Path, dst: Path, max_px: int) -> bool:
    if not shutil.which("sips"):
        return False
    cmd = [
        "sips",
        "-s",
        "format",
        "jpeg",
        "-Z",
        str(max_px),
        str(src),
        "--out",
        str(dst),
    ]
    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return result.returncode == 0 and dst.exists()


def normalize_rotation(degrees: int | float | str | None) -> int:
    if degrees in (None, ""):
        return 0
    normalized = int(degrees) % 360
    if normalized not in {0, 90, 180, 270}:
        raise ValueError(f"rotation must be one of 0, 90, 180, 270 degrees: {degrees}")
    return normalized


def load_rotation_map(path: Path | None) -> dict[str, int]:
    if path is None:
        return {}
    data = json.loads(path.expanduser().read_text(encoding="utf-8"))
    mapping: dict[str, int] = {}

    if isinstance(data, dict) and isinstance(data.get("records"), list):
        for record in data["records"]:
            if not isinstance(record, dict):
                continue
            name = record.get("source_name") or record.get("filename") or record.get("file")
            if name:
                mapping[str(name)] = normalize_rotation(record.get("rotate_degrees_clockwise"))
        return mapping

    if not isinstance(data, dict):
        raise ValueError("rotation map must be a JSON object or an orientation manifest with records")

    for name, value in data.items():
        if isinstance(value, dict):
            value = value.get("rotate_degrees_clockwise")
        mapping[str(name)] = normalize_rotation(value)
    return mapping


def rotation_for(src: Path, rotation_map: dict[str, int]) -> int:
    if src.name in rotation_map:
        return rotation_map[src.name]
    if src.stem in rotation_map:
        return rotation_map[src.stem]
    return 0


def write_normalized_jpeg(src: Path, dst: Path, max_px: int, rotate_degrees_clockwise: int = 0) -> dict:
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        if rotate_degrees_clockwise:
            # Pillow rotates counter-clockwise for positive angles.
            im = im.rotate(-rotate_degrees_clockwise, expand=True)
        im.thumbnail((max_px, max_px), Image.LANCZOS)
        im.save(dst, quality=88, optimize=True)
        return {
            "orientation_normalized": True,
            "manual_rotation_degrees_clockwise": rotate_degrees_clockwise,
            "thumbnail_width": im.width,
            "thumbnail_height": im.height,
        }


def make_preview(src: Path, dst: Path, max_px: int, rotate_degrees_clockwise: int = 0) -> dict:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.suffix.lower() in {".heic", ".heif"}:
        with tempfile.TemporaryDirectory(prefix="md-report-skill-") as tmpdir:
            tmp_jpeg = Path(tmpdir) / f"{src.stem}.jpg"
            if convert_with_sips(src, tmp_jpeg, max_px):
                return write_normalized_jpeg(tmp_jpeg, dst, max_px, rotate_degrees_clockwise)
    try:
        return write_normalized_jpeg(src, dst, max_px, rotate_degrees_clockwise)
    except Exception as exc:
        with tempfile.TemporaryDirectory(prefix="md-report-skill-") as tmpdir:
            tmp_jpeg = Path(tmpdir) / f"{src.stem}.jpg"
            if convert_with_sips(src, tmp_jpeg, max_px):
                return write_normalized_jpeg(tmp_jpeg, dst, max_px, rotate_degrees_clockwise)
        raise RuntimeError(f"failed to convert {src}: {exc}") from exc


def draw_contact_sheet(
    entries: list[dict],
    sheet_path: Path,
    title: str,
    columns: int,
    cell_w: int,
    cell_h: int,
) -> None:
    margin = 28
    header_h = 70
    rows = math.ceil(len(entries) / columns)
    width = margin * 2 + columns * cell_w
    height = margin * 2 + header_h + rows * cell_h
    sheet = Image.new("RGB", (width, height), (247, 247, 247))
    draw = ImageDraw.Draw(sheet)
    title_font = load_font(28)
    label_font = load_font(22)
    draw.text((margin, margin), title, fill=(20, 20, 20), font=title_font)

    for idx, entry in enumerate(entries):
        row = idx // columns
        col = idx % columns
        x = margin + col * cell_w
        y = margin + header_h + row * cell_h
        draw.rectangle(
            (x, y, x + cell_w - 18, y + cell_h - 18),
            outline=(205, 205, 205),
            width=2,
        )
        draw.text((x + 12, y + 10), entry["source_name"], fill=(0, 0, 0), font=label_font)
        with Image.open(entry["thumbnail_abs"]) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((cell_w - 46, cell_h - 86), Image.LANCZOS)
            ix = x + (cell_w - 18 - im.width) // 2
            iy = y + 54 + (cell_h - 86 - im.height) // 2
            sheet.paste(im, (ix, iy))

    sheet_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(sheet_path, quality=90, optimize=True)


def relpath(path: Path, base: Path) -> str:
    return path.relative_to(base).as_posix()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-dir", required=True, type=Path)
    parser.add_argument("--assets-dir", required=True, type=Path)
    parser.add_argument("--title", default="Visual evidence")
    parser.add_argument("--thumb-size", type=int, default=900)
    parser.add_argument("--sheet-size", type=int, default=420)
    parser.add_argument("--columns", type=int, default=4)
    parser.add_argument("--per-sheet", type=int, default=13)
    parser.add_argument(
        "--rotation-map",
        type=Path,
        help="Optional JSON map or orientation manifest with rotate_degrees_clockwise values.",
    )
    args = parser.parse_args()

    image_dir = args.image_dir.expanduser().resolve()
    assets_dir = args.assets_dir.expanduser().resolve()
    report_root = assets_dir.parent
    thumb_dir = assets_dir / "thumbnails"
    sheet_dir = assets_dir / "contact_sheets"

    if not image_dir.is_dir():
        print(f"image directory not found: {image_dir}", file=sys.stderr)
        return 2

    images = sorted(
        [p for p in image_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS],
        key=natural_key,
    )
    if not images:
        print(f"no supported images found in {image_dir}", file=sys.stderr)
        return 2

    rotation_map = load_rotation_map(args.rotation_map)
    entries = []
    for src in images:
        thumb_path = thumb_dir / f"{src.stem}.jpg"
        rotation = rotation_for(src, rotation_map)
        preview_meta = make_preview(src, thumb_path, args.thumb_size, rotation)
        entries.append(
            {
                "source_abs": str(src),
                "source_name": src.name,
                "thumbnail_abs": str(thumb_path),
                "thumbnail": relpath(thumb_path, report_root),
                **preview_meta,
            }
        )

    sheet_entries = []
    for start in range(0, len(entries), args.per_sheet):
        batch = entries[start : start + args.per_sheet]
        sheet_idx = len(sheet_entries) + 1
        sheet_path = sheet_dir / f"contact_sheet_{sheet_idx:02d}.jpg"
        first = batch[0]["source_name"]
        last = batch[-1]["source_name"]
        draw_contact_sheet(
            batch,
            sheet_path,
            f"{args.title} {sheet_idx}: {first} - {last}",
            args.columns,
            args.sheet_size,
            args.sheet_size + 35,
        )
        sheet_entries.append(
            {
                "path_abs": str(sheet_path),
                "path": relpath(sheet_path, report_root),
                "first": first,
                "last": last,
                "count": len(batch),
            }
        )

    manifest = {
        "schema_version": 1,
        "image_dir": str(image_dir),
        "assets_dir": str(assets_dir),
        "thumbnail_count": len(entries),
        "contact_sheet_count": len(sheet_entries),
        "thumbnails": entries,
        "contact_sheets": sheet_entries,
    }
    manifest_path = assets_dir / "visual_assets_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")

    print(
        json.dumps(
            {
                "ok": True,
                "thumbnail_count": len(entries),
                "contact_sheet_count": len(sheet_entries),
                "manifest": str(manifest_path),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
