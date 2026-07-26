from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class Step5StageName:
    code: float
    slug: str
    title: str


_STAGES: tuple[Step5StageName, ...] = (
    Step5StageName(20.0, "program_bootstrap", "program bootstrap"),
    Step5StageName(22.0, "preposition_to_entry", "preposition to entry"),
    Step5StageName(23.0, "software_baseline_reset", "software baseline reset"),
    Step5StageName(24.0, "first_contact_search_far", "first contact search far"),
    Step5StageName(24.2, "first_contact_search_near", "first contact search near"),
    Step5StageName(24.3, "second_contact_search_far", "second contact search far"),
    Step5StageName(24.4, "second_contact_search_near", "second contact search near"),
    Step5StageName(25.05, "first_contact_detach", "first contact detach"),
    Step5StageName(25.1, "post_latch_lift", "post latch lift"),
    Step5StageName(25.15, "post_latch_orientation_check", "post latch orientation check"),
    Step5StageName(25.2, "attitude_correction", "attitude correction"),
    Step5StageName(25.3, "line_entry_gate", "line entry gate"),
    Step5StageName(25.0, "contact_line_control", "contact line control"),
    Step5StageName(26.0, "unload", "unload"),
    Step5StageName(27.0, "retract", "retract"),
    Step5StageName(29.0, "program_done", "program done"),
)


def step5_stage_name(stage: float, *, tolerance: float = 0.005) -> Step5StageName:
    if not math.isfinite(stage):
        return Step5StageName(stage, "unknown", "unknown")
    for candidate in _STAGES:
        if abs(stage - candidate.code) <= tolerance:
            return candidate
    return Step5StageName(stage, f"stage_{stage:.2f}".replace(".", "_"), f"stage {stage:.2f}")


def stage_slug(stage: float) -> str:
    return step5_stage_name(stage).slug


def stage_title(stage: float) -> str:
    return step5_stage_name(stage).title


def format_stage(stage: float) -> str:
    name = step5_stage_name(stage)
    return f"stage={name.slug} code={stage:.2f}"
