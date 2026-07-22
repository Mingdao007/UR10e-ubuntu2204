from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = ROOT.parents[1]
OCI = REPOSITORY / "ci/step5d-v3"
BASE = (
    "ros:humble-ros-base-jammy@sha256:"
    "5c793b92e0b12d6babb438cb20eed7766495fde6419a21e3d2e918464f09dc17"
)


def test_both_images_bind_base_uv_and_same_lock() -> None:
    for name, group in (("Dockerfile.cpu", "test-hermetic"), ("Dockerfile.cuda", "control")):
        source = (OCI / name).read_text(encoding="utf-8")
        assert f"FROM {BASE}" in source
        assert "FROM ghcr.io/astral-sh/uv:0.9.30 AS uv" in source
        assert "COPY --from=uv /uv /uvx /bin/" in source
        assert "python3 -m pip" not in source
        assert "COPY experiments/tase-contact-reproduction/uv.lock ./uv.lock" in source
        assert f"uv sync --frozen --only-group {group}" in source
    cuda = (OCI / "Dockerfile.cuda").read_text(encoding="utf-8")
    assert "uv sync --frozen --only-group optimizer" in cuda


def test_runner_can_issue_only_hermetic_ci_authority() -> None:
    path = OCI / "run_oci_gate.py"
    source = path.read_text(encoding="utf-8")
    ast.parse(source, filename=str(path))
    assert 'AUTHORITY = "HERMETIC_CI_PROVEN"' in source
    assert '"offline_proven": False' in source
    assert 'AUTHORITY = "OFFLINE_PROVEN"' not in source
    assert 'STEP5D_OCI_NETWORK_MODE") != "none"' in source
    assert 'STEP5D_OCI_REPOSITORY_MODE") != "read_only"' in source


def test_workflow_mounts_repo_read_only_and_disables_runtime_network() -> None:
    workflow = (REPOSITORY / ".github/workflows/step5d-autotune-v3.yml").read_text(
        encoding="utf-8"
    )
    assert "docker run --rm --network none --read-only" in workflow
    assert '--user "$(id -u):$(id -g)"' in workflow
    assert "dst=/workspace,readonly" in workflow
    assert "dst=/output" in workflow
    assert "Dockerfile.cpu" in workflow
    assert "Dockerfile.cuda" not in workflow
