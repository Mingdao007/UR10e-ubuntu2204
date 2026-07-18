#!/usr/bin/env python3
"""Strict resolver for historical and current Step4e controller routes."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TABLE = ROOT / "config" / "step4e_stage_table.json"
SCHEMA = "step4e.stage-table-v1"
VERSION_RE = re.compile(r"^v([1-9]|[12][0-9]|3[01])$")
ALLOWED_LIFECYCLES = {
    "current",
    "previous",
    "fallback",
    "failed_archive",
    "historical",
}
TSV_FIELDS = (
    "version",
    "program_basename",
    "local_dir",
    "controller_dir",
    "controller_urp",
    "run_label",
    "lifecycle",
)


class RouteError(ValueError):
    """Raised when the route table or requested route fails closed."""


def _unique_object(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RouteError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _strict_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RouteError(f"route table must be a regular file: {path}")
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                RouteError(f"non-finite JSON constant: {value}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RouteError(f"route table is not strict JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RouteError("route table must be an object")
    return payload


def _selector_versions(selector: Any, *, minimum: int, maximum: int) -> set[int]:
    if not isinstance(selector, dict) or set(selector) != {"ranges", "versions"}:
        raise RouteError("selector fields differ")
    ranges = selector["ranges"]
    versions = selector["versions"]
    if not isinstance(ranges, list) or not isinstance(versions, list):
        raise RouteError("selector ranges and versions must be arrays")
    expanded: set[int] = set()
    for interval in ranges:
        if (
            not isinstance(interval, list)
            or len(interval) != 2
            or any(type(value) is not int for value in interval)
        ):
            raise RouteError("selector range must contain two integers")
        start, end = interval
        if start > end or start < minimum or end > maximum:
            raise RouteError(f"selector range is outside v{minimum}..v{maximum}")
        for value in range(start, end + 1):
            if value in expanded:
                raise RouteError(f"selector repeats v{value}")
            expanded.add(value)
    for value in versions:
        if type(value) is not int or value < minimum or value > maximum:
            raise RouteError(f"selector version is outside v{minimum}..v{maximum}")
        if value in expanded:
            raise RouteError(f"selector repeats v{value}")
        expanded.add(value)
    if not expanded:
        raise RouteError("selector cannot be empty")
    return expanded


def _safe_route_string(value: Any, *, field: str, absolute: bool) -> str:
    if not isinstance(value, str) or not value or any(char in value for char in "\t\r\n"):
        raise RouteError(f"{field} must be a non-empty single-line string")
    path = Path(value)
    if ".." in path.parts or path.is_absolute() is not absolute:
        raise RouteError(f"unsafe {field}: {value}")
    if absolute and not value.startswith("/programs/andyl/kunwei/step4"):
        raise RouteError(f"{field} is outside the Step4 controller root")
    return value.rstrip("/")


def load_routes(path: Path = DEFAULT_TABLE) -> tuple[dict[int, dict[str, str]], dict[str, Any]]:
    payload = _strict_json(path)
    expected_keys = {
        "schema_version",
        "current_version",
        "version_domain",
        "route_families",
        "lifecycle_rules",
        "current_contract",
        "evidence_policy",
    }
    if set(payload) != expected_keys or payload.get("schema_version") != SCHEMA:
        raise RouteError("route table top-level contract differs")
    domain = payload["version_domain"]
    if not isinstance(domain, dict) or set(domain) != {"minimum", "maximum"}:
        raise RouteError("version domain fields differ")
    minimum, maximum = domain["minimum"], domain["maximum"]
    if (minimum, maximum) != (1, 31):
        raise RouteError("version domain must be v1..v31")

    route_by_number: dict[int, dict[str, str]] = {}
    families = payload["route_families"]
    if not isinstance(families, list) or not families:
        raise RouteError("route families must be a non-empty array")
    for family in families:
        if not isinstance(family, dict) or set(family) != {
            "selector", "basename_template", "local_dir", "controller_dir"
        }:
            raise RouteError("route family fields differ")
        template = family["basename_template"]
        if not isinstance(template, str) or template.count("{version}") != 1:
            raise RouteError("basename template must contain one {version}")
        local_dir = _safe_route_string(family["local_dir"], field="local_dir", absolute=False)
        controller_dir = _safe_route_string(
            family["controller_dir"], field="controller_dir", absolute=True
        )
        for number in _selector_versions(family["selector"], minimum=minimum, maximum=maximum):
            if number in route_by_number:
                raise RouteError(f"route family overlaps at v{number}")
            version = f"v{number}"
            basename = template.format(version=version)
            if not re.fullmatch(r"step4e_[a-z0-9_]+_v[0-9]+", basename):
                raise RouteError(f"unsafe program basename: {basename}")
            route_by_number[number] = {
                "version": version,
                "program_basename": basename,
                "local_dir": local_dir,
                "controller_dir": controller_dir,
                "controller_urp": f"{controller_dir}/{basename}.urp",
                "run_label": basename,
            }
    expected_numbers = set(range(minimum, maximum + 1))
    if set(route_by_number) != expected_numbers:
        missing = sorted(expected_numbers - set(route_by_number))
        raise RouteError(f"route coverage differs; missing={missing}")

    lifecycle_by_number: dict[int, str] = {}
    rules = payload["lifecycle_rules"]
    if not isinstance(rules, list) or not rules:
        raise RouteError("lifecycle rules must be a non-empty array")
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != {"status", "selector"}:
            raise RouteError("lifecycle rule fields differ")
        status = rule["status"]
        if status not in ALLOWED_LIFECYCLES:
            raise RouteError(f"unknown lifecycle: {status}")
        for number in _selector_versions(rule["selector"], minimum=minimum, maximum=maximum):
            if number in lifecycle_by_number:
                raise RouteError(f"lifecycle overlaps at v{number}")
            lifecycle_by_number[number] = status
    if set(lifecycle_by_number) != expected_numbers:
        missing = sorted(expected_numbers - set(lifecycle_by_number))
        raise RouteError(f"lifecycle coverage differs; missing={missing}")
    if [number for number, status in lifecycle_by_number.items() if status == "current"] != [31]:
        raise RouteError("exactly v31 must be current")
    for number, route in route_by_number.items():
        route["lifecycle"] = lifecycle_by_number[number]

    current = route_by_number[31]
    contract = payload["current_contract"]
    if not isinstance(contract, dict):
        raise RouteError("current contract must be an object")
    for field in ("version", "program_basename", "local_dir", "controller_target"):
        expected = current["controller_urp"] if field == "controller_target" else current[field]
        if contract.get(field) != expected:
            raise RouteError(f"current contract {field} differs")
    if payload["current_version"] != "v31" or contract.get("bridge_profile") != "v31":
        raise RouteError("current v31 binding differs")
    evidence = payload["evidence_policy"]
    if not isinstance(evidence, dict) or evidence.get("embedded_in_route_table") is not False:
        raise RouteError("evidence must remain separate from route rules")
    return route_by_number, payload


def resolve(version: str, path: Path = DEFAULT_TABLE) -> dict[str, str]:
    match = VERSION_RE.fullmatch(version)
    if match is None:
        raise RouteError("Step4e version must be one of v1..v31")
    routes, _ = load_routes(path)
    return dict(routes[int(match.group(1))])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", type=Path, default=DEFAULT_TABLE)
    parser.add_argument("--version")
    parser.add_argument("--format", choices=("json", "tsv"), default="json")
    parser.add_argument("--validate-all", action="store_true")
    args = parser.parse_args(argv)
    try:
        table_path = args.table.absolute()
        routes, payload = load_routes(table_path)
        if args.validate_all:
            result: Any = {
                "schema_version": payload["schema_version"],
                "ok": True,
                "route_count": len(routes),
                "current_version": payload["current_version"],
            }
        elif args.version:
            match = VERSION_RE.fullmatch(args.version)
            if match is None:
                raise RouteError("Step4e version must be one of v1..v31")
            result = dict(routes[int(match.group(1))])
        else:
            raise RouteError("--version or --validate-all is required")
        if args.format == "tsv":
            if args.validate_all:
                raise RouteError("--format tsv requires --version")
            print("\t".join(result[field] for field in TSV_FIELDS))
        else:
            print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    except RouteError as exc:
        print(json.dumps({"ok": False, "blocker": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
