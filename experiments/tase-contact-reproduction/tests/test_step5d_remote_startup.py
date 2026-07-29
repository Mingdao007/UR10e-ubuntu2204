from __future__ import annotations

from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT.parent.parent / "src"))

from step5d_remote_startup import (  # noqa: E402
    DashboardWriteError,
    DashboardWriteOutcome,
    HOME_RTDE_FIELDS,
    RemoteBridgeStarter,
    RemoteCampaignPlayer,
    RemoteDashboardWriter,
    RemoteHomeObserver,
    RemoteR026Loader,
    RemoteScript1Trigger,
    RemoteStartupError,
    RemoteStartupPending,
    RemoteStartupTerminal,
)
from step5d_autotune_v3.remote_play import REMOTE_PLAY_SCHEMA  # noqa: E402
from step5d_no_tube_handoff import load_manifest  # noqa: E402


MANIFEST = ROOT / "config/step5d/no_tube_handoff.json"
SCRIPT1_TARGET = "/programs/andyl/kunwei/step5/step5d_autotune_start_hover_r001.urp"
R026_TARGET = "/programs/andyl/kunwei/step5/step5d_strict_rnn_autotune_v3_r027.urp"


class _Socket:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = iter(chunks)
        self.sent = b""

    def __enter__(self) -> "_Socket":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def settimeout(self, _timeout: float) -> None:
        return None

    def sendall(self, payload: bytes) -> None:
        self.sent += payload

    def recv(self, _size: int) -> bytes:
        try:
            return next(self.chunks)
        except StopIteration:
            return b""


def _dashboard(
    target: str,
    *,
    running: bool = False,
    remote: bool = True,
    safety: str = "NORMAL",
    robot_mode: str = "RUNNING",
) -> dict[str, str]:
    state = "PLAYING" if running else "STOPPED"
    return {
        "is in remote control": "true" if remote else "false",
        "safetymode": f"Safetymode: {safety}",
        "robotmode": f"Robotmode: {robot_mode}",
        "running": f"Program running: {'true' if running else 'false'}",
        "programState": f"{state} {target.rsplit('/', 1)[-1]}",
        "get loaded program": f"Loaded program: {target}",
    }


class _Writer:
    def __init__(self, target: str, *, responses: dict[str, str] | None = None) -> None:
        self.load_target = target
        self.commands: list[str] = []
        self.responses = responses or {}

    def write(self, command: str) -> DashboardWriteOutcome:
        self.commands.append(command)
        return DashboardWriteOutcome(
            command=command,
            response=self.responses.get(command, "Starting program" if command == "play" else "Stopped"),
            command_sent=True,
        )


def test_remote_dashboard_writer_allows_exact_load_and_no_arbitrary_command() -> None:
    fake = _Socket(
        [
            b"Connected\nLoading program: /programs/andyl/kunwei/step5/",
            b"step5d_autotune_start_hover_r001.urp\n",
        ]
    )
    writer = RemoteDashboardWriter(
        "robot",
        load_target=SCRIPT1_TARGET,
        timeout_s=0.2,
        connector=lambda *_args, **_kwargs: fake,
    )
    outcome = writer.write(f"load {SCRIPT1_TARGET}")
    assert outcome.response == f"Loading program: {SCRIPT1_TARGET}"
    assert outcome.command_sent is True
    assert writer.last_command_sent == f"load {SCRIPT1_TARGET}"
    assert fake.sent == f"load {SCRIPT1_TARGET}\n".encode()
    with pytest.raises(RemoteStartupError, match="unsupported"):
        writer.write("power on")
    with pytest.raises(RemoteStartupError, match="unsupported"):
        writer.write(f"load {R026_TARGET}")


def test_script1_refuses_remote_false_before_any_write() -> None:
    manifest = load_manifest(MANIFEST)
    writer = _Writer(SCRIPT1_TARGET)
    adapter = RemoteScript1Trigger(
        robot_host="robot",
        writer=writer,  # type: ignore[arg-type]
        dashboard_observer=lambda *_args, **_kwargs: _dashboard(
            SCRIPT1_TARGET, remote=False
        ),
    )
    with pytest.raises(RemoteStartupError, match="Remote Control"):
        adapter.load(manifest)
    assert writer.commands == []


def test_script1_refuses_non_normal_safety_before_any_write() -> None:
    manifest = load_manifest(MANIFEST)
    writer = _Writer(SCRIPT1_TARGET)
    adapter = RemoteScript1Trigger(
        robot_host="robot",
        writer=writer,  # type: ignore[arg-type]
        dashboard_observer=lambda *_args, **_kwargs: _dashboard(
            SCRIPT1_TARGET, safety="PROTECTIVE_STOP"
        ),
    )
    with pytest.raises(RemoteStartupError, match="safety is not NORMAL"):
        adapter.load(manifest)
    assert writer.commands == []


def test_script1_rejects_wrong_loaded_identity_after_exact_load() -> None:
    manifest = load_manifest(MANIFEST)
    writer = _Writer(SCRIPT1_TARGET)
    adapter = RemoteScript1Trigger(
        robot_host="robot",
        writer=writer,  # type: ignore[arg-type]
        dashboard_observer=lambda *_args, **_kwargs: _dashboard(R026_TARGET),
    )
    with pytest.raises(RemoteStartupError, match="loaded program differs"):
        adapter.load(manifest)
    assert writer.commands == [f"load {SCRIPT1_TARGET}"]


def test_script1_play_compensates_with_bounded_stop() -> None:
    manifest = load_manifest(MANIFEST)
    writer = _Writer(SCRIPT1_TARGET)
    observations = iter(
        [
            _dashboard(SCRIPT1_TARGET),
            _dashboard(SCRIPT1_TARGET),
            _dashboard(SCRIPT1_TARGET),
        ]
    )
    ticks = iter([0.0, 0.0, 0.1, 0.2, 10.0])
    adapter = RemoteScript1Trigger(
        robot_host="robot",
        writer=writer,  # type: ignore[arg-type]
        dashboard_observer=lambda *_args, **_kwargs: next(observations),
        observe_timeout_s=0.2,
        monotonic=ticks.__next__,
        sleeper=lambda _seconds: None,
    )
    with pytest.raises(RemoteStartupError, match="compensation=Stopped"):
        adapter.play(manifest)
    assert writer.commands == ["play", "stop"]
    assert adapter.play_issued is True


def test_script1_connect_before_send_failure_does_not_compensate() -> None:
    manifest = load_manifest(MANIFEST)
    writer = RemoteDashboardWriter(
        "robot",
        load_target=SCRIPT1_TARGET,
        connector=lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectionError("offline")),
    )
    adapter = RemoteScript1Trigger(
        robot_host="robot",
        writer=writer,
        dashboard_observer=lambda *_args, **_kwargs: _dashboard(SCRIPT1_TARGET),
    )
    with pytest.raises(RemoteStartupError, match="compensation=not_attempted"):
        adapter.play(manifest)
    assert adapter.play_issued is False
    assert writer.last_command_sent is None


def test_script1_send_success_response_failure_compensates_once() -> None:
    manifest = load_manifest(MANIFEST)

    class _NoResponseSocket(_Socket):
        def recv(self, _size: int) -> bytes:
            raise socket.timeout()

    import socket

    fake = _NoResponseSocket([])
    writer = RemoteDashboardWriter(
        "robot",
        load_target=SCRIPT1_TARGET,
        timeout_s=0.01,
        connector=lambda *_args, **_kwargs: fake,
    )
    adapter = RemoteScript1Trigger(
        robot_host="robot",
        writer=writer,
        dashboard_observer=lambda *_args, **_kwargs: _dashboard(SCRIPT1_TARGET),
    )
    with pytest.raises(RemoteStartupError, match="compensation=stop_failed"):
        adapter.play(manifest)
    assert adapter.play_issued is True
    assert fake.sent == b"play\nstop\n"
    # The failed Play caused one and only one bounded stop attempt.  The same
    # socket is deliberately not reused: its send payload proves the writer
    # did not silently issue a second Play or an unbounded retry.
    assert writer.last_command_sent == "stop"


class _RTDE:
    def __init__(self, rows: list[list[object]]) -> None:
        self.rows = iter(rows)
        self.calls: list[object] = []

    def __enter__(self) -> "_RTDE":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def negotiate(self, version: int) -> None:
        self.calls.append(("negotiate", version))

    def setup_outputs(self, frequency_hz: float, fields: tuple[str, ...]) -> tuple[int, list[str]]:
        self.calls.append(("setup_outputs", frequency_hz, fields))
        assert fields == HOME_RTDE_FIELDS
        return 7, ["DOUBLE", "VECTOR6D", "VECTOR6D", "VECTOR6D", "VECTOR6D"]

    def start(self) -> None:
        self.calls.append("start")

    def recv_recipe_sample(self, recipe_id: int, type_names: list[str]) -> list[object]:
        self.calls.append(("recv", recipe_id, type_names))
        return next(self.rows)


def test_home_observer_is_read_only_and_emits_fresh_stationary_samples() -> None:
    manifest = load_manifest(MANIFEST)
    home_rows = [
        [
            1.0 + index * 0.1,
            [0.487834547, 0.129337053, 0.033, 3.120752062, 0.0, 0.068626833],
            [0.0] * 6,
            [0.1] * 6,
            [0.0] * 6,
        ]
        for index in range(6)
    ]
    rtde = _RTDE(
        home_rows
    )
    observed_ns = iter(
        [1_000_000_000 + index * 100_000_000 for index in range(6)]
    ).__next__
    observer = RemoteHomeObserver(
        robot_host="robot",
        rtde_factory=lambda *_args, **_kwargs: rtde,
        dashboard_observer=lambda *_args, **_kwargs: _dashboard(SCRIPT1_TARGET),
        monotonic=lambda: 1.0,
        monotonic_ns=observed_ns,
        sleeper=lambda _seconds: None,
    )
    samples = observer.observe(manifest)
    assert len(samples) == 6
    assert all(sample["remote_control"] is True for sample in samples)
    assert all(sample["fresh"] is True for sample in samples)
    assert [call[0] for call in rtde.calls if isinstance(call, tuple)] == [
        "negotiate",
        "setup_outputs",
        "recv",
        "recv",
        "recv",
        "recv",
        "recv",
        "recv",
    ]
    assert all(call != "input_write" for call in rtde.calls)


def test_r026_loader_rejects_wrong_exact_binding_before_write() -> None:
    manifest = load_manifest(MANIFEST)
    writer = _Writer(SCRIPT1_TARGET)
    loader = RemoteR026Loader(
        robot_host="robot",
        writer=writer,  # type: ignore[arg-type]
        dashboard_observer=lambda *_args, **_kwargs: _dashboard(R026_TARGET),
    )
    with pytest.raises(RemoteStartupError, match="binding differs"):
        loader.load(manifest)
    assert writer.commands == []


class _Process:
    returncode = None
    pid = 123

    def poll(self) -> None:
        return None

    def send_signal(self, _signal: int) -> None:
        self.returncode = 130

    def wait(self, timeout: float) -> int:
        return 0

    def terminate(self) -> None:
        self.returncode = 143

    def kill(self) -> None:
        self.returncode = 137


def _bridge_status(
    manifest,
    output_root: Path,
    campaign_root: Path,
    *,
    attempt_id: str,
    target: str = R026_TARGET,
) -> dict:
    return {
        "schema": "step5d.bridge/governed-status-v3",
        "state": "BENCH_READY",
        "compatibility_phase": "WAITING_FOR_PLAY",
        "controller": {
            "loaded": {
                "verified": True,
                "expected": target,
                "observed": target,
            }
        },
        "release": {"sha256": manifest.payload["script2"]["release_manifest_sha256"]},
        "predicates": {
            "bridge_heartbeat_fresh": True,
            "single_writer": True,
            "lease_valid": True,
            "loaded_program_verified": True,
            "safety_normal": True,
        },
        "bridge": {"alive": True, "heartbeat_fresh": True},
        "campaign_lease": {"valid": True},
        "launch_attempt": {
            "present": True,
            "state": "STARTED",
            "attempt_id": attempt_id,
            "bindings": {
                "resource_owner": {"pid": 123, "starttime_ticks": 456},
                "output_root": str(output_root.absolute()),
                "campaign_root": str(campaign_root.absolute()),
            },
        },
    }


def test_bridge_starter_uses_canonical_launcher_and_readiness_claim(tmp_path: Path) -> None:
    manifest = load_manifest(MANIFEST)
    pre_status = {
        "launch_attempt": {"present": True, "attempt_id": "prior-attempt"},
    }
    status = {
        "schema": "step5d.bridge/governed-status-v3",
        "state": "BENCH_READY",
        "compatibility_phase": "WAITING_FOR_PLAY",
        "controller": {"loaded": {"verified": True, "expected": R026_TARGET, "observed": R026_TARGET}},
        "release": {"sha256": manifest.payload["script2"]["release_manifest_sha256"]},
        "predicates": {
            "bridge_heartbeat_fresh": True,
            "single_writer": True,
            "lease_valid": True,
            "loaded_program_verified": True,
            "safety_normal": True,
        },
        "bridge": {"alive": True, "heartbeat_fresh": True},
        "campaign_lease": {"valid": True},
        "launch_attempt": {
            "present": True,
            "state": "STARTED",
            "attempt_id": "attempt-remote-1",
            "bindings": {
                "resource_owner": {"pid": 123, "starttime_ticks": 456},
                "output_root": str((tmp_path / "output" / "bridge-live").absolute()),
                "campaign_root": str((tmp_path / "campaign").absolute()),
            },
        },
    }
    process = _Process()
    commands: list[list[str]] = []
    resolver_calls = 0

    def status_resolver(_root: Path):
        nonlocal resolver_calls
        resolver_calls += 1
        return pre_status if resolver_calls == 1 else status

    clock = [0.0]

    def monotonic() -> float:
        value = clock[0]
        clock[0] += 0.1
        return value

    starter = RemoteBridgeStarter(
        experiment_root=manifest.experiment_root,
        robot_host="robot",
        output_root=tmp_path / "output",
        campaign_root=tmp_path / "campaign",
        shell_path=manifest.experiment_root / "scripts/step5d-autotune-v3.sh",
        status_resolver=status_resolver,
        readiness_claim_builder=lambda _status, state: {
            "state": state,
            "attempt_id": "attempt-remote-1",
        },
        readiness_claim_verifier=lambda _status, _claim: {},
        dashboard_observer=lambda *_args, **_kwargs: _dashboard(R026_TARGET),
        popen_factory=lambda command, **_kwargs: commands.append(command) or process,
        owner_starttime_reader=lambda _pid: 456,
        monotonic=monotonic,
        sleeper=lambda _seconds: None,
    )
    receipt = starter.start(manifest)
    assert receipt["bridge_id"] == "canonical:attempt-remote-1"
    assert commands[0][0:2] == [str(manifest.experiment_root / "scripts/step5d-autotune-v3.sh"), "bridge-live"]
    assert receipt["safety_normal"] is True
    starter.abort()


def test_bridge_starter_does_not_accept_stale_prior_ready_status(tmp_path: Path) -> None:
    manifest = load_manifest(MANIFEST)
    process = _Process()
    old = _bridge_status(
        manifest,
        tmp_path / "output" / "bridge-live",
        tmp_path / "campaign",
        attempt_id="prior-attempt",
    )
    calls = 0

    def status_resolver(_root: Path):
        nonlocal calls
        calls += 1
        return old

    clock = iter([0.0, 0.1, 100.0]).__next__
    starter = RemoteBridgeStarter(
        experiment_root=manifest.experiment_root,
        robot_host="robot",
        output_root=tmp_path / "output",
        campaign_root=tmp_path / "campaign",
        shell_path=manifest.experiment_root / "scripts/step5d-autotune-v3.sh",
        status_resolver=status_resolver,
        readiness_claim_builder=lambda status, state: {
            "state": state,
            "attempt_id": status["launch_attempt"]["attempt_id"],
        },
        readiness_claim_verifier=lambda _status, _claim: {},
        dashboard_observer=lambda *_args, **_kwargs: _dashboard(R026_TARGET),
        popen_factory=lambda _command, **_kwargs: process,
        owner_starttime_reader=lambda _pid: 456,
        monotonic=clock,
        sleeper=lambda _seconds: None,
    )

    with pytest.raises(RemoteStartupPending, match="timed out"):
        starter.start(manifest)
    assert calls == 2
    assert process.returncode == 130
    assert starter.bridge_id is None


def test_bridge_startup_waits_through_pending_status_until_fresh_ready(
    tmp_path: Path,
) -> None:
    manifest = load_manifest(MANIFEST)
    process = _Process()
    fresh = _bridge_status(
        manifest,
        tmp_path / "output" / "bridge-live",
        tmp_path / "campaign",
        attempt_id="fresh-attempt",
    )
    statuses = iter(
        [
            {"launch_attempt": {"present": False}},
            {"launch_attempt": {"present": True, "attempt_id": "fresh-attempt"}},
            fresh,
        ]
    )
    starter = RemoteBridgeStarter(
        experiment_root=manifest.experiment_root,
        robot_host="robot",
        output_root=tmp_path / "output",
        campaign_root=tmp_path / "campaign",
        shell_path=manifest.experiment_root / "scripts/step5d-autotune-v3.sh",
        status_resolver=lambda _root: next(statuses),
        readiness_claim_builder=lambda status, state: {
            "state": state,
            "attempt_id": status.get("launch_attempt", {}).get("attempt_id"),
        },
        readiness_claim_verifier=lambda _status, _claim: {},
        dashboard_observer=lambda *_args, **_kwargs: _dashboard(R026_TARGET),
        popen_factory=lambda _command, **_kwargs: process,
        owner_starttime_reader=lambda _pid: 456,
        monotonic=iter([0.0, 0.1, 0.2]).__next__,
        sleeper=lambda _seconds: None,
    )

    receipt = starter.start(manifest)
    assert receipt["bridge_id"] == "canonical:fresh-attempt"
    starter.abort()


def test_bridge_fresh_identity_violation_is_terminal_not_pending(tmp_path: Path) -> None:
    manifest = load_manifest(MANIFEST)
    process = _Process()
    wrong = _bridge_status(
        manifest,
        tmp_path / "output" / "bridge-live",
        tmp_path / "campaign",
        attempt_id="fresh-attempt",
        target="/programs/wrong.urp",
    )
    statuses = iter([{"launch_attempt": {"present": False}}, wrong])
    starter = RemoteBridgeStarter(
        experiment_root=manifest.experiment_root,
        robot_host="robot",
        output_root=tmp_path / "output",
        campaign_root=tmp_path / "campaign",
        shell_path=manifest.experiment_root / "scripts/step5d-autotune-v3.sh",
        status_resolver=lambda _root: next(statuses),
        readiness_claim_builder=lambda status, state: {
            "state": state,
            "attempt_id": status["launch_attempt"]["attempt_id"],
        },
        readiness_claim_verifier=lambda _status, _claim: {},
        dashboard_observer=lambda *_args, **_kwargs: _dashboard(R026_TARGET),
        popen_factory=lambda _command, **_kwargs: process,
        owner_starttime_reader=lambda _pid: 456,
        monotonic=iter([0.0, 0.1]).__next__,
        sleeper=lambda _seconds: None,
    )

    with pytest.raises(RemoteStartupTerminal, match="identity differs"):
        starter.start(manifest)
    assert process.returncode == 130


class _ReadyBridge:
    def __init__(self, bridge_id: str) -> None:
        self.bridge_id = bridge_id


def _play_receipt(manifest, *, status: str = "play_observed") -> dict:
    return {
        "schema": REMOTE_PLAY_SCHEMA,
        "status": status,
        "attempt_id": "fresh-attempt",
        "release_manifest_sha256": manifest.payload["script2"]["release_manifest_sha256"],
        "expected_program": R026_TARGET,
        "governed_status_sha256": "a" * 64,
        "observed_at_unix_ns": 100,
        "play_observed_at_unix_ns": 200,
        "dashboard_before": _dashboard(R026_TARGET),
        "dashboard_write_response": "Starting program\n",
        "dashboard_after": _dashboard(R026_TARGET, running=True),
    }


def test_campaign_player_rejects_malformed_or_failed_governed_receipts(
    tmp_path: Path,
) -> None:
    manifest = load_manifest(MANIFEST)
    for raw_receipt in ({}, _play_receipt(manifest, status="play_failed")):
        player = RemoteCampaignPlayer(
            experiment_root=manifest.experiment_root,
            robot_host="robot",
            output_root=tmp_path,
            bridge=_ReadyBridge("canonical:fresh-attempt"),  # type: ignore[arg-type]
            play_callable=lambda *_args, raw_receipt=raw_receipt, **_kwargs: raw_receipt,
        )
        with pytest.raises(RemoteStartupTerminal):
            player.play(manifest)


def test_campaign_player_binds_upper_receipt_to_real_governed_receipt(
    tmp_path: Path,
) -> None:
    manifest = load_manifest(MANIFEST)
    raw_receipt = _play_receipt(manifest)
    player = RemoteCampaignPlayer(
        experiment_root=manifest.experiment_root,
        robot_host="robot",
        output_root=tmp_path,
        bridge=_ReadyBridge("canonical:fresh-attempt"),  # type: ignore[arg-type]
        play_callable=lambda *_args, **_kwargs: raw_receipt,
    )

    result = player.play(manifest)
    assert result["accepted"] is True
    assert result["program_running"] is True
    assert result["safety_mode"] == "NORMAL"
    assert result["remote_play_receipt"] == raw_receipt
