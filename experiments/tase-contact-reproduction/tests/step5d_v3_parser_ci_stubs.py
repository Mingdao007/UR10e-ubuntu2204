"""Test-only import stubs for the frozen bridge parser on hosted CI.

The protected bridge module imports numeric and live transport dependencies at
module load time even though ``parse_args`` never executes them.  This module
stubs only that unused import surface.  It never replaces the bridge, argparse,
the parser, its env/default/configure helpers, or step5d_runtime_interface.
"""

from __future__ import annotations

import sys
import types


STUBBED_MODULES = frozenset(
    {
        "_ur_common",
        "capture_kunwei_kwr75_1khz",
        "numpy",
        "pandas",
        "pinocchio",
        "xacro",
        "yaml",
    }
)
FORBIDDEN_STUBS = frozenset(
    {
        "argparse",
        "kunwei_rtde_bridge",
        "step5d_runtime_interface",
    }
)


class _UnusedDependency(types.ModuleType):
    """Supply inert names needed only while unrelated runtime code is defined."""

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        value = (
            type(name, (), {})
            if name[:1].isupper() or name == "ndarray"
            else (lambda *_args, **_kwargs: None)
        )
        setattr(self, name, value)
        return value


def install() -> None:
    """Install the narrow dependency doubles before the real bridge import."""

    if STUBBED_MODULES & FORBIDDEN_STUBS:
        raise RuntimeError("parser CI stub allowlist overlaps a protected module")
    if "kunwei_rtde_bridge" in sys.modules:
        raise RuntimeError("parser CI stubs must be installed before the bridge import")
    for name in STUBBED_MODULES:
        sys.modules[name] = _UnusedDependency(name)

    capture = sys.modules["capture_kunwei_kwr75_1khz"]
    capture.FIELDS = ("fx", "fy", "fz", "mx", "my", "mz")
    capture.FORCE_KG_TO_N = 9.80665
    capture.MOMENT_KG_M_TO_NM = 9.80665
    capture.START_STREAM = b""
    capture.STOP_STREAM = b""

    common = sys.modules["_ur_common"]
    common.RTDEClient = type("RTDEClient", (), {})
    common.dashboard_exchange = lambda *_args, **_kwargs: None
