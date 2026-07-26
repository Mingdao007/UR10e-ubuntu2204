from __future__ import annotations

from pathlib import Path
import hashlib
import os
from typing import Any
import pytest


ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "tools"))

import step5d_bridge_authority as authority  # noqa: E402


def _write_basis(base: Path, name: str) -> tuple[str, str]:
    path = base / name
    path.write_text("launch basis", encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return str(path), digest


def _attempt_ctx(base: Path, suffix: str) -> tuple[str, str, str, str]:
    worktree = base / f"wt-{suffix}"
    worktree.mkdir()
    basis_path, basis_sha = _write_basis(base, f"basis-{suffix}.py")
    head = f"head-{suffix}".ljust(40, "0")
    return str(worktree), head, basis_path, basis_sha


def test_default_global_registry_is_shared_across_worktrees_and_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    authority_dir = tmp_path / "run-user"
    registry = authority_dir / authority.DEFAULT_RESOURCE_ID
    monkeypatch.setattr(authority, "DEFAULT_AUTHORITY_ROOT", authority_dir)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg-one"))
    monkeypatch.setenv("HOME", str(tmp_path / "home-one"))

    owner_a = 101
    owner_b = 102
    monkeypatch.setattr(authority.os, "getppid", lambda: owner_a)
    monkeypatch.setattr(
        authority,
        "read_proc_starttime_ticks",
        lambda pid: {owner_a: 700, owner_b: 701}.get(pid),
    )

    wt_a, head_a, basis_a, basis_sha_a = _attempt_ctx(tmp_path, "a")
    payload_a = authority.begin(
        None,
        attempt_id="attempt-worktree-a",
        owner_pid=owner_a,
        owner_starttime_ticks=700,
        worktree_root=wt_a,
        repository_head=head_a,
        launch_basis_path=basis_a,
        launch_basis_sha256=basis_sha_a,
    )
    assert payload_a["state"] == "ACTIVE"
    assert payload_a["launch_basis_path"] is None
    assert payload_a["launch_basis_sha256"] is None
    assert payload_a["worktree_root"] == wt_a
    assert registry == authority.DEFAULT_AUTHORITY_ROOT / authority.DEFAULT_RESOURCE_ID
    assert (registry / authority.STATE_FILE).exists()

    wt_b, head_b, basis_b, basis_sha_b = _attempt_ctx(tmp_path, "b")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg-two"))
    monkeypatch.setattr(authority.os, "getppid", lambda: owner_b)
    with pytest.raises(authority.BridgeAuthorityError, match="already active|another canonical"):
        authority.begin(
            None,
            attempt_id="attempt-worktree-b",
            owner_pid=owner_b,
            owner_starttime_ticks=701,
            worktree_root=wt_b,
            repository_head=head_b,
            launch_basis_path=basis_b,
            launch_basis_sha256=basis_sha_b,
        )


def test_stale_takeover_on_process_disappears(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    authority_dir = tmp_path / "run-user"
    monkeypatch.setattr(authority, "DEFAULT_AUTHORITY_ROOT", authority_dir)

    owner_a = 201
    owner_b = 202
    current_owner = owner_a

    def fake_ppid() -> int:
        return current_owner

    monkeypatch.setattr(authority.os, "getppid", fake_ppid)
    monkeypatch.setattr(
        authority,
        "read_proc_starttime_ticks",
        lambda pid: {owner_a: 800, owner_b: 810}.get(pid),
    )
    wt_a, head_a, basis_a, basis_sha_a = _attempt_ctx(tmp_path, "stale-a")
    first = authority.begin(
        None,
        attempt_id="attempt-stale",
        owner_pid=owner_a,
        owner_starttime_ticks=800,
        worktree_root=wt_a,
        repository_head=head_a,
        launch_basis_path=basis_a,
        launch_basis_sha256=basis_sha_a,
    )

    current_owner = owner_b
    monkeypatch.setattr(
        authority, "read_proc_starttime_ticks", lambda pid: {owner_a: None, owner_b: 810}.get(pid)
    )
    wt_b, head_b, basis_b, basis_sha_b = _attempt_ctx(tmp_path, "stale-b")
    second = authority.begin(
        None,
        attempt_id="attempt-stale-2",
        owner_pid=owner_b,
        owner_starttime_ticks=810,
        worktree_root=wt_b,
        repository_head=head_b,
        launch_basis_path=basis_b,
        launch_basis_sha256=basis_sha_b,
    )
    assert second["state"] == "ACTIVE"
    assert second["authority_epoch"] == first["authority_epoch"] + 1


def test_pid_reuse_and_boot_change_takeover_are_separate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    authority_dir = tmp_path / "run-user"
    monkeypatch.setattr(authority, "DEFAULT_AUTHORITY_ROOT", authority_dir)

    owner = 301
    current_owner = owner

    def fake_ppid() -> int:
        return current_owner

    monkeypatch.setattr(authority.os, "getppid", fake_ppid)
    mapping = {owner: 900}
    monkeypatch.setattr(
        authority, "read_proc_starttime_ticks", lambda pid: mapping.get(pid)
    )
    wt_a, head_a, basis_a, basis_sha_a = _attempt_ctx(tmp_path, "reuse-a")
    first = authority.begin(
        None,
        attempt_id="attempt-pid-reuse",
        owner_pid=owner,
        owner_starttime_ticks=900,
        worktree_root=wt_a,
        repository_head=head_a,
        launch_basis_path=basis_a,
        launch_basis_sha256=basis_sha_a,
    )

    mapping[owner] = 901
    wt_b, head_b, basis_b, basis_sha_b = _attempt_ctx(tmp_path, "reuse-b")
    second = authority.begin(
        None,
        attempt_id="attempt-pid-reuse-2",
        owner_pid=owner,
        owner_starttime_ticks=901,
        worktree_root=wt_b,
        repository_head=head_b,
        launch_basis_path=basis_b,
        launch_basis_sha256=basis_sha_b,
    )
    assert second["authority_epoch"] == first["authority_epoch"] + 1

    monkeypatch.setattr(authority, "_read_boot_id", lambda: "boot-a")
    wt_c, head_c, basis_c, basis_sha_c = _attempt_ctx(tmp_path, "boot-a")
    base_owner = 401
    base_start = 1111
    mapping = {owner: 901, base_owner: base_start}
    monkeypatch.setattr(
        authority,
        "read_proc_starttime_ticks",
        lambda pid: mapping.get(pid),
    )
    current_owner = base_owner
    boot_owner = authority.begin(
        None,
        attempt_id="attempt-boot-1",
        owner_pid=base_owner,
        owner_starttime_ticks=base_start,
        worktree_root=wt_c,
        repository_head=head_c,
        launch_basis_path=basis_c,
        launch_basis_sha256=basis_sha_c,
    )

    monkeypatch.setattr(authority, "_read_boot_id", lambda: "boot-b")
    wt_d, head_d, basis_d, basis_sha_d = _attempt_ctx(tmp_path, "boot-b")
    takeover = authority.begin(
        None,
        attempt_id="attempt-boot-2",
        owner_pid=base_owner,
        owner_starttime_ticks=base_start,
        worktree_root=wt_d,
        repository_head=head_d,
        launch_basis_path=basis_d,
        launch_basis_sha256=basis_sha_d,
    )
    assert takeover["authority_epoch"] == boot_owner["authority_epoch"] + 1


def test_revoke_is_idempotent_only_for_exact_owner_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority_dir = tmp_path / "run-user"
    monkeypatch.setattr(authority, "DEFAULT_AUTHORITY_ROOT", authority_dir)
    owner = 501
    monkeypatch.setattr(authority.os, "getppid", lambda: owner)
    monkeypatch.setattr(authority, "read_proc_starttime_ticks", lambda _pid: 1400)
    wt, head, basis, basis_sha = _attempt_ctx(tmp_path, "revoke")
    first = authority.begin(
        None,
        attempt_id="attempt-revoke",
        owner_pid=owner,
        owner_starttime_ticks=1400,
        worktree_root=wt,
        repository_head=head,
        launch_basis_path=basis,
        launch_basis_sha256=basis_sha,
    )
    revoked = authority.revoke(
        None,
        attempt_id="attempt-revoke",
        owner_pid=owner,
        owner_starttime_ticks=1400,
        reason="completed",
    )
    assert revoked["state"] == "REVOKED"
    assert revoked["sequence"] == first["sequence"] + 1
    again = authority.revoke(
        None,
        attempt_id="attempt-revoke",
        owner_pid=owner,
        owner_starttime_ticks=1400,
        reason="completed",
    )
    assert again["sequence"] == revoked["sequence"]

    wt_other, head_other, basis_other, basis_sha_other = _attempt_ctx(tmp_path, "revoke-other")
    with pytest.raises(authority.BridgeAuthorityError, match="owned by another attempt|not ACTIVE|owner binding differs"):
        authority.revoke(
            None,
            attempt_id="attempt-other",
            owner_pid=owner,
            owner_starttime_ticks=1400,
            reason="cancelled",
        )
    authority.begin(
        None,
        attempt_id="attempt-revoke-other",
        owner_pid=owner,
        owner_starttime_ticks=1400,
        worktree_root=wt_other,
        repository_head=head_other,
        launch_basis_path=basis_other,
        launch_basis_sha256=basis_sha_other,
    )


def test_runtime_gateway_records_attestation_and_checks_owner_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority_dir = tmp_path / "run-user"
    monkeypatch.setattr(authority, "DEFAULT_AUTHORITY_ROOT", authority_dir)
    owner = 601
    owner_start = 1600
    monkeypatch.setattr(authority.os, "getppid", lambda: owner)
    monkeypatch.setattr(authority, "read_proc_starttime_ticks", lambda _pid: owner_start)
    wt, head, basis, basis_sha = _attempt_ctx(tmp_path, "runtime")
    payload = authority.begin(
        None,
        attempt_id="attempt-runtime",
        owner_pid=owner,
        owner_starttime_ticks=owner_start,
        worktree_root=wt,
        repository_head=head,
        launch_basis_path=basis,
        launch_basis_sha256=basis_sha,
    )
    authority.bind_basis(
        None,
        attempt_id="attempt-runtime",
        owner_pid=owner,
        owner_starttime_ticks=owner_start,
        sequence=payload["sequence"],
        launch_basis_path=basis,
        launch_basis_sha256=basis_sha,
    )

    started = authority.record_runtime_attempt(
        None,
        attempt_id="attempt-runtime",
        owner_pid=owner,
        owner_starttime_ticks=owner_start,
        state="STARTED",
        worktree_root=wt,
        repository_head=head,
        launch_basis_path=basis,
        launch_basis_sha256=basis_sha,
    )
    assert started["attestation"]["phase"] == "runtime_gate"

    monkeypatch.setattr(authority.os, "getppid", lambda: owner + 1)
    with pytest.raises(authority.BridgeAuthorityError, match="bound shell owner"):
        authority.record_runtime_attempt(
            None,
            attempt_id="attempt-runtime",
            owner_pid=owner,
            owner_starttime_ticks=owner_start,
            state="FAILED",
            exit_code=13,
            reason_code="RUNTIME_NOT_PROVISIONED",
            detail="runtime pointer unavailable",
            worktree_root=wt,
            repository_head=head,
            launch_basis_path=basis,
            launch_basis_sha256=basis_sha,
        )


def test_explicit_authority_root_keeps_legacy_direct_path_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority_dir = tmp_path / "legacy-root"
    owner = 701
    monkeypatch.setattr(authority.os, "getppid", lambda: owner)
    monkeypatch.setattr(authority, "read_proc_starttime_ticks", lambda _pid: 1700)

    payload = authority.begin(
        authority_dir,
        attempt_id="attempt-legacy",
        owner_pid=owner,
        owner_starttime_ticks=1700,
    )
    assert payload["state"] == "ACTIVE"
    assert (authority_dir / authority.STATE_FILE).exists()
    assert (authority_dir / authority.LOCK_FILE).exists()

    loaded = authority.load_current(
        authority_dir,
        attempt_id="attempt-legacy",
        owner_pid=owner,
        owner_starttime_ticks=1700,
    )
    assert loaded is not None


def test_begin_and_revoke_reject_spoofed_caller_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority_dir = tmp_path / "run-user"
    monkeypatch.setattr(authority, "DEFAULT_AUTHORITY_ROOT", authority_dir)
    owner = 801
    monkeypatch.setattr(authority.os, "getppid", lambda: owner)
    monkeypatch.setattr(authority, "read_proc_starttime_ticks", lambda _pid: 1800)
    wt, head, basis, basis_sha = _attempt_ctx(tmp_path, "spoof")
    authority.begin(
        None,
        attempt_id="attempt-spoof-guard",
        owner_pid=owner,
        owner_starttime_ticks=1800,
        worktree_root=wt,
        repository_head=head,
        launch_basis_path=basis,
        launch_basis_sha256=basis_sha,
    )

    monkeypatch.setattr(authority.os, "getppid", lambda: owner + 1)
    with pytest.raises(authority.BridgeAuthorityError, match="bound shell owner differs"):
        authority.begin(
            None,
            attempt_id="attempt-spoof-guard-bad",
            owner_pid=owner,
            owner_starttime_ticks=1800,
            worktree_root=wt,
            repository_head=head,
            launch_basis_path=basis,
            launch_basis_sha256=basis_sha,
        )
    with pytest.raises(authority.BridgeAuthorityError, match="bound shell owner differs"):
        authority.revoke(
            None,
            attempt_id="attempt-spoof-guard",
            owner_pid=owner,
            owner_starttime_ticks=1800,
            reason="completed",
        )


def test_authority_fence_assert_revoked_uses_next_sequence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority_dir = tmp_path / "run-user"
    monkeypatch.setattr(authority, "DEFAULT_AUTHORITY_ROOT", authority_dir)
    owner = 901
    monkeypatch.setattr(authority.os, "getppid", lambda: owner)
    monkeypatch.setattr(authority, "read_proc_starttime_ticks", lambda _pid: 1900)
    wt, head, basis, basis_sha = _attempt_ctx(tmp_path, "assert")
    payload = authority.begin(
        None,
        attempt_id="attempt-assert",
        owner_pid=owner,
        owner_starttime_ticks=1900,
        worktree_root=wt,
        repository_head=head,
        launch_basis_path=basis,
        launch_basis_sha256=basis_sha,
    )
    revoked = authority.revoke(
        None,
        attempt_id="attempt-assert",
        owner_pid=owner,
        owner_starttime_ticks=1900,
        reason="completed",
    )
    assert revoked["state"] == "REVOKED"

    fence = authority.AuthorityFence(
        authority_dir / authority.DEFAULT_RESOURCE_ID,
        attempt_id="attempt-assert",
        sequence=payload["sequence"],
        owner_pid=owner,
        owner_starttime_ticks=1900,
    )
    assert fence.assert_revoked()["sequence"] == revoked["sequence"]

    stale_fence = authority.AuthorityFence(
        authority_dir / authority.DEFAULT_RESOURCE_ID,
        attempt_id="attempt-assert",
        sequence=payload["sequence"] + 1,
        owner_pid=owner,
        owner_starttime_ticks=1900,
    )
    with pytest.raises(authority.BridgeAuthorityError):
        stale_fence.assert_revoked()


def test_authority_fence_requires_bound_basis(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    authority_dir = tmp_path / "run-user"
    monkeypatch.setattr(authority, "DEFAULT_AUTHORITY_ROOT", authority_dir)
    owner = 1001
    monkeypatch.setattr(authority.os, "getppid", lambda: owner)
    monkeypatch.setattr(authority, "read_proc_starttime_ticks", lambda _pid: 2000)
    wt, head, basis, basis_sha = _attempt_ctx(tmp_path, "fence")
    payload = authority.begin(
        None,
        attempt_id="attempt-fence-prebind",
        owner_pid=owner,
        owner_starttime_ticks=2000,
        worktree_root=wt,
        repository_head=head,
        launch_basis_path=basis,
        launch_basis_sha256=basis_sha,
    )

    prebind_fence = authority.AuthorityFence(
        authority_dir / authority.DEFAULT_RESOURCE_ID,
        attempt_id="attempt-fence-prebind",
        sequence=payload["sequence"],
        owner_pid=owner,
        owner_starttime_ticks=2000,
    )
    with pytest.raises(authority.BridgeAuthorityError, match="launch basis is not bound"):
        prebind_fence.assert_active()

    authority.bind_basis(
        None,
        attempt_id="attempt-fence-prebind",
        owner_pid=owner,
        owner_starttime_ticks=2000,
        sequence=payload["sequence"],
        launch_basis_path=basis,
        launch_basis_sha256=basis_sha,
    )
    assert (
        prebind_fence.assert_active()["launch_basis_path"] == basis
    )


def test_authority_bind_basis_is_idempotent_and_rejects_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority_dir = tmp_path / "run-user"
    monkeypatch.setattr(authority, "DEFAULT_AUTHORITY_ROOT", authority_dir)
    owner = 1002
    monkeypatch.setattr(authority.os, "getppid", lambda: owner)
    monkeypatch.setattr(authority, "read_proc_starttime_ticks", lambda _pid: 2100)
    wt, head, basis, basis_sha = _attempt_ctx(tmp_path, "bind-idem")
    payload = authority.begin(
        None,
        attempt_id="attempt-bind-idem",
        owner_pid=owner,
        owner_starttime_ticks=2100,
        worktree_root=wt,
        repository_head=head,
        launch_basis_path=basis,
        launch_basis_sha256=basis_sha,
    )

    bound_first = authority.bind_basis(
        None,
        attempt_id="attempt-bind-idem",
        owner_pid=owner,
        owner_starttime_ticks=2100,
        sequence=payload["sequence"],
        launch_basis_path=basis,
        launch_basis_sha256=basis_sha,
    )
    bound_second = authority.bind_basis(
        None,
        attempt_id="attempt-bind-idem",
        owner_pid=owner,
        owner_starttime_ticks=2100,
        sequence=payload["sequence"],
        launch_basis_path=basis,
        launch_basis_sha256=basis_sha,
    )
    assert bound_first["sequence"] == bound_second["sequence"] == payload["sequence"]

    other_basis, other_basis_sha = _write_basis(tmp_path, "other-basis.py")
    with pytest.raises(
        authority.BridgeAuthorityError, match="launch basis is already bound"
    ):
        authority.bind_basis(
            None,
            attempt_id="attempt-bind-idem",
            owner_pid=owner,
            owner_starttime_ticks=2100,
            sequence=payload["sequence"],
            launch_basis_path=other_basis,
            launch_basis_sha256=other_basis_sha,
        )


def test_authority_bind_basis_and_two_worktree_xdg_contention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority_dir = tmp_path / "run-user"
    registry = authority_dir / authority.DEFAULT_RESOURCE_ID
    monkeypatch.setattr(authority, "DEFAULT_AUTHORITY_ROOT", authority_dir)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg-one"))
    monkeypatch.setenv("HOME", str(tmp_path / "home-one"))

    owner_a = 3001
    owner_b = 3002
    monkeypatch.setattr(authority.os, "getppid", lambda: owner_a)
    monkeypatch.setattr(
        authority,
        "read_proc_starttime_ticks",
        lambda pid: {owner_a: 3100, owner_b: 3200}.get(pid),
    )

    wt_a, head_a, basis_a, basis_sha_a = _attempt_ctx(tmp_path, "wt-xdg-a")
    authority.begin(
        None,
        attempt_id="attempt-xdg-a",
        owner_pid=owner_a,
        owner_starttime_ticks=3100,
        worktree_root=wt_a,
        repository_head=head_a,
        launch_basis_path=basis_a,
        launch_basis_sha256=basis_sha_a,
    )

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg-two"))
    monkeypatch.setenv("HOME", str(tmp_path / "home-two"))
    monkeypatch.setattr(authority.os, "getppid", lambda: owner_b)
    wt_b, head_b, basis_b, basis_sha_b = _attempt_ctx(tmp_path, "wt-xdg-b")
    with pytest.raises(authority.BridgeAuthorityError, match="already active|another canonical"):
        authority.begin(
            None,
            attempt_id="attempt-xdg-b",
            owner_pid=owner_b,
            owner_starttime_ticks=3200,
            worktree_root=wt_b,
            repository_head=head_b,
            launch_basis_path=basis_b,
            launch_basis_sha256=basis_sha_b,
        )
    assert (registry / authority.STATE_FILE).exists()


def test_state_file_symlink_is_unsafe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority_dir = tmp_path / "run-user"
    monkeypatch.setattr(authority, "DEFAULT_AUTHORITY_ROOT", authority_dir)
    owner = 1001
    monkeypatch.setattr(authority.os, "getppid", lambda: owner)
    monkeypatch.setattr(authority, "read_proc_starttime_ticks", lambda _pid: 2100)
    wt, head, basis, basis_sha = _attempt_ctx(tmp_path, "symlink")
    authority.begin(
        None,
        attempt_id="attempt-symlink",
        owner_pid=owner,
        owner_starttime_ticks=2100,
        worktree_root=wt,
        repository_head=head,
        launch_basis_path=basis,
        launch_basis_sha256=basis_sha,
    )

    state_path = authority_dir / authority.DEFAULT_RESOURCE_ID / authority.STATE_FILE
    state_path.unlink()
    state_path.symlink_to(tmp_path / "foreign-state")

    with pytest.raises(authority.BridgeAuthorityError, match="path is unsafe"):
        authority.load_current(
            None,
            attempt_id="attempt-symlink",
            owner_pid=owner,
            owner_starttime_ticks=2100,
        )


def test_begin_rejects_authority_root_with_ancestor_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority_root = tmp_path / authority.DEFAULT_RESOURCE_ID
    symlink_parent = tmp_path / "linked-root-parent"
    symlink_parent_target = tmp_path / "linked-root-target"
    symlink_parent_target.mkdir()
    symlink_parent.symlink_to(symlink_parent_target)

    owner = 1301
    monkeypatch.setattr(authority.os, "getppid", lambda: owner)
    monkeypatch.setattr(authority, "read_proc_starttime_ticks", lambda _pid: 2400)
    worktree = tmp_path / "wt-safe"
    worktree.mkdir()
    basis_path, basis_sha = _write_basis(tmp_path, "ancestor-root.py")
    head = "ancestor-root".ljust(40, "0")

    with pytest.raises(authority.BridgeAuthorityError, match="path is unsafe"):
        authority.begin(
            symlink_parent / authority_root.name,
            attempt_id="attempt-root-ancestor-symlink",
            owner_pid=owner,
            owner_starttime_ticks=2400,
            worktree_root=str(worktree),
            repository_head=head,
            launch_basis_path=basis_path,
            launch_basis_sha256=basis_sha,
        )


def test_begin_rejects_authority_root_symlink_leaf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority_root = tmp_path / "run-user"
    real_root = tmp_path / "run-real"
    real_root.mkdir()
    authority_root.symlink_to(real_root)

    owner = 1302
    monkeypatch.setattr(authority.os, "getppid", lambda: owner)
    monkeypatch.setattr(authority, "read_proc_starttime_ticks", lambda _pid: 2410)
    worktree = tmp_path / "wt-safe-2"
    worktree.mkdir()
    basis_path, basis_sha = _write_basis(tmp_path, "leaf-root.py")
    head = "leaf-root".ljust(40, "0")

    with pytest.raises(authority.BridgeAuthorityError, match="path is unsafe"):
        authority.begin(
            authority_root,
            attempt_id="attempt-root-leaf-symlink",
            owner_pid=owner,
            owner_starttime_ticks=2410,
            worktree_root=str(worktree),
            repository_head=head,
            launch_basis_path=basis_path,
            launch_basis_sha256=basis_sha,
        )


def test_begin_rejects_launch_basis_symlink_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority_dir = tmp_path / "run-user"
    monkeypatch.setattr(authority, "DEFAULT_AUTHORITY_ROOT", authority_dir)

    owner = 1303
    monkeypatch.setattr(authority.os, "getppid", lambda: owner)
    monkeypatch.setattr(authority, "read_proc_starttime_ticks", lambda _pid: 2500)
    worktree = tmp_path / "wt-launch-ancestor"
    worktree.mkdir()
    base_root = tmp_path / "real-basis-root"
    base_root.mkdir()
    real_basis = base_root / "launch.py"
    real_basis.write_text("launch basis", encoding="utf-8")
    basis_sha = hashlib.sha256(real_basis.read_bytes()).hexdigest()
    symlink_root = tmp_path / "basis-parent-link"
    symlink_root.symlink_to(base_root)
    symlinked_basis = symlink_root / "launch.py"

    with pytest.raises(authority.BridgeAuthorityError, match="path is unsafe"):
        authority.begin(
            None,
            attempt_id="attempt-basis-ancestor-symlink",
            owner_pid=owner,
            owner_starttime_ticks=2500,
            worktree_root=str(worktree),
            repository_head="basis-ancestor".ljust(40, "0"),
            launch_basis_path=str(symlinked_basis),
            launch_basis_sha256=basis_sha,
        )


def test_begin_rejects_launch_basis_symlink_leaf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority_dir = tmp_path / "run-user"
    monkeypatch.setattr(authority, "DEFAULT_AUTHORITY_ROOT", authority_dir)

    owner = 1304
    monkeypatch.setattr(authority.os, "getppid", lambda: owner)
    monkeypatch.setattr(authority, "read_proc_starttime_ticks", lambda _pid: 2600)
    worktree = tmp_path / "wt-launch-leaf"
    worktree.mkdir()
    real_basis = tmp_path / "real-leaf-basis.py"
    real_basis.write_text("launch basis", encoding="utf-8")
    basis_sha = hashlib.sha256(real_basis.read_bytes()).hexdigest()
    symlinked_basis = tmp_path / "leaf-basis-link.py"
    symlinked_basis.unlink(missing_ok=True)
    symlinked_basis.symlink_to(real_basis)

    with pytest.raises(authority.BridgeAuthorityError, match="path is unsafe"):
        authority.begin(
            None,
            attempt_id="attempt-basis-leaf-symlink",
            owner_pid=owner,
            owner_starttime_ticks=2600,
            worktree_root=str(worktree),
            repository_head="basis-leaf".ljust(40, "0"),
            launch_basis_path=str(symlinked_basis),
            launch_basis_sha256=basis_sha,
        )


def test_runtime_attempt_recorded_under_authority_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority_dir = tmp_path / "run-user"
    monkeypatch.setattr(authority, "DEFAULT_AUTHORITY_ROOT", authority_dir)
    owner = 1101
    monkeypatch.setattr(authority.os, "getppid", lambda: owner)
    monkeypatch.setattr(authority, "read_proc_starttime_ticks", lambda _pid: 2200)
    wt, head, basis, basis_sha = _attempt_ctx(tmp_path, "runtime-root")
    payload = authority.begin(
        None,
        attempt_id="attempt-runtime-root",
        owner_pid=owner,
        owner_starttime_ticks=2200,
        worktree_root=wt,
        repository_head=head,
        launch_basis_path=basis,
        launch_basis_sha256=basis_sha,
    )
    authority.bind_basis(
        None,
        attempt_id="attempt-runtime-root",
        owner_pid=owner,
        owner_starttime_ticks=2200,
        sequence=payload["sequence"],
        launch_basis_path=basis,
        launch_basis_sha256=basis_sha,
    )

    captured = {}

    def fake_publish(
        authority_root: Path,
        **kwargs: object,
    ) -> dict[str, Any]:
        captured["root"] = authority_root
        return {"attestation": {"sequence": 7}}

    monkeypatch.setattr(authority, "publish_launch_attempt", fake_publish)
    authority.record_runtime_attempt(
        None,
        attempt_id="attempt-runtime-root",
        owner_pid=owner,
        owner_starttime_ticks=2200,
        state="STARTED",
        worktree_root=wt,
        repository_head=head,
        launch_basis_path=basis,
        launch_basis_sha256=basis_sha,
    )
    assert captured["root"] == authority_dir / authority.DEFAULT_RESOURCE_ID


def test_cli_prints_only_integer_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    authority_dir = tmp_path / "run-user"
    monkeypatch.setattr(authority, "DEFAULT_AUTHORITY_ROOT", authority_dir)
    owner = 1201
    monkeypatch.setattr(authority.os, "getppid", lambda: owner)
    monkeypatch.setattr(authority, "read_proc_starttime_ticks", lambda _pid: 2300)
    wt, head, basis, basis_sha = _attempt_ctx(tmp_path, "cli")

    authority.main(
        [
            "begin",
            "--attempt-id",
            "attempt-cli",
            "--owner-pid",
            str(owner),
            "--owner-starttime",
            "2300",
            "--worktree-root",
            wt,
            "--repository-head",
            head,
            "--launch-basis-path",
            basis,
            "--launch-basis-sha256",
            basis_sha,
        ]
    )
    out = capsys.readouterr().out.strip()
    assert out == "1"

    authority.main(
        [
            "bind-basis",
            "--attempt-id",
            "attempt-cli",
            "--owner-pid",
            str(owner),
            "--owner-starttime",
            "2300",
            "--sequence",
            "1",
            "--launch-basis-path",
            basis,
            "--launch-basis-sha256",
            basis_sha,
        ]
    )
    out = capsys.readouterr().out.strip()
    assert out == "1"

    authority.main(
        [
            "revoke",
            "--attempt-id",
            "attempt-cli",
            "--owner-pid",
            str(owner),
            "--owner-starttime",
            "2300",
            "--reason",
            "completed",
        ]
    )
    out = capsys.readouterr().out.strip()
    assert out == "2"

    authority.main(
        [
            "begin",
            "--attempt-id",
            "attempt-runtime-cli",
            "--owner-pid",
            str(owner),
            "--owner-starttime",
            "2300",
            "--worktree-root",
            wt,
            "--repository-head",
            head,
            "--launch-basis-path",
            basis,
            "--launch-basis-sha256",
            basis_sha,
        ]
    )
    out = capsys.readouterr().out.strip()
    assert out == "3"

    authority.main(
        [
            "bind-basis",
            "--attempt-id",
            "attempt-runtime-cli",
            "--owner-pid",
            str(owner),
            "--owner-starttime",
            "2300",
            "--sequence",
            "3",
            "--launch-basis-path",
            basis,
            "--launch-basis-sha256",
            basis_sha,
        ]
    )
    out = capsys.readouterr().out.strip()
    assert out == "3"

    def fake_publish(*_args: object, **_kwargs: object) -> dict[str, Any]:
        return {"attestation": {"sequence": 18}}

    monkeypatch.setattr(authority, "publish_launch_attempt", fake_publish)
    authority.main(
        [
            "runtime-start",
            "--attempt-id",
            "attempt-runtime-cli",
            "--owner-pid",
            str(owner),
            "--owner-starttime",
            "2300",
            "--worktree-root",
            wt,
            "--repository-head",
            head,
            "--launch-basis-path",
            basis,
            "--launch-basis-sha256",
            basis_sha,
        ]
    )
    out = capsys.readouterr().out.strip()
    assert out == "18"

    authority.main(
        [
            "runtime-fail",
            "--attempt-id",
            "attempt-runtime-cli",
            "--owner-pid",
            str(owner),
            "--owner-starttime",
            "2300",
            "--worktree-root",
            wt,
            "--repository-head",
            head,
            "--launch-basis-path",
            basis,
            "--launch-basis-sha256",
            basis_sha,
            "--exit-code",
            "1",
            "--reason-code",
            "RUNTIME_NOT_PROVISIONED",
            "--detail",
            "x",
        ]
    )
    out = capsys.readouterr().out.strip()
    assert out == "18"
