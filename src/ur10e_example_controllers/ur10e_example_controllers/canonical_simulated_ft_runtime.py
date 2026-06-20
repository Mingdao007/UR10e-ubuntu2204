from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Any

from . import canonical_wrench_contract as contract


@dataclass(frozen=True)
class RuntimeConfig:
    canonical_wrench_topic: str = contract.CANONICAL_WRENCH_TOPIC
    simulated_ft_wrench_topic: str = contract.SIMULATED_FT_WRENCH_TOPIC
    simulated_ft_status_topic: str = contract.SIMULATED_FT_STATUS_TOPIC
    contact_state_topic: str = contract.CONTACT_STATE_TOPIC
    controller_status_topic: str = contract.CONTROLLER_STATUS_TOPIC
    run_metadata_topic: str = contract.RUN_METADATA_TOPIC
    publish_hz: float = 50.0
    max_samples: int = 100
    contact_surface_z_m: float = 0.008044839
    nominal_contact_load_n: float = 5.0
    stale_after_s: float = 0.1
    dry_run_summary: Path | None = None
    source_switching_policy: str = "launch_config_or_remap_only_no_controller_logic"
    live_robot_command_authorized: bool = False
    bridge_start_authorized: bool = False
    payload_tcp_safety_writes_authorized: bool = False


def _value(overrides: dict[str, Any], name: str, default: Any) -> Any:
    raw = overrides.get(name, default)
    if hasattr(raw, "perform"):
        return default
    return raw


def build_runtime_config(overrides: dict[str, Any]) -> RuntimeConfig:
    dry_run_raw = _value(overrides, "dry_run_summary", None)
    dry_run_summary = Path(str(dry_run_raw)) if dry_run_raw not in {None, ""} else None
    return RuntimeConfig(
        canonical_wrench_topic=str(_value(overrides, "canonical_wrench_topic", contract.CANONICAL_WRENCH_TOPIC)),
        simulated_ft_wrench_topic=str(
            _value(overrides, "simulated_ft_wrench_topic", contract.SIMULATED_FT_WRENCH_TOPIC)
        ),
        simulated_ft_status_topic=str(
            _value(overrides, "simulated_ft_status_topic", contract.SIMULATED_FT_STATUS_TOPIC)
        ),
        contact_state_topic=str(_value(overrides, "contact_state_topic", contract.CONTACT_STATE_TOPIC)),
        controller_status_topic=str(_value(overrides, "controller_status_topic", contract.CONTROLLER_STATUS_TOPIC)),
        run_metadata_topic=str(_value(overrides, "run_metadata_topic", contract.RUN_METADATA_TOPIC)),
        publish_hz=float(_value(overrides, "publish_hz", 50.0)),
        max_samples=max(1, int(_value(overrides, "max_samples", 100))),
        contact_surface_z_m=float(_value(overrides, "contact_surface_z_m", 0.008044839)),
        nominal_contact_load_n=float(_value(overrides, "nominal_contact_load_n", 5.0)),
        stale_after_s=float(_value(overrides, "stale_after_s", 0.1)),
        dry_run_summary=dry_run_summary,
    )


def _sample_rows(config: RuntimeConfig) -> list[dict[str, Any]]:
    period_s = 1.0 / max(config.publish_hz, 1e-9)
    return [
        {
            "t_s": index * period_s,
            "base_z_m": config.contact_surface_z_m,
        }
        for index in range(config.max_samples)
    ]


def build_dry_run_payload(config: RuntimeConfig) -> dict[str, Any]:
    trace = contract.simulated_ft_trace_from_rows(
        _sample_rows(config),
        contact_surface_z_m=config.contact_surface_z_m,
        nominal_contact_load_n=config.nominal_contact_load_n,
        source_topic=config.simulated_ft_wrench_topic,
    )
    status_rows = []
    contact_rows = []
    controller_rows = []
    for row in trace["rows"]:
        status_rows.append(
            {
                "topic": config.simulated_ft_status_topic,
                "t_s": row["t_s"],
                "sequence": row["sequence"],
                "source": row["source"],
                "valid": row["valid"],
                "status": row["status"],
                "quality": row["quality"],
                "baseline_policy": row["baseline_policy"],
                "diagnostic_flags": row["diagnostic_flags"],
            }
        )
        contact_rows.append(
            {
                "topic": config.contact_state_topic,
                "t_s": row["t_s"],
                "sequence": row["sequence"],
                "contact_state": row["contact_state"],
                "normal_load_n": row["normal_load_n"],
            }
        )
        controller_rows.append(
            {
                "topic": config.controller_status_topic,
                "t_s": row["t_s"],
                "sequence": row["sequence"],
                "status": "ready" if row["valid"] else "hold",
                "reason": "fresh_valid_wrench" if row["valid"] else row["status"],
                "source": row["source"],
            }
        )
    return {
        "schema": "ur10e_canonical_simulated_ft_runtime_dry_run_v1",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "offline_no_motion",
        "live_robot_command_authorized": config.live_robot_command_authorized,
        "bridge_start_authorized": config.bridge_start_authorized,
        "payload_tcp_safety_writes_authorized": config.payload_tcp_safety_writes_authorized,
        "source_switching_policy": config.source_switching_policy,
        "topics": {
            "canonical_wrench": config.canonical_wrench_topic,
            "simulated_ft_wrench": config.simulated_ft_wrench_topic,
            "simulated_ft_status": config.simulated_ft_status_topic,
            "contact_state": config.contact_state_topic,
            "controller_status": config.controller_status_topic,
            "run_metadata": config.run_metadata_topic,
        },
        "config": {
            "publish_hz": config.publish_hz,
            "max_samples": config.max_samples,
            "contact_surface_z_m": config.contact_surface_z_m,
            "nominal_contact_load_n": config.nominal_contact_load_n,
            "stale_after_s": config.stale_after_s,
        },
        "wrench_trace": trace,
        "status_rows": status_rows,
        "contact_state_rows": contact_rows,
        "controller_status_rows": controller_rows,
        "run_metadata": {
            "topic": config.run_metadata_topic,
            "contract": contract.canonical_contract_spec(),
            "claim_tier": "simulated_ft",
            "force_source": contract.SOURCE_SIMULATED_FT,
        },
    }


def write_dry_run_summary(config: RuntimeConfig) -> dict[str, Any]:
    payload = build_dry_run_payload(config)
    if config.dry_run_summary is not None:
        config.dry_run_summary.parent.mkdir(parents=True, exist_ok=True)
        config.dry_run_summary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def _json_msg(payload: dict[str, Any]) -> Any:
    from std_msgs.msg import String

    msg = String()
    msg.data = json.dumps(payload, sort_keys=True)
    return msg


def _wrench_msg(row: dict[str, Any]) -> Any:
    from geometry_msgs.msg import WrenchStamped

    msg = WrenchStamped()
    msg.header.stamp.sec = int(float(row["header"]["stamp_s"]))
    msg.header.stamp.nanosec = int((float(row["header"]["stamp_s"]) % 1.0) * 1_000_000_000)
    msg.header.frame_id = str(row["header"]["frame_id"])
    msg.wrench.force.x = float(row["force_n"][0])
    msg.wrench.force.y = float(row["force_n"][1])
    msg.wrench.force.z = float(row["force_n"][2])
    msg.wrench.torque.x = float(row["torque_nm"][0])
    msg.wrench.torque.y = float(row["torque_nm"][1])
    msg.wrench.torque.z = float(row["torque_nm"][2])
    return msg


def run_ros(config: RuntimeConfig) -> int:
    import rclpy
    from rclpy.node import Node
    from std_msgs.msg import String
    from geometry_msgs.msg import WrenchStamped

    payload = write_dry_run_summary(config)
    rows = payload["wrench_trace"]["rows"]

    rclpy.init()
    node = Node("canonical_simulated_ft_runtime")
    wrench_pub = node.create_publisher(WrenchStamped, config.simulated_ft_wrench_topic, 10)
    canonical_pub = node.create_publisher(String, config.canonical_wrench_topic, 10)
    status_pub = node.create_publisher(String, config.simulated_ft_status_topic, 10)
    contact_pub = node.create_publisher(String, config.contact_state_topic, 10)
    controller_pub = node.create_publisher(String, config.controller_status_topic, 10)
    metadata_pub = node.create_publisher(String, config.run_metadata_topic, 10)

    state = {"index": 0}

    def tick() -> None:
        index = state["index"]
        if index == 0:
            metadata_pub.publish(_json_msg(payload["run_metadata"]))
        if index >= len(rows):
            node.destroy_timer(timer)
            rclpy.shutdown()
            return
        row = rows[index]
        wrench_pub.publish(_wrench_msg(row))
        canonical_pub.publish(_json_msg(row))
        status_pub.publish(_json_msg(payload["status_rows"][index]))
        contact_pub.publish(_json_msg(payload["contact_state_rows"][index]))
        controller_pub.publish(_json_msg(payload["controller_status_rows"][index]))
        state["index"] = index + 1

    timer = node.create_timer(1.0 / max(config.publish_hz, 1e-9), tick)
    try:
        rclpy.spin(node)
    finally:
        if rclpy.ok():
            rclpy.shutdown()
        node.destroy_node()
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish finite offline canonical simulated FT samples.")
    parser.add_argument("--canonical-wrench-topic", default=contract.CANONICAL_WRENCH_TOPIC)
    parser.add_argument("--simulated-ft-wrench-topic", default=contract.SIMULATED_FT_WRENCH_TOPIC)
    parser.add_argument("--simulated-ft-status-topic", default=contract.SIMULATED_FT_STATUS_TOPIC)
    parser.add_argument("--contact-state-topic", default=contract.CONTACT_STATE_TOPIC)
    parser.add_argument("--controller-status-topic", default=contract.CONTROLLER_STATUS_TOPIC)
    parser.add_argument("--run-metadata-topic", default=contract.RUN_METADATA_TOPIC)
    parser.add_argument("--publish-hz", type=float, default=50.0)
    parser.add_argument("--max-samples", type=int, default=100)
    parser.add_argument("--contact-surface-z-m", type=float, default=0.008044839)
    parser.add_argument("--nominal-contact-load-n", type=float, default=5.0)
    parser.add_argument("--stale-after-s", type=float, default=0.1)
    parser.add_argument("--dry-run-summary", default="")
    parser.add_argument("--dry-run-only", action="store_true")
    return parser.parse_args(argv)


def _config_from_args(args: argparse.Namespace) -> RuntimeConfig:
    return build_runtime_config(
        {
            "canonical_wrench_topic": args.canonical_wrench_topic,
            "simulated_ft_wrench_topic": args.simulated_ft_wrench_topic,
            "simulated_ft_status_topic": args.simulated_ft_status_topic,
            "contact_state_topic": args.contact_state_topic,
            "controller_status_topic": args.controller_status_topic,
            "run_metadata_topic": args.run_metadata_topic,
            "publish_hz": args.publish_hz,
            "max_samples": args.max_samples,
            "contact_surface_z_m": args.contact_surface_z_m,
            "nominal_contact_load_n": args.nominal_contact_load_n,
            "stale_after_s": args.stale_after_s,
            "dry_run_summary": args.dry_run_summary,
        }
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = _config_from_args(args)
    if args.dry_run_only:
        payload = write_dry_run_summary(config)
        if config.dry_run_summary is None:
            print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    return run_ros(config)


if __name__ == "__main__":
    raise SystemExit(main())
