#!/usr/bin/env python3
"""Offline semantic equivalence checks for the Step5d profile wire contract."""

from __future__ import annotations

import ast
from dataclasses import dataclass
import gzip
import html
import math
import xml.etree.ElementTree as ET
from typing import Any, Iterable, Mapping


PROFILE_FUNCTION = "codex_autotune_network_profile_valid"
NETWORK_NORMAL_DIGITS = (1, 2, 3, 5, 6)
ACTUATOR_DIGITS = (1, 2, 3)
NETWORK_PROFILE_CODES = frozenset(
    100 * normal + 10 * host + tp
    for normal in NETWORK_NORMAL_DIGITS
    for host in ACTUATOR_DIGITS
    for tp in ACTUATOR_DIGITS
)
PROFILE_CODE_SPACE = tuple(range(1000)) + (1000, 2_147_483_647)


class ProfileSemanticContractError(ValueError):
    """The host profile lattice and final TP script semantics differ."""


@dataclass(frozen=True)
class ProfileSemanticReport:
    expected_codes: frozenset[int]
    tp_accepted_codes: frozenset[int]
    host_only_codes: tuple[int, ...]
    tp_only_codes: tuple[int, ...]

    @property
    def ok(self) -> bool:
        return not self.host_only_codes and not self.tp_only_codes


def _function_lines(script: str, function_name: str = PROFILE_FUNCTION) -> list[str]:
    header = f"def {function_name}(execution_profile_id):"
    lines = script.splitlines()
    matches = [index for index, line in enumerate(lines) if line == header]
    if len(matches) != 1:
        raise ProfileSemanticContractError(
            f"TP script must contain exactly one {function_name} definition"
        )
    body: list[str] = []
    for line in lines[matches[0] + 1 :]:
        if line == "end":
            break
        if not line.startswith("  "):
            raise ProfileSemanticContractError(
                f"unsupported TP profile-function statement: {line!r}"
            )
        body.append(line[2:])
    else:
        raise ProfileSemanticContractError("TP profile function has no closing end")
    if not body:
        raise ProfileSemanticContractError("TP profile function is empty")
    return body


def _evaluate(node: ast.AST, variables: Mapping[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _evaluate(node.body, variables)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (bool, int, float)):
            return node.value
        raise ProfileSemanticContractError("unsupported constant in TP profile predicate")
    if isinstance(node, ast.Name):
        if node.id not in variables:
            raise ProfileSemanticContractError(
                f"unknown TP profile-predicate variable: {node.id}"
            )
        return variables[node.id]
    if isinstance(node, ast.BinOp):
        left = _evaluate(node.left, variables)
        right = _evaluate(node.right, variables)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
        raise ProfileSemanticContractError("unsupported arithmetic in TP profile predicate")
    if isinstance(node, ast.UnaryOp):
        value = _evaluate(node.operand, variables)
        if isinstance(node.op, ast.Not):
            return not bool(value)
        if isinstance(node.op, ast.USub):
            return -value
        if isinstance(node.op, ast.UAdd):
            return +value
        raise ProfileSemanticContractError("unsupported unary operator in TP profile predicate")
    if isinstance(node, ast.BoolOp):
        values = [_evaluate(value, variables) for value in node.values]
        if isinstance(node.op, ast.And):
            return all(bool(value) for value in values)
        if isinstance(node.op, ast.Or):
            return any(bool(value) for value in values)
        raise ProfileSemanticContractError("unsupported boolean operator in TP profile predicate")
    if isinstance(node, ast.Compare):
        left = _evaluate(node.left, variables)
        for operator, comparator in zip(node.ops, node.comparators):
            right = _evaluate(comparator, variables)
            if isinstance(operator, ast.Eq):
                passed = left == right
            elif isinstance(operator, ast.NotEq):
                passed = left != right
            elif isinstance(operator, ast.Lt):
                passed = left < right
            elif isinstance(operator, ast.LtE):
                passed = left <= right
            elif isinstance(operator, ast.Gt):
                passed = left > right
            elif isinstance(operator, ast.GtE):
                passed = left >= right
            else:
                raise ProfileSemanticContractError(
                    "unsupported comparison in TP profile predicate"
                )
            if not passed:
                return False
            left = right
        return True
    if isinstance(node, ast.Call):
        if (
            not isinstance(node.func, ast.Name)
            or node.func.id != "floor"
            or len(node.args) != 1
            or node.keywords
        ):
            raise ProfileSemanticContractError(
                "only floor(x) is allowed in the TP profile predicate"
            )
        return math.floor(_evaluate(node.args[0], variables))
    raise ProfileSemanticContractError(
        f"unsupported AST node in TP profile predicate: {type(node).__name__}"
    )


def _expression(source: str) -> ast.Expression:
    try:
        parsed = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise ProfileSemanticContractError(
            f"invalid TP profile-predicate expression: {source!r}"
        ) from exc
    if not isinstance(parsed, ast.Expression):
        raise ProfileSemanticContractError("TP profile predicate is not an expression")
    return parsed


def tp_accepts_network_profile(script: str, execution_profile_id: int) -> bool:
    if isinstance(execution_profile_id, bool) or not isinstance(execution_profile_id, int):
        raise ProfileSemanticContractError("execution_profile_id must be an integer")
    variables: dict[str, Any] = {"execution_profile_id": execution_profile_id}
    returned: bool | None = None
    for statement in _function_lines(script):
        if statement.startswith("local "):
            assignment = statement[len("local ") :]
            if assignment.count("=") != 1:
                raise ProfileSemanticContractError(
                    f"unsupported TP profile assignment: {statement!r}"
                )
            name, source = (part.strip() for part in assignment.split("=", 1))
            if not name.isidentifier() or name in variables:
                raise ProfileSemanticContractError(
                    f"invalid or repeated TP profile variable: {name!r}"
                )
            variables[name] = _evaluate(_expression(source), variables)
        elif statement.startswith("return "):
            if returned is not None:
                raise ProfileSemanticContractError("TP profile function returns more than once")
            returned = bool(
                _evaluate(_expression(statement[len("return ") :]), variables)
            )
        else:
            raise ProfileSemanticContractError(
                f"unsupported TP profile-function statement: {statement!r}"
            )
    if returned is None:
        raise ProfileSemanticContractError("TP profile function has no return statement")
    return returned


def compare_network_profile_semantics(
    script: str,
    *,
    expected_codes: Iterable[int] = NETWORK_PROFILE_CODES,
    code_space: Iterable[int] = PROFILE_CODE_SPACE,
) -> ProfileSemanticReport:
    expected = frozenset(int(value) for value in expected_codes)
    accepted = frozenset(
        code for code in code_space if tp_accepts_network_profile(script, code)
    )
    return ProfileSemanticReport(
        expected_codes=expected,
        tp_accepted_codes=accepted,
        host_only_codes=tuple(sorted(expected - accepted)),
        tp_only_codes=tuple(sorted(accepted - expected)),
    )


def assert_network_profile_semantics(
    script: str,
    *,
    expected_codes: Iterable[int] = NETWORK_PROFILE_CODES,
) -> ProfileSemanticReport:
    report = compare_network_profile_semantics(script, expected_codes=expected_codes)
    if not report.ok:
        raise ProfileSemanticContractError(
            "Step5d host/TP network-profile semantics differ: "
            f"host_only={list(report.host_only_codes)}, "
            f"tp_only={list(report.tp_only_codes)}"
        )
    return report


def cached_script_from_urp(urp: bytes) -> str:
    try:
        root = ET.fromstring(gzip.decompress(urp).decode("utf-8"))
    except (OSError, UnicodeError, ET.ParseError) as exc:
        raise ProfileSemanticContractError("TP .urp is not valid gzip/XML") from exc
    cached = [html.unescape(node.text or "") for node in root.iter() if node.tag == "cachedContents"]
    if len(cached) != 1 or not cached[0]:
        raise ProfileSemanticContractError("TP .urp must contain one non-empty cachedContents")
    return cached[0]


def assert_triplet_network_profile_semantics(
    script: str,
    urp: bytes,
) -> dict[str, ProfileSemanticReport]:
    cached = cached_script_from_urp(urp)
    if cached != script:
        raise ProfileSemanticContractError("TP .script and .urp cachedContents differ")
    return {
        "script": assert_network_profile_semantics(script),
        "urp_cached_contents": assert_network_profile_semantics(cached),
    }


__all__ = [
    "ACTUATOR_DIGITS",
    "NETWORK_NORMAL_DIGITS",
    "NETWORK_PROFILE_CODES",
    "PROFILE_CODE_SPACE",
    "ProfileSemanticContractError",
    "ProfileSemanticReport",
    "assert_network_profile_semantics",
    "assert_triplet_network_profile_semantics",
    "cached_script_from_urp",
    "compare_network_profile_semantics",
    "tp_accepts_network_profile",
]
