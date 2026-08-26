from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MAP = json.loads((ROOT / "config/ur10e_test_dependency_map_v1.json").read_text())
sys.path.insert(0, str(ROOT / "tools"))

from ur10e_parallel import (  # noqa: E402
    ResourceProfile,
    formal_timing_lease,
    formal_timing_owner,
    notify_formal_timing_owner,
)


_FORMAL_TIMING_LEASE = None


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    groups = MAP.get("resource_groups", {})
    for item in items:
        try:
            relative = Path(str(item.fspath)).resolve().relative_to(ROOT).as_posix()
        except ValueError:
            # Repository-level shared-runtime tests are selected alongside the
            # experiment suite. They do not participate in experiment-local
            # resource groups and must remain collectable as a separate scope.
            continue
        for group, paths in groups.items():
            if relative in paths:
                item.add_marker(getattr(pytest.mark, group))
                item.add_marker(pytest.mark.xdist_group(name=group))


def pytest_collection_finish(session: pytest.Session) -> None:
    global _FORMAL_TIMING_LEASE
    if not any(item.get_closest_marker("formal_timing") for item in session.items):
        return
    profile = ResourceProfile.from_env()
    try:
        lease = formal_timing_lease(
            profile,
            task=f"pytest:{ROOT}",
            blocking=False,
        )
        lease.__enter__()
        _FORMAL_TIMING_LEASE = lease
    except BlockingIOError as exc:
        owner = formal_timing_owner(profile)
        notice_sent = notify_formal_timing_owner(
            owner,
            f"pytest blocked by formal timing owner: {owner or 'throughput owner unavailable'}",
        )
        raise pytest.UsageError(
            "formal_timing resource is busy; "
            f"owner={owner!r}; notice_sent={notice_sent}"
        ) from exc


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    del session, exitstatus
    global _FORMAL_TIMING_LEASE
    if _FORMAL_TIMING_LEASE is not None:
        _FORMAL_TIMING_LEASE.__exit__(None, None, None)
        _FORMAL_TIMING_LEASE = None
