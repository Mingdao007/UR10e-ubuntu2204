#!/usr/bin/env python3
"""Fail closed until TP v2 is proven to preserve the fetched controller triplet."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any


EXTENSIONS = (".script", ".txt", ".urp")
ROOT = Path(__file__).resolve().parents[1]
WATCHDOG_MANIFEST = ROOT / "config/step5/tp_watchdog_v2.json"
EXPECTED_BLOCK_REQUIREMENTS = {
    "host_heartbeat_fail_closed_v1": frozenset({".script", ".urp"}),
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


def _normalize_urscript_identity(
    raw: bytes, *, source_program: str, current_program: str, source: Path
) -> bytes:
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
    matching_files = [node for node in file_nodes if (node.text or "").endswith(file_suffix)]
    if len(matching_files) > 1:
        raise ValueError(f"URP program file identity is ambiguous: {path}")
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
    return ContentInspection(normalized=normalized, blocks=tuple(escaped_blocks))


def _validate_canonical_source(block: WatchdogBlock, *, source: Path) -> None:
    code_lines = tuple(
        line.strip()
        for line in block.payload.splitlines()
        if line.strip() and not line.lstrip().startswith(b"#")
    )
    if not code_lines:
        raise ValueError(f"canonical watchdog block is comment-only: {source}")
    required_code = (
        b"if host_heartbeat_timeout:",
        b'textmsg("host_heartbeat_timeout_at_home")',
        b'textmsg("host_heartbeat_timeout_unknown_home")',
        b"halt",
    )
    missing = [line.decode("ascii") for line in required_code if line not in code_lines]
    if missing:
        raise ValueError(f"canonical watchdog block lacks executable policy {missing}: {source}")
    forbidden = (
        b"movej(",
        b"movel(",
        b"speedj(",
        b"speedl(",
        b"servoj(",
        b"force_mode(",
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
    gate = manifest.get("watchdog_diff_gate")
    if not isinstance(gate, dict) or gate.get("schema") != "step5d.autotune.tp-watchdog-blocks/v2":
        raise ValueError("unknown TP watchdog canonical-block manifest schema")
    if gate.get("outside_block_normalization") != ["program_identity", "urp_crcValue"]:
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
        normalized = _normalize_urscript_identity(
            outside,
            source_program=source_program,
            current_program=current_program,
            source=path,
        )
    elif path.suffix == ".txt":
        if blocks:
            raise ValueError(f"watchdog block is forbidden in TP documentation: {path}")
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--current-readback", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--program", default="step5d_strict_rnn_autotune_v2")
    parser.add_argument("--attestation", type=Path, required=True)
    args = parser.parse_args()
    current = args.current_readback.resolve()
    candidate = args.candidate.resolve()
    attestation = json.loads(args.attestation.read_text(encoding="utf-8"))
    if attestation.get("schema") != "step5d.autotune.tp-watchdog-diff/v2":
        raise SystemExit("unknown TP watchdog diff attestation schema")
    if attestation.get("control_math_changed") is not False:
        raise SystemExit("TP watchdog attestation must prove unchanged control math")
    if attestation.get("trajectory_changed") is not False:
        raise SystemExit("TP watchdog attestation must prove unchanged trajectory")
    if attestation.get("waypoint_changed") is not False:
        raise SystemExit("TP watchdog attestation must prove unchanged waypoint")
    fetched = attestation.get("fetched_controller_sha256") or {}
    candidate_sha = attestation.get("candidate_sha256") or {}
    normalized_sha = attestation.get("normalized_content_sha256") or {}
    watchdog_counts = attestation.get("watchdog_block_count") or {}
    attested_block_sha = attestation.get("watchdog_block_sha256") or {}
    try:
        canonical = load_canonical_blocks()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot load canonical TP watchdog blocks: {exc}") from exc
    for extension in EXTENSIONS:
        current_matches = list(current.glob(f"*{extension}"))
        next_path = candidate / f"{args.program}{extension}"
        if len(current_matches) != 1 or not next_path.is_file():
            raise SystemExit(f"triplet member missing or ambiguous: {extension}")
        if fetched.get(extension) != sha(current_matches[0]):
            raise SystemExit(f"fetched controller SHA drift: {extension}")
        if candidate_sha.get(extension) != sha(next_path):
            raise SystemExit(f"candidate SHA drift: {extension}")
        try:
            current_inspection = inspect_content(
                current_matches[0],
                current_program=current_matches[0].stem,
                candidate_program=args.program,
            )
            candidate_inspection = inspect_content(
                next_path,
                current_program=current_matches[0].stem,
                candidate_program=args.program,
            )
            block_hashes = _validated_block_hashes(
                candidate_inspection,
                extension=extension,
                canonical=canonical,
            )
        except (OSError, ValueError, gzip.BadGzipFile) as exc:
            raise SystemExit(f"cannot normalize TP diff {extension}: {exc}") from exc
        if current_inspection.blocks:
            raise SystemExit(f"fetched controller unexpectedly contains v2 markers: {extension}")
        actual_normalized = hashlib.sha256(candidate_inspection.normalized).hexdigest()
        if candidate_inspection.normalized != current_inspection.normalized:
            raise SystemExit(
                f"candidate changes content outside identity/watchdog blocks: {extension}"
            )
        if normalized_sha.get(extension) != actual_normalized:
            raise SystemExit(f"normalized content attestation drift: {extension}")
        if watchdog_counts.get(extension) != len(candidate_inspection.blocks):
            raise SystemExit(f"watchdog block-count attestation drift: {extension}")
        if attested_block_sha.get(extension) != block_hashes:
            raise SystemExit(f"watchdog canonical-block SHA attestation drift: {extension}")
    print("tp_watchdog_diff_gate=pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
