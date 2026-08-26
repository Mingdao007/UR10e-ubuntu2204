#!/usr/bin/env python3

from __future__ import annotations

import ipaddress
import json
import socket
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULTS_PATH = SKILL_ROOT / "bench_defaults.json"


def load_defaults(path: str | None = None) -> dict[str, Any]:
    defaults_path = Path(path) if path else DEFAULTS_PATH
    with defaults_path.open(encoding="utf-8") as handle:
        return json.load(handle)


def run_command(args: list[str], check: bool = True) -> str:
    completed = subprocess.run(args, check=check, capture_output=True, text=True)
    return completed.stdout


def emit(summary_lines: list[str], payload: dict[str, Any], json_only: bool = False) -> None:
    if not json_only:
        for line in summary_lines:
            print(line)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def emit_error(message: str, json_only: bool = False, **context: Any) -> None:
    payload = {"ok": False, "error": message, **context}
    emit([f"error={message}"], payload, json_only=json_only)


def parse_ipv4(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def same_subnet(ip_a: str | None, ip_b: str | None, prefix: int = 24) -> bool | None:
    if not ip_a or not ip_b:
        return None
    try:
        net_a = ipaddress.ip_network(f"{ip_a}/{prefix}", strict=False)
        return ipaddress.ip_address(ip_b) in net_a
    except ValueError:
        return None


def dashboard_exchange(host: str, commands: list[str], port: int = 29999, timeout: float = 3.0) -> dict[str, str]:
    responses: dict[str, str] = {}
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        banner = sock.recv(1024).decode(errors="ignore").strip()
        responses["banner"] = banner
        for command in commands:
            sock.sendall((command + "\n").encode())
            responses[command] = sock.recv(1024).decode(errors="ignore").strip()
    return responses


def probe_port(host: str, port: int, timeout: float = 1.5) -> dict[str, Any]:
    result: dict[str, Any] = {"port": port, "open": False}
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect((host, port))
            result["open"] = True
            try:
                banner = sock.recv(128)
            except socket.timeout:
                banner = b""
            if banner:
                try:
                    text = banner.decode("ascii")
                    if text.isprintable():
                        result["banner"] = text.strip()
                    else:
                        result["banner_hex"] = banner.hex()
                except UnicodeDecodeError:
                    result["banner_hex"] = banner.hex()
        except OSError as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
    return result


class RTDEClient:
    def __init__(self, host: str, port: int = 30004, timeout: float = 3.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock: socket.socket | None = None

    def __enter__(self) -> "RTDEClient":
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def _recvn(self, n: int) -> bytes:
        assert self.sock is not None
        data = b""
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise RuntimeError("socket closed")
            data += chunk
        return data

    def _recv_packet(self) -> tuple[int, bytes]:
        header = self._recvn(3)
        size, ptype = struct.unpack("!HB", header)
        payload = self._recvn(size - 3) if size > 3 else b""
        return ptype, payload

    def _send_packet(self, ptype: str, payload: bytes = b"") -> None:
        assert self.sock is not None
        frame = struct.pack("!HB", 3 + len(payload), ord(ptype)) + payload
        self.sock.sendall(frame)

    def negotiate(self, version: int = 2) -> None:
        self._send_packet("V", struct.pack("!H", version))
        ptype, payload = self._recv_packet()
        if ptype != ord("V") or payload != b"\x01":
            raise RuntimeError(f"RTDE protocol negotiation failed: type={ptype} payload={payload!r}")

    def setup_outputs(self, frequency_hz: float, fields: list[str]) -> tuple[int, list[str]]:
        payload = struct.pack("!d", frequency_hz) + ",".join(fields).encode()
        self._send_packet("O", payload)
        ptype, data = self._recv_packet()
        if ptype != ord("O"):
            raise RuntimeError(f"unexpected RTDE setup response type {ptype}")
        recipe_id = data[0]
        type_names = data[1:].decode("ascii", errors="replace").split(",")
        if recipe_id == 0 or any(name == "NOT_FOUND" for name in type_names):
            raise RuntimeError(f"invalid RTDE output recipe: id={recipe_id} types={type_names}")
        return recipe_id, type_names

    def start(self) -> None:
        self._send_packet("S")
        ptype, payload = self._recv_packet()
        if ptype != ord("S") or payload != b"\x01":
            raise RuntimeError(f"RTDE start failed: type={ptype} payload={payload!r}")

    def recv_recipe_sample(self, recipe_id: int, type_names: list[str]) -> list[Any]:
        while True:
            ptype, payload = self._recv_packet()
            if ptype != ord("U"):
                continue
            if payload[0] != recipe_id:
                continue
            cursor = 1
            values: list[Any] = []
            for type_name in type_names:
                fmt = _type_to_struct(type_name)
                width = struct.calcsize("!" + fmt)
                unpacked = struct.unpack("!" + fmt, payload[cursor:cursor + width])
                cursor += width
                if len(unpacked) == 1:
                    values.append(unpacked[0])
                else:
                    values.append(list(unpacked))
            return values


def _type_to_struct(type_name: str) -> str:
    mapping = {
        "DOUBLE": "d",
        "VECTOR3D": "3d",
        "VECTOR6D": "6d",
        "UINT32": "I",
        "UINT64": "Q",
        "INT32": "i",
        "BOOL": "?",
    }
    if type_name not in mapping:
        raise RuntimeError(f"unsupported RTDE type in v1 helper: {type_name}")
    return mapping[type_name]


def read_rtde_once(host: str, fields: list[str], frequency_hz: float = 10.0, port: int = 30004, timeout: float = 3.0) -> dict[str, Any]:
    with RTDEClient(host, port=port, timeout=timeout) as client:
        client.negotiate()
        recipe_id, type_names = client.setup_outputs(frequency_hz, fields)
        client.start()
        values = client.recv_recipe_sample(recipe_id, type_names)
    return {field: value for field, value in zip(fields, values)}


def abort(message: str) -> None:
    raise SystemExit(message)


def main_guard(func: Any) -> None:
    try:
        func()
    except KeyboardInterrupt:
        abort("interrupted")
    except Exception as exc:  # pragma: no cover - best-effort CLI guard
        abort(f"{type(exc).__name__}: {exc}")
