from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1] / "src" / "ur10e_experiment_runtime"))
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_contract import ForceCandidate  # noqa: E402
from step5d_parameter_feeder import (  # noqa: E402
    CONFIG_SCHEMA,
    FeederConfig,
    ParameterFeeder,
)
from step5d_parameter_outbox import RESULT_SCHEMA  # noqa: E402
from step5d_parameter_bo import load_observations  # noqa: E402
from step5d_parameter_queue import (  # noqa: E402
    authoritative_view,
    bind_home,
    finish_dispatch,
    initialize,
    prepare_next_dispatch,
    submit,
    submit_manifest,
)


PROFILE = ROOT / "config/step5/step5d_autotune_v3_launch_profile.json"


def _catalog(count: int = 24) -> tuple[ForceCandidate, ...]:
    rows = []
    for index in range(count):
        rows.append(
            ForceCandidate.from_log2(
                p=((index % 7) - 3) * 0.25,
                damping=((index // 7) - 1) * 0.25,
                i=0.0,
                i_off=True,
                filter_tau=((index % 5) - 2) * 0.25,
            )
        )
    return tuple(dict((row.candidate_uid, row) for row in rows).values())


def _queue(tmp_path: Path) -> Path:
    root = tmp_path / "receiver"
    initialize(
        root,
        campaign_id="campaign-test",
        release_manifest_sha256="a" * 64,
        launch_profile_path=PROFILE,
    )
    return root


def _config(tmp_path: Path, queue_root: Path) -> FeederConfig:
    outbox = tmp_path / "outbox"
    (outbox / "tasks").mkdir(parents=True)
    (outbox / "results").mkdir()
    return FeederConfig(
        queue_root=queue_root,
        outbox_root=outbox,
        capture_root=tmp_path / "captures",
        campaign_config_path=tmp_path / "campaign.json",
        control_contract_path=tmp_path / "contract.json",
        launch_profile_path=PROFILE,
        state_root=tmp_path / "feeder-state",
        interval_s=0.001,
        target_depth=8,
        low_watermark=4,
    )


def _degraded_optimizer(*_args, **_kwargs):
    raise RuntimeError("CUDA unavailable in focused test")


def _feeder(
    tmp_path: Path,
    queue_root: Path,
    *,
    process_task=None,
    optimizer=_degraded_optimizer,
    catalog=None,
    submitter=None,
) -> ParameterFeeder:
    return ParameterFeeder(
        _config(tmp_path, queue_root),
        process_task=process_task,
        optimizer=optimizer,
        submitter=submitter,
        catalog_provider=lambda: catalog or _catalog(),
    )


def _write_result(outbox: Path, index: int, candidate: ForceCandidate) -> Path:
    path = outbox / "results" / f"result-{index:03d}.json"
    payload = {
        "schema": RESULT_SCHEMA,
        "status": "SUCCEEDED",
        "dispatch_sequence": index + 1,
        "trial_uid": f"{index + 1:064x}",
        "identity": {"backend_id": "offline-test"},
        "candidate": {
            **candidate.payload(),
            "orientation_ko": 0.4,
            "force_p_gain": candidate.force_p_gain,
            "force_i_gain": candidate.force_i_gain,
            "force_damping": candidate.force_damping,
            "normal_filter_tau_s": candidate.normal_filter_tau_s,
        },
        "objective": {
            "complete_bins": 550,
            "required_bins": 550,
            "mae_n": 0.5 + index / 100.0,
            "bias_n": 0.0,
            "std_n": 0.1,
            "coverage_12_plus_minus_1_ratio": 1.0,
        },
        "profile": {"orientation_qualified": True},
    }
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _write_task(outbox: Path, name: str) -> Path:
    path = outbox / "tasks" / name
    path.write_text(json.dumps({"schema": "sealed-task"}) + "\n", encoding="utf-8")
    return path


def _terminal_observed(dispatch: dict) -> dict:
    packet = dispatch["packet"]
    return {
        "campaign_epoch": packet["campaign_epoch"],
        "trial_id": packet["trial_id"],
        "state": 78,
        "candidate_token": packet["candidate_token"],
        "execution_profile_id": packet["execution_profile_id"],
        "consumed_command_seq": packet["command_seq"],
        "logical_batch_sequence": packet["logical_batch_sequence"],
        "batch_row_index": 1,
        "terminal_reason": 1,
    }


def test_initial_prefill_reaches_target_depth(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    feeder = _feeder(tmp_path, queue)
    receipt = feeder.cycle()
    assert receipt["before_depth"] == 0
    assert receipt["after_depth"] == 8
    assert receipt["fallback_reason"]
    assert len(authoritative_view(queue)["pending_requests"]) == 8


def test_dry_run_with_full_queue_reports_success_without_submission(
    tmp_path: Path,
) -> None:
    queue = _queue(tmp_path)
    feeder = _feeder(tmp_path, queue)
    feeder.cycle()

    receipt = feeder.cycle(dry_run=True)

    assert receipt["status"] == "SUCCEEDED"
    assert receipt["dry_run"] is True
    assert receipt["before_depth"] == 8
    assert receipt["after_depth"] == 8
    assert receipt["submitted_request_uids"] == []
    assert receipt["errors"] == []


def test_limiter_saturation_diagnostic_does_not_delete_mae_observation(
    tmp_path: Path,
) -> None:
    queue = _queue(tmp_path)
    feeder = _feeder(tmp_path, queue)
    candidate = _catalog()[0]
    result_path = _write_result(feeder.config.outbox_root, 0, candidate)
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    payload["profile"] = {
        "orientation_qualified": False,
        "orientation_error_p95_rad": 0.02,
        "orientation_error_max_rad": 0.03,
        "angular_saturation_duty": 0.25,
    }
    result_path.write_text(
        json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"
    )

    observations, candidate_uids, material = load_observations(
        feeder.config.outbox_root
    )

    assert len(observations) == 1
    assert candidate.candidate_uid in candidate_uids
    assert material[0]["eligible"] is True
    assert material[0]["exclusion_reasons"] == []


def test_low_watermark_refill_and_pending_only_depth(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    feeder = _feeder(tmp_path, queue)
    feeder.cycle()
    bind_home(queue, campaign_epoch=1, last_trial_id=0, last_command_seq=0)
    dispatch = prepare_next_dispatch(queue)
    assert dispatch is not None
    view = authoritative_view(queue)
    assert len(view["pending_requests"]) == 7
    assert view["state"]["inflight"] is not None
    assert view["depth"] == 7  # only pending rows are valid backup capacity
    stale = queue / "parameter_receiver_status.json"
    stale.write_text(json.dumps({"pending_count": 0, "revision": 0}) + "\n")
    receipt = feeder.cycle()
    assert receipt["before_depth"] == 7
    assert receipt["after_depth"] == 7
    assert receipt["proposal_mode"] == "not_needed_above_low_watermark"

    finish_dispatch(queue, status="SUCCEEDED", observed=_terminal_observed(dispatch))
    for _ in range(3):
        next_dispatch = prepare_next_dispatch(queue)
        assert next_dispatch is not None
        finish_dispatch(
            queue,
            status="SUCCEEDED",
            observed=_terminal_observed(next_dispatch),
        )
    assert authoritative_view(queue)["depth"] == 4
    refill = feeder.cycle()
    assert refill["before_depth"] == 4
    assert refill["after_depth"] == 8


def test_restart_resubmits_immutable_intent_without_duplicate_revision(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    config = _config(tmp_path, queue)

    def submit_then_fail(*args, **kwargs):
        submit_manifest(*args, **kwargs)
        raise RuntimeError("crash after atomic queue commit")

    first = ParameterFeeder(
        config,
        optimizer=_degraded_optimizer,
        submitter=submit_then_fail,
        catalog_provider=lambda: _catalog(),
    )
    first_receipt = first.cycle()
    assert first_receipt["errors"]
    assert json.loads((config.state_root / "state.json").read_text())["pending_intent"]
    assert authoritative_view(queue)["state"]["revision"] == 8

    second = ParameterFeeder(
        config,
        optimizer=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("restart must replay intent, not optimize")
        ),
        catalog_provider=lambda: _catalog(),
    )
    receipt = second.cycle()
    assert receipt["proposal_mode"] == "restart_idempotent_resubmit"
    assert authoritative_view(queue)["state"]["revision"] == 8
    assert json.loads((config.state_root / "state.json").read_text())["pending_intent"] is None


def test_skips_existing_result_without_reprocessing(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    feeder = _feeder(tmp_path, queue, process_task=lambda *_args, **_kwargs: pytest.fail("reprocessed"))
    outbox = feeder.config.outbox_root
    task = _write_task(outbox, "existing.json")
    (outbox / "results" / task.name).write_text(
        json.dumps({"schema": RESULT_SCHEMA, "status": "SUCCEEDED"}) + "\n",
        encoding="utf-8",
    )
    feeder.cycle()
    state = json.loads((feeder.state_root / "state.json").read_text(encoding="utf-8"))
    assert state["processed_tasks"][task.name]["status"] == "SKIPPED_EXISTING_RESULT"


def test_one_failing_task_does_not_block_other_sealed_tasks(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    calls: list[str] = []
    feeder = _feeder(tmp_path, queue)
    bad = _write_task(feeder.config.outbox_root, "bad.json")
    good = _write_task(feeder.config.outbox_root, "good.json")

    def process(task_path: Path, **_kwargs):
        calls.append(task_path.name)
        if task_path.name == bad.name:
            raise RuntimeError("one task unavailable")
        result = feeder.config.outbox_root / "results" / task_path.name
        result.write_text(
            json.dumps({"schema": RESULT_SCHEMA, "status": "SUCCEEDED"}) + "\n",
            encoding="utf-8",
        )
        return result

    feeder.process_task = process
    receipt = feeder.cycle()
    assert calls == ["bad.json", "good.json"]
    assert any(row["task"] == bad.name for row in receipt["failed_tasks"])
    state = json.loads((feeder.state_root / "state.json").read_text(encoding="utf-8"))
    assert state["processed_tasks"][good.name]["status"] == "PROCESSED"
    assert bad.name not in state["processed_tasks"]


def test_part_result_never_enters_bo(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    feeder = _feeder(tmp_path, queue)
    partial = feeder.config.outbox_root / "results" / "trial-001.part.json"
    partial.write_text(
        json.dumps({"schema": RESULT_SCHEMA, "status": "SUCCEEDED"}) + "\n",
        encoding="utf-8",
    )
    observations, candidate_uids, material = load_observations(feeder.config.outbox_root)
    assert observations == ()
    assert candidate_uids == frozenset()
    assert material == ()


def test_cuda_failure_uses_unique_validated_catalog_candidates(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    feeder = _feeder(tmp_path, queue, catalog=_catalog(30))
    candidates = _catalog(6)
    for index, candidate in enumerate(candidates):
        _write_result(feeder.config.outbox_root, index, candidate)
    receipt = feeder.cycle()
    assert receipt["fallback_reason"].startswith("optimizer_unavailable:")
    requests = authoritative_view(queue)["requests"]
    assert len(requests) == 8
    assert len({row["overlay"]["control_candidate_uid"] for row in requests}) == 8
    assert len({row["occurrence_nonce"] for row in requests}) == 8
    assert all("degraded_bootstrap" in row["source"] for row in requests)


def test_feeder_fails_closed_if_catalog_contains_damping_below_floor(
    tmp_path: Path,
) -> None:
    queue = _queue(tmp_path)
    unsafe = ForceCandidate.from_log2(
        p=0.0,
        damping=-0.5,
        i=0.0,
    )
    feeder = _feeder(tmp_path, queue, catalog=(unsafe, *_catalog(30)))
    receipt = feeder.cycle()
    assert receipt["errors"]
    assert "below the 5 search floor" in receipt["errors"][0]["error"]
    assert authoritative_view(queue)["requests"] == ()


def test_atomic_batch_submission_commits_one_revision_and_all_rows(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    rows = [
        {
            "force_p_gain": candidate.force_p_gain,
            "force_i_gain": candidate.force_i_gain,
            "force_damping": candidate.force_damping,
            "normal_filter_tau_s": candidate.normal_filter_tau_s,
            "orientation_ko": 0.4,
            "source": "atomic-test",
            "occurrence_nonce": f"{index + 1:032x}",
        }
        for index, candidate in enumerate(_catalog(8))
    ]
    submitted = submit_manifest(queue, launch_profile_path=PROFILE, rows=rows)
    assert len(submitted) == 8
    view = authoritative_view(queue)
    assert view["state"]["revision"] == 8
    assert [row["enqueue_sequence"] for row in view["requests"]] == list(range(1, 9))


def test_concurrent_finish_and_submit_preserve_revisions(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    submit(queue, launch_profile_path=PROFILE, force_p=0.001, force_i=0.00001, force_damping=7.0)
    bind_home(queue, campaign_epoch=1, last_trial_id=0, last_command_seq=0)
    dispatch = prepare_next_dispatch(queue)
    assert dispatch is not None
    rows = [
        {
            "force_p_gain": candidate.force_p_gain,
            "force_i_gain": candidate.force_i_gain,
            "force_damping": candidate.force_damping,
            "normal_filter_tau_s": candidate.normal_filter_tau_s,
            "orientation_ko": 0.4,
            "source": "concurrent-submit",
            "occurrence_nonce": f"{index + 1:032x}",
        }
        for index, candidate in enumerate(_catalog(6))
    ]
    barrier = threading.Barrier(2)

    def finish():
        barrier.wait()
        return finish_dispatch(queue, status="SUCCEEDED", observed=_terminal_observed(dispatch))

    def enqueue():
        barrier.wait()
        return submit_manifest(queue, launch_profile_path=PROFILE, rows=rows)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(finish)
        second = pool.submit(enqueue)
        first.result()
        second.result()
    view = authoritative_view(queue)
    assert view["state"]["revision"] == 7
    assert view["state"]["inflight"] is None
    assert len(view["requests"]) == 7
    assert len(list((queue / "receipts").glob("*.json"))) == 1


def test_fake_fast_receiver_composition_never_reaches_zero(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    feeder = _feeder(tmp_path, queue, catalog=_catalog(40))
    feeder.cycle()
    bind_home(queue, campaign_epoch=1, last_trial_id=0, last_command_seq=0)
    depths: list[int] = []
    for _ in range(16):
        dispatch = prepare_next_dispatch(queue)
        assert dispatch is not None
        depths.append(authoritative_view(queue)["depth"])
        finish_dispatch(queue, status="SUCCEEDED", observed=_terminal_observed(dispatch))
        if authoritative_view(queue)["depth"] <= feeder.config.low_watermark:
            feeder.cycle()
    assert min(depths) >= 1
    assert authoritative_view(queue)["depth"] >= 1


def test_feeder_cli_help_is_offline_and_complete() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools/step5d_parameter_feeder.py"), "--help"],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                (
                    str(ROOT.parents[1] / "src" / "ur10e_experiment_runtime"),
                    str(ROOT / "tools"),
                    os.environ.get("PYTHONPATH", ""),
                )
            ),
        },
    )
    assert result.returncode == 0
    assert "--dry-run" in result.stdout
    assert "--once" in result.stdout
