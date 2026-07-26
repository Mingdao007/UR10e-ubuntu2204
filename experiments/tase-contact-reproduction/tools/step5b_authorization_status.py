#!/usr/bin/env python3
"""Step5b live-contact authorization status gate.

Single read-only reporter for whether Step5b live contact is authorized. It
prints the LOCKED defaults (which MUST NOT be re-asked of the user) and the
OPEN gates that remain, and exits nonzero whenever Step5b is not fully
authorized so it can block in a launch path.

This gate authorizes nothing on its own: it never starts a bridge, never
plays a TP program, never moves the robot, and never calls zero_ftsensor().
It only reports state read from:

  - config/step5b_authorization_state.json        (LOCKED defaults + OPEN gates)
  - latest runs/step5b_zero_policy_readiness_*/summary.json   (readiness)
  - config/step5b_live_runner_acceptance.json     (live runner acceptance)

The point of this gate is mechanical: the four LOCKED values live here as data
the agent can read, so they are not regenerated as open questions after a
context reset.
"""
from __future__ import annotations

import argparse
import json
import py_compile
import subprocess
import sys
from pathlib import Path
from typing import Any

EXPERIMENT = Path(__file__).resolve().parents[1]
CONFIG = EXPERIMENT / "config"
RUNS = EXPERIMENT / "runs"
LEDGER_PATH = CONFIG / "step5b_authorization_state.json"
ACCEPTANCE_PATH = CONFIG / "step5b_live_runner_acceptance.json"
READINESS_DIR_GLOB = "step5b_zero_policy_readiness_*"

REQUIRED_LOCKED_KEYS = ("force_source", "zero_policy", "route", "params_source")
ZERO_POLICY_KEYS = (
    "ur_zero_ftsensor_called",
    "kunwei_hardware_tare_or_config_written",
    "software_baseline_subtraction",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report Step5b live-contact authorization status; exit nonzero unless fully authorized.",
    )
    parser.add_argument("--ledger", type=Path, default=LEDGER_PATH)
    parser.add_argument("--acceptance", type=Path, default=ACCEPTANCE_PATH)
    parser.add_argument("--runs-dir", type=Path, default=RUNS)
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON only.")
    args = parser.parse_args(argv)

    report = evaluate(
        ledger_path=args.ledger,
        acceptance_path=args.acceptance,
        runs_dir=args.runs_dir,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render(report))
    return 0 if report["authorized"] else 2


def evaluate(*, ledger_path: Path, acceptance_path: Path, runs_dir: Path) -> dict[str, Any]:
    blocking: list[str] = []

    ledger = load_json(ledger_path)
    if ledger is None:
        blocking.append(f"ledger_unreadable:{ledger_path}")
        locked: dict[str, Any] = {}
        open_section: dict[str, Any] = {}
    else:
        locked = ledger.get("locked", {}) or {}
        for key in REQUIRED_LOCKED_KEYS:
            if not locked.get(key) and locked.get(key) is not False:
                blocking.append(f"ledger_missing_locked:{key}")
        open_section = ledger.get("open_gates", {}) or {}

    # User Step5b contact authorization is a LOCKED fact (already given). Its
    # absence is a real problem, but it must never be reported as a blocker when
    # it is true. The per-run go is operator_final_trigger_received (OPEN gate).
    user_contact_auth = bool(locked.get("user_step5b_contact_authorization", False))
    if not user_contact_auth:
        blocking.append("user_step5b_contact_authorization:not_set")
    operator_trigger = bool(open_section.get("operator_final_trigger_received", False))

    readiness = evaluate_readiness(runs_dir)
    if not readiness["met"]:
        blocking.append(f"readiness_artifact_pass:{readiness['reason']}")

    drift = evaluate_policy_drift(locked, readiness.get("summary"))
    if drift["mismatch"]:
        blocking.append("policy_drift:" + ";".join(drift["details"]))

    acceptance = evaluate_acceptance(acceptance_path, locked_route=locked.get("route"))
    if not acceptance["met"]:
        blocking.append(f"live_runner_auditor_accepted:{acceptance['reason']}")

    if not operator_trigger:
        blocking.append("operator_final_trigger_received:not_set")

    open_gates = {
        "readiness_artifact_pass": readiness,
        "live_runner_auditor_accepted": acceptance,
        "operator_final_trigger_received": {"met": operator_trigger, "reason": "ledger field set" if operator_trigger else "ledger field false"},
    }
    authorized = not blocking
    return {
        "role": "step5b_live_contact_authorization_status",
        "authorized": authorized,
        "step5b_live_contact_authorized": authorized,
        "motion_authorized": False,
        "user_step5b_contact_authorization": user_contact_auth,
        "locked": render_locked(locked),
        "open_gates": open_gates,
        "policy_drift": drift,
        "blocking_reasons": blocking,
        "next_action": next_action(blocking),
    }


def evaluate_readiness(runs_dir: Path) -> dict[str, Any]:
    summary_path = latest_readiness_summary(runs_dir)
    if summary_path is None:
        return {"met": False, "reason": "no_readiness_run_found", "summary_path": None, "summary": None}
    summary = load_json(summary_path)
    if summary is None:
        return {"met": False, "reason": f"summary_unreadable:{summary_path}", "summary_path": str(summary_path), "summary": None}
    ok = bool(summary.get("ok"))
    return {
        "met": ok,
        "reason": "summary_ok" if ok else f"summary_not_ok:{summary.get('failure_reason')}",
        "summary_path": str(summary_path),
        "summary": summary,
    }


def latest_readiness_summary(runs_dir: Path) -> Path | None:
    if not runs_dir.is_dir():
        return None
    candidates = sorted(
        (p for p in runs_dir.glob(f"{READINESS_DIR_GLOB}/summary.json") if p.is_file()),
        key=lambda p: p.parent.name,
    )
    return candidates[-1] if candidates else None


def evaluate_policy_drift(locked: dict[str, Any], summary: dict[str, Any] | None) -> dict[str, Any]:
    if not locked or summary is None:
        return {"mismatch": False, "checked": False, "details": []}
    details: list[str] = []
    if "force_source" in summary and summary.get("force_source") != locked.get("force_source"):
        details.append(f"force_source ledger={locked.get('force_source')} readiness={summary.get('force_source')}")
    ledger_zero = locked.get("zero_policy", {}) or {}
    summary_zero = summary.get("zero_policy", {}) or {}
    for key in ZERO_POLICY_KEYS:
        if key in summary_zero and key in ledger_zero and summary_zero.get(key) != ledger_zero.get(key):
            details.append(f"zero_policy.{key} ledger={ledger_zero.get(key)} readiness={summary_zero.get(key)}")
    return {"mismatch": bool(details), "checked": True, "details": details}


def evaluate_acceptance(acceptance_path: Path, locked_route: str | None = None) -> dict[str, Any]:
    acceptance = load_json(acceptance_path)
    if acceptance is None:
        return {"met": False, "reason": f"acceptance_unreadable:{acceptance_path}"}
    if not bool(acceptance.get("accepted")):
        return {"met": False, "reason": "accepted_false"}
    entrypoint = str(acceptance.get("live_runner_entrypoint") or "").strip()
    if not entrypoint:
        return {"met": False, "reason": "accepted_true_but_no_entrypoint"}
    path = Path(entrypoint)
    if not path.is_absolute():
        path = (EXPERIMENT / entrypoint).resolve()
    if not path.is_file():
        return {"met": False, "reason": f"entrypoint_missing:{path}"}
    ok, why = entrypoint_compiles(path)
    if not ok:
        return {"met": False, "reason": f"entrypoint_does_not_compile:{why}"}
    route = str(acceptance.get("live_runner_route") or "").strip()
    if not route:
        return {"met": False, "reason": "accepted_true_but_no_route"}
    if locked_route is not None and route != locked_route:
        return {"met": False, "reason": f"route_mismatch:accepted={route} locked={locked_route}"}
    return {"met": True, "reason": "accepted_entrypoint_and_route_valid", "entrypoint": str(path), "route": route}


def entrypoint_compiles(path: Path) -> tuple[bool, str]:
    suffix = path.suffix.lower()
    try:
        if suffix == ".py":
            py_compile.compile(str(path), doraise=True)
            return True, "py_compile_ok"
        if suffix == ".sh":
            result = subprocess.run(
                ["bash", "-n", str(path)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                return True, "bash_n_ok"
            return False, result.stderr.strip() or "bash -n failed"
        return False, f"unsupported_entrypoint_suffix:{suffix or 'none'}"
    except py_compile.PyCompileError as exc:
        return False, str(exc).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


def next_action(blocking: list[str]) -> str:
    if not blocking:
        return "All OPEN gates green: Step5b live contact may be authorized per the auditor/role chain."
    head = blocking[0]
    if head.startswith("readiness_artifact_pass"):
        return "Run step5b_zero_policy_check.sh on the current bench to produce a passing readiness summary."
    if head.startswith("policy_drift"):
        return "Resolve the policy drift between the ledger LOCKED values and the readiness summary before anything else."
    if head.startswith("live_runner_auditor_accepted"):
        if "route_mismatch" in head:
            return "Accepted live runner route differs from the locked route; only a runner on the locked route (or an explicit user route reopen) can be accepted."
        return "Have the Implementor deliver a live Step5b runner on the locked route; Auditor records acceptance in config/step5b_live_runner_acceptance.json."
    if head.startswith("operator_final_trigger_received"):
        return "Per-run operator trigger not set; set operator_final_trigger_received=true in the ledger only at run time, after readiness + route-matched acceptance are green."
    if head.startswith("user_step5b_contact_authorization"):
        return "User Step5b contact authorization is missing from the ledger; only the user can set it."
    return "Resolve the first blocking reason listed above."


def render_locked(locked: dict[str, Any]) -> dict[str, Any]:
    return {
        "note": "LOCKED — do not re-ask the user.",
        "force_source": locked.get("force_source"),
        "zero_policy": locked.get("zero_policy"),
        "route": locked.get("route"),
        "params_source": locked.get("params_source"),
    }


def render(report: dict[str, Any]) -> str:
    locked = report["locked"]
    zero = locked.get("zero_policy") or {}
    lines = [
        "Step5b live-contact authorization status",
        "=" * 44,
        f"AUTHORIZED: {report['authorized']}  (this gate authorizes no motion by itself)",
        "",
        "LOCKED defaults (do NOT re-ask the user):",
        f"  - force_source = {locked.get('force_source')}",
        f"  - zero_policy  = software_baseline_only "
        f"(zero_ftsensor={zero.get('ur_zero_ftsensor_called')}, "
        f"kunwei_tare={zero.get('kunwei_hardware_tare_or_config_written')}, "
        f"software_baseline={zero.get('software_baseline_subtraction')})",
        f"  - route        = {locked.get('route')} (TP/bridge is NOT a selectable live route)",
        f"  - params       = {locked.get('params_source')}",
        f"  - user_step5b_contact_authorization = {report.get('user_step5b_contact_authorization')} (already given; do NOT re-ask)",
        "",
        "OPEN gates (the only items still to settle):",
    ]
    for name in ("readiness_artifact_pass", "live_runner_auditor_accepted", "operator_final_trigger_received"):
        gate = report["open_gates"][name]
        mark = "PASS" if gate.get("met") else "NOT-MET"
        lines.append(f"  [{mark}] {name}: {gate.get('reason')}")
    drift = report["policy_drift"]
    if drift.get("mismatch"):
        lines.append("")
        lines.append("POLICY DRIFT (blocks authorization):")
        for detail in drift["details"]:
            lines.append(f"  - {detail}")
    lines.append("")
    lines.append(f"Next action: {report['next_action']}")
    return "\n".join(lines)


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


if __name__ == "__main__":
    raise SystemExit(main())
