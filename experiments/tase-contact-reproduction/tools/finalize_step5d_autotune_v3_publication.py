"""Consume one Step5d publication plan at an explicit local Git boundary."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import stat
import subprocess
import sys
from typing import Any, Mapping

from step5d_autotune_v3 import release_transition as transition


FINAL_SCHEMA = "step5d.autotune-v3/publication-finalization-v1"
CANONICAL_SHELL_RELATIVE = Path("scripts/step5d-autotune-v3.sh")
_PLAN_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _safe_plan_path(root: Path, path: Path) -> Path:
    experiment = root.resolve(strict=True)
    runs = experiment / "runs"
    if runs.is_symlink() or not runs.is_dir():
        raise transition.ReleaseTransitionError(
            "publication plan runs root is missing or symlinked"
        )
    try:
        resolved_runs = runs.resolve(strict=True)
    except OSError as exc:
        raise transition.ReleaseTransitionError(
            "publication plan runs root is missing or unsafe"
        ) from exc
    candidate = _absolute_without_resolving(path)
    try:
        candidate.relative_to(runs)
    except ValueError as exc:
        raise transition.ReleaseTransitionError(
            "publication plan must be under runs/"
        ) from exc
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise transition.ReleaseTransitionError(
            "publication plan path is missing or unsafe"
        ) from exc
    if resolved != candidate or not resolved.is_relative_to(resolved_runs):
        raise transition.ReleaseTransitionError(
            "publication plan path contains a symlink or escapes the resolved runs root"
        )
    return candidate


def _load_plan_bounded(path: Path, expected_sha256: str) -> dict[str, Any]:
    """Read and parse one plan without allowing an oversized raw document."""

    if not isinstance(expected_sha256, str) or _PLAN_SHA256.fullmatch(expected_sha256) is None:
        raise transition.ReleaseTransitionError(
            "expected publication plan SHA-256 must be a lowercase SHA-256"
        )
    if path.is_symlink():
        raise transition.ReleaseTransitionError("publication plan is unsafe")
    try:
        metadata = path.stat()
    except OSError as exc:
        raise transition.ReleaseTransitionError(
            "publication plan is missing or unsafe"
        ) from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise transition.ReleaseTransitionError("publication plan is not a regular file")
    if metadata.st_size > transition.PUBLICATION_PLAN_MAX_BYTES:
        raise transition.ReleaseTransitionError(
            "post-promotion publication plan exceeds 32KiB"
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise transition.ReleaseTransitionError(
            "publication plan cannot be read safely"
        ) from exc
    if len(raw) > transition.PUBLICATION_PLAN_MAX_BYTES:
        raise transition.ReleaseTransitionError(
            "post-promotion publication plan exceeds 32KiB"
        )
    observed_sha256 = hashlib.sha256(raw).hexdigest()
    if observed_sha256 != expected_sha256:
        raise transition.ReleaseTransitionError(
            "publication plan SHA-256 differs from the producer binding"
        )

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise transition.ReleaseTransitionError(
                    f"publication plan repeats JSON key {key!r}"
                )
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=lambda constant: (_ for _ in ()).throw(
                transition.ReleaseTransitionError(
                    f"publication plan contains non-finite {constant}"
                )
            ),
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise transition.ReleaseTransitionError(
            f"publication plan is invalid JSON: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise transition.ReleaseTransitionError("publication plan must be a JSON object")
    return value


def _absolute_without_resolving(path: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    return Path(os.path.abspath(expanded))


def _git(
    root: Path,
    *arguments: str,
    check: bool = True,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = _git_with_env(
        root,
        *arguments,
        check=check,
        env=env,
        text=True,
    )
    return completed


def _git_with_env(
    root: Path,
    *arguments: str,
    env: Mapping[str, str] | None = None,
    check: bool = True,
    text: bool = True,
) -> subprocess.CompletedProcess[str]:
    repository = root.resolve(strict=True).parents[1]
    command_env = os.environ.copy()
    if env is not None:
        command_env.update(env)
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        check=False,
        env=command_env,
    )
    if check and completed.returncode != 0:
        raise transition.ReleaseTransitionError(
            f"Git {' '.join(arguments)} failed: {completed.stderr.strip()}"
        )
    return completed


def _git_bytes(
    root: Path,
    *arguments: str,
    env: Mapping[str, str] | None = None,
) -> bytes:
    repository = root.resolve(strict=True).parents[1]
    command_env = os.environ.copy()
    if env is not None:
        command_env.update(env)
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        check=False,
        env=command_env,
    )
    if completed.returncode != 0:
        raise transition.ReleaseTransitionError(
            f"Git {' '.join(arguments)} failed: {completed.stderr.decode(errors='replace').strip()}"
        )
    return completed.stdout


def _repo_paths(root: Path, paths: tuple[str, ...]) -> tuple[str, ...]:
    repository = root.resolve(strict=True).parents[1]
    prefix = root.resolve(strict=True).relative_to(repository).as_posix()
    return tuple(f"{prefix}/{relative}" for relative in paths)


def _decode_path(raw: bytes, role: str) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise transition.ReleaseTransitionError(f"{role} is not valid UTF-8") from exc


def _repo_path(root: Path, raw: bytes, role: str) -> str:
    repository = root.resolve(strict=True).parents[1]
    experiment_prefix = root.resolve(strict=True).relative_to(repository).as_posix()
    expected = f"{experiment_prefix}/".encode()
    if not raw.startswith(expected):
        raise transition.ReleaseTransitionError(f"{role} is outside the experiment root")
    return transition._plan_relative(
        _decode_path(raw[len(expected):], role),
        role,
    )


def _nul_names(root: Path, raw: bytes, role: str) -> tuple[str, ...]:
    paths = tuple(
        sorted(
            _repo_path(root, item, role)
            for item in raw.split(b"\0")
            if item
        )
    )
    if len(paths) != len(set(paths)):
        raise transition.ReleaseTransitionError(f"{role} contains duplicate paths")
    return paths


def _index_tree(root: Path) -> str:
    return _git(root, "write-tree").stdout.strip()


def _current_publication_ref(root: Path) -> str:
    completed = _git_with_env(root, "symbolic-ref", "-q", "HEAD", check=False)
    if completed.returncode != 0 or not completed.stdout.strip():
        return "HEAD"
    return completed.stdout.strip()


def _transaction_staging_ref(plan: Mapping[str, Any]) -> str:
    transaction = str(plan["release"]["transaction_id"])
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", transaction).strip("._")
    if not safe:
        raise transition.ReleaseTransitionError("publication transaction id is invalid")
    return f"refs/heads/step5d-autotune-v3/publication/{safe}/{os.getpid()}"


def _cleanup_staging_ref(root: Path, ref: str) -> None:
    _git(root, "update-ref", "-d", ref, check=False)


def _remove_temporary_worktree(root: Path, worktree: Path | None) -> None:
    if worktree is None:
        return
    _git(root, "worktree", "remove", "--force", str(worktree), check=False)
    if worktree.exists():
        try:
            shutil.rmtree(worktree)
        except FileNotFoundError:
            pass


def _build_candidate_commit(
    root: Path,
    paths: tuple[str, ...],
    baseline_head: str,
    commit_message: str,
    staging_ref: str,
) -> str:
    with tempfile.TemporaryDirectory(
        prefix="step5d-autotune-v3-publication-"
    ) as temporary:
        staging_index = Path(temporary) / "index"
        env = {"GIT_INDEX_FILE": str(staging_index)}
        _git_with_env(root, "read-tree", baseline_head, env=env)
        _git_with_env(
            root,
            "add",
            "--",
            *_repo_paths(root, paths),
            env=env,
        )
        _verify_cached_delta(root, baseline_head, paths, env=env)
        tree = _git_with_env(root, "write-tree", env=env).stdout.strip()
        commit = _git_with_env(
            root,
            "commit-tree",
            tree,
            "-p",
            baseline_head,
            "-m",
            str(commit_message),
            env=env,
        ).stdout.strip()
        _git(root, "update-ref", staging_ref, commit)
        return commit


def _create_candidate_worktree(root: Path, commit: str) -> tuple[Path, Path]:
    worktree_root = root.resolve(strict=True)
    worktree_repo = Path(tempfile.mkdtemp(prefix="step5d-autotune-v3-publication-"))
    relative_root = worktree_root.relative_to(worktree_root.parents[1])
    _git(root, "worktree", "add", "--detach", str(worktree_repo), commit)
    return worktree_repo, worktree_repo / relative_root


def _publish_candidate(
    root: Path,
    publication_ref: str,
    baseline_head: str,
    candidate: str,
    paths: tuple[str, ...],
) -> None:
    _git(root, "read-tree", candidate)
    try:
        _git(root, "update-ref", publication_ref, candidate, baseline_head)
    except transition.ReleaseTransitionError as exc:
        restore = _git(root, "read-tree", baseline_head, check=False)
        if restore.returncode != 0:
            raise transition.ReleaseTransitionError(
                "publication branch CAS update failed and index restore to baseline failed"
            ) from exc
        raise

    if _git(root, "diff", "--cached", "--quiet", check=False).returncode != 0:
        rollback = _git(root, "update-ref", publication_ref, baseline_head, candidate, check=False)
        restore = _git(root, "read-tree", baseline_head, check=False)
        if rollback.returncode != 0:
            raise transition.ReleaseTransitionError(
                "publication branch advanced to candidate, cached state changed and rollback failed"
            )
        if restore.returncode != 0:
            raise transition.ReleaseTransitionError(
                "publication branch advanced to candidate, cached state changed and index restore to baseline failed"
            )
        raise transition.ReleaseTransitionError(
            "publication branch advanced to candidate, cached state differs and index was restored to baseline"
        )

    if _git(root, "diff", "--quiet", check=False).returncode != 0:
        rollback = _git(root, "update-ref", publication_ref, baseline_head, candidate, check=False)
        restore = _git(root, "read-tree", baseline_head, check=False)
        if rollback.returncode != 0:
            raise transition.ReleaseTransitionError(
                "publication branch advanced to candidate, worktree failed and rollback failed"
            )
        if restore.returncode != 0:
            raise transition.ReleaseTransitionError(
                "publication branch advanced to candidate, worktree failed and index restore to baseline failed"
            )
        raise transition.ReleaseTransitionError(
            "publication branch advanced to candidate, worktree failed and state restored to baseline"
        )


def _verify_before_stage(
    root: Path,
    plan: Mapping[str, Any],
) -> tuple[str, str, tuple[str, ...]]:
    baseline = plan["baseline"]
    baseline_head = str(baseline["head"])
    baseline_tree = str(baseline["tree"])
    head = _git(root, "rev-parse", "--verify", "HEAD").stdout.strip()
    tree = _git(root, "rev-parse", "--verify", "HEAD^{tree}").stdout.strip()
    index_tree = _index_tree(root)
    if head != baseline_head:
        raise transition.ReleaseTransitionError("publication baseline HEAD is not current")
    if tree != baseline_tree or index_tree != baseline_tree:
        raise transition.ReleaseTransitionError("publication baseline tree or index tree differs")
    paths = tuple(plan["publication"]["allowlist"])
    actual = transition._status_paths_nul(root)
    if actual != paths:
        raise transition.ReleaseTransitionError(
            f"publication status paths differ: expected {list(paths)}, got {list(actual)}"
        )
    expected = plan["publication"]["sha256"]
    for relative in paths:
        path = root / Path(*relative.split("/"))
        if path.is_symlink() or not path.exists() or not stat.S_ISREG(path.stat().st_mode):
            raise transition.ReleaseTransitionError(
                f"publication output is not a regular file: {relative}"
            )
        if transition._sha256(path, f"publication output {relative}") != expected[relative]:
            raise transition.ReleaseTransitionError(
                f"publication output SHA-256 differs: {relative}"
            )
    return baseline_head, baseline_tree, paths


def _verify_cached_delta(
    root: Path,
    baseline_head: str,
    paths: tuple[str, ...],
    *,
    env: Mapping[str, str] | None = None,
) -> None:
    actual = _nul_names(
        root,
        _git_bytes(
            root,
            "diff",
            "--cached",
            "--name-only",
            "--no-renames",
            "-z",
            baseline_head,
            "--",
            env=env,
        ),
        "cached Git delta",
    )
    if actual != paths:
        raise transition.ReleaseTransitionError(
            f"cached Git delta differs: expected {list(paths)}, got {list(actual)}"
        )
    for relative in paths:
        records = _git_bytes(
            root,
            "ls-files",
            "--stage",
            "-z",
            "--",
            *_repo_paths(root, (relative,)),
            env=env,
        ).split(b"\0")
        records = [record for record in records if record]
        if len(records) != 1 or b"\t" not in records[0]:
            raise transition.ReleaseTransitionError(f"cached Git entry is unsafe: {relative}")
        metadata, raw_path = records[0].split(b"\t", 1)
        fields = metadata.split()
        expected_repo_path = _repo_paths(root, (relative,))[0]
        if len(fields) != 3 or _decode_path(raw_path, "cached Git path") != expected_repo_path:
            raise transition.ReleaseTransitionError(f"cached Git entry differs: {relative}")
        if fields[0] not in {b"100644", b"100755"}:
            raise transition.ReleaseTransitionError(f"cached special path is unsafe: {relative}")
        expected_blob = _git(
            root,
            "hash-object",
            "--",
            *_repo_paths(root, (relative,)),
        ).stdout.strip()
        if fields[1].decode("ascii", errors="ignore") != expected_blob:
            raise transition.ReleaseTransitionError(f"cached Git hash differs: {relative}")


def _verify_commit(
    root: Path,
    commit: str,
    baseline_head: str,
    paths: tuple[str, ...],
    *,
    require_clean: bool = True,
) -> dict[str, str]:
    parents = _git(root, "rev-list", "--parents", "-n", "1", commit).stdout.split()
    if parents != [commit, baseline_head]:
        raise transition.ReleaseTransitionError("publication commit is not a single direct child of baseline")
    tree = _git(root, "rev-parse", f"{commit}^{{tree}}").stdout.strip()
    actual = _nul_names(
        root,
        _git_bytes(root, "diff-tree", "--root", "--no-commit-id", "--name-only", "--no-renames", "-r", "-z", commit),
        "committed Git delta",
    )
    if actual != paths:
        raise transition.ReleaseTransitionError(
            f"committed Git delta differs: expected {list(paths)}, got {list(actual)}"
        )
    if require_clean and transition._status_paths_nul(root):
        raise transition.ReleaseTransitionError("publication commit did not leave a clean worktree")
    return {"oid": commit, "parent": baseline_head, "tree": tree}


def _canonical_shell(root: Path, candidate: Path) -> Path:
    expected = root / CANONICAL_SHELL_RELATIVE
    if (
        expected.is_symlink()
        or not expected.is_file()
        or not stat.S_ISREG(expected.stat().st_mode)
        or not os.access(expected, os.X_OK)
    ):
        raise transition.ReleaseTransitionError(
            "canonical shell is missing, non-regular, symlinked, or not executable"
        )
    if candidate.is_symlink():
        raise transition.ReleaseTransitionError(
            "canonical shell argument must not be a symlink"
        )
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise transition.ReleaseTransitionError(
            "canonical shell argument is missing or unsafe"
        ) from exc
    if resolved != expected.resolve(strict=True):
        raise transition.ReleaseTransitionError(
            "canonical shell argument is not the experiment canonical shell"
        )
    return expected


def _final_payload(
    *,
    plan_path: Path,
    plan: Mapping[str, Any],
    status: str,
    commit: Mapping[str, str],
    revalidate_rc: int,
    recovery: str,
    reused_commit: bool = False,
) -> dict[str, Any]:
    return {
        "schema": FINAL_SCHEMA,
        "status": status,
        "observed_stages": [
            "promotion_success",
            "allowlisted_publication_boundary",
            "existing_exact_direct_child" if reused_commit else "local_commit",
            "clean_revalidate" if revalidate_rc == 0 else "revalidate_failed_clean_commit_preserved",
        ],
        "plan": {
            "path": str(plan_path),
            "transaction_id": plan["release"]["transaction_id"],
            "baseline_head": plan["baseline"]["head"],
            "baseline_tree": plan["baseline"]["tree"],
        },
        "commit": dict(commit),
        "revalidate": {"command": "scripts/step5d-autotune-v3.sh revalidate-current", "returncode": revalidate_rc},
        "recovery": recovery,
        "push": False,
    }


def _run_revalidate(root: Path, plan: Mapping[str, Any], canonical_shell: Path) -> int:
    evidence = root / "runs/step5d_autotune_v3" / f"publication-revalidate-{plan['release']['transaction_id']}.json"
    command = [
        str(canonical_shell),
        "revalidate-current",
        "--artifact-dir",
        str(root / "programs/step5/step5d"),
        "--evidence-output",
        str(evidence),
    ]
    return subprocess.run(
        command,
        cwd=str(root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    ).returncode


def _finish_revalidation(
    root: Path,
    plan_path: Path,
    plan: Mapping[str, Any],
    canonical_shell: Path,
    commit: Mapping[str, str],
    *,
    reused_commit: bool,
) -> tuple[int, dict[str, Any]]:
    baseline_head = str(plan["baseline"]["head"])
    paths = tuple(plan["publication"]["allowlist"])
    revalidate_rc = _run_revalidate(root, plan, canonical_shell)
    try:
        _verify_commit(
            root,
            commit["oid"],
            baseline_head,
            paths,
            require_clean=False,
        )
        transition.validate_post_promotion_publication_plan(
            root,
            plan,
            require_clean=True,
            require_publication=True,
        )
    except transition.ReleaseTransitionError as exc:
        raise transition.ReleaseTransitionError(
            f"revalidate failed and publication recovery is not clean: {exc}"
        ) from exc
    if revalidate_rc == 0:
        return 0, _final_payload(
            plan_path=plan_path,
            plan=plan,
            status="existing_commit_revalidated" if reused_commit else "ok",
            commit=commit,
            revalidate_rc=0,
            recovery=(
                "existing_exact_direct_child_reused; no second commit"
                if reused_commit
                else "none"
            ),
            reused_commit=reused_commit,
        )
    return 1, _final_payload(
        plan_path=plan_path,
        plan=plan,
        status="existing_commit_revalidate_failed" if reused_commit else "revalidate_failed",
        commit=commit,
        revalidate_rc=revalidate_rc,
        recovery=(
            "existing_exact_direct_child_preserved; rerun canonical revalidate-current; no automatic retry"
            if reused_commit
            else "commit_preserved; rerun canonical revalidate-current with the same plan; no automatic retry"
        ),
        reused_commit=reused_commit,
    )


def finalize(
    root: Path,
    plan_path: Path,
    canonical_shell: Path,
    expected_sha256: str,
) -> tuple[int, dict[str, Any]]:
    plan_path = _safe_plan_path(root, plan_path)
    canonical_shell = _canonical_shell(root, canonical_shell)
    plan = _load_plan_bounded(plan_path, expected_sha256)
    transition.validate_post_promotion_publication_plan(root, plan)
    baseline_head = str(plan["baseline"]["head"])
    paths = tuple(plan["publication"]["allowlist"])
    current_head = _git(root, "rev-parse", "--verify", "HEAD").stdout.strip()
    if current_head != baseline_head:
        existing_commit = _verify_commit(
            root,
            current_head,
            baseline_head,
            paths,
            require_clean=False,
        )
        transition.validate_post_promotion_publication_plan(
            root,
            plan,
            require_clean=True,
            require_publication=True,
        )
        return _finish_revalidation(
            root,
            plan_path,
            plan,
            canonical_shell,
            existing_commit,
            reused_commit=True,
        )
    baseline_head, _baseline_tree, paths = _verify_before_stage(root, plan)
    staging_ref = _transaction_staging_ref(plan)
    publication_ref = _current_publication_ref(root)
    if publication_ref == "HEAD":
        raise transition.ReleaseTransitionError("publication branch is detached and cannot be updated")
    staging_worktree: Path | None = None
    staging_root: Path | None = None
    try:
        commit = _build_candidate_commit(
            root=root,
            paths=paths,
            baseline_head=baseline_head,
            commit_message=str(plan["publication"]["commit_message"]),
            staging_ref=staging_ref,
        )
        staging_worktree, staging_root = _create_candidate_worktree(root, commit)
        staging_shell = _canonical_shell(staging_root, staging_root / CANONICAL_SHELL_RELATIVE)
        commit_info = _verify_commit(
            staging_root,
            commit,
            baseline_head,
            paths,
            require_clean=False,
        )
        transition.validate_post_promotion_publication_plan(
            staging_root,
            plan,
            require_clean=True,
            require_publication=True,
        )
        revalidate_rc, payload = _finish_revalidation(
            staging_root,
            plan_path,
            plan,
            staging_shell,
            commit_info,
            reused_commit=False,
        )
        if revalidate_rc == 0:
            _publish_candidate(
                root=root,
                publication_ref=publication_ref,
                baseline_head=baseline_head,
                candidate=commit,
                paths=paths,
            )
            return revalidate_rc, payload
        return revalidate_rc, payload
    finally:
        _cleanup_staging_ref(root, staging_ref)
        _remove_temporary_worktree(root, staging_worktree)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--canonical-shell", type=Path, required=True)
    args = parser.parse_args(argv)
    root = args.root.resolve(strict=True)
    rc, payload = finalize(root, args.plan, args.canonical_shell, args.plan_sha256)
    encoded = (
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )
    if len(encoded.encode("ascii")) > transition.PUBLICATION_PLAN_MAX_BYTES:
        raise RuntimeError("publication final JSON exceeds 32KiB")
    print(encoded, end="")
    return rc


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, transition.ReleaseTransitionError) as exc:
        print(f"publication finalization refused: {exc}", file=sys.stderr)
        raise SystemExit(64)
