# Markdown Visual Experiment Report Contract

## Minimum Structure

Use this structure for hardware, robotics, sensor, bench, and measurement
reports when the reader's first task is to inspect the photo evidence:

```markdown
# <Experiment / kit / bench report title>

## 核心结论

Short human-readable summary. Mention model, date, goal, and decision.

## Visual Overview

![Contact sheet 1](assets/contact_sheets/contact_sheet_01.jpg)

One short paragraph explaining how to read the overview.

## Evidence Groups

### <Group Name>

![Human label](assets/thumbnails/IMG_0001.jpg)

**Human label.** What the image shows and why it matters.
Metadata: original `IMG_0001.HEIC`; use: immediate / backup / document evidence;
uncertainty: what must still be checked.

## Test / Decision Plan

Procedure, gates, and acceptance criteria.

## Open Questions

Only questions that cannot be resolved from the current evidence.

## File Locations

Raw originals, assets, manifest, and backup paths.
```

Use this structure when the reader's first task is to perform a safe setup,
bench test, or measurement procedure:

```markdown
# <Experiment / kit / bench report title>

## 核心结论

Short human-readable summary. Mention model, date, goal, and decision.

## 现场最短路径

Tell the reader which sections to use first and what not to touch.

## <Packout / Scope-of-Delivery Crosswalk>

If a numbered box-lid, kit diagram, or scope-of-delivery image exists, map each
number to the photographed object, confirmation state, use, and uncertainty.

## <Procedure / Bring-Up / Test Plan>

Procedure, gates, and acceptance criteria.

## Evidence Groups

Grouped image cards with thumbnails, human labels, purpose, and uncertainty.

## Open Questions

Only questions that cannot be resolved from the current evidence.

## File Locations

Raw originals, assets, manifest, and backup paths.

## Visual Overview

Closing contact sheets or grouped overview images.
```

## Visual Rules

- Prefer JPEG/PNG previews in Markdown.
- Keep HEIC originals as archival sources only.
- Burn orientation into preview pixels; do not rely on EXIF Orientation support
  from the Markdown viewer.
- If raw originals must be rotated, keep a checksum backup and an orientation
  manifest before replacing them.
- Evidence-first reports should put at least one contact sheet in the first
  screenful after the summary.
- Procedure-first reports may put contact sheets at the end, but the first
  screenful must tell the reader where to go first and must show any critical
  object thumbnails needed for safe operation inside the procedure.
- Use grouped evidence sections rather than a single giant inventory table.
- The image label should be a thing name: `Compute Box port panel`, not
  `IMG_1203`.
- Put file names in metadata after the human label.
- Mark uncertainty locally near the image, not only in a final notes section.
- For numbered packout or scope-of-delivery diagrams, create a crosswalk table
  with: diagram number, diagram label, corresponding photo(s), confirmation
  state, use, and missing/uncertain items.

## Required Gates

Before saying the report is ready:

- all embedded images exist on disk;
- no embedded image path ends in `.HEIC`;
- embedded JPEG/PNG previews have no residual EXIF Orientation values such as
  `3`, `6`, or `8`;
- contact sheets are visually upright under a manual spot check;
- a contact sheet is embedded somewhere in the report;
- if the report is evidence-first, a contact sheet is embedded near the top;
- important image groups have visible thumbnails;
- no large file-first table is the main evidence surface;
- the report can be understood by a reader who does not know the camera roll.
- if a numbered packout diagram exists, each visible number is mapped to real
  photo evidence or explicitly marked missing/uncertain.
