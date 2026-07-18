from __future__ import annotations

import copy
import json
import struct
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import run_step5d_autotune_v3_ursim_hold_gate as gate  # noqa: E402


EXPECTED_IMAGE = (
    "universalrobots/ursim_e-series:5.25.2@sha256:"
    "a4c4365207d54d1a1a4ead87526ff3781e2e98ae703c72f362060a46688fa7a4"
)
EXPECTED_REPO_DIGEST = (
    "universalrobots/ursim_e-series@sha256:"
    "a4c4365207d54d1a1a4ead87526ff3781e2e98ae703c72f362060a46688fa7a4"
)


def _docker_fixtures() -> dict[str, object]:
    image_id = "sha256:" + "a" * 64
    container = {
        "Id": "b" * 64,
        "Image": image_id,
        "Config": {"Image": EXPECTED_IMAGE},
        "State": {"Running": True},
        "HostConfig": {
            "Privileged": False,
            "PortBindings": {},
            "NetworkMode": "step5d-v3-internal",
        },
        "NetworkSettings": {
            "Ports": {"29999/tcp": None, "30004/tcp": None},
            "Networks": {
                "step5d-v3-internal": {"IPAddress": "172.29.0.2"}
            },
        },
    }
    return {
        "version": "27.5.1",
        "container": [container],
        "image": [{"Id": image_id, "RepoDigests": [EXPECTED_REPO_DIGEST]}],
        "network": [{"Name": "step5d-v3-internal", "Internal": True}],
    }


def _install_docker_fixture(
    monkeypatch: pytest.MonkeyPatch, fixture: dict[str, object]
) -> None:
    def read(*arguments: str) -> object:
        if arguments[0] == "version":
            return fixture["version"]
        if arguments[:3] == ("inspect", "--type", "container"):
            return fixture["container"]
        if arguments[:2] == ("image", "inspect"):
            return fixture["image"]
        if arguments[:2] == ("network", "inspect"):
            return fixture["network"]
        raise AssertionError(f"unexpected Docker action: {arguments}")

    monkeypatch.setattr(gate, "_docker_json", read)


def test_matrix_binds_manual_internal_network_hold_lane() -> None:
    matrix = json.loads(gate.MATRIX_PATH.read_text(encoding="utf-8"))
    lane = matrix["lanes"]["large_ursim"]
    evidence = json.loads(
        (ROOT / matrix["evidence_manifest"]).read_text(encoding="utf-8")
    )["lanes"]["large_ursim"]
    assert gate._expected_image() == EXPECTED_IMAGE
    assert lane["network_allowed"] is True
    assert lane["network_scope"] == "single_prestarted_container_on_docker_internal_network"
    assert lane["external_network_allowed"] is False
    assert lane["robot_network_allowed"] is False
    assert lane["container_lifecycle_mutation_allowed"] is False
    assert lane["dashboard_commands_allowed"] == list(gate.DASHBOARD_COMMANDS)
    assert lane["rtde_output_recipe_only"] is True
    assert lane["rtde_input_recipe_allowed"] is False
    assert lane["simulator_polyscope_version"] == "5.25.2"
    assert lane["target_polyscope_version_equivalence_claimed"] is False
    assert evidence["status"] == "pass"
    assert evidence["result"].endswith(
        "step5d_autotune_v3_ursim_hold_result.json"
    )
    assert evidence["raw_evidence_sha256"] == (
        "b9e7c21709b6b85b8eef6312f95ffd094ce42d20370e8df7d00c3fae59cca41c"
    )
    assert evidence["cleanup_completed"] is True
    assert lane["commands"][0][1].endswith("run_step5d_autotune_v3_ursim_hold_gate.py")


def test_container_binding_accepts_only_exact_digest_and_internal_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_docker_fixture(monkeypatch, _docker_fixtures())
    report = gate.inspect_ursim_container("step5d-autotune-v3-ursim-hold", EXPECTED_IMAGE)
    assert report["expected_image"] == EXPECTED_IMAGE
    assert report["network_internal"] is True
    assert report["host_ports_published"] is False
    assert report["container_ip"] == "172.29.0.2"


@pytest.mark.parametrize(
    ("mutate", "blocker"),
    [
        (
            lambda payload: payload["container"][0]["Config"].update(Image="tag-only"),
            "ursim_container_image_reference_drift",
        ),
        (
            lambda payload: payload["container"][0]["State"].update(Running=False),
            "ursim_container_not_running",
        ),
        (
            lambda payload: payload["container"][0]["HostConfig"].update(Privileged=True),
            "ursim_container_privileged",
        ),
        (
            lambda payload: payload["container"][0]["HostConfig"].update(
                PortBindings={"29999/tcp": [{"HostPort": "29999"}]}
            ),
            "ursim_host_port_published",
        ),
        (
            lambda payload: payload["network"][0].update(Internal=False),
            "ursim_network_not_internal",
        ),
        (
            lambda payload: payload["image"][0].update(RepoDigests=[]),
            "ursim_container_image_digest_drift",
        ),
    ],
)
def test_container_binding_mutations_fail_closed(
    monkeypatch: pytest.MonkeyPatch, mutate, blocker: str
) -> None:
    fixture = copy.deepcopy(_docker_fixtures())
    mutate(fixture)
    _install_docker_fixture(monkeypatch, fixture)
    with pytest.raises(gate.GateBlocked) as caught:
        gate.inspect_ursim_container("step5d-autotune-v3-ursim-hold", EXPECTED_IMAGE)
    assert caught.value.code == blocker


def test_docker_mutating_action_is_rejected_before_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not execute")),
    )
    with pytest.raises(gate.GateBlocked) as caught:
        gate._docker_text(("run", "--rm", EXPECTED_IMAGE))
    assert caught.value.code == "unsafe_docker_action_rejected"


def test_evidence_writer_allows_owned_service_workspace_beside_result(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "run" / "ursim_hold_gate.json"
    (evidence.parent / "service-work").mkdir(parents=True)
    gate._atomic_json(evidence, {"ok": False, "blocker": "fixture"})
    assert json.loads(evidence.read_text(encoding="utf-8"))["blocker"] == "fixture"


def _hold_sample(*, qd: float = 0.0, speed: float = 0.0) -> tuple[dict, dict]:
    dashboard = {"programState": "STOPPED", "running": "Program running: false"}
    rtde = {
        "fields": {
            "actual_qd": [qd] * 6,
            "actual_TCP_speed": [speed] * 6,
            "runtime_state": 1,
            "robot_mode": 7,
            "safety_mode": 1,
        },
        "sent_packet_types": ["V", "O", "S"],
    }
    return dashboard, rtde


def test_hold_sample_requires_stopped_output_only_and_zero_velocity() -> None:
    dashboard, rtde = _hold_sample()
    assert gate.validate_hold_sample(dashboard, rtde)["max_abs"]["actual_qd"] == 0.0

    dashboard["programState"] = "PLAYING"
    with pytest.raises(gate.GateBlocked, match="PLAYING"):
        gate.validate_hold_sample(dashboard, rtde)

    dashboard, rtde = _hold_sample(qd=2e-6)
    with pytest.raises(gate.GateBlocked) as caught:
        gate.validate_hold_sample(dashboard, rtde)
    assert caught.value.code == "ursim_motion_observed"

    dashboard, rtde = _hold_sample()
    rtde["sent_packet_types"].append("I")
    with pytest.raises(gate.GateBlocked) as caught:
        gate.validate_hold_sample(dashboard, rtde)
    assert caught.value.code == "rtde_not_output_only"


class _FakeRtdeSocket:
    def __init__(self, *, pre_version_packets: tuple[str, ...] = ("M",)) -> None:
        self.pending = bytearray()
        self.sent_types: list[str] = []
        self.pre_version_packets = pre_version_packets

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def settimeout(self, _timeout: float) -> None:
        return None

    def sendall(self, frame: bytes) -> None:
        _size, kind = struct.unpack("!HB", frame[:3])
        token = chr(kind)
        self.sent_types.append(token)
        if token == "V":
            for pre_version_kind in self.pre_version_packets:
                if pre_version_kind == "M":
                    notice = b"\x02SafetySetup has not been confirmed yet"
                    self.pending.extend(
                        struct.pack("!HB", 3 + len(notice), ord("M")) + notice
                    )
                else:
                    self.pending.extend(struct.pack("!HB", 3, ord(pre_version_kind)))
            payload = b"\x01"
        elif token == "O":
            payload = b"\x01VECTOR6D,VECTOR6D,UINT32,INT32,INT32"
        elif token == "S":
            payload = b"\x01"
        else:
            raise AssertionError(f"unexpected packet type {token}")
        self.pending.extend(struct.pack("!HB", 3 + len(payload), kind) + payload)
        if token == "S":
            sample = b"\x01" + struct.pack("!12dIii", *([0.0] * 12), 1, 7, 1)
            self.pending.extend(struct.pack("!HB", 3 + len(sample), ord("U")) + sample)

    def recv(self, size: int) -> bytes:
        result = bytes(self.pending[:size])
        del self.pending[:size]
        return result


def test_rtde_client_emits_only_version_output_recipe_and_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeRtdeSocket()
    monkeypatch.setattr(gate.socket, "create_connection", lambda *_args, **_kwargs: fake)
    result = gate._rtde_snapshot("172.29.0.2", 1.0)
    assert fake.sent_types == ["V", "O", "S"]
    assert result["output_recipe_only"] is True
    assert result["fields"]["actual_qd"] == [0.0] * 6
    assert result["received_text_messages"] == [
        {
            "protocol_version": 1,
            "message_type": 2,
            "message": "SafetySetup has not been confirmed yet",
            "payload_hex": "02536166657479536574757020686173206e6f74206265656e20636f6e6669726d656420796574",
        }
    ]


def test_rtde_client_rejects_unexpected_packet_before_version_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeRtdeSocket(pre_version_packets=("P",))
    monkeypatch.setattr(gate.socket, "create_connection", lambda *_args, **_kwargs: fake)
    with pytest.raises(gate.GateBlocked) as caught:
        gate._rtde_snapshot("172.29.0.2", 1.0)
    assert caught.value.code == "rtde_unexpected_packet"


def test_rtde_client_bounds_interleaved_text_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeRtdeSocket(pre_version_packets=("M",) * 9)
    monkeypatch.setattr(gate.socket, "create_connection", lambda *_args, **_kwargs: fake)
    with pytest.raises(gate.GateBlocked) as caught:
        gate._rtde_snapshot("172.29.0.2", 1.0)
    assert caught.value.code == "rtde_text_message_limit_exceeded"


def test_docker_unavailable_writes_blocker_and_never_opens_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        gate,
        "check_effective_config",
        lambda **_kwargs: {
            "control_fingerprint": "a" * 64,
            "contract_sha256": "b" * 64,
            "effective_config": {"profile": "frozen-v1"},
        },
    )
    monkeypatch.setattr(
        gate,
        "selector_snapshot",
        lambda _root: {"current_stage_id": "step5d_strict_rnn_autotune_v1"},
    )
    monkeypatch.setattr(gate, "_expected_image", lambda: EXPECTED_IMAGE)
    monkeypatch.setattr(
        gate,
        "inspect_ursim_container",
        lambda *_args: (_ for _ in ()).throw(
            gate.GateBlocked("docker_daemon_unavailable", "permission denied")
        ),
    )
    monkeypatch.setattr(
        gate,
        "_dashboard_snapshot",
        lambda *_args: (_ for _ in ()).throw(AssertionError("network must not open")),
    )

    report = gate.run_gate(
        container="step5d-autotune-v3-ursim-hold",
        output_root=tmp_path / "evidence",
        timeout_s=1.0,
        sample_count=3,
        sample_interval_s=0.0,
    )
    evidence = Path(report["evidence_path"])
    assert report["ok"] is False
    assert report["blocker"] == "docker_daemon_unavailable"
    assert json.loads(evidence.read_text(encoding="utf-8"))["blocker"] == report["blocker"]


def test_mocked_full_gate_records_lifecycle_watchdog_and_rollback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    launch = {
        "control_fingerprint": "c" * 64,
        "contract_sha256": "d" * 64,
        "effective_config": {"profile": "frozen-v1"},
    }
    artifacts = {
        "current_stage_id": "step5d_strict_rnn_autotune_v1",
        "v3_active": False,
    }
    dashboard, rtde = _hold_sample()
    container = {"container_ip": "172.29.0.2", "expected_image": EXPECTED_IMAGE}

    class FakeProcess:
        returncode = None

        @staticmethod
        def poll():
            return None

    monkeypatch.setattr(gate, "check_effective_config", lambda **_kwargs: launch)
    monkeypatch.setattr(gate, "selector_snapshot", lambda _root: dict(artifacts))
    monkeypatch.setattr(gate, "_expected_image", lambda: EXPECTED_IMAGE)
    monkeypatch.setattr(gate, "inspect_ursim_container", lambda *_args: dict(container))
    monkeypatch.setattr(gate, "_dashboard_snapshot", lambda *_args: dict(dashboard))
    monkeypatch.setattr(gate, "_rtde_snapshot", lambda *_args: copy.deepcopy(rtde))
    monkeypatch.setattr(gate.subprocess, "Popen", lambda *_args, **_kwargs: FakeProcess())
    monkeypatch.setattr(
        gate,
        "_wait_service_ready",
        lambda *_args: (
            {
                "phase": "ready_home",
                "hardware_enabled": False,
                "control_fingerprint": "c" * 64,
                "details": {
                    "bridge_started": False,
                    "controller_touched": False,
                    "tp_started": False,
                },
            },
            ["starting", "ready_home"],
        ),
    )
    monkeypatch.setattr(gate, "_stop_service", lambda *_args: (0, "", ""))
    monkeypatch.setattr(gate, "read_service_state", lambda _paths: {"phase": "stopped"})

    report = gate.run_gate(
        container="step5d-autotune-v3-ursim-hold",
        output_root=tmp_path / "evidence",
        timeout_s=1.0,
        sample_count=3,
        sample_interval_s=0.0,
    )
    assert report["ok"] is True
    assert [row["state"] for row in report["lifecycle"]["observed"]] == [
        "STOPPED",
        "STARTING",
        "READY_HOME",
        "STOPPED",
    ]
    assert report["watchdog"]["tp_watchdog_runtime_executed"] is False
    assert report["rollback"]["v1_selector_after"] == "step5d_strict_rnn_autotune_v1"
    assert Path(report["evidence_path"]).is_file()
