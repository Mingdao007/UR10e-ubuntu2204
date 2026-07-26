from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
TOOLS = ROOT / "tools"
HARNESS = TESTS / "step5d_v3_fake_bridge_harness.py"
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TOOLS))

if (
    os.environ.get("STEP5D_V3_HERMETIC_PARSER_CI") == "1"
    and "kunwei_rtde_bridge" not in sys.modules
):
    from step5d_v3_parser_ci_stubs import install as install_parser_ci_stubs

    install_parser_ci_stubs()

from step5d_autotune_journal import SupervisorJournal  # noqa: E402
from step5d_autotune_live_driver import AtomicCommandMailbox  # noqa: E402
from step5d_autotune_state_machine import HostCommand  # noqa: E402
from step5d_autotune_store import CampaignStore  # noqa: E402
from step5d_autotune_v3.launcher import check_effective_config  # noqa: E402
from step5d_v3_fake_bridge_harness import (  # noqa: E402
    immutable_launch_profile,
)


def read_events(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def process_starttime(pid: int) -> int:
    fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
    return int(fields[21])


def wait_until(
    predicate: Callable[[], bool],
    *,
    processes: dict[str, subprocess.Popen[str]],
    timeout_s: float = 20.0,
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        exited = {
            name: returncode
            for name, process in processes.items()
            if (returncode := process.poll()) is not None
        }
        if exited:
            diagnostics = {}
            for name in exited:
                stdout, stderr = processes[name].communicate(timeout=1.0)
                diagnostics[name] = {"stdout": stdout, "stderr": stderr}
            raise AssertionError(
                f"acceptance subprocess exited early: {exited}; {diagnostics}"
            )
        time.sleep(0.005)
    raise AssertionError("timed out waiting for exact ten-row acceptance evidence")


def spawn(*arguments: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, str(HARNESS), *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        close_fds=True,
    )


def test_exact_ten_row_fake_bridge_uses_durable_batch_and_trial_brief_truth(
    tmp_path: Path,
) -> None:
    contract = check_effective_config()
    assert contract["ok"] is True
    control_fingerprint = contract["control_fingerprint"]

    gate = tmp_path / "faithful-fake-gate"
    gate.mkdir()
    snapshot = gate / "tp_snapshot.json"
    heartbeat = gate / "runner_heartbeat.json"
    start_gate = gate / "start"
    stop = gate / "stop"
    done = gate / "done.json"
    bridge_ready = gate / "bridge_ready.json"
    runner_ready = gate / "runner_ready.json"
    watchdog_ready = gate / "watchdog_ready.json"
    bridge_events_path = gate / "bridge_events.jsonl"
    runner_events_path = gate / "runner_events.jsonl"
    watchdog_events_path = gate / "watchdog_events.jsonl"

    bridge = spawn(
        "bridge",
        "--root",
        str(gate),
        "--snapshot",
        str(snapshot),
        "--events",
        str(bridge_events_path),
        "--ready",
        str(bridge_ready),
        "--stop",
        str(stop),
    )
    runner = spawn(
        "runner",
        "--root",
        str(gate),
        "--snapshot",
        str(snapshot),
        "--events",
        str(runner_events_path),
        "--heartbeat",
        str(heartbeat),
        "--ready",
        str(runner_ready),
        "--done",
        str(done),
        "--stop",
        str(stop),
        "--start-gate",
        str(start_gate),
        "--control-fingerprint",
        control_fingerprint,
    )
    watchdog = spawn(
        "watchdog",
        "--events",
        str(watchdog_events_path),
        "--heartbeat",
        str(heartbeat),
        "--ready",
        str(watchdog_ready),
        "--stop",
        str(stop),
        "--runner-pid",
        str(runner.pid),
        "--bridge-pid",
        str(bridge.pid),
    )
    processes = {"bridge": bridge, "runner": runner, "watchdog": watchdog}

    try:
        wait_until(
            lambda: all(
                path.is_file()
                for path in (bridge_ready, runner_ready, watchdog_ready)
            ),
            processes=processes,
        )
        initial_process_identity = {
            name: (process.pid, process_starttime(process.pid))
            for name, process in processes.items()
        }
        start_gate.touch()
        wait_until(done.is_file, processes=processes)

        # The processes intentionally remain alive after campaign completion so
        # PID reuse or candidate-level restarts cannot hide behind final exit.
        final_process_identity = {
            name: (process.pid, process_starttime(process.pid))
            for name, process in processes.items()
        }
        assert final_process_identity == initial_process_identity
        assert all(process.poll() is None for process in processes.values())

        bridge_events = read_events(bridge_events_path)
        runner_events = read_events(runner_events_path)
        watchdog_events = read_events(watchdog_events_path)
        assert [row["event"] for row in bridge_events].count("simulated_play") == 1
        assert [row["event"] for row in bridge_events].count("process_started") == 1
        assert [row["event"] for row in runner_events].count("process_started") == 1
        assert [row["event"] for row in watchdog_events].count("process_started") == 1
        assert {row["pid"] for row in bridge_events} == {bridge.pid}
        assert {row["pid"] for row in runner_events} == {runner.pid}
        assert {row["pid"] for row in watchdog_events} == {watchdog.pid}
        assert any(row["event"] == "heartbeat_observed" for row in watchdog_events)
        assert not any(row["event"] == "child_exit" for row in watchdog_events)
        assert not any(
            "restart" in row["event"]
            for row in bridge_events + runner_events + watchdog_events
        )
        assert not any(
            row["event"] == "operator_prompt"
            for row in bridge_events + runner_events + watchdog_events
        )

        arm_events = [row for row in runner_events if row["event"] == "arm_dispatched"]
        wait_events = [row for row in bridge_events if row["event"] == "wait_ack"]
        bundle_events = [
            row for row in runner_events if row["event"] == "bundle_evaluated"
        ]
        ack_events = [row for row in runner_events if row["event"] == "ack_dispatched"]
        ready_events = [
            row
            for row in bridge_events
            if row["event"] in {"ready_near", "ready_home_closed"}
        ]
        persisted_return_events = [
            row
            for row in runner_events
            if row["event"] == "typed_return_persisted"
        ]
        assert all(
            len(rows) == 10
            for rows in (
                arm_events,
                wait_events,
                bundle_events,
                ack_events,
                ready_events,
                persisted_return_events,
            )
        )
        assert [row["event"] for row in bridge_events] == [
            "process_started",
            "simulated_play",
            *[
                event
                for index in range(1, 11)
                for event in (
                    "wait_ack",
                    "ready_home_closed" if index == 10 else "ready_near",
                )
            ],
        ]
        assert [row["event"] for row in runner_events] == [
            "process_started",
            *[
                event
                for _ in range(10)
                for event in (
                    "arm_dispatched",
                    "wait_ack_reconciled",
                    "bundle_evaluated",
                    "ack_dispatched",
                    "typed_return_persisted",
                )
            ],
            "campaign_complete",
        ]

        trial_uids = [row["trial_uid"] for row in arm_events]
        assert len(set(trial_uids)) == 10
        assert [row["trial_uid"] for row in wait_events] == trial_uids
        assert [row["trial_uid"] for row in bundle_events] == trial_uids
        assert [row["trial_uid"] for row in ack_events] == trial_uids
        assert [row["trial_uid"] for row in ready_events] == trial_uids
        assert [row["trial_uid"] for row in persisted_return_events] == trial_uids
        assert {
            row["control_fingerprint"] for row in runner_events
        } == {control_fingerprint}

        store = CampaignStore(gate / "store")
        history = store.read_resume_history()
        assert [row["trial_uid"] for row in history] == trial_uids
        assert len({row["candidate_uid"] for row in history}) == 10
        assert all(row["evaluation"]["eligible"] is True for row in history)
        assert all(
            row["evaluation"]["disposition"] == "objective" for row in history
        )

        for index, (history_row, arm, wait, ack, ready) in enumerate(
            zip(history, arm_events, wait_events, ack_events, ready_events, strict=True),
            1,
        ):
            trial = history_row["trial"]
            wait_snapshot = wait["snapshot"]
            typed_ready_snapshot = ready["snapshot"]
            assert arm["candidate_index"] == index
            assert arm["command_seq"] == trial["command_seq"]
            assert wait["packet"]["command"] == int(HostCommand.ARM)
            assert wait["packet"]["command_seq"] == arm["command_seq"]
            assert wait_snapshot == {
                "campaign_epoch_echo": trial["campaign"]["campaign_epoch"],
                "trial_id_echo": trial["trial_id"],
                "state": "WAIT_ACK",
                "candidate_token_echo": trial["candidate_token"],
                "terminal_reason": 1,
                "execution_profile_integer_id_echo": wait["packet"][
                    "execution_profile_id"
                ],
                "consumed_command_seq": arm["command_seq"],
            }
            assert ack["command_seq"] > arm["command_seq"]
            assert ready["packet"]["command"] == int(HostCommand.ACK_BUNDLE)
            assert ready["packet"]["command_seq"] == ack["command_seq"]
            assert typed_ready_snapshot == {
                "campaign_epoch_echo": trial["campaign"]["campaign_epoch"],
                "trial_id_echo": trial["trial_id"],
                "state": "READY_HOME_CLOSED" if index == 10 else "READY_NEAR",
                "candidate_token_echo": trial["candidate_token"],
                "terminal_reason": 1,
                "execution_profile_integer_id_echo": ready["packet"][
                    "execution_profile_id"
                ],
                "consumed_command_seq": ack["command_seq"],
            }

        latest = SupervisorJournal(gate / "journal").load_latest()
        assert latest.state.phase == "home"
        assert latest.state.active_trial is None
        assert latest.state.pending_ack is None
        assert [fate.kind for fate in latest.state.terminal_fates] == [
            "ack_consumed"
        ] * 10
        assert [fate.trial.trial_uid for fate in latest.state.terminal_fates] == trial_uids
        for fate, ack in zip(latest.state.terminal_fates, ack_events, strict=True):
            assert fate.command_seq == ack["command_seq"]
            expected_state = (
                "READY_HOME_CLOSED"
                if fate.trial.trial_uid == trial_uids[-1]
                else "READY_NEAR"
            )
            assert fate.tp_snapshot.state == expected_state
            assert fate.tp_snapshot.consumed_command_seq == ack["command_seq"]
            assert (
                fate.tp_snapshot.campaign_epoch_echo,
                fate.tp_snapshot.trial_id_echo,
                fate.tp_snapshot.candidate_token_echo,
                fate.tp_snapshot.execution_profile_integer_id_echo,
                fate.tp_snapshot.terminal_reason,
            ) == (
                latest.state.campaign.campaign_epoch,
                fate.trial.trial_id,
                fate.trial.candidate_token,
                fate.trial.execution_profile_integer_id,
                1,
            )

        trial_briefs = sorted((gate / "trial_briefs").glob("*.trial-brief.json"))
        assert len(trial_briefs) == 10
        brief_documents = [
            json.loads(path.read_text(encoding="utf-8")) for path in trial_briefs
        ]
        assert {row["trial_uid"] for row in brief_documents} == set(trial_uids)
        assert all(row["publication_unique"] is True for row in brief_documents)
        assert all(row["optimizer_eligible"] is True for row in brief_documents)

        batch_roots = [
            path
            for path in (gate / "runtime_batches").iterdir()
            if path.is_dir()
        ]
        assert len(batch_roots) == 1
        batch_result = json.loads(
            (batch_roots[0] / "batch_result.json").read_text(encoding="utf-8")
        )
        assert batch_result["row_count"] == 10
        assert batch_result["exit_code"] == 0
        assert [row["fate"] for row in batch_result["rows"]] == [
            "ack_completed"
        ] * 10
        done_document = json.loads(done.read_text(encoding="utf-8"))
        assert done_document["batch_result_uid"] == batch_result["batch_result_uid"]
        assert done_document["verified_exit_code"] == 0

        final_mailbox = AtomicCommandMailbox(
            (gate / "control" / "command_mailbox.json").absolute(),
            launch_profile=immutable_launch_profile(),
        ).read_latest()
        assert final_mailbox is not None
        assert final_mailbox.packet.command is HostCommand.ACK_BUNDLE
        assert final_mailbox.packet.command_seq == ack_events[-1]["command_seq"]
        assert final_mailbox.binding.trial_uid == trial_uids[-1]

        # Derive non-motion queue overhead from independent event timestamps:
        # Durable typed return persisted for trial N -> next ARM mailbox dispatch.
        ready_by_trial = {
            row["trial_uid"]: row["monotonic_ns"]
            for row in persisted_return_events
        }
        latencies_s = [
            (arm_events[index]["monotonic_ns"] - ready_by_trial[trial_uids[index - 1]])
            / 1_000_000_000.0
            for index in range(1, 10)
        ]
        assert all(value >= 0.0 for value in latencies_s)
        rank = max(0, math.ceil(0.95 * len(latencies_s)) - 1)
        p95_s = sorted(latencies_s)[rank]
        derived_evidence = {
            "schema": "step5d.autotune-v3.fake-bridge-acceptance/v1",
            "typed_return_to_next_arm_s": latencies_s,
            "typed_return_to_next_arm_p95_s": p95_s,
            "trial_uids": trial_uids,
            "mailbox_command_sequences": {
                "arm": [row["command_seq"] for row in arm_events],
                "ack": [row["command_seq"] for row in ack_events],
            },
            "process_identity": final_process_identity,
        }
        (gate / "derived_acceptance.json").write_text(
            json.dumps(derived_evidence, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        assert p95_s <= 2.0
    finally:
        stop.touch()
        for process in processes.values():
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.terminate()
        for process in processes.values():
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2.0)

    diagnostics: dict[str, dict[str, str]] = {}
    for name, process in processes.items():
        stdout, stderr = process.communicate(timeout=1.0)
        diagnostics[name] = {"stdout": stdout, "stderr": stderr}
    assert {name: process.returncode for name, process in processes.items()} == {
        "bridge": 0,
        "runner": 0,
        "watchdog": 0,
    }, diagnostics
