#!/usr/bin/env python3
"""Canonical contact-experiment runtime resolver, launcher and import guard.

The contact-six runtime is a composition, not a single interpreter:

* the ``.venv-contact-six`` interpreter (numpy/scipy/osqp/... but no pinocchio;
  ``include-system-site-packages = false``), plus
* the ROS Humble Python paths, which are the only source of ``pinocchio``.

Running experiment code with system ``python3`` (or with the venv interpreter
but without the ROS paths) stops at ``ModuleNotFoundError: pinocchio``.  This
module is stdlib-only so system Python can run it to *find* the right runtime,
and it never opens a device, socket, or ROS node.

    scripts/contact-python.sh --check            # no-device import smoke
    scripts/contact-python.sh tools/X.py ARGS    # run any tool, managed
    scripts/contact-python.sh -m module ARGS

Device entrypoints and the modules that hard-require pinocchio
``import contact_runtime_bootstrap`` first, which calls
:func:`ensure_managed_runtime_on_import`.  Started under the wrong Python (system ``python3``, or the venv
without the ROS paths) they transparently re-exec under the canonical runtime,
so the first ordinary invocation succeeds; only if that runtime truly cannot
start do they exit 69 with one diagnostic, before any endpoint opens.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = Path(__file__).resolve().parent
VENV_NAME = ".venv-contact-six"
ROS_PREFIX = "/opt/ros/humble"
ROS_PYTHON_PATHS = (
    f"{ROS_PREFIX}/lib/python3.10/site-packages",
    f"{ROS_PREFIX}/local/lib/python3.10/dist-packages",
)
# Worktrees do not carry the gitignored venv; the provisioned owner checkout
# is the shared fallback (same path contact-yield-live.sh already used).
SHARED_VENV_PYTHONS = (
    "/home/andy/.codex-worktrees/contact-yield-recovery-20260920/"
    "experiments/tase-contact-reproduction/.venv-contact-six/bin/python",
)
OVERRIDE_ENV = "TASE_CONTACT_PYTHON"
REEXEC_ENV = "TASE_CONTACT_RUNTIME_REEXEC"  # loop guard: set on the re-exec child
REQUIRED_MODULES = ("pinocchio",)
EXIT_RUNTIME = 69  # EX_UNAVAILABLE: same code the contact-six wrappers use


class RuntimeEnvError(RuntimeError):
    """The managed experiment runtime cannot be resolved."""


def candidate_pythons(
    experiment_root: Path = EXPERIMENT_ROOT,
    environ: Mapping[str, str] | None = None,
) -> list[Path]:
    """Ordered candidates; an explicit override is exclusive."""
    environ = os.environ if environ is None else environ
    override = environ.get(OVERRIDE_ENV)
    if override:
        return [Path(override)]
    return [Path(experiment_root) / VENV_NAME / "bin" / "python",
            *(Path(p) for p in SHARED_VENV_PYTHONS)]


def resolve_interpreter(
    experiment_root: Path = EXPERIMENT_ROOT,
    environ: Mapping[str, str] | None = None,
) -> Path:
    candidates = candidate_pythons(experiment_root, environ)
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return path
    raise RuntimeEnvError(
        "no contact-six interpreter found; tried "
        + ", ".join(str(p) for p in candidates)
        + ". Run scripts/contact-six.sh provision, or set "
        + f"{OVERRIDE_ENV}. Do not fall back to system python or pip install."
    )


def runtime_environment(
    experiment_root: Path = EXPERIMENT_ROOT,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The one environment every managed experiment process runs under."""
    env = dict(os.environ if base is None else base)
    for name in ("VIRTUAL_ENV", "PYTHONHOME"):
        env.pop(name, None)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(Path(experiment_root) / "tools"), *ROS_PYTHON_PATHS]
    )
    env["PYTHONNOUSERSITE"] = "1"
    env["AMENT_PREFIX_PATH"] = ROS_PREFIX
    env["OMP_NUM_THREADS"] = "1"
    env["OPENBLAS_NUM_THREADS"] = "1"
    return env


def _under_ros(location: str) -> bool:
    return any(location.startswith(os.path.realpath(p) + os.sep) for p in ROS_PYTHON_PATHS)


def managed_problems(
    experiment_root: Path = EXPERIMENT_ROOT,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """Bootstrap detection from stdlib-only process facts; imports nothing.

    Decides whether *this process* is the canonical composition using only
    ``sys.prefix``/``sys.base_prefix`` and ``sys.path``.  It never attempts to
    import pinocchio (or any experiment dependency), so a wrong-Python process
    cannot even internally repeat the fail-then-correct pattern.
    """
    problems: list[str] = []
    prefixes = {
        str(p.parent.parent.absolute()) for p in candidate_pythons(experiment_root, environ)
    }
    if sys.prefix == sys.base_prefix or str(Path(sys.prefix).absolute()) not in prefixes:
        problems.append(
            f"wrong interpreter {sys.executable} (sys.prefix={sys.prefix}); "
            f"expected a {VENV_NAME} interpreter"
        )
    present = {os.path.realpath(entry) for entry in sys.path if entry}
    absent = [p for p in ROS_PYTHON_PATHS if not Path(p).is_dir()]
    if absent:
        problems.append(f"ROS Humble Python paths missing on this host: {absent}")
    off_path = [
        p for p in ROS_PYTHON_PATHS
        if Path(p).is_dir() and os.path.realpath(p) not in present
    ]
    if off_path:
        problems.append(f"ROS Python paths not on sys.path/PYTHONPATH: {off_path}")
    return problems


def dependency_smoke(modules: Sequence[str] = REQUIRED_MODULES) -> dict[str, Any]:
    """Import the required modules.  Only call from the canonical runtime."""
    import importlib

    loaded: dict[str, str] = {}
    problems: list[str] = []
    for name in modules:
        try:
            module = importlib.import_module(name)
            location = os.path.realpath(getattr(module, "__file__", "") or "")
            loaded[name] = location
            if name == "pinocchio" and not _under_ros(location):
                problems.append(f"pinocchio did not load from ROS Humble: {location}")
        except Exception as exc:  # exact failure is host specific
            problems.append(
                f"missing dependency {name} in {sys.executable}: {type(exc).__name__}: {exc}"
            )
    return {"loaded": loaded, "problems": problems}


def diagnose(
    experiment_root: Path = EXPERIMENT_ROOT,
    environ: Mapping[str, str] | None = None,
    modules: Sequence[str] = REQUIRED_MODULES,
) -> dict[str, Any]:
    """Report on the current process; dependencies import only if it is managed."""
    problems = managed_problems(experiment_root, environ)
    smoke: dict[str, Any] = {"loaded": {}, "problems": []}
    attempted = not problems
    if attempted:
        smoke = dependency_smoke(modules)
        problems = smoke["problems"]
    return {
        "ok": not problems,
        "problems": problems,
        "dependency_import_attempted": attempted,
        "executable": sys.executable,
        "sys_prefix": sys.prefix,
        "loaded": smoke["loaded"],
        "remedy": "run through experiments/tase-contact-reproduction/scripts/contact-python.sh",
    }


def _fail(result: Mapping[str, Any], extra: Mapping[str, Any] | None = None) -> None:
    print(
        json.dumps(
            {"schema": "contact-runtime-guard-v1", "ok": False,
             "device_endpoints_opened": False, **result, **(extra or {})},
            indent=2, sort_keys=True,
        ),
        file=sys.stderr,
    )
    raise SystemExit(EXIT_RUNTIME)


_INTERPRETER_FLAGS = frozenset({"-I", "-E", "-s", "-S", "-B", "-u", "-O", "-OO"})


def _reexec_argv() -> list[str]:
    """How this process was started (script, ``-m``, or ``-c``), minus interpreter flags."""
    orig = list(getattr(sys, "orig_argv", []))[1:]
    while orig and orig[0] in _INTERPRETER_FLAGS:
        orig.pop(0)
    if not orig:
        raise RuntimeEnvError("cannot re-exec an interactive session; use scripts/contact-python.sh")
    if orig[0] == "-":
        raise RuntimeEnvError(
            "cannot re-exec a stdin script (already consumed); use scripts/contact-python.sh"
        )
    return orig


def _ensure(experiment_root: Path, modules: Sequence[str], *, allow_reexec: bool) -> None:
    problems = managed_problems(experiment_root)
    if not problems:
        smoke = dependency_smoke(modules)
        if smoke["problems"]:
            _fail({"ok": False, "problems": smoke["problems"], "executable": sys.executable,
                   "sys_prefix": sys.prefix, "dependency_import_attempted": True,
                   "loaded": smoke["loaded"]},
                  {"detail": "the canonical runtime started but a dependency is unusable"})
        return
    result = {"problems": problems, "executable": sys.executable, "sys_prefix": sys.prefix,
              "dependency_import_attempted": False}
    if os.environ.get(REEXEC_ENV):
        _fail(result, {"reexec_attempted": True,
                       "detail": "re-exec'd under the canonical runtime but it is still not managed"})
    if not allow_reexec:
        return
    try:
        interpreter = resolve_interpreter(experiment_root)
        argv = _reexec_argv()
    except RuntimeEnvError as exc:
        _fail(result, {"detail": str(exc)})
    env = runtime_environment(experiment_root)
    env[REEXEC_ENV] = "1"
    sys.stdout.flush()
    sys.stderr.write(
        f"experiment_runtime: {sys.executable} is not the contact runtime "
        f"({problems[0]}); re-exec under {interpreter}\n"
    )
    sys.stderr.flush()
    os.execve(str(interpreter), [str(interpreter), "-B", *argv], env)


def ensure_managed_runtime(
    experiment_root: Path = EXPERIMENT_ROOT,
    modules: Sequence[str] = REQUIRED_MODULES,
) -> None:
    """Make this process the managed runtime, re-exec'ing if it is not.

    Call before importing numpy, pinocchio, project modules, or opening any
    device endpoint.  Returns normally when already managed (after the
    dependency smoke).  Otherwise the process is replaced, from stdlib-only
    facts and with no dependency import, by the same command line under the
    canonical interpreter/environment; the canonical child then imports the
    dependencies.  If the canonical runtime cannot start, exit 69 with one
    diagnostic naming the selected interpreter and the missing dependency.
    """
    _ensure(experiment_root, modules, allow_reexec=True)


def ensure_managed_runtime_on_import(
    experiment_root: Path = EXPERIMENT_ROOT,
    modules: Sequence[str] = REQUIRED_MODULES,
) -> None:
    """Library-import variant, used by ``contact_runtime_bootstrap``.

    Long-run KWR75 owner scripts are their own ``__main__`` and merely import
    ``run_contact_recovery`` / the live writer; a ``__main__`` guard inside
    those modules would never run.  Importing them in an unmanaged process
    re-execs the whole original command line instead.  Pytest and other
    harnesses that own the interpreter are left alone.
    """
    if "pytest" in sys.modules or getattr(sys.flags, "interactive", 0):
        return
    _ensure(experiment_root, modules, allow_reexec=True)


def launch(argv: Sequence[str], experiment_root: Path = EXPERIMENT_ROOT) -> int:
    """Verify then exec ``argv`` under the managed interpreter/environment."""
    if not argv:
        raise RuntimeEnvError("nothing to launch: pass a script path, -m module, or -c code")
    interpreter = resolve_interpreter(experiment_root)
    env = runtime_environment(experiment_root)
    smoke = subprocess.run(
        [str(interpreter), "-B", str(TOOLS_ROOT / "experiment_runtime.py"), "check"],
        env=env, capture_output=True, text=True, check=False,
    )
    if smoke.returncode != 0:
        sys.stderr.write(smoke.stderr or smoke.stdout)
        raise SystemExit(EXIT_RUNTIME)
    os.execve(str(interpreter), [str(interpreter), "-B", *argv], env)


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="no-device import smoke of the current process")
    sub.add_parser("resolve", help="print the resolved interpreter path")
    sub.add_parser("env", help="print the managed environment additions as JSON")
    launcher = sub.add_parser("launch", help="verify, then exec under the managed runtime")
    launcher.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        if args.command == "resolve":
            print(resolve_interpreter())
            return 0
        if args.command == "env":
            base = runtime_environment(EXPERIMENT_ROOT, {})
            print(json.dumps(base, indent=2, sort_keys=True))
            return 0
        if args.command == "launch":
            rest = list(args.argv)
            if rest[:1] == ["--"]:
                rest = rest[1:]
            return launch(rest)
    except RuntimeEnvError as exc:
        print(f"experiment_runtime: {exc}", file=sys.stderr)
        return EXIT_RUNTIME
    result = diagnose()
    print(json.dumps({"schema": "contact-runtime-check-v1", **result}, indent=2, sort_keys=True))
    return 0 if result["ok"] else EXIT_RUNTIME


if __name__ == "__main__":
    raise SystemExit(_main())
