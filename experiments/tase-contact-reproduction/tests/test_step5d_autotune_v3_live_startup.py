from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import inspect
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest
import subprocess


ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(ROOT / "tools"))

if (
    os.environ.get("STEP5D_V3_HERMETIC_PARSER_CI") == "1"
    and "kunwei_rtde_bridge" not in sys.modules
):
    from step5d_v3_parser_ci_stubs import install as install_parser_ci_stubs

    install_parser_ci_stubs()

import run_step5d_autotune_v3_live as live  # noqa: E402
import preflight_step5d_autotune_v3 as preflight  # noqa: E402
import run_step5d_autotune_v3_coordinator as coordinator  # noqa: E402
from ur10e_parallel import (  # noqa: E402
    ResourceProfile,
    writer_lease,
    writer_lease_owner,
)


def test_post_play_steady_state_uses_runtime_and_process_outcome_paths() -> None:
    source = inspect.getsource(live._run_live_session)
    post_play = source[source.index("V3_CAMPAIGN_RUNNING_ONE_PLAY_CONTINUOUS") :]
    outcome = post_play.index("if runner.poll() is not None and runner.returncode == 0:")

    assert "ARM_GATE_REFRESH_INTERVAL_S" not in source
    assert "_refresh_arm_gate(" not in post_play
    assert "ARM gate observation failed" not in post_play
    assert "RECOVERING" not in post_play
    assert "durable_queue_empty_waiting" in post_play[outcome:]
    assert "return None" in post_play[outcome:]
    assert '"campaign_complete"' in post_play[outcome:]


def test_live_owner_never_constructs_an_optimizer_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pointer = {
        "profiles": {
            "control": {"python_executable": "/runtime/control/python"},
        }
    }

    def receive(_args: Any, observed: Mapping[str, Any]) -> dict[str, Any]:
        assert observed is pointer
        return {"optimizer_required": False}

    monkeypatch.setattr(live, "_run_live", receive)

    assert live.run(
        SimpleNamespace(_runtime_pointer=pointer, single_session=True)
    ) == {
        "optimizer_required": False
    }
    source = inspect.getsource(live.run)
    assert "optimizer" not in source
    assert "gpu" not in source


def _row(*, runtime_state: int, state: int = 10) -> dict[str, str]:
    row = {
        "ur_output_int_register_26": str(state),
        "ur_runtime_state": str(runtime_state),
        "ur_safety_mode": "1",
    }
    for index in (24, 25, 27, 28, 29, 30):
        row[f"ur_output_int_register_{index}"] = "0"
    return row


def _arm_gate_fixture(tmp_path: Path) -> tuple[Any, dict[str, Any], dict[str, str]]:
    lease = live.CampaignLease.issue(
        lease_id="1" * 32,
        launch_id="2" * 32,
        manifest_sha256="a" * 64,
        release_stage_id="step5d_strict_rnn_autotune_v3",
        program_id="fake_r010",
        protocol_id="rolling-v1",
        campaign_id="campaign-a",
        campaign_epoch=7,
        campaign_fingerprint="b" * 64,
        safety_envelope_sha256="c" * 64,
    )
    contract = {
        "manifest_sha256": lease.manifest_sha256,
        "safety_envelope_sha256": lease.safety_envelope_sha256,
        "expected_loaded_program": "/programs/fake_r010.urp",
        "tp_runtime_identity": {
            "registers": {
                "protocol_version": 35,
                "digest_hi": 36,
                "digest_lo": 37,
            },
            "protocol_version": 1,
            "digest_hi": 1234,
            "digest_lo": 5678,
        },
    }
    dashboard = {
        "get loaded program": "Loaded program: /programs/fake_r010.urp",
        "programState": "PLAYING fake_r010.urp",
        "safetymode": "Safetymode: NORMAL",
    }
    return lease, contract, dashboard


def _arm_binding(*, digest: str, trial_id: int, command_seq: int) -> dict[str, Any]:
    return {
        "mailbox_sha256": digest,
        "campaign_epoch": 7,
        "trial_id": trial_id,
        "command": 1,
        "candidate_token": 100 + trial_id,
        "execution_profile_id": 633,
        "command_seq": command_seq,
        "logical_batch_sequence": 1,
        "trial_uid": f"trial-{trial_id}",
    }


def _delivery_observation(observed_at_unix_ns: int) -> dict[str, Any]:
    return {
        "schema": "step5d.autotune-v3/delivery-observation-v1",
        "transaction_id": "f" * 32,
        "fresh_controller_checked_at": datetime.fromtimestamp(
            observed_at_unix_ns / 1_000_000_000,
            tz=timezone.utc,
        ).isoformat(),
    }


def test_postplay_runtime_requires_playing_normal() -> None:
    playing = _row(runtime_state=2)

    assert live._runtime_playing_normal(playing) is True


def test_arm_gate_refresh_binds_post_request_dashboard_and_rtde_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease, contract, dashboard = _arm_gate_fixture(tmp_path)
    dashboard = {**dashboard, "programState": "STOPPED"}
    binding = _arm_binding(digest="d" * 64, trial_id=1, command_seq=1)
    command = SimpleNamespace(
        sha256=binding["mailbox_sha256"],
        arm_gate_binding=binding,
        packet=SimpleNamespace(command=SimpleNamespace(name="ARM")),
    )
    events: list[str] = []

    class Mailbox:
        def read_latest(self) -> Any:
            events.append("mailbox")
            return command

    fixed_ns = time.time_ns()
    row = {
        "t_wall_ns": str(fixed_ns + 1),
        "rtde_reconnects": "0",
        "ur_timestamp": "100.0",
        "ur_runtime_state": "2",
        "ur_safety_mode": "1",
        "ur_output_int_register_26": "10",
        "ur_output_int_register_35": "1",
        "ur_output_int_register_36": "1234",
        "ur_output_int_register_37": "5678",
    }
    csv_path = tmp_path / "bridge.csv"
    csv_path.write_text("fresh\n", encoding="utf-8")

    class Follower:
        path = csv_path

        def poll(self) -> dict[str, str]:
            events.append("rtde")
            return row

    class Bridge:
        pid = os.getpid()
        returncode = None

        @staticmethod
        def poll() -> None:
            return None

    def exchange(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        events.append("dashboard")
        return dashboard

    monkeypatch.setattr(live.time, "time_ns", lambda: fixed_ns)
    monkeypatch.setattr(live, "dashboard_exchange", exchange)
    delivery = _delivery_observation((fixed_ns // 1_000) * 1_000)
    monkeypatch.setattr(
        live,
        "validate_delivery_observation",
        lambda *_args, **_kwargs: delivery,
    )

    observed, _ = live._refresh_arm_gate(
        (tmp_path / "arm_gate.json").absolute(),
        lease=lease,
        lease_sha256=lease.sha256,
        bridge=Bridge(),
        csv_follower=Follower(),
        robot_host="127.0.0.1",
        runtime_contract=contract,
        mailbox_reader=Mailbox(),
        delivery_observation=delivery,
        release=SimpleNamespace(),
    )

    assert observed["gate_kind"] == "arm_grant"
    assert observed["arm_permitted"] is True
    assert observed["arm_command"] == binding
    assert events == ["mailbox", "dashboard", "rtde", "mailbox"]


def test_arm_gate_refresh_retries_if_mailbox_changes_during_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease, contract, dashboard = _arm_gate_fixture(tmp_path)
    binding1 = _arm_binding(digest="d" * 64, trial_id=1, command_seq=1)
    binding2 = _arm_binding(digest="e" * 64, trial_id=2, command_seq=2)

    def command(binding: dict[str, Any]) -> Any:
        return SimpleNamespace(
            sha256=binding["mailbox_sha256"],
            arm_gate_binding=binding,
            packet=SimpleNamespace(command=SimpleNamespace(name="ARM")),
        )

    commands = iter(
        (command(binding1), command(binding2), command(binding2), command(binding2))
    )

    class Mailbox:
        def read_latest(self) -> Any:
            return next(commands)

    fixed_ns = time.time_ns()
    row = {
        "t_wall_ns": str(fixed_ns + 1),
        "rtde_reconnects": "0",
        "ur_timestamp": "100.0",
        "ur_runtime_state": "2",
        "ur_safety_mode": "1",
        "ur_output_int_register_26": "10",
        "ur_output_int_register_35": "1",
        "ur_output_int_register_36": "1234",
        "ur_output_int_register_37": "5678",
    }
    csv_path = tmp_path / "bridge.csv"
    csv_path.write_text("fresh\n", encoding="utf-8")

    class Follower:
        path = csv_path

        @staticmethod
        def poll() -> dict[str, str]:
            return row

    class Bridge:
        pid = os.getpid()
        returncode = None

        @staticmethod
        def poll() -> None:
            return None

    monkeypatch.setattr(live.time, "time_ns", lambda: fixed_ns)
    monkeypatch.setattr(
        live, "dashboard_exchange", lambda *_args, **_kwargs: dashboard
    )
    delivery = _delivery_observation((fixed_ns // 1_000) * 1_000)
    monkeypatch.setattr(
        live,
        "validate_delivery_observation",
        lambda *_args, **_kwargs: delivery,
    )

    observed, _ = live._refresh_arm_gate(
        (tmp_path / "arm_gate.json").absolute(),
        lease=lease,
        lease_sha256=lease.sha256,
        bridge=Bridge(),
        csv_follower=Follower(),
        robot_host="127.0.0.1",
        runtime_contract=contract,
        mailbox_reader=Mailbox(),
        delivery_observation=delivery,
        release=SimpleNamespace(),
    )

    assert observed["arm_command"] == binding2
    assert observed["arm_command"] != binding1


def test_arm_gate_waits_for_explicit_rtde_event_without_deadline(
    tmp_path: Path,
) -> None:
    row = {"t_wall_ns": "101"}
    polls = iter((None, None, row))
    sleeps: list[float] = []

    bridge = SimpleNamespace(poll=lambda: None, returncode=None)
    follower = SimpleNamespace(poll=lambda: next(polls))

    observed = live._wait_arm_rtde_row(
        bridge,
        csv_follower=follower,
        not_before_unix_ns=100,
        poll_interval_s=0.001,
        sleep=sleeps.append,
    )

    assert observed is row
    assert sleeps == [0.001, 0.001]


def test_dashboard_attempt_timeout_only_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = iter((OSError("unavailable"), {"programState": "PLAYING"}))
    sleeps: list[float] = []

    def exchange(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        result = next(attempts)
        if isinstance(result, Exception):
            raise result
        return result

    observed = live._wait_dashboard_observation(
        SimpleNamespace(poll=lambda: None, returncode=None),
        robot_host="robot",
        poll_interval_s=0.01,
        exchange=exchange,
        sleep=sleeps.append,
    )

    assert observed["programState"] == "PLAYING"
    assert sleeps == [0.01]


def test_readiness_wait_has_no_elapsed_time_failure(tmp_path: Path) -> None:
    ready = tmp_path / "ready.json"
    sleeps: list[float] = []

    def sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) == 3:
            ready.write_text("{}", encoding="utf-8")

    live._wait_file(
        ready,
        SimpleNamespace(poll=lambda: None, returncode=None),
        "runner",
        poll_interval_s=0.01,
        sleep=sleep,
    )

    assert sleeps == [0.01, 0.01, 0.01]


def test_arm_gate_refresh_uses_rtde_authority_across_dashboard_flip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease, contract, dashboard = _arm_gate_fixture(tmp_path)
    fixed_ns = time.time_ns()
    csv_path = tmp_path / "bridge.csv"
    csv_path.write_text("fresh\n", encoding="utf-8")
    dashboards = iter(
        (
            {**dashboard, "programState": "STOPPED"},
            {**dashboard, "programState": "PLAYING fake_r010.urp"},
            {**dashboard, "programState": "STOPPED"},
        )
    )
    rows = iter(
        (
            {"ur_timestamp": "100.0", "ur_runtime_state": "2", "ur_safety_mode": "1", "ur_output_int_register_26": "10", "ur_output_int_register_35": "1", "ur_output_int_register_36": "1234", "ur_output_int_register_37": "5678"},
            {"ur_timestamp": "101.0", "ur_runtime_state": "2", "ur_safety_mode": "1", "ur_output_int_register_26": "10", "ur_output_int_register_35": "1", "ur_output_int_register_36": "1234", "ur_output_int_register_37": "5678"},
            {"ur_timestamp": "102.0", "ur_runtime_state": "1", "ur_safety_mode": "1", "ur_output_int_register_26": "10", "ur_output_int_register_35": "1", "ur_output_int_register_36": "1234", "ur_output_int_register_37": "5678"},
        )
    )

    class Mailbox:
        @staticmethod
        def read_latest() -> None:
            return None

    class Follower:
        path = csv_path

        @staticmethod
        def poll() -> dict[str, str]:
            return next(rows)

    class Bridge:
        pid = os.getpid()
        returncode = None

        @staticmethod
        def poll() -> None:
            return None

    monkeypatch.setattr(live.time, "time_ns", lambda: fixed_ns)
    monkeypatch.setattr(
        live, "dashboard_exchange", lambda *_args, **_kwargs: next(dashboards)
    )
    delivery = _delivery_observation((fixed_ns // 1_000) * 1_000)
    monkeypatch.setattr(
        live,
        "validate_delivery_observation",
        lambda *_args, **_kwargs: delivery,
    )

    for _ in range(2):
        observed, _ = live._refresh_arm_gate(
            (tmp_path / "arm_gate.json").absolute(),
            lease=lease,
            lease_sha256=lease.sha256,
            bridge=Bridge(),
            csv_follower=Follower(),
            robot_host="127.0.0.1",
            runtime_contract=contract,
            mailbox_reader=Mailbox(),
            delivery_observation=delivery,
            release=SimpleNamespace(),
        )
        assert observed["arm_permitted"] is True

    with pytest.raises(live.LiveLaunchError, match="rtde_playing_normal"):
        live._refresh_arm_gate(
            (tmp_path / "arm_gate.json").absolute(),
            lease=lease,
            lease_sha256=lease.sha256,
            bridge=Bridge(),
            csv_follower=Follower(),
            robot_host="127.0.0.1",
            runtime_contract=contract,
            mailbox_reader=Mailbox(),
            delivery_observation=delivery,
            release=SimpleNamespace(),
        )


def test_preplay_does_not_wait_for_stale_stopped_tp_output_registers() -> None:
    source = (ROOT / "tools/run_step5d_autotune_v3_live.py").read_text(
        encoding="utf-8"
    )

    assert "_ready_home_zero_identity" not in source
    assert "pre-Play READY_HOME" not in source
    assert "stationary zero-identity READY_HOME" not in source


def test_live_supervisor_fences_children_to_parent_lifetime() -> None:
    source = (ROOT / "tools/run_step5d_autotune_v3_live.py").read_text(
        encoding="utf-8"
    )
    shell = (ROOT / "scripts/step5d-autotune-v3.sh").read_text(encoding="utf-8")

    assert "PR_SET_PDEATHSIG" in source
    assert source.count("preexec_fn=") >= 2
    assert '--canonical-owner-pid "$$"' in shell
    assert '--canonical-owner-starttime "${launch_owner_starttime}"' in shell


def test_release_contract_has_no_simulator_internal_route() -> None:
    source = (ROOT / "scripts/step5d-autotune-v3.sh").read_text(encoding="utf-8")

    route = source.index("resolve_step5d_bridge_route.py")
    v3_contract = source.index("check_step5d_autotune_v3_bridge_admission.py")
    assert route < v3_contract
    assert "manual_release_contract" not in source
    assert "INTERNAL_QUALIFICATION_SHELL_PID" not in source
    assert "V3_QUALIFICATION_SIMULATED_PLAY_BARRIER" not in source


def test_route_snapshot_parser_preserves_empty_recovery_manifest_sha(
    tmp_path: Path,
) -> None:
    source = (ROOT / "scripts/step5d-autotune-v3.sh").read_text(encoding="utf-8")
    assert r"IFS=$'\x1f' read -r bridge_route resolved_release_sha route_reason_code" in source
    assert r'sep="\x1f"' in source

    snapshot = tmp_path / "route.json"
    snapshot.write_text(
        json.dumps(
            {
                "route": "autotune_v3",
                "manual_release_manifest_sha256": None,
                "autotune_release_manifest_sha256": None,
                "reason_code": None,
            }
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            "bash",
            "-c",
            r"""
IFS=$'\x1f' read -r bridge_route resolved_release_sha route_reason_code < <(
  "${2}" -c \
    'import json,sys; p=json.load(open(sys.argv[1], encoding="utf-8")); print(p["route"], p.get("manual_release_manifest_sha256") or p.get("autotune_release_manifest_sha256") or "", p.get("reason_code") or "LAUNCH_ATTEMPT_FAILED", sep="\x1f")' \
    "${1}"
)
printf '%s\n%s\n%s\n' "${bridge_route}" "${resolved_release_sha}" "${route_reason_code}"
""",
            "bash",
            str(snapshot),
            sys.executable,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.splitlines() == [
        "autotune_v3",
        "",
        "LAUNCH_ATTEMPT_FAILED",
    ]


def test_live_consumer_accepts_the_complete_production_preflight_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    program_id = "step5d_strict_rnn_autotune_v3_r999"
    runtime_identity = {"protocol_version": 1}
    release = SimpleNamespace(
        release_stage_id=live.RELEASE_STAGE_ID,
        control_profile_id=live.CONTROL_PROFILE_ID,
        program_id=program_id,
        manifest_sha256="a" * 64,
        generated_files={live.LAUNCH_PROFILE_PATH: "b" * 64},
    )
    monkeypatch.setattr(
        live,
        "release_runtime_contract",
        lambda *_args: {
            "expected_loaded_program": f"/programs/{program_id}.urp",
            "tp_runtime_identity": runtime_identity,
        },
    )
    payload = {
        "schema": preflight.SCHEMA,
        "ok": True,
        "fresh": True,
        "candidate_stage_id": live.RELEASE_STAGE_ID,
        "control_profile_id": live.CONTROL_PROFILE_ID,
        "tp_program_id": program_id,
        "release_manifest_sha256": release.manifest_sha256,
        "expected_loaded_program": f"/programs/{program_id}.urp",
        "tp_runtime_identity": runtime_identity,
        "launch_profile": {
            "path": live.LAUNCH_PROFILE_PATH,
            "sha256": "b" * 64,
        },
        "predicates": {
            name: {"ok": True} for name in preflight.PREDICATE_NAMES
        },
    }
    path = tmp_path / "live_preflight.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    observed = live._validate_preflight(path, release)

    assert observed == payload
    assert "prealign_start_clearance" not in observed["predicates"]

    del payload["predicates"]["robot_stationary"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(live.LiveLaunchError, match="predicates are incomplete"):
        live._validate_preflight(path, release)


def test_runner_is_observable_but_first_arm_waits_for_post_play_gate() -> None:
    source = inspect.getsource(live._run_live_session)
    writer_lease_acquired = source.index("writer_guard.__enter__()")
    bridge_start = source.index("_run_bridge_command_and_wait_for_readiness(")
    runner_start = source.index("runner = subprocess.Popen(")
    runner_ready = source.index(
        '_wait_file(runner_ready, runner, "campaign runner")'
    )
    bridge_ready = source.index(
        'bridge_ready = read_strict_json(\n                bridge_run / "bridge_ready.json"'
    )
    no_arm_ready = source.index('print("V3_BRIDGE_READY_NO_ARM"', bridge_ready)
    claim_gate = source.index("_publish_canonical_readiness_claim(", runner_ready)
    campaign_ready = source.index('print("V3_CAMPAIGN_READY_FOR_TP_PLAY"', claim_gate)
    play_signal = source.index('print("READY_FOR_ONE_PLAY_TO_MOVE"')
    play_observed = source.index("if _runtime_playing_normal", play_signal)
    gate_refresh = source.index(
        "play_gate_observation, play_dashboard = _refresh_arm_gate(",
        play_observed,
    )

    assert writer_lease_acquired < bridge_start < bridge_ready < no_arm_ready
    assert no_arm_ready < runner_start < runner_ready
    assert runner_ready < claim_gate < campaign_ready < play_signal < play_observed < gate_refresh
    assert '"--receiver-root"' in source
    assert '"--initial-manifest"' not in source
    assert "while True:" in source[source.index("READY_FOR_ONE_PLAY_TO_MOVE") :]
    assert "TP Play was not observed before timeout" not in source
    assert "readiness timeout" not in inspect.getsource(live._wait_file)
    assert "deadline" not in inspect.getsource(live._wait_arm_rtde_row)
    runner_source = (ROOT / "tools/run_step5d_parameter_campaign.py").read_text(
        encoding="utf-8"
    )
    ready_publish = runner_source.index("atomic_json(args.runner_ready_file, ready)")
    home_wait = runner_source.index("_wait_initial_home(args, follower)", ready_publish)
    first_send = runner_source.index(
        "arm, prepared = _send(args, binding=binding, dispatch=dispatch)",
        home_wait,
    )
    assert ready_publish < home_wait < first_send
    assert source.count("READY_FOR_ONE_PLAY_TO_MOVE") == 1
    assert "V3_QUALIFICATION_SIMULATED_PLAY_BARRIER" not in source
    assert "--offline-release-gate" not in runner_source
    assert 'READY_FOR_TP_PLAY_V3"' not in source
    assert "campaign_authorization.json" not in source
    assert '"--authorization-file"' not in source
    assert '"--campaign-binding"' in source
    assert 'validate_strict_bridge_ready(' in source
    assert '_validate_active_launch_identity(' in source
    assert 'read_and_validate_launch_basis(' in inspect.getsource(
        live._validate_active_launch_identity
    )
    assert '"STEP5D_BRIDGE_LAUNCH_NONCE": launch_id' in source
    assert "prepare(\n" not in source
    assert '"--campaign-arming-context"' not in source
    assert "legacy_campaign_root" not in source
    assert "--close-after-plan-revision" not in source


def test_production_writer_lease_has_exact_live_owner_and_excludes_overlap(
    tmp_path: Path,
) -> None:
    profile = ResourceProfile(
        cpu_workers=1,
        gpu_workers=1,
        gpu_vram_limit_pct=85.0,
        parallel=False,
        lock_root=tmp_path / "locks",
    )
    with writer_lease(profile, "step5d-autotune-v3-production-bridge"):
        owner = writer_lease_owner(profile)
        assert owner is not None
        assert owner["pid"] == os.getpid()
        assert owner["task"] == "step5d-autotune-v3-production-bridge"
        with pytest.raises(BlockingIOError):
            with writer_lease(profile, "second-live-writer", blocking=False):
                pytest.fail("overlapping live writer was admitted")
    assert writer_lease_owner(profile) is None


def test_canonical_shell_bridge_route_has_only_autotune_v3() -> None:
    source = (ROOT / "scripts/step5d-autotune-v3.sh").read_text(encoding="utf-8")

    assert '"${1:-}" == "bridge-live"' in source
    assert '"${1:-}" == "bridge"' in source
    assert "compatibility cutoff passed" in source
    assert '"${1:-}" == "live"' not in source
    assert source.count("resolve_step5d_bridge_route.py") == 1
    assert 'route_snapshot="${output_root}/route-snapshot.json"' in source
    assert "run_step5d_manual_bridge_live.py" not in source
    assert "run_step5d_manual_live_campaign.py" not in source
    assert "manual_v2" not in source
    assert "manual_v1" not in source
    assert "--campaign-arming-context" not in source
    assert "run_step5d_autotune_v3_live.py" in source
    assert "step5d-autotune-live.sh" not in source
    assert "bridge-line-operator.sh" not in source


def test_shell_authority_root_args_are_reusable_and_resource_id_is_shared() -> None:
    source = (ROOT / "scripts" / "step5d-autotune-v3.sh").read_text(
        encoding="utf-8"
    )

    def _function_block(function_name: str, next_name: str | None = None) -> str:
        start = source.index(f"{function_name}() {{")
        if next_name is None:
            return source[start:]
        return source[start : source.index(f"{next_name}() {{", start)]

    authority_root_snippet = (
        "BRIDGE_AUTHORITY_ROOT=\"${STEP5D_V3_AUTHORITY_ROOT:-}\"\n"
        "authority_resource_id=\"${STEP5D_V3_AUTHORITY_RESOURCE_ID:-step5d-bridge-writer}\"\n"
    )
    acquire_block = _function_block(
        "bridge_acquire_authority",
        "bridge_runtime_fail",
    )
    bootstrap_block = _function_block(
        "bridge_record_launch_attempt",
        "bridge_begin_phase",
    )
    revoke_block = _function_block("bridge_revoke_authority", "bridge_acquire_authority")
    coordinator = source[source.index("run_step5d_autotune_v3_coordinator.py") : source.index(
        '\"${CONTROL_PYTHON}\" "${EXPERIMENT_ROOT}/tools/run_step5d_autotune_v3_live.py"'
    )]

    assert authority_root_snippet in source
    assert "authority_root_args=()" in source
    assert 'authority_root_args=(--authority-root "${BRIDGE_AUTHORITY_ROOT}")' in source
    assert "${authority_root_args[@]}" in acquire_block
    assert "--resource-id \"${authority_resource_id}\"" in acquire_block
    assert "--worktree-root \"${REPOSITORY_ROOT}\"" in acquire_block
    assert "--repository-head \"${launch_repository_head}\"" in acquire_block
    assert "--launch-basis" not in acquire_block
    assert "run_step5d_autotune_v3" not in acquire_block

    assert "--authority-root" not in bootstrap_block
    assert "--resource-id \"${authority_resource_id}\"" in bootstrap_block
    assert "${authority_root_args[@]}" in bootstrap_block
    assert "runtime-start" in bootstrap_block
    assert "runtime-fail" in bootstrap_block
    assert "--attempt-id \"${launch_attempt_id}\"" in bootstrap_block
    assert "--owner-pid \"$$\"" in bootstrap_block
    assert "--owner-starttime \"${launch_owner_starttime}\"" in bootstrap_block
    assert "--resource-id \"${authority_resource_id}\"" in bootstrap_block
    assert "${authority_root_args[@]}" in bootstrap_block

    assert "${authority_root_args[@]}" in revoke_block
    assert "--resource-id \"${authority_resource_id}\"" in revoke_block

    coordinator = source[source.index("run_step5d_autotune_v3_coordinator.py") : source.index(
        '\"${CONTROL_PYTHON}\" "${EXPERIMENT_ROOT}/tools/run_step5d_autotune_v3_live.py"'
    )]
    assert "${authority_root_args[@]}" in coordinator
    assert "--authority-resource-id \"${authority_resource_id}\"" in coordinator
    assert "--resource-id" not in coordinator

    assert "export STEP5D_V3_AUTHORITY_ROOT" in source
    assert 'export STEP5D_V3_AUTHORITY_RESOURCE_ID="${authority_resource_id}"' in source

    assert source.count('--campaign-root "${campaign_root}"') == 2
    assert '--campaign-root "${LAUNCH_ATTEMPT_ROOT}"' in source
    assert '--_launch-campaign-path "${campaign_root}"' in source


def test_bridge_startup_sequence_places_runtime_start_after_basis_and_before_local_phases() -> None:
    source = (ROOT / "scripts/step5d-autotune-v3.sh").read_text(encoding="utf-8")

    acquire = source.index('"${EXPERIMENT_ROOT}/tools/step5d_bridge_authority.py" begin')
    coordinator = source.index("run_step5d_autotune_v3_coordinator.py")
    runtime_bootstrap = source.index("launch_runtime_bootstrap=1", coordinator)
    runtime_gate_records = [
        source.index("bridge_record_launch_attempt STARTED runtime_gate", coordinator)
    ]
    runtime_gate_records.append(
        source.index(
            "bridge_record_launch_attempt STARTED runtime_gate",
            runtime_gate_records[0] + 1,
        )
    )
    authority_runtime_start, local_attempt_start = runtime_gate_records
    launch_basis_binding = source.index(
        '--launch-basis "${output_root}/launch-basis.json"',
        coordinator,
    )
    preflight = source.index("bridge_begin_phase preflight")
    live_handoff = source.index("bridge_begin_phase live_handoff")
    live_runner = source.index('"${CONTROL_PYTHON}" "${EXPERIMENT_ROOT}/tools/run_step5d_autotune_v3_live.py"')

    assert acquire < coordinator < launch_basis_binding < runtime_bootstrap
    assert runtime_bootstrap < authority_runtime_start < local_attempt_start
    assert local_attempt_start < preflight < live_handoff < live_runner
    assert '--campaign-root "${LAUNCH_ATTEMPT_ROOT}"' in source


def test_production_play_prompt_requires_route_neutral_readiness_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine_status = {
        "state": "WAITING_FOR_PLAY",
        "launch_attempt": {"attempt_id": "attempt-1"},
    }
    expected_claim = {
        "schema": "step5d.bridge/readiness-claim-v1",
        "state": "WAITING_FOR_PLAY",
        "attempt_id": "attempt-1",
    }
    monkeypatch.setattr(live, "resolve_bridge_status", lambda _root: machine_status)

    def admit(status: dict[str, Any], required_state: str) -> dict[str, Any]:
        assert status is machine_status
        assert required_state == "WAITING_FOR_PLAY"
        return expected_claim

    monkeypatch.setattr(live, "readiness_claim", admit)
    monkeypatch.setattr(
        live,
        "verify_readiness_claim",
        lambda status, claim: claim
        if status is machine_status and claim is expected_claim
        else pytest.fail("V3 readiness claim verification inputs differ"),
    )

    observed = live._publish_canonical_readiness_claim(
        tmp_path, "WAITING_FOR_PLAY"
    )

    assert observed == expected_claim
    assert json.loads((tmp_path / "readiness-claim.json").read_text()) == expected_claim


def test_production_play_prompt_fails_closed_when_claim_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(live, "resolve_bridge_status", lambda _root: {"state": None})
    monkeypatch.setattr(
        live,
        "readiness_claim",
        lambda *_args: (_ for _ in ()).throw(ValueError("not admitted")),
    )

    with pytest.raises(live.LiveLaunchError, match="not admitted"):
        live._publish_canonical_readiness_claim(tmp_path, "WAITING_FOR_PLAY")

    assert not (tmp_path / "readiness-claim.json").exists()


def test_canonical_shell_reuses_existing_v3_contract_and_delivery() -> None:
    source = (ROOT / "scripts/step5d-autotune-v3.sh").read_text(encoding="utf-8")
    production = source[source.index("resolve_step5d_bridge_route.py") :]

    assert "tools/build_step5d_autotune_tp_v3.py" not in production
    assert "tools/promote_step5d_r009_atomic_release.py" not in production
    assert "tools/run_step5d_autotune_v3_qualification.py" not in production
    assert "tools/run_step5d_autotune_v3_tp_transaction.py" not in production
    assert '--delivery-observation "${delivery_observation}"' in production


def test_source_rebind_and_embedded_delivery_recovery_are_removed() -> None:
    source = (ROOT / "scripts/step5d-autotune-v3.sh").read_text(encoding="utf-8")

    assert "source_rebind" not in source
    assert "--source-rebind" not in source
    delivery = source.index("if (( tp_deliver_mode == 1 )); then")
    bridge = source.index("if (( bridge_mode == 1 )); then", delivery)
    assert source.count("run_step5d_autotune_v3_tp_transaction.py") == 2
    assert "run_step5d_autotune_v3_tp_transaction.py" in source[delivery:bridge]
    assert "run_step5d_autotune_v3_tp_transaction.py" not in source[bridge:]
    assert "--publication-plan-output" in source
    tp_execution = source[delivery:source.index("if (( runtime_revalidate_mode == 1 ));", delivery)]
    assert "--revalidate-current" not in tp_execution
    assert "--revalidate-current" in source


def test_shell_defaults_bind_current_release_and_durable_parameter_campaign() -> None:
    source = (ROOT / "scripts/step5d-autotune-v3.sh").read_text(encoding="utf-8")

    assert 'campaign_root=""' in source
    assert "parameter-campaign" in source
    assert 'campaign_root="${EXPERIMENT_ROOT}/runs/step5d_autotune_v3"' not in source
    revalidate = source[source.index('if [[ "${1:-}" == "revalidate-current"') :]
    assert 'artifact_dir=""' in revalidate
    assert 'pointer = json.loads((root / "config/step5d/current.json")' in revalidate
    assert '--artifact-dir "${artifact_dir}"' in revalidate


def test_bridge_wrapper_skips_parent_mailbox_while_identity_is_pending() -> None:
    source = (ROOT / "tools/run_step5d_autotune_v3_bridge.py").read_text(
        encoding="utf-8"
    )
    observe = source.index("identity_ready = self._v3_arm_gate.observe_rtde(")
    pending = source.index("if not identity_ready:", observe)
    parent = source.index("return super().poll(", pending)

    assert observe < pending < parent
    assert "return False" in source[pending:parent]


def test_canonical_shell_records_only_direct_live_phases() -> None:
    source = (ROOT / "scripts/step5d-autotune-v3.sh").read_text(encoding="utf-8")

    for phase in (
        "campaign_prepare",
        "preflight",
        "live_handoff",
    ):
        assert f"bridge_begin_phase {phase}" in source
    assert "bridge_begin_phase status_before" not in source
    assert "bridge_begin_phase status_after_delivery" not in source
    assert 'status --json >"${output_root}/status-before.json"' not in source
    assert 'status --json >"${output_root}/status-after-delivery.json"' not in source
    assert 'launch_attempt_phase="runtime_gate"' in source
    assert "bridge_record_launch_attempt STARTED runtime_gate" in source
    admission = source.index(
        '"${EXPERIMENT_ROOT}/tools/check_step5d_autotune_v3_bridge_admission.py"'
    )
    authority = source.index("bridge_acquire_authority", admission)
    assert admission < authority
    assert "trap bridge_failure_trap ERR" in source
    assert "--_launch-attempt-state" in source
    exact_live = (
        '"${CONTROL_PYTHON}" '
        '"${EXPERIMENT_ROOT}/tools/run_step5d_autotune_v3_live.py"'
    )
    assert source.count(exact_live) == 1
    assert f"exec {exact_live}" not in source
    assert 'python3 "${EXPERIMENT_ROOT}/tools/run_step5d_autotune_v3_live.py"' not in source


def _fake_governed_shell(
    tmp_path: Path,
    *,
    fail_prepare: bool,
) -> tuple[Path, Path, dict[str, str]]:
    repository = tmp_path / "repository"
    experiment = repository / "experiments/fake"
    scripts = experiment / "scripts"
    tools = experiment / "tools"
    scripts.mkdir(parents=True)
    tools.mkdir()
    governance_package = tools / "step5d_autotune_v3"
    governance_package.mkdir()
    for relative in (
        "tools/step5d_bridge_authority.py",
        "tools/step5d_autotune_v3/__init__.py",
        "tools/step5d_autotune_v3/atomic_io.py",
        "tools/step5d_autotune_v3/governance.py",
        "tools/step5d_autotune_v3/delivery_observation.py",
        "tools/step5d_autotune_v3/release_transition.py",
        "tools/step5d_autotune_v3/release_identity.py",
        "tools/step5d_autotune_v3/runtime_identity.py",
        "tools/step5d_autotune_v3/source_fingerprint_contract.py",
    ):
        source_path = ROOT / relative
        destination = tools / Path(relative).relative_to("tools")
        shutil.copy2(source_path, destination)
    shell = scripts / "step5d-autotune-v3.sh"
    shell_source = (ROOT / "scripts/step5d-autotune-v3.sh").read_text(
        encoding="utf-8"
    )
    production_ros_python = (
        "/opt/ros/humble/lib/python${PYTHON_ABI}/site-packages"
    )
    fake_ros_root = repository / "tests/fixtures/ros/humble"
    (fake_ros_root / "lib/python3.10/site-packages").mkdir(parents=True)
    fake_ros_python = (
        f"{fake_ros_root}/lib/python${{PYTHON_ABI}}/site-packages"
    )
    assert shell_source.count(production_ros_python) == 1
    shell_source = shell_source.replace(
        production_ros_python,
        fake_ros_python,
        1,
    )
    shell.write_text(
        shell_source,
        encoding="utf-8",
    )
    shell.chmod(0o755)
    (repository / "src/ur10e_experiment_runtime/ur10e_experiment_runtime").mkdir(
        parents=True
    )
    (repository / "install/share/ament_index/resource_index").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Step5d Test"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-q", "-m", "fixture"], check=True
    )

    command_log = tmp_path / "python-commands.log"
    profile_python = tmp_path / "governed-profile-python"
    profile_python.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "printf '%q ' \"$@\" >>\"${STEP5D_TEST_COMMAND_LOG:?}\"\n"
        "printf '\\n' >>\"${STEP5D_TEST_COMMAND_LOG:?}\"\n"
        "if [[ \" $* \" == *'/check_step5d_autotune_v3_bridge_admission.py '* ]]; then\n"
        "  admission_output=''\n"
        "  while (( $# > 0 )); do\n"
        "    if [[ \"$1\" == '--output' ]]; then admission_output=\"$2\"; break; fi\n"
        "    shift\n"
        "  done\n"
        "  [[ -n \"${admission_output}\" ]]\n"
        "  printf '%s\\n' '{\"ok\":true,\"state\":\"BRIDGE_START_READY\",\"reason_code\":\"DELIVERY_VERIFIED\",\"delivery_observation\":{\"path\":\"scripts/step5d-autotune-v3.sh\"}}' >\"${admission_output}\"\n"
        "  printf '{}\\n'\n"
        "  exit 0\n"
        "fi\n"
        "if [[ \"${1:-}\" == '-c' ]]; then printf '%032d\\n' 0; exit 0; fi\n"
        + (
            "if [[ \" $* \" == *'run_step5d_autotune_v3_coordinator.py'* ]]; then exit 41; fi\n"
            if fail_prepare
            else ""
        )
        + "printf '{}\\n'\n",
        encoding="utf-8",
    )
    profile_python.chmod(0o755)
    resolver = tools / "resolve_step5d_autotune_v3_runtime.py"
    resolver.write_text(
        "import os, sys\n"
        "if '--shell-binding' not in sys.argv:\n"
        "    raise SystemExit(64)\n"
        "if os.environ.get('STEP5D_TEST_RUNTIME_RESOLVER_FAIL') == '1':\n"
        "    raise SystemExit(78)\n"
        "python = os.environ['STEP5D_TEST_PROFILE_PYTHON']\n"
        "digest = 'a' * 64\n"
        "environment = 'environment-id'\n"
        "gpu = 'GPU-93d64fd3-924c-9c86-6c3d-b4781ed2133a'\n"
        "ld_library_path = '/runtime/control/nvidia'\n"
        "cupy_cache_dir = '/runtime/cache/cupy'\n"
        "print('\\t'.join((python, digest, digest, digest, digest, environment, gpu, ld_library_path, cupy_cache_dir)))\n",
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "PATH": "/usr/bin:/bin",
        "STEP5D_TEST_COMMAND_LOG": str(command_log),
        "STEP5D_TEST_PROFILE_PYTHON": str(profile_python),
        "STEP5D_V3_AUTHORITY_ROOT": str((repository / "runs/step5d_bridge_authority").resolve()),
    }
    return shell, command_log, environment


def test_shell_runtime_gate_failure_precedes_attempt_and_authority(
    tmp_path: Path,
) -> None:
    shell, _command_log, environment = _fake_governed_shell(
        tmp_path,
        fail_prepare=False,
    )
    environment["STEP5D_TEST_RUNTIME_RESOLVER_FAIL"] = "1"
    output = tmp_path / "output"
    result = subprocess.run(
        [
            str(shell),
            "bridge-live",
            "--output-root",
            str(output),
            "--campaign-root",
            str(tmp_path / "campaign"),
            "--delivery-observation",
            str(shell),
        ],
        cwd=shell.parent.parent,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10.0,
        check=False,
    )

    assert result.returncode == 78
    authority_root = shell.parent.parent / "runs/step5d_bridge_authority"
    assert not authority_root.exists()
    assert not (output / "bridge-authority-epoch.txt").exists()
    assert not (output / "launch-attempt-recorder.log").exists()


def test_shell_failure_trap_records_started_and_failed_phase(tmp_path: Path) -> None:
    shell, command_log, environment = _fake_governed_shell(
        tmp_path,
        fail_prepare=True,
    )
    experiment = shell.parent.parent
    output = tmp_path / "output"
    campaign = tmp_path / "campaign"
    result = subprocess.run(
        [
            str(shell),
            "bridge-live",
            "--output-root",
            str(output),
            "--campaign-root",
            str(campaign),
            "--delivery-observation",
            str(shell),
        ],
        cwd=experiment,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10.0,
        check=False,
    )

    assert result.returncode == 41, result.stderr
    commands = command_log.read_text(encoding="utf-8").splitlines()
    assert not any("_launch-attempt-phase campaign_prepare" in line for line in commands)
    assert not any("_launch-attempt-state STARTED" in line for line in commands)
    assert not any("_launch-attempt-state FAILED" in line for line in commands)
    assert any("step5d_bridge_authority.py revoke" in line for line in commands)
    assert not (output / "launch-attempt-recorder.log").exists()
    assert not any("preflight_step5d_autotune_v3.py" in line for line in commands)


def test_shell_successful_live_handoff_exits_without_operator_cli_fallthrough(
    tmp_path: Path,
) -> None:
    shell, command_log, environment = _fake_governed_shell(
        tmp_path,
        fail_prepare=False,
    )
    result = subprocess.run(
        [
            str(shell),
            "bridge-live",
            "--output-root",
            str(tmp_path / "output"),
            "--campaign-root",
            str(tmp_path / "campaign"),
            "--delivery-observation",
            str(shell),
        ],
        cwd=shell.parent.parent,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10.0,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    commands = command_log.read_text(encoding="utf-8").splitlines()
    live_calls = [
        line for line in commands if "run_step5d_autotune_v3_live.py" in line
    ]
    assert len(live_calls) == 1
    assert "--preflight" in live_calls[0]
    assert "step5d_autotune_v3.cli" not in live_calls[0]
    coordinator_calls = [
        line
        for line in commands
        if "run_step5d_autotune_v3_coordinator.py" in line
    ]
    assert len(coordinator_calls) == 1
    assert "--launch-basis" in coordinator_calls[0]


def test_shell_has_no_load_action_required_admission_branch() -> None:
    shell = (ROOT / "scripts/step5d-autotune-v3.sh").read_text(encoding="utf-8")
    assert "admission_rc == 75" not in shell
    assert "LOAD_EXACT_PROGRAM_ON_TP_AND_LEAVE_STOPPED" not in shell


def test_shell_tp_deliver_is_independent_from_bridge_authority(
    tmp_path: Path,
) -> None:
    shell, command_log, environment = _fake_governed_shell(
        tmp_path,
        fail_prepare=False,
    )
    experiment = shell.parent.parent
    evidence = experiment / "runs/delivery-observation.json"
    result = subprocess.run(
        [
            str(shell),
            "tp-deliver",
            "--release-candidate",
            str(shell),
            "--release-certificate",
            str(shell),
            "--evidence-output",
            str(evidence),
        ],
        cwd=experiment,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10.0,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    commands = command_log.read_text(encoding="utf-8").splitlines()
    transaction_calls = [
        line
        for line in commands
        if "run_step5d_autotune_v3_tp_transaction.py" in line
    ]
    assert len(transaction_calls) == 1
    assert "--release-candidate" in transaction_calls[0]
    assert "--release-certificate" in transaction_calls[0]
    assert "--evidence-output" in transaction_calls[0]
    assert "--publication-plan-output" in transaction_calls[0]
    assert "--publish-and-revalidate" not in transaction_calls[0]
    assert not any("check_step5d_autotune_v3_bridge_admission.py" in line for line in commands)
    assert not (experiment / "runs/step5d_bridge_authority").exists()


def test_shell_tp_deliver_explicit_publication_flag_reaches_transaction(
    tmp_path: Path,
) -> None:
    shell, command_log, environment = _fake_governed_shell(
        tmp_path,
        fail_prepare=False,
    )
    experiment = shell.parent.parent
    result = subprocess.run(
        [
            str(shell),
            "tp-deliver",
            "--release-candidate",
            str(shell),
            "--publish-and-revalidate",
            "--release-certificate",
            str(shell),
            "--evidence-output",
            str(experiment / "runs/delivery-publication.json"),
        ],
        cwd=experiment,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10.0,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    calls = [
        line
        for line in command_log.read_text(encoding="utf-8").splitlines()
        if "run_step5d_autotune_v3_tp_transaction.py" in line
    ]
    assert len(calls) == 1
    assert "--publish-and-revalidate" in calls[0]


def test_shell_release_contract_is_offline_and_independent_from_bridge_authority(
    tmp_path: Path,
) -> None:
    shell, command_log, environment = _fake_governed_shell(
        tmp_path,
        fail_prepare=False,
    )
    experiment = shell.parent.parent
    result = subprocess.run(
        [
            str(shell),
            "release-contract-check",
            "--release-candidate",
            str(shell),
        ],
        cwd=experiment,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10.0,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    commands = command_log.read_text(encoding="utf-8").splitlines()
    contract_calls = [
        line
        for line in commands
        if "run_step5d_release_contract.py" in line
    ]
    assert len(contract_calls) == 1
    assert "--release-candidate" in contract_calls[0]
    assert "--output-root" in contract_calls[0]
    assert not any(
        "run_step5d_autotune_v3_tp_transaction.py" in line for line in commands
    )
    assert not any(
        "check_step5d_autotune_v3_bridge_admission.py" in line for line in commands
    )
    assert not (experiment / "runs/step5d_bridge_authority").exists()


def test_shell_release_contract_requires_explicit_artifact_when_candidate_omitted(
    tmp_path: Path,
) -> None:
    shell, command_log, environment = _fake_governed_shell(
        tmp_path,
        fail_prepare=False,
    )
    experiment = shell.parent.parent
    result = subprocess.run(
        [str(shell), "release-contract-check"],
        cwd=experiment,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10.0,
        check=False,
    )

    assert result.returncode == 64
    assert "--artifact-dir is required" in result.stderr
    assert not command_log.exists()
    assert not (experiment / "runs/step5d_bridge_authority").exists()


def _run_shell_argv_gate(
    tmp_path: Path,
    arguments: list[str],
) -> tuple[subprocess.CompletedProcess[str], Path]:
    shim_root = tmp_path / "shim"
    shim_root.mkdir()
    python_marker = tmp_path / "python-invoked"
    python = shim_root / "python3"
    python.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "printf 'invoked\\n' > \"${STEP5D_TEST_PYTHON_MARKER:?}\"\n"
        "exit 99\n",
        encoding="utf-8",
    )
    python.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": os.pathsep.join((str(shim_root), "/usr/bin", "/bin")),
        "STEP5D_TEST_PYTHON_MARKER": str(python_marker),
    }
    completed = subprocess.run(
        [str(ROOT / "scripts/step5d-autotune-v3.sh"), *arguments],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=5.0,
        check=False,
    )
    return completed, python_marker


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["bridge-live", "--unknown"], "unsupported option"),
        (["bridge-live", "unexpected-positional"], "positional argument"),
        (["bridge-live", "--output-root"], "requires a value"),
        (
            ["bridge-live", "--output-root", "--play-timeout-s", "1"],
            "requires a value",
        ),
        (["bridge-live", "--output-root="], "requires a value"),
        (["bridge-live", "--campaign-root", ""], "requires a value"),
        (
            ["bridge-live", "--source-rebind"],
            "unsupported option",
        ),
        (["tp-deliver"], "--release-candidate is required"),
        (
            ["tp-deliver", "--release-candidate", "/tmp/candidate.json"],
            "--release-certificate is required",
        ),
        (["tp-deliver", "--unknown"], "unsupported option"),
        (["release-contract-check", "--unknown"], "unsupported option"),
        (["bridge-live", "--prepare-only"], "internal worker option"),
        (
            ["bridge-live", "--qualification-endpoints=/tmp/endpoints.json"],
            "unsupported option",
        ),
        (["bridge-live", "--preflight", "/tmp/preflight.json"], "internal worker option"),
        (["bridge-live", "--ready-timeout-s", "nan"], "finite positive decimal"),
        (["bridge-live", "--play-timeout-s", "0"], "unsupported option"),
        (
            [
                "bridge-live",
                "--ready-timeout-s",
                "1",
                "--ready-timeout-s=2",
            ],
            "may appear only once",
        ),
    ],
)
def test_bridge_argv_is_fully_rejected_before_python_or_side_effects(
    tmp_path: Path,
    arguments: list[str],
    message: str,
) -> None:
    completed, python_marker = _run_shell_argv_gate(tmp_path, arguments)

    assert completed.returncode == 64
    assert message in completed.stderr
    assert not python_marker.exists()


def test_bridge_invalid_argv_cannot_create_requested_output_root(tmp_path: Path) -> None:
    requested_output = tmp_path / "must-not-exist"

    completed, python_marker = _run_shell_argv_gate(
        tmp_path,
        ["bridge-live", "--output-root", str(requested_output), "--unknown"],
    )

    assert completed.returncode == 64
    assert not requested_output.exists()
    assert not python_marker.exists()


def test_bridge_alias_is_past_its_release_window(tmp_path: Path) -> None:
    completed, python_marker = _run_shell_argv_gate(tmp_path, ["bridge"])

    assert completed.returncode == 64
    assert "compatibility cutoff passed" in completed.stderr
    assert "bridge-live" in completed.stderr
    assert not python_marker.exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ["--help"],
        ["bridge-live", "--help"],
        ["tp-deliver", "--help"],
    ],
)
def test_shell_help_has_no_runtime_or_controller_side_effects(
    tmp_path: Path,
    arguments: list[str],
) -> None:
    completed, python_marker = _run_shell_argv_gate(tmp_path, arguments)

    assert completed.returncode == 0
    assert "Usage: step5d-autotune-v3.sh" in completed.stdout
    assert completed.stderr == ""
    assert not python_marker.exists()


def test_internal_live_worker_refuses_direct_execution(tmp_path: Path) -> None:
    environment = dict(os.environ)
    environment.pop(live.CANONICAL_LAUNCH_ENV, None)
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/run_step5d_autotune_v3_live.py"),
            "--output-root",
            str(tmp_path / "output"),
            "--preflight",
            str(tmp_path / "preflight.json"),
        ],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "not a public entrypoint" in result.stdout
    assert "step5d-autotune-v3.sh" in result.stdout


def test_live_parser_rejects_missing_identity_but_prepare_only_remains_compatible(
    tmp_path: Path,
) -> None:
    base = [
        "--output-root", str(tmp_path / "output"),
        "--preflight", str(tmp_path / "preflight.json"),
        "--delivery-observation", str(tmp_path / "delivery.json"),
        "--admission", str(tmp_path / "admission.json"),
        "--authority-epoch", "7",
        "--launch-basis", str(tmp_path / "basis.json"),
        "--launch-basis-sha256", "a" * 64,
        "--campaign-prepare", str(tmp_path / "campaign-prepare.json"),
        "--canonical-owner-pid", "123",
        "--canonical-owner-starttime", "456",
    ]
    for flag in (
        "--launch-basis",
        "--launch-basis-sha256",
        "--admission",
        "--authority-epoch",
        "--delivery-observation",
    ):
        index = base.index(flag)
        with pytest.raises(SystemExit):
            live.parse_args(base[:index] + base[index + 2:])

    prepared = live.parse_args(
        [
            "--prepare-only",
            "--campaign-prepare", str(tmp_path / "campaign-prepare.json"),
            "--canonical-owner-pid", "123",
            "--canonical-owner-starttime", "456",
        ]
    )
    assert prepared.prepare_only is True


def test_live_parser_retry_budget_defaults_to_zero(tmp_path: Path) -> None:
    args = live.parse_args(
        [
            "--output-root", str(tmp_path / "output"),
            "--preflight", str(tmp_path / "preflight.json"),
            "--delivery-observation", str(tmp_path / "delivery.json"),
            "--admission", str(tmp_path / "admission.json"),
            "--authority-epoch", "7",
            "--launch-basis", str(tmp_path / "basis.json"),
            "--launch-basis-sha256", "a" * 64,
            "--campaign-prepare", str(tmp_path / "campaign-prepare.json"),
            "--canonical-owner-pid", "123",
            "--canonical-owner-starttime", "456",
        ]
    )

    assert args.retry_budget == 0


def test_live_runtime_missing_identity_fails_before_output_creation(tmp_path: Path) -> None:
    args = SimpleNamespace(
        output_root=tmp_path / "output",
        preflight=tmp_path / "preflight.json",
        delivery_observation=None,
        admission=None,
        authority_epoch=None,
        launch_basis=None,
        launch_basis_sha256=None,
        campaign_prepare=tmp_path / "campaign-prepare.json",
    )
    with pytest.raises(live.LiveLaunchError, match="admission|authority-epoch"):
        live._run_live(args, {"profiles": {"control": {"python_executable": sys.executable}}})
    assert not args.output_root.exists()


def test_run_recoverable_live_session_preserves_first_pre_bridge_error_and_stops_retrying(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    args = SimpleNamespace(
        output_root=tmp_path / "output",
        preflight=tmp_path / "preflight.json",
        delivery_observation=tmp_path / "delivery.json",
        admission=tmp_path / "admission.json",
        authority_epoch=7,
        launch_basis=tmp_path / "basis.json",
        launch_basis_sha256="a" * 64,
        campaign_prepare=tmp_path / "campaign-prepare.json",
        campaign_root=tmp_path / "campaign",
        canonical_owner_pid=123,
        canonical_owner_starttime=456,
    )
    attempt_root = args.output_root / "attempt-0001"
    release = SimpleNamespace(
        release_stage_id=live.RELEASE_STAGE_ID,
        control_profile_id=live.CONTROL_PROFILE_ID,
        program_id="step5d_strict_rnn_autotune_v3_r999",
        manifest_sha256="e" * 64,
        generated_files={live.LAUNCH_PROFILE_PATH: "a" * 64},
    )

    def fail_identity(*_args: Any, **_kwargs: Any) -> tuple[Any, Any, Any]:
        raise live.LiveLaunchError("bridge admission campaign identity differs")

    monkeypatch.setattr(live, "_validate_active_launch_identity", fail_identity)
    monkeypatch.setattr(
        live,
        "load_runtime_release",
        lambda _root: release,
    )
    monkeypatch.setattr(
        live,
        "load_delivery_observation",
        lambda *_args, **_kwargs: {
            "schema": "step5d.autotune-v3/delivery-observation-v1",
            "recorded_at_unix_ns": 1,
            "release_manifest_sha256": release.manifest_sha256,
            "program_id": release.program_id,
            "controller_target": "/programs/fake_r010.urp",
            "transaction_id": "f" * 32,
            "fresh_controller_checked_at": "2026-01-01T00:00:00+00:00",
            "triplet_sha256": {
                ".script": "a" * 64,
                ".txt": "b" * 64,
                ".urp": "c" * 64,
            },
            "receipt": {"path": "runs/delivery-observation.json", "sha256": "d" * 64},
            },
    )
    args.output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    coordinator._create_coordinator_runtime_root(
        SimpleNamespace(
            output_root=args.output_root,
            owner_pid=args.canonical_owner_pid,
            owner_starttime=args.canonical_owner_starttime,
            attempt_id="attempt-no-retry",
            authority_epoch=args.authority_epoch,
        )
    )

    with pytest.raises(live.LiveLaunchError, match="bridge admission campaign identity differs"):
        live._run_live(args, {"profiles": {"control": {"python_executable": sys.executable}}})

    status = live.read_strict_json(
        args.output_root / "recoverable_session_status.json", role="recoverable session status"
    )
    assert status["state"] == "RECOVERING"
    assert status["attempt"] == 0
    assert status["orchestration_cycle"] == 1
    assert status["bridge_launch_attempt"] == 0
    assert status["session_attempt"] == 0
    assert "bridge admission campaign identity differs" in status["error"]
    assert status["attempt_root"] == str(args.output_root)
    assert not attempt_root.exists()
    assert not (args.output_root / "bridge.log").exists()
    assert not (args.output_root / "campaign_runner.log").exists()


def test_run_recoverable_live_session_writer_lease_failure_stops_before_attempt_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    args = SimpleNamespace(
        output_root=tmp_path / "output",
        preflight=tmp_path / "preflight.json",
        delivery_observation=tmp_path / "delivery.json",
        admission=tmp_path / "admission.json",
        authority_epoch=7,
        launch_basis=tmp_path / "basis.json",
        launch_basis_sha256="a" * 64,
        campaign_prepare=tmp_path / "campaign-prepare.json",
        campaign_root=tmp_path / "campaign",
        canonical_owner_pid=123,
        canonical_owner_starttime=456,
    )
    attempt_root = args.output_root / "attempt-0001"
    release = SimpleNamespace(
        release_stage_id=live.RELEASE_STAGE_ID,
        control_profile_id=live.CONTROL_PROFILE_ID,
        program_id="step5d_strict_rnn_autotune_v3_r999",
        protocol_id="v3_full_home_rolling_arm_v1",
        manifest_sha256="e" * 64,
        generated_files={live.LAUNCH_PROFILE_PATH: "a" * 64},
    )
    basis = {
        "launch_nonce": "f" * 32,
        "basis_sha256": "b" * 64,
        "campaign_fingerprint": "c" * 64,
        "delivery_observation_sha256": "d" * 64,
        "release_manifest_sha256": "e" * 64,
        "runtime_identity_sha256": "f" * 64,
        "authority_epoch": 7,
        "issued_at_unix_ns": 1,
        "expires_at_unix_ns": 10,
    }

    def validate_active(*_args: Any, **_kwargs: Any) -> tuple[Any, Any, Any]:
        return (
            basis,
            {
                "schema": "step5d.autotune-v3/bridge-admission-v1",
                "campaign_fingerprint": basis["campaign_fingerprint"],
                "release": {
                    "manifest_sha256": basis["release_manifest_sha256"],
                    "program_id": release.program_id,
                },
                "delivery_observation": {
                    "path": "runs/delivery-observation.json",
                    "sha256": "d" * 64,
                    "transaction_id": "f" * 32,
                },
            },
            {
                "schema": coordinator.CAMPAIGN_PREPARE_SCHEMA,
                "ok": True,
                "fresh": True,
                "created_at_unix_ns": 2,
                "launch_basis_sha256": basis["basis_sha256"],
                "identity": {
                    "campaign_id": "campaign-lease-failure",
                    "campaign_epoch": 1,
                    "campaign_fingerprint": basis["campaign_fingerprint"],
                    "release_manifest_sha256": basis["release_manifest_sha256"],
                    "runtime_identity_sha256": basis["runtime_identity_sha256"],
                },
                "result": {
                    "ok": True,
                    "campaign_id": "campaign-lease-failure",
                    "campaign_epoch": 1,
                    "campaign_fingerprint": basis["campaign_fingerprint"],
                    "campaign_root": str(tmp_path / "campaign"),
                    "campaign_binding_file": str(tmp_path / "campaign-binding.json"),
                    "launch_profile_path": str(tmp_path / "launch-profile.json"),
                    "launch_profile_sha256": "a" * 64,
                    "machine_binding_status": "pending_exact_candidate_and_overlay_plans",
                    "candidate_plan": str(tmp_path / "candidate-plan.json"),
                    "trial_overlay_plan": str(tmp_path / "trial-overlay-plan.json"),
                    "receiver_root": str(tmp_path / "receiver"),
                },
            },
        )

    (tmp_path / "campaign").mkdir(parents=True, exist_ok=True)
    args.output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.preflight.write_text("{}", encoding="utf-8")
    args.delivery_observation.write_text("{}", encoding="utf-8")
    args.admission.write_text("{}", encoding="utf-8")
    args.launch_basis.write_text(
        json.dumps({"campaign_fingerprint": "c" * 64}, separators=(",", ":")),
        encoding="utf-8",
    )
    (tmp_path / "candidate-plan.json").write_text(
        json.dumps(
            {
                "schema": "step5d.parameter-receiver/launch-plan-v1",
                "campaign_id": "campaign-lease-failure",
                "revision": 1,
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "trial-overlay-plan.json").write_text('{"revision":1}\n', encoding="utf-8")
    args.campaign_prepare.write_text(
        json.dumps(validate_active()[2], separators=(",", ":")),
        encoding="utf-8",
    )
    runtime_root = tmp_path / "coordinator-runtime"
    control_runtime_root = tmp_path / "control-runtime-root"
    bridge_runtime = runtime_root / "bridge" / "runtime"
    bridge_run = runtime_root / "bridge"
    control_runtime_root.mkdir(parents=True, exist_ok=True)
    bridge_runtime.mkdir(parents=True, exist_ok=True)
    runtime_pointer = {
        "profiles": {
            "control": {
                "root": str(control_runtime_root),
                "python_executable": sys.executable,
                "environment_id": "control-env-id",
            }
        },
        "attestation_sha256": "attestation-id",
        "bundle_id": "bundle-id",
    }

    monkeypatch.setattr(live, "_validate_active_launch_identity", validate_active)
    monkeypatch.setattr(
        live,
        "load_runtime_release",
        lambda _root: release,
    )
    monkeypatch.setattr(
        live,
        "load_delivery_observation",
        lambda *_args, **_kwargs: {"schema": "step5d.autotune-v3/delivery-observation-v1"},
    )
    monkeypatch.setattr(
        live,
        "_validate_coordinator_runtime_root",
        lambda *_args, **_kwargs: runtime_root,
    )
    monkeypatch.setattr(
        live,
        "_create_bridge_runtime",
        lambda *_args: (bridge_run, bridge_runtime),
    )
    monkeypatch.setattr(
        live,
        "load_contract",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        live,
        "load_launch_profile",
        lambda *_args, **_kwargs: SimpleNamespace(fingerprint="a" * 64),
    )
    monkeypatch.setattr(
        live,
        "check_effective_config",
        lambda **_kwargs: {"effective_config": {"robot_host": "192.0.2.1"}},
    )
    monkeypatch.setattr(
        live,
        "release_payload_path",
        lambda *_args, **_kwargs: tmp_path / "payload.json",
    )
    monkeypatch.setattr(
        live,
        "overlay_fingerprint",
        lambda *_args, **_kwargs: "a" * 64,
    )
    monkeypatch.setattr(
        live,
        "_validate_preflight",
        lambda *_args, **_kwargs: {"controller_identity_sha256": "a" * 64},
    )
    monkeypatch.setattr(
        live,
        "release_runtime_contract",
        lambda *_args, **_kwargs: {
            "expected_loaded_program": release.program_id,
            "safety_envelope_sha256": "a" * 64,
        },
    )

    def writer_lease_busy(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("writer lease unavailable")

    monkeypatch.setattr(live, "writer_lease", writer_lease_busy)
    monkeypatch.setattr(
        live,
        "build_bridge_argv",
        lambda *_args, **_kwargs: ["python", str(live.WRAPPER), "bridge"],
    )
    monkeypatch.setattr(
        live,
        "production_runtime_environment",
        lambda *_args, **_kwargs: {"PATH": "/bin", "PYTHONPATH": "", "HOME": "/tmp"},
    )

    with pytest.raises(live.LiveLaunchError, match="writer lease is unavailable"):
        live._run_live_session(
            args,
            runtime_pointer,
        )

    assert not attempt_root.exists()
    assert not (args.output_root / "bridge.log").exists()
    assert not (args.output_root / "campaign_runner.log").exists()


def test_run_recoverable_live_session_stops_retry_on_raw_session_exception(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    args = SimpleNamespace(
        output_root=tmp_path / "output",
        preflight=tmp_path / "preflight.json",
        delivery_observation=tmp_path / "delivery.json",
        admission=tmp_path / "admission.json",
        authority_epoch=7,
        launch_basis=tmp_path / "basis.json",
        launch_basis_sha256="a" * 64,
        campaign_prepare=tmp_path / "campaign-prepare.json",
        campaign_root=tmp_path / "campaign",
        canonical_owner_pid=123,
        canonical_owner_starttime=456,
    )
    attempt_root = args.output_root / "attempt-0001"
    attempts: list[int] = []

    def hard_fail(*_args: Any, **_kwargs: Any) -> dict[str, bool]:
        attempts.append(1)
        raise RuntimeError("unclassified runtime fault")

    monkeypatch.setattr(live, "_run_live_session", hard_fail)

    with pytest.raises(RuntimeError, match="unclassified runtime fault"):
        live._run_live(args, {"profiles": {"control": {"python_executable": sys.executable}}})

    status = live.read_strict_json(
        args.output_root / "recoverable_session_status.json", role="recoverable session status"
    )
    assert status["state"] == "RECOVERING"
    assert status["attempt"] == 0
    assert status["orchestration_cycle"] == 1
    assert status["bridge_launch_attempt"] == 0
    assert status["session_attempt"] == 0
    assert "unclassified runtime fault" in status["error"]
    assert status["attempt_root"] == str(args.output_root)
    assert not attempt_root.exists()
    assert attempts == [1]


def test_run_live_single_session_nonrecoverable_failure_preserves_recovering_and_invokes_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    args = SimpleNamespace(
        output_root=tmp_path / "output",
        preflight=tmp_path / "preflight.json",
        delivery_observation=tmp_path / "delivery.json",
        admission=tmp_path / "admission.json",
        authority_epoch=7,
        launch_basis=tmp_path / "basis.json",
        launch_basis_sha256="a" * 64,
        campaign_prepare=tmp_path / "campaign-prepare.json",
        campaign_root=tmp_path / "campaign",
        canonical_owner_pid=123,
        canonical_owner_starttime=456,
        single_session=True,
    )
    attempt_root = args.output_root / "attempt-0001"
    dispatch_calls: list[str] = []
    original_dispatch = live.dispatch_single_session

    def dispatch_spy(session_callable: Any, cleanup: Any) -> Any:
        dispatch_calls.append("dispatch")

        def wrapped_cleanup() -> None:
            dispatch_calls.append("cleanup")
            cleanup()

        return original_dispatch(session_callable, wrapped_cleanup)

    monkeypatch.setattr(live, "dispatch_single_session", dispatch_spy)
    monkeypatch.setattr(
        live,
        "_run_live_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            live.LiveSessionAttemptError(RuntimeError("single session hard failure"), recoverable=False)
        ),
    )

    with pytest.raises(RuntimeError, match="single session hard failure"):
        live._run_live(args, {"profiles": {"control": {"python_executable": sys.executable}}})

    assert dispatch_calls == ["dispatch", "cleanup"]
    status = live.read_strict_json(
        args.output_root / "recoverable_session_status.json",
        role="recoverable session status",
    )
    assert status["state"] == "RECOVERING"
    assert status["attempt"] == 0
    assert status["attempt_root"] == str(args.output_root)
    assert status["orchestration_cycle"] == 1
    assert status["bridge_launch_attempt"] == 0
    assert status["session_attempt"] == 0
    assert "single session hard failure" in status["error"]


def test_run_recoverable_live_session_retries_once_for_recoverable_session_failure_waiting_before_backoff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    args = SimpleNamespace(
        output_root=tmp_path / "output",
        preflight=tmp_path / "preflight.json",
        delivery_observation=tmp_path / "delivery.json",
        admission=tmp_path / "admission.json",
        authority_epoch=7,
        launch_basis=tmp_path / "basis.json",
        launch_basis_sha256="a" * 64,
        campaign_prepare=tmp_path / "campaign-prepare.json",
        campaign_root=tmp_path / "campaign",
        canonical_owner_pid=123,
        canonical_owner_starttime=456,
    )
    attempts: list[int] = []
    status_before_backoff: list[str] = []

    def recoverable_then_success(*_args: Any, **_kwargs: Any) -> dict[str, bool]:
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            raise live.RecoverableLiveSessionAttemptError(
                RuntimeError("temporary hardware fault")
            )
        return {"ok": True}

    def record_waiting_before_backoff(_seconds: float = live.RECOVERY_BACKOFF_S) -> None:
        status = live.read_strict_json(
            args.output_root / "recoverable_session_status.json",
            role="recoverable session status",
        )
        status_before_backoff.append(status["state"])

    monkeypatch.setattr(live, "_run_live_session", recoverable_then_success)
    args.retry_budget = 1
    monkeypatch.setattr(live.time, "sleep", record_waiting_before_backoff)

    result = live._run_live(args, {"profiles": {"control": {"python_executable": sys.executable}}})

    assert result["ok"] is True
    assert attempts == [1, 2]
    assert status_before_backoff == ["WAITING_FOR_HARDWARE"]


def test_refresh_live_bridge_admission_prefers_fresh_observation_when_reference_is_stale(
    tmp_path: Path,
) -> None:
    root = tmp_path
    basis = {
        "launch_nonce": "stale-observation-test",
        "campaign_fingerprint": "c" * 64,
    }
    reference_admission = {
        "schema": "step5d.autotune-v3/bridge-admission-v2",
        "campaign_fingerprint": "c" * 64,
        "observed_at_unix_ns": 100_000_000_0,
        "state": "BRIDGE_START_READY",
        "ok": True,
        "reason_code": "DELIVERY_VERIFIED",
        "checks": {},
        "program_state": "STOPPED step5d_strict_rnn_autotune_v3_r999",
        "loaded_program": "step5d_strict_rnn_autotune_v3_r999",
        "expected_loaded_program": "step5d_strict_rnn_autotune_v3_r999",
        "operator_action": None,
        "authority_acquired": False,
        "attempt_created": False,
        "release_contract": {
            "certificate_path": "runs/contract.json",
            "certificate_sha256": "a" * 64,
            "evidence_path": "runs/contract-evidence.json",
            "evidence_sha256": "b" * 64,
        },
        "publication_lineage": {
            "path": "runs/publication-lineage.json",
            "sha256": "c" * 64,
        },
        "dashboard": {
            "programState": "STOPPED step5d_strict_rnn_autotune_v3_r999",
            "get loaded program": "/programs/step5d_strict_rnn_autotune_v3_r999.urp",
        },
        "release": {
            "manifest_sha256": "e" * 64,
            "program_id": "step5d_strict_rnn_autotune_v3_r999",
        },
        "delivery_observation": {
            "path": "runs/delivery-observation.json",
            "sha256": "d" * 64,
            "transaction_id": "f" * 32,
        },
    }
    fresh_observation = dict(reference_admission)
    fresh_observation["observed_at_unix_ns"] = 10_000_000_000

    observed: list[str] = []

    def observe(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        observed.append("observe")
        return dict(fresh_observation)

    def validate(
        *_args: Any,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        observed.append("validate")
        return dict(fresh_observation)

    indexed = root / "runs/step5d_autotune_v3/bridge-admissions/e" / "cafebabe.json"

    def write_indexed(*_args: Any, **_kwargs: Any) -> Path:
        observed.append("write")
        return indexed

    release = SimpleNamespace(manifest_sha256="e" * 64, program_id="step5d_strict_rnn_autotune_v3_r999")
    result = live._refresh_live_bridge_admission(
        root,
        basis=basis,
        reference_admission=reference_admission,
        release=release,
        compatibility_delivery_observation=tmp_path / "runs/delivery-observation.json",
        observe=observe,
        validate=validate,
        write_indexed=write_indexed,
        now_ns=10_000_000_005,
    )

    assert result == indexed
    assert observed == ["observe", "validate", "write"]
    assert fresh_observation["campaign_fingerprint"] == basis["campaign_fingerprint"]


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        ("campaign", "campaign identity"),
        ("release", "release binding"),
        ("delivery", "delivery binding"),
    ],
)
def test_refresh_live_bridge_admission_rejects_drift_before_runner_spawn(
    tmp_path: Path,
    mutate: str,
    fragment: str,
) -> None:
    basis = {
        "launch_nonce": "drift-observation-test",
        "campaign_fingerprint": "c" * 64,
    }
    release = SimpleNamespace(manifest_sha256="e" * 64, program_id="step5d_strict_rnn_autotune_v3_r999")
    reference_admission = {
        "schema": "step5d.autotune-v3/bridge-admission-v2",
        "campaign_fingerprint": "c" * 64,
        "state": "BRIDGE_START_READY",
        "ok": True,
        "reason_code": "DELIVERY_VERIFIED",
        "checks": {},
        "observed_at_unix_ns": 100_000_000_0,
        "program_state": "STOPPED step5d_strict_rnn_autotune_v3_r999",
        "loaded_program": "step5d_strict_rnn_autotune_v3_r999",
        "expected_loaded_program": "step5d_strict_rnn_autotune_v3_r999",
        "operator_action": None,
        "authority_acquired": False,
        "attempt_created": False,
        "release_contract": {
            "certificate_path": "runs/contract.json",
            "certificate_sha256": "a" * 64,
            "evidence_path": "runs/contract-evidence.json",
            "evidence_sha256": "b" * 64,
        },
        "publication_lineage": {
            "path": "runs/publication-lineage.json",
            "sha256": "c" * 64,
        },
        "dashboard": {
            "programState": "STOPPED step5d_strict_rnn_autotune_v3_r999",
            "get loaded program": "/programs/step5d_strict_rnn_autotune_v3_r999.urp",
        },
        "release": {
            "manifest_sha256": "e" * 64,
            "program_id": "step5d_strict_rnn_autotune_v3_r999",
        },
        "delivery_observation": {
            "path": "runs/delivery-observation.json",
            "sha256": "d" * 64,
            "transaction_id": "f" * 32,
        },
        "observed_at_unix_ns": 1,
    }
    fresh = deepcopy(reference_admission)
    fresh.update(
        {
            "observed_at_unix_ns": 2,
            "ok": True,
            "state": "BRIDGE_START_READY",
            "program_state": "STOPPED step5d_strict_rnn_autotune_v3_r999",
        }
    )
    if mutate == "campaign":
        fresh["campaign_fingerprint"] = "d" * 64
    elif mutate == "release":
        fresh["release"] = {"manifest_sha256": "d" * 64, "program_id": "other"}
    elif mutate == "delivery":
        fresh["delivery_observation"]["transaction_id"] = "e" * 32

    def observe(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return dict(fresh)

    def validate(
        *_args: Any,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        return dict(fresh)

    def write_indexed(*_args: Any, **_kwargs: Any) -> Path:
        return tmp_path / "should-not-exist"

    with pytest.raises(live.LiveLaunchError, match=fragment):
        live._refresh_live_bridge_admission(
            tmp_path,
            basis=basis,
            reference_admission=reference_admission,
            release=release,
            compatibility_delivery_observation=tmp_path / "runs/delivery-observation.json",
            observe=observe,
            validate=validate,
            write_indexed=write_indexed,
            now_ns=10_000_000_005,
        )


def test_run_live_single_session_keyboard_interrupt_preserves_shutdown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    args = SimpleNamespace(
        output_root=tmp_path / "output",
        preflight=tmp_path / "preflight.json",
        delivery_observation=tmp_path / "delivery.json",
        admission=tmp_path / "admission.json",
        authority_epoch=7,
        launch_basis=tmp_path / "basis.json",
        launch_basis_sha256="a" * 64,
        campaign_prepare=tmp_path / "campaign-prepare.json",
        campaign_root=tmp_path / "campaign",
        canonical_owner_pid=123,
        canonical_owner_starttime=456,
        single_session=True,
    )

    monkeypatch.setattr(
        live,
        "_run_live_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt),
    )

    with pytest.raises(KeyboardInterrupt):
        live._run_live(args, {"profiles": {"control": {"python_executable": sys.executable}}})

    status = live.read_strict_json(
        args.output_root / "recoverable_session_status.json",
        role="recoverable session status",
    )
    assert status["state"] == "SHUTDOWN"


def test_validate_active_launch_identity_maps_live_cli_shape_to_coordinator_namespace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    args = SimpleNamespace(
        output_root=tmp_path / "attempt-0001",
        _coordinator_output_root=tmp_path / "coordinator-output",
        preflight=tmp_path / "preflight.json",
        delivery_observation=tmp_path / "delivery.json",
        admission=tmp_path / "admission.json",
        authority_epoch=7,
        launch_basis=tmp_path / "basis.json",
        launch_basis_sha256="a" * 64,
        campaign_prepare=tmp_path / "campaign-prepare.json",
        campaign_root=tmp_path / "campaign",
        canonical_owner_pid=111,
        canonical_owner_starttime=222,
    )
    args.admission.write_text(
        json.dumps(
            {
                "schema": "step5d.autotune-v3/bridge-admission-v1",
                "campaign_fingerprint": "c" * 64,
                "observed_at_unix_ns": 10_000_000_000,
                "state": "BENCH_READY",
                "ok": True,
                "reason_code": "PROGRAM_LOADED_STOPPED",
                "checks": {},
                "program_state": "STOPPED step5d_strict_rnn_autotune_v3_r999",
                "loaded_program": "step5d_strict_rnn_autotune_v3_r999",
                "expected_loaded_program": "step5d_strict_rnn_autotune_v3_r999",
                "operator_action": None,
                "authority_acquired": False,
                "attempt_created": False,
                "release_contract": {
                    "certificate_path": "runs/contract.json",
                    "certificate_sha256": "a" * 64,
                    "evidence_path": "runs/contract-evidence.json",
                    "evidence_sha256": "b" * 64,
                },
                "publication_lineage": {
                    "path": "runs/publication-lineage.json",
                    "sha256": "c" * 64,
                },
                "dashboard": {
                    "programState": "STOPPED step5d_strict_rnn_autotune_v3_r999",
                    "get loaded program": "/programs/step5d_strict_rnn_autotune_v3_r999.urp",
                },
                "release": {
                    "manifest_sha256": "e" * 64,
                    "program_id": "step5d_strict_rnn_autotune_v3_r999",
                },
                "delivery_observation": {
                    "path": "runs/delivery-observation.json",
                    "sha256": "d" * 64,
                    "transaction_id": "f" * 32,
                },
            }
        ),
        encoding="utf-8",
    )

    basis = {
        "launch_nonce": "live-attempt-sentinel",
        "basis_sha256": "b" * 64,
        "campaign_fingerprint": "c" * 64,
        "delivery_observation_sha256": "d" * 64,
        "release_manifest_sha256": "e" * 64,
        "runtime_identity_sha256": "f" * 64,
        "authority_epoch": 7,
        "issued_at_unix_ns": 1,
        "expires_at_unix_ns": 10,
    }
    campaign_prepare = {
        "schema": coordinator.CAMPAIGN_PREPARE_SCHEMA,
        "ok": True,
        "fresh": True,
        "created_at_unix_ns": 2,
        "launch_basis_sha256": basis["basis_sha256"],
        "identity": {
            "campaign_id": "campaign-mapping",
            "campaign_epoch": 1,
            "campaign_fingerprint": basis["campaign_fingerprint"],
            "release_manifest_sha256": basis["release_manifest_sha256"],
            "runtime_identity_sha256": basis["runtime_identity_sha256"],
        },
        "result": {
            "ok": True,
            "campaign_id": "campaign-mapping",
            "campaign_epoch": 1,
            "campaign_fingerprint": basis["campaign_fingerprint"],
            "campaign_root": str(tmp_path / "campaign"),
            "campaign_binding_file": str(tmp_path / "campaign-binding.json"),
            "launch_profile_path": str(tmp_path / "launch-profile.json"),
            "launch_profile_sha256": "0" * 64,
            "machine_binding_status": "ready_parameter_receiver",
            "candidate_plan": str(tmp_path / "candidate-plan.json"),
            "trial_overlay_plan": str(tmp_path / "trial-overlay-plan.json"),
            "receiver_root": str(tmp_path / "receiver"),
        },
    }
    coordinator_output = tmp_path / "coordinator-output"
    coordinator_output.mkdir(mode=0o700, exist_ok=True)
    coordinator._create_coordinator_runtime_root(
        SimpleNamespace(
            output_root=coordinator_output,
            _coordinator_output_root=coordinator_output,
            owner_pid=111,
            owner_starttime=222,
            attempt_id=basis["launch_nonce"],
            authority_epoch=7,
        )
    )

    def fake_read_and_validate_launch_basis(*_args: Any, **_kwargs: Any) -> dict[str, object]:
        return basis

    def fake_validate_delivery_observation_binding(
        *_args: Any, **_kwargs: Any
    ) -> None:
        pass

    def fake_validate_bridge_admission(
        *_args: Any, **_kwargs: Any
    ) -> dict[str, Any]:
        return {
            "schema": "step5d.autotune-v3/bridge-admission-v1",
            "observed_at_unix_ns": 10_000_000_000,
            "state": "BENCH_READY",
            "ok": True,
            "reason_code": "PROGRAM_LOADED_STOPPED",
            "checks": {},
            "program_state": "STOPPED step5d_strict_rnn_autotune_v3_r999",
            "loaded_program": "step5d_strict_rnn_autotune_v3_r999",
            "expected_loaded_program": "step5d_strict_rnn_autotune_v3_r999",
            "operator_action": None,
            "authority_acquired": False,
            "attempt_created": False,
            "release_contract": {
                "certificate_path": "runs/contract.json",
                "certificate_sha256": "a" * 64,
                "evidence_path": "runs/contract-evidence.json",
                "evidence_sha256": "b" * 64,
            },
            "publication_lineage": {
                "path": "runs/publication-lineage.json",
                "sha256": "c" * 64,
            },
            "dashboard": {
                "programState": "STOPPED step5d_strict_rnn_autotune_v3_r999",
                "get loaded program": "/programs/step5d_strict_rnn_autotune_v3_r999.urp",
            },
            "campaign_fingerprint": basis["campaign_fingerprint"],
            "release": {
                "manifest_sha256": basis["release_manifest_sha256"],
                "program_id": "step5d_strict_rnn_autotune_v3_r999",
            },
            "delivery_observation": {
                "path": "runs/delivery-observation.json",
                "sha256": "d" * 64,
                "transaction_id": "f" * 32,
            },
        }

    def fake_validate_campaign_prepare(
        payload: Any, basis_payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        assert payload == campaign_prepare
        return campaign_prepare

    def fake_release_identity(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(program_id="step5d_strict_rnn_autotune_v3_r999")

    monkeypatch.setattr(live, "read_and_validate_launch_basis", fake_read_and_validate_launch_basis)
    monkeypatch.setattr(live, "validate_delivery_observation_binding", fake_validate_delivery_observation_binding)
    monkeypatch.setattr(live, "validate_bridge_admission", fake_validate_bridge_admission)
    monkeypatch.setattr(coordinator, "_validate_campaign_prepare", fake_validate_campaign_prepare)

    args.launch_basis = tmp_path / "basis.json"
    args.launch_basis_sha256 = basis["basis_sha256"]
    args.campaign_prepare.write_text(json.dumps(campaign_prepare), encoding="utf-8")
    args.launch_basis.write_text(json.dumps(basis), encoding="utf-8")

    assert not hasattr(args, "owner_pid")
    assert not hasattr(args, "owner_starttime")

    checked_basis, checked_admission, checked_campaign = live._validate_active_launch_identity(
        args,
        fake_release_identity(),
    )

    assert checked_basis == basis
    assert checked_admission == {
        "schema": "step5d.autotune-v3/bridge-admission-v1",
        "observed_at_unix_ns": 10_000_000_000,
        "state": "BENCH_READY",
        "ok": True,
        "reason_code": "PROGRAM_LOADED_STOPPED",
        "checks": {},
        "program_state": "STOPPED step5d_strict_rnn_autotune_v3_r999",
        "loaded_program": "step5d_strict_rnn_autotune_v3_r999",
        "expected_loaded_program": "step5d_strict_rnn_autotune_v3_r999",
        "operator_action": None,
        "authority_acquired": False,
        "attempt_created": False,
        "release_contract": {
            "certificate_path": "runs/contract.json",
            "certificate_sha256": "a" * 64,
            "evidence_path": "runs/contract-evidence.json",
            "evidence_sha256": "b" * 64,
        },
        "publication_lineage": {
            "path": "runs/publication-lineage.json",
            "sha256": "c" * 64,
        },
        "dashboard": {
            "programState": "STOPPED step5d_strict_rnn_autotune_v3_r999",
            "get loaded program": "/programs/step5d_strict_rnn_autotune_v3_r999.urp",
        },
        "campaign_fingerprint": basis["campaign_fingerprint"],
        "release": {
            "manifest_sha256": "e" * 64,
            "program_id": "step5d_strict_rnn_autotune_v3_r999",
        },
        "delivery_observation": {
            "path": "runs/delivery-observation.json",
            "sha256": "d" * 64,
            "transaction_id": "f" * 32,
        },
    }
    assert checked_campaign == campaign_prepare

    contract_path = coordinator_output / "runtime" / coordinator.RUNTIME_ROOT_CONTRACT_FILE
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    assert contract["attempt_id"] == basis["launch_nonce"]
    assert contract["owner_pid"] == args.canonical_owner_pid
    assert contract["owner_starttime"] == args.canonical_owner_starttime
    assert contract["authority_epoch"] == 7


def test_run_live_session_uses_basis_launch_nonce_not_mutable_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    args = SimpleNamespace(
        output_root=tmp_path / "output",
        preflight=tmp_path / "preflight.json",
        delivery_observation=tmp_path / "delivery.json",
        admission=tmp_path / "admission.json",
        authority_epoch=7,
        launch_basis=tmp_path / "basis.json",
        launch_basis_sha256="a" * 64,
        campaign_prepare=tmp_path / "campaign-prepare.json",
        campaign_root=tmp_path / "campaign",
        canonical_owner_pid=111,
        canonical_owner_starttime=222,
    )
    basis = {
        "launch_nonce": "basis-attempt-lock",
        "basis_sha256": "b" * 64,
        "campaign_fingerprint": "c" * 64,
        "delivery_observation_sha256": "d" * 64,
        "release_manifest_sha256": "e" * 64,
        "runtime_identity_sha256": "f" * 64,
        "authority_epoch": 7,
        "issued_at_unix_ns": 1,
        "expires_at_unix_ns": 10,
    }
    observed = {"attempt_id": None}

    class _NullWriterLease:
        def __enter__(self) -> None:
            return None

        def __exit__(self, *_args: Any) -> None:
            return None

    monkeypatch.setenv("STEP5D_V3_LAUNCH_ATTEMPT_ID", "drifted-attempt")
    monkeypatch.setattr(
        live,
        "writer_lease",
        lambda *_args, **_kwargs: _NullWriterLease(),
    )
    monkeypatch.setattr(
        live,
        "_validate_active_launch_identity",
        lambda *_args: (
            basis,
            {
                "schema": "step5d.autotune-v3/bridge-admission-v1",
                "campaign_fingerprint": basis["campaign_fingerprint"],
                "release": {
                    "manifest_sha256": basis["release_manifest_sha256"],
                    "program_id": "step5d_strict_rnn_autotune_v3_r999",
                },
                "delivery_observation": {
                    "path": "runs/delivery-observation.json",
                    "sha256": "d" * 64,
                    "transaction_id": "f" * 32,
                },
            },
            {},
        ),
    )
    monkeypatch.setattr(
        live,
        "load_runtime_release",
        lambda _root: SimpleNamespace(
            release_stage_id=live.RELEASE_STAGE_ID,
            control_profile_id=live.CONTROL_PROFILE_ID,
            program_id="step5d_strict_rnn_autotune_v3_r999",
            manifest_sha256="e" * 64,
            generated_files={live.LAUNCH_PROFILE_PATH: "a" * 64},
        ),
    )
    monkeypatch.setattr(
        live,
        "load_delivery_observation",
        lambda *_args, **_kwargs: {"schema": "ignored"},
    )
    monkeypatch.setattr(
        live,
        "_validate_coordinator_runtime_root",
        lambda *args, **kwargs: (
            observed.__setitem__(
                "attempt_id",
                kwargs.get("basis", {}).get("launch_nonce")
                if isinstance(kwargs.get("basis"), Mapping)
                else None,
            ),
            tmp_path / "runtime-root",
        )[1],
    )
    monkeypatch.setattr(
        live,
        "_create_bridge_runtime",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("stop-before-bridge")),
    )

    with pytest.raises(RuntimeError, match="stop-before-bridge"):
        live._run_live_session(
            args,
            {"profiles": {"control": {"python_executable": sys.executable}}},
        )

    assert observed["attempt_id"] == basis["launch_nonce"]


def test_run_live_session_refreshes_fresh_bridge_admission_before_runner_spawn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class _DummyWriterLease:
        def __enter__(self) -> None:
            return None

        def __exit__(self, *_args: Any) -> None:
            return None

    class _FakeProcess:
        def __init__(self, *, returncode: int | None = None, pid: int = 1234) -> None:
            self.returncode = returncode
            self.pid = pid

        def send_signal(self, *_args: Any) -> None:
            if self.returncode is None:
                self.returncode = 0

        def terminate(self) -> None:
            self.returncode = 0

        def kill(self) -> None:
            self.returncode = 0

        def wait(self, *_args: Any, **_kwargs: Any) -> int | None:
            return self.returncode

        def poll(self) -> int | None:
            return self.returncode

    class _DummyPublisher:
        def update_lifecycle(self, *args: Any, **_kwargs: Any) -> None:
            return None

        def revoke_lease(self, *args: Any, **_kwargs: Any) -> None:
            return None

    output_root = tmp_path / "attempt-0001"
    output_root.mkdir(parents=True)
    args = SimpleNamespace(
        output_root=output_root,
        preflight=tmp_path / "preflight.json",
        delivery_observation=tmp_path / "delivery.json",
        admission=tmp_path / "admission.json",
        authority_epoch=7,
        launch_basis=tmp_path / "basis.json",
        launch_basis_sha256="a" * 64,
        campaign_prepare=tmp_path / "campaign-prepare.json",
        campaign_root=tmp_path / "campaign",
        canonical_owner_pid=111,
        canonical_owner_starttime=222,
    )
    args.output_root.mkdir(parents=True, exist_ok=True)
    args.campaign_root.mkdir(parents=True, exist_ok=True)
    args.preflight.write_text("{}", encoding="utf-8")
    args.delivery_observation.write_text("{}", encoding="utf-8")
    args.admission.write_text(
        json.dumps(
            {
                "schema": "step5d.autotune-v3/bridge-admission-v1",
                "campaign_fingerprint": "c" * 64,
                "release": {
                    "manifest_sha256": "e" * 64,
                    "program_id": "step5d_strict_rnn_autotune_v3_r999",
                },
                "delivery_observation": {
                    "path": "runs/delivery-observation.json",
                    "sha256": "d" * 64,
                    "transaction_id": "f" * 32,
                },
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    args.launch_basis.write_text(json.dumps({"campaign_fingerprint": "c" * 64}, separators=(",", ":")), encoding="utf-8")

    runtime_root = tmp_path / "coordinator-runtime"
    bridge_runtime = runtime_root / "bridge" / "runtime"
    bridge_run = runtime_root / "bridge"
    bridge_runtime.mkdir(parents=True, exist_ok=True)
    control_runtime_root = tmp_path / "control-runtime-root"
    control_runtime_root.mkdir(parents=True, exist_ok=True)
    basis = {
        "launch_nonce": "b" * 32,
        "basis_sha256": "b" * 64,
        "campaign_fingerprint": "c" * 64,
        "delivery_observation_sha256": "d" * 64,
        "release_manifest_sha256": "e" * 64,
        "runtime_identity_sha256": "f" * 64,
        "authority_epoch": 7,
        "issued_at_unix_ns": 1,
        "expires_at_unix_ns": 10,
    }
    plan_path = tmp_path / "candidate-plan.json"
    trial_plan_path = tmp_path / "trial-overlay-plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "schema": "step5d.parameter-receiver/launch-plan-v1",
                "campaign_id": "campaign-order-check",
                "revision": 1,
            }
        ),
        encoding="utf-8",
    )
    trial_plan_path.write_text('{"revision":1}\n', encoding="utf-8")
    campaign_prepare = {
        "schema": coordinator.CAMPAIGN_PREPARE_SCHEMA,
        "ok": True,
        "fresh": True,
        "created_at_unix_ns": 2,
        "launch_basis_sha256": basis["basis_sha256"],
        "identity": {
            "campaign_id": "campaign-order-check",
            "campaign_epoch": 1,
            "campaign_fingerprint": basis["campaign_fingerprint"],
            "release_manifest_sha256": basis["release_manifest_sha256"],
            "runtime_identity_sha256": basis["runtime_identity_sha256"],
        },
        "result": {
            "ok": True,
            "campaign_id": "campaign-order-check",
            "campaign_epoch": 1,
            "campaign_fingerprint": basis["campaign_fingerprint"],
            "campaign_root": str(tmp_path / "campaign"),
            "campaign_binding_file": str(tmp_path / "campaign-binding.json"),
            "launch_profile_path": str(tmp_path / "launch-profile.json"),
            "launch_profile_sha256": "a" * 64,
            "machine_binding_status": "pending_exact_candidate_and_overlay_plans",
            "candidate_plan": str(plan_path),
            "trial_overlay_plan": str(trial_plan_path),
            "receiver_root": str(tmp_path / "receiver"),
        },
    }
    args.campaign_prepare.write_text(json.dumps(campaign_prepare, separators=(",", ":")), encoding="utf-8")

    order: list[str] = []
    indexed_admission = tmp_path / "runs/step5d_autotune_v3/bridge-admissions/e" / "fresh.json"
    command_seen: dict[str, list[str] | None] = {"runner": None}

    def validate_active(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        return basis, {
            "schema": "step5d.autotune-v3/bridge-admission-v1",
            "campaign_fingerprint": basis["campaign_fingerprint"],
            "release": {
                "manifest_sha256": basis["release_manifest_sha256"],
                "program_id": "step5d_strict_rnn_autotune_v3_r999",
            },
            "delivery_observation": {
                "path": "runs/delivery-observation.json",
                "sha256": "d" * 64,
                "transaction_id": "f" * 32,
            },
        }, campaign_prepare

    def validate_ready(*_args: Any, **_kwargs: Any) -> None:
        order.append("bridge_ready")

    def refresh_admission(*_args: Any, **_kwargs: Any) -> Path:
        order.append("fresh_admission")
        return indexed_admission

    def bridge_command(*_args: Any, **_kwargs: Any) -> tuple[subprocess.Popen[Any], int, int]:
        order.append("bridge_launch")
        return _FakeProcess(), 0, 0

    def runner_process(command: list[str], *args: Any, **kwargs: Any) -> _FakeProcess:
        order.append("runner_spawn")
        command_seen["runner"] = list(command)
        return _FakeProcess(pid=4321, returncode=None)

    def fake_wait_file(*_args: Any, **_kwargs: Any) -> None:
        return None

    def fake_read_json(path: Path, *, role: str) -> dict[str, Any]:
        if role == "parameter receiver plan":
            return {"revision": 1}
        if role == "bridge readiness":
            return {
                "schema": "step5d.autotune-v3/bridge-ready-v1",
            }
        return {"result": {}}

    monkeypatch.setattr(live, "writer_lease", lambda *_args, **_kwargs: _DummyWriterLease())
    monkeypatch.setattr(live, "_validate_active_launch_identity", validate_active)
    monkeypatch.setattr(
        live,
        "load_runtime_release",
        lambda _root: SimpleNamespace(
            release_stage_id=live.RELEASE_STAGE_ID,
            control_profile_id=live.CONTROL_PROFILE_ID,
            protocol_id="v3_full_home_rolling_arm_v1",
            program_id="step5d_strict_rnn_autotune_v3_r999",
            manifest_sha256=basis["release_manifest_sha256"],
            generated_files={live.LAUNCH_PROFILE_PATH: "a" * 64},
        ),
    )
    monkeypatch.setattr(
        live,
        "load_delivery_observation",
        lambda *_args, **_kwargs: {"transaction_id": "f" * 32},
    )
    monkeypatch.setattr(live, "_validate_coordinator_runtime_root", lambda *_args, **_kwargs: runtime_root)
    monkeypatch.setattr(live, "_create_bridge_runtime", lambda *_args: (bridge_run, bridge_runtime))
    monkeypatch.setattr(live, "_run_bridge_command_and_wait_for_readiness", bridge_command)
    monkeypatch.setattr(live, "validate_strict_bridge_ready", validate_ready)
    monkeypatch.setattr(live, "_refresh_live_bridge_admission", refresh_admission)
    monkeypatch.setattr(live, "_wait_file", fake_wait_file)
    monkeypatch.setattr(live, "release_payload_path", lambda *_args, **_kwargs: tmp_path / "payload.json")
    monkeypatch.setattr(live, "load_contract", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        live,
        "load_launch_profile",
        lambda *_args, **_kwargs: SimpleNamespace(fingerprint="a" * 64),
    )
    monkeypatch.setattr(
        live,
        "check_effective_config",
        lambda **_kwargs: {"effective_config": {"robot_host": "192.0.2.1"}},
    )
    monkeypatch.setattr(
        live,
        "release_runtime_contract",
        lambda *_args, **_kwargs: {
            "expected_loaded_program": "step5d_strict_rnn_autotune_v3_r999",
            "safety_envelope_sha256": "a" * 64,
        },
    )
    monkeypatch.setattr(
        live,
        "build_bridge_argv",
        lambda *_args, **_kwargs: ["python", str(live.WRAPPER), "bridge"],
    )
    monkeypatch.setattr(
        live,
        "overlay_fingerprint",
        lambda *_args, **_kwargs: "a" * 64,
    )
    monkeypatch.setattr(live, "_validate_preflight", lambda *_args, **_kwargs: {"controller_identity_sha256": "a" * 64})
    monkeypatch.setattr(
        live,
        "RuntimeObservationPublisher",
        SimpleNamespace(start=lambda **_kwargs: _DummyPublisher()),
    )
    monkeypatch.setattr(
        live,
        "production_runtime_environment",
        lambda *_args, **_kwargs: {
            "PATH": "/bin",
            "PYTHONPATH": "",
            "HOME": "/tmp",
        },
    )
    monkeypatch.setattr(live, "_release_contract_evidence_reference", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(live, "load_current_release_snapshot", lambda _root: SimpleNamespace(valid=True))
    monkeypatch.setattr(live, "_publish_runtime_observation", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("runner observation stop")))
    monkeypatch.setattr(live, "_revoke_campaign_authority", lambda *_args, **_kwargs: ["ok"])
    monkeypatch.setattr(live, "read_strict_json", fake_read_json)
    monkeypatch.setattr(live.subprocess, "Popen", runner_process)
    monkeypatch.setattr(live, "process_starttime", lambda _pid: 1)

    with pytest.raises(RuntimeError, match="runner observation stop"):
        live._run_live_session(
            args,
            {
                "profiles": {
                    "control": {
                        "root": str(control_runtime_root),
                        "python_executable": sys.executable,
                        "environment_id": "control-env-id",
                    },
                },
                "attestation_sha256": "attestation-id",
                "bundle_id": "bundle-id",
            },
        )

    assert order == [
        "bridge_launch",
        "bridge_ready",
        "fresh_admission",
        "runner_spawn",
    ]
    runner_command = command_seen["runner"]
    assert runner_command is not None
    assert "--admission" in runner_command
    admission_idx = runner_command.index("--admission") + 1
    assert runner_command[admission_idx] == str(indexed_admission)


def test_run_recoverable_live_session_retries_once_for_recoverable_session_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    args = SimpleNamespace(
        output_root=tmp_path / "output",
        preflight=tmp_path / "preflight.json",
        delivery_observation=tmp_path / "delivery.json",
        admission=tmp_path / "admission.json",
        authority_epoch=7,
        launch_basis=tmp_path / "basis.json",
        launch_basis_sha256="a" * 64,
        campaign_prepare=tmp_path / "campaign-prepare.json",
        campaign_root=tmp_path / "campaign",
        canonical_owner_pid=123,
        canonical_owner_starttime=456,
    )
    attempts: list[int] = []

    def recoverable_then_success(*_args: Any, **_kwargs: Any) -> dict[str, bool]:
        attempts.append(1)
        attempt_args = _args[0]
        attempt_no = int(getattr(attempt_args, "session_attempt", 0)) + 1
        attempt_args.session_attempt = attempt_no
        attempt_args.output_root = attempt_args.output_root / f"attempt-{attempt_no:04d}"
        attempt_args.bridge_launch_attempt = int(getattr(attempt_args, "bridge_launch_attempt", 0)) + 1
        Path(attempt_args.output_root).mkdir(parents=True, exist_ok=False, mode=0o700)
        if len(attempts) == 1:
            raise live.RecoverableLiveSessionAttemptError(
                RuntimeError("temporary hardware fault")
            )
        return {"ok": True}

    sleeps: list[float] = []
    monkeypatch.setattr(live, "_run_live_session", recoverable_then_success)
    args.retry_budget = 1
    monkeypatch.setattr(live.time, "sleep", sleeps.append)

    result = live._run_live(
        args,
        {"profiles": {"control": {"python_executable": sys.executable}}},
    )

    assert result["ok"] is True
    assert len(attempts) == 2
    assert sleeps == [live.RECOVERY_BACKOFF_S]
    final_status = live.read_strict_json(
        args.output_root / "recoverable_session_status.json",
        role="recoverable session status",
    )
    assert final_status["state"] == "COMPLETED"
    assert final_status["attempt"] == 2
    assert final_status["orchestration_cycle"] == 2
    assert final_status["bridge_launch_attempt"] == 1
    assert final_status["session_attempt"] == 2
    assert final_status["attempt_root"] == str(args.output_root / "attempt-0002")
    assert (args.output_root / "attempt-0001").exists()
    assert (args.output_root / "attempt-0002").exists()


def test_run_recoverable_live_session_default_retry_budget_does_not_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    args = SimpleNamespace(
        output_root=tmp_path / "output",
        preflight=tmp_path / "preflight.json",
        delivery_observation=tmp_path / "delivery.json",
        admission=tmp_path / "admission.json",
        authority_epoch=7,
        launch_basis=tmp_path / "basis.json",
        launch_basis_sha256="a" * 64,
        campaign_prepare=tmp_path / "campaign-prepare.json",
        campaign_root=tmp_path / "campaign",
        canonical_owner_pid=123,
        canonical_owner_starttime=456,
    )
    attempts: list[int] = []

    def recoverable_then_fail(*_args: Any, **_kwargs: Any) -> dict[str, bool]:
        attempts.append(1)
        raise live.RecoverableLiveSessionAttemptError(
            RuntimeError("retry-budget default zero no retry")
        )

    monkeypatch.setattr(live, "_run_live_session", recoverable_then_fail)

    with pytest.raises(RuntimeError, match="retry-budget default zero no retry"):
        live._run_live(
            args,
            {"profiles": {"control": {"python_executable": sys.executable}}},
        )

    assert attempts == [1]
    status = live.read_strict_json(
        args.output_root / "recoverable_session_status.json",
        role="recoverable session status",
    )
    assert status["state"] == "RECOVERING"
    assert status["attempt"] == 0
    assert status["orchestration_cycle"] == 1
    assert status["bridge_launch_attempt"] == 0
    assert status["session_attempt"] == 0


def test_parameter_receiver_binds_observed_home_before_first_dispatch() -> None:
    source = (ROOT / "tools/run_step5d_parameter_campaign.py").read_text(
        encoding="utf-8"
    )
    run = source.index("def run(args:")
    wait_home = source.index("_wait_initial_home(args, follower)", run)
    bind_home = source.index("bind_home(", wait_home)
    dispatch = source.index("_wait_next_dispatch(", bind_home)
    arm = source.index("_send(args, binding=binding, dispatch=dispatch)", dispatch)

    assert wait_home < bind_home < dispatch < arm


def test_fake_bridge_cannot_replace_the_production_bridge() -> None:
    fake_bridge = ROOT / "tests/step5d_v3_production_chain_fake_bridge.py"
    production_source = Path(live.__file__).read_text(encoding="utf-8")

    assert str(fake_bridge) not in production_source
    assert live.WRAPPER.name == "run_step5d_autotune_v3_bridge.py"
    assert fake_bridge.name not in production_source


def test_trial_bundle_discovery_rejects_symlink_evidence(tmp_path: Path) -> None:
    campaign = tmp_path / "campaign"
    good = campaign / "trials/good/immutable_trial_bundle.json"
    good.parent.mkdir(parents=True)
    good.write_text("{}\n", encoding="utf-8")
    assert live._immutable_trial_bundles(campaign) == {good.resolve()}

    source = tmp_path / "outside.json"
    source.write_text("{}\n", encoding="utf-8")
    unsafe = campaign / "trials/unsafe/immutable_trial_bundle.json"
    unsafe.parent.mkdir(parents=True)
    unsafe.symlink_to(source)
    with pytest.raises(live.LiveLaunchError, match="real file"):
        live._immutable_trial_bundles(campaign)


def test_dashboard_outage_has_named_machine_evidence(tmp_path: Path) -> None:
    blocker = live._dashboard_external_blocker(
        tmp_path / "campaign",
        ConnectionRefusedError("dashboard refused connection"),
    )

    assert blocker["reason_code"] == "DASHBOARD_UNREACHABLE"
    payload = json.loads(Path(blocker["evidence"]).read_text(encoding="utf-8"))
    assert payload["reason_code"] == "DASHBOARD_UNREACHABLE"
    assert payload["error_type"] == "ConnectionRefusedError"


def test_lifecycle_ack_uses_production_tp_states_not_legacy_state_two(
    tmp_path: Path,
) -> None:
    class Publisher:
        def __init__(self) -> None:
            self.events = {
                "first_arm_ack": None,
                "trial_completion": None,
                "next_arm_ack": None,
            }

        def update_lifecycle(self, event: str, **details: Any) -> None:
            self.events[event] = details

    publisher = Publisher()
    command = SimpleNamespace(
        packet=SimpleNamespace(
            command=SimpleNamespace(name="ARM"),
            command_seq=7,
        )
    )
    row = {
        "ur_output_int_register_26": str(int(live.TpLoopState.ARMED)),
        "ur_output_int_register_30": "7",
    }
    live._update_observed_lifecycle(
        publisher,
        row=row,
        command=command,
        campaign_root=tmp_path,
        preexisting_bundles=set(),
    )
    assert publisher.events["first_arm_ack"]["sequence"] == 7

    publisher.events["trial_completion"] = {"trial_id": "trial-1"}
    command.packet.command_seq = 8
    row["ur_output_int_register_26"] = str(int(live.TpLoopState.RUN))
    row["ur_output_int_register_30"] = "8"
    live._update_observed_lifecycle(
        publisher,
        row=row,
        command=command,
        campaign_root=tmp_path,
        preexisting_bundles=set(),
    )
    assert publisher.events["next_arm_ack"]["sequence"] == 8

    rejected = Publisher()
    command.packet.command_seq = 9
    row["ur_output_int_register_26"] = "2"
    row["ur_output_int_register_30"] = "9"
    live._update_observed_lifecycle(
        rejected,
        row=row,
        command=command,
        campaign_root=tmp_path,
        preexisting_bundles=set(),
    )
    assert rejected.events["first_arm_ack"] is None


def test_fault_after_play_has_one_immediate_operator_action(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        live,
        "dashboard_exchange",
        lambda *_args, **_kwargs: {"programState": "PLAYING program.urp"},
    )

    assert live._announce_stop_if_playing("robot") is True
    assert capsys.readouterr().out.strip() == "ACTION_REQUIRED_PRESS_TP_STOP_NOW"


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def sleep(self, duration: float) -> None:
        self.value += duration


def test_cleanup_never_sends_dashboard_stop() -> None:
    replies: list[dict[str, Any]] = [
        {"programState": "PLAYING step5d_strict_rnn_autotune_v3.urp"},
        {"programState": "STOPPED step5d_strict_rnn_autotune_v3.urp"},
    ]
    commands: list[list[str]] = []

    def exchange(_host, requested, **_kwargs):
        commands.append(requested)
        return replies.pop(0)

    result = live._stop_v3_program(
        "robot", exchange=exchange
    )
    assert result["ok"] is True
    assert result["method"] == "observed_stopped_after_operator_stop"
    assert commands == [["programState"], ["programState"]]
    assert result["stop_request"] is None


def test_operator_tp_stop_is_observed_read_only() -> None:
    replies: list[dict[str, Any]] = [
        {"programState": "PLAYING step5d_strict_rnn_autotune_v3.urp"},
        {"programState": "STOPPED step5d_strict_rnn_autotune_v3.urp"},
    ]
    clock = _Clock()
    result = live._stop_v3_program(
        "robot",
        exchange=lambda *_args, **_kwargs: replies.pop(0),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    assert result["ok"] is True
    assert result["method"] == "observed_stopped_after_operator_stop"


def test_persistent_playing_requires_one_explicit_tp_stop_action() -> None:
    clock = _Clock()

    def exchange(_host, commands, **_kwargs):
        assert commands == ["programState"]
        return {"programState": "PLAYING step5d_strict_rnn_autotune_v3.urp"}

    result = live._stop_v3_program(
        "robot",
        timeout_s=0.3,
        poll_interval_s=0.1,
        exchange=exchange,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    assert result["ok"] is False
    assert result["required_operator_action"] == "PRESS_TP_STOP"
    assert result["stop_request"] is None


def test_bridge_csv_follower_reads_only_appended_complete_rows(tmp_path: Path) -> None:
    bridge_csv = tmp_path / "bridge.csv"
    bridge_csv.write_bytes(b"state,sequence\n10,1\n11")
    follower = live._LatestCsvFollower(bridge_csv)

    assert follower.poll() == {"state": "10", "sequence": "1"}
    initial_bytes = bridge_csv.stat().st_size
    assert follower.bytes_read == initial_bytes
    assert follower.poll() == {"state": "10", "sequence": "1"}
    assert follower.bytes_read == initial_bytes

    with bridge_csv.open("ab") as handle:
        handle.write(b",2\n")
    assert follower.poll() == {"state": "11", "sequence": "2"}
    assert follower.bytes_read == initial_bytes + 3


def test_live_owner_has_no_optimizer_or_batch_producer_dependency() -> None:
    source = (ROOT / "tools/run_step5d_autotune_v3_live.py").read_text(
        encoding="utf-8"
    )
    assert "ExactOptimizerClient" not in source
    assert "RollingBatchProducer" not in source
    assert "_AsyncProducerPoller" not in source
    assert 'profile="optimizer"' not in source


def test_play_identity_recheck_uses_actual_gate_observation() -> None:
    triplet = {
        ".script": "1" * 64,
        ".txt": "2" * 64,
        ".urp": "3" * 64,
    }
    observed = live._play_identity_recheck_observation(
        {
            "arm_permitted": True,
            "controller": {
                "loaded_program": "Loaded program: /programs/ACTUAL_R009.urp"
            },
            "rtde": {
                "protocol_version": 91,
                "digest_hi": 92,
                "digest_lo": 93,
            },
        },
        {"triplet_sha256": triplet},
    )

    assert observed == {
        "readback_triplet_sha256": triplet,
        "loaded_program": "/programs/actual_r009.urp",
        "tp_runtime_identity": {
            "protocol_version": 91,
            "digest_hi": 92,
            "digest_lo": 93,
        },
    }
    with pytest.raises(live.LiveLaunchError, match="unique loaded program"):
        live._play_identity_recheck_observation(
            {
                "arm_permitted": True,
                "controller": {
                    "loaded_program": "/programs/a.urp /programs/b.urp"
                },
                "rtde": {
                    "protocol_version": 1,
                    "digest_hi": 2,
                    "digest_lo": 3,
                },
            },
            {"triplet_sha256": triplet},
        )


def test_campaign_authority_revoke_updates_lease_and_gate(monkeypatch, tmp_path: Path) -> None:
    publisher_calls: list[dict[str, Any]] = []
    gate_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    class Publisher:
        def revoke_lease(self, **kwargs: Any) -> None:
            publisher_calls.append(kwargs)

    def revoke_gate(*args: Any, **kwargs: Any) -> None:
        gate_calls.append((args, kwargs))

    monkeypatch.setattr(live, "revoke_arm_observation", revoke_gate)
    lease = object()
    gate = tmp_path / "arm_gate.json"

    errors = live._revoke_campaign_authority(
        gate,
        lease=lease,
        lease_sha256="a" * 64,
        publisher=Publisher(),
        reason="campaign_terminal",
    )

    assert errors == []
    assert publisher_calls[0]["reason"] == "campaign_terminal"
    assert isinstance(publisher_calls[0]["observed_at_unix_ns"], int)
    assert gate_calls == [
        (
            (gate,),
            {
                "lease": lease,
                "lease_sha256": "a" * 64,
                "reason_code": "campaign_terminal",
            },
        )
    ]


def test_campaign_authority_revoke_reports_both_failures(monkeypatch, tmp_path: Path) -> None:
    class Publisher:
        def revoke_lease(self, **_kwargs: Any) -> None:
            raise RuntimeError("publisher failed")

    def fail_gate(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("gate failed")

    monkeypatch.setattr(live, "revoke_arm_observation", fail_gate)
    errors = live._revoke_campaign_authority(
        tmp_path / "arm_gate.json",
        lease=object(),
        lease_sha256="a" * 64,
        publisher=Publisher(),
        reason="supervisor_exit",
    )

    assert errors == [
        "observation_lease:RuntimeError:publisher failed",
        "arm_gate:RuntimeError:gate failed",
    ]


def test_post_play_loop_never_runs_an_optimizer_or_producer() -> None:
    source = inspect.getsource(live._run_live_session)
    post_play = source[source.index("V3_CAMPAIGN_RUNNING_ONE_PLAY_CONTINUOUS") :]

    assert "producer.poll_once(" not in post_play
    assert "producer_poller" not in post_play
    assert "ExactOptimizerClient" not in post_play
    assert 'profile="optimizer"' not in post_play
