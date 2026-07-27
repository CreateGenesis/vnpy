from __future__ import annotations

import json
import socket
import struct
from hashlib import sha256
from pathlib import Path
from threading import Thread
from typing import Any

import pytest

from vnpy.demo_web.runtime import build_demo_runtime
from vnpy.demo_web.transport import LengthPrefixedJsonTransport


def digest(label: str) -> str:
    return f"sha256:{sha256(label.encode()).hexdigest()}"


def test_runtime_starts_blocked_without_candidate_or_local_services(tmp_path: Path) -> None:
    runtime = build_demo_runtime(tmp_path, host="127.0.0.1", port=8765)

    readiness = runtime.backend.readiness()
    projection = runtime.backend.projection()

    assert readiness["ready"] is False
    assert readiness["state"] == "blocked"
    assert {item["code"] for item in readiness["blockers"]} >= {
        "CANDIDATE_NOT_READY",
        "RUN_XTP_UNAVAILABLE",
        "RUN_TORA_UNAVAILABLE",
        "SIDE_MASTER_UNAVAILABLE",
    }
    assert projection["candidate"]["readiness"] == "unavailable"
    assert projection["current"]["campaign_state"] == "unavailable"
    assert projection["permitted_actions"] == ["emergency_stop"]
    assert runtime.guidance is None


def test_runtime_rejects_malformed_candidate_and_non_loopback_descriptors(
    tmp_path: Path,
) -> None:
    state = tmp_path / ".demo-state"
    state.mkdir()
    (state / "ready-candidate.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="DEMO_CANDIDATE_INVALID"):
        build_demo_runtime(tmp_path, host="127.0.0.1", port=8765)

    candidate = {
        "contract_version": 1,
        "ready": True,
        "candidate_digest": digest("candidate"),
        "author_lineage_digest": digest("author"),
        "package_digest": digest("package"),
        "configuration_digest": digest("configuration"),
        "policy_digest": digest("policy"),
        "symbols": ["600000.SSE"],
        "calendar_sessions": [
            "2026-07-27",
            "2026-07-28",
            "2026-07-29",
            "2026-07-30",
            "2026-07-31",
        ],
        "lifecycle_revision": 1,
    }
    (state / "ready-candidate.json").write_text(json.dumps(candidate), encoding="utf-8")
    endpoint_root = state / "runs" / "XTP"
    endpoint_root.mkdir(parents=True)
    (endpoint_root / "endpoint.json").write_text(
        json.dumps(
            {
                "contract_version": 1,
                "transport": "tcp-loopback",
                "address": "203.0.113.8:17801",
                "gateway": "XTP",
                "run_digest": digest("run"),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="DEMO_ENDPOINT_LOOPBACK_REQUIRED"):
        build_demo_runtime(tmp_path, host="127.0.0.1", port=8765)


def test_length_prefixed_transport_authenticates_and_round_trips_strict_json() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    address = listener.getsockname()
    captured: list[dict[str, Any]] = []

    def serve() -> None:
        connection, _ = listener.accept()
        with connection:
            size = struct.unpack(">I", _read_exact(connection, 4))[0]
            request = json.loads(_read_exact(connection, size))
            captured.append(request)
            response = json.dumps({"status": "ok", "echo": request["operation"]}).encode()
            connection.sendall(struct.pack(">I", len(response)) + response)
        listener.close()

    thread = Thread(target=serve)
    thread.start()
    transport = LengthPrefixedJsonTransport("local-secret-token-value-00000001")
    result = transport.request(
        f"tcp://{address[0]}:{address[1]}",
        "demo.side_master.chat.v1",
        {"content": "research only"},
    )
    thread.join(timeout=2)

    assert result == {"status": "ok", "echo": "demo.side_master.chat.v1"}
    assert captured == [
        {
            "kind": "demo_command",
            "transport_version": 1,
            "transport_token": "local-secret-token-value-00000001",
            "operation": "demo.side_master.chat.v1",
            "payload": {"content": "research only"},
        }
    ]


def test_transport_rejects_http_and_remote_endpoints() -> None:
    transport = LengthPrefixedJsonTransport("local-secret-token-value-00000001")
    with pytest.raises(ValueError, match="IPC_LOOPBACK_REQUIRED"):
        transport.request("http://127.0.0.1:17801", "health", {})
    with pytest.raises(ValueError, match="IPC_LOOPBACK_REQUIRED"):
        transport.request("tcp://192.0.2.1:17801", "health", {})


def _read_exact(connection: socket.socket, size: int) -> bytes:
    payload = bytearray()
    while len(payload) < size:
        chunk = connection.recv(size - len(payload))
        if not chunk:
            raise RuntimeError("connection closed")
        payload.extend(chunk)
    return bytes(payload)
