"""Bounded length-prefixed JSON transport for local demo services."""

from __future__ import annotations

from collections.abc import Mapping
import json
import socket
import struct
from typing import Any
from urllib.parse import urlsplit


class LengthPrefixedJsonTransport:
    """Authenticate one fixed operation over a bounded loopback TCP frame."""

    def __init__(
        self,
        transport_token: str,
        *,
        timeout_seconds: float = 5.0,
        maximum_bytes: int = 1_048_576,
    ) -> None:
        if not 24 <= len(transport_token) <= 512 or not transport_token.isascii():
            raise ValueError("IPC_TOKEN_INVALID")
        if not 0.1 <= timeout_seconds <= 120 or not 1_024 <= maximum_bytes <= 8_388_608:
            raise ValueError("IPC_CONFIGURATION_INVALID")
        self._transport_token = transport_token
        self._timeout_seconds = timeout_seconds
        self._maximum_bytes = maximum_bytes

    def request(
        self,
        endpoint: str,
        operation: str,
        payload: dict[str, Any],
    ) -> Mapping[str, Any]:
        host, port = _parse_endpoint(endpoint)
        if not operation or len(operation) > 128 or not isinstance(payload, dict):
            raise ValueError("IPC_REQUEST_INVALID")
        envelope = {
            "kind": "demo_command",
            "transport_version": 1,
            "transport_token": self._transport_token,
            "operation": operation,
            "payload": payload,
        }
        encoded = json.dumps(
            envelope,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if not encoded or len(encoded) > self._maximum_bytes:
            raise ValueError("IPC_REQUEST_TOO_LARGE")
        try:
            with socket.create_connection(
                (host, port), timeout=self._timeout_seconds
            ) as connection:
                connection.settimeout(self._timeout_seconds)
                connection.sendall(struct.pack(">I", len(encoded)) + encoded)
                response_size = struct.unpack(">I", _read_exact(connection, 4))[0]
                if not 1 <= response_size <= self._maximum_bytes:
                    raise RuntimeError("IPC_RESPONSE_TOO_LARGE")
                response_bytes = _read_exact(connection, response_size)
        except (OSError, TimeoutError) as exc:
            raise RuntimeError("IPC_UNAVAILABLE") from exc
        try:
            value = json.loads(
                response_bytes,
                object_pairs_hook=_unique_object,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError("IPC_RESPONSE_INVALID") from exc
        if not isinstance(value, dict):
            raise RuntimeError("IPC_RESPONSE_INVALID")
        return value


def _parse_endpoint(endpoint: str) -> tuple[str, int]:
    parsed = urlsplit(endpoint)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("IPC_LOOPBACK_REQUIRED") from exc
    if (
        parsed.scheme != "tcp"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or not 1 <= port <= 65_535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("IPC_LOOPBACK_REQUIRED")
    return "127.0.0.1", port


def _read_exact(connection: socket.socket, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        chunk = connection.recv(size - len(result))
        if not chunk:
            raise RuntimeError("IPC_RESPONSE_TRUNCATED")
        result.extend(chunk)
    return bytes(result)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result
