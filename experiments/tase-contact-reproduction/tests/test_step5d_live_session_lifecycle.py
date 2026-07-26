from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from run_step5d_autotune_v3_live import (  # noqa: E402
    LiveSessionLifecycle,
    LiveSessionState,
    LiveSessionAttemptError,
    RecoverableLiveSessionAttemptError,
    LiveLaunchError,
    _run_recoverable_sessions,
    _should_retry_session_error,
    _should_request_program_stop,
)


def test_first_failure_cleanup_backoff_then_second_isolated_session() -> None:
    calls: list[int] = []
    events: list[str] = []
    lifecycle = LiveSessionLifecycle()

    def session() -> dict[str, object]:
        calls.append(1)
        events.append(f"session-{len(calls)}")
        if len(calls) == 1:
            raise RecoverableLiveSessionAttemptError(RuntimeError("runner failed"))
        return {"ok": True}

    result = _run_recoverable_sessions(
        session,
        cleanup=lambda: events.append("cleanup"),
        backoff=lambda: events.append("backoff"),
        lifecycle=lifecycle,
        retry_budget=1,
    )

    assert result == {"ok": True}
    assert len(calls) == 2
    assert events == ["session-1", "cleanup", "backoff", "session-2"]
    assert lifecycle.receiver_accepting is True
    assert lifecycle.transitions == [LiveSessionState.RECOVERING.value, LiveSessionState.WAITING_FOR_HARDWARE.value]


def test_hardware_false_never_starts_second_session() -> None:
    calls: list[int] = []
    cleanups: list[str] = []
    lifecycle = LiveSessionLifecycle()

    def session() -> dict[str, object]:
        calls.append(1)
        raise RuntimeError("bridge failed")

    def backoff() -> None:
        if cleanups:
            raise KeyboardInterrupt

    with pytest.raises(RuntimeError, match="bridge failed"):
        _run_recoverable_sessions(
            session,
            cleanup=lambda: cleanups.append("cleanup"),
            backoff=backoff,
            lifecycle=lifecycle,
        )

    assert len(calls) == 1
    assert cleanups == []
    assert lifecycle.state is LiveSessionState.WAITING_FOR_PARAMETERS
    assert lifecycle.receiver_accepting is True


def test_retry_budget_default_zero_prevents_additional_retries() -> None:
    calls: list[int] = []
    lifecycle = LiveSessionLifecycle()

    def session() -> dict[str, object]:
        calls.append(1)
        raise RecoverableLiveSessionAttemptError(RuntimeError("budgeted no retry"))

    with pytest.raises(RuntimeError, match="budgeted no retry"):
        _run_recoverable_sessions(
            session,
            cleanup=lambda: None,
            backoff=lambda: pytest.fail("backoff should not run when budget is zero"),
            lifecycle=lifecycle,
        )

    assert calls == [1]
    assert lifecycle.receiver_accepting is True


@pytest.mark.parametrize("retry_budget", [2, 1.5, True, "1"])
def test_retry_budget_rejects_invalid_values(retry_budget: Any) -> None:
    lifecycle = LiveSessionLifecycle()

    def session() -> dict[str, object]:
        return {"ok": True}

    with pytest.raises(LiveLaunchError, match="retry budget"):
        _run_recoverable_sessions(
            session,
            cleanup=lambda: None,
            backoff=lambda: None,
            lifecycle=lifecycle,
            retry_budget=retry_budget,
        )


def test_non_explicit_live_session_attempt_error_is_not_retried() -> None:
    calls: list[int] = []
    lifecycle = LiveSessionLifecycle()

    def session() -> None:
        calls.append(1)
        raise LiveSessionAttemptError(
            RuntimeError("implicit recoverable flag false"),
            recoverable=True,
        )

    with pytest.raises(RuntimeError, match="implicit recoverable flag false"):
        _run_recoverable_sessions(
            session,
            cleanup=lambda: None,
            backoff=lambda: pytest.fail("backoff should not run for non-explicit marker"),
            lifecycle=lifecycle,
            retry_budget=1,
        )

    assert calls == [1]
    assert _should_retry_session_error(
        LiveSessionAttemptError(RuntimeError("test"), recoverable=True)
    ) is False


def test_empty_queue_keeps_receiver_accepting_until_shutdown() -> None:
    calls: list[int] = []
    cleanups: list[str] = []
    waits = [0]

    def session() -> None:
        calls.append(1)
        return None

    def backoff() -> None:
        waits[0] += 1
        if waits[0] == 2:
            raise KeyboardInterrupt

    lifecycle = LiveSessionLifecycle()
    with pytest.raises(KeyboardInterrupt):
        _run_recoverable_sessions(
            session,
            cleanup=lambda: cleanups.append("cleanup"),
            backoff=backoff,
            lifecycle=lifecycle,
        )

    assert len(calls) == 2
    assert lifecycle.receiver_accepting is False
    assert lifecycle.state is LiveSessionState.SHUTDOWN
    assert cleanups == ["cleanup"]


def test_failed_session_cleanup_targets_runner_and_bridge_without_shutdown() -> None:
    lifecycle = LiveSessionLifecycle()
    runner = object()
    bridge = object()
    lifecycle.start_session(runner=runner, bridge=bridge)

    assert lifecycle.cleanup_targets(failed_session=True) == (runner, bridge)
    assert lifecycle.receiver_accepting is True
    assert lifecycle.state is not LiveSessionState.SHUTDOWN


def test_recoverable_error_does_not_request_tp_stop() -> None:
    lifecycle = LiveSessionLifecycle()
    lifecycle.observe(error=RuntimeError("bridge failed"))

    assert _should_request_program_stop(
        campaign_completed=False,
        explicit_shutdown=lifecycle.explicit_shutdown,
    ) is False


def test_explicit_shutdown_requests_tp_stop() -> None:
    lifecycle = LiveSessionLifecycle()
    lifecycle.request_shutdown()

    assert _should_request_program_stop(
        campaign_completed=False,
        explicit_shutdown=lifecycle.explicit_shutdown,
    ) is True


def test_terminal_campaign_requests_safe_tp_stop() -> None:
    assert _should_request_program_stop(
        campaign_completed=True,
        explicit_shutdown=False,
    ) is True
