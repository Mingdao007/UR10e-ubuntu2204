from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
WORKTREE_ROOT = ROOT.parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(WORKTREE_ROOT / "src" / "ur10e_experiment_runtime"))

import check_step5d_autotune_v3_bridge_admission as admission_cli  # noqa: E402
from step5d_autotune_v3 import bridge_admission  # noqa: E402


EXPECTED_PROGRAM = (
    "/programs/andyl/kunwei/step5/"
    "step5d_strict_rnn_autotune_v3_r012.urp"
)


def _seed_git_reference(source_root: Path, fixture_root: Path) -> None:
    source = source_root / ".git"
    if source.is_file():
        raw = source.read_text(encoding="utf-8").splitlines()
        if not raw or not raw[0].startswith("gitdir:"):
            raise ValueError(f"Invalid Git worktree metadata: {source}")
        target = Path(raw[0][len("gitdir:") :].strip())
        if not target.is_absolute():
            target = source.parent / target
        content = f"gitdir: {target.resolve()}\n"
    elif source.is_dir():
        content = f"gitdir: {source.resolve(strict=True)}\n"
    else:
        raise FileNotFoundError(f"Git metadata is unavailable: {source}")
    (fixture_root / ".git").write_text(content, encoding="utf-8")


def _seed_campaign_source_contract(root: Path) -> None:
    from step5d_autotune_backend import Step5dV35Backend

    def _copy_path(src: Path, dst: Path) -> None:
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    for path in Step5dV35Backend.SOURCE_PATHS:
        _copy_path(ROOT / path, root / path)
    for path in Step5dV35Backend.CONFIG_PATHS:
        _copy_path(ROOT / path, root / path)
    _copy_path(
        ROOT / "config" / "step5d" / "manifests",
        root / "config" / "step5d" / "manifests",
    )
    _copy_path(
        ROOT / "programs" / "step5" / "step5d",
        root / "programs" / "step5" / "step5d",
    )
    _seed_git_reference(WORKTREE_ROOT, root)


def test_seed_git_reference_supports_linked_and_ordinary_checkouts(
    tmp_path: Path,
) -> None:
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / ".git").write_text("gitdir: ../linked-admin\n", encoding="utf-8")
    linked_fixture = tmp_path / "linked-fixture"
    linked_fixture.mkdir()

    ordinary = tmp_path / "ordinary"
    (ordinary / ".git").mkdir(parents=True)
    (ordinary / ".git/HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    ordinary_fixture = tmp_path / "ordinary-fixture"
    ordinary_fixture.mkdir()

    _seed_git_reference(linked, linked_fixture)
    _seed_git_reference(ordinary, ordinary_fixture)

    assert (linked_fixture / ".git").read_text(encoding="utf-8") == (
        f"gitdir: {(linked / '../linked-admin').resolve()}\n"
    )
    assert (ordinary_fixture / ".git").read_text(encoding="utf-8") == (
        f"gitdir: {(ordinary / '.git').resolve(strict=True)}\n"
    )


def _observation_fixture(
    tmp_path: Path,
    monkeypatch,
    *,
    program_state: str,
    loaded_program: str,
) -> tuple[dict[str, object], list[str]]:
    root = tmp_path / "experiment"
    root.mkdir()
    _seed_campaign_source_contract(root)

    delivery_path = root / "runs/delivery-observation.json"
    delivery_path.parent.mkdir()
    delivery_path.write_text('{"fixture":true}\n', encoding="utf-8")
    release = SimpleNamespace(
        manifest_sha256="a" * 64,
        program_id="step5d_strict_rnn_autotune_v3_r012",
    )
    commands: list[str] = []

    monkeypatch.setattr(
        bridge_admission,
        "load_runtime_release",
        lambda _root: release,
    )
    monkeypatch.setattr(
        bridge_admission,
        "release_contract_reference",
        lambda _root, _release: {
            "certificate_path": "runs/certificate.json",
            "certificate_sha256": "c" * 64,
            "evidence_path": "runs/contract.json",
            "evidence_sha256": "d" * 64,
        },
    )
    monkeypatch.setattr(
        bridge_admission,
        "resolve_delivery_observation",
        lambda _root, **_kwargs: (
            delivery_path,
            {"transaction_id": "b" * 32},
        ),
    )
    monkeypatch.setattr(
        bridge_admission,
        "load_delivery_observation",
        lambda _root, _path, **_kwargs: {"transaction_id": "b" * 32},
    )
    del program_state, loaded_program
    result = bridge_admission.observe_bridge_admission(root)
    return result, commands


def test_loaded_program_is_not_a_bridge_start_admission_input(
    tmp_path: Path,
    monkeypatch,
) -> None:
    result, commands = _observation_fixture(
        tmp_path,
        monkeypatch,
        program_state="STOPPED wrong.urp",
        loaded_program="/programs/wrong.urp",
    )

    assert result["state"] == "BRIDGE_START_READY"
    assert result["reason_code"] == "DELIVERY_VERIFIED"
    assert result["ok"] is True
    assert result["authority_acquired"] is False
    assert result["attempt_created"] is False
    assert commands == []


def test_loaded_stopped_program_produces_the_same_bridge_start_admission(
    tmp_path: Path,
    monkeypatch,
) -> None:
    result, commands = _observation_fixture(
        tmp_path,
        monkeypatch,
        program_state="STOPPED step5d_strict_rnn_autotune_v3_r012.urp",
        loaded_program=EXPECTED_PROGRAM,
    )

    assert result["state"] == "BRIDGE_START_READY"
    assert result["reason_code"] == "DELIVERY_VERIFIED"
    assert result["ok"] is True
    assert result["authority_acquired"] is False
    assert result["attempt_created"] is False
    assert commands == []


def test_validation_rejects_legacy_loaded_program_field(
    tmp_path: Path,
    monkeypatch,
) -> None:
    result, _commands = _observation_fixture(
        tmp_path,
        monkeypatch,
        program_state="STOPPED step5d_strict_rnn_autotune_v3_r012.urp",
        loaded_program=EXPECTED_PROGRAM,
    )
    result["loaded_program"] = EXPECTED_PROGRAM

    with pytest.raises(
        bridge_admission.BridgeAdmissionError,
        match="fields or freshness differ",
    ):
        bridge_admission.validate_bridge_admission(
            tmp_path / "experiment",
            result,
            release=type("Release", (), {
                "manifest_sha256": "a" * 64,
                "program_id": "step5d_strict_rnn_autotune_v3_r012",
            })(),
            now_ns=int(result["observed_at_unix_ns"]),
        )


def test_validation_rejects_legacy_publication_lineage_field(
    tmp_path: Path,
    monkeypatch,
) -> None:
    result, _commands = _observation_fixture(
        tmp_path,
        monkeypatch,
        program_state="STOPPED step5d_strict_rnn_autotune_v3_r012.urp",
        loaded_program=EXPECTED_PROGRAM,
    )
    result["publication_lineage"] = {"path": "obsolete", "sha256": "f" * 64}
    release = SimpleNamespace(
        manifest_sha256="a" * 64,
        program_id="step5d_strict_rnn_autotune_v3_r012",
    )

    with pytest.raises(
        bridge_admission.BridgeAdmissionError,
        match="fields or freshness differ",
    ):
        bridge_admission.validate_bridge_admission(
            tmp_path / "experiment",
            result,
            release=release,
            now_ns=int(result["observed_at_unix_ns"]),
        )


def test_cli_has_no_external_action_required_exit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = tmp_path / "admission.json"
    payload = {
        "schema": bridge_admission.SCHEMA,
        "state": "BRIDGE_START_READY",
        "ok": True,
        "reason_code": "DELIVERY_VERIFIED",
        "authority_acquired": False,
        "attempt_created": False,
    }
    monkeypatch.setattr(
        admission_cli,
        "observe_bridge_admission",
        lambda *_args, **_kwargs: payload,
    )
    monkeypatch.setattr(
        admission_cli,
        "write_indexed_bridge_admission",
        lambda *_args, **_kwargs: tmp_path / "indexed-admission.json",
    )

    assert admission_cli.main(["--root", str(tmp_path), "--output", str(output)]) == 0
    assert json.loads(output.read_text(encoding="utf-8")) == payload


def test_checker_writer_and_resolver_share_content_addressed_bytes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    payload, _commands = _observation_fixture(
        tmp_path,
        monkeypatch,
        program_state="STOPPED step5d_strict_rnn_autotune_v3_r012.urp",
        loaded_program=EXPECTED_PROGRAM,
    )
    root = tmp_path / "experiment"
    output = tmp_path / "admission.json"
    monkeypatch.setattr(
        admission_cli,
        "observe_bridge_admission",
        lambda *_args, **_kwargs: payload,
    )

    assert admission_cli.main(["--root", str(root), "--output", str(output)]) == 0

    indexed_path = bridge_admission.admission_index_path(root, payload)
    assert indexed_path.exists()
    assert indexed_path.stem == hashlib.sha256(indexed_path.read_bytes()).hexdigest()
    release = SimpleNamespace(
        manifest_sha256="a" * 64,
        program_id="step5d_strict_rnn_autotune_v3_r012",
    )
    resolved_path, resolved_payload = bridge_admission.resolve_bridge_admission(
        root,
        release=release,
        now_ns=int(payload["observed_at_unix_ns"]),
    )
    assert resolved_path == indexed_path
    assert resolved_payload == payload


def test_write_indexed_bridge_admission_rejects_conflicting_bytes(
    tmp_path: Path,
) -> None:
    value = {"release": {"manifest_sha256": "a" * 64}}
    indexed_path = bridge_admission.admission_index_path(tmp_path, value)
    indexed_path.parent.mkdir(parents=True)
    conflicting = b'{"conflict":true}\n'
    indexed_path.write_bytes(conflicting)

    with pytest.raises(
        bridge_admission.BridgeAdmissionError,
        match="content-addressed bridge admission differs",
    ):
        bridge_admission.write_indexed_bridge_admission(tmp_path, value)
    assert indexed_path.read_bytes() == conflicting


def test_write_indexed_bridge_admission_rejects_json_over_32kib(
    tmp_path: Path,
) -> None:
    value = {
        "release": {"manifest_sha256": "a" * 64},
        "payload": "x" * (32 * 1024),
    }

    with pytest.raises(
        bridge_admission.BridgeAdmissionError,
        match="exceeds 32768 bytes",
    ):
        bridge_admission.write_indexed_bridge_admission(tmp_path, value)
