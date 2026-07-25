import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ur10e_vic.tacdiffusion.queue import (
    CampaignRunner,
    DurableHookOutbox,
    CampaignLifecycle,
    CampaignState,
    EpisodeRequest,
    HomeIdentityLedger,
    PersistentRollingQueue,
    QueueDecision,
    SafeRetractPlan,
)


def item(name):
    return EpisodeRequest(name, "circle", int(name[-1]), "campaign-home")


def retract_plan(request, identity):
    return SafeRetractPlan(identity, request.task_ready_home, (0.0, 0.0, 1.0), (0.0, 0.0, -1.0), 0.01)


def retract_executor(plan):
    assert plan.retract_distance_m > 0.0
    return True


def noop_reset():
    return None


class QueueLifecycleTests(unittest.TestCase):
    def test_empty_queue_waits_without_timeout_blocked_or_exit(self):
        queue = PersistentRollingQueue(campaign_home="campaign-home")
        self.assertEqual(queue.next().decision, QueueDecision.WAITING_FOR_EPISODE)
        queue.append(item("episode-1"))
        self.assertEqual(queue.next().item.episode_id, "episode-1")
        self.assertEqual(queue.next().decision, QueueDecision.WAITING_FOR_EPISODE)

    def test_priority_append_end_drain_and_campaign_home(self):
        queue = PersistentRollingQueue(campaign_home="campaign-home")
        queue.append(item("episode-1"))
        queue.append(item("episode-2"))
        queue.priority_insert(item("priority-3"))
        self.assertEqual(queue.next().item.episode_id, "priority-3")
        queue.complete(result_identity="priority-result")
        queue.request_drain()
        self.assertEqual(queue.next().item.episode_id, "episode-1")
        queue.complete(result_identity="result-1")
        self.assertEqual(queue.next().item.episode_id, "episode-2")
        queue.complete(result_identity="result-2")
        self.assertEqual(queue.next().decision, QueueDecision.COMPLETE)
        with self.assertRaisesRegex(RuntimeError, "END or DRAIN"):
            queue.append(item("late-4"))
        lifecycle = CampaignLifecycle(queue)
        self.assertEqual(lifecycle.acquire_next().decision, QueueDecision.COMPLETE)
        self.assertEqual(lifecycle.state, CampaignState.COMPLETE)
        self.assertEqual(lifecycle.captured_campaign_home, "campaign-home")

    def test_episode_failure_continues_and_protective_stop_is_not_global_blocked(self):
        queue = PersistentRollingQueue(campaign_home="home")
        queue.append(item("episode-1"))
        queue.append(item("episode-2"))
        lifecycle = CampaignLifecycle(queue)
        lifecycle.acquire_next()
        lifecycle.episode_failed(reason="stale_model", evidence_id="evidence-1")
        self.assertEqual(lifecycle.state, CampaignState.READY)
        self.assertEqual(lifecycle.acquire_next().item.episode_id, "episode-2")
        lifecycle.protective_stop()
        self.assertEqual(lifecycle.state, CampaignState.WAITING_HARDWARE)
        lifecycle.hardware_recovered(normal=True, verified_safe_home=True, reconcile="failed", result_identity="evidence-2")
        self.assertEqual(lifecycle.state, CampaignState.READY)
        self.assertEqual(lifecycle.failures[0].episode_id, "episode-1")

    def test_end_terminalizes_undispatched_rows_and_restart_never_replays_inflight(self):
        with TemporaryDirectory() as directory:
            state = Path(directory) / "queue.json"
            queue = PersistentRollingQueue(state_path=state, campaign_home="home")
            queue.append(item("episode-1"))
            queue.append(item("episode-2"))
            dispatched = queue.next().item
            self.assertEqual(dispatched.episode_id, "episode-1")
            restarted = PersistentRollingQueue(state_path=state, campaign_home="home")
            self.assertEqual(restarted.inflight.dispatch_id, dispatched.dispatch_id)
            self.assertEqual(restarted.next().decision, QueueDecision.WAITING_FOR_EPISODE)
            restarted.reconcile_recovered_inflight(outcome="failed", result_identity="recovery-1")
            restarted.request_end()
            self.assertEqual(restarted.mode.value, "COMPLETE")
            self.assertEqual({row.episode_id for row in restarted.failed}, {"episode-1", "episode-2"})

    def test_result_identity_collision_and_idempotent_append(self):
        with TemporaryDirectory() as directory:
            queue = PersistentRollingQueue(state_path=Path(directory) / "queue.json", campaign_home="home")
            first = item("episode-1")
            queue.append(first)
            queue.append(first)
            self.assertEqual(queue.pending_count, 1)
            queue.next()
            queue.complete(result_identity="result-1")
            queue.append(EpisodeRequest("episode-2", "circle", 2, "home", "episode-2"))
            queue.next()
            with self.assertRaisesRegex(ValueError, "result identity"):
                queue.complete(result_identity="result-1")

    def test_fifty_episode_campaign_mixed_failures_receiver_alive_and_fast_cycles(self):
        with TemporaryDirectory() as directory:
            queue = PersistentRollingQueue(state_path=Path(directory) / "queue.json", campaign_home="home", max_pending=100)
            for index in range(50):
                queue.append(EpisodeRequest(f"episode-{index}", "circle", index, "home"))
            failures = {index for index in range(50) if index % 7 == 0}
            processed = 0
            for index in range(50):
                read = queue.next()
                self.assertEqual(read.decision, QueueDecision.ITEM)
                if index in failures:
                    queue.fail(reason="trial_local", result_identity=f"result-{index}")
                else:
                    queue.complete(result_identity=f"result-{index}")
                processed += 1
            queue.request_drain()
            self.assertEqual(queue.next().decision, QueueDecision.COMPLETE)
            self.assertEqual(processed, 50)
            for index in range(100):
                cycle = PersistentRollingQueue(state_path=Path(directory) / f"cycle-{index}.json", campaign_home="home")
                cycle.append(EpisodeRequest(f"cycle-{index}", "circle", index, "home"))
                cycle.next()
                cycle.fail(reason="local", result_identity=f"cycle-result-{index}")
                cycle.request_end()
                self.assertEqual(cycle.mode.value, "COMPLETE")

    def test_stale_home_row_requires_current_identity_ack_and_consume(self):
        ledger = HomeIdentityLedger()
        ledger.announce("home-new")
        with self.assertRaisesRegex(ValueError, "overwrite"):
            ledger.announce("home-other")
        with self.assertRaisesRegex(ValueError, "ACKed"):
            ledger.accept_terminal_row("home-new")
        ledger.acknowledge("home-new")
        with self.assertRaisesRegex(ValueError, "consume"):
            ledger.accept_terminal_row("home-new")
        ledger.consume("home-new")
        ledger.accept_terminal_row("home-new")
        with self.assertRaisesRegex(ValueError, "current identity"):
            ledger.accept_terminal_row("home-old")

    def test_home_ledger_restarts_with_ack_consume_state(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "home.json"
            ledger = HomeIdentityLedger(state_path=path)
            ledger.announce("home-identity-1")
            ledger.acknowledge("home-identity-1")
            restarted = HomeIdentityLedger(state_path=path)
            self.assertTrue(restarted.current.acknowledged)
            restarted.consume("home-identity-1")
            restarted_again = HomeIdentityLedger(state_path=path)
            self.assertTrue(restarted_again.current.consumed)

    def test_campaign_runner_is_long_lived_with_isolated_hooks_and_explicit_home(self):
        with TemporaryDirectory() as directory:
            queue = PersistentRollingQueue(state_path=Path(directory) / "queue.json", campaign_home="home", max_pending=100)
            ledger = HomeIdentityLedger(state_path=Path(directory) / "home.json")
            for index in range(50):
                queue.append(EpisodeRequest(f"episode-{index}", "circle", index, "home"))
            lifecycle = CampaignLifecycle(queue)
            seen = []
            reset_count = [0, 0]

            def episode_runner(request):
                seen.append(request.episode_id)
                return int(request.episode_id.rsplit("-", 1)[1]) % 5 != 0

            def bad_capture(request):
                if int(request.episode_id.rsplit("-", 1)[1]) % 7 == 0:
                    raise RuntimeError("capture local")

            runner = CampaignRunner(lifecycle, ledger, episode_runner=episode_runner, retract_plan_provider=retract_plan, retract_to_home=retract_executor, hooks={"capture": bad_capture}, expert_reset=lambda: reset_count.__setitem__(0, reset_count[0] + 1), filter_reset=lambda: reset_count.__setitem__(1, reset_count[1] + 1))
            for _ in range(50):
                step = runner.step()
                self.assertEqual(step.decision, "WAITING_HOME")
                runner.acknowledge_and_consume_home(runner.pending_home_identity)
            self.assertEqual(len(seen), 50)
            self.assertEqual(reset_count, [50, 50])
            queue.request_drain()
            self.assertEqual(runner.step().decision, QueueDecision.COMPLETE.value)
            self.assertEqual(lifecycle.state, CampaignState.COMPLETE)

            for index in range(100):
                cycle_queue = PersistentRollingQueue(state_path=Path(directory) / f"cycle-{index}.json", campaign_home="home")
                cycle_ledger = HomeIdentityLedger(state_path=Path(directory) / f"cycle-{index}-home.json")
                cycle_queue.append(EpisodeRequest(f"cycle-{index}", "circle", index, "home"))
                cycle = CampaignRunner(CampaignLifecycle(cycle_queue), cycle_ledger, episode_runner=lambda _: False, retract_plan_provider=retract_plan, retract_to_home=retract_executor, expert_reset=noop_reset, filter_reset=noop_reset)
                cycle_step = cycle.step()
                self.assertEqual(cycle_step.decision, "WAITING_HOME")
                cycle.acknowledge_and_consume_home(cycle.pending_home_identity)
                cycle.request_end()
                self.assertEqual(cycle.step().decision, QueueDecision.COMPLETE.value)

    def test_mandatory_reset_failure_skips_episode_but_retracts_and_continues(self):
        queue = PersistentRollingQueue(campaign_home="home")
        queue.append(EpisodeRequest("reset-fails", "circle", 1, "home"))
        queue.append(EpisodeRequest("reset-recovers", "circle", 2, "home"))
        ledger = HomeIdentityLedger()
        executed = []
        retracts = []
        reset_calls = [0, 0]

        def expert_reset():
            reset_calls[0] += 1
            if reset_calls[0] == 1:
                raise RuntimeError("expert state unavailable")

        def filter_reset():
            reset_calls[1] += 1

        runner = CampaignRunner(
            CampaignLifecycle(queue),
            ledger,
            episode_runner=lambda request: executed.append(request.episode_id),
            retract_plan_provider=retract_plan,
            retract_to_home=lambda plan: retracts.append(plan.home_identity),
            expert_reset=expert_reset,
            filter_reset=filter_reset,
        )
        first = runner.step()
        self.assertEqual(first.decision, "WAITING_HOME")
        self.assertEqual(first.status, "failed")
        self.assertEqual(executed, [])
        self.assertEqual(len(retracts), 1)
        runner.acknowledge_and_consume_home(runner.pending_home_identity)
        second = runner.step()
        self.assertEqual(second.decision, "WAITING_HOME")
        self.assertEqual(second.status, "completed")
        self.assertEqual(executed, ["reset-recovers"])
        self.assertEqual(reset_calls, [2, 2])

    def test_campaign_runner_restart_between_retract_announce_ack_and_consume(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            queue_path, home_path, runner_path, outbox_path = (root / name for name in ("queue.json", "home.json", "runner.json", "outbox.json"))
            queue = PersistentRollingQueue(state_path=queue_path, campaign_home="home")
            request = EpisodeRequest("episode-restart", "circle", 1, "home")
            queue.append(request)
            ledger = HomeIdentityLedger(state_path=home_path)
            lifecycle = CampaignLifecycle(queue)
            retracted = []

            def plan(request, identity):
                return SafeRetractPlan(identity, request.task_ready_home, (0.0, 0.0, 1.0), (0.0, 0.0, -1.0), 0.01)

            runner = CampaignRunner(
                lifecycle,
                ledger,
                episode_runner=lambda _: True,
                retract_plan_provider=plan,
                retract_to_home=lambda item: retracted.append(item.home_identity),
                expert_reset=noop_reset,
                filter_reset=noop_reset,
                hooks={"capture": lambda _: (_ for _ in ()).throw(RuntimeError("consumer is separate"))},
                outbox=DurableHookOutbox(state_path=outbox_path),
                state_path=runner_path,
            )
            first = runner.step()
            self.assertEqual(first.decision, "WAITING_HOME")
            self.assertEqual(retracted, [runner.pending_home_identity])
            runner.request_end()

            restarted = CampaignRunner(
                CampaignLifecycle(PersistentRollingQueue(state_path=queue_path, campaign_home="home")),
                HomeIdentityLedger(state_path=home_path),
                retract_plan_provider=plan,
                retract_to_home=retract_executor,
                expert_reset=noop_reset,
                filter_reset=noop_reset,
                outbox=DurableHookOutbox(state_path=outbox_path),
                state_path=runner_path,
            )
            self.assertEqual(restarted.step().decision, "WAITING_HOME")
            with self.assertRaisesRegex(ValueError, "home identity"):
                restarted.acknowledge_and_consume_home("stale-home")
            restarted.acknowledge_and_consume_home(restarted.pending_home_identity)
            self.assertEqual(restarted.step().decision, QueueDecision.COMPLETE.value)
            self.assertGreater(restarted.outbox.pending_count, 0)
            self.assertEqual(restarted.outbox.process_one(lambda _: (_ for _ in ()).throw(RuntimeError("consumer failed"))), "FAILED")
            self.assertEqual(restarted.outbox.pending_count, 0)
            self.assertTrue(restarted.outbox.failed)
            restarted.outbox.enqueue(identity="crash-handoff", kind="capture", episode_id="episode-restart")
            claimed = restarted.outbox.claim()
            self.assertEqual(claimed["identity"], "crash-handoff")
            recovered_outbox = DurableHookOutbox(state_path=outbox_path)
            self.assertEqual(recovered_outbox.claimed, ("crash-handoff",))
            recovered_outbox.reconcile_claimed("crash-handoff", outcome="failed", reason="consumer_crash")
            self.assertIn("crash-handoff", recovered_outbox.failed)

    def test_campaign_runner_reconstructs_ack_consume_and_terminalization_windows(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            queue_path, home_path, runner_path = root / "queue.json", root / "home.json", root / "runner.json"
            queue = PersistentRollingQueue(state_path=queue_path, campaign_home="home")
            queue.append(EpisodeRequest("window", "circle", 1, "home"))
            runner = CampaignRunner(CampaignLifecycle(queue), HomeIdentityLedger(state_path=home_path), episode_runner=lambda _: True, retract_plan_provider=retract_plan, retract_to_home=retract_executor, expert_reset=noop_reset, filter_reset=noop_reset, state_path=runner_path)
            pending = runner.step()
            identity = runner.pending_home_identity
            runner.home_ledger.acknowledge(identity)
            after_ack = CampaignRunner(CampaignLifecycle(PersistentRollingQueue(state_path=queue_path, campaign_home="home")), HomeIdentityLedger(state_path=home_path), retract_plan_provider=retract_plan, retract_to_home=retract_executor, expert_reset=noop_reset, filter_reset=noop_reset, state_path=runner_path)
            self.assertEqual(after_ack.step().decision, "WAITING_HOME")
            after_ack.home_ledger.consume(identity)
            after_consume = CampaignRunner(CampaignLifecycle(PersistentRollingQueue(state_path=queue_path, campaign_home="home")), HomeIdentityLedger(state_path=home_path), retract_plan_provider=retract_plan, retract_to_home=retract_executor, expert_reset=noop_reset, filter_reset=noop_reset, state_path=runner_path)
            # Simulate a crash after queue terminalization but before the
            # runner state clear; reload must clear only the exact terminal row.
            after_consume.lifecycle.episode_completed(result_identity=pending.evidence_id)
            queue2 = PersistentRollingQueue(state_path=queue_path, campaign_home="home")
            self.assertIsNone(queue2.inflight)
            rebuilt = CampaignRunner(CampaignLifecycle(queue2), HomeIdentityLedger(state_path=home_path), retract_plan_provider=retract_plan, retract_to_home=retract_executor, expert_reset=noop_reset, filter_reset=noop_reset, state_path=runner_path)
            self.assertIsNone(rebuilt.pending_home_identity)


if __name__ == "__main__":
    unittest.main()
