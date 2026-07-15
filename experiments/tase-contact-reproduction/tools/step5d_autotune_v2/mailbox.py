"""Small atomic command transport; durable state remains in SQLite."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .model import canonical_json_bytes


MAILBOX_SCHEMA = "step5d.autotune.mailbox/v2"
MAX_MAILBOX_BYTES = 2 * 1024 * 1024


class MailboxError(RuntimeError):
    """Raised when a mailbox snapshot is incomplete, unsafe, or invalid."""


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


@dataclass(frozen=True)
class MailboxEnvelope:
    sequence: int
    payload: Mapping[str, Any]
    checksum: str
    inode: int


class AtomicMailbox:
    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise MailboxError("mailbox path must be absolute")
        self.path = path

    @staticmethod
    def _material(sequence: int, payload: Mapping[str, Any]) -> dict[str, Any]:
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise MailboxError("mailbox sequence must be a positive integer")
        return {"schema": MAILBOX_SCHEMA, "sequence": sequence, "payload": dict(payload)}

    def publish(self, *, sequence: int, payload: Mapping[str, Any]) -> str:
        material = self._material(sequence, payload)
        checksum = self.checksum_for(sequence=sequence, payload=payload)
        encoded = canonical_json_bytes({**material, "checksum": checksum})
        if len(encoded) > MAX_MAILBOX_BYTES:
            raise MailboxError("mailbox payload exceeds the bounded transport size")
        parent = self.path.parent
        if not parent.is_dir() or parent.is_symlink():
            raise MailboxError("mailbox parent must be an existing real directory")
        if self.path.is_symlink():
            raise MailboxError("mailbox must not replace a symlink")
        directory_fd = os.open(
            parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        temporary = parent / f".{self.path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        descriptor = -1
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise MailboxError("short mailbox write")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, self.path)
            os.fsync(directory_fd)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            os.close(directory_fd)
        return checksum

    @classmethod
    def checksum_for(cls, *, sequence: int, payload: Mapping[str, Any]) -> str:
        return hashlib.sha256(
            canonical_json_bytes(cls._material(sequence, payload))
        ).hexdigest()

    def read_latest(self) -> MailboxEnvelope | None:
        try:
            descriptor = os.open(
                self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise MailboxError("mailbox is unsafe or unreadable") from exc
        try:
            before = os.fstat(descriptor)
            # A valid old inode may become unlinked immediately after open when
            # the writer commits a new snapshot with os.replace().  The open fd
            # remains a complete immutable snapshot, so nlink=0 is expected.
            if not stat.S_ISREG(before.st_mode) or before.st_nlink not in {0, 1}:
                raise MailboxError("mailbox must be a regular file without hard links")
            if before.st_size > MAX_MAILBOX_BYTES:
                raise MailboxError("mailbox snapshot exceeds the bounded transport size")
            chunks: list[bytes] = []
            remaining = MAX_MAILBOX_BYTES + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if before_identity != after_identity:
            raise MailboxError("opened mailbox inode changed while being read")
        encoded = b"".join(chunks)
        if len(encoded) > MAX_MAILBOX_BYTES or len(encoded) != before.st_size:
            raise MailboxError("mailbox snapshot is incomplete")
        try:
            value = json.loads(
                encoded.decode("ascii"),
                parse_constant=_reject_constant,
                object_pairs_hook=_unique_object,
            )
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise MailboxError(f"mailbox is not strict finite JSON: {exc}") from exc
        if not isinstance(value, dict) or set(value) != {
            "schema",
            "sequence",
            "payload",
            "checksum",
        }:
            raise MailboxError("mailbox has unknown or missing top-level fields")
        if value["schema"] != MAILBOX_SCHEMA or not isinstance(value["payload"], dict):
            raise MailboxError("mailbox schema or payload shape is invalid")
        material = self._material(value["sequence"], value["payload"])
        checksum = hashlib.sha256(canonical_json_bytes(material)).hexdigest()
        if value["checksum"] != checksum:
            raise MailboxError("mailbox checksum mismatch")
        return MailboxEnvelope(
            sequence=value["sequence"],
            payload=value["payload"],
            checksum=checksum,
            inode=before.st_ino,
        )
