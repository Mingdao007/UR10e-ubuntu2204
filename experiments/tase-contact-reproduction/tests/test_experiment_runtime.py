"""First-call runtime selection for contact-experiment entrypoints.

Regression for the recurring two-step failure: routine experiment code started
under system Python (or the venv without the ROS paths), died on
``ModuleNotFoundError: pinocchio``, and only a manual retry under the composed
runtime worked.  The fix has two halves, both tested here:

* the decision to re-exec uses stdlib-only process facts, so the wrong Python
  never attempts to import pinocchio (even to catch the failure);
* the bootstrap fires on *library import* of the pinocchio-dependent chain, not
  just in ``__main__``, because Long-run KWR75 owner scripts are their own
  ``__main__`` and merely import ``step5d_autotune_v4_live_writer``,
  ``run_contact_recovery`` and ``LiveR004KunweiTransport``.

Everything is offline; the only endpoint is a loopback fake KWR75 listener.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

import experiment_runtime as rt  # noqa: E402

SYSTEM_PYTHON = "/usr/bin/python3"
BOOTSTRAPPED = (
    "contact_yield_live",
    "run_contact_recovery",
    "step5c_calibrated_kinematics_audit",
    "step5d_autotune_v4/calibrated_runtime",
    "step5d_autotune_v4_r004/calibrated_runtime",
)

# Records every import request (by name) with the requesting interpreter, before
# any project code runs.  A meta_path finder sees the request even if it fails.
FINDER = """\
import sys
class _Log:
    def find_spec(self, name, path=None, target=None):
        if name.split('.')[0] in ('pinocchio', 'numpy', 'scipy'):
            with open({log!r}, 'a') as stream:
                stream.write(sys.executable + ' ' + name + '\\n')
        return None
sys.meta_path.insert(0, _Log())
"""


def _canonical() -> Path:
    try:
        return rt.resolve_interpreter()
    except rt.RuntimeEnvError:
        pytest.skip("canonical .venv-contact-six interpreter is not provisioned on this host")


def _fresh_env(**extra: str) -> dict[str, str]:
    """A fresh worker shell: no venv, no PYTHONPATH, nothing pre-selected."""
    env = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp")}
    env.update(extra)
    return env


def _run(argv, env, cwd="/tmp"):
    return subprocess.run(argv, env=env, cwd=cwd, capture_output=True, text=True, check=False)


def _log_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _entry_script(tmp_path: Path, body: str) -> tuple[Path, Path, Path]:
    log, out = tmp_path / "imports.log", tmp_path / "result.json"
    script = tmp_path / "entry.py"
    script.write_text(
        FINDER.format(log=str(log))
        + "import sys\n"
        + f"sys.path[:0] = [{str(TOOLS)!r}]\n"
        + body,
        encoding="utf-8",
    )
    return script, log, out


def test_wrong_python_makes_no_dependency_import_then_canonical_child_does(tmp_path):
    canonical = _canonical()
    script, log, out = _entry_script(
        tmp_path,
        "from experiment_runtime import ensure_managed_runtime\n"
        "ensure_managed_runtime()\n"
        "import pinocchio, json\n"
        f"json.dump({{'exe': sys.executable, 'argv': sys.argv[1:], 'pin': pinocchio.__file__}}, "
        f"open({str(tmp_path / 'result.json')!r}, 'a'))\n",
    )
    completed = _run([SYSTEM_PYTHON, "-B", str(script), "--flag", "value with space"], _fresh_env())
    assert completed.returncode == 0, completed.stderr
    assert "re-exec under" in completed.stderr
    assert "ModuleNotFoundError" not in completed.stderr
    requests = _log_lines(log)
    assert requests, "the canonical child should have imported pinocchio"
    assert not [line for line in requests if line.startswith(SYSTEM_PYTHON + " ")], requests
    assert all(line.startswith(str(canonical.absolute()) + " ") for line in requests)
    assert any(line.endswith(" pinocchio") for line in requests)
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["exe"] == str(canonical.absolute())
    assert result["argv"] == ["--flag", "value with space"]
    assert result["pin"].startswith("/opt/ros/humble/")


def test_reexec_loop_guard_fails_before_endpoint_and_without_import_attempt(tmp_path):
    _canonical()
    script, log, out = _entry_script(
        tmp_path,
        "from experiment_runtime import ensure_managed_runtime\n"
        "ensure_managed_runtime()\n"
        f"open({str(tmp_path / 'result.json')!r}, 'w').write('endpoint opened')\n",
    )
    completed = _run([SYSTEM_PYTHON, "-B", str(script)], _fresh_env(**{rt.REEXEC_ENV: "1"}))
    assert completed.returncode == rt.EXIT_RUNTIME
    assert not out.exists()
    assert _log_lines(log) == []
    report = json.loads(completed.stderr)
    assert report["ok"] is False
    assert report["device_endpoints_opened"] is False
    assert report["dependency_import_attempted"] is False
    assert report["reexec_attempted"] is True
    assert SYSTEM_PYTHON in report["executable"]


def test_missing_canonical_interpreter_fails_naming_what_was_tried(tmp_path):
    script, log, out = _entry_script(
        tmp_path,
        "from experiment_runtime import ensure_managed_runtime\n"
        "ensure_managed_runtime()\n"
        f"open({str(tmp_path / 'result.json')!r}, 'w').write('endpoint opened')\n",
    )
    missing = tmp_path / "no-such-venv" / "bin" / "python"
    completed = _run([SYSTEM_PYTHON, "-B", str(script)], _fresh_env(**{rt.OVERRIDE_ENV: str(missing)}))
    assert completed.returncode == rt.EXIT_RUNTIME
    assert not out.exists() and _log_lines(log) == []
    report = json.loads(completed.stderr)
    assert str(missing) in report["detail"]
    assert report["device_endpoints_opened"] is False


KWR_OWNER = """\
# Shape of the Long-run KWR75 owner scripts (custom_zero_host.py,
# kunwei_guard_feed.py, official_zero_owner.py, ros2_zero_owner.py): a plain
# __main__ that puts tools on sys.path and *imports* the live writer and the
# Kunwei transport.  A fake loopback listener stands in for the KWR75 endpoint.
import json, socket, threading
server = socket.socket()
server.bind(('127.0.0.1', 0))
server.listen(1)
received = []
def serve():
    conn, _ = server.accept()
    conn.settimeout(2)
    try:
        received.append(conn.recv(16).hex())
    finally:
        conn.close()
thread = threading.Thread(target=serve, daemon=True)
thread.start()
from step5d_autotune_v4_live_writer import WritableRTDEClient  # noqa: E402
from step5d_autotune_v4_r004.transport import LiveR004KunweiTransport  # noqa: E402
import pinocchio  # noqa: E402
sensor = LiveR004KunweiTransport('127.0.0.1', port=server.getsockname()[1], timeout_s=1.0)
sensor.open()
thread.join()
sensor.close()
json.dump({'exe': sys.executable, 'pin': pinocchio.__file__, 'sensor_start': received},
          open(__OUT__, 'w'))
"""


def test_kwr75_owner_import_path_selects_canonical_runtime_on_first_call(tmp_path):
    """Fresh shell, plain ``python3 owner.py``: the library-import chain re-execs."""
    canonical = _canonical()
    script, log, out = _entry_script(tmp_path, KWR_OWNER.replace("__OUT__", repr(str(tmp_path / "result.json"))))
    completed = subprocess.run(
        ["env", "-i", "PATH=/usr/bin:/bin", f"HOME={os.environ.get('HOME', '/tmp')}",
         "bash", "-c", f"cd /tmp && python3 {script} --kwr75-sample"],
        capture_output=True, text=True, check=False, timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert "re-exec under" in completed.stderr and "ModuleNotFoundError" not in completed.stderr
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["exe"] == str(canonical.absolute())
    assert result["pin"].startswith("/opt/ros/humble/")
    assert result["sensor_start"] == ["48aa0d0a"]  # START_STREAM reached the fake, from the child
    requests = _log_lines(log)
    assert requests, "the canonical child should have imported numpy/pinocchio"
    # Wrong Python must make no numpy/scipy/pinocchio import attempt at all: the
    # earliest module of the chain (step5d_autotune_v4.calibrated_runtime, reached
    # from step5d_autotune_v4_live_writer.py:53) re-execs before any of them.
    assert not [line for line in requests if line.startswith(SYSTEM_PYTHON + " ")], requests
    assert all(line.startswith(str(canonical.absolute()) + " ") for line in requests)


def test_ordinary_live_entry_under_system_python_succeeds_on_first_call():
    _canonical()
    completed = _run([SYSTEM_PYTHON, str(TOOLS / "contact_yield_live.py"), "status"], _fresh_env())
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["command"] == "status" and payload["endpoints_opened"] is False
    assert "re-exec under" in completed.stderr and "ModuleNotFoundError" not in completed.stderr


def test_venv_interpreter_without_ros_paths_is_corrected_on_first_call():
    venv_python = _canonical()
    completed = _run([str(venv_python), str(TOOLS / "contact_yield_live.py"), "status"], _fresh_env())
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["endpoints_opened"] is False
    assert "re-exec under" in completed.stderr


def test_launcher_check_passes_from_fresh_shell():
    _canonical()
    completed = _run([str(ROOT / "scripts" / "contact-python.sh"), "--check"], _fresh_env())
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["ok"] is True and report["dependency_import_attempted"] is True
    assert report["loaded"]["pinocchio"].startswith("/opt/ros/humble/")


def test_wrong_runtime_diagnosis_does_not_import_dependencies():
    code = (
        "import sys; sys.path.insert(0, %r); import experiment_runtime as r; "
        "p = r.managed_problems(); d = r.diagnose(); "
        "print(bool(p), d['dependency_import_attempted'], 'pinocchio' in sys.modules)"
    ) % str(TOOLS)
    completed = _run([SYSTEM_PYTHON, "-c", code], _fresh_env())
    assert completed.stdout.split() == ["True", "False", "False"], completed.stderr


def test_runtime_environment_composes_tools_and_ros_paths():
    env = rt.runtime_environment(ROOT, {"VIRTUAL_ENV": "/x", "PYTHONHOME": "/y", "KEEP": "1"})
    assert env["PYTHONPATH"].split(os.pathsep) == [str(ROOT / "tools"), *rt.ROS_PYTHON_PATHS]
    assert "VIRTUAL_ENV" not in env and "PYTHONHOME" not in env and env["KEEP"] == "1"
    assert env["OMP_NUM_THREADS"] == env["OPENBLAS_NUM_THREADS"] == "1"
    assert env["AMENT_PREFIX_PATH"] == "/opt/ros/humble" and env["PYTHONNOUSERSITE"] == "1"


def test_override_is_exclusive_and_local_venv_precedes_shared():
    assert rt.candidate_pythons(ROOT, {rt.OVERRIDE_ENV: "/o/python"}) == [Path("/o/python")]
    ordered = rt.candidate_pythons(ROOT, {})
    assert ordered[0] == ROOT / ".venv-contact-six" / "bin" / "python"
    assert [str(p) for p in ordered[1:]] == list(rt.SHARED_VENV_PYTHONS)


@pytest.mark.parametrize("name", BOOTSTRAPPED)
def test_bootstrap_is_the_first_import_of_the_pinocchio_call_chain(name):
    tree = ast.parse((TOOLS / f"{name}.py").read_text(encoding="utf-8"))
    body = [
        node for node in tree.body
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
        and not (isinstance(node, ast.ImportFrom) and node.module == "__future__")
    ]
    first = body[0]
    assert isinstance(first, ast.Import) and first.names[0].name == "contact_runtime_bootstrap", name
