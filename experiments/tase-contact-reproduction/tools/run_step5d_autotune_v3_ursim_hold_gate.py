#!/usr/bin/env python3
"""Run the manual digest-pinned URSim HOLD-only v3 compatibility gate.

The gate attaches only to an already-running URSim container.  It never pulls,
creates, starts, stops, copies into, or executes inside a container.  Dashboard
queries are read-only, the RTDE recipe contains outputs only, and the production
v3 service remains in its explicit offline mode.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import math
import os
import re
import socket
import struct
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v3.launcher import check_effective_config  # noqa: E402
from step5d_autotune_v3.state import CampaignPaths, read_service_state  # noqa: E402
from verify_step5d_autotune_v3_artifacts import verify as verify_artifacts  # noqa: E402


SCHEMA = "step5d.autotune-v3/ursim-hold-gate-v1"
MATRIX_PATH = ROOT / "config" / "step5d_autotune_v3_test_matrix.json"
DASHBOARD_COMMANDS = (
    "PolyscopeVersion",
    "robotmode",
    "programState",
    "running",
    "safetymode",
)
RTDE_OUTPUT_FIELDS = (
    "actual_qd",
    "actual_TCP_speed",
    "runtime_state",
    "robot_mode",
    "safety_mode",
)
_CONTAINER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_DOCKER_READ_ONLY_SHAPES = (
    ("version", "--format"),
    ("inspect", "--type", "container"),
    ("image", "inspect"),
    ("network", "inspect"),
)


class GateBlocked(RuntimeError):
    """A fail-closed precondition or HOLD invariant was not proved."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path = path.absolute()
    if path.exists() or path.is_symlink():
        raise GateBlocked("evidence_path_not_fresh", f"refusing existing evidence path: {path}")
    if any(parent.is_symlink() for parent in (path.parent, *path.parent.parents)):
        raise GateBlocked("evidence_path_uses_symlink", str(path.parent))
    if path.parent.exists():
        if not path.parent.is_dir():
            raise GateBlocked("evidence_parent_not_directory", str(path.parent))
    else:
        path.parent.mkdir(parents=True, exist_ok=False)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, allow_nan=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _expected_image() -> str:
    payload = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    image = payload["lanes"]["large_ursim"]["container_image"]
    if not isinstance(image, str) or re.fullmatch(
        r"[^\s@]+@sha256:[0-9a-f]{64}", image
    ) is None:
        raise GateBlocked("ursim_image_pin_invalid", "test matrix lacks an exact image digest")
    return image


def _docker_text(arguments: Sequence[str]) -> str:
    shape = tuple(arguments[:3])
    allowed = any(shape[: len(prefix)] == prefix for prefix in _DOCKER_READ_ONLY_SHAPES)
    if not allowed:
        raise GateBlocked("unsafe_docker_action_rejected", repr(list(arguments)))
    try:
        completed = subprocess.run(
            ["docker", *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=20.0,
        )
    except FileNotFoundError as exc:
        raise GateBlocked("docker_cli_unavailable", str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise GateBlocked("docker_daemon_unavailable", str(exc)) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        lowered = detail.lower()
        code = (
            "docker_daemon_unavailable"
            if "permission denied" in lowered or "cannot connect" in lowered
            else "docker_read_only_inspection_failed"
        )
        raise GateBlocked(code, detail or f"docker returned {completed.returncode}")
    return completed.stdout


def _docker_json(*arguments: str) -> Any:
    try:
        return json.loads(_docker_text(arguments))
    except json.JSONDecodeError as exc:
        raise GateBlocked("docker_inspection_not_json", str(exc)) from exc


def _one(payload: Any, role: str) -> Mapping[str, Any]:
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise GateBlocked("docker_inspection_shape_invalid", role)
    return payload[0]


def inspect_ursim_container(container: str, expected_image: str) -> dict[str, Any]:
    if _CONTAINER_NAME.fullmatch(container) is None:
        raise GateBlocked("container_name_invalid", container)
    server_version = _docker_json("version", "--format", "{{json .Server.Version}}")
    if not isinstance(server_version, str) or not server_version:
        raise GateBlocked("docker_daemon_unavailable", "Docker server version is absent")
    info = _one(
        _docker_json("inspect", "--type", "container", container), "container"
    )
    image = _one(_docker_json("image", "inspect", expected_image), "image")
    if (info.get("State") or {}).get("Running") is not True:
        raise GateBlocked("ursim_container_not_running", container)
    if (info.get("Config") or {}).get("Image") != expected_image:
        raise GateBlocked("ursim_container_image_reference_drift", container)
    if info.get("Image") != image.get("Id") or expected_image not in image.get("RepoDigests", []):
        raise GateBlocked("ursim_container_image_digest_drift", container)
    host_config = info.get("HostConfig") or {}
    if host_config.get("Privileged") is True:
        raise GateBlocked("ursim_container_privileged", container)
    if any((host_config.get("PortBindings") or {}).values()):
        raise GateBlocked("ursim_host_port_published", container)
    network_settings = info.get("NetworkSettings") or {}
    if any(value for value in (network_settings.get("Ports") or {}).values()):
        raise GateBlocked("ursim_host_port_published", container)
    networks = network_settings.get("Networks") or {}
    if len(networks) != 1:
        raise GateBlocked("ursim_network_scope_ambiguous", repr(sorted(networks)))
    network_name, endpoint = next(iter(networks.items()))
    network = _one(_docker_json("network", "inspect", network_name), "network")
    if network.get("Internal") is not True:
        raise GateBlocked("ursim_network_not_internal", network_name)
    host = str((endpoint or {}).get("IPAddress") or "")
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise GateBlocked("ursim_container_ip_invalid", host) from exc
    if address.version != 4 or not address.is_private or address.is_loopback:
        raise GateBlocked("ursim_container_ip_not_isolated", host)
    return {
        "container": container,
        "container_id": info.get("Id"),
        "container_image_id": info.get("Image"),
        "expected_image": expected_image,
        "docker_server_version": server_version,
        "network": network_name,
        "network_internal": True,
        "host_ports_published": False,
        "container_ip": host,
    }


def _dashboard_snapshot(host: str, timeout_s: float) -> dict[str, str]:
    responses: dict[str, str] = {}
    with socket.create_connection((host, 29999), timeout=timeout_s) as connection:
        connection.settimeout(timeout_s)
        responses["banner"] = connection.recv(1024).decode(errors="replace").strip()
        for command in DASHBOARD_COMMANDS:
            connection.sendall((command + "\n").encode("ascii"))
            responses[command] = connection.recv(1024).decode(errors="replace").strip()
    return responses


def _recv_exact(connection: socket.socket, size: int) -> bytes:
    payload = b""
    while len(payload) < size:
        chunk = connection.recv(size - len(payload))
        if not chunk:
            raise GateBlocked("rtde_socket_closed", "URSim closed the RTDE connection")
        payload += chunk
    return payload


def _rtde_snapshot(host: str, timeout_s: float) -> dict[str, Any]:
    formats = {"DOUBLE": "d", "VECTOR6D": "6d", "UINT32": "I", "INT32": "i"}
    sent: list[str] = []
    with socket.create_connection((host, 30004), timeout=timeout_s) as connection:
        connection.settimeout(timeout_s)

        def send(kind: str, payload: bytes = b"") -> None:
            if kind not in {"V", "O", "S"}:
                raise GateBlocked("rtde_write_packet_rejected", kind)
            sent.append(kind)
            connection.sendall(struct.pack("!HB", len(payload) + 3, ord(kind)) + payload)

        def receive() -> tuple[int, bytes]:
            size, kind = struct.unpack("!HB", _recv_exact(connection, 3))
            return kind, _recv_exact(connection, size - 3) if size > 3 else b""

        send("V", struct.pack("!H", 2))
        kind, payload = receive()
        if kind != ord("V") or payload != b"\x01":
            raise GateBlocked("rtde_protocol_negotiation_failed", repr((kind, payload)))
        recipe = struct.pack("!d", 10.0) + ",".join(RTDE_OUTPUT_FIELDS).encode("ascii")
        send("O", recipe)
        kind, payload = receive()
        if kind != ord("O") or not payload or payload[0] == 0:
            raise GateBlocked("rtde_output_recipe_failed", repr((kind, payload)))
        recipe_id = payload[0]
        types = payload[1:].decode("ascii", errors="strict").split(",")
        if len(types) != len(RTDE_OUTPUT_FIELDS) or any(name not in formats for name in types):
            raise GateBlocked("rtde_output_recipe_types_invalid", repr(types))
        send("S")
        kind, payload = receive()
        if kind != ord("S") or payload != b"\x01":
            raise GateBlocked("rtde_output_stream_start_failed", repr((kind, payload)))
        while True:
            kind, payload = receive()
            if kind == ord("U") and payload and payload[0] == recipe_id:
                break
        cursor = 1
        values: list[Any] = []
        for type_name in types:
            fmt = formats[type_name]
            width = struct.calcsize("!" + fmt)
            unpacked = struct.unpack("!" + fmt, payload[cursor : cursor + width])
            cursor += width
            values.append(unpacked[0] if len(unpacked) == 1 else list(unpacked))
    return {
        "fields": dict(zip(RTDE_OUTPUT_FIELDS, values)),
        "output_recipe_only": True,
        "sent_packet_types": sent,
    }


def _dashboard_value(response: str) -> str:
    return response.rsplit(":", 1)[-1].strip().upper()


def validate_hold_sample(dashboard: Mapping[str, str], rtde: Mapping[str, Any]) -> dict[str, Any]:
    program_state = _dashboard_value(str(dashboard.get("programState", ""))).split()[0]
    running = _dashboard_value(str(dashboard.get("running", "")))
    fields = rtde.get("fields") if isinstance(rtde, Mapping) else None
    if program_state != "STOPPED" or running not in {"FALSE", "0"}:
        raise GateBlocked("ursim_not_stopped_hold", repr((program_state, running)))
    if not isinstance(fields, Mapping):
        raise GateBlocked("rtde_hold_fields_missing", repr(fields))
    maxima: dict[str, float] = {}
    for name in ("actual_qd", "actual_TCP_speed"):
        values = fields.get(name)
        if (
            not isinstance(values, list)
            or len(values) != 6
            or any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values)
            or any(not math.isfinite(float(value)) for value in values)
        ):
            raise GateBlocked("rtde_hold_vector_invalid", name)
        maxima[name] = max(abs(float(value)) for value in values)
    if maxima["actual_qd"] > 1e-6 or maxima["actual_TCP_speed"] > 1e-6:
        raise GateBlocked("ursim_motion_observed", repr(maxima))
    if list(rtde.get("sent_packet_types", [])) != ["V", "O", "S"]:
        raise GateBlocked("rtde_not_output_only", repr(rtde.get("sent_packet_types")))
    return {"program_state": program_state, "running": False, "max_abs": maxima}


def _wait_service_ready(paths: CampaignPaths, process: subprocess.Popen[str], timeout_s: float) -> tuple[dict[str, Any], list[str]]:
    deadline = time.monotonic() + timeout_s
    phases: list[str] = []
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise GateBlocked("v3_service_exited_before_ready", f"rc={process.returncode}")
        if paths.service_state.is_file():
            state = read_service_state(paths)
            phase = str(state.get("phase"))
            if not phases or phases[-1] != phase:
                phases.append(phase)
            if phase == "ready_home":
                return state, phases
        time.sleep(0.01)
    raise GateBlocked("v3_service_ready_timeout", repr(phases))


def _stop_service(process: subprocess.Popen[str], timeout_s: float) -> tuple[int, str, str]:
    if process.poll() is None:
        process.terminate()
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate(timeout=2.0)
        raise GateBlocked("v3_service_cleanup_forced", stderr.strip())
    return int(process.returncode or 0), stdout, stderr


def run_gate(
    *,
    container: str,
    output_root: Path,
    timeout_s: float,
    sample_count: int,
    sample_interval_s: float,
) -> dict[str, Any]:
    observed_at = datetime.now(timezone.utc).isoformat()
    process: subprocess.Popen[str] | None = None
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "ok": False,
        "observed_at": observed_at,
        "claim_boundary": "digest_pinned_ursim_hold_only_not_robot_acceptance",
        "motion_allowed": False,
        "arm_allowed": False,
        "play_allowed": False,
        "controller_write_allowed": False,
        "forbidden_action_count": 0,
    }
    try:
        launch = check_effective_config(
            runtime_root=output_root.absolute() / "contract-runtime"
        )
        fingerprint = str(launch["control_fingerprint"])
        evidence = (
            output_root.absolute()
            / f"{fingerprint[:16]}-{_utc_stamp()}-{os.getpid()}"
            / "ursim_hold_gate.json"
        )
    except Exception as exc:
        evidence = (
            output_root.absolute()
            / f"blocked-{_utc_stamp()}-{os.getpid()}"
            / "ursim_hold_gate.json"
        )
        payload.update(
            blocker="production_launcher_contract_failed",
            error=f"{type(exc).__name__}: {exc}",
            evidence_path=str(evidence),
        )
        _atomic_json(evidence, payload)
        return payload
    payload["evidence_path"] = str(evidence)
    workspace = evidence.parent / "service-work"
    paths = CampaignPaths(workspace / "campaign")
    try:
        artifacts_before = verify_artifacts(ROOT)
        expected_image = _expected_image()
        container_report = inspect_ursim_container(container, expected_image)
        initial_dashboard = _dashboard_snapshot(container_report["container_ip"], timeout_s)
        initial_rtde = _rtde_snapshot(container_report["container_ip"], timeout_s)
        initial_hold = validate_hold_sample(initial_dashboard, initial_rtde)

        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(ROOT / "tools") + (
            f":{environment['PYTHONPATH']}" if environment.get("PYTHONPATH") else ""
        )
        command = [
            sys.executable,
            "-m",
            "step5d_autotune_v3.cli",
            "--experiment-root",
            str(ROOT),
            "--campaign-root",
            str(paths.root),
            "--_service",
        ]
        lifecycle = [{"state": "STOPPED", "source": "fresh_validation_workspace"}]
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        lifecycle.append({"state": "STARTING", "source": "production_service_spawn"})
        service_ready, service_phases = _wait_service_ready(paths, process, timeout_s)
        details = service_ready.get("details") or {}
        if (
            service_ready.get("hardware_enabled") is not False
            or service_ready.get("control_fingerprint") != fingerprint
            or details.get("bridge_started") is not False
            or details.get("controller_touched") is not False
            or details.get("tp_started") is not False
        ):
            raise GateBlocked("v3_service_offline_boundary_drift", repr(service_ready))
        lifecycle.append(
            {
                "state": "READY_HOME",
                "source": "production_service_ready_home_plus_ursim_stopped_hold",
                "tp_ready_home_echo_observed": False,
            }
        )
        samples: list[dict[str, Any]] = []
        for index in range(sample_count):
            if process.poll() is not None:
                raise GateBlocked("v3_service_died_during_hold_watchdog", f"sample={index}")
            dashboard = _dashboard_snapshot(container_report["container_ip"], timeout_s)
            rtde = _rtde_snapshot(container_report["container_ip"], timeout_s)
            samples.append(
                {
                    "index": index,
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "hold": validate_hold_sample(dashboard, rtde),
                    "dashboard": dashboard,
                    "rtde": rtde,
                }
            )
            if index + 1 < sample_count:
                time.sleep(sample_interval_s)

        returncode, stdout, stderr = _stop_service(process, timeout_s)
        process = None
        final_state = read_service_state(paths)
        if returncode != 0 or final_state.get("phase") != "stopped":
            raise GateBlocked(
                "v3_service_rollback_failed",
                repr((returncode, final_state.get("phase"), stderr.strip())),
            )
        lifecycle.append({"state": "STOPPED", "source": "graceful_service_rollback"})
        artifacts_after = verify_artifacts(ROOT)
        if artifacts_before != artifacts_after or artifacts_after.get("current_stage_id") != "step5d_strict_rnn_autotune_v1":
            raise GateBlocked("v1_selector_rollback_drift", repr(artifacts_after))
        payload.update(
            ok=True,
            blocker=None,
            production_launcher={
                "contract_sha256": launch["contract_sha256"],
                "control_fingerprint": fingerprint,
                "effective_field_count": len(launch["effective_config"]),
            },
            container=container_report,
            lifecycle={
                "expected_prefix": ["STOPPED", "STARTING", "READY_HOME"],
                "observed": lifecycle,
                "service_phases_observed": service_phases,
            },
            initial_hold=initial_hold,
            hold_watchdog={
                "ok": True,
                "sample_count": len(samples),
                "samples": samples,
                "dashboard_commands": list(DASHBOARD_COMMANDS),
                "rtde_output_fields": list(RTDE_OUTPUT_FIELDS),
                "rtde_input_recipe_created": False,
            },
            watchdog={
                "immutable_tp_watchdog_artifact_verified": True,
                "read_only_hold_monitor_verified": True,
                "tp_watchdog_runtime_executed": False,
                "reason": "HOLD-only gate forbids TP Play",
            },
            rollback={
                "service_exit_code": returncode,
                "service_final_phase": final_state["phase"],
                "v1_selector_before": artifacts_before["current_stage_id"],
                "v1_selector_after": artifacts_after["current_stage_id"],
                "v3_active_after": artifacts_after["v3_active"],
                "stdout": stdout.strip(),
                "stderr": stderr.strip(),
            },
        )
    except GateBlocked as exc:
        payload.update(blocker=exc.code, error=str(exc))
    except Exception as exc:
        payload.update(blocker="ursim_hold_gate_unexpected_error", error=f"{type(exc).__name__}: {exc}")
    finally:
        if process is not None:
            try:
                _stop_service(process, min(timeout_s, 5.0))
            except Exception as exc:
                payload["cleanup_error"] = f"{type(exc).__name__}: {exc}"
                payload["ok"] = False
                payload["blocker"] = "v3_service_cleanup_failed"
        _atomic_json(evidence, payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--timeout-s", type=float, default=15.0)
    parser.add_argument("--sample-count", type=int, default=3)
    parser.add_argument("--sample-interval-s", type=float, default=0.25)
    args = parser.parse_args(argv)
    if not 1.0 <= args.timeout_s <= 60.0:
        parser.error("--timeout-s must be between 1 and 60 seconds")
    if not 3 <= args.sample_count <= 20:
        parser.error("--sample-count must be between 3 and 20")
    if not 0.0 <= args.sample_interval_s <= 5.0:
        parser.error("--sample-interval-s must be between 0 and 5 seconds")
    report = run_gate(
        container=args.container,
        output_root=args.output_root,
        timeout_s=args.timeout_s,
        sample_count=args.sample_count,
        sample_interval_s=args.sample_interval_s,
    )
    print(
        json.dumps(
            {
                "ok": report.get("ok") is True,
                "blocker": report.get("blocker"),
                "evidence_path": report.get("evidence_path"),
            },
            sort_keys=True,
        )
    )
    return 0 if report.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
