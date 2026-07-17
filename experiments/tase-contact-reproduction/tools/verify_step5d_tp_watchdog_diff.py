#!/usr/bin/env python3
"""Fail closed until TP v2 is proven to preserve the fetched controller triplet."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import posixpath
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any


EXTENSIONS = (".script", ".txt", ".urp")
ROOT = Path(__file__).resolve().parents[1]
WATCHDOG_MANIFEST = ROOT / "config/step5/tp_watchdog_v2.json"
EXPECTED_BLOCK_REQUIREMENTS = {
    "host_heartbeat_fail_closed_v2": frozenset({".script", ".urp"}),
}
BEGIN = b"AUTOTUNE_WATCHDOG_V2_BEGIN"
END = b"AUTOTUNE_WATCHDOG_V2_END"
BEGIN_RE = re.compile(
    rb"^[ \t]*# AUTOTUNE_WATCHDOG_V2_BEGIN "
    rb"(?P<block_id>[a-z][a-z0-9_]*)(?:\n)$"
)
END_RE = re.compile(
    rb"^[ \t]*# AUTOTUNE_WATCHDOG_V2_END "
    rb"(?P<block_id>[a-z][a-z0-9_]*)(?:\n)$"
)


@dataclass(frozen=True)
class WatchdogBlock:
    block_id: str
    payload: bytes
    start_offset: int
    end_offset: int


@dataclass(frozen=True)
class ContentInspection:
    normalized: bytes
    blocks: tuple[WatchdogBlock, ...]
    executable_script: bytes | None = None


@dataclass(frozen=True)
class CanonicalBlock:
    block_id: str
    source: Path
    payload: bytes
    sha256: str
    required_in: frozenset[str]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _decoded_bytes(path: Path) -> bytes:
    return gzip.decompress(path.read_bytes()) if path.suffix == ".urp" else path.read_bytes()


def _extract_watchdog_blocks(
    raw: bytes, *, source: Path
) -> tuple[bytes, tuple[WatchdogBlock, ...]]:
    output: list[bytes] = []
    blocks: list[WatchdogBlock] = []
    active_id: str | None = None
    active_lines: list[bytes] = []
    active_start: int | None = None
    offset = 0
    for line in raw.splitlines(keepends=True):
        line_start = offset
        offset += len(line)
        begin_match = BEGIN_RE.fullmatch(line)
        end_match = END_RE.fullmatch(line)
        if BEGIN in line:
            if begin_match is None:
                raise ValueError(f"malformed watchdog begin marker in {source}")
            if active_id is not None:
                raise ValueError(f"nested watchdog marker in {source}")
            active_id = begin_match.group("block_id").decode("ascii")
            active_lines = [line]
            active_start = line_start
            continue
        if END in line:
            if end_match is None:
                raise ValueError(f"malformed watchdog end marker in {source}")
            if active_id is None:
                raise ValueError(f"orphan watchdog end marker in {source}")
            end_id = end_match.group("block_id").decode("ascii")
            if end_id != active_id:
                raise ValueError(
                    f"watchdog marker id mismatch in {source}: {active_id} != {end_id}"
                )
            active_lines.append(line)
            assert active_start is not None
            blocks.append(
                WatchdogBlock(
                    active_id,
                    b"".join(active_lines),
                    active_start,
                    offset,
                )
            )
            active_id = None
            active_lines = []
            active_start = None
            continue
        if active_id is None:
            output.append(line)
        else:
            active_lines.append(line)
    if active_id is not None:
        raise ValueError(f"unterminated watchdog marker in {source}")
    return b"".join(output), tuple(blocks)


def _urscript_scope_stack(prefix: str) -> tuple[tuple[str, str | None], ...]:
    stack: list[tuple[str, str | None]] = []
    definition = re.compile(
        r"^def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(.*\):$"
    )
    nested = re.compile(r"^(?:if|while|for|thread|loop|switch)\b.*:$")
    for raw_line in prefix.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = definition.fullmatch(line)
        if match is not None:
            stack.append(("def", match.group(1)))
        elif nested.fullmatch(line) is not None:
            stack.append(("scope", None))
        elif line == "end" and stack:
            stack.pop()
    return tuple(stack)


def _enclosing_urscript_function(
    stack: tuple[tuple[str, str | None], ...],
) -> str | None:
    for kind, name in reversed(stack):
        if kind == "def":
            return name
    return None


def _main_function_names(program: str) -> tuple[str, str]:
    return program, f"codex_{program}"


def _has_top_level_terminator(prefix: str, *, function_name: str) -> bool:
    """A block after an unconditional main-scope halt/return is dead code."""

    stack: list[tuple[str, str | None]] = []
    definition = re.compile(r"^def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(.*\):$")
    nested = re.compile(r"^(?:if|while|for|thread|loop|switch)\b.*:$")
    for raw_line in prefix.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = definition.fullmatch(line)
        if match is not None:
            stack.append(("def", match.group(1)))
            continue
        if stack == [("def", function_name)] and (
            line == "halt" or re.fullmatch(r"return(?:\s+.*)?", line) is not None
        ):
            return True
        if nested.fullmatch(line) is not None:
            stack.append(("scope", None))
        elif line == "end" and stack:
            stack.pop()
    return False


def _validate_executable_context(
    raw: bytes,
    block: WatchdogBlock,
    *,
    source: Path,
    candidate_program: str,
    decoded_urp_script: bool = False,
) -> None:
    prefix = raw[: block.start_offset]
    if source.suffix == ".urp" and not decoded_urp_script:
        opening = prefix.rfind(b"<cachedContents>")
        closing = raw.find(b"</cachedContents>", block.end_offset)
        if opening < 0 or closing < 0:
            raise ValueError(
                f"watchdog block is outside URP cachedContents: {source}"
            )
        prefix = prefix[opening + len(b"<cachedContents>") :]
        prefix = html.unescape(prefix.decode("utf-8")).encode("utf-8")
    try:
        scope_stack = _urscript_scope_stack(prefix.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"watchdog executable context is not UTF-8: {source}"
        ) from exc
    function_name = _enclosing_urscript_function(scope_stack)
    if function_name not in _main_function_names(candidate_program):
        raise ValueError(
            "watchdog block is not inside the exact candidate program function: "
            f"{source}"
        )
    if scope_stack != (("def", function_name),):
        raise ValueError(
            "watchdog block is inside a nested or conditionally unreachable scope: "
            f"{source}"
        )
    if _has_top_level_terminator(
        prefix.decode("utf-8"), function_name=function_name
    ):
        raise ValueError(
            "watchdog block is dead after a top-level halt/return: "
            f"{source}"
        )


def _source_program_for_path(
    path: Path, *, current_program: str, candidate_program: str
) -> str:
    if path.stem == candidate_program:
        return candidate_program
    if path.stem == current_program:
        return current_program
    raise ValueError(f"triplet filename does not bind an exact program identity: {path}")


def _replace_spans(raw: bytes, replacements: list[tuple[int, int, bytes]]) -> bytes:
    normalized = raw
    for start, end, replacement in sorted(replacements, reverse=True):
        normalized = normalized[:start] + replacement + normalized[end:]
    return normalized


def _normalize_source_stamp(
    raw: bytes,
    *,
    actual_stamp: str,
    normalized_stamp: str,
    source: Path,
) -> bytes:
    pattern = re.compile(
        rb"(?m)^# VERSION: (?P<stamp>[^\r\n]+)(?:\r?$)"
    )
    matches = list(pattern.finditer(raw))
    matching = [
        match
        for match in matches
        if match.group("stamp") == actual_stamp.encode("ascii")
    ]
    if len(matching) != 1:
        raise ValueError(
            f"exact source stamp identity count is {len(matching)}, expected 1: {source}"
        )
    match = matching[0]
    return _replace_spans(
        raw,
        [(*match.span("stamp"), normalized_stamp.encode("ascii"))],
    )


def _normalize_literal_stamp(
    raw: bytes,
    *,
    actual_stamp: str,
    normalized_stamp: str,
    source: Path,
) -> bytes:
    actual = actual_stamp.encode("ascii")
    if raw.count(actual) != 1:
        raise ValueError(
            f"exact source stamp identity count is {raw.count(actual)}, expected 1: {source}"
        )
    return raw.replace(actual, normalized_stamp.encode("ascii"), 1)


def _normalize_urscript_identity(
    raw: bytes, *, source_program: str, current_program: str, source: Path
) -> bytes:
    stage_pattern = re.compile(
        rb"(?m)^# STEP5_STAGE_ID: (?P<program>[^\r\n]+)(?:\r?$)"
    )
    stage_rows = list(stage_pattern.finditer(raw))
    if stage_rows:
        if (
            len(stage_rows) != 1
            or stage_rows[0].group("program")
            != source_program.encode("ascii")
        ):
            raise ValueError(f"stage program identity is missing or ambiguous: {source}")
        raw = _replace_spans(
            raw,
            [
                (
                    *stage_rows[0].span("program"),
                    current_program.encode("ascii"),
                )
            ],
        )
    aliases = _main_function_names(source_program)
    definition = re.compile(
        rb"(?m)^def[ \t]+(?P<name>"
        + b"|".join(re.escape(name.encode("ascii")) for name in aliases)
        + rb")[ \t]*\([ \t]*\)[ \t]*:[ \t]*(?:\n|$)"
    )
    definitions = list(definition.finditer(raw))
    if len(definitions) != 1:
        raise ValueError(
            f"exact main function definition count is {len(definitions)}, expected 1: {source}"
        )
    function_name = definitions[0].group("name").decode("ascii")
    invocation = re.compile(
        rb"(?m)^(?P<name>" + re.escape(function_name.encode("ascii")) + rb")"
        rb"[ \t]*\([ \t]*\)[ \t]*(?:\n|$)"
    )
    invocations = list(invocation.finditer(raw))
    if len(invocations) != 1:
        raise ValueError(
            f"exact main function invocation count is {len(invocations)}, expected 1: {source}"
        )
    replacement_name = (
        f"codex_{current_program}"
        if function_name.startswith("codex_")
        else current_program
    ).encode("ascii")
    return _replace_spans(
        raw,
        [
            (*definitions[0].span("name"), replacement_name),
            (*invocations[0].span("name"), replacement_name),
        ],
    )


def _normalize_txt_identity(
    raw: bytes, *, source_program: str, current_program: str, source: Path
) -> bytes:
    pattern = re.compile(
        rb"(?m)^(?P<prefix>[ \t]*/programs/[^\r\n]*/)"
        + re.escape(source_program.encode("ascii"))
        + rb"(?P<suffix>\.urp[ \t]*)(?:\r?\n|$)"
    )
    matches = list(pattern.finditer(raw))
    if len(matches) != 1:
        raise ValueError(
            f"exact TP documentation program path count is {len(matches)}, expected 1: {source}"
        )
    match = matches[0]
    old_identity_start = match.start("suffix") - len(source_program)
    return _replace_spans(
        raw,
        [
            (
                old_identity_start,
                match.start("suffix"),
                current_program.encode("ascii"),
            )
        ],
    )


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def _normalized_root_start_tag(
    raw: bytes,
    *,
    root: ET.Element,
    source_program: str,
    current_program: str,
    source: Path,
) -> tuple[bytes, bytes]:
    if _xml_local_name(root.tag) not in {"Program", "URProgram"}:
        raise ValueError(f"URP root is not Program/URProgram: {source}")
    if root.get("name") != source_program:
        raise ValueError(f"URP root program identity mismatch: {source}")
    if root.get("crcValue") is None:
        raise ValueError(f"URP root lacks crcValue: {source}")
    tag_name = root.tag.encode("utf-8")
    starts = list(re.finditer(rb"<" + re.escape(tag_name) + rb"\b[^>]*>", raw))
    if len(starts) != 1:
        raise ValueError(f"URP root start tag is missing or ambiguous: {source}")
    old = starts[0].group(0)

    def replace_attribute(tag: bytes, name: bytes, expected: bytes, replacement: bytes) -> bytes:
        attr = re.compile(
            rb"(?P<prefix>\s" + name + rb"\s*=\s*)(?P<quote>[\"'])(?P<value>[^\"']*)(?P=quote)"
        )
        matches = list(attr.finditer(tag))
        if len(matches) != 1 or matches[0].group("value") != expected:
            raise ValueError(
                f"URP root {name.decode()} attribute is missing, duplicated, or mismatched: {source}"
            )
        return _replace_spans(
            tag,
            [(*matches[0].span("value"), replacement)],
        )

    new = replace_attribute(
        old,
        b"name",
        source_program.encode("ascii"),
        current_program.encode("ascii"),
    )
    crc_match = re.search(rb"\scrcValue\s*=\s*([\"'])(?P<value>[^\"']*)\1", new)
    assert crc_match is not None
    new = _replace_spans(new, [(*crc_match.span("value"), b"NORMALIZED")])
    return old, new


def _inspect_urp(
    path: Path,
    raw: bytes,
    *,
    current_program: str,
    candidate_program: str,
    require_executable_context: bool,
    source_stamp: str | None,
    candidate_stamp: str | None,
    controller_directory: str | None,
) -> ContentInspection:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError(f"decompressed URP is not well-formed XML: {path}: {exc}") from exc
    source_program = _source_program_for_path(
        path,
        current_program=current_program,
        candidate_program=candidate_program,
    )
    if controller_directory is not None:
        if root.get("directory") != controller_directory:
            raise ValueError(f"URP controller directory mismatch: {path}")
        expected_install = posixpath.relpath(
            "/programs/default", controller_directory
        )
        if root.get("installationRelativePath") != expected_install:
            raise ValueError(f"URP installationRelativePath mismatch: {path}")
    cached_nodes = [
        node for node in root.iter() if _xml_local_name(node.tag) == "cachedContents"
    ]
    if not cached_nodes or any(list(node) for node in cached_nodes):
        raise ValueError(f"URP cachedContents is missing or contains child XML: {path}")
    semantic_marker_counts = {
        BEGIN: sum((node.text or "").encode("utf-8").count(BEGIN) for node in cached_nodes),
        END: sum((node.text or "").encode("utf-8").count(END) for node in cached_nodes),
    }
    for marker, expected_count in semantic_marker_counts.items():
        if raw.count(marker) != expected_count:
            raise ValueError(
                f"watchdog marker occurs outside actual cachedContents text: {path}"
            )

    main_nodes: list[tuple[ET.Element, bytes, bytes, tuple[WatchdogBlock, ...]]] = []
    for node in cached_nodes:
        script = (node.text or "").encode("utf-8")
        outside, blocks = _extract_watchdog_blocks(script, source=path)
        try:
            if source_stamp is not None or candidate_stamp is not None:
                if source_stamp is None or candidate_stamp is None:
                    raise ValueError("source/candidate stamp normalization must be paired")
                outside = _normalize_source_stamp(
                    outside,
                    actual_stamp=(
                        source_stamp
                        if source_program == current_program
                        else candidate_stamp
                    ),
                    normalized_stamp=source_stamp,
                    source=path,
                )
            normalized_script = _normalize_urscript_identity(
                outside,
                source_program=source_program,
                current_program=current_program,
                source=path,
            )
        except ValueError:
            continue
        main_nodes.append((node, script, normalized_script, blocks))
    if len(main_nodes) != 1:
        raise ValueError(
            f"URP executable cachedContents main program count is {len(main_nodes)}, expected 1: {path}"
        )
    _, script, normalized_script, semantic_blocks = main_nodes[0]
    if sum(semantic_marker_counts.values()) != sum(
        script.count(marker) for marker in (BEGIN, END)
    ):
        raise ValueError(f"watchdog marker appears outside the exact main cachedContents: {path}")
    if require_executable_context:
        for block in semantic_blocks:
            _validate_executable_context(
                script,
                block,
                source=path,
                candidate_program=candidate_program,
                decoded_urp_script=True,
            )

    escaped_script = html.escape(script.decode("utf-8"), quote=True).encode("utf-8")
    if raw.count(escaped_script) != 1:
        raise ValueError(f"URP cachedContents byte representation is not exact/unique: {path}")
    escaped_normalized = html.escape(
        normalized_script.decode("utf-8"), quote=True
    ).encode("utf-8")
    root_old, root_new = _normalized_root_start_tag(
        raw,
        root=root,
        source_program=source_program,
        current_program=current_program,
        source=path,
    )
    normalized = raw.replace(root_old, root_new, 1).replace(
        escaped_script, escaped_normalized, 1
    )

    file_nodes = [node for node in root.iter() if _xml_local_name(node.tag) == "file"]
    file_suffix = f"/{source_program}.script"
    if controller_directory is None:
        matching_files = [
            node for node in file_nodes if (node.text or "").endswith(file_suffix)
        ]
        if len(matching_files) > 1:
            raise ValueError(f"URP program file identity is ambiguous: {path}")
    else:
        expected_file = f"{controller_directory}/{source_program}.script"
        matching_files = [
            node
            for node in file_nodes
            if node.attrib.get("resolves-to") == "file"
            and (node.text or "") == expected_file
        ]
        if len(matching_files) != 1:
            raise ValueError(f"URP Script node path/resolves-to mismatch: {path}")
        other_resolved = [
            node
            for node in file_nodes
            if node.attrib.get("resolves-to") == "file" and node not in matching_files
        ]
        if other_resolved:
            raise ValueError(f"URP contains an ambiguous extra resolved file node: {path}")
    if matching_files:
        old_text = matching_files[0].text or ""
        new_text = old_text[: -len(file_suffix)] + f"/{current_program}.script"
        old_encoded = html.escape(old_text, quote=True).encode("utf-8")
        if normalized.count(old_encoded) != 1:
            raise ValueError(f"URP program file text is not exact/unique: {path}")
        normalized = normalized.replace(
            old_encoded, html.escape(new_text, quote=True).encode("utf-8"), 1
        )

    escaped_blocks: list[WatchdogBlock] = []
    raw_script_start = raw.index(escaped_script)
    for block in semantic_blocks:
        payload = html.escape(block.payload.decode("utf-8"), quote=True).encode("utf-8")
        if escaped_script.count(payload) != 1:
            raise ValueError(f"URP watchdog block bytes are not exact/unique: {path}")
        relative_start = escaped_script.index(payload)
        escaped_blocks.append(
            WatchdogBlock(
                block.block_id,
                payload,
                raw_script_start + relative_start,
                raw_script_start + relative_start + len(payload),
            )
        )
    return ContentInspection(
        normalized=normalized,
        blocks=tuple(escaped_blocks),
        executable_script=script,
    )


def _validate_canonical_source(block: WatchdogBlock, *, source: Path) -> None:
    code_lines = tuple(
        line.strip()
        for line in block.payload.splitlines()
        if line.strip() and not line.lstrip().startswith(b"#")
    )
    if not code_lines:
        raise ValueError(f"canonical watchdog block is comment-only: {source}")
    code = b"\n".join(code_lines)
    required_code = (
        b"local watchdog_home_pose = get_actual_tcp_pose()",
        b"local watchdog_home_q = get_actual_joint_positions()",
        b"thread codex_autotune_v2_heartbeat_watchdog():",
        b"local watchdog_last_heartbeat = read_input_float_register(26)",
        b"local watchdog_heartbeat = read_input_float_register(26)",
        b"local watchdog_state = read_output_integer_register(26)",
        b"local watchdog_timeout_s = 0.100",
        b"if watchdog_state == 70 and watchdog_measured_home_verified:",
        b"watchdog_timeout_s = 2.000",
        b"local watchdog_measured_home_verified = watchdog_position_error_m <= 0.003 and watchdog_orientation_error_rad <= 0.050 and watchdog_joint_error_rad <= 0.010",
        b"if watchdog_stale_s > watchdog_timeout_s:",
        b'textmsg("host_heartbeat_timeout_at_verified_home")',
        b"stopj(0.500)",
        b'textmsg("host_heartbeat_timeout_unknown_home_controlled_stop")',
        b"local watchdog_thread_handle = run codex_autotune_v2_heartbeat_watchdog()",
    )
    missing = [line.decode("ascii") for line in required_code if line not in code_lines]
    if missing:
        raise ValueError(
            f"canonical watchdog block lacks executable policy {missing}: {source}"
        )
    if code_lines.count(b"halt") != 2:
        raise ValueError(f"canonical watchdog requires exactly two terminal halts: {source}")
    if code.count(b"read_input_float_register(26)") != 2:
        raise ValueError(f"canonical watchdog heartbeat register binding drift: {source}")
    if code.count(b"stopj(0.500)") != 1:
        raise ValueError(f"canonical watchdog controlled-stop binding drift: {source}")
    decoded = code.decode("utf-8")
    identifiers_without_strings = set(
        re.findall(
            r"\b[A-Za-z_][A-Za-z0-9_]*\b",
            re.sub(r'"[^"\\]*(?:\\.[^"\\]*)*"', "", decoded),
        )
    )
    undefined_placeholders = {
        "host_heartbeat_timeout",
        "measured_home_verified",
    }
    found_placeholders = [
        token for token in sorted(undefined_placeholders) if token in identifiers_without_strings
    ]
    if found_placeholders:
        raise ValueError(
            f"canonical watchdog contains undefined placeholder symbols "
            f"{found_placeholders}: {source}"
        )
    identifiers = set(
        re.findall(
            r"\b(?:watchdog_[A-Za-z0-9_]+|codex_autotune_v2_heartbeat_watchdog)\b",
            decoded,
        )
    )
    declarations = set(
        re.findall(
            r"(?:^|\n)(?:local\s+)?(watchdog_[A-Za-z0-9_]+)\s*=",
            decoded,
        )
    )
    declarations.update(
        re.findall(
            r"(?:^|\n)thread\s+(codex_autotune_v2_heartbeat_watchdog)\s*\(\s*\)\s*:",
            decoded,
        )
    )
    undefined = sorted(identifiers - declarations)
    if undefined:
        raise ValueError(
            f"canonical watchdog contains undefined bounded identifiers {undefined}: {source}"
        )
    forbidden = (
        b"movej(",
        b"movel(",
        b"movec(",
        b"movep(",
        b"speedj(",
        b"speedl(",
        b"servoj(",
        b"servoc(",
        b"force_mode(",
        b"freedrive_mode(",
        b"set_payload(",
        b"set_target_payload(",
        b"set_tcp(",
        b"zero_ftsensor(",
        b"stopl(",
        b"codex_should_auto_home",
        b"campaign_home_pose",
        b"retract_pose",
        b"force_error",
        b"jacobian",
        b"qdot",
        b"wrench",
    )
    lowered = block.payload.lower()
    found = [token.decode("ascii") for token in forbidden if token in lowered]
    if found:
        raise ValueError(
            f"canonical watchdog block contains motion/control code {found}: {source}"
        )


def load_canonical_blocks(path: Path = WATCHDOG_MANIFEST) -> dict[str, CanonicalBlock]:
    manifest: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema") != "step5d.autotune.tp-watchdog/v2":
        raise ValueError("unknown TP watchdog policy manifest schema")
    heartbeat = manifest.get("heartbeat")
    restart_home = manifest.get("restart_home_gate")
    wait_ack = manifest.get("wait_ack")
    if (
        not isinstance(heartbeat, dict)
        or heartbeat.get("source_float_register") != 26
        or heartbeat.get("run_timeout_s") != 0.1
        or heartbeat.get("wait_ack_timeout_s") != 2.0
        or manifest.get("auto_home_after_heartbeat_loss") is not False
        or not isinstance(restart_home, dict)
        or restart_home.get("position_error_max_m") != 0.003
        or restart_home.get("orientation_error_max_rad") != 0.05
        or restart_home.get("joint_error_max_rad") != 0.01
        or restart_home.get("mismatch_action") != "refuse_start"
        or not isinstance(wait_ack, dict)
        or wait_ack.get("verified_home_timeout_action")
        != "publish_terminal_and_halt"
        or wait_ack.get("unknown_home_timeout_action")
        != "controlled_stop_and_halt"
    ):
        raise ValueError("TP watchdog runtime policy drift")
    gate = manifest.get("watchdog_diff_gate")
    if not isinstance(gate, dict) or gate.get("schema") != "step5d.autotune.tp-watchdog-blocks/v2":
        raise ValueError("unknown TP watchdog canonical-block manifest schema")
    if gate.get("outside_block_normalization") != [
        "program_identity",
        "source_stamp_identity",
        "urp_crcValue",
    ]:
        raise ValueError("TP watchdog outside-block normalization policy drift")
    rows = gate.get("required_blocks")
    if not isinstance(rows, list) or not rows:
        raise ValueError("TP watchdog manifest has no required canonical blocks")
    canonical: dict[str, CanonicalBlock] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("TP watchdog canonical-block row must be an object")
        block_id = row.get("block_id")
        relative_source = row.get("source")
        expected_sha = row.get("sha256")
        required_in = row.get("required_in")
        if not isinstance(block_id, str) or not isinstance(relative_source, str):
            raise ValueError("TP watchdog canonical block lacks stable id/source")
        if block_id in canonical:
            raise ValueError(f"duplicate canonical watchdog block id: {block_id}")
        if not isinstance(expected_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
            raise ValueError(f"invalid canonical watchdog SHA: {block_id}")
        if not isinstance(required_in, list) or not required_in:
            raise ValueError(f"canonical watchdog block lacks required_in: {block_id}")
        extension_set = frozenset(required_in)
        if not extension_set <= frozenset(EXTENSIONS):
            raise ValueError(f"canonical watchdog block has unknown extension: {block_id}")
        if EXPECTED_BLOCK_REQUIREMENTS.get(block_id) != extension_set:
            raise ValueError(f"canonical watchdog block placement policy drift: {block_id}")
        source = (ROOT / relative_source).resolve()
        try:
            source.relative_to(ROOT)
        except ValueError as exc:
            raise ValueError(f"canonical watchdog source escapes repository: {block_id}") from exc
        payload = source.read_bytes()
        actual_sha = hashlib.sha256(payload).hexdigest()
        if actual_sha != expected_sha:
            raise ValueError(
                f"canonical watchdog source SHA drift: {block_id}: "
                f"expected {expected_sha}, got {actual_sha}"
            )
        outside, parsed = _extract_watchdog_blocks(payload, source=source)
        if outside or len(parsed) != 1 or parsed[0].block_id != block_id:
            raise ValueError(
                "canonical watchdog source must contain exactly its named block: "
                f"{block_id}"
            )
        _validate_canonical_source(parsed[0], source=source)
        canonical[block_id] = CanonicalBlock(
            block_id=block_id,
            source=source,
            payload=payload,
            sha256=expected_sha,
            required_in=extension_set,
        )
    if set(canonical) != set(EXPECTED_BLOCK_REQUIREMENTS):
        raise ValueError("TP watchdog stable block-id set drift")
    return canonical


def inspect_content(
    path: Path,
    *,
    current_program: str,
    candidate_program: str,
    require_executable_context: bool = True,
    source_stamp: str | None = None,
    candidate_stamp: str | None = None,
    controller_directory: str | None = None,
) -> ContentInspection:
    raw = _decoded_bytes(path)
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"triplet member is not UTF-8 after decoding: {path}") from exc
    if path.suffix == ".urp":
        return _inspect_urp(
            path,
            raw,
            current_program=current_program,
            candidate_program=candidate_program,
            require_executable_context=require_executable_context,
            source_stamp=source_stamp,
            candidate_stamp=candidate_stamp,
            controller_directory=controller_directory,
        )
    source_program = _source_program_for_path(
        path,
        current_program=current_program,
        candidate_program=candidate_program,
    )
    outside, blocks = _extract_watchdog_blocks(raw, source=path)
    if require_executable_context:
        for block in blocks:
            _validate_executable_context(
                raw,
                block,
                source=path,
                candidate_program=candidate_program,
            )
    if path.suffix == ".script":
        if source_stamp is not None or candidate_stamp is not None:
            if source_stamp is None or candidate_stamp is None:
                raise ValueError("source/candidate stamp normalization must be paired")
            outside = _normalize_source_stamp(
                outside,
                actual_stamp=(
                    source_stamp
                    if source_program == current_program
                    else candidate_stamp
                ),
                normalized_stamp=source_stamp,
                source=path,
            )
        normalized = _normalize_urscript_identity(
            outside,
            source_program=source_program,
            current_program=current_program,
            source=path,
        )
    elif path.suffix == ".txt":
        if blocks:
            raise ValueError(f"watchdog block is forbidden in TP documentation: {path}")
        if source_stamp is not None or candidate_stamp is not None:
            if source_stamp is None or candidate_stamp is None:
                raise ValueError("source/candidate stamp normalization must be paired")
            outside = _normalize_literal_stamp(
                outside,
                actual_stamp=(
                    source_stamp
                    if source_program == current_program
                    else candidate_stamp
                ),
                normalized_stamp=source_stamp,
                source=path,
            )
        normalized = _normalize_txt_identity(
            outside,
            source_program=source_program,
            current_program=current_program,
            source=path,
        )
    else:
        raise ValueError(f"unsupported TP triplet extension: {path}")
    return ContentInspection(normalized=normalized, blocks=blocks)


def normalized_content(
    path: Path, *, current_program: str, candidate_program: str
) -> tuple[bytes, int]:
    """Compatibility helper for callers that only need outside-block content/count."""

    inspected = inspect_content(
        path,
        current_program=current_program,
        candidate_program=candidate_program,
    )
    return inspected.normalized, len(inspected.blocks)


def _validated_block_hashes(
    inspected: ContentInspection,
    *,
    extension: str,
    canonical: dict[str, CanonicalBlock],
) -> dict[str, str]:
    by_id: dict[str, list[WatchdogBlock]] = {}
    for block in inspected.blocks:
        by_id.setdefault(block.block_id, []).append(block)
    unexpected = sorted(set(by_id) - set(canonical))
    if unexpected:
        raise ValueError(f"candidate contains unknown watchdog blocks: {unexpected}")
    hashes: dict[str, str] = {}
    for block_id, expected in canonical.items():
        occurrences = by_id.get(block_id, [])
        required = extension in expected.required_in
        expected_count = 1 if required else 0
        if len(occurrences) != expected_count:
            raise ValueError(
                f"watchdog block {block_id} count for {extension}: "
                f"expected {expected_count}, got {len(occurrences)}"
            )
        if not required:
            continue
        actual = occurrences[0].payload
        expected_payload = expected.payload
        if extension == ".urp":
            expected_payload = html.escape(
                expected.payload.decode("utf-8"), quote=True
            ).encode("utf-8")
        actual_sha = hashlib.sha256(actual).hexdigest()
        expected_sha = hashlib.sha256(expected_payload).hexdigest()
        if actual != expected_payload or actual_sha != expected_sha:
            raise ValueError(
                f"watchdog block bytes/hash differ from reviewed canonical source: "
                f"{extension}:{block_id}"
            )
        hashes[block_id] = actual_sha
    return hashes


ATTESTATION_KEYS = {
    "schema",
    "source_class",
    "promotable",
    "blocked_reason",
    "source_receipt",
    "source_program",
    "candidate_program",
    "controller_directory",
    "source_stamp",
    "candidate_stamp",
    "allowed_changes",
    "control_math_changed",
    "trajectory_changed",
    "waypoint_changed",
    "command_order_changed",
    "fetched_controller_sha256",
    "candidate_sha256",
    "normalized_content_sha256",
    "watchdog_block_count",
    "watchdog_block_sha256",
    "canonical_watchdog",
    "urp_crcValue",
    "deploy_manifest",
}
SOURCE_RECEIPT_KEYS = {
    "schema",
    "path",
    "sha256",
    "host",
    "controller_directory",
    "basename",
    "output_dir",
    "captured_at",
    "source_class",
}
ALLOWED_CHANGES = [
    "program_identity",
    "source_stamp_identity",
    "urp_crcValue",
    "canonical_watchdog_block",
]


def _strict_object(value: object, keys: set[str], *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{label} contains missing or unsupported fields")
    return value


def _root_crc(path: Path) -> str:
    raw = _decoded_bytes(path)
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError(f"decompressed URP is not well-formed XML: {path}: {exc}") from exc
    value = root.get("crcValue")
    if value is None:
        raise ValueError(f"URP root lacks crcValue: {path}")
    return value


def _validate_bound_deploy_manifest(
    *,
    attested: dict[str, Any],
    source_class: str,
    candidate: Path,
    program: str,
    controller_directory: str,
    candidate_sha: dict[str, str],
) -> None:
    _strict_object(
        attested,
        {"path", "sha256", "schema", "promotable"},
        label="deploy manifest attestation",
    )
    path_raw = attested.get("path")
    if not isinstance(path_raw, str):
        raise ValueError("deploy manifest path must be a string")
    path = Path(path_raw)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("deploy manifest path must be absolute and traversal-free")
    if path.is_symlink() or not path.is_file():
        raise ValueError("deploy manifest must be a regular non-symlink file")
    path = path.resolve()
    if path != candidate / f"{program}.deploy-manifest.json":
        raise ValueError("deploy manifest path does not bind candidate output directory")
    if attested.get("sha256") != sha(path):
        raise ValueError("deploy manifest SHA256 attestation drift")
    payload = json.loads(path.read_text(encoding="utf-8"))
    bound = payload
    if source_class == "fresh_controller_snapshot":
        if attested.get("schema") != "ur10e.controller.deployment-manifest/v1":
            raise ValueError("fresh deploy manifest schema attestation drift")
        if attested.get("promotable") is not True:
            raise ValueError("fresh deploy manifest must be marked promotable")
    else:
        _strict_object(
            payload,
            {"schema", "promotable", "blocked_reason", "would_deploy"},
            label="non-promotable deploy manifest",
        )
        if (
            payload.get("schema")
            != "step5d.autotune.non-promotable-deploy-manifest/v1"
            or payload.get("promotable") is not False
            or payload.get("blocked_reason") != "stale_source_fixture"
            or attested.get("schema") != payload.get("schema")
            or attested.get("promotable") is not False
        ):
            raise ValueError("stale fixture deploy manifest promotion guard drift")
        bound = payload.get("would_deploy")
    bound = _strict_object(
        bound,
        {"schema_version", "basename", "controller_directory", "artifacts"},
        label="bound deployment manifest",
    )
    if (
        bound.get("schema_version") != 1
        or bound.get("basename") != program
        or bound.get("controller_directory") != controller_directory
    ):
        raise ValueError("deploy manifest identity binding drift")
    artifacts = bound.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != len(EXTENSIONS):
        raise ValueError("deploy manifest must bind exactly one triplet")
    seen: set[str] = set()
    for row in artifacts:
        row = _strict_object(
            row,
            {"filename", "source", "sha256"},
            label="deploy manifest artifact",
        )
        filename = row.get("filename")
        if not isinstance(filename, str):
            raise ValueError("deploy manifest artifact filename must be a string")
        suffix = Path(filename).suffix
        if (
            suffix not in EXTENSIONS
            or suffix in seen
            or filename != f"{program}{suffix}"
            or row.get("source") != filename
            or row.get("sha256") != candidate_sha.get(suffix)
        ):
            raise ValueError("deploy manifest exact triplet binding drift")
        seen.add(suffix)
    if seen != set(EXTENSIONS):
        raise ValueError("deploy manifest triplet suffix set drift")


def verify_triplet(
    *,
    current: Path,
    candidate: Path,
    program: str,
    attestation_path: Path,
) -> dict[str, Any]:
    from build_step5d_autotune_v2_tp import (  # noqa: PLC0415
        CANDIDATE_PROGRAM,
        RECEIPT_SCHEMA,
        SOURCE_PROGRAM,
        V2_STAMP_RE,
        load_snapshot_receipt,
    )

    if current.is_symlink() or candidate.is_symlink() or attestation_path.is_symlink():
        raise ValueError("triplet/attestation paths must not be symlinks")
    current = current.resolve()
    candidate = candidate.resolve()
    attestation_path = attestation_path.resolve()
    if not current.is_dir() or not candidate.is_dir():
        raise ValueError("current and candidate triplet directories must exist")
    if not attestation_path.is_file():
        raise ValueError("TP watchdog diff attestation is missing")
    attestation = _strict_object(
        json.loads(attestation_path.read_text(encoding="utf-8")),
        ATTESTATION_KEYS,
        label="TP watchdog diff attestation",
    )
    if attestation.get("schema") != "step5d.autotune.tp-watchdog-diff/v2":
        raise ValueError("unknown TP watchdog diff attestation schema")
    if program != CANDIDATE_PROGRAM or attestation.get("candidate_program") != program:
        raise ValueError("candidate program identity attestation drift")
    if attestation.get("source_program") != SOURCE_PROGRAM:
        raise ValueError("source program identity attestation drift")
    source_class = attestation.get("source_class")
    if source_class not in {"fresh_controller_snapshot", "stale_source_fixture"}:
        raise ValueError("unsupported attested source_class")
    promotable = source_class == "fresh_controller_snapshot"
    if (
        attestation.get("promotable") is not promotable
        or attestation.get("blocked_reason")
        != (None if promotable else "stale_source_fixture")
    ):
        raise ValueError("source-class promotion classification drift")
    if attestation.get("allowed_changes") != ALLOWED_CHANGES:
        raise ValueError("TP watchdog allowed-change policy drift")
    for field, label in (
        ("control_math_changed", "control math"),
        ("trajectory_changed", "trajectory"),
        ("waypoint_changed", "waypoint"),
        ("command_order_changed", "command order"),
    ):
        if attestation.get(field) is not False:
            raise ValueError(f"TP watchdog attestation must prove unchanged {label}")

    source_receipt = _strict_object(
        attestation.get("source_receipt"),
        SOURCE_RECEIPT_KEYS,
        label="source receipt attestation",
    )
    receipt_path_raw = source_receipt.get("path")
    if not isinstance(receipt_path_raw, str):
        raise ValueError("source receipt path must be a string")
    receipt = load_snapshot_receipt(Path(receipt_path_raw), current)
    expected_receipt = {
        "schema": RECEIPT_SCHEMA,
        "path": str(receipt.path),
        "sha256": receipt.sha256,
        "host": receipt.host,
        "controller_directory": str(receipt.controller_directory),
        "basename": receipt.basename,
        "output_dir": str(receipt.triplet_dir),
        "captured_at": receipt.captured_at,
        "source_class": receipt.source_class,
    }
    if source_receipt != expected_receipt:
        raise ValueError("source receipt attestation binding drift")
    if receipt.source_class != source_class:
        raise ValueError("source receipt class differs from diff attestation")
    controller_directory = str(receipt.controller_directory)
    if attestation.get("controller_directory") != controller_directory:
        raise ValueError("controller directory attestation drift")
    source_stamp = attestation.get("source_stamp")
    candidate_stamp = attestation.get("candidate_stamp")
    if not isinstance(source_stamp, str) or not source_stamp.endswith(
        "_STEP5D_STRICT_RNN_AUTOTUNE_V1"
    ):
        raise ValueError("source stamp attestation is stale or malformed")
    if not isinstance(candidate_stamp, str) or V2_STAMP_RE.fullmatch(candidate_stamp) is None:
        raise ValueError("candidate source stamp attestation is stale or malformed")
    if candidate_stamp == source_stamp:
        raise ValueError("candidate source stamp must differ from source stamp")

    fetched = _strict_object(
        attestation.get("fetched_controller_sha256"),
        set(EXTENSIONS),
        label="fetched controller SHA map",
    )
    candidate_sha = _strict_object(
        attestation.get("candidate_sha256"),
        set(EXTENSIONS),
        label="candidate SHA map",
    )
    normalized_sha = _strict_object(
        attestation.get("normalized_content_sha256"),
        set(EXTENSIONS),
        label="normalized content SHA map",
    )
    watchdog_counts = _strict_object(
        attestation.get("watchdog_block_count"),
        set(EXTENSIONS),
        label="watchdog block-count map",
    )
    attested_block_sha = _strict_object(
        attestation.get("watchdog_block_sha256"),
        set(EXTENSIONS),
        label="watchdog block SHA map",
    )
    canonical = load_canonical_blocks()
    canonical_attested = _strict_object(
        attestation.get("canonical_watchdog"),
        {"block_id", "sha256"},
        label="canonical watchdog attestation",
    )
    if len(canonical) != 1:
        raise ValueError("canonical watchdog block set must contain exactly one block")
    block_id, canonical_block = next(iter(canonical.items()))
    if canonical_attested != {
        "block_id": block_id,
        "sha256": canonical_block.sha256,
    }:
        raise ValueError("canonical watchdog source attestation drift")

    source_script_bytes = receipt.files[".script"].path.read_bytes()
    candidate_script_path = candidate / f"{program}.script"
    candidate_script_bytes = (
        candidate_script_path.read_bytes() if candidate_script_path.is_file() else b""
    )
    for extension in EXTENSIONS:
        current_path = receipt.files[extension].path
        next_path = candidate / f"{program}{extension}"
        if next_path.is_symlink() or not next_path.is_file():
            raise ValueError(f"triplet member missing, ambiguous, or symlinked: {extension}")
        if fetched.get(extension) != sha(current_path):
            raise ValueError(f"fetched controller SHA drift: {extension}")
        if candidate_sha.get(extension) != sha(next_path):
            raise ValueError(f"candidate SHA drift: {extension}")
        try:
            current_inspection = inspect_content(
                current_path,
                current_program=SOURCE_PROGRAM,
                candidate_program=program,
                source_stamp=source_stamp,
                candidate_stamp=candidate_stamp,
                controller_directory=controller_directory,
            )
            candidate_inspection = inspect_content(
                next_path,
                current_program=SOURCE_PROGRAM,
                candidate_program=program,
                source_stamp=source_stamp,
                candidate_stamp=candidate_stamp,
                controller_directory=controller_directory,
            )
            block_hashes = _validated_block_hashes(
                candidate_inspection,
                extension=extension,
                canonical=canonical,
            )
        except (OSError, ValueError, gzip.BadGzipFile) as exc:
            raise ValueError(f"cannot normalize TP diff {extension}: {exc}") from exc
        if current_inspection.blocks:
            raise ValueError(
                f"fetched controller unexpectedly contains v2 markers: {extension}"
            )
        if extension == ".urp":
            if current_inspection.executable_script != source_script_bytes:
                raise ValueError("source URP cachedContents differs from source .script")
            if candidate_inspection.executable_script != candidate_script_bytes:
                raise ValueError("candidate URP cachedContents differs from candidate .script")
        actual_normalized = hashlib.sha256(
            candidate_inspection.normalized
        ).hexdigest()
        if candidate_inspection.normalized != current_inspection.normalized:
            raise ValueError(
                f"candidate changes content outside identity/watchdog blocks: {extension}"
            )
        if normalized_sha.get(extension) != actual_normalized:
            raise ValueError(f"normalized content attestation drift: {extension}")
        if watchdog_counts.get(extension) != len(candidate_inspection.blocks):
            raise ValueError(f"watchdog block-count attestation drift: {extension}")
        if attested_block_sha.get(extension) != block_hashes:
            raise ValueError(
                f"watchdog canonical-block SHA attestation drift: {extension}"
            )

    crc_attested = _strict_object(
        attestation.get("urp_crcValue"),
        {"source", "candidate"},
        label="URP crcValue attestation",
    )
    if crc_attested != {
        "source": _root_crc(receipt.files[".urp"].path),
        "candidate": _root_crc(candidate / f"{program}.urp"),
    }:
        raise ValueError("URP crcValue attestation drift")
    _validate_bound_deploy_manifest(
        attested=_strict_object(
            attestation.get("deploy_manifest"),
            {"path", "sha256", "schema", "promotable"},
            label="deploy manifest attestation",
        ),
        source_class=source_class,
        candidate=candidate,
        program=program,
        controller_directory=controller_directory,
        candidate_sha=candidate_sha,
    )
    return {
        "schema": "step5d.autotune.tp-watchdog-diff-verification/v2",
        "pass": True,
        "source_class": source_class,
        "promotable": promotable,
        "program": program,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--current-readback", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--program", default="step5d_strict_rnn_autotune_v2")
    parser.add_argument("--attestation", type=Path, required=True)
    args = parser.parse_args()
    try:
        verify_triplet(
            current=args.current_readback,
            candidate=args.candidate,
            program=args.program,
            attestation_path=args.attestation,
        )
    except (OSError, ValueError, json.JSONDecodeError, gzip.BadGzipFile) as exc:
        raise SystemExit(str(exc)) from exc
    print("tp_watchdog_diff_gate=pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
