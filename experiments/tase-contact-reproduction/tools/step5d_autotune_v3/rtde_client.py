"""Repository-owned minimal Dashboard and RTDE v2 clients."""

from __future__ import annotations

import socket
import struct
from typing import Any, Sequence


def dashboard_exchange(
    host: str,
    commands: Sequence[str],
    *,
    port: int = 29999,
    timeout: float = 3.0,
) -> dict[str, str]:
    responses: dict[str, str] = {}
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        responses["banner"] = sock.recv(1024).decode(errors="ignore").strip()
        for command in commands:
            sock.sendall((command + "\n").encode())
            responses[command] = sock.recv(1024).decode(errors="ignore").strip()
    return responses


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

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def _recvn(self, size: int) -> bytes:
        if self.sock is None:
            raise RuntimeError("RTDE socket is not connected")
        data = bytearray()
        while len(data) < size:
            chunk = self.sock.recv(size - len(data))
            if not chunk:
                raise RuntimeError("RTDE socket closed")
            data.extend(chunk)
        return bytes(data)

    def _recv_packet(self) -> tuple[int, bytes]:
        packet_size, packet_type = struct.unpack("!HB", self._recvn(3))
        if packet_size < 3:
            raise RuntimeError("RTDE packet size is invalid")
        return packet_type, self._recvn(packet_size - 3)

    def _send_packet(self, packet_type: str, payload: bytes = b"") -> None:
        if self.sock is None:
            raise RuntimeError("RTDE socket is not connected")
        self.sock.sendall(
            struct.pack("!HB", 3 + len(payload), ord(packet_type)) + payload
        )

    def negotiate(self, version: int = 2) -> None:
        self._send_packet("V", struct.pack("!H", version))
        packet_type, payload = self._recv_packet()
        if packet_type != ord("V") or payload != b"\x01":
            raise RuntimeError("RTDE protocol negotiation failed")

    def setup_outputs(
        self, frequency_hz: float, fields: Sequence[str]
    ) -> tuple[int, list[str]]:
        self._send_packet("O", struct.pack("!d", frequency_hz) + ",".join(fields).encode())
        packet_type, payload = self._recv_packet()
        if packet_type != ord("O") or not payload:
            raise RuntimeError("RTDE output setup failed")
        recipe_id = payload[0]
        type_names = payload[1:].decode("ascii", errors="replace").split(",")
        if recipe_id == 0 or len(type_names) != len(fields) or "NOT_FOUND" in type_names:
            raise RuntimeError("RTDE output recipe is invalid")
        return recipe_id, type_names

    def start(self) -> None:
        self._send_packet("S")
        packet_type, payload = self._recv_packet()
        if packet_type != ord("S") or payload != b"\x01":
            raise RuntimeError("RTDE start failed")

    def recv_recipe_sample(
        self, recipe_id: int, type_names: Sequence[str]
    ) -> list[Any]:
        while True:
            packet_type, payload = self._recv_packet()
            if packet_type != ord("U") or not payload or payload[0] != recipe_id:
                continue
            cursor = 1
            values: list[Any] = []
            for type_name in type_names:
                format_code = _TYPE_FORMATS.get(type_name)
                if format_code is None:
                    raise RuntimeError(f"unsupported RTDE type: {type_name}")
                width = struct.calcsize("!" + format_code)
                end = cursor + width
                if end > len(payload):
                    raise RuntimeError("RTDE sample is truncated")
                unpacked = struct.unpack("!" + format_code, payload[cursor:end])
                values.append(unpacked[0] if len(unpacked) == 1 else list(unpacked))
                cursor = end
            if cursor != len(payload):
                raise RuntimeError("RTDE sample has trailing bytes")
            return values


_TYPE_FORMATS = {
    "DOUBLE": "d",
    "VECTOR3D": "3d",
    "VECTOR6D": "6d",
    "UINT32": "I",
    "UINT64": "Q",
    "INT32": "i",
    "BOOL": "?",
}


__all__ = ["RTDEClient", "dashboard_exchange"]
