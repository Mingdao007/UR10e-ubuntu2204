from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import shutil
import sys
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step5d_autotune_tp_v3 as builder  # noqa: E402
import promote_step5d_r009_atomic_release as promotion  # noqa: E402
import run_step5d_autotune_v3_tp_transaction as transaction  # noqa: E402
import finalize_step5d_autotune_v3_publication as publication_consumer  # noqa: E402
from step5d_autotune_v3 import delivery_observation as delivery_module  # noqa: E402
from step5d_autotune_v3.delivery_observation import (  # noqa: E402
    DeliveryObservationError,
    build_delivery_observation,
    delivery_index_path,
    resolve_delivery_observation,
    validate_delivery_observation,
    write_indexed_delivery_observation,
)
from step5d_autotune_v3.release_identity import load_local_release_candidate  # noqa: E402
from step5d_autotune_v3 import release_transition as transition  # noqa: E402

PROGRAM = "step5d_strict_rnn_autotune_v3_r999"
TEST_STAMP = "2026-07-23T0000HKT_" + PROGRAM.upper()


def test_upload_runner_reports_bounded_child_failure_output() -> None:
    failure = subprocess.CalledProcessError(
        1,
        ["controller-helper.py", "readback"],
        output=("x" * 13_000) + "\nNo route to host\n",
        stderr="readback failed\n",
    )
    with (
        mock.patch.object(
            transaction.upload.subprocess,
            "run",
            side_effect=failure,
        ),
        pytest.raises(RuntimeError) as raised,
    ):
        transaction.upload.run(
            ["controller-helper.py", "readback"],
            dry_run=False,
            capture=True,
        )

    message = str(raised.value)
    assert "bounded child output tail" in message
    assert "No route to host" in message
    assert "readback failed" in message
    assert len(message) < 12_500


def _write_receipt(
    root: Path,
    local: Path,
    *,
    suffix: str,
    transaction_id: str,
    checked_at: str,
    stamp: str,
    controller: str = "root@192.168.1.18",
    delivery_mode: str = "full_upload_readback",
) -> Path:
    readback = root / "runs" / f"controller_readback_{PROGRAM}_{suffix}"
    readback.mkdir(parents=True)
    hashes: dict[str, str] = {}
    for extension in promotion.EXTENSIONS:
        source = local / f"{PROGRAM}{extension}"
        data = source.read_bytes()
        (readback / source.name).write_bytes(data)
        hashes[extension] = hashlib.sha256(data).hexdigest()
    manifest = {
        "status": "controller read-back verified",
        "controller": controller,
        "target_dir": promotion.TARGET_DIR,
        "validation": {
            "stamp": stamp,
            "program": PROGRAM,
            "target_dir": promotion.TARGET_DIR,
            "script_node_path": (
                f"{promotion.TARGET_DIR}/{PROGRAM}.script"
            ),
            "script_sha256": hashes[".script"],
            "txt_sha256": hashes[".txt"],
            "urp_sha256": hashes[".urp"],
        },
        "sha256": {
            role: dict(hashes) for role in ("local", "controller", "readback")
        },
        "delivery_mode": delivery_mode,
        "fresh_controller_sha_verified": True,
        "fresh_controller_checked_at": checked_at,
        "readback_source": "fresh_controller_get",
        "upload_transaction_id": transaction_id,
    }
    path = readback / "manifest.json"
    path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    return path


def _fixture_manifest(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "experiment"
    local = root / promotion.PACKAGE_DIR
    local.mkdir(parents=True)
    builder.write_triplet(
        local,
        TEST_STAMP, program_id=PROGRAM
    )
    return root, _write_receipt(
        root,
        local,
        suffix="fixture",
        transaction_id="a" * 32,
        checked_at="2026-07-21T11:00:33+08:00",
        stamp="fixture",
    )


def _release_for_receipt(path: Path) -> SimpleNamespace:
    receipt = json.loads(path.read_text(encoding="utf-8"))
    return SimpleNamespace(
        manifest_sha256="f" * 64,
        program_id=PROGRAM,
        controller_target=f"{promotion.TARGET_DIR}/{PROGRAM}.urp",
        artifact_sha256=dict(receipt["sha256"]["readback"]),
    )


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source.read_bytes())


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _transition_fixture(
    tmp_path: Path,
) -> tuple[Path, SimpleNamespace, SimpleNamespace, Path]:
    repository = tmp_path / "repository"
    root = repository / "experiments/tase-contact-reproduction"
    root.mkdir(parents=True)
    initial_receiver = root / "config/step5d/parameter_receiver_initial.json"
    initial_receiver.parent.mkdir(parents=True, exist_ok=True)
    initial_receiver.write_text(
        '{"schema":"step5d.parameter-receiver/initial-v1","revision":1}\n',
        encoding="utf-8",
    )
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "test@example.invalid")
    _git(repository, "config", "user.name", "release transition test")
    (root / ".gitignore").write_text("runs/\n", encoding="utf-8")
    basis_manifest = root / "config/step5d/releases/basis/manifest.json"
    basis_manifest.parent.mkdir(parents=True)
    basis_manifest.write_text('{"basis":true}\n', encoding="utf-8")
    basis_manifest_sha256 = hashlib.sha256(
        basis_manifest.read_bytes()
    ).hexdigest()
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "basis release")
    triplet = {
        ".script": "1" * 64,
        ".txt": "2" * 64,
        ".urp": "3" * 64,
    }
    target = f"{promotion.TARGET_DIR}/{PROGRAM}.urp"
    basis = SimpleNamespace(
        manifest_path=basis_manifest.relative_to(root).as_posix(),
        manifest_sha256=basis_manifest_sha256,
        program_id=PROGRAM,
        controller_target=target,
        artifact_sha256=dict(triplet),
    )
    candidate = SimpleNamespace(
        manifest_path="config/step5d/releases/candidate/manifest.json",
        manifest_sha256="f" * 64,
        program_id=PROGRAM,
        controller_target=target,
        artifact_sha256=dict(triplet),
    )
    receipt = tmp_path / "prior-full-readback.json"
    receipt.write_text(
        json.dumps(
            {
                "status": "controller read-back verified",
                "target_dir": promotion.TARGET_DIR,
                "validation": {
                    "program": PROGRAM,
                    "script_node_path": target.replace(".urp", ".script"),
                    "script_sha256": triplet[".script"],
                    "txt_sha256": triplet[".txt"],
                    "urp_sha256": triplet[".urp"],
                },
                "sha256": {
                    role: dict(triplet)
                    for role in ("local", "controller", "readback")
                },
                "delivery_mode": "full_upload_readback",
                "fresh_controller_sha_verified": True,
                "fresh_controller_checked_at": "2026-07-24T13:30:31+08:00",
                "readback_source": "fresh_controller_get",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return root, candidate, basis, receipt


def test_tracked_basis_imports_exact_prior_full_readback(
    tmp_path: Path,
) -> None:
    root, candidate, basis, receipt = _transition_fixture(tmp_path)

    path, payload = transition.create_delivery_basis(
        root,
        candidate_release=candidate,
        basis_release=basis,
        prior_full_receipt=receipt,
    )

    assert path == transition.delivery_basis_path(root, candidate)
    assert payload["candidate_release_manifest_sha256"] == "f" * 64
    prior = payload["prior_full_readback_receipt"]
    imported = root / prior["path"]
    assert imported.name == f"{prior['sha256']}.json"
    assert transition.delivery_basis_reference(
        root,
        release=candidate,
    ) == {
        "path": path.relative_to(root).as_posix(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def test_existing_receipt_requires_exact_tracked_basis(
    tmp_path: Path,
) -> None:
    root, candidate, basis, prior = _transition_fixture(tmp_path)
    transition.create_delivery_basis(
        root,
        candidate_release=candidate,
        basis_release=basis,
        prior_full_receipt=prior,
    )
    reference = transition.delivery_basis_reference(root, release=candidate)

    assert transition.require_receipt_delivery_basis(
        root,
        {"delivery_basis": reference},
        release=candidate,
    ) == reference
    with pytest.raises(
        transition.ReleaseTransitionError,
        match="basis reference differs",
    ):
        transition.require_receipt_delivery_basis(
            root,
            {"delivery_basis": {**reference, "sha256": "0" * 64}},
            release=candidate,
        )


def test_runtime_lineage_binds_clean_exact_head_and_tree(
    tmp_path: Path,
) -> None:
    root, candidate, basis, prior = _transition_fixture(tmp_path)
    transition.create_delivery_basis(
        root,
        candidate_release=candidate,
        basis_release=basis,
        prior_full_receipt=prior,
    )
    repository = root.parents[1]
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "publish candidate basis")

    path, payload = transition.write_publication_lineage(
        root,
        release=candidate,
    )

    assert path.is_file()
    assert payload["release_commit"] == _git(repository, "rev-parse", "HEAD")
    assert payload["release_tree"] == _git(repository, "rev-parse", "HEAD^{tree}")
    assert transition.resolve_publication_lineage(
        root,
        release=candidate,
    )[1] == payload

    (root / ".gitignore").write_text("runs/\nchanged\n", encoding="utf-8")
    with pytest.raises(
        transition.ReleaseTransitionError,
        match="no valid publication lineage",
    ):
        transition.resolve_publication_lineage(root, release=candidate)


def _publication_plan_fixture(
    tmp_path: Path,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    repository = tmp_path / "publication-repository"
    root = repository / "experiments/tase-contact-reproduction"
    root.mkdir(parents=True)
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "test@example.invalid")
    _git(repository, "config", "user.name", "publication plan test")
    (root / ".gitignore").write_text("runs/\n", encoding="utf-8")
    canonical_shell = root / "scripts/step5d-autotune-v3.sh"
    canonical_shell.parent.mkdir(parents=True, exist_ok=True)
    canonical_shell.write_text(
        "#!/bin/sh\n"
        "if [ -n \"${FAKE_REVALIDATE_LOG:-}\" ]; then printf 'revalidate\\n' >> \"$FAKE_REVALIDATE_LOG\"; fi\n"
        "exit \"${FAKE_REVALIDATE_RC:-0}\"\n",
        encoding="utf-8",
    )
    canonical_shell.chmod(0o755)
    tracked_mirror = root / "config/current_stage.json"
    tracked_mirror.parent.mkdir(parents=True, exist_ok=True)
    tracked_mirror.write_text('{"selected":"old"}\n', encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "clean publication baseline")
    baseline = transition.git_snapshot(root)

    manifest_bytes = (
        json.dumps(
            {"identity": {"program_id": PROGRAM}},
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    digest = hashlib.sha256(manifest_bytes).hexdigest()
    bundle = root / "config/step5d/releases" / digest
    bundle.mkdir(parents=True)
    (bundle / "manifest.json").write_bytes(manifest_bytes)
    (bundle / "config/current_stage.json").parent.mkdir(parents=True)
    (bundle / "config/current_stage.json").write_text(
        '{"selected":"step5d_strict_rnn_autotune_v3_r999"}\n',
        encoding="utf-8",
    )
    pointer = root / "config/step5d/current.json"
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(
        json.dumps(
            {
                "schema": "step5d.autotune-v3/current-release-pointer-v1",
                "manifest_path": f"config/step5d/releases/{digest}/manifest.json",
                "manifest_sha256": digest,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    mirror = tracked_mirror
    mirror.write_bytes((bundle / "config/current_stage.json").read_bytes())
    basis = root / "config/step5d/delivery-bases" / f"{digest}.json"
    basis.parent.mkdir(parents=True, exist_ok=True)
    basis.write_text('{"basis":"fixture"}\n', encoding="utf-8")
    plan = transition.build_post_promotion_publication_plan(
        root,
        release_manifest_sha256=digest,
        program_id=PROGRAM,
        transaction_id="a" * 32,
        baseline=baseline,
        compatibility_targets={
            "config/current_stage.json": "config/current_stage.json",
        },
        additional_outputs={
            basis.relative_to(root).as_posix(): basis,
        },
    )
    return root, plan, baseline


def test_post_promotion_plan_removes_dirty_revalidate_and_agent_roundtrip(
    tmp_path: Path,
) -> None:
    root, plan, _baseline = _publication_plan_fixture(tmp_path)

    assert plan["schema"] == transition.PUBLICATION_PLAN_SCHEMA
    assert plan["trace"]["observed_stages"] == ["promotion_success"]
    assert plan["trace"]["timing_scope"].endswith("controller_timing_unmeasured")
    encoded = transition._canonical_bytes(plan)
    assert len(encoded) <= transition.PUBLICATION_PLAN_MAX_BYTES
    transition.validate_post_promotion_publication_plan(root, plan)
    with pytest.raises(
        transition.ReleaseTransitionError,
        match="tracked repository state is not clean",
    ):
        transition.validate_post_promotion_publication_plan(
            root,
            plan,
            require_clean=True,
        )


def test_post_promotion_plan_binds_allowlist_and_clean_publication(
    tmp_path: Path,
) -> None:
    root, plan, _baseline = _publication_plan_fixture(tmp_path)
    output = root / "runs/post-promotion-plan.json"
    written = transition.write_post_promotion_publication_plan(root, output, plan)
    assert written == output
    assert len(written.read_bytes()) <= transition.PUBLICATION_PLAN_MAX_BYTES

    repository = root.parents[1]
    for relative in plan["publication"]["allowlist"]:
        _git(repository, "add", str(root / relative))
    _git(repository, "commit", "-qm", plan["publication"]["commit_message"])
    validated = transition.validate_post_promotion_publication_plan(
        root,
        plan,
        require_clean=True,
        require_publication=True,
    )
    assert validated == plan


def test_post_promotion_plan_binds_preexisting_bundle_and_new_dirty_output(
    tmp_path: Path,
) -> None:
    root, original_plan, _ = _publication_plan_fixture(tmp_path)
    repository = root.parents[1]
    for relative in original_plan["publication"]["allowlist"]:
        _git(repository, "add", str(root / relative))
    _git(repository, "commit", "-qm", "preexisting immutable release bundle")
    baseline = transition.git_publication_snapshot(root)
    post_basis = root / "config/step5d/delivery-bases/post-promotion.json"
    post_basis.write_text('{"basis":"post"}\n', encoding="utf-8")
    plan = transition.build_post_promotion_publication_plan(
        root,
        release_manifest_sha256=original_plan["release"]["manifest_sha256"],
        program_id=PROGRAM,
        transaction_id="b" * 32,
        baseline=baseline,
        compatibility_targets={
            "config/current_stage.json": "config/current_stage.json",
        },
        additional_outputs={
            post_basis.relative_to(root).as_posix(): post_basis,
        },
    )

    assert plan["publication"]["allowlist"] == [
        "config/step5d/delivery-bases/post-promotion.json"
    ]
    assert plan["publication"]["changed_paths"] == plan["publication"]["allowlist"]
    assert set(plan["publication"]["sha256"]) >= set(
        plan["publication"]["allowlist"]
    )
    assert len(transition._canonical_bytes(plan)) <= transition.PUBLICATION_PLAN_MAX_BYTES


def test_post_promotion_plan_fails_closed_on_unallowlisted_or_mutated_output(
    tmp_path: Path,
) -> None:
    root, plan, _baseline = _publication_plan_fixture(tmp_path)
    unrelated = root / "config/unrelated-publication.txt"
    unrelated.parent.mkdir(parents=True, exist_ok=True)
    unrelated.write_text("must fail closed\n", encoding="utf-8")
    with pytest.raises(
        transition.ReleaseTransitionError,
        match="exceed allowlist",
    ):
        transition.build_post_promotion_publication_plan(
            root,
            release_manifest_sha256=plan["release"]["manifest_sha256"],
            program_id=PROGRAM,
            transaction_id="a" * 32,
            baseline=plan["baseline"] | {"tracked_clean": True},
            compatibility_targets={
                "config/current_stage.json": "config/current_stage.json",
            },
        )

    unrelated.unlink()
    (root / "config/current_stage.json").write_text(
        '{"selected":"tampered"}\n',
        encoding="utf-8",
    )
    with pytest.raises(
        transition.ReleaseTransitionError,
        match="SHA-256 differs",
    ):
        transition.validate_post_promotion_publication_plan(root, plan)


def _write_fake_revalidate(path: Path, *, returncode: int) -> Path:
    path.write_text(
        "#!/bin/sh\n"
        "printf 'revalidate\n' >> \"$FAKE_REVALIDATE_LOG\"\n"
        f"exit {returncode}\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _prepare_consumer_fixture(tmp_path: Path) -> tuple[Path, dict[str, Any], Path, Path]:
    root, plan, _baseline = _publication_plan_fixture(tmp_path)
    plan_path = root / "runs/post-promotion-publication.json"
    transition.write_post_promotion_publication_plan(root, plan_path, plan)
    return root, plan, plan_path, root / "scripts/step5d-autotune-v3.sh"


def _plan_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_publication_consumer_end_to_end_commits_then_cleanly_revalidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, plan, plan_path, canonical_shell = _prepare_consumer_fixture(tmp_path)
    log = tmp_path / "revalidate.log"
    monkeypatch.setenv("FAKE_REVALIDATE_LOG", str(log))
    real_git = shutil.which("git")
    assert real_git is not None
    git_log = tmp_path / "git.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {git_log}\n"
        "case \" $* \" in *' push '*) exit 99;; esac\n"
        f"exec {real_git} \"$@\"\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")

    rc, payload = publication_consumer.finalize(
        root, plan_path, canonical_shell, _plan_sha256(plan_path)
    )

    assert rc == 0
    assert payload["schema"] == publication_consumer.FINAL_SCHEMA
    assert payload["status"] == "ok"
    assert payload["observed_stages"][-1] == "clean_revalidate"
    assert payload["commit"]["parent"] == plan["baseline"]["head"]
    assert payload["push"] is False
    assert all(" push " not in f" {line} " for line in git_log.read_text(encoding="utf-8").splitlines())
    assert log.read_text(encoding="utf-8").splitlines() == ["revalidate"]
    assert _git(root.parents[1], "status", "--porcelain=v2", "-z") == ""
    assert _git(root.parents[1], "rev-parse", "HEAD^") == plan["baseline"]["head"]


def test_publication_consumer_preserves_exact_commit_when_revalidate_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, plan, plan_path, canonical_shell = _prepare_consumer_fixture(tmp_path)
    log = tmp_path / "revalidate-fails.log"
    monkeypatch.setenv("FAKE_REVALIDATE_LOG", str(log))
    monkeypatch.setenv("FAKE_REVALIDATE_RC", "23")

    expected_sha256 = _plan_sha256(plan_path)
    rc, payload = publication_consumer.finalize(
        root, plan_path, canonical_shell, expected_sha256
    )

    assert rc == 1
    assert payload["status"] == "revalidate_failed"
    assert payload["observed_stages"][-1] == "revalidate_failed_clean_commit_preserved"
    assert "no automatic retry" in payload["recovery"]
    assert log.read_text(encoding="utf-8").splitlines() == ["revalidate"]
    assert _git(root.parents[1], "status", "--porcelain=v2", "-z") == ""
    assert _git(root.parents[1], "rev-parse", "HEAD^") == plan["baseline"]["head"]
    monkeypatch.setenv("FAKE_REVALIDATE_RC", "0")
    rc2, payload2 = publication_consumer.finalize(
        root, plan_path, canonical_shell, expected_sha256
    )

    assert rc2 == 0
    assert payload2["status"] == "existing_commit_revalidated"
    assert payload2["recovery"] == "existing_exact_direct_child_reused; no second commit"
    assert payload2["observed_stages"][2] == "existing_exact_direct_child"
    assert payload2["observed_stages"][-1] == "clean_revalidate"
    assert log.read_text(encoding="utf-8").splitlines() == ["revalidate", "revalidate"]
    assert _git(root.parents[1], "rev-list", "--count", "HEAD") == "2"
    assert _git(root.parents[1], "status", "--porcelain=v2", "-z") == ""


def test_publication_consumer_rejects_external_or_symlink_canonical_shell(
    tmp_path: Path,
) -> None:
    root, _plan, plan_path, canonical_shell = _prepare_consumer_fixture(tmp_path)
    external = _write_fake_revalidate(tmp_path / "external.sh", returncode=0)
    with pytest.raises(
        transition.ReleaseTransitionError,
        match="not the experiment canonical shell",
    ):
        publication_consumer.finalize(
            root, plan_path, external, _plan_sha256(plan_path)
        )

    symlink = root / "scripts/alternate-shell.sh"
    symlink.symlink_to(canonical_shell.name)
    with pytest.raises(
        transition.ReleaseTransitionError,
        match="must not be a symlink",
    ):
        publication_consumer.finalize(
            root, plan_path, symlink, _plan_sha256(plan_path)
        )


@pytest.mark.parametrize("mutation", ["baseline_head", "tree", "index", "extra", "missing", "hash", "rename", "special"])
def test_publication_consumer_fail_closed_matrix(
    tmp_path: Path,
    mutation: str,
) -> None:
    root, plan, plan_path, fake = _prepare_consumer_fixture(tmp_path)
    target = root / "config/current_stage.json"
    if mutation == "baseline_head":
        plan["baseline"]["head"] = "0" * 40
        plan_path.write_bytes(transition._canonical_bytes(plan))
    elif mutation == "tree":
        plan["baseline"]["tree"] = "0" * 40
        plan_path.write_bytes(transition._canonical_bytes(plan))
    elif mutation == "index":
        extra = root / "config/index-pollution.txt"
        extra.write_text("pollution\n", encoding="utf-8")
        _git(root.parents[1], "add", str(extra))
    elif mutation == "extra":
        (root / "config/extra.txt").write_text("extra\n", encoding="utf-8")
    elif mutation == "missing":
        target.unlink()
    elif mutation == "hash":
        target.write_text("mutated\n", encoding="utf-8")
    elif mutation == "rename":
        _git(root.parents[1], "mv", str(target), str(root / "config/renamed-stage.json"))
    elif mutation == "special":
        import os

        os.mkfifo(root / "config/special.fifo")
    with pytest.raises((publication_consumer.transition.ReleaseTransitionError, RuntimeError)):
        publication_consumer.finalize(
            root, plan_path, fake, _plan_sha256(plan_path)
        )


def test_publication_plan_rejects_over_32k_fixed_json(tmp_path: Path) -> None:
    root, plan, _baseline = _publication_plan_fixture(tmp_path)
    plan["publication"]["commit_message"] = "x" * (32 * 1024)
    with pytest.raises(transition.ReleaseTransitionError, match="32KiB"):
        transition.write_post_promotion_publication_plan(
            root,
            root / "runs/oversize-plan.json",
            plan,
        )


def test_publication_plan_raw_json_is_ascii_safe_and_bounded(tmp_path: Path) -> None:
    root, plan, _baseline = _publication_plan_fixture(tmp_path)
    plan["publication"]["commit_message"] = "发布 Step5d ✓"
    output = root / "runs/non-ascii-plan.json"
    written = transition.write_post_promotion_publication_plan(root, output, plan)
    raw = written.read_bytes()

    assert len(raw) <= transition.PUBLICATION_PLAN_MAX_BYTES
    assert raw.decode("ascii")
    assert json.loads(raw) == plan


def test_publication_consumer_rejects_oversized_raw_plan_before_git_or_revalidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, plan, plan_path, canonical_shell = _prepare_consumer_fixture(tmp_path)
    canonical = transition._canonical_bytes(plan)
    oversized = b" " + canonical + b" " * (
        transition.PUBLICATION_PLAN_MAX_BYTES - len(canonical)
    )
    assert len(oversized) == transition.PUBLICATION_PLAN_MAX_BYTES + 1
    assert json.loads(oversized) == plan
    plan_path.write_bytes(oversized)
    before_head = _git(root.parents[1], "rev-parse", "HEAD")
    before_status = _git(root.parents[1], "status", "--porcelain=v2", "-z")
    log = tmp_path / "oversized-revalidate.log"
    monkeypatch.setenv("FAKE_REVALIDATE_LOG", str(log))

    with pytest.raises(transition.ReleaseTransitionError, match="32KiB"):
        publication_consumer.finalize(
            root, plan_path, canonical_shell, _plan_sha256(plan_path)
        )

    assert _git(root.parents[1], "rev-parse", "HEAD") == before_head
    assert _git(root.parents[1], "status", "--porcelain=v2", "-z") == before_status
    assert not log.exists()


def test_publication_consumer_accepts_exact_32k_raw_plan_boundary(
    tmp_path: Path,
) -> None:
    root, plan, plan_path, _canonical_shell = _prepare_consumer_fixture(tmp_path)
    canonical = transition._canonical_bytes(plan)
    exact = b" " + canonical + b" " * (
        transition.PUBLICATION_PLAN_MAX_BYTES - len(canonical) - 1
    )
    assert len(exact) == transition.PUBLICATION_PLAN_MAX_BYTES
    assert publication_consumer._load_plan_bounded(
        plan_path, _plan_sha256(plan_path)
    ) == plan
    plan_path.write_bytes(exact)
    assert publication_consumer._load_plan_bounded(
        plan_path, _plan_sha256(plan_path)
    ) == plan


def test_publication_consumer_rejects_replaced_plan_before_git_or_revalidate(
    tmp_path: Path,
) -> None:
    root, plan, plan_path, canonical_shell = _prepare_consumer_fixture(tmp_path)
    expected_sha256 = _plan_sha256(plan_path)
    replaced = json.loads(plan_path.read_text(encoding="utf-8"))
    replaced["publication"]["commit_message"] = "attacker-controlled message"
    plan_path.write_bytes(transition._canonical_bytes(replaced))
    before_head = _git(root.parents[1], "rev-parse", "HEAD")
    before_status = _git(root.parents[1], "status", "--porcelain=v2", "-z")
    before_log = _git(root.parents[1], "log", "-1", "--format=%H:%s")

    with (
        mock.patch.object(publication_consumer, "_git") as git,
        mock.patch.object(publication_consumer, "_run_revalidate") as revalidate,
        pytest.raises(
            transition.ReleaseTransitionError,
            match="SHA-256 differs from the producer binding",
        ),
    ):
        publication_consumer.finalize(
            root, plan_path, canonical_shell, expected_sha256
        )

    git.assert_not_called()
    revalidate.assert_not_called()
    assert _git(root.parents[1], "rev-parse", "HEAD") == before_head
    assert _git(root.parents[1], "status", "--porcelain=v2", "-z") == before_status
    assert _git(root.parents[1], "log", "-1", "--format=%H:%s") == before_log


def test_publication_consumer_rejects_parent_symlink_escape_before_git_or_revalidate(
    tmp_path: Path,
) -> None:
    root, plan, plan_path, canonical_shell = _prepare_consumer_fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_plan = outside / "plan.json"
    outside_plan.write_bytes(plan_path.read_bytes())
    (root / "runs/parent-link").symlink_to(outside, target_is_directory=True)
    before_head = _git(root.parents[1], "rev-parse", "HEAD")
    before_status = _git(root.parents[1], "status", "--porcelain=v2", "-z")

    with (
        mock.patch.object(publication_consumer, "_git") as git,
        mock.patch.object(publication_consumer, "_run_revalidate") as revalidate,
        pytest.raises(
            transition.ReleaseTransitionError,
            match="symlink or escapes the resolved runs root",
        ),
    ):
        publication_consumer.finalize(
            root,
            root / "runs/parent-link/plan.json",
            canonical_shell,
            _plan_sha256(outside_plan),
        )

    git.assert_not_called()
    revalidate.assert_not_called()
    assert _git(root.parents[1], "rev-parse", "HEAD") == before_head
    assert _git(root.parents[1], "status", "--porcelain=v2", "-z") == before_status


@pytest.mark.parametrize("expected_sha256", ["A" * 64, "0" * 63, "not-a-sha"])
def test_publication_consumer_rejects_malformed_plan_sha_before_git(
    tmp_path: Path,
    expected_sha256: str,
) -> None:
    root, _plan, plan_path, canonical_shell = _prepare_consumer_fixture(tmp_path)
    with (
        mock.patch.object(publication_consumer, "_git") as git,
        pytest.raises(
            transition.ReleaseTransitionError,
            match="lowercase SHA-256",
        ),
    ):
        publication_consumer.finalize(
            root, plan_path, canonical_shell, expected_sha256
        )
    git.assert_not_called()


@pytest.mark.parametrize("precommitted_bundle", [False, True])
def test_real_basis_promotion_plan_covers_imported_receipt_and_one_publication_child(
    tmp_path: Path,
    precommitted_bundle: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, artifact_dir = _composition_fixture(tmp_path)
    repository = root.parents[1]
    (root / ".gitignore").write_text("runs/\n", encoding="utf-8")
    shell = root / "scripts/step5d-autotune-v3.sh"
    shell.chmod(0o755)
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "test@example.invalid")
    _git(repository, "config", "user.name", "real publication lifecycle test")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "offline lifecycle source baseline")

    manifest, _bundle, _targets = promotion.compose_local_release(root, artifact_dir)
    manifest_bytes = promotion.canonical_bytes(manifest)
    candidate_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    program_id = manifest["identity"]["program_id"]
    artifact_sha256 = {
        extension: reference["sha256"]
        for extension, reference in manifest["artifacts"].items()
    }
    candidate = SimpleNamespace(
        manifest_path=(
            Path("config/step5d/releases") / candidate_sha256 / "manifest.json"
        ).as_posix(),
        manifest_sha256=candidate_sha256,
        program_id=program_id,
        controller_target=manifest["controller_target"],
        artifact_sha256=artifact_sha256,
    )
    basis_manifest = root / "config/step5d/releases/basis/manifest.json"
    basis_manifest.parent.mkdir(parents=True, exist_ok=True)
    basis_manifest.write_bytes(manifest_bytes)
    basis = SimpleNamespace(
        manifest_path=basis_manifest.relative_to(root).as_posix(),
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        program_id=program_id,
        controller_target=manifest["controller_target"],
        artifact_sha256=artifact_sha256,
    )
    _git(repository, "add", str(basis_manifest))
    _git(repository, "commit", "-qm", "offline basis manifest")

    if precommitted_bundle:
        first_receipt = _write_receipt(
            root,
            artifact_dir,
            suffix="preexisting",
            transaction_id="1" * 32,
            checked_at="2026-07-21T11:00:33+08:00",
            stamp="preexisting",
            controller="root@controller-a",
        )
        promotion.promote(
            root,
            first_receipt,
            artifact_dir,
            expected_transaction_id="1" * 32,
            expected_manifest_sha256=hashlib.sha256(first_receipt.read_bytes()).hexdigest(),
            expected_candidate_manifest_sha256=candidate_sha256,
        )
        _git(repository, "add", ".")
        _git(repository, "commit", "-qm", "precommitted immutable bundle")

    baseline = transition.git_publication_snapshot(root)
    receipt = _write_receipt(
        root,
        artifact_dir,
        suffix="publication",
        transaction_id="2" * 32,
        checked_at="2026-07-21T11:07:45+08:00",
        stamp="publication",
        controller="root@controller-b",
    )
    basis_path, basis_payload = transition.create_delivery_basis(
        root,
        candidate_release=candidate,
        basis_release=candidate if precommitted_bundle else basis,
        prior_full_receipt=receipt,
    )
    additional: dict[str, Path] = {}
    transaction._add_delivery_basis_outputs(
        root,
        candidate,
        basis_path,
        basis_payload,
        additional,
    )
    promotion_result = promotion.promote(
        root,
        receipt,
        artifact_dir,
        expected_transaction_id="2" * 32,
        expected_manifest_sha256=hashlib.sha256(receipt.read_bytes()).hexdigest(),
        expected_candidate_manifest_sha256=candidate_sha256,
    )
    plan = transition.build_post_promotion_publication_plan(
        root,
        release_manifest_sha256=candidate_sha256,
        program_id=program_id,
        transaction_id="3" * 32,
        baseline=baseline,
        compatibility_targets=promotion_result["compatibility_targets"],
        additional_outputs=additional,
    )
    plan_path = root / "runs/post-promotion-publication.json"
    transition.write_post_promotion_publication_plan(root, plan_path, plan)
    before_publication_count = int(_git(repository, "rev-list", "--count", "HEAD"))
    revalidate_calls: list[Path] = []
    with mock.patch.object(
        publication_consumer,
        "_run_revalidate",
        side_effect=lambda _root, _plan, canonical: revalidate_calls.append(canonical) or 0,
    ):
        rc, payload = publication_consumer.finalize(
            root, plan_path, shell, _plan_sha256(plan_path)
        )

    assert rc == 0
    assert payload["status"] == "ok"
    assert plan["publication"]["changed_paths"] == plan["publication"]["allowlist"]
    assert revalidate_calls == [shell]
    assert (root / "config/step5d/delivery-bases" / f"{candidate_sha256}.json").is_file()
    basis_relative = basis_path.relative_to(root).as_posix()
    prior_relative = basis_payload["prior_full_readback_receipt"]["path"]
    assert {basis_relative, prior_relative} <= set(plan["publication"]["allowlist"])
    bundle_paths = {
        path
        for path in plan["publication"]["sha256"]
        if path.startswith("config/step5d/releases/")
    }
    if precommitted_bundle:
        assert bundle_paths.isdisjoint(plan["publication"]["allowlist"])
    else:
        assert bundle_paths <= set(plan["publication"]["allowlist"])
    assert _git(repository, "rev-parse", "HEAD^") == baseline["head"]
    assert int(_git(repository, "rev-list", "--count", "HEAD")) == before_publication_count + 1
    assert _git(repository, "status", "--porcelain=v2", "-z") == ""


def test_publish_and_revalidate_rejects_dry_run_before_environment_or_io(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match="cannot be used with --dry-run"):
        transaction.main(
            [
                "--root",
                str(tmp_path),
                "--artifact-dir",
                str(tmp_path),
                "--dry-run",
                "--publish-and-revalidate",
            ]
        )


def _composition_fixture(tmp_path: Path) -> tuple[Path, Path]:
    repository = tmp_path / "repository"
    root = repository / "experiments/tase-contact-reproduction"
    _copy_file(
        ROOT / "config/step5d/parameter_receiver_initial.json",
        root / "config/step5d/parameter_receiver_initial.json",
    )
    for relative in promotion.SOURCE_INPUTS:
        _copy_file(ROOT / relative, root / relative)
    contract = json.loads(
        (
            ROOT / "config/step5/step5d_autotune_v3_control_contract.json"
        ).read_text(encoding="utf-8")
    )
    for relative_text in contract["source_sha256"]:
        relative = Path(relative_text)
        _copy_file(ROOT / relative, root / relative)
    for relative in promotion.REPOSITORY_SOURCE_INPUTS:
        _copy_file(ROOT.parents[1] / relative, repository / relative)
    for relative in promotion.STATIC_PROJECTIONS:
        _copy_file(ROOT / relative, root / relative)
    artifact_dir = root / "runs/content-identity-artifacts"
    artifact_dir.mkdir(parents=True)
    builder.write_triplet(
        artifact_dir,
        TEST_STAMP, program_id=PROGRAM
    )
    return root, artifact_dir


def test_delivery_manifest_drives_only_exact_fresh_triplet(tmp_path: Path) -> None:
    root, manifest = _fixture_manifest(tmp_path)
    artifact_dir = root / promotion.PACKAGE_DIR
    payload, hashes = promotion.validate_delivery(root, manifest, artifact_dir)
    assert payload["upload_transaction_id"] == "a" * 32
    assert hashes == payload["sha256"]["readback"]
    manifest_sha256 = hashlib.sha256(manifest.read_bytes()).hexdigest()

    with pytest.raises(promotion.R009PromotionError, match="identity"):
        promotion.validate_delivery(
            root,
            manifest,
            artifact_dir,
            expected_transaction_id="b" * 32,
            expected_manifest_sha256=manifest_sha256,
        )
    with pytest.raises(promotion.R009PromotionError, match="handoff SHA-256"):
        promotion.validate_delivery(
            root,
            manifest,
            artifact_dir,
            expected_transaction_id="a" * 32,
            expected_manifest_sha256="0" * 64,
        )

    payload["sha256"]["controller"][".script"] = "0" * 64
    manifest.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    with pytest.raises(promotion.R009PromotionError, match="SHA closure"):
        promotion.validate_delivery(root, manifest, artifact_dir)


def test_existing_program_fresh_readback_is_promotion_and_observation_eligible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, receipt = _fixture_manifest(tmp_path)
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["delivery_mode"] = "existing_program_fresh_readback"
    basis_reference = {
        "path": "config/step5d/delivery-bases/fixture.json",
        "sha256": "9" * 64,
    }
    payload["delivery_basis"] = basis_reference
    now = datetime(2026, 7, 21, 3, 1, tzinfo=timezone.utc)
    payload["fresh_controller_checked_at"] = now.isoformat()
    receipt.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    validated, hashes = promotion.validate_delivery(
        root,
        receipt,
        root / promotion.PACKAGE_DIR,
        expected_transaction_id="a" * 32,
        expected_manifest_sha256=hashlib.sha256(receipt.read_bytes()).hexdigest(),
        expected_delivery_basis=basis_reference,
    )
    assert validated["delivery_mode"] == "existing_program_fresh_readback"
    release = _release_for_receipt(receipt)
    monkeypatch.setattr(
        delivery_module,
        "require_receipt_delivery_basis",
        lambda *_args, **_kwargs: basis_reference,
    )
    observation = build_delivery_observation(
        root,
        receipt_path=receipt,
        receipt_sha256=hashlib.sha256(receipt.read_bytes()).hexdigest(),
        transaction_id="a" * 32,
        release=release,
        now=now,
    )
    assert observation["triplet_sha256"] == hashes


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("fresh_controller_sha_verified", False),
        ("readback_source", "prior_full_readback"),
        ("target_dir", "/programs/andyl/other"),
    ],
)
def test_existing_program_readback_receipt_fails_closed_on_binding_drift(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    root, receipt = _fixture_manifest(tmp_path)
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["delivery_mode"] = "existing_program_fresh_readback"
    payload[field] = value
    receipt.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    with pytest.raises(
        promotion.R009PromotionError,
        match="identity, target, or freshness",
    ):
        promotion.validate_delivery(
            root,
            receipt,
            root / promotion.PACKAGE_DIR,
        )


def test_promoter_rejects_symlink_receipt_and_artifact_handoffs(
    tmp_path: Path,
) -> None:
    root, receipt = _fixture_manifest(tmp_path)
    receipt_link = root / "receipt-link.json"
    receipt_link.symlink_to(receipt)
    with pytest.raises(promotion.R009PromotionError, match="manifest handoff is unsafe"):
        promotion.validate_delivery(
            root,
            receipt_link,
            root / promotion.PACKAGE_DIR,
        )

    artifact_link = root / "artifact-link"
    artifact_link.symlink_to(root / promotion.PACKAGE_DIR, target_is_directory=True)
    with pytest.raises(
        promotion.R009PromotionError,
        match="pending artifact directory is unsafe",
    ):
        promotion.compose_release(root, receipt, artifact_link)


def test_delivery_observation_revalidates_receipt_and_release_closure(
    tmp_path: Path,
) -> None:
    root, receipt = _fixture_manifest(tmp_path)
    now = datetime(2026, 7, 21, 3, 1, tzinfo=timezone.utc)
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["fresh_controller_checked_at"] = (
        now - timedelta(seconds=5)
    ).isoformat()
    receipt.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    release = _release_for_receipt(receipt)
    receipt_sha256 = hashlib.sha256(receipt.read_bytes()).hexdigest()

    observation = build_delivery_observation(
        root,
        receipt_path=receipt,
        receipt_sha256=receipt_sha256,
        transaction_id="a" * 32,
        release=release,
        now=now,
    )

    validated = validate_delivery_observation(
        root,
        observation,
        release=release,
    )
    assert validated == observation
    assert validated["fresh_controller_checked_at"] == (
        now - timedelta(seconds=5)
    ).isoformat()
    changed = json.loads(json.dumps(observation))
    changed["fresh_controller_checked_at"] = now.isoformat()
    with pytest.raises(DeliveryObservationError, match="receipt content differs"):
        validate_delivery_observation(root, changed, release=release)


def test_delivery_observation_age_does_not_expire_exact_content_binding(
    tmp_path: Path,
) -> None:
    root, receipt = _fixture_manifest(tmp_path)
    checked_at = datetime(2026, 7, 21, 3, 1, tzinfo=timezone.utc)
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["fresh_controller_checked_at"] = checked_at.isoformat()
    receipt.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    release = _release_for_receipt(receipt)
    observation = build_delivery_observation(
        root,
        receipt_path=receipt,
        receipt_sha256=hashlib.sha256(receipt.read_bytes()).hexdigest(),
        transaction_id="a" * 32,
        release=release,
        now=checked_at,
    )

    validated = validate_delivery_observation(
        root,
        observation,
        release=release,
    )
    assert validated == observation
    assert validated["fresh_controller_checked_at"] == checked_at.isoformat()


def test_current_release_resolves_latest_content_addressed_delivery_receipt(
    tmp_path: Path,
) -> None:
    root, receipt = _fixture_manifest(tmp_path)
    release = _release_for_receipt(receipt)
    receipt_sha256 = hashlib.sha256(receipt.read_bytes()).hexdigest()
    first = build_delivery_observation(
        root,
        receipt_path=receipt,
        receipt_sha256=receipt_sha256,
        transaction_id="a" * 32,
        release=release,
        now=datetime(2026, 7, 21, 3, 1, tzinfo=timezone.utc),
    )
    second = {
        **first,
        "recorded_at_unix_ns": first["recorded_at_unix_ns"] + 1,
    }
    for observation in (first, second):
        path = write_indexed_delivery_observation(
            root,
            observation,
            release=release,
        )
        assert path.stem == hashlib.sha256(path.read_bytes()).hexdigest()
    corrupt = delivery_index_path(root, first).parent / f"{'f' * 64}.json"
    corrupt.write_text('{"broken":true}\n', encoding="utf-8")

    resolved_path, resolved = resolve_delivery_observation(
        root,
        release=release,
    )

    assert resolved == second
    assert resolved_path == delivery_index_path(root, second)


def test_explicit_delivery_observation_remains_read_only_compatibility_path(
    tmp_path: Path,
) -> None:
    root, receipt = _fixture_manifest(tmp_path)
    release = _release_for_receipt(receipt)
    observation = build_delivery_observation(
        root,
        receipt_path=receipt,
        receipt_sha256=hashlib.sha256(receipt.read_bytes()).hexdigest(),
        transaction_id="a" * 32,
        release=release,
        now=datetime(2026, 7, 21, 3, 1, tzinfo=timezone.utc),
    )
    compatibility = root / "runs/campaign/delivery-observation.json"
    compatibility.parent.mkdir(parents=True)
    compatibility.write_text(json.dumps(observation) + "\n", encoding="utf-8")

    resolved_path, resolved = resolve_delivery_observation(
        root,
        release=release,
        compatibility_path=compatibility,
    )

    assert resolved_path == compatibility
    assert resolved == observation


def test_delivery_observation_rejects_receipt_triplet_not_bound_to_release(
    tmp_path: Path,
) -> None:
    root, receipt = _fixture_manifest(tmp_path)
    now = datetime(2026, 7, 21, 3, 1, tzinfo=timezone.utc)
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["fresh_controller_checked_at"] = now.isoformat()
    receipt.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    release = _release_for_receipt(receipt)
    release.artifact_sha256[".script"] = "0" * 64

    with pytest.raises(DeliveryObservationError, match="identity closure differs"):
        build_delivery_observation(
            root,
            receipt_path=receipt,
            receipt_sha256=hashlib.sha256(receipt.read_bytes()).hexdigest(),
            transaction_id="a" * 32,
            release=release,
            now=now,
        )


def test_delivery_observation_rejects_receipt_validation_sha_mismatch(
    tmp_path: Path,
) -> None:
    root, receipt = _fixture_manifest(tmp_path)
    now = datetime(2026, 7, 21, 3, 1, tzinfo=timezone.utc)
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["fresh_controller_checked_at"] = now.isoformat()
    payload["validation"]["script_sha256"] = "0" * 64
    receipt.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    release = _release_for_receipt(receipt)

    with pytest.raises(DeliveryObservationError, match="identity closure differs"):
        build_delivery_observation(
            root,
            receipt_path=receipt,
            receipt_sha256=hashlib.sha256(receipt.read_bytes()).hexdigest(),
            transaction_id="a" * 32,
            release=release,
            now=now,
        )


def test_manifest_v3_and_bundle_ignore_receipt_time_and_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, artifact_dir = _composition_fixture(tmp_path)
    contract = json.loads(
        (
            root / "config/step5/step5d_autotune_v3_control_contract.json"
        ).read_text(encoding="utf-8")
    )
    dynamic_contract_fields = {
        "source_sha256",
        "candidate_tp_artifact_sha256",
        "tp_artifact_sha256",
        "candidate_tp_identity",
        "deployment_tp_identity",
        "promotion_status",
    }
    static_contract = {
        key: value
        for key, value in contract.items()
        if key not in dynamic_contract_fields
    }
    monkeypatch.setattr(
        promotion,
        "CONTRACT_STATIC_SHA256",
        hashlib.sha256(promotion.canonical_bytes(static_contract)).hexdigest(),
    )
    first_receipt = _write_receipt(
        root,
        artifact_dir,
        suffix="first",
        transaction_id="1" * 32,
        checked_at="2026-07-21T11:00:33+08:00",
        stamp="first-receipt",
        controller="root@controller-a",
    )
    second_receipt = _write_receipt(
        root,
        artifact_dir,
        suffix="second",
        transaction_id="2" * 32,
        checked_at="2026-07-21T11:07:45+08:00",
        stamp="second-receipt",
        controller="root@controller-b",
    )
    first_receipt_sha = hashlib.sha256(first_receipt.read_bytes()).hexdigest()
    second_receipt_sha = hashlib.sha256(second_receipt.read_bytes()).hexdigest()
    assert first_receipt_sha != second_receipt_sha

    local = promotion.compose_local_release(root, artifact_dir)
    for relative in (
        Path("config/tase_protocol_table.json"),
        Path("config/step5d/v3_active_surface.json"),
    ):
        if relative.name == "v3_active_surface.json":
            rendered = json.loads(local[1][relative.as_posix()])
            source = json.loads((root / relative).read_text(encoding="utf-8"))
            rendered.pop("recovery_loaded_program_ids", None)
            source.pop("recovery_loaded_program_ids", None)
            rendered.pop("release_claims", None)
            source.pop("release_claims", None)
            assert rendered == source
        else:
            assert local[1][relative.as_posix()] == (root / relative).read_bytes()
    rendered_contract = json.loads(
        local[1][
            "config/step5/step5d_autotune_v3_control_contract.json"
        ]
    )
    assert rendered_contract["schema"] == "step5d.autotune.v3.control-contract/v1"
    first = promotion.compose_release(
        root,
        first_receipt,
        artifact_dir,
        expected_transaction_id="1" * 32,
        expected_manifest_sha256=first_receipt_sha,
    )
    second = promotion.compose_release(
        root,
        second_receipt,
        artifact_dir,
        expected_transaction_id="2" * 32,
        expected_manifest_sha256=second_receipt_sha,
    )
    first_manifest, first_bundle, first_targets = first
    second_manifest, second_bundle, second_targets = second

    assert promotion.canonical_bytes(local[0]) == promotion.canonical_bytes(
        first_manifest
    )
    assert local[1:] == first[1:]
    assert promotion.canonical_bytes(first_manifest) == promotion.canonical_bytes(
        second_manifest
    )
    assert first_bundle == second_bundle
    assert first_targets == second_targets
    immutable_bytes = promotion.canonical_bytes(first_manifest) + b"".join(
        first_bundle[path] for path in sorted(first_bundle)
    )
    for dynamic in (
        b"1" * 32,
        b"2" * 32,
        b"first-receipt",
        b"second-receipt",
        b"2026-07-21T11:00:33+08:00",
        b"2026-07-21T11:07:45+08:00",
    ):
        assert dynamic not in immutable_bytes

    staged = promotion.stage_local_candidate(root, artifact_dir)
    assert staged["schema"] == "step5d.autotune-v3/local-release-candidate-v1"
    assert staged["manifest_sha256"] == hashlib.sha256(
        promotion.canonical_bytes(first_manifest)
    ).hexdigest()
    assert staged["current_pointer_changed"] is False
    assert staged["compatibility_mirrors_changed"] is False
    assert not (root / "config/step5d/current.json").exists()
    descriptor_path = root / "runs/local-release-candidate.json"
    descriptor_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor_path.write_text(json.dumps(staged) + "\n", encoding="utf-8")
    loaded_candidate, loaded_descriptor = load_local_release_candidate(
        root,
        descriptor_path,
    )
    assert loaded_candidate.manifest_sha256 == staged["manifest_sha256"]
    assert loaded_descriptor == staged

    publisher = promotion.AtomicReleasePublisher(root)

    def publish(
        manifest: dict[str, Any],
        bundle: dict[str, bytes],
        targets: dict[str, str],
    ) -> dict[str, Any]:
        def verify(stage: Path, manifest_path: Path, digest: str) -> None:
            promotion.verify_release_manifest(
                root,
                manifest_path,
                expected_manifest_sha256=digest,
                path_overrides={
                    target: stage / source for target, source in targets.items()
                },
            )

        return publisher.publish(
            manifest=manifest,
            bundle_files=bundle,
            compatibility_targets=targets,
            stage_verifier=verify,
        )

    first_result = publish(first_manifest, first_bundle, first_targets)
    bundle_path = Path(first_result["bundle"])
    first_tree = {
        path.relative_to(bundle_path).as_posix(): path.read_bytes()
        for path in sorted(bundle_path.rglob("*"))
        if path.is_file()
    }
    second_result = publish(second_manifest, second_bundle, second_targets)
    second_tree = {
        path.relative_to(bundle_path).as_posix(): path.read_bytes()
        for path in sorted(bundle_path.rglob("*"))
        if path.is_file()
    }
    assert first_result["manifest_sha256"] == second_result["manifest_sha256"]
    assert first_result["bundle"] == second_result["bundle"]
    assert first_tree == second_tree


def test_transaction_passes_exact_uploader_manifest_to_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "experiment"
    root.mkdir()
    artifact_dir = root / promotion.PACKAGE_DIR
    artifact_dir.mkdir(parents=True)
    builder.write_triplet(
        artifact_dir,
        TEST_STAMP, program_id=PROGRAM
    )
    events: list[str] = []
    exact: list[Path] = []
    upload_arguments: list[str] = []
    promotion_arguments: dict[str, Any] = {}
    evidence_output = root / "runs/campaign/delivery-observation.json"
    candidate_path = root / "candidate.json"
    contract_path = root / "release-contract.json"
    consumer = root / "tools/finalize_step5d_autotune_v3_publication.py"
    consumer.parent.mkdir(parents=True)
    consumer.write_text("# consumer fixture\n", encoding="utf-8")
    launcher = root / "scripts/step5d-autotune-v3.sh"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv(transaction.CANONICAL_LAUNCH_ENV, str(launcher))
    replaced_plan_bytes = b'{"fixture":"replaced"}\n'
    consumer_result = subprocess.CompletedProcess(
        args=["consumer"],
        returncode=0,
        stdout=json.dumps({"schema": "fixture"}),
        stderr="",
    )

    def fake_plan_write(*_args: Any, **_kwargs: Any) -> Path:
        events.append("plan-write")
        plan_path = root / "runs/plan.json"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_bytes(replaced_plan_bytes)
        return plan_path

    def fake_upload(arguments: list[str]) -> int:
        events.append("upload")
        upload_arguments.extend(arguments)
        token = arguments[arguments.index("--upload-transaction-id") + 1]
        result_path = Path(arguments[arguments.index("--manifest-path-output") + 1])
        manifest = root / "runs" / f"controller_readback_{PROGRAM}_exact" / "manifest.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"upload_transaction_id": token}) + "\n")
        result_path.write_text(
            json.dumps(
                {
                    "schema_version": "ur10e_upload_result_v1",
                    "upload_transaction_id": token,
                    "manifest_path": str(manifest.resolve()),
                    "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                }
            )
            + "\n"
        )
        exact.append(manifest.resolve())
        return 0

    def fake_promote(
        _root: Path,
        manifest: Path,
        artifact_dir: Path,
        **kwargs: Any,
    ) -> dict[str, Any]:
        assert events[-2:] == [
            "evidence:delivery-observation.json",
            "evidence:indexed-delivery.json",
        ]
        events.append("promote")
        exact.extend((manifest, artifact_dir))
        promotion_arguments.update(kwargs)
        return {
            "manifest_sha256": "f" * 64,
            "compatibility_targets": {
                "config/current_stage.json": "config/current_stage.json",
            },
        }

    release = SimpleNamespace(
        manifest_sha256="f" * 64,
        program_id=PROGRAM,
        controller_target=f"{promotion.TARGET_DIR}/{PROGRAM}.urp",
    )

    with (
        mock.patch.object(
            transaction,
            "require_runtime_profile",
            return_value={"bundle_id": "a" * 64},
        ),
        mock.patch.object(
            transaction,
            "_validate_candidate_and_certificate",
            side_effect=lambda *_args: events.append("certify") or release,
        ),
        mock.patch.object(
            transaction,
            "owner_dependency",
            return_value={
                "owner_id": "ur10e-controller-access",
                "path": "/verified/controller-helper.py",
                "sha256": "a" * 64,
            },
        ),
        mock.patch.object(transaction, "acquire_controller_mutation_locks", side_effect=lambda: events.append("lock") or [object()]),
        mock.patch.object(transaction.upload, "_main", side_effect=fake_upload),
        mock.patch.object(
            transaction,
            "create_delivery_basis",
            return_value=(root / "config/step5d/delivery-bases/fixture.json", {}),
        ),
        mock.patch.object(transaction, "_add_delivery_basis_outputs"),
        mock.patch.object(
            transaction,
            "git_snapshot",
            return_value={"head": "a" * 40, "tree": "b" * 40, "tracked_clean": True},
        ),
        mock.patch.object(transaction.promote, "promote", side_effect=fake_promote),
        mock.patch.object(
            transaction,
            "load_current_release",
            side_effect=lambda _root: events.append("load") or release,
        ),
        mock.patch.object(
            transaction,
            "build_delivery_observation",
            side_effect=lambda *args, **kwargs: events.append("observe")
            or {
                "schema": "fixture",
                "release_manifest_sha256": release.manifest_sha256,
            },
        ),
        mock.patch.object(
            transaction,
            "write_indexed_delivery_observation",
            side_effect=lambda *_args, **_kwargs: events.append(
                "evidence:indexed-delivery.json"
            )
            or exact.append(evidence_output.with_name("indexed-delivery.json"))
            or evidence_output.with_name("indexed-delivery.json"),
        ),
        mock.patch.object(
            transaction,
            "atomic_json",
            side_effect=lambda path, value: events.append(f"evidence:{path.name}")
            or exact.append(path),
        ),
        mock.patch.object(
            transaction,
            "build_post_promotion_publication_plan",
            side_effect=lambda *args, **kwargs: events.append("plan") or {"fixture": True},
        ),
        mock.patch.object(
            transaction,
            "write_post_promotion_publication_plan",
            side_effect=fake_plan_write,
        ) as plan_writer,
        mock.patch.object(
            transaction.subprocess,
            "run",
            return_value=consumer_result,
        ) as consumer_run,
        mock.patch.object(transaction, "release_controller_mutation_locks", side_effect=lambda _handles: events.append("release")),
    ):
        assert transaction.main(
            [
                "--root",
                str(root),
                "--artifact-dir",
                str(root / promotion.PACKAGE_DIR),
                "--release-candidate",
                str(candidate_path),
                "--release-certificate",
                str(contract_path),
                "--evidence-output",
                str(evidence_output),
                "--publish-and-revalidate",
            ]
        ) == 0

    assert events == [
        "certify",
        "lock",
        "upload",
        "observe",
        "evidence:delivery-observation.json",
        "evidence:indexed-delivery.json",
        "promote",
        "load",
        "plan",
        "plan-write",
        "release",
    ]
    assert upload_arguments[0] == PROGRAM
    assert any(
        value.startswith(f"manifest-driven {PROGRAM}")
        for value in upload_arguments
    )
    assert upload_arguments[
        upload_arguments.index("--controller-helper") + 1
    ] == "/verified/controller-helper.py"
    assert upload_arguments[
        upload_arguments.index("--controller-helper-sha256") + 1
    ] == "a" * 64
    assert exact[0] == exact[3]
    assert exact[4] == (root / promotion.PACKAGE_DIR).resolve()
    assert exact[1] == evidence_output.resolve()
    assert exact[2] == evidence_output.with_name("indexed-delivery.json").resolve()
    token = upload_arguments[upload_arguments.index("--upload-transaction-id") + 1]
    assert promotion_arguments == {
        "expected_transaction_id": token,
        "expected_manifest_sha256": hashlib.sha256(exact[0].read_bytes()).hexdigest(),
        "expected_candidate_manifest_sha256": release.manifest_sha256,
        "expected_delivery_basis": None,
    }
    consumer_argv = consumer_run.call_args.args[0]
    expected_plan_sha256 = transition.publication_plan_sha256({"fixture": True})
    assert consumer_argv[consumer_argv.index("--plan-sha256") + 1] == expected_plan_sha256
    assert expected_plan_sha256 != hashlib.sha256(replaced_plan_bytes).hexdigest()
    assert plan_writer.call_args.kwargs["expected_sha256"] == expected_plan_sha256
    assert consumer_run.call_args.kwargs["cwd"] == str(root)


def test_readback_only_transaction_adopts_exact_candidate_without_upload_or_load(
    tmp_path: Path,
) -> None:
    root = tmp_path / "experiment"
    artifact_dir = root / promotion.PACKAGE_DIR
    artifact_dir.mkdir(parents=True)
    builder.write_triplet(
        artifact_dir,
        TEST_STAMP, program_id=PROGRAM
    )
    artifact_sha = {
        extension: hashlib.sha256(
            (artifact_dir / f"{PROGRAM}{extension}").read_bytes()
        ).hexdigest()
        for extension in promotion.EXTENSIONS
    }
    release = SimpleNamespace(
        manifest_sha256="f" * 64,
        program_id=PROGRAM,
        controller_target=f"{promotion.TARGET_DIR}/{PROGRAM}.urp",
        artifact_sha256=artifact_sha,
        tp_runtime_identity={"protocol_version": 1},
    )
    basis_release = SimpleNamespace(
        manifest_sha256="e" * 64,
        program_id=PROGRAM,
        controller_target=f"{promotion.TARGET_DIR}/{PROGRAM}.urp",
        artifact_sha256=artifact_sha,
        tp_runtime_identity={"protocol_version": 1},
    )
    evidence_output = root / "runs/campaign/delivery-observation.json"
    upload_arguments: list[str] = []

    def fake_upload(arguments: list[str]) -> int:
        upload_arguments.extend(arguments)
        transaction_id = arguments[arguments.index("--upload-transaction-id") + 1]
        result_path = Path(arguments[arguments.index("--manifest-path-output") + 1])
        receipt = root / "runs/readback/manifest.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text(
            json.dumps({"upload_transaction_id": transaction_id}) + "\n",
            encoding="utf-8",
        )
        result_path.write_text(
            json.dumps(
                {
                    "schema_version": "ur10e_upload_result_v1",
                    "upload_transaction_id": transaction_id,
                    "manifest_path": str(receipt),
                    "manifest_sha256": hashlib.sha256(
                        receipt.read_bytes()
                    ).hexdigest(),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return 0

    with (
        mock.patch.object(transaction, "require_runtime_profile", return_value={}),
        mock.patch.object(
            transaction,
            "_validate_candidate_and_certificate",
            return_value=release,
        ),
        mock.patch.object(
            transaction,
            "load_current_release",
            return_value=release,
        ) as load_current,
        mock.patch.object(
            transaction,
            "load_current_release_for_compatible_readback",
            return_value=basis_release,
        ) as load_compatible_current,
        mock.patch.object(
            transaction,
            "load_delivery_basis",
            return_value=(
                root / "config/step5d/delivery-bases/prior.json",
                {
                    "prior_full_readback_receipt": {
                        "path": "runs/prior-full-readback.json",
                    },
                },
            ),
        ) as load_basis,
        mock.patch.object(transaction, "owner_dependency", return_value={
            "path": "/verified/helper.py",
            "sha256": "a" * 64,
        }),
        mock.patch.object(
            transaction,
            "acquire_controller_mutation_locks",
            return_value=[object()],
        ),
        mock.patch.object(transaction.upload, "_main", side_effect=fake_upload),
        mock.patch.object(
            transaction,
            "create_delivery_basis",
            return_value=(root / "config/step5d/delivery-bases/fixture.json", {}),
        ) as create_basis,
        mock.patch.object(transaction, "_add_delivery_basis_outputs"),
        mock.patch.object(
            transaction,
            "git_snapshot",
            return_value={"head": "a" * 40, "tree": "b" * 40, "tracked_clean": True},
        ),
        mock.patch.object(
            transaction,
            "delivery_basis_reference",
            return_value={
                "path": "config/step5d/delivery-bases/fixture.json",
                "sha256": "9" * 64,
            },
        ),
        mock.patch.object(
            transaction.promote,
            "promote",
            return_value={
                "manifest_sha256": release.manifest_sha256,
                "compatibility_targets": {
                    "config/current_stage.json": "config/current_stage.json",
                },
            },
        ),
        mock.patch.object(
            transaction,
            "build_post_promotion_publication_plan",
            return_value={"fixture": True},
        ),
        mock.patch.object(
            transaction,
            "write_post_promotion_publication_plan",
            return_value=root / "runs/plan.json",
        ),
        mock.patch.object(
            transaction,
            "build_delivery_observation",
            return_value={
                "schema": "fixture",
                "release_manifest_sha256": release.manifest_sha256,
            },
        ),
        mock.patch.object(
            transaction,
            "write_indexed_delivery_observation",
            return_value=evidence_output.with_name("indexed-delivery.json"),
        ),
        mock.patch.object(transaction, "atomic_json"),
        mock.patch.object(transaction, "release_controller_mutation_locks"),
    ):
        assert transaction.main(
            [
                "--root",
                str(root),
                "--artifact-dir",
                str(artifact_dir),
                "--release-candidate",
                str(root / "candidate.json"),
                "--release-certificate",
                str(root / "release-contract.json"),
                "--evidence-output",
                str(evidence_output),
                "--readback-only-existing",
            ]
        ) == 0

    assert "--readback-only-existing" in upload_arguments
    assert "--force-upload-readback" not in upload_arguments
    assert upload_arguments[
        upload_arguments.index("--delivery-basis-path") + 1
    ] == "config/step5d/delivery-bases/fixture.json"
    assert upload_arguments[
        upload_arguments.index("--delivery-basis-sha256") + 1
    ] == "9" * 64
    assert load_current.call_count == 1
    load_compatible_current.assert_called_once_with(root)
    load_basis.assert_called_once_with(root, release=basis_release)
    assert create_basis.call_args.kwargs == {
        "candidate_release": release,
        "basis_release": basis_release,
        "prior_full_receipt": root / "runs/prior-full-readback.json",
    }


def test_readback_only_get_failure_prevents_evidence_and_promotion(
    tmp_path: Path,
) -> None:
    root = tmp_path / "experiment"
    artifact_dir = root / promotion.PACKAGE_DIR
    artifact_dir.mkdir(parents=True)
    builder.write_triplet(
        artifact_dir,
        TEST_STAMP, program_id=PROGRAM
    )
    candidate = SimpleNamespace(
        manifest_sha256="f" * 64,
        program_id=PROGRAM,
        controller_target=f"{promotion.TARGET_DIR}/{PROGRAM}.urp",
        artifact_sha256={
            extension: "a" * 64 for extension in promotion.EXTENSIONS
        },
        tp_runtime_identity={"protocol_version": 1},
    )

    with (
        mock.patch.object(transaction, "require_runtime_profile", return_value={}),
        mock.patch.object(
            transaction,
            "_validate_candidate_and_certificate",
            return_value=candidate,
        ),
        mock.patch.object(
            transaction,
            "load_current_release",
            return_value=candidate,
        ) as load_current,
        mock.patch.object(
            transaction,
            "create_delivery_basis",
            return_value=(root / "config/step5d/delivery-bases/fixture.json", {}),
        ),
        mock.patch.object(transaction, "_add_delivery_basis_outputs"),
        mock.patch.object(
            transaction,
            "git_snapshot",
            return_value={"head": "a" * 40, "tree": "b" * 40, "tracked_clean": True},
        ),
        mock.patch.object(
            transaction,
            "delivery_basis_reference",
            return_value={
                "path": "config/step5d/delivery-bases/fixture.json",
                "sha256": "9" * 64,
            },
        ),
        mock.patch.object(
            transaction,
            "owner_dependency",
            return_value={
                "path": "/verified/helper.py",
                "sha256": "a" * 64,
            },
        ),
        mock.patch.object(
            transaction,
            "acquire_controller_mutation_locks",
            return_value=[object()],
        ),
        mock.patch.object(
            transaction.upload,
            "_main",
            side_effect=RuntimeError(
                "existing local/controller/readback triplet SHA closure differs"
            ),
        ) as upload,
        mock.patch.object(transaction, "atomic_json") as write_evidence,
        mock.patch.object(transaction.promote, "promote") as promote_release,
        mock.patch.object(transaction, "release_controller_mutation_locks") as unlock,
        pytest.raises(RuntimeError, match="triplet SHA closure differs"),
    ):
        transaction.main(
            [
                "--root",
                str(root),
                "--artifact-dir",
                str(artifact_dir),
                "--release-candidate",
                str(root / "candidate.json"),
                "--release-certificate",
                str(root / "release-contract.json"),
                "--evidence-output",
                str(root / "runs/campaign/delivery-observation.json"),
                "--readback-only-existing",
                "--prior-full-readback-receipt",
                str(root / "prior-full-readback.json"),
            ]
        )

    assert "--readback-only-existing" in upload.call_args.args[0]
    write_evidence.assert_not_called()
    promote_release.assert_not_called()
    load_current.assert_not_called()
    unlock.assert_called_once()


def test_transaction_rejects_evidence_output_outside_runs_before_lock(
    tmp_path: Path,
) -> None:
    root = tmp_path / "experiment"
    artifact_dir = root / promotion.PACKAGE_DIR
    artifact_dir.mkdir(parents=True)
    builder.write_triplet(
        artifact_dir,
        TEST_STAMP, program_id=PROGRAM
    )

    with (
        mock.patch.object(transaction, "acquire_controller_mutation_locks") as lock,
        mock.patch.object(transaction.upload, "_main") as upload,
        pytest.raises(RuntimeError, match="escapes runs evidence root"),
    ):
        transaction.main(
            [
                "--root",
                str(root),
                "--artifact-dir",
                str(artifact_dir),
                "--evidence-output",
                str(root / "runs/../outside.json"),
            ]
        )

    lock.assert_not_called()
    upload.assert_not_called()

    with (
        mock.patch.object(transaction, "acquire_controller_mutation_locks") as lock,
        mock.patch.object(transaction.upload, "_main") as upload,
        pytest.raises(RuntimeError, match="publication plan output escapes runs evidence root"),
    ):
        transaction.main(
            [
                "--root",
                str(root),
                "--artifact-dir",
                str(artifact_dir),
                "--evidence-output",
                str(root / "runs/observation.json"),
                "--publication-plan-output",
                str(root / "outside-plan.json"),
            ]
        )
    lock.assert_not_called()
    upload.assert_not_called()


def test_transaction_contract_failure_precedes_controller_lock_and_upload(
    tmp_path: Path,
) -> None:
    root = tmp_path / "experiment"
    artifact_dir = root / promotion.PACKAGE_DIR
    artifact_dir.mkdir(parents=True)
    builder.write_triplet(
        artifact_dir,
        TEST_STAMP, program_id=PROGRAM
    )
    evidence_output = root / "runs/campaign/delivery-observation.json"

    with (
        mock.patch.object(
            transaction,
            "require_runtime_profile",
            return_value={"bundle_id": "a" * 64},
        ),
        mock.patch.object(
            transaction,
            "_validate_candidate_and_certificate",
            side_effect=RuntimeError("release certificate differs"),
        ) as certificate_gate,
        mock.patch.object(transaction, "acquire_controller_mutation_locks") as lock,
        mock.patch.object(transaction.upload, "_main") as upload,
        pytest.raises(RuntimeError, match="release certificate differs"),
    ):
        transaction.main(
            [
                "--root",
                str(root),
                "--artifact-dir",
                str(artifact_dir),
                "--release-candidate",
                str(root / "candidate.json"),
                "--release-certificate",
                str(root / "release-contract.json"),
                "--evidence-output",
                str(evidence_output),
            ]
        )

    certificate_gate.assert_called_once()
    lock.assert_not_called()
    upload.assert_not_called()


def test_transaction_rejects_symlink_artifacts_and_evidence_before_lock(
    tmp_path: Path,
) -> None:
    root = tmp_path / "experiment"
    artifact_dir = root / promotion.PACKAGE_DIR
    artifact_dir.mkdir(parents=True)
    builder.write_triplet(
        artifact_dir,
        TEST_STAMP, program_id=PROGRAM
    )
    artifact_link = root / "artifact-link"
    artifact_link.symlink_to(artifact_dir, target_is_directory=True)
    runs = root / "runs"
    runs.mkdir()
    evidence_target = runs / "actual-observation.json"
    evidence_target.write_text("{}\n", encoding="utf-8")
    evidence_link = runs / "observation-link.json"
    evidence_link.symlink_to(evidence_target)

    with (
        mock.patch.object(transaction, "acquire_controller_mutation_locks") as lock,
        mock.patch.object(transaction.upload, "_main") as upload,
        pytest.raises(RuntimeError, match="pending artifact directory is unsafe"),
    ):
        transaction.main(
            [
                "--root",
                str(root),
                "--artifact-dir",
                str(artifact_link),
                "--evidence-output",
                str(runs / "unused.json"),
            ]
        )
    lock.assert_not_called()
    upload.assert_not_called()

    with (
        mock.patch.object(transaction, "acquire_controller_mutation_locks") as lock,
        mock.patch.object(transaction.upload, "_main") as upload,
        pytest.raises(RuntimeError, match="delivery evidence output is unsafe"),
    ):
        transaction.main(
            [
                "--root",
                str(root),
                "--artifact-dir",
                str(artifact_dir),
                "--evidence-output",
                str(evidence_link),
            ]
        )
    lock.assert_not_called()
    upload.assert_not_called()


def test_transaction_rejects_legacy_deploy_schema_before_lock_or_upload(
    tmp_path: Path,
) -> None:
    root = tmp_path / "experiment"
    artifact_dir = root / promotion.PACKAGE_DIR
    artifact_dir.mkdir(parents=True)
    generated = builder.write_triplet(
        artifact_dir,
        TEST_STAMP, program_id=PROGRAM
    )
    deploy = Path(generated["deploy_manifest"])
    payload = json.loads(deploy.read_text(encoding="utf-8"))
    payload["schema_version"] = 1
    deploy.write_text(json.dumps(payload), encoding="utf-8")

    with (
        mock.patch.object(transaction, "acquire_controller_mutation_locks") as lock,
        mock.patch.object(transaction.upload, "_main") as upload,
        pytest.raises(RuntimeError, match="exactly one schema-v2"),
    ):
        transaction.main(
            ["--root", str(root), "--artifact-dir", str(artifact_dir)]
        )

    lock.assert_not_called()
    upload.assert_not_called()


def test_transaction_rejects_runtime_identity_tamper_before_upload(
    tmp_path: Path,
) -> None:
    root = tmp_path / "experiment"
    artifact_dir = root / promotion.PACKAGE_DIR
    artifact_dir.mkdir(parents=True)
    generated = builder.write_triplet(
        artifact_dir,
        TEST_STAMP, program_id=PROGRAM
    )
    deploy = Path(generated["deploy_manifest"])
    payload = json.loads(deploy.read_text(encoding="utf-8"))
    payload["tp_runtime_identity"]["program_id"] = (
        "step5d_strict_rnn_autotune_v3_r009"
    )
    deploy.write_text(json.dumps(payload), encoding="utf-8")

    with (
        mock.patch.object(transaction, "acquire_controller_mutation_locks") as lock,
        mock.patch.object(transaction.upload, "_main") as upload,
        pytest.raises(RuntimeError, match="basename and runtime identity program differ"),
    ):
        transaction.main(
            ["--root", str(root), "--artifact-dir", str(artifact_dir)]
        )

    lock.assert_not_called()
    upload.assert_not_called()
