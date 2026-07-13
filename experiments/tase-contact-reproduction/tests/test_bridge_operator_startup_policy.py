from pathlib import Path
import csv
import os
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def read_script(name: str) -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8")


class BridgeOperatorStartupPolicyTest(unittest.TestCase):
    def test_failed_bench_gate_cannot_refresh_long_check_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bench_gate = root / "bench_gate.py"
            cache = root / "long-check-cache.json"
            cache.write_text('{"ok": true, "seed": "must be invalidated"}\n', encoding="utf-8")
            bench_gate.write_text(
                'print(\'{"ok": false, "issues": ["robot_ping_failed"]}\')\nraise SystemExit(2)\n',
                encoding="utf-8",
            )
            script = f"""
set -euo pipefail
export BRIDGE_OPERATOR_SOURCE_ONLY=1
source "{ROOT / 'scripts' / 'bridge-line-operator.sh'}"
BENCH_GATE="{bench_gate}"
READONLY_PREFLIGHT="{bench_gate}"
LONG_CHECK_CACHE="{cache}"
set +e
refresh_bench_gate_cache
rc=$?
set -e
printf 'rc=%s\n' "$rc"
test "$rc" -eq 24
test ! -e "{cache}"
"""
            completed = subprocess.run(
                ["bash", "-lc", script],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertIn("rc=24", completed.stdout)

    def test_bridge_line_operator_exposes_explicit_long_gate_skip_knob(self) -> None:
        script = read_script("bridge-line-operator.sh")

        self.assertIn("BRIDGE_SKIP_BENCH_GATE", script)
        self.assertIn("BRIDGE_SKIP_LONG_CHECKS", script)
        self.assertIn("skipping long bench gate by request", script)

    def test_bridge_postprocess_emits_step5d_fast_analysis_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            run_dir = base / "run"
            run_dir.mkdir()
            (run_dir / ".capture_complete.json").write_text(
                '{"capture_closed": true, "immutable": true}\n', encoding="utf-8"
            )
            bridge_csv = run_dir / "bridge_rtde_500hz.csv"
            with bridge_csv.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "t_monotonic_s",
                        "ur_output_double_register_30",
                        "ur_output_double_register_35",
                        "_step4e_normal_load_n",
                        "_step5d_force_settle_filtered_normal_load_n",
                        "force_norm_n",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "t_monotonic_s": "0.000",
                        "ur_output_double_register_30": "17",
                        "ur_output_double_register_35": "25.3",
                        "_step4e_normal_load_n": "11.0",
                        "_step5d_force_settle_filtered_normal_load_n": "10.8",
                        "force_norm_n": "11.2",
                    }
                )
            script = f"""
set -euo pipefail
export BRIDGE_OPERATOR_SOURCE_ONLY=1
export BRIDGE_PROFILE=step5d_strict_rnn_ablation_v25
export STEP5D_DERIVED_ROOT="{base / 'derived'}"
source "{ROOT / 'scripts' / 'bridge-line-operator.sh'}"
postprocess_run "{run_dir}"
"""
            completed = subprocess.run(
                ["bash", "-lc", script],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            derived = next((base / "derived").iterdir())
            self.assertTrue(
                (derived / "frequency-summary/stage_frequency_summary.json").exists()
            )
            self.assertTrue(
                (derived / "step5d-analysis/step5d_bridge_analysis.json").exists()
            )
            self.assertFalse((run_dir / "stage_frequency_summary.json").exists())
            self.assertFalse((run_dir / "step5d_bridge_analysis.json").exists())
            self.assertIn("[operator] immutable source run:", completed.stdout)
            self.assertIn("[operator] derived artifacts:", completed.stdout)
            self.assertIn("root-cause classification: no_stage25_preload_dwell_short", completed.stdout)

    def test_step5d_contact_bridge_defaults_to_short_start_path(self) -> None:
        script = read_script("step5d-liveprep-operator.sh")

        self.assertIn('"${BRIDGE_OPERATOR}" line-bridge-fast', script)
        self.assertNotIn('BRIDGE_SKIP_BENCH_GATE="${BRIDGE_SKIP_BENCH_GATE:-1}"', script)
        self.assertIn('WAIT_FOR_PLAY_S="${WAIT_FOR_PLAY_S:-20}"', script)
        self.assertIn('AUTOWATCH_WAIT_FOR_PLAY_S="${AUTOWATCH_WAIT_FOR_PLAY_S:-20}"', script)
        self.assertIn('STEP5D_DEFAULT_REZERO_S="${STEP5D_DEFAULT_REZERO_S:-${TASE_STEP5D_REZERO_S}}"', script)
        self.assertIn('STEP5D_REZERO_S="${STEP5D_REZERO_S:-${STEP5D_DEFAULT_REZERO_S:-1.0}}"', script)
        self.assertIn(
            "v27/v28/v29 defaults to Step5b envelope 50/60 N with torque guard 3.0 Nm",
            script,
        )
        self.assertIn('STEP5D_VERSION}" == "step5d_strict_rnn_ablation_v29"', script)
        self.assertIn('STEP5D_STAGE25_CONTROL_MODE_DEFAULT="${STEP5D_STAGE25_CONTROL_MODE_DEFAULT:-speedj_rnn_live}"', script)
        self.assertIn('READBACK_GATE="${ROOT}/tools/verify_step5d_current_binding.py"', script)
        self.assertIn("current_step5d_version()", script)
        self.assertIn("require_live_bridge_authorization_gate", script)
        self.assertIn("--require-live-bridge-authorization", script)
        self.assertIn("--stage25-control-mode", script)
        self.assertIn("--rnn-backend", script)
        self.assertIn("--rnn-inner-iterations", script)
        self.assertIn("--epsilon", script)
        self.assertIn("--sigr-exponent-r", script)
        self.assertIn("--qdot-cap-rad-s", script)
        self.assertIn('STEP5D_EPSILON="${STEP5D_EPSILON:-0.010}"', script)
        self.assertIn('STEP5D_SIGR_EXPONENT_R="${STEP5D_SIGR_EXPONENT_R:-0.800}"', script)
        self.assertIn('STEP5D_RNN_INNER_ITERATIONS="${STEP5D_RNN_INNER_ITERATIONS:-1024}"', script)
        self.assertIn('STEP5D_RNN_BACKEND="${STEP5D_RNN_BACKEND:-cupy}"', script)
        self.assertIn('STEP5D_ALLOW_PENDING_OFFLINE_AUDIT="${STEP5D_ALLOW_PENDING_OFFLINE_AUDIT:-1}"', script)
        self.assertIn(
            '[[ "${STEP5D_VERSION}" == "step5d_strict_rnn_ablation_v29"',
            script,
        )
        self.assertIn('STEP5D_EPSILON="${STEP5D_EPSILON:-}"', script)
        self.assertIn('STEP5D_RNN_BACKEND="${STEP5D_RNN_BACKEND:-}"', script)

    def test_v29_bridge_operator_direct_profile_uses_strict_rnn_defaults(self) -> None:
        script = f"""
set -euo pipefail
export BRIDGE_OPERATOR_SOURCE_ONLY=1
export BRIDGE_PROFILE=step5d_strict_rnn_ablation_v29
source "{ROOT / 'scripts' / 'bridge-line-operator.sh'}"
printf '%s\\n' "$STEP5D_EPSILON" "$STEP5D_SIGR_EXPONENT_R" "$STEP5D_RNN_INNER_ITERATIONS" "$STEP5D_RNN_BACKEND"
"""
        completed = subprocess.run(
            ["bash", "-lc", script],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(completed.stdout.splitlines()[-4:], ["0.010", "0.800", "1024", "cupy"])

    def test_v30_and_p0_operator_profiles_export_canonical_rnn512(self) -> None:
        for profile in (
            "step5d_strict_rnn_ablation_v30",
            "step5d_strict_rnn_no_contact_p0_v8",
        ):
            script = f"""
set -euo pipefail
export BRIDGE_OPERATOR_SOURCE_ONLY=1
export BRIDGE_PROFILE={profile}
source "{ROOT / 'scripts' / 'bridge-line-operator.sh'}"
printf '%s\\n' "$STEP5D_EPSILON" "$STEP5D_SIGR_EXPONENT_R" "$STEP5D_RNN_INNER_ITERATIONS" "$STEP5D_RNN_BACKEND" "$STEP5D_QDOT_LIMIT_RAD_S"
"""
            completed = subprocess.run(
                ["bash", "-lc", script],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(
                completed.returncode,
                0,
                profile + "\n" + completed.stdout + completed.stderr,
            )
            self.assertEqual(
                completed.stdout.splitlines()[-5:],
                ["0.010", "0.800", "512", "cupy", "0.050"],
            )

    def test_v29_bridge_operator_binds_ros_python_and_realtime_launcher(self) -> None:
        script = read_script("bridge-line-operator.sh")
        self.assertIn("/opt/ros/humble/local/lib/python3.10/dist-packages", script)
        self.assertIn("/opt/ros/humble/lib/python3.10/site-packages", script)
        self.assertIn('bridge_launcher=(chrt -f 20 python3)', script)
        self.assertIn("require_v29_realtime_launcher_policy", script)

    def test_v29_realtime_launcher_policy_fails_without_chrt_or_exact_priority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = Path(tmp)
            fake_chrt = fake_bin / "chrt"
            fake_chrt.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            fake_chrt.chmod(0o755)
            common = f'''
set -euo pipefail
export BRIDGE_OPERATOR_SOURCE_ONLY=1
export BRIDGE_PROFILE=step5d_strict_rnn_ablation_v29
source "{ROOT / 'scripts' / 'bridge-line-operator.sh'}"
'''
            missing = subprocess.run(
                ["bash", "-lc", common + "PATH=/nonexistent; require_v29_realtime_launcher_policy"],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(missing.returncode, 24, missing.stdout + missing.stderr)
            wrong = subprocess.run(
                [
                    "bash",
                    "-lc",
                    common
                    + f"PATH={fake_bin}; STEP5D_RT_PRIORITY=19; require_v29_realtime_launcher_policy",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(wrong.returncode, 24, wrong.stdout + wrong.stderr)

    def test_v29_startup_confirmation_requires_ready_sentinel_and_preserves_early_rc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            common = f'''
set -euo pipefail
export BRIDGE_OPERATOR_SOURCE_ONLY=1
export BRIDGE_PROFILE=step5d_strict_rnn_ablation_v29
source "{ROOT / 'scripts' / 'bridge-line-operator.sh'}"
'''
            success = subprocess.run(
                [
                    "bash",
                    "-lc",
                    common
                    + f'''STEP5D_BRIDGE_LAUNCH_NONCE=test-nonce python3 - "{run_dir / 'bridge_ready.json'}" <<'PY' &
import json
import os
import sys
import time
from pathlib import Path

Path(sys.argv[1]).write_text(json.dumps({{
    "ready_schema": "step5d_bridge_ready_v2",
    "ok": True,
    "pid": os.getpid(),
    "launch_nonce": os.environ["STEP5D_BRIDGE_LAUNCH_NONCE"],
    "bridge_profile": "step5d_strict_rnn_ablation_v29",
    "rtde_hz": 500.0,
    "runtime_scheduler": {{"policy": "SCHED_FIFO", "priority": 20}},
    "prewarm_status": "ok",
    "v30_runtime_complete": True,
    "rtde_connected": True,
    "rtde_send_succeeded": True,
    "sensor_stream_ready": True,
    "sensor_samples": 1000,
    "baseline_ready": True,
    "sensor_age_s": 0.001,
    "sensor_stale_s": 0.1,
    "parse_errors": 0,
}}), encoding="utf-8")
time.sleep(0.2)
PY
pid=$!
wait_for_bridge_output_started "{run_dir}" "$pid" test-nonce
wait "$pid"
''',
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(success.returncode, 0, success.stdout + success.stderr)
            self.assertIn("v29 bridge startup confirmed", success.stdout)

            stale = subprocess.run(
                [
                    "bash",
                    "-lc",
                    common
                    + f'''set +e
(sleep 0.2) &
pid=$!
python3 - "{run_dir / 'bridge_ready.json'}" "$pid" <<'PY'
import json
import sys
from pathlib import Path

Path(sys.argv[1]).write_text(json.dumps({{
    "ready_schema": "step5d_bridge_ready_v2",
    "ok": True,
    "pid": int(sys.argv[2]),
    "launch_nonce": "old-nonce",
    "bridge_profile": "step5d_strict_rnn_ablation_v29",
    "rtde_hz": 500.0,
    "runtime_scheduler": {{"policy": "SCHED_FIFO", "priority": 20}},
    "prewarm_status": "ok",
    "v30_runtime_complete": True,
    "rtde_connected": True,
    "rtde_send_succeeded": True,
    "sensor_stream_ready": True,
    "sensor_samples": 1000,
    "baseline_ready": True,
    "sensor_age_s": 0.001,
    "sensor_stale_s": 0.1,
    "parse_errors": 0,
}}), encoding="utf-8")
PY
wait_for_bridge_output_started "{run_dir}" "$pid" new-nonce
startup_rc=$?
wait "$pid"
set -e
printf 'startup_rc=%s\n' "$startup_rc"
test "$startup_rc" -eq 1
''',
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(stale.returncode, 0, stale.stdout + stale.stderr)
            self.assertIn("startup_rc=1", stale.stdout)

            early = subprocess.run(
                [
                    "bash",
                    "-lc",
                    common
                    + '''set +e
(exit 77) &
pid=$!
wait_for_bridge_output_started "''' + str(run_dir) + '''" "$pid" early-nonce
startup_rc=$?
set -e
printf 'startup_rc=%s child_rc=%s\n' "$startup_rc" "$BRIDGE_EARLY_EXIT_RC"
test "$startup_rc" -eq 1
test "$BRIDGE_EARLY_EXIT_RC" -eq 77
''',
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(early.returncode, 0, early.stdout + early.stderr)
            self.assertIn("child_rc=77", early.stdout)

    def test_v29_operator_propagates_child_exit_and_requires_ready_file(self) -> None:
        script = read_script("bridge-line-operator.sh")
        self.assertIn('local ready="${out_dir}/bridge_ready.json"', script)
        self.assertIn('BRIDGE_EARLY_EXIT_RC="$?"', script)
        self.assertIn('return "${child_rc}"', script)

    def test_v29_direct_bridge_gate_receives_every_exact_profile_field(self) -> None:
        script = read_script("bridge-line-operator.sh")
        section = script.split("step5d_live_bridge_authorized()", 1)[1].split("refresh_bench_gate_cache()", 1)[0]

        for flag in (
            "--stage25-control-mode",
            "--rnn-backend",
            "--rnn-inner-iterations",
            "--epsilon",
            "--sigr-exponent-r",
            "--qdot-cap-rad-s",
        ):
            self.assertIn(flag, section)
        self.assertIn('--step5d-qdot-limit-rad-s "${STEP5D_QDOT_LIMIT_RAD_S:-0.050}"', script)

    def test_step5d_contact_bridge_override_cannot_bypass_frozen_v29_state(self) -> None:
        missing_cache = Path(tempfile.gettempdir()) / "missing-step5d-v29-live-cache.json"
        missing_cache.unlink(missing_ok=True)
        env = os.environ.copy()
        env.update(
            {
                "STEP5D_STAGE25_CONTROL_MODE": "speedj_rnn_live",
                "STEP5D_CONFIRM": "LIVE STEP5D STRICT RNN LIVEPREP",
                "LONG_CHECK_CACHE": str(missing_cache),
            }
        )

        completed = subprocess.run(
            ["bash", str(ROOT / "scripts" / "step5d-liveprep-operator.sh"), "contact-bridge"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 24, completed.stdout + completed.stderr)
        self.assertIn("v29_frozen_fallback", completed.stderr or completed.stdout)
        self.assertNotIn("bridge output:", completed.stdout)

    def test_no_contact_p0_capture_profile_has_separate_bridge_gate(self) -> None:
        script = read_script("bridge-line-operator.sh")

        self.assertIn("step5d_strict_rnn_no_contact_p0_v7", script)
        self.assertIn('PROGRAM_LINE="/programs/andyl/kunwei/step5/${BRIDGE_PROFILE}.urp"', script)
        self.assertIn("step5d_no_contact_p0_capture_authorized", script)
        self.assertIn("BRIDGE_ALLOW_NO_CONTACT_P0_CAPTURE", script)
        self.assertIn("step5d_live_bridge_authorized", script)

    def test_no_contact_p0_wrapper_prints_table_preflight(self) -> None:
        script = read_script("step5d-strict-rnn-p0.sh")

        self.assertIn("p0_table_preflight", script)
        self.assertIn("P0 table preflight", script)
        self.assertIn("controller_target", script)
        self.assertIn("sha256", script)
        self.assertIn("duration_s", script)

    def test_no_contact_p0_capture_bridge_uses_preplay_bridge_route(self) -> None:
        script = read_script("step5d-strict-rnn-p0.sh")
        capture_start = script.index("capture-bridge)")
        capture_end = script.index("validate-run)", capture_start)
        capture_body = script[capture_start:capture_end]

        prep_idx = capture_body.index('"${BRIDGE_OPERATOR}" prep-long-checks')
        bridge_idx = capture_body.index('"${BRIDGE_OPERATOR}" line-bridge-fast')
        export_idx = capture_body.index("BRIDGE_REQUIRE_PREPLAY_STOPPED=1")

        self.assertLess(prep_idx, bridge_idx)
        self.assertLess(export_idx, bridge_idx)
        self.assertIn('"${BRIDGE_OPERATOR}" line-bridge-fast', capture_body)
        self.assertIn('export WAIT_FOR_PLAY_S="${WAIT_FOR_PLAY_S:-20}"', capture_body)
        self.assertNotIn('"${BRIDGE_OPERATOR}" line-autowatch', capture_body)
        self.assertNotIn("P0 bridge is running. Now press TP Play", capture_body)
        self.assertIn("set -euo pipefail", script)

    def test_fast_bridge_waits_for_output_before_p0_pre_arm_check(self) -> None:
        script = read_script("bridge-line-operator.sh")
        run_start = script.index("run_bridge_for_mode()")
        run_end = script.index("maybe_start_background_push", run_start)
        run_body = script[run_start:run_end]

        wait_idx = run_body.index('wait_for_bridge_output_started "${out_dir}" "${bridge_pid}" "${launch_nonce}"')
        pre_arm_idx = run_body.index('p0_pre_arm_dashboard_check "${bridge_pid}"')

        self.assertLess(wait_idx, pre_arm_idx)

    def test_no_contact_p0_base_operator_preserves_exported_env(self) -> None:
        script = f"""
set -euo pipefail
export BRIDGE_OPERATOR_SOURCE_ONLY=1
export BRIDGE_PROFILE=step5d_strict_rnn_no_contact_p0_v7
export BRIDGE_DURATION_S=181
export BRIDGE_FORCE_P_GAIN=0.002
export BRIDGE_NORMAL_MIN_FORCE_N=0.002
source "{ROOT / 'scripts' / 'bridge-line-operator.sh'}"
printf '%s\\n' "$BRIDGE_DURATION_S" "$BRIDGE_FORCE_P_GAIN" "$BRIDGE_NORMAL_MIN_FORCE_N"
"""
        completed = subprocess.run(
            ["bash", "-lc", script],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(completed.stdout.splitlines()[-3:], ["181", "0.002", "0.002"])

    def test_no_contact_p0_cupy_backend_bootstrap_uses_configured_pythonpath(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_cupy = Path(tmp) / "cupy"
            fake_cupy.mkdir()
            (fake_cupy / "__init__.py").write_text(
                """
__version__ = 'fake-test-cupy'
float32 = float

class _Array:
    def __init__(self):
        self.value = 0.0
    def get(self):
        return [self.value]

def zeros(_size, dtype=None):
    return _Array()

class RawKernel:
    def __init__(self, _code, _name):
        pass
    def __call__(self, _grid, _block, args):
        args[0].value = 1.0

class _NullStream:
    @staticmethod
    def synchronize():
        pass

class _Stream:
    null = _NullStream()

class _Cuda:
    Stream = _Stream

cuda = _Cuda()
""",
                encoding="utf-8",
            )
            nvrtc_pkg = Path(tmp) / "cupy_backends" / "cuda" / "libs"
            nvrtc_pkg.mkdir(parents=True)
            for pkg in (Path(tmp) / "cupy_backends", Path(tmp) / "cupy_backends" / "cuda", nvrtc_pkg):
                (pkg / "__init__.py").write_text("", encoding="utf-8")
            (nvrtc_pkg / "nvrtc.py").write_text("def getVersion():\n    return (12, 9)\n", encoding="utf-8")
            script = f"""
set -euo pipefail
unset PYTHONPATH
export BRIDGE_OPERATOR_SOURCE_ONLY=1
export BRIDGE_PROFILE=step5d_strict_rnn_no_contact_p0_v7
export STEP5D_RNN_BACKEND=cupy
export STEP5D_CUPY_PYTHONPATH="{tmp}"
source "{ROOT / 'scripts' / 'bridge-line-operator.sh'}"
ensure_step5d_rnn_backend_ready
python3 - <<'PY'
import cupy
print(cupy.__version__)
PY
"""
            completed = subprocess.run(
                ["bash", "-lc", script],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("fake-test-cupy", completed.stdout)

    def test_step5d_workflow_upload_uses_table_resolved_target(self) -> None:
        script = read_script("step5d-workflow.sh")
        upload_calls = [line for line in script.splitlines() if 'python3 "${UPLOAD_TOOL}"' in line]

        self.assertGreaterEqual(len(upload_calls), 2)
        self.assertNotIn('--target-dir "${TARGET_DIR}"', script)
        self.assertNotIn('--target-dir "${target_dir}"', script)

    def test_no_contact_p0_fast_bridge_refuses_without_capture_env_before_start(self) -> None:
        env = os.environ.copy()
        env.update(
            {
                "BRIDGE_PROFILE": "step5d_strict_rnn_no_contact_p0_v7",
                "STEP5D_P0_CONFIRM": "LIVE STEP5D STRICT RNN NO CONTACT P0",
                "BRIDGE_SKIP_BENCH_GATE": "1",
                "BRIDGE_SKIP_LONG_CHECKS": "1",
                "LONG_CHECK_CACHE": str(Path(tempfile.gettempdir()) / "missing-step5d-p0-cache.json"),
            }
        )

        completed = subprocess.run(
            [str(ROOT / "scripts" / "bridge-line-operator.sh"), "line-bridge-fast"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 24, completed.stdout + completed.stderr)
        self.assertIn("no-contact P0 capture is not authorized", completed.stdout + completed.stderr)
        self.assertNotIn("RTDE quick probe passed", completed.stdout + completed.stderr)

    def test_no_contact_p0_fast_bridge_refuses_allow_flag_without_confirm_token(self) -> None:
        env = os.environ.copy()
        env.update(
            {
                "BRIDGE_PROFILE": "step5d_strict_rnn_no_contact_p0_v7",
                "BRIDGE_ALLOW_NO_CONTACT_P0_CAPTURE": "1",
                "BRIDGE_SKIP_BENCH_GATE": "1",
                "BRIDGE_SKIP_LONG_CHECKS": "1",
                "LONG_CHECK_CACHE": str(Path(tempfile.gettempdir()) / "missing-step5d-p0-cache.json"),
            }
        )

        completed = subprocess.run(
            [str(ROOT / "scripts" / "bridge-line-operator.sh"), "line-bridge-fast"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 40, output)
        self.assertIn("STEP5D_P0_CONFIRM", output)
        self.assertNotIn("skipping long bench gate", output)
        self.assertNotIn("RTDE quick probe passed", output)

    def test_no_contact_p0_autowatch_refuses_before_bench_gate(self) -> None:
        env = os.environ.copy()
        env.update(
            {
                "BRIDGE_PROFILE": "step5d_strict_rnn_no_contact_p0_v7",
                "STEP5D_P0_CONFIRM": "LIVE STEP5D STRICT RNN NO CONTACT P0",
                "BRIDGE_SKIP_BENCH_GATE": "1",
                "BRIDGE_SKIP_LONG_CHECKS": "1",
                "LONG_CHECK_CACHE": str(Path(tempfile.gettempdir()) / "missing-step5d-p0-cache.json"),
            }
        )

        completed = subprocess.run(
            [str(ROOT / "scripts" / "bridge-line-operator.sh"), "line-autowatch"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 24, output)
        self.assertIn("no-contact P0 capture is not authorized", output)
        self.assertNotIn("skipping long bench gate", output)
        self.assertNotIn("RTDE quick probe passed", output)

    def test_no_contact_p0_autowatch_refuses_even_when_capture_authorized(self) -> None:
        env = os.environ.copy()
        env.update(
            {
                "BRIDGE_PROFILE": "step5d_strict_rnn_no_contact_p0_v7",
                "STEP5D_P0_CONFIRM": "LIVE STEP5D STRICT RNN NO CONTACT P0",
                "BRIDGE_ALLOW_NO_CONTACT_P0_CAPTURE": "1",
                "BRIDGE_SKIP_BENCH_GATE": "1",
                "BRIDGE_SKIP_LONG_CHECKS": "1",
                "AUTOWATCH_WAIT_FOR_PLAY_S": "0.01",
                "LONG_CHECK_CACHE": str(Path(tempfile.gettempdir()) / "missing-step5d-p0-cache.json"),
            }
        )

        completed = subprocess.run(
            [str(ROOT / "scripts" / "bridge-line-operator.sh"), "line-autowatch"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 24, output)
        self.assertIn("P0 capture requires bridge-before-Play", output)
        self.assertIn("rerun capture-bridge", output)
        self.assertNotIn("RTDE quick probe passed", output)

    def test_no_contact_p0_trigger_dashboard_already_running_is_hard_refusal(self) -> None:
        script = f"""
set -euo pipefail
export BRIDGE_OPERATOR_SOURCE_ONLY=1
export BRIDGE_PROFILE=step5d_strict_rnn_no_contact_p0_v7
source "{ROOT / 'scripts' / 'bridge-line-operator.sh'}"
dashboard_snapshot() {{
  printf '%s\\n' 'Program running: true' 'Loaded program: /programs/andyl/kunwei/step5/step5d_strict_rnn_no_contact_p0_v7.urp' 'PLAYING' 'Safetymode: NORMAL'
  return 10
}}
set +e
trigger_dashboard_check
rc="$?"
set -e
printf 'rc=%s\\n' "$rc"
"""
        completed = subprocess.run(
            ["bash", "-lc", script],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 0, output)
        self.assertIn("rc=24", output)
        self.assertIn("TP program is already PLAYING before P0 bridge armed", output)
        self.assertIn("rerun capture-bridge", output)
        self.assertNotIn("starting bridge late with already_running=1", output)

    def test_non_p0_trigger_dashboard_already_running_late_bridge_path_is_unchanged(self) -> None:
        script = f"""
set -euo pipefail
export BRIDGE_OPERATOR_SOURCE_ONLY=1
export BRIDGE_PROFILE=step5d_strict_rnn_ablation_v25
source "{ROOT / 'scripts' / 'bridge-line-operator.sh'}"
dashboard_snapshot() {{
  printf '%s\\n' 'Program running: true' 'Loaded program: /programs/andyl/kunwei/step5/step5d_strict_rnn_ablation_v25.urp' 'PLAYING' 'Safetymode: NORMAL'
  return 10
}}
set +e
trigger_dashboard_check
rc="$?"
set -e
printf 'rc=%s\\n' "$rc"
"""
        completed = subprocess.run(
            ["bash", "-lc", script],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 0, output)
        self.assertIn("rc=10", output)
        self.assertIn("starting bridge late with already_running=1", output)
        self.assertNotIn("exact v7", output)
        self.assertNotIn("capture-bridge", output)

    def test_require_preplay_stopped_env_hard_refuses_non_p0_already_running(self) -> None:
        script = f"""
set -euo pipefail
export BRIDGE_OPERATOR_SOURCE_ONLY=1
export BRIDGE_PROFILE=step5d_strict_rnn_ablation_v25
export BRIDGE_REQUIRE_PREPLAY_STOPPED=1
source "{ROOT / 'scripts' / 'bridge-line-operator.sh'}"
dashboard_snapshot() {{
  printf '%s\\n' 'Program running: true' 'Loaded program: /programs/andyl/kunwei/step5/step5d_strict_rnn_ablation_v25.urp' 'PLAYING' 'Safetymode: NORMAL'
  return 10
}}
set +e
trigger_dashboard_check
rc="$?"
set -e
printf 'rc=%s\\n' "$rc"
"""
        completed = subprocess.run(
            ["bash", "-lc", script],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 0, output)
        self.assertIn("rc=24", output)
        self.assertIn("already running before bridge arm", output)
        self.assertNotIn("starting bridge late with already_running=1", output)
        self.assertNotIn("exact v7", output)
        self.assertNotIn("capture-bridge", output)

    def test_no_contact_p0_pre_arm_recheck_stops_bridge_if_played_early(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stop_log = Path(tmp) / "stop.log"
            script = f"""
set -euo pipefail
export BRIDGE_OPERATOR_SOURCE_ONLY=1
export BRIDGE_PROFILE=step5d_strict_rnn_no_contact_p0_v7
source "{ROOT / 'scripts' / 'bridge-line-operator.sh'}"
dashboard_snapshot() {{
  printf '%s\\n' 'Program running: true' 'Loaded program: /programs/andyl/kunwei/step5/step5d_strict_rnn_no_contact_p0_v7.urp' 'PLAYING' 'Safetymode: NORMAL'
  return 10
}}
stop_bridge_process() {{
  printf 'pid=%s reason=%s\\n' "$1" "$2" >"{stop_log}"
}}
set +e
p0_pre_arm_dashboard_check 4242
rc="$?"
set -e
printf 'rc=%s\\n' "$rc"
"""
            completed = subprocess.run(
                ["bash", "-lc", script],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            output = completed.stdout + completed.stderr
            self.assertEqual(completed.returncode, 0, output)
            self.assertIn("rc=24", output)
            self.assertIn("TP Play happened before P0 bridge armed", output)
            self.assertEqual(stop_log.read_text(encoding="utf-8").strip(), "pid=4242 reason=TP Play happened before P0 bridge armed")

    def test_no_contact_p0_pre_arm_unexpected_dashboard_state_stops_bridge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stop_log = Path(tmp) / "stop.log"
            script = f"""
set -euo pipefail
export BRIDGE_OPERATOR_SOURCE_ONLY=1
export BRIDGE_PROFILE=step5d_strict_rnn_no_contact_p0_v7
source "{ROOT / 'scripts' / 'bridge-line-operator.sh'}"
dashboard_snapshot() {{
  printf '%s\\n' 'Program running: false' 'Loaded program: <unknown>' 'Safetymode: NORMAL'
  return 99
}}
stop_bridge_process() {{
  printf 'pid=%s reason=%s\\n' "$1" "$2" >"{stop_log}"
}}
set +e
p0_pre_arm_dashboard_check 4242
rc="$?"
set -e
printf 'rc=%s\\n' "$rc"
"""
            completed = subprocess.run(
                ["bash", "-lc", script],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            output = completed.stdout + completed.stderr
            self.assertEqual(completed.returncode, 0, output)
            self.assertIn("rc=99", output)
            self.assertIn("Dashboard state is not ready for P0 bridge arm", output)
            self.assertFalse(any(line.startswith("[operator] P0 bridge armed") for line in output.splitlines()))
            self.assertEqual(stop_log.read_text(encoding="utf-8").strip(), "pid=4242 reason=P0 pre-arm dashboard not ready")

    def test_step5d_bridge_path_does_not_background_git_push(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = f"""
	set -euo pipefail
	export BRIDGE_OPERATOR_SOURCE_ONLY=1
export BRIDGE_PROFILE=step5d_strict_rnn_ablation_v25
export BRIDGE_BACKGROUND_PUSH_AFTER_LIVE=1
source "{ROOT / 'scripts' / 'bridge-line-operator.sh'}"
maybe_start_background_push "{tmp}"
"""
            completed = subprocess.run(
                ["bash", "-lc", script],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertNotIn("background git push started", completed.stdout)
            self.assertFalse((Path(tmp) / "background_git_push.log").exists())

    def test_autowatch_bridge_uses_live_authorization_gate_before_start(self) -> None:
        script = read_script("bridge-line-operator.sh")
        autowatch_start = script.index("*-autowatch)")
        autowatch_end = script.index("*-bridge-fast)", autowatch_start)
        autowatch_body = script[autowatch_start:autowatch_end]

        gate_idx = autowatch_body.index("step5d_live_bridge_authorized")
        wait_idx = autowatch_body.index("wait_for_tp_play_autowatch")
        run_idx = autowatch_body.index("run_bridge_for_mode")

        self.assertLess(gate_idx, wait_idx)
        self.assertLess(gate_idx, run_idx)

    def test_fast_bridge_uses_two_hour_fingerprint_cache_and_rtde_probe(self) -> None:
        script = read_script("bridge-line-operator.sh")

        self.assertIn('LONG_CHECK_TTL_S="${LONG_CHECK_TTL_S:-7200}"', script)
        self.assertIn("step5d_runtime_interface", script)
        self.assertIn('"fingerprint": current_fingerprint(gate)', script)
        self.assertIn("long_check_cache_status", script)
        self.assertIn("require_rtde_quick_probe", script)
        self.assertIn("(host, 30004)", script)

    def test_step5d_workflow_separates_dev_promote_and_live(self) -> None:
        script = read_script("step5d-workflow.sh")

        self.assertIn('LATEST_CANDIDATE_INDEX="${RUN_ROOT}/local_tp_packages/.latest_step5d_candidate.json"', script)
        self.assertIn('PROMOTE_TOOL="${ROOT}/tools/promote_step5d_current.py"', script)
        self.assertIn('PUBLISH_GATE="${ROOT}/tools/verify_step5d_publish_gate.py"', script)
        self.assertIn("record_latest_candidate()", script)
        self.assertIn("latest_candidate_exports()", script)
        self.assertIn('record_latest_candidate "${candidate_dir}"', script)
        self.assertIn("promoting latest local-only candidate", script)
        self.assertIn('READBACK_GATE="${ROOT}/tools/verify_step5d_current_binding.py"', script)
        self.assertIn('python3 "${PROMOTE_TOOL}"', script)
        self.assertIn('python3 "${PUBLISH_GATE}" --root "${ROOT}" --json', script)
        self.assertIn("dev-loop)", script)
        self.assertIn("--local-only --output-dir", script)
        self.assertIn("--dry-run", script)
        self.assertIn("not delivered; current_stage unchanged", script)
        self.assertIn("promote-package)", script)
        self.assertIn("--allow-local-candidate-promote", script)
        self.assertIn("contact-bridge)", script)
        self.assertIn('"${OPERATOR}" contact-bridge', script)
        contact_section = script.split("contact-bridge)", 1)[1]
        self.assertNotIn("build_step5d_liveprep.py", contact_section)
        self.assertNotIn("upload_ur_tp_package.py", contact_section)

    def test_fast_bridge_requires_fresh_cache_even_when_skip_env_is_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env.update(
                {
                    "BRIDGE_PROFILE": "step5d_strict_rnn_liveprep_v19",
                    "LONG_CHECK_CACHE": str(Path(tmp) / "missing-cache.json"),
                    "BRIDGE_SKIP_BENCH_GATE": "1",
                    "BRIDGE_SKIP_LONG_CHECKS": "1",
                }
            )
            completed = subprocess.run(
                [str(ROOT / "scripts" / "bridge-line-operator.sh"), "line-bridge-fast"],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 24, completed.stdout + completed.stderr)
        self.assertIn("refusing fast bridge", completed.stdout)
        self.assertNotIn("skipping long bench gate cache requirement", completed.stdout)


if __name__ == "__main__":
    unittest.main()
