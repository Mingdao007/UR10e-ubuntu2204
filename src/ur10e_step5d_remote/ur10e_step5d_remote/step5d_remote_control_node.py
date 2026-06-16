from __future__ import annotations

import sys
from pathlib import Path

from .replay_shadow import DEFAULT_CONFIG, run_shadow_replay


def main(args: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if args is None else args)
    config_path = DEFAULT_CONFIG
    enable_motion = False
    for index, item in enumerate(argv):
        if item in {"--config", "-c"} and index + 1 < len(argv):
            config_path = Path(argv[index + 1])
        if item == "--enable-motion":
            enable_motion = True
    if enable_motion:
        raise RuntimeError("Step5d remote-control node refuses --enable-motion in v1 shadow mode")

    try:
        import rclpy  # type: ignore
        from rclpy.node import Node  # type: ignore
    except Exception:
        summary = run_shadow_replay(config_path=config_path)
        print(f"Step5d shadow artifact: {summary['artifact_dir']}")
        return 0

    rclpy.init(args=args)
    node = Node("step5d_remote_shadow")
    try:
        node.declare_parameter("enable_motion", False)
        node.declare_parameter("config", str(config_path))
        motion_param = node.get_parameter("enable_motion").value
        if str(motion_param).lower() in {"true", "1", "yes"}:
            raise RuntimeError("Step5d remote shadow launch refuses enable_motion=true")
        config_param = Path(str(node.get_parameter("config").value))
        summary = run_shadow_replay(config_path=config_param)
        node.get_logger().info(f"Step5d shadow artifact: {summary['artifact_dir']}")
        return 0
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
