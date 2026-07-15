#!/usr/bin/env python3
"""Build the audience-facing HTML from the retained Markdown source."""

from __future__ import annotations

import json
import re
from pathlib import Path

import jinja2
import markdown


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "ur10e_force_control_progress_20260716.md"
TEMPLATE = ROOT / "report_template.html.j2"
METRICS = ROOT / "metrics.json"
OUTPUT = ROOT / "ur10e_force_control_progress_20260716.html"

REQUIRED_ASSETS = (
    "assets/surface_preparation_abrasives.jpg",
    "assets/ksm_8n_m6_product.jpg",
    "assets/normal_load_vs_time.svg",
    "assets/metric_small_multiples.svg",
    "assets/five_second_stability_windows.svg",
    "assets/motion_force_relationship.svg",
    "assets/latest_constrained_controller_contact_demo.mp4",
    "assets/latest_constrained_controller_contact_demo_poster.jpg",
)

REQUIRED_SECTION_IDS = (
    "summary",
    "setup",
    "surface-preparation",
    "why-12n",
    "controller-realization",
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

MAX_AUDIENCE_WORDS = 1600
MAX_PARAGRAPH_WORDS = 45


def validate_metrics() -> None:
    data = json.loads(METRICS.read_text(encoding="utf-8"))
    comparison = data["comparison"]
    runs = comparison["runs"]
    for key in ("baseline_a", "baseline_b", "constrained"):
        if runs[key]["metrics"]["bins"] != 550:
            raise ValueError(f"{key}: expected 550 bins")

    expected = {
        "force_std_reduction_pct": 57.07205861014122,
        "force_mae_reduction_pct": 52.68571586643815,
        "within_1n_change_pp": 27.454545454545467,
        "normal_speed_rms_reduction_pct": 48.621767061629015,
    }
    for key, value in expected.items():
        actual = comparison["constrained_change"][key]
        if abs(actual - value) > 1e-9:
            raise ValueError(f"{key}: expected {value}, got {actual}")

    pre_sanding = data["surface_preparation"]["pre_sanding_context"]
    if pre_sanding["bins"] != 550 or pre_sanding["target_force_n"] != 5.0:
        raise ValueError("Pre-sanding context must retain 550 bins and its original 5 N target")
    if not pre_sanding["comparison_role"].startswith("visual context only"):
        raise ValueError("Pre-sanding run must remain outside the matched headline comparison")

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
    if "Latest constrained-controller contact recording" not in html:
        raise ValueError("Missing latest constrained-controller video label")
    if re.search(r"step5[bd]", html, flags=re.IGNORECASE):
        raise ValueError("Internal controller-stage labels remain in audience-facing HTML")
    if "placeholder" in html.lower():
        raise ValueError("Obsolete video placeholder wording remains")

    for details in re.findall(r"<details\b[^>]*>(.*?)</details>", html, flags=re.IGNORECASE | re.DOTALL):
        if re.search(r"\[[^\]]+\]\(#[^)]+\)", details):
            raise ValueError("Unrendered Markdown link remains inside a disclosure")
        if re.search(r"(?:^|\n)\s*-\s+\S", details):
            raise ValueError("Unrendered Markdown list remains inside a disclosure")
    if re.search(r'<div class="pipeline"[^>]*>\s*<p\b', html, flags=re.IGNORECASE):
        raise ValueError("Pipeline contains an injected Markdown paragraph")

    content = re.sub(r"<(style|script)\b[^>]*>.*?</\1>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    content = re.sub(r"<[^>]+>", " ", content)
    audience_words = re.findall(r"\b[\w±≤≥→−–—.]+\b", content)
    if len(audience_words) > MAX_AUDIENCE_WORDS:
        raise ValueError(f"Audience-facing HTML is too text-heavy: {len(audience_words)} words")

    for index, paragraph in enumerate(re.findall(r"<p\b[^>]*>(.*?)</p>", html, flags=re.IGNORECASE | re.DOTALL), 1):
        paragraph_text = re.sub(r"<[^>]+>", " ", paragraph)
        paragraph_words = re.findall(r"\b[\w±≤≥→−–—.]+\b", paragraph_text)
        if len(paragraph_words) > MAX_PARAGRAPH_WORDS:
            raise ValueError(f"Paragraph {index} is too dense: {len(paragraph_words)} words")


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
