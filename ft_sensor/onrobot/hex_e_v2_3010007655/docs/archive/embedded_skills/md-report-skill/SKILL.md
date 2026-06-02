---
name: md-report-skill
description: Create and repair Markdown experiment reports that depend on photo, screenshot, or figure evidence, especially robotics, sensor, hardware, bench, and measurement reports where readers must see the images. Use when a Markdown report has opaque filenames instead of visible images, needs upright thumbnails/contact sheets, has sideways or upside-down previews from EXIF orientation, or must connect each visual to a human label, use, and uncertainty.
---

# MD Report Skill

## Mission

Make Markdown experiment reports readable when visual evidence is central.

This skill owns the report layer between raw images and a human-facing
technical report. It does not own domain theory, hardware design, robot motion,
or formal PDF/deck layout.

## Use When

- A report references photos, screenshots, scans, or figures.
- The reader cannot understand the report without seeing the images.
- A report contains opaque IDs such as `IMG_1182.HEIC` as the primary surface.
- HEIC originals need Markdown-safe JPEG/PNG previews.
- Previews or contact sheets are sideways/upside down because EXIF orientation
  was not burned into pixels.
- A robotics, sensor, hardware, bench, or measurement report needs image cards,
  contact sheets, and image-to-purpose mapping.

Do not use this skill for:

- pure text summaries with no visual evidence;
- rendered-page QA for PDFs/decks/posters, which belongs to
  `visual-deliverable-check`;
- general image editing or generation;
- robot/sensor bring-up itself.

## Output Contract

Every visual-evidence Markdown report must have:

- a top-level visual overview with at least one embedded contact sheet or
  grouped overview image, either near the top for evidence-first reports or as
  a closing visual index for procedure-first reports;
- report-local preview assets under an `assets/` directory;
- no direct Markdown embedding of `.HEIC` originals;
- previews and contact sheets must be upright in pixels, not dependent on EXIF
  orientation support in the viewer;
- human labels before filenames;
- each important visual connected to purpose and uncertainty;
- filenames only as secondary metadata, never as the main reading path.
- numbered packout, scope-of-delivery, or kit diagrams must be converted into a
  crosswalk from diagram number to real photo evidence, status, use, and missing
  or uncertain items.

Preferred evidence-first report shape:

1. Human summary.
2. Visual overview.
3. Grouped evidence sections.
4. Test/decision plan.
5. Open questions and file locations.

Preferred procedure-first report shape:

1. Human summary.
2. Reader path or shortest safe procedure.
3. Packout crosswalk, if a numbered kit diagram exists.
4. Procedure, test gates, and decision plan.
5. Grouped evidence sections.
6. Open questions, file locations, and closing visual overview.

For detailed structure, load `references/report-contract.md`.

## Workflow

### 1. Inspect inputs

Identify:

- report root and existing Markdown report;
- raw image folder;
- image formats;
- whether a previous report needs backup.

If images are HEIC, plan to create JPEG previews.

If images appear sideways or upside down, fix the preview pipeline first. Modify
raw originals only when the user explicitly asks for archival correction and a
checksum backup exists.

### 2. Build visual assets

Run:

```bash
python3 scripts/build_visual_assets.py \
  --image-dir <raw-image-dir> \
  --assets-dir <report-root>/assets \
  --title "<short report title>"
```

This creates:

- `assets/thumbnails/`
- `assets/contact_sheets/`
- `assets/visual_assets_manifest.json`

Use contact sheets for overview and thumbnails for per-image cards.

If raw orientation metadata is unreliable, pass a rotation map:

```bash
python3 scripts/build_visual_assets.py \
  --image-dir <raw-image-dir> \
  --assets-dir <report-root>/assets \
  --rotation-map <rotation-map.json>
```

The map may be either `{ "IMG_0001.HEIC": {"rotate_degrees_clockwise": 90} }`
or an orientation manifest with `records[].source_name` and
`records[].rotate_degrees_clockwise`.

### 3. Write or repair the report

Write the Markdown so a reader can understand the report by scrolling:

- put contact sheets near the top for evidence-first reports, or at the end as
  a visual index for procedure-first reports;
- group images by reader task;
- place a thumbnail before or beside the human description;
- keep raw filenames in a small metadata line;
- replace giant file-first tables with grouped visual cards.
- when a numbered packout or kit diagram exists, add a crosswalk table before
  the detailed photo inventory so the reader can map each diagram number to the
  actual photographed object and its current status.

### 4. Validate the report

Run:

```bash
python3 scripts/check_visual_report.py <report.md>
```

The report is not ready if:

- local image links are broken;
- a `.HEIC` is directly embedded;
- embedded JPEG/PNG previews retain EXIF Orientation values such as `3`, `6`,
  or `8` instead of having rotation burned into pixels;
- there is no top-level contact sheet;
- the report still has a large file-first table as the main image inventory.
- a numbered packout diagram is present but the report lacks a number-to-object
  crosswalk.

### 5. Optional rendered check

If the report will be exported to PDF/HTML or judged by rendered pages, route
the final artifact to `visual-deliverable-check` after this skill passes.

## Failure Modes To Avoid

- A table with 30 rows of `IMG_####.HEIC` and no visible images.
- A report that says “photo shows X” but only links a raw HEIC.
- A contact sheet without readable labels.
- A contact sheet where thumbnails are sideways because preview JPEGs kept EXIF
  orientation instead of normal pixels.
- A report where images are grouped by camera filename instead of reader task.
- A procedure-first report that starts with photos before the safe action path.
- An evidence-first report that hides visual evidence below a long abstract/test
  plan.
- A kit report that mentions a numbered box-lid diagram but never maps numbers
  to the real photographed parts.

## Validation And Checkpoints

- Before final handoff, report the asset count, contact sheet count, and
  `check_visual_report.py` result.
- Before replacing a user-facing report, keep a timestamped backup or
  `.before_md_report_skill.md` copy.
- If image conversion fails, stop and report the specific missing tool or file.
