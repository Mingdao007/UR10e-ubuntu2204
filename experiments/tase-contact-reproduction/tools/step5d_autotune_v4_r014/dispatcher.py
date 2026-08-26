"""Typed Home-first command dispatcher for the public R014 entrypoint."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping, Sequence

from .catalog import catalog_document
from .common import R014Error, atomic_write_json, load_json, sha256_value
from .profiles import FORMAL_PROFILE_NAME, ProfileRegistry, StrategyProfile


RUN_SCHEMA = "step5d.autotuner-r014/run-state-v1"
BACKEND_CONTRACT_SCHEMA = "step5d.autotuner-r014/live-backend-contract-v1"
BACKEND_CONTRACT_VERSION = 1
BACKEND_PLACEHOLDERS = ("{run_dir}", "{plan_path}")
RUN_STATES = (
    "CREATED",
    "PREFLIGHT_OK",
    "HOME_VERIFIED",
    "RUNNING",
    "STOP_REQUESTED",
    "RETURN_HOME",
    "STOPPED_HOME",
    "CLOSED",
)


class Action(str, Enum):
    START = "start"
    STATUS = "status"
    STOP = "stop"
    RESUME = "resume"


@dataclass(frozen=True)
class Request:
    action: Action
    trajectory: str
    mode: str
    attempts: int | None
    candidate: str | None
    strategy: str
    contact: str
    early_stop: str
    run_id: str | None
    dry_run: bool
    json_output: bool


@dataclass(frozen=True)
class LiveBackendContract:
    """Owner-supplied executable binding for one qualified R014 plan."""

    executable: Path
    arguments: tuple[str, ...]
    profile_sha256: str
    source_identity: Mapping[str, Any]
    catalog_sha256: str
    metric: Mapping[str, Any]

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        experiment_root: Path,
        plan: Mapping[str, Any],
    ) -> "LiveBackendContract":
        raw = load_json(path)
        required = {
            "schema",
            "version",
            "executable",
            "arguments",
            "profile_sha256",
            "source_identity",
            "catalog_sha256",
            "metric",
        }
        if set(raw) != required:
            raise R014Error("live backend contract fields differ")
        if raw.get("schema") != BACKEND_CONTRACT_SCHEMA or raw.get("version") != BACKEND_CONTRACT_VERSION:
            raise R014Error("live backend contract schema/version mismatch")

        executable_value = raw.get("executable")
        if not isinstance(executable_value, str) or not executable_value.strip():
            raise R014Error("live backend executable is missing")
        executable = Path(executable_value)
        if not executable.is_absolute():
            executable = experiment_root / executable
        executable = executable.resolve()
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise R014Error("live backend executable is not executable")

        arguments_value = raw.get("arguments")
        if (
            not isinstance(arguments_value, list)
            or not arguments_value
            or any(not isinstance(value, str) for value in arguments_value)
        ):
            raise R014Error("live backend arguments must be a nonempty string list")
        arguments = tuple(arguments_value)
        if any(arguments.count(placeholder) != 1 for placeholder in BACKEND_PLACEHOLDERS):
            raise R014Error("live backend arguments must bind run_dir and plan_path exactly once")

        source_identity = raw.get("source_identity")
        metric = raw.get("metric")
        if not isinstance(source_identity, Mapping) or not isinstance(metric, Mapping):
            raise R014Error("live backend identity bindings must be objects")
        command = plan.get("command")
        if not isinstance(command, Mapping):
            raise R014Error("dispatch plan command is not typed")
        bindings = (
            ("profile_sha256", raw.get("profile_sha256"), command.get("profile_sha256")),
            ("source_identity", dict(source_identity), command.get("source_identity")),
            ("catalog_sha256", raw.get("catalog_sha256"), command.get("catalog_sha256")),
            ("metric", dict(metric), command.get("metric")),
        )
        for name, actual, expected in bindings:
            if actual != expected:
                raise R014Error(f"live backend {name} binding differs from dispatch plan")
        return cls(
            executable=executable,
            arguments=arguments,
            profile_sha256=str(raw["profile_sha256"]),
            source_identity=dict(source_identity),
            catalog_sha256=str(raw["catalog_sha256"]),
            metric=dict(metric),
        )

    def command(self, *, run_dir: Path, plan_path: Path) -> list[str]:
        replacements = {"{run_dir}": str(run_dir), "{plan_path}": str(plan_path)}
        return [
            str(self.executable),
            *[replacements.get(argument, argument) for argument in self.arguments],
        ]


class WriterLock:
    """Process-held global live writer lock."""

    def __init__(self, path: Path):
        self.path = path
        self._stream: Any = None

    def __enter__(self) -> "WriterLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._stream.close()
            raise R014Error("another autotuner live writer holds the single-writer lock") from exc
        self._stream.seek(0)
        self._stream.truncate()
        self._stream.write(f"pid={os.getpid()}\n")
        self._stream.flush()
        return self

    def __exit__(self, *_exc: object) -> None:
        assert self._stream is not None
        fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        self._stream.close()


def _validate_closed_backend_state(
    state: Mapping[str, Any],
    *,
    run_id: str,
    plan: Mapping[str, Any],
    stop_marker: Path,
) -> None:
    """Read back the owner boundary before the dispatcher claims success."""

    if state.get("schema") != RUN_SCHEMA or state.get("version") != 1:
        raise R014Error("owner backend run-state schema/version mismatch")
    if state.get("run_id") != run_id:
        raise R014Error("owner backend run id differs")
    command = plan.get("command")
    if not isinstance(command, Mapping):
        raise R014Error("dispatch plan command is not typed")
    for name in (
        "profile_path",
        "profile_sha256",
        "source_identity",
        "catalog_sha256",
        "metric",
    ):
        if state.get(name) != command.get(name):
            raise R014Error(f"owner backend {name} identity differs")
    if state.get("campaign_fingerprint") != plan.get("campaign_fingerprint"):
        raise R014Error("owner backend campaign_fingerprint identity differs")
    if state.get("home_before_arm") is not True:
        raise R014Error("owner backend did not preserve Home-before-ARM")

    stop_requested = stop_marker.is_file()
    if stop_requested:
        try:
            marker = stop_marker.read_text(encoding="utf-8")
        except OSError as exc:
            raise R014Error("graceful stop marker cannot be read back") from exc
        if marker != "graceful\n":
            raise R014Error("graceful stop marker differs")
    expected_transitions = [
        "CREATED",
        "PREFLIGHT_OK",
        "HOME_VERIFIED",
        "RUNNING",
    ]
    if stop_requested:
        expected_transitions.append("STOP_REQUESTED")
    expected_transitions.extend(("RETURN_HOME", "STOPPED_HOME", "CLOSED"))
    transitions = state.get("transitions")
    if transitions != expected_transitions:
        raise R014Error("owner backend state transitions are incomplete or out of order")
    if state.get("state") != "CLOSED":
        raise R014Error("owner backend did not close the run")
    if state.get("home_verified") is not True or state.get("closed_home") is not True:
        raise R014Error("owner backend closed-Home read-back is incomplete")
    terminal_home = state.get("terminal_home")
    if (
        not isinstance(terminal_home, Mapping)
        or terminal_home.get("state") != "STOPPED_HOME"
        or terminal_home.get("home_verified") is not True
        or terminal_home.get("closed_home") is not True
    ):
        raise R014Error("owner backend terminal Home evidence is missing")


def _trajectory(value: str) -> str:
    normalized = value.lower()
    if normalized in {"circle", "arc", "circular-arc"}:
        raise argparse.ArgumentTypeError(
            f"unsupported trajectory '{value}'; v1 supports only cycloid or figure8"
        )
    if normalized not in {"cycloid", "figure8"}:
        raise argparse.ArgumentTypeError(f"unknown trajectory '{value}'")
    return normalized


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autotuner.sh",
        description="Qualification-aware Home-first force-control autotuner.",
    )
    parser.add_argument(
        "action",
        nargs="?",
        choices=tuple(action.value for action in Action),
        default=Action.START.value,
    )
    parser.add_argument("--trajectory", type=_trajectory, default="cycloid")
    parser.add_argument(
        "--mode",
        choices=("continuous", "budgeted", "certify", "fixed"),
        default="continuous",
    )
    parser.add_argument("--attempts", type=int)
    parser.add_argument("--candidate")
    parser.add_argument(
        "--strategy", choices=("current", "finite-time", "legacy-r1"), default="current"
    )
    parser.add_argument(
        "--contact", choices=("current", "two-stage", "sigmoid"), default="current"
    )
    parser.add_argument(
        "--early-stop", choices=("auto", "on", "off"), default="auto"
    )
    parser.add_argument("--run", dest="run_id")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", dest="json_output", action="store_true")
    return parser


def parse_request(argv: Sequence[str] | None = None) -> Request:
    args = build_parser().parse_args(argv)
    request = Request(
        action=Action(args.action),
        trajectory=args.trajectory,
        mode=args.mode,
        attempts=args.attempts,
        candidate=args.candidate,
        strategy=args.strategy,
        contact=args.contact,
        early_stop=args.early_stop,
        run_id=args.run_id,
        dry_run=args.dry_run,
        json_output=args.json_output,
    )
    validate_request(request)
    return request


def validate_request(request: Request) -> None:
    if request.action is not Action.START:
        return
    if request.mode == "budgeted":
        if request.attempts is None or request.attempts < 1:
            raise R014Error("budgeted mode requires --attempts N with N >= 1")
    elif request.mode == "fixed":
        if request.attempts is None:
            object.__setattr__(request, "attempts", 1)
        if request.attempts is None or request.attempts < 1 or not request.candidate:
            raise R014Error("fixed mode requires --candidate ID and positive --attempts")
    elif request.attempts is not None:
        raise R014Error(f"--attempts is not valid for {request.mode} mode")
    if request.candidate is not None and request.mode != "fixed":
        raise R014Error("--candidate is valid only in fixed mode")
    if request.mode == "certify" and request.strategy == "legacy-r1":
        raise R014Error("formal certification refuses legacy-r1")
    if request.mode == "certify" and request.contact == "sigmoid":
        raise R014Error("formal certification locks contact=two-stage")
    if request.mode == "certify" and request.early_stop == "on":
        raise R014Error("formal certification forces early-stop=off")


class Dispatcher:
    def __init__(self, experiment_root: Path, *, state_root: Path | None = None):
        self.experiment_root = experiment_root.resolve()
        self.registry = ProfileRegistry(self.experiment_root)
        configured = os.environ.get("AUTOTUNER_R014_STATE_ROOT")
        self.state_root = (
            Path(configured).resolve()
            if configured
            else (state_root.resolve() if state_root else self.experiment_root / "runs/r014_autotuner")
        )
        self.runs_root = self.state_root / "runs"
        self.writer_lock = self.state_root / "live-writer.lock"

    def _resolve_profile(self, request: Request) -> tuple[StrategyProfile, list[str]]:
        blockers: list[str] = []
        try:
            profile = self.registry.by_name(request.strategy)
        except R014Error as exc:
            if not request.dry_run or request.strategy != "current":
                raise
            profile = self.registry.by_name("finite-time")
            blockers.append(f"current-qualified-unavailable: {exc}")
        return profile, blockers

    @staticmethod
    def _effective_contact(request: Request, profile: StrategyProfile) -> str:
        if request.mode == "certify":
            return "two-stage"
        if request.contact == "current":
            return str(profile.raw["contact"]["default"])
        return request.contact

    @staticmethod
    def _effective_early_stop(request: Request) -> str:
        if request.mode in {"certify", "fixed"}:
            return "off"
        if request.early_stop == "auto":
            return "on"
        return request.early_stop

    def _figure8_blockers(self) -> list[str]:
        receipt = self.registry.root / "qualification/figure8.json"
        if not receipt.is_file():
            return [
                "figure8-v5-controller-readback-missing",
                "figure8-home-no-contact-receipt-missing",
                "figure8-three-non-bo-contact-receipts-missing",
                "figure8-timing-rollover-receipts-missing",
            ]
        value = load_json(receipt)
        required = (
            "controller_readback",
            "home_no_contact",
            "three_non_bo_contact_acquisitions",
            "timing_rollover",
        )
        return [f"figure8-{key}-not-qualified" for key in required if value.get(key) is not True]

    def _plan(self, request: Request) -> dict[str, Any]:
        profile, blockers = self._resolve_profile(request)
        contact = self._effective_contact(request, profile)
        early_stop = self._effective_early_stop(request)
        catalog = catalog_document()

        if request.mode == "certify":
            if profile.name != FORMAL_PROFILE_NAME or profile.solver_id != "finite-time-r08":
                blockers.append("certification-requires-exact-finite-time-r08-formal-profile")
            if contact != "two-stage":
                blockers.append("certification-requires-two-stage-contact")
        if contact == "sigmoid":
            blockers.append("sigmoid-live-qualification-absent")
        if request.trajectory == "figure8":
            blockers.extend(self._figure8_blockers())
        if profile.qualification_status != "qualified":
            blockers.append(f"profile-not-qualified:{profile.qualification_status}")
        source_identity = profile.raw.get("host_source")
        if not isinstance(source_identity, Mapping) or not source_identity:
            blockers.append("profile-source-identity-missing")
            source_identity = {}
        metric = profile.raw.get("metric")
        if not isinstance(metric, Mapping) or not metric:
            blockers.append("profile-metric-identity-missing")
            metric = {}

        command = {
            "trajectory": request.trajectory,
            "mode": request.mode,
            "attempts": request.attempts,
            "candidate": request.candidate,
            "strategy_selection": request.strategy,
            "profile_path": str(profile.path),
            "profile_sha256": profile.sha256,
            "source_identity": dict(source_identity),
            "contact": contact,
            "early_stop": early_stop,
            "catalog_sha256": catalog["catalog_sha256"],
            "metric": dict(metric),
            "continuous_epoch_attempts": 100 if request.mode == "continuous" else None,
            "continuous_fresh_sobol_pool_per_epoch": request.mode == "continuous",
            "discovery_data_allowed_in_certificate": False,
        }
        fingerprint = sha256_value(command)
        return {
            "schema": "step5d.autotuner-r014/dispatch-plan-v1",
            "version": 1,
            "motion": False if request.dry_run else None,
            "command": command,
            "campaign_fingerprint": fingerprint,
            "state_machine": list(RUN_STATES),
            "home_before_arm": True,
            "normal_stop": "finish-or-censor-current-attempt_then_return-home",
            "blockers": sorted(set(blockers)),
            "ready_for_live": not blockers,
        }

    def _run_paths(self) -> list[Path]:
        if not self.runs_root.is_dir():
            return []
        return sorted(path for path in self.runs_root.iterdir() if (path / "state.json").is_file())

    def _load_run(self, run_id: str) -> tuple[Path, dict[str, Any]]:
        run_dir = self.runs_root / run_id
        if not run_dir.is_dir():
            raise R014Error(f"run does not exist: {run_id}")
        return run_dir, load_json(run_dir / "state.json")

    def _status(self, request: Request) -> dict[str, Any]:
        if request.run_id:
            run_dir, state = self._load_run(request.run_id)
            return {"ok": True, "run_dir": str(run_dir), "run": state}
        runs = [load_json(path / "state.json") for path in self._run_paths()]
        return {"ok": True, "runs": runs}

    def _stop(self, request: Request) -> dict[str, Any]:
        if not request.run_id:
            raise R014Error("stop requires --run RUN_ID")
        run_dir, state = self._load_run(request.run_id)
        if state.get("state") not in {"RUNNING", "PREFLIGHT_OK", "HOME_VERIFIED"}:
            raise R014Error(f"run is not stoppable from state {state.get('state')}")
        (run_dir / "stop.requested").write_text("graceful\n", encoding="utf-8")
        state["state"] = "STOP_REQUESTED"
        state["stop_policy"] = "finish-or-censor-current-attempt_then-return-home"
        atomic_write_json(run_dir / "state.json", state)
        return {"ok": True, "run_id": request.run_id, "state": "STOP_REQUESTED"}

    def _resume(self, request: Request) -> dict[str, Any]:
        candidates: list[tuple[Path, dict[str, Any]]] = []
        for path in self._run_paths():
            state = load_json(path / "state.json")
            if state.get("state") == "STOPPED_HOME":
                candidates.append((path, state))
        if request.run_id:
            candidates = [item for item in candidates if item[0].name == request.run_id]
        if len(candidates) != 1:
            raise R014Error(
                "resume requires exactly one Home-closed compatible run; candidates="
                + ",".join(path.name for path, _state in candidates)
            )
        path, state = candidates[0]
        profile = self.registry.current_qualified()
        catalog_sha = str(catalog_document()["catalog_sha256"])
        source_identity = profile.raw.get("host_source")
        metric = profile.raw.get("metric")
        if (
            state.get("profile_sha256") != profile.sha256
            or state.get("source_identity") != source_identity
            or state.get("catalog_sha256") != catalog_sha
            or state.get("metric") != metric
        ):
            raise R014Error("resume source/profile/catalog/metric identity is incompatible")
        return {
            "ok": True,
            "run_id": path.name,
            "resume_allowed": True,
            "required_first_state": "HOME_VERIFIED",
            "campaign_fingerprint": state.get("campaign_fingerprint"),
        }

    def _start(self, request: Request) -> dict[str, Any]:
        plan = self._plan(request)
        if request.dry_run:
            return {"ok": True, "dry_run": True, **plan}
        if plan["blockers"]:
            raise R014Error("live start refused: " + "; ".join(plan["blockers"]))
        # The R014 physical backend is deliberately admitted only through an
        # owner-generated execution contract.  Reaching this seam means profile
        # promotion succeeded, but does not let the dispatcher invent a writer.
        backend_contract = self.registry.root / "qualification/live-backend.json"
        if not backend_contract.is_file():
            raise R014Error("live backend contract is absent after profile qualification")
        backend = LiveBackendContract.load(
            backend_contract,
            experiment_root=self.experiment_root,
            plan=plan,
        )
        with WriterLock(self.writer_lock):
            run_id = request.run_id or datetime.now(timezone.utc).strftime("r014_%Y%m%d_%H%M%S")
            run_dir = self.runs_root / run_id
            if run_dir.exists():
                raise R014Error(f"run id already exists: {run_id}")
            state = {
                "schema": RUN_SCHEMA,
                "version": 1,
                "run_id": run_id,
                "state": "CREATED",
                "profile_path": plan["command"]["profile_path"],
                "profile_sha256": plan["command"]["profile_sha256"],
                "source_identity": plan["command"]["source_identity"],
                "catalog_sha256": plan["command"]["catalog_sha256"],
                "metric": plan["command"]["metric"],
                "campaign_fingerprint": plan["campaign_fingerprint"],
                "home_before_arm": True,
                "home_verified": False,
                "closed_home": False,
                "transitions": ["CREATED"],
            }
            atomic_write_json(run_dir / "state.json", state)
            plan_path = run_dir / "dispatch-plan.json"
            atomic_write_json(plan_path, plan)
            try:
                completed = subprocess.run(
                    backend.command(run_dir=run_dir, plan_path=plan_path),
                    cwd=self.experiment_root,
                    check=False,
                )
            except OSError as exc:
                raise R014Error(f"owner backend invocation failed: {exc}") from exc
            if completed.returncode != 0:
                raise R014Error(
                    f"owner backend exited unsuccessfully: {completed.returncode}; run={run_dir}"
                )
            final_state = load_json(run_dir / "state.json")
            _validate_closed_backend_state(
                final_state,
                run_id=run_id,
                plan=plan,
                stop_marker=run_dir / "stop.requested",
            )
        return {
            "ok": True,
            "run_id": run_id,
            "run_dir": str(run_dir),
            "state": "CLOSED",
            "closed_home": True,
        }

    def dispatch(self, request: Request) -> dict[str, Any]:
        if request.action is Action.STATUS:
            return self._status(request)
        if request.action is Action.STOP:
            return self._stop(request)
        if request.action is Action.RESUME:
            return self._resume(request)
        return self._start(request)


def experiment_root_from_file() -> Path:
    return Path(__file__).resolve().parents[2]


def main(argv: Sequence[str] | None = None) -> int:
    try:
        request = parse_request(argv)
        result = Dispatcher(experiment_root_from_file()).dispatch(request)
    except R014Error as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    if request.json_output:
        print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    return 0
