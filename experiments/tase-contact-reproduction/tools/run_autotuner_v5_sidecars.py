#!/usr/bin/env python3
"""Explicit-launch entrypoint for the Autotuner V5 observation sidecars."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from step6_figure8_autotune_v1.v5_camera_observer import (  # noqa: E402
    ArmCameraObserverV1,
    FFmpegRTSPFrameSourceV1,
)
from step6_figure8_autotune_v1.v5_filter_shadow import FilterShadowJobQueueV1  # noqa: E402
from step6_figure8_autotune_v1.v5_ros2_observation_mirror import (  # noqa: E402
    CompositeObservationSinkV1,
    ObservationEnvelopeV1,
    ROS2ObservationMirrorV1,
    ROS2StringPublisherSinkV1,
    Rosbag2SQLiteSinkV1,
)
from step6_figure8_autotune_v1.v5_sidecar_bundle import V5SidecarBundleConfigV1  # noqa: E402


DEFAULT_CONFIG = ROOT / "config" / "step6" / "autotuner_v5_sidecars_v1.json"


def _emit(value: Any) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False), flush=True)


def _filter_worker(args: argparse.Namespace) -> int:
    queue = FilterShadowJobQueueV1(args.queue_root)
    if not args.watch:
        _emit(queue.run_one() or {"status": "IDLE", "physical_campaign_dependency": False})
        return 0
    try:
        while True:
            status = queue.run_one()
            if status is not None:
                _emit(status)
            else:
                time.sleep(args.poll_s)
    except KeyboardInterrupt:
        return 0


def _camera_observer(args: argparse.Namespace) -> int:
    if not 1.0 <= args.duration_s <= 3600.0:
        raise ValueError("camera duration must be within [1,3600] seconds")
    observer = ArmCameraObserverV1(args.evidence_root)
    deadline = time.monotonic() + args.duration_s
    captured = 0
    with FFmpegRTSPFrameSourceV1(lock_path=args.lock_path) as source:
        while time.monotonic() < deadline:
            frame = source.read(timeout_s=min(2.0, max(0.0, deadline - time.monotonic())))
            if frame is None:
                continue
            now = time.monotonic_ns()
            receipt = observer.observe(frame, monotonic_ns=now, source_timestamp_ns=time.time_ns())
            captured += int(receipt["captured"])
    recommendation = observer.recommendation(now_monotonic_ns=time.monotonic_ns())
    _emit({
        "schema": "step6.autotune/arm-camera-run-receipt-v1",
        "version": 1,
        "captured": captured,
        "recommendation": recommendation.as_dict(),
        "physical_campaign_dependency": False,
        "control_authority": False,
    })
    return 0


def _ros2_mirror(args: argparse.Namespace) -> int:
    bag = Rosbag2SQLiteSinkV1(args.bag_uri)
    sink = CompositeObservationSinkV1(bag, ROS2StringPublisherSinkV1()) if args.publish else bag
    mirror = ROS2ObservationMirrorV1(sink)
    mirror.start()
    try:
        for number, line in enumerate(sys.stdin, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
                envelope = ObservationEnvelopeV1.from_mapping(value)
            except Exception as exc:  # noqa: BLE001 -- line number is the public boundary
                raise ValueError(f"invalid observation JSONL row {number}") from exc
            _emit(mirror.submit(envelope))
    finally:
        mirror.close(drain=True, timeout_s=5.0)
    _emit(mirror.snapshot())
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("contract", help="print the cold-validated sidecar contract receipt")

    worker = subparsers.add_parser("filter-worker", help="run the isolated artifact-only filter worker")
    worker.add_argument("--queue-root", type=Path, required=True)
    worker.add_argument("--watch", action="store_true")
    worker.add_argument("--poll-s", type=float, default=0.25)

    camera = subparsers.add_parser("camera-observer", help="run the single loopback RTSP observer")
    camera.add_argument("--evidence-root", type=Path, required=True)
    camera.add_argument("--lock-path", type=Path, default=Path("/tmp/autotuner-v5-arm-camera.lock"))
    camera.add_argument("--duration-s", type=float, default=15.0)

    ros2 = subparsers.add_parser("ros2-mirror", help="mirror typed JSONL observations to rosbag2 and optional ROS2 topics")
    ros2.add_argument("--bag-uri", type=Path, required=True)
    ros2.add_argument("--publish", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = V5SidecarBundleConfigV1.from_path(args.config)
    if args.command == "contract":
        _emit(config.receipt())
        return 0
    if args.command == "filter-worker":
        if not 0.01 <= args.poll_s <= 10.0:
            raise ValueError("filter worker poll interval is outside bounds")
        return _filter_worker(args)
    if args.command == "camera-observer":
        return _camera_observer(args)
    if args.command == "ros2-mirror":
        return _ros2_mirror(args)
    raise AssertionError("unknown sidecar command")


if __name__ == "__main__":
    raise SystemExit(main())
