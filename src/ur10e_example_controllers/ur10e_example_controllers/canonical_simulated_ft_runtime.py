from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import time
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
    runtime_observation_summary: Path | None = None
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
    observation_raw = _value(overrides, "runtime_observation_summary", None)
    runtime_observation_summary = Path(str(observation_raw)) if observation_raw not in {None, ""} else None
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
        runtime_observation_summary=runtime_observation_summary,
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


def _wrench_to_row(msg: Any) -> dict[str, Any]:
    stamp_s = float(msg.header.stamp.sec) + (float(msg.header.stamp.nanosec) / 1_000_000_000.0)
    return {
        "header": {
            "stamp_s": stamp_s,
            "frame_id": str(msg.header.frame_id),
        },
        "force_n": [
            float(msg.wrench.force.x),
            float(msg.wrench.force.y),
            float(msg.wrench.force.z),
        ],
        "torque_nm": [
            float(msg.wrench.torque.x),
            float(msg.wrench.torque.y),
            float(msg.wrench.torque.z),
        ],
    }


def _first(values: list[Any]) -> Any:
    return values[0] if values else {}


def _observed_counts(observed: dict[str, list[Any]]) -> dict[str, int]:
    return {key: len(value) for key, value in observed.items()}


def run_observed_ros(config: RuntimeConfig) -> dict[str, Any]:
    import rclpy
    from geometry_msgs.msg import WrenchStamped
    from rclpy.context import Context
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.node import Node
    from std_msgs.msg import String

    dry_run_payload = write_dry_run_summary(config)
    rows = dry_run_payload["wrench_trace"]["rows"]
    expected_counts = {
        "canonical_wrench": len(rows),
        "simulated_ft_wrench": len(rows),
        "simulated_ft_status": len(rows),
        "contact_state": len(rows),
        "controller_status": len(rows),
        "run_metadata": 1,
    }
    observed: dict[str, list[Any]] = {key: [] for key in expected_counts}

    context = Context()
    rclpy.init(context=context)
    publisher_node: Node | None = None
    observer_node: Node | None = None
    executor: SingleThreadedExecutor | None = None
    discovery_ready = False
    try:
        publisher_node = Node("canonical_simulated_ft_runtime_observed_publisher", context=context)
        observer_node = Node("canonical_simulated_ft_runtime_observer", context=context)
        executor = SingleThreadedExecutor(context=context)
        executor.add_node(publisher_node)
        executor.add_node(observer_node)

        wrench_pub = publisher_node.create_publisher(WrenchStamped, config.simulated_ft_wrench_topic, 10)
        canonical_pub = publisher_node.create_publisher(String, config.canonical_wrench_topic, 10)
        status_pub = publisher_node.create_publisher(String, config.simulated_ft_status_topic, 10)
        contact_pub = publisher_node.create_publisher(String, config.contact_state_topic, 10)
        controller_pub = publisher_node.create_publisher(String, config.controller_status_topic, 10)
        metadata_pub = publisher_node.create_publisher(String, config.run_metadata_topic, 10)
        publishers = [wrench_pub, canonical_pub, status_pub, contact_pub, controller_pub, metadata_pub]

        observer_node.create_subscription(
            WrenchStamped,
            config.simulated_ft_wrench_topic,
            lambda msg: observed["simulated_ft_wrench"].append(_wrench_to_row(msg)),
            10,
        )
        observer_node.create_subscription(
            String,
            config.canonical_wrench_topic,
            lambda msg: observed["canonical_wrench"].append(json.loads(msg.data)),
            10,
        )
        observer_node.create_subscription(
            String,
            config.simulated_ft_status_topic,
            lambda msg: observed["simulated_ft_status"].append(json.loads(msg.data)),
            10,
        )
        observer_node.create_subscription(
            String,
            config.contact_state_topic,
            lambda msg: observed["contact_state"].append(json.loads(msg.data)),
            10,
        )
        observer_node.create_subscription(
            String,
            config.controller_status_topic,
            lambda msg: observed["controller_status"].append(json.loads(msg.data)),
            10,
        )
        observer_node.create_subscription(
            String,
            config.run_metadata_topic,
            lambda msg: observed["run_metadata"].append(json.loads(msg.data)),
            10,
        )

        discovery_deadline = time.monotonic() + 2.0
        while time.monotonic() < discovery_deadline:
            executor.spin_once(timeout_sec=0.05)
            if all(publisher.get_subscription_count() > 0 for publisher in publishers):
                discovery_ready = True
                break

        state = {"index": 0, "publishing_done": False}

        def tick() -> None:
            index = state["index"]
            if index == 0:
                metadata_pub.publish(_json_msg(dry_run_payload["run_metadata"]))
            if index >= len(rows):
                state["publishing_done"] = True
                publisher_node.destroy_timer(timer)
                return
            row = rows[index]
            wrench_pub.publish(_wrench_msg(row))
            canonical_pub.publish(_json_msg(row))
            status_pub.publish(_json_msg(dry_run_payload["status_rows"][index]))
            contact_pub.publish(_json_msg(dry_run_payload["contact_state_rows"][index]))
            controller_pub.publish(_json_msg(dry_run_payload["controller_status_rows"][index]))
            state["index"] = index + 1

        timer = publisher_node.create_timer(1.0 / max(config.publish_hz, 1e-9), tick)
        deadline = time.monotonic() + max(3.0, (len(rows) / max(config.publish_hz, 1e-9)) + 2.0)
        while time.monotonic() < deadline:
            executor.spin_once(timeout_sec=0.05)
            counts = _observed_counts(observed)
            if state["publishing_done"] and all(counts[key] >= value for key, value in expected_counts.items()):
                break
    finally:
        if executor is not None:
            if publisher_node is not None:
                executor.remove_node(publisher_node)
            if observer_node is not None:
                executor.remove_node(observer_node)
        if publisher_node is not None:
            publisher_node.destroy_node()
        if observer_node is not None:
            observer_node.destroy_node()
        if context.ok():
            rclpy.shutdown(context=context)

    counts = _observed_counts(observed)
    first_wrench = _first(observed["simulated_ft_wrench"])
    first_canonical = _first(observed["canonical_wrench"])
    observed_complete = all(counts[key] >= value for key, value in expected_counts.items())
    artifact = {
        "schema": "ur10e_canonical_simulated_ft_runtime_observation_v1",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "offline_ros2_runtime_observation",
        "claim_tier": "simulated_ft",
        "force_source": contract.SOURCE_SIMULATED_FT,
        "live_robot_command_authorized": config.live_robot_command_authorized,
        "bridge_start_authorized": config.bridge_start_authorized,
        "payload_tcp_safety_writes_authorized": config.payload_tcp_safety_writes_authorized,
        "source_switching_policy": config.source_switching_policy,
        "topics": dry_run_payload["topics"],
        "published_sample_count": len(rows),
        "expected_counts": expected_counts,
        "observed_counts": counts,
        "observed_complete": observed_complete,
        "discovery_ready": discovery_ready,
        "first_wrench": first_wrench,
        "first_canonical_row": first_canonical,
        "first_status_row": _first(observed["simulated_ft_status"]),
        "first_contact_state_row": _first(observed["contact_state"]),
        "first_controller_status_row": _first(observed["controller_status"]),
        "run_metadata": _first(observed["run_metadata"]),
        "summary_path": str(config.runtime_observation_summary) if config.runtime_observation_summary else "",
        "evidence_fields_present": {
            "stamp": bool(first_wrench.get("header", {}).get("stamp_s") is not None),
            "frame_id": bool(first_wrench.get("header", {}).get("frame_id")),
            "source": bool(first_canonical.get("source")),
            "status": bool(first_canonical.get("status")),
            "baseline": bool(first_canonical.get("baseline_policy")),
            "log_evidence": config.runtime_observation_summary is not None,
        },
    }
    if config.runtime_observation_summary is not None:
        config.runtime_observation_summary.parent.mkdir(parents=True, exist_ok=True)
        config.runtime_observation_summary.write_text(
            json.dumps(artifact, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return artifact


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
    parser.add_argument("--runtime-observation-summary", default="")
    parser.add_argument("--dry-run-only", action="store_true")
    parser.add_argument("--observe-runtime-only", action="store_true")
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
            "runtime_observation_summary": args.runtime_observation_summary,
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
    if args.observe_runtime_only or config.runtime_observation_summary is not None:
        payload = run_observed_ros(config)
        if config.runtime_observation_summary is None:
            print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["observed_complete"] else 2
    return run_ros(config)


if __name__ == "__main__":
    raise SystemExit(main())
