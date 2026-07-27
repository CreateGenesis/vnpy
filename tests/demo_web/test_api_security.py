from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import re
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from vnpy.demo_web.app import DemoWebConfig, create_demo_app
from vnpy.demo_web.run_clients import BrokerSimulationRunClient, RunClientBinding


SESSION_TOKEN = "s" * 48
CSRF_TOKEN = "c" * 48
ORIGIN = "http://127.0.0.1:8765"


def digest(label: str) -> str:
    return f"sha256:{sha256(label.encode()).hexdigest()}"


@dataclass
class FakeBackend:
    commands: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    leak_readiness: bool = False
    projection_revision: int = 4

    def readiness(self) -> dict[str, Any]:
        if self.leak_readiness:
            return {"state": "ready", "credential_ref": "host-secret"}
        return {"state": "ready", "candidate_digest": digest("candidate")}

    def projection(self) -> dict[str, Any]:
        return {
            "contract_version": 1,
            "entity_type": "investor_demo_projection",
            "revision": self.projection_revision,
            "performance_scope": "broker_simulation",
            "candidate": {"candidate_digest": digest("candidate")},
            "current": {"label": "current_broker_simulation", "gateways": []},
            "history": [],
        }

    def start_campaign(self, command: dict[str, Any]) -> dict[str, Any]:
        self.commands.append(("start", command))
        return {"campaign_digest": digest("campaign"), "state": "starting"}

    def pause_campaign(self, campaign_id: str) -> dict[str, Any]:
        self.commands.append(("pause", {"campaign_id": campaign_id}))
        return {"campaign_id": campaign_id, "state": "paused"}

    def emergency_stop(self) -> dict[str, Any]:
        self.commands.append(("emergency_stop", {}))
        return {"receipt_digest": digest("stop"), "state": "stopped"}

    def evidence(self, campaign_id: str) -> dict[str, Any]:
        return {
            "campaign_id": campaign_id,
            "scope": "historical_broker_simulation_evidence",
            "evidence_digest": digest("evidence"),
        }


def config(**overrides: Any) -> DemoWebConfig:
    values = {
        "bind_host": "127.0.0.1",
        "port": 8765,
        "allowed_origin": ORIGIN,
        "session_token": SESSION_TOKEN,
        "csrf_token": CSRF_TOKEN,
    }
    values.update(overrides)
    return DemoWebConfig(**values)


def authenticated_client(backend: FakeBackend | None = None) -> tuple[TestClient, FakeBackend]:
    selected = backend or FakeBackend()
    client = TestClient(create_demo_app(config(), selected))
    client.cookies.set("auto_trade_host_session", SESSION_TOKEN)
    return client, selected


def test_app_rejects_non_loopback_bind_and_cross_origin_configuration() -> None:
    with pytest.raises(ValueError, match="DEMO_LOOPBACK_BIND_REQUIRED"):
        create_demo_app(config(bind_host="0.0.0.0"), FakeBackend())
    with pytest.raises(ValueError, match="DEMO_SAME_ORIGIN_REQUIRED"):
        create_demo_app(config(allowed_origin="https://demo.example"), FakeBackend())


def test_api_requires_host_session_and_csrf_for_every_write() -> None:
    backend = FakeBackend()
    client = TestClient(create_demo_app(config(), backend))

    assert client.get("/api/v1/readiness").status_code == 401
    client.cookies.set("auto_trade_host_session", "wrong-token-that-is-still-long-enough")
    assert client.get("/api/v1/readiness").status_code == 401
    client.cookies.set("auto_trade_host_session", SESSION_TOKEN)
    assert client.get("/api/v1/readiness").status_code == 200

    body = {
        "candidate_digest": digest("candidate"),
        "gateways": ["XTP", "TORA"],
        "idempotency_key": "campaign-request-0001",
    }
    assert client.post("/api/v1/campaigns", json=body).status_code == 403
    assert client.post(
        "/api/v1/campaigns",
        json=body,
        headers={"Origin": "http://evil.invalid", "X-CSRF-Token": CSRF_TOKEN},
    ).status_code == 403
    response = client.post(
        "/api/v1/campaigns",
        json=body,
        headers={"Origin": ORIGIN, "X-CSRF-Token": CSRF_TOKEN},
    )
    assert response.status_code == 202
    assert backend.commands == [("start", body)]


def test_routes_are_allowlisted_and_forbidden_trading_surfaces_do_not_exist() -> None:
    client, _ = authenticated_client()
    route_paths = {route.path for route in client.app.routes if route.path.startswith("/api/")}
    assert route_paths == {
        "/api/v1/readiness",
        "/api/v1/projection",
        "/api/v1/campaigns",
        "/api/v1/campaigns/{campaign_id}/pause",
        "/api/v1/emergency-stop",
        "/api/v1/evidence/{campaign_id}",
        "/api/v1/events",
    }

    for path in (
        "/api/v1/orders",
        "/api/v1/orders/123/cancel",
        "/api/v1/rpc",
        "/api/v1/risk",
        "/api/v1/lifecycle",
        "/docs",
        "/openapi.json",
    ):
        assert client.post(
            path,
            headers={"Origin": ORIGIN, "X-CSRF-Token": CSRF_TOKEN},
        ).status_code == 404


def test_root_bootstraps_same_origin_session_and_serves_built_assets() -> None:
    client = TestClient(create_demo_app(config(), FakeBackend()))
    response = client.get("/")

    assert response.status_code == 200
    assert "Auto Trade Investor Broker Simulation" in response.text
    assert "__AUTO_TRADE_CSRF_TOKEN__" not in response.text
    assert CSRF_TOKEN in response.text
    assert response.headers["cache-control"] == "no-store"
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]

    asset_path = re.search(r'(?:src|href)="(/assets/[^"]+)"', response.text)
    assert asset_path is not None
    assert client.get(asset_path.group(1)).status_code == 200


def test_responses_fail_closed_on_secret_bearing_backend_data() -> None:
    client, _ = authenticated_client(FakeBackend(leak_readiness=True))
    response = client.get("/api/v1/readiness")

    assert response.status_code == 500
    assert response.json()["errors"][0]["code"] == "RESPONSE_REDACTION_FAILED"
    assert "host-secret" not in response.text
    assert "credential_ref" not in response.text


def test_websocket_requires_session_and_same_origin() -> None:
    app = create_demo_app(config(), FakeBackend())
    unauthenticated = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as unauthenticated_error:
        with unauthenticated.websocket_connect("/api/v1/events", headers={"Origin": ORIGIN}):
            pass
    assert unauthenticated_error.value.code == 4401

    client, _ = authenticated_client()
    with pytest.raises(WebSocketDisconnect) as origin_error:
        with client.websocket_connect(
            "/api/v1/events", headers={"Origin": "http://evil.invalid"}
        ):
            pass
    assert origin_error.value.code == 4403

    with client.websocket_connect("/api/v1/events", headers={"Origin": ORIGIN}) as websocket:
        event = websocket.receive_json()
        assert event["event"] == "projection.snapshot"
        assert event["data"]["performance_scope"] == "broker_simulation"


def test_websocket_keeps_streaming_projection_revisions_on_one_connection() -> None:
    backend = FakeBackend()
    client, _ = authenticated_client(backend)

    with client.websocket_connect(
        "/api/v1/events",
        headers={"Origin": ORIGIN},
    ) as websocket:
        assert websocket.receive_json()["data"]["revision"] == 4
        backend.projection_revision = 5
        assert websocket.receive_json()["data"]["revision"] == 5


@dataclass
class RecordingTransport:
    calls: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)

    def request(self, endpoint: str, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((endpoint, operation, payload))
        return {
            "contract_version": 1,
            "gateway": payload["gateway"],
            "run_digest": payload["run_digest"],
            "operation": operation,
            "state": "accepted",
            "receipt_digest": digest(operation),
        }


def test_per_run_client_is_loopback_bound_and_has_no_generic_or_trading_methods() -> None:
    transport = RecordingTransport()
    client = BrokerSimulationRunClient(
        RunClientBinding("XTP", digest("run:XTP"), "http://127.0.0.1:18765"),
        transport,
    )

    receipt = client.read_status()
    client.prepare_campaign(digest("campaign"), digest("candidate"), "request-00000001")
    client.pause_campaign(digest("campaign"), "request-00000002")
    client.emergency_stop("request-00000003")

    assert receipt["gateway"] == "XTP"
    assert {call[1] for call in transport.calls} == {
        "run.status.v1",
        "run.prepare_campaign.v1",
        "run.pause_campaign.v1",
        "run.emergency_stop.v1",
    }
    for forbidden_method in (
        "call",
        "invoke",
        "request",
        "send_order",
        "cancel_order",
        "set_risk",
        "apply_lifecycle",
    ):
        assert not hasattr(client, forbidden_method)

    with pytest.raises(ValueError, match="RUN_CLIENT_LOOPBACK_REQUIRED"):
        RunClientBinding("TORA", digest("run:TORA"), "http://192.0.2.10:18766")
