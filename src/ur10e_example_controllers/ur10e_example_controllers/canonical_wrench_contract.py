from __future__ import annotations

from dataclasses import dataclass, replace
import math
from typing import Any

from . import step5b_contact_control_core as core


Vec3 = tuple[float, float, float]

CONTRACT_VERSION = "ur10e_canonical_wrench_contract_v1"
TRACE_SCHEMA = "ur10e_canonical_wrench_trace_v1"

CANONICAL_WRENCH_TOPIC = "/ur10e/contact/canonical_wrench"
SIMULATED_FT_WRENCH_TOPIC = "/ur10e/contact/simulated_ft/wrench"
SIMULATED_FT_STATUS_TOPIC = "/ur10e/contact/simulated_ft/status"
CONTACT_STATE_TOPIC = "/ur10e/contact/contact_state"
CONTROLLER_STATUS_TOPIC = "/ur10e/contact/controller_status"
RUN_METADATA_TOPIC = "/ur10e/contact/run_metadata"

SOURCE_VIRTUAL_SOFTWARE = "virtual/software force-loop"
SOURCE_SOFTWARE_REPLAY = "software_replay"
SOURCE_SIMULATED_FT = "simulated_ft"
SOURCE_GAZEBO_CONTACT = "gazebo_contact"
SOURCE_REAL_KUNWEI_READ_ONLY = "real_kunwei_read_only"

SOURCE_CLASSES = (
    SOURCE_VIRTUAL_SOFTWARE,
    SOURCE_SOFTWARE_REPLAY,
    SOURCE_SIMULATED_FT,
    SOURCE_GAZEBO_CONTACT,
    SOURCE_REAL_KUNWEI_READ_ONLY,
)

CLAIM_TIER_BY_SOURCE = {
    SOURCE_VIRTUAL_SOFTWARE: "virtual/software force-loop",
    SOURCE_SOFTWARE_REPLAY: "simulated_ft",
    SOURCE_SIMULATED_FT: "simulated_ft",
    SOURCE_GAZEBO_CONTACT: "physical Gazebo collision/contact physics",
    SOURCE_REAL_KUNWEI_READ_ONLY: "real Kunwei read-only",
}

REQUIRED_FRAMES = (
    "world",
    "base",
    "base_link",
    "tool0",
    "flange",
    "ft_sensor",
    "tcp",
    "contact_tip",
    "contact_surface",
    "surface_normal",
)

REQUIRED_SAMPLE_FIELDS = (
    "header",
    "force_n",
    "torque_nm",
    "source",
    "valid",
    "quality",
    "status",
    "baseline_policy",
    "latency_s",
    "stale_after_s",
    "diagnostic_flags",
)


def _norm3(values: Vec3) -> float:
    return math.sqrt(sum(value * value for value in values))


def _dot3(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _as_vec3(values: Any, *, label: str) -> Vec3:
    if not isinstance(values, (list, tuple)) or len(values) != 3:
        raise ValueError(f"{label} must contain exactly three values")
    return (float(values[0]), float(values[1]), float(values[2]))


@dataclass(frozen=True)
class CanonicalWrenchSample:
    stamp_s: float
    frame_id: str
    force_n: Vec3
    torque_nm: Vec3
    source: str
    valid: bool
    quality: str
    status: str
    baseline_policy: str
    latency_s: float
    stale_after_s: float
    diagnostic_flags: tuple[str, ...] = ()
    sequence: int = 0
    contact_state: str = "unknown"
    reaction_normal: Vec3 = (0.0, 0.0, 1.0)
    approach_normal: Vec3 = (0.0, 0.0, -1.0)

    def __post_init__(self) -> None:
        if self.source not in SOURCE_CLASSES:
            raise ValueError(f"unknown canonical wrench source {self.source!r}")
        if self.frame_id not in REQUIRED_FRAMES:
            raise ValueError(f"frame_id {self.frame_id!r} is outside required frame set")
        if not math.isfinite(self.stamp_s):
            raise ValueError("stamp_s must be finite")
        if self.latency_s < 0.0 or self.stale_after_s <= 0.0:
            raise ValueError("latency_s must be nonnegative and stale_after_s must be positive")

    @property
    def claim_tier(self) -> str:
        return CLAIM_TIER_BY_SOURCE[self.source]

    @property
    def force_norm_n(self) -> float:
        return _norm3(self.force_n)

    @property
    def torque_norm_nm(self) -> float:
        return _norm3(self.torque_nm)

    @property
    def normal_load_n(self) -> float:
        return max(0.0, _dot3(self.force_n, self.reaction_normal))

    def with_freshness(self, now_s: float) -> "CanonicalWrenchSample":
        age_s = max(0.0, float(now_s) - self.stamp_s)
        if age_s <= self.stale_after_s and self.valid:
            return replace(self, latency_s=age_s, status="valid")
        flags = tuple(dict.fromkeys((*self.diagnostic_flags, "stale_wrench")))
        return replace(self, valid=False, latency_s=age_s, status="stale", diagnostic_flags=flags)

    def to_row(self) -> dict[str, Any]:
        return {
            "t_s": self.stamp_s,
            "header": {"stamp_s": self.stamp_s, "frame_id": self.frame_id},
            "sequence": self.sequence,
            "source": self.source,
            "valid": self.valid,
            "quality": self.quality,
            "status": self.status,
            "baseline_policy": self.baseline_policy,
            "latency_s": self.latency_s,
            "stale_after_s": self.stale_after_s,
            "diagnostic_flags": list(self.diagnostic_flags),
            "contact_state": self.contact_state,
            "claim_tier": self.claim_tier,
            "force_n": list(self.force_n),
            "torque_nm": list(self.torque_nm),
            "Fx_N": self.force_n[0],
            "Fy_N": self.force_n[1],
            "Fz_N": self.force_n[2],
            "Mx_Nm": self.torque_nm[0],
            "My_Nm": self.torque_nm[1],
            "Mz_Nm": self.torque_nm[2],
            "reaction_normal": list(self.reaction_normal),
            "approach_normal": list(self.approach_normal),
            "normal_load_n": self.normal_load_n,
            "force_norm_n": self.force_norm_n,
            "torque_norm_nm": self.torque_norm_nm,
        }


def canonical_contract_spec() -> dict[str, Any]:
    return {
        "schema": CONTRACT_VERSION,
        "canonical_wrench_topic": CANONICAL_WRENCH_TOPIC,
        "simulated_ft_wrench_topic": SIMULATED_FT_WRENCH_TOPIC,
        "simulated_ft_status_topic": SIMULATED_FT_STATUS_TOPIC,
        "contact_state_topic": CONTACT_STATE_TOPIC,
        "controller_status_topic": CONTROLLER_STATUS_TOPIC,
        "run_metadata_topic": RUN_METADATA_TOPIC,
        "required_input_topics": ["/joint_states", "/tf", "/tf_static", "/clock"],
        "required_frames": list(REQUIRED_FRAMES),
        "source_classes": list(SOURCE_CLASSES),
        "source_switching_policy": "launch_config_or_remap_only_no_controller_logic",
        "consumer_interface": {
            "type": "canonical_wrench_envelope",
            "required_fields": list(REQUIRED_SAMPLE_FIELDS),
            "controller_private_gazebo_topic_dependency_allowed": False,
            "consumer_frame_id": "base",
            "wrench_frame_policy": "canonical topic carries explicit header.frame_id; current Step5b offline consumer requires base-frame wrench",
        },
        "force_frame_semantics": {
            "normal_load_n": "dot(force_base, reaction_normal)",
            "reaction_normal": [0.0, 0.0, 1.0],
            "approach_normal": [0.0, 0.0, -1.0],
            "orientation_target_axis": "approach_normal",
        },
    }


def force_source_lineage_table() -> list[dict[str, Any]]:
    return [
        {
            "source_name": SOURCE_VIRTUAL_SOFTWARE,
            "input_topic_or_file": "/joint_states + /tf + configured virtual surface",
            "output_topic": CANONICAL_WRENCH_TOPIC,
            "frame_id": "base",
            "frequency": "controller/update-loop derived",
            "timestamp_source": "/clock or monotonic replay time",
            "zero_baseline_policy": "software model baseline only",
            "real_transfer_readiness": "not transfer-ready as force proof",
            "known_limits": "kinematic penetration formula; no FT sensor or collision physics",
            "allowed_claim_tier": CLAIM_TIER_BY_SOURCE[SOURCE_VIRTUAL_SOFTWARE],
        },
        {
            "source_name": SOURCE_SOFTWARE_REPLAY,
            "input_topic_or_file": "retained wrench CSV/JSON artifact",
            "output_topic": CANONICAL_WRENCH_TOPIC,
            "frame_id": "base",
            "frequency": "artifact timestamp delta",
            "timestamp_source": "artifact t_monotonic_s or /clock replay",
            "zero_baseline_policy": "recorded software baseline",
            "real_transfer_readiness": "same consumer path if schema validates",
            "known_limits": "replay only; no new hardware observation",
            "allowed_claim_tier": CLAIM_TIER_BY_SOURCE[SOURCE_SOFTWARE_REPLAY],
        },
        {
            "source_name": SOURCE_SIMULATED_FT,
            "input_topic_or_file": SIMULATED_FT_WRENCH_TOPIC,
            "output_topic": CANONICAL_WRENCH_TOPIC,
            "frame_id": "base",
            "frequency": "sim clock sampled",
            "timestamp_source": "/clock",
            "zero_baseline_policy": "simulated zero/no-contact baseline",
            "real_transfer_readiness": "same canonical consumer as Kunwei adapter",
            "known_limits": "not real Kunwei; collision/contact physics separate",
            "allowed_claim_tier": CLAIM_TIER_BY_SOURCE[SOURCE_SIMULATED_FT],
        },
        {
            "source_name": SOURCE_GAZEBO_CONTACT,
            "input_topic_or_file": "Gazebo contact/collision events + wrench adapter",
            "output_topic": CANONICAL_WRENCH_TOPIC,
            "frame_id": "base",
            "frequency": "sim physics update sampled",
            "timestamp_source": "/clock",
            "zero_baseline_policy": "simulated zero/no-contact baseline",
            "real_transfer_readiness": "same consumer once contact events correlate",
            "known_limits": "requires contact-event evidence before physics claim",
            "allowed_claim_tier": CLAIM_TIER_BY_SOURCE[SOURCE_GAZEBO_CONTACT],
        },
        {
            "source_name": SOURCE_REAL_KUNWEI_READ_ONLY,
            "input_topic_or_file": "retained Kunwei KWR75B zeroed wrench log",
            "output_topic": CANONICAL_WRENCH_TOPIC,
            "frame_id": "base",
            "frequency": "logged sample or bridge output frequency",
            "timestamp_source": "t_monotonic_s/t_wall_ns from retained artifact",
            "zero_baseline_policy": "software-baseline-only; no zero_ftsensor/no device tare",
            "real_transfer_readiness": "same canonical consumer path",
            "known_limits": "read-only retained log unless live read-only run is authorized",
            "allowed_claim_tier": CLAIM_TIER_BY_SOURCE[SOURCE_REAL_KUNWEI_READ_ONLY],
        },
    ]


def validate_canonical_sample_row(row: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    for field in REQUIRED_SAMPLE_FIELDS:
        if field not in row:
            issues.append(f"missing:{field}")
    header = row.get("header")
    if not isinstance(header, dict):
        issues.append("header:not_object")
    else:
        if "stamp_s" not in header:
            issues.append("missing:header.stamp_s")
        if "frame_id" not in header:
            issues.append("missing:header.frame_id")
    if row.get("source") not in SOURCE_CLASSES:
        issues.append("source:not_allowed")
    if "force_n" in row:
        try:
            _as_vec3(row["force_n"], label="force_n")
        except ValueError as exc:
            issues.append(str(exc))
    if "torque_nm" in row:
        try:
            _as_vec3(row["torque_nm"], label="torque_nm")
        except ValueError as exc:
            issues.append(str(exc))
    return issues


def trace_payload(samples: list[CanonicalWrenchSample], *, source_topic: str) -> dict[str, Any]:
    rows = [sample.to_row() for sample in samples]
    sample_issues = [
        {"index": index, "issues": issues}
        for index, row in enumerate(rows)
        if (issues := validate_canonical_sample_row(row))
    ]
    max_force = max((float(row["force_norm_n"]) for row in rows), default=0.0)
    max_load = max((float(row["normal_load_n"]) for row in rows), default=0.0)
    sources = sorted({sample.source for sample in samples})
    source = sources[0] if len(sources) == 1 else "mixed"
    return {
        "schema": TRACE_SCHEMA,
        "contract": canonical_contract_spec(),
        "source_topic": source_topic,
        "canonical_wrench_topic": CANONICAL_WRENCH_TOPIC,
        "status_topic": SIMULATED_FT_STATUS_TOPIC if source == SOURCE_SIMULATED_FT else CONTROLLER_STATUS_TOPIC,
        "contact_state_topic": CONTACT_STATE_TOPIC,
        "controller_status_topic": CONTROLLER_STATUS_TOPIC,
        "force_source": source,
        "source_classes": sources,
        "claim_tier": CLAIM_TIER_BY_SOURCE.get(source, "mixed"),
        "frame_policy": "explicit_header_frame_required",
        "baseline_policy": rows[0]["baseline_policy"] if rows else "unknown",
        "reaction_normal": [0.0, 0.0, 1.0],
        "approach_normal": [0.0, 0.0, -1.0],
        "normal_load_definition": "dot(force_base, reaction_normal)",
        "max_force_norm_n": max_force,
        "max_normal_load_n": max_load,
        "sample_count": len(rows),
        "schema_issues": sample_issues,
        "rows": rows,
    }


def simulated_ft_sample(
    *,
    stamp_s: float,
    sequence: int,
    force_n: Vec3,
    torque_nm: Vec3 = (0.0, 0.0, 0.0),
    contact_state: str = "contact",
    diagnostic_flags: tuple[str, ...] = (),
    baseline_policy: str = "simulated_zero_no_contact_baseline",
) -> CanonicalWrenchSample:
    return CanonicalWrenchSample(
        stamp_s=float(stamp_s),
        frame_id="base",
        force_n=force_n,
        torque_nm=torque_nm,
        source=SOURCE_SIMULATED_FT,
        valid=True,
        quality="nominal",
        status="valid",
        baseline_policy=baseline_policy,
        latency_s=0.0,
        stale_after_s=0.1,
        diagnostic_flags=diagnostic_flags,
        sequence=sequence,
        contact_state=contact_state,
    )


def simulated_ft_trace_from_rows(
    rows: list[dict[str, Any]],
    *,
    contact_surface_z_m: float,
    nominal_contact_load_n: float,
    stiffness_n_m: float = 500.0,
    source_topic: str = SIMULATED_FT_WRENCH_TOPIC,
) -> dict[str, Any]:
    samples: list[CanonicalWrenchSample] = []
    for sequence, row in enumerate(rows):
        t_s = float(row["t_s"])
        raw_z = row.get("tcp_z_m", row.get("base_z_m"))
        z_m = None if raw_z is None else float(raw_z)
        penetration_m = max(0.0, contact_surface_z_m - z_m) if z_m is not None else 0.0
        load_n = max(0.0, nominal_contact_load_n + penetration_m * stiffness_n_m)
        contact_state = "contact" if load_n > 0.0 else "no_contact"
        flags = ("simulated_ft_contact_model",) if contact_state == "contact" else ("no_contact_static_baseline",)
        samples.append(
            simulated_ft_sample(
                stamp_s=t_s,
                sequence=sequence,
                force_n=(0.0, 0.0, load_n),
                contact_state=contact_state,
                diagnostic_flags=flags,
            )
        )
    return trace_payload(samples, source_topic=source_topic)


def kunwei_csv_row_to_canonical(row: dict[str, Any], *, sequence: int = 0) -> CanonicalWrenchSample:
    stamp_s = float(row.get("t_monotonic_s") or 0.0)
    force = (
        float(row["fx_n_zeroed"]),
        float(row["fy_n_zeroed"]),
        float(row["fz_n_zeroed"]),
    )
    torque = (
        float(row["mx_nm_zeroed"]),
        float(row["my_nm_zeroed"]),
        float(row["mz_nm_zeroed"]),
    )
    valid = bool(force) and math.isfinite(stamp_s)
    return CanonicalWrenchSample(
        stamp_s=stamp_s,
        frame_id="base",
        force_n=force,
        torque_nm=torque,
        source=SOURCE_REAL_KUNWEI_READ_ONLY,
        valid=valid,
        quality="retained_log",
        status="valid" if valid else "invalid",
        baseline_policy="software-baseline-only-no-device-zero",
        latency_s=0.0,
        stale_after_s=0.1,
        diagnostic_flags=("retained_real_kunwei_read_only_log",),
        sequence=sequence,
        contact_state="contact" if float(row.get("normal_force_n") or 0.0) > 0.0 else "unknown",
    )


def controller_status_from_canonical(sample: CanonicalWrenchSample, *, now_s: float) -> dict[str, Any]:
    fresh = sample.with_freshness(now_s)
    if not fresh.valid or fresh.status == "stale":
        return {
            "topic": CONTROLLER_STATUS_TOPIC,
            "status": "hold",
            "reason": fresh.status if fresh.status == "stale" else "invalid_wrench",
            "valid": False,
            "source": fresh.source,
            "latency_s": fresh.latency_s,
            "diagnostic_flags": list(fresh.diagnostic_flags),
        }
    return {
        "topic": CONTROLLER_STATUS_TOPIC,
        "status": "ready",
        "reason": "fresh_valid_wrench",
        "valid": True,
        "source": fresh.source,
        "latency_s": fresh.latency_s,
        "diagnostic_flags": list(fresh.diagnostic_flags),
    }


def step5b_sample_from_canonical_wrench(
    sample: CanonicalWrenchSample,
    *,
    tcp_pose: tuple[float, float, float, float, float, float],
    robot_stage: float,
    dt_s: float,
) -> core.Step5bSample:
    sensor_ok = 1.0 if sample.valid and sample.status == "valid" else 0.0
    return core.Step5bSample(
        tcp_pose=tcp_pose,
        tcp_wrench=(
            sample.force_n[0],
            sample.force_n[1],
            sample.force_n[2],
            sample.torque_nm[0],
            sample.torque_nm[1],
            sample.torque_nm[2],
        ),
        sensor_ok=sensor_ok,
        robot_stage=robot_stage,
        dt_s=dt_s,
    )
