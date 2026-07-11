#!/usr/bin/env python3
"""Emit a self-contained, read-only v30 timing program on stdout.

The Ubuntu checkout intentionally remains unchanged.  This bundler embeds the
local contact semantics, solver, compact outer loop, control contract, runtime
interface, calibrated kinematics, bridge, and timing harness into one Python
stdin program.  The seven runtime modules, harness, bundler, aggregator, and
readiness builder are SHA-256 bound.  Pipe stdout directly to
``ssh ... python3 -``; no remote file is created.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"


def sha256_text(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def build_bundle() -> str:
    modules = {
        "contact_semantics": (TOOLS / "contact_semantics.py").read_text(encoding="utf-8"),
        "step5c_strict_rnn": (TOOLS / "step5c_strict_rnn.py").read_text(encoding="utf-8"),
        "step5d_paper_outer_loop": (TOOLS / "step5d_paper_outer_loop.py").read_text(encoding="utf-8"),
        "step5d_control_contract": (TOOLS / "step5d_control_contract.py").read_text(encoding="utf-8"),
        "step5d_runtime_interface": (TOOLS / "step5d_runtime_interface.py").read_text(encoding="utf-8"),
        "step5c_calibrated_kinematics_audit": (
            TOOLS / "step5c_calibrated_kinematics_audit.py"
        ).read_text(encoding="utf-8"),
        "kunwei_rtde_bridge": (TOOLS / "kunwei_rtde_bridge.py").read_text(encoding="utf-8"),
    }
    harness = (TOOLS / "run_step5d_v30_remote_timing.py").read_text(encoding="utf-8")
    auxiliary_source_binding = {
        "bundler_sha256": sha256_text(Path(__file__).read_text(encoding="utf-8")),
        "aggregator_sha256": sha256_text(
            (TOOLS / "step5d_v30_timing.py").read_text(encoding="utf-8")
        ),
        "readiness_builder_sha256": sha256_text(
            (TOOLS / "build_step5d_v30_offline_readiness.py").read_text(
                encoding="utf-8"
            )
        ),
    }
    lines = [
        "import sys as _sys, types as _types",
        "from pathlib import Path as _Path",
        "_bundle_root = _Path.cwd()",
        "if '--experiment-root' in _sys.argv:",
        "    _bundle_root = _Path(_sys.argv[_sys.argv.index('--experiment-root') + 1]).resolve()",
        "_sys.path.insert(0, str(_bundle_root / 'tools'))",
        "def _install_v30_module(_name, _source, _source_sha):",
        "    _module = _types.ModuleType(_name)",
        "    _module.__file__ = str(_bundle_root / 'tools' / (_name + '.py'))",
        "    _module.__package__ = ''",
        "    _sys.modules[_name] = _module",
        "    exec(compile(_source, _module.__file__, 'exec'), _module.__dict__)",
        "    _module.__v30_source_sha256__ = _source_sha",
        "    _module.__v30_source_delivery__ = 'stdin_bundle'",
    ]
    for name, source in modules.items():
        lines.append(f"_install_v30_module({name!r}, {source!r}, {sha256_text(source)!r})")
    harness_sha = sha256_text(harness)
    lines.extend(
        [
            f"_harness_source = {harness!r}",
            "_harness_globals = {",
            "    '__name__': '__main__',",
            "    '__file__': '<v30-stdin-bundle>/run_step5d_v30_remote_timing.py',",
            f"    '__v30_source_sha256__': {harness_sha!r},",
            "    '__v30_source_delivery__': 'stdin_bundle',",
            f"    '__v30_auxiliary_source_binding__': {auxiliary_source_binding!r},",
            "}",
            "exec(compile(_harness_source, _harness_globals['__file__'], 'exec'), _harness_globals)",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    print(build_bundle(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
