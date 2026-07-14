#!/usr/bin/env python3
"""Build the audience-facing HTML from the retained Markdown source."""

from __future__ import annotations

import json
import re
from pathlib import Path

import jinja2
import markdown


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "step5d_progress_20260716.md"
TEMPLATE = ROOT / "report_template.html.j2"
METRICS = ROOT / "metrics.json"
OUTPUT = ROOT / "step5d_progress_20260716.html"

REQUIRED_ASSETS = (
    "assets/surface_preparation_abrasives.jpg",
    "assets/ksm_8n_m6_product.jpg",
    "assets/normal_load_vs_time.svg",
    "assets/metric_small_multiples.svg",
    "assets/five_second_stability_windows.svg",
    "assets/motion_force_relationship.svg",
    "assets/previous_contact_control_demo.mp4",
    "assets/previous_contact_control_demo_poster.jpg",
)

REQUIRED_SECTION_IDS = (
    "summary",
    "setup",
    "surface-preparation",
    "why-12n",
    "step5b-to-step5d",
    "comparison-protocol",
    "force-stability",
    "stability-mechanism",
    "completion-boundary",
    "tuning-objective",
    "parameter-search",
    "physical-loop",
    "video-next",
)

AUDIENCE_BANNED_PATTERNS = (
    r"/home/",
    r"/Users/",
    r"worktree",
    r"branch/",
    r"controller_state",
    r"state\s*524",
    r"state\s*30",
    r"stage\s*25",
    r"\bv3[45]\b",
    r"bridge_step",
)


def validate_metrics() -> None:
    data = json.loads(METRICS.read_text(encoding="utf-8"))
    comparison = data["comparison"]
    runs = comparison["runs"]
    for key in ("step5b_a", "step5b_b", "step5d"):
        if runs[key]["metrics"]["bins"] != 550:
            raise ValueError(f"{key}: expected 550 bins")

    expected = {
        "force_std_reduction_pct": 57.07205861014122,
        "force_mae_reduction_pct": 52.68571586643815,
        "within_1n_change_pp": 27.454545454545467,
        "normal_speed_rms_reduction_pct": 48.621767061629015,
    }
    for key, value in expected.items():
        actual = comparison["step5d_change"][key]
        if abs(actual - value) > 1e-9:
            raise ValueError(f"{key}: expected {value}, got {actual}")

    if data["autotune"]["physical_tuning_results_available"]:
        raise ValueError("This report must not claim physical tuning results")


def validate_sources() -> None:
    for path in (SOURCE, TEMPLATE, METRICS):
        if not path.is_file() or path.stat().st_size == 0:
            raise FileNotFoundError(path)
    for relative in REQUIRED_ASSETS:
        path = ROOT / relative
        if not path.is_file() or path.stat().st_size == 0:
            raise FileNotFoundError(path)

    source_text = SOURCE.read_text(encoding="utf-8")
    for section_id in REQUIRED_SECTION_IDS:
        if f'id="{section_id}"' not in source_text:
            raise ValueError(f"Missing section id: {section_id}")


def validate_audience_html(html: str) -> None:
    lowered = html.lower()
    for pattern in AUDIENCE_BANNED_PATTERNS:
        if re.search(pattern, html, flags=re.IGNORECASE):
            raise ValueError(f"Audience-facing HTML contains banned pattern: {pattern}")
    for section_id in REQUIRED_SECTION_IDS:
        if f'id="{section_id}"' not in lowered:
            raise ValueError(f"Rendered HTML missing section id: {section_id}")
    if "KSM-8N" not in html or "8N does not mean 8 newtons" not in html:
        raise ValueError("KSM model-name boundary is missing")
    if "no eligible tuning trial" not in html:
        raise ValueError("Missing no-tuning-result boundary")
    if "placeholder until the new Step5D recording is available" not in html:
        raise ValueError("Missing video placeholder label")


def main() -> None:
    validate_sources()
    validate_metrics()
    source = SOURCE.read_text(encoding="utf-8")
    body = markdown.markdown(
        source,
        extensions=("extra", "md_in_html", "sane_lists"),
        output_format="html5",
    )
    environment = jinja2.Environment(autoescape=True)
    template = environment.from_string(TEMPLATE.read_text(encoding="utf-8"))
    html = template.render(body=body)
    validate_audience_html(html)
    OUTPUT.write_text(html, encoding="utf-8")
    print(json.dumps({"ok": True, "output": OUTPUT.name, "bytes": OUTPUT.stat().st_size}))


if __name__ == "__main__":
    main()
