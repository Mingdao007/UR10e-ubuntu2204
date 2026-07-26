"""Ensure the canonical shell's active Python executables are fingerprinted."""

from __future__ import annotations

import re
from pathlib import Path

from step5d_autotune_v3 import source_closure
from step5d_autotune_v3.source_fingerprint_contract import (
    REQUIRED_EXPERIMENT_SOURCE_FINGERPRINTS,
)


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "scripts/step5d-autotune-v3.sh"
CANONICAL_REQUIRED = frozenset(
    {
        "tools/check_step5d_autotune_v3_bridge_admission.py",
        "tools/run_step5d_autotune_v3_coordinator.py",
    }
)
SHELL_PYTHON_PATH = re.compile(
    r"(?:\"\$\{CONTROL_PYTHON\}\"|/usr/bin/python3\.10)"
    r"\s+(?:-B\s+-I\s+)?(?:\\\s*)*"
    r"\"\$\{EXPERIMENT_ROOT\}/(?P<path>tools/[^\"\s]+\.py)\""
)


def _active_canonical_shell_source() -> str:
    source = SHELL.read_text(encoding="utf-8")
    bridge_marker = "if (( bridge_mode == 1 )); then"
    manual_marker = 'if [[ "${bridge_route}" == "manual_v2" ]]; then'
    manual_end = "\n  fi\n  admission="
    prefix, bridge = source.split(bridge_marker, 1)
    if manual_marker not in bridge:
        return prefix + bridge
    manual_start = bridge.index(manual_marker)
    manual_end_index = bridge.index(manual_end, manual_start)
    return prefix + bridge[:manual_start] + bridge[manual_end_index:]


def _canonical_shell_python_paths() -> frozenset[str]:
    source = _active_canonical_shell_source()
    paths = {match.group("path") for match in SHELL_PYTHON_PATH.finditer(source)}
    if re.search(
        r'"\$\{CONTROL_PYTHON\}"\s+-m\s+step5d_autotune_v3\.cli',
        source,
    ):
        paths.add("tools/step5d_autotune_v3/cli.py")
    return frozenset(paths)


def test_every_active_canonical_shell_python_executable_is_fingerprinted() -> None:
    shell_paths = _canonical_shell_python_paths()

    assert CANONICAL_REQUIRED <= shell_paths
    assert CANONICAL_REQUIRED <= source_closure.PRODUCTION_EXPERIMENT_SEEDS
    assert shell_paths <= REQUIRED_EXPERIMENT_SOURCE_FINGERPRINTS
