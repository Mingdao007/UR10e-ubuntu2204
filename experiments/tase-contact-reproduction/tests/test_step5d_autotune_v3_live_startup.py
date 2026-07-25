from __future__ import annotations

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
from typing import Any

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
from ur10e_parallel import (  # noqa: E402
    ResourceProfile,
    writer_lease,
    writer_lease_owner,
)


def test_arm_gate_refresh_cadence_has_watchdog_margin() -> None:
    assert live.ARM_GATE_REFRESH_INTERVAL_S <= live.ARM_GRANT_MAX_AGE_S * 0.5


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

    assert live.run(SimpleNamespace(_runtime_pointer=pointer)) == {
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
    assert "prealign_start_clearance" in observed["predicates"]

    del payload["predicates"]["prealign_start_clearance"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(live.LiveLaunchError, match="predicates are incomplete"):
        live._validate_preflight(path, release)


def test_runner_is_observable_but_first_arm_waits_for_post_play_gate() -> None:
    source = inspect.getsource(live._run_live)
    writer_lease_acquired = source.index("writer_guard.__enter__()")
    bridge_start = source.index("bridge = subprocess.Popen(")
    runner_start = source.index("runner = subprocess.Popen(")
    runner_ready = source.index(
        '_wait_file(runner_ready, runner, args.ready_timeout_s, "campaign runner")'
    )
    bridge_ready = source.index(
        '_wait_file(bridge_run / "bridge_ready.json", bridge, args.ready_timeout_s, "bridge")'
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
    assert '"--initial-manifest"' in source
    assert "while True:" in source[source.index("READY_FOR_ONE_PLAY_TO_MOVE") :]
    assert "TP Play was not observed before timeout" not in source
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
    assert "--revalidate-current" in source


def test_shell_defaults_bind_current_release_and_durable_parameter_campaign() -> None:
    source = (ROOT / "scripts/step5d-autotune-v3.sh").read_text(encoding="utf-8")

    assert 'campaign_root=""' in source
    assert "parameter-campaign" in source
    assert 'campaign_root="${EXPERIMENT_ROOT}/runs/step5d_autotune_v3"' not in source
    revalidate = source[source.index('if [[ "${1:-}" == "revalidate-current"') :]
    assert 'artifact_dir=""' in revalidate
    assert 'revalidate_command+=(--artifact-dir "${artifact_dir}")' in revalidate


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
    assert source.count(exact_live) == 2
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
        "  if [[ \"${STEP5D_TEST_ADMISSION_RC:-0}\" == '75' ]]; then\n"
        "    printf '%s\\n' '{\"ok\":false,\"state\":\"ACTION_REQUIRED\",\"reason_code\":\"EXTERNAL_ACTION_REQUIRED\"}' >\"${admission_output}\"\n"
        "    exit 75\n"
        "  fi\n"
        "  printf '%s\\n' '{\"ok\":true,\"state\":\"BENCH_READY\",\"delivery_observation\":{\"path\":\"scripts/step5d-autotune-v3.sh\"}}' >\"${admission_output}\"\n"
        "  printf '{}\\n'\n"
        "  exit 0\n"
        "fi\n"
        "if [[ \"${1:-}\" == '-c' ]]; then printf '%032d\\n' 0; exit 0; fi\n"
        + (
            "if [[ \" $* \" == *' --prepare-only '* ]]; then exit 41; fi\n"
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
        "gpu = 'GPU-93d64fd3-924c-9c86-6c3d-b4781ed2133a'\n"
        "ld_library_path = '/runtime/control/nvidia'\n"
        "cupy_cache_dir = '/runtime/cache/cupy'\n"
        "print('\\t'.join((python, digest, digest, digest, digest, digest, gpu, ld_library_path, cupy_cache_dir)))\n",
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "PATH": "/usr/bin:/bin",
        "STEP5D_TEST_COMMAND_LOG": str(command_log),
        "STEP5D_TEST_PROFILE_PYTHON": str(profile_python),
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
    started = next(
        line
        for line in commands
        if "_launch-attempt-state STARTED" in line
        and "_launch-attempt-phase campaign_prepare" in line
    )
    failed = next(
        line
        for line in commands
        if "_launch-attempt-state FAILED" in line
        and "_launch-attempt-phase campaign_prepare" in line
    )
    assert "_launch-attempt-phase campaign_prepare" in started
    assert "_launch-attempt-phase campaign_prepare" in failed
    assert "_launch-attempt-exit-code 41" in failed
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
    assert len(live_calls) == 2
    assert "--preflight" in live_calls[-1]
    assert "step5d_autotune_v3.cli" not in live_calls[-1]


def test_shell_action_required_creates_no_attempt_or_authority(
    tmp_path: Path,
) -> None:
    shell, command_log, environment = _fake_governed_shell(
        tmp_path,
        fail_prepare=False,
    )
    environment["STEP5D_TEST_ADMISSION_RC"] = "75"
    experiment = shell.parent.parent
    output = experiment / "runs/action-required"
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
        cwd=experiment,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10.0,
        check=False,
    )

    assert result.returncode == 75, result.stderr
    assert "EXTERNAL_ACTION_REQUIRED" in result.stderr
    commands = command_log.read_text(encoding="utf-8").splitlines()
    assert any("check_step5d_autotune_v3_bridge_admission.py" in line for line in commands)
    assert not any("run_step5d_release_contract.py" in line for line in commands)
    assert not any("run_step5d_autotune_v3_tp_transaction.py" in line for line in commands)
    authority_root = experiment / "runs/step5d_bridge_authority"
    assert not authority_root.exists()
    assert not (output / "bridge-authority-epoch.txt").exists()
    assert not (output / "launch-attempt-recorder.log").exists()


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
    assert not any("check_step5d_autotune_v3_bridge_admission.py" in line for line in commands)
    assert not (experiment / "runs/step5d_bridge_authority").exists()


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
    source = inspect.getsource(live._run_live)
    post_play = source[source.index("V3_CAMPAIGN_RUNNING_ONE_PLAY_CONTINUOUS") :]

    assert "producer.poll_once(" not in post_play
    assert "producer_poller" not in post_play
    assert "ExactOptimizerClient" not in post_play
    assert 'profile="optimizer"' not in post_play
