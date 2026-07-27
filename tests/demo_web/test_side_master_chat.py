from __future__ import annotations

from base64 import b64encode
from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any

from blake3 import blake3
from fastapi.testclient import TestClient

from vnpy.demo_web.app import DemoWebConfig, create_demo_app
from vnpy.demo_web.guidance import (
    GuidanceClientBinding,
    SideMasterGuidanceClient,
)


SESSION_TOKEN = "s" * 48
CSRF_TOKEN = "c" * 48
ORIGIN = "http://127.0.0.1:8765"
PROPOSAL_ID = "1a216598-1144-4b21-8714-a711c66f9f31"


def digest(label: str) -> str:
    return f"sha256:{sha256(label.encode()).hexdigest()}"


def dynamic_text(value: str) -> dict[str, Any]:
    encoded = value.encode()
    return {
        "media_type": "text/plain; charset=utf-8",
        "body": value,
        "canonical_body_base64": b64encode(encoded).decode(),
        "body_digest": f"blake3:{blake3(encoded).hexdigest()}",
    }


def proposal(state: str = "pending") -> dict[str, Any]:
    return {
        "contract_version": 1,
        "entity_type": "side_master_approval_proposal",
        "proposal_id": PROPOSAL_ID,
        "session_id": "side-session-1",
        "mission_id": "research-mission-1",
        "side_master_identity": "side-master:demo",
        "source_turn_digest": digest("source-turn"),
        "material_direction_change": True,
        "interpretation": "Prefer drawdown stability over turnover",
        "proposed_guidance": "Research lower-turnover drawdown controls",
        "provider_outcome": "certain",
        "state": state,
        "created_at_ms": 1_000,
        "expires_at_ms": 61_000,
        "proposal_digest": digest("proposal"),
    }


def chat_result(
    *,
    content: str,
    state: str = "completed",
    proposal_value: dict[str, Any] | None = None,
) -> dict[str, Any]:
    uncertain = state == "uncertain"
    return {
        "contract_version": 1,
        "entity_type": "demo_side_master_chat_result",
        "session_id": "side-session-1",
        "mission_id": "research-mission-1",
        "state": state,
        "reply": None if uncertain else dynamic_text(content),
        "proposal": proposal_value,
        "provider_outcome": "uncertain" if uncertain else "certain",
        "result_digest": digest(f"result:{content}:{state}"),
    }


@dataclass
class FakeBackend:
    def readiness(self) -> dict[str, Any]:
        return {"state": "ready"}

    def projection(self) -> dict[str, Any]:
        return {"revision": 1, "state": "ready"}

    def start_campaign(self, command: dict[str, Any]) -> dict[str, Any]:
        return {"state": "starting"}

    def pause_campaign(self, campaign_id: str) -> dict[str, Any]:
        return {"campaign_id": campaign_id, "state": "paused"}

    def emergency_stop(self) -> dict[str, Any]:
        return {"state": "stopped"}

    def evidence(self, campaign_id: str) -> dict[str, Any]:
        return {"campaign_id": campaign_id, "state": "retained"}


class RecordingGuidanceTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.effects: list[tuple[str, str]] = []
        self._retained: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}

    def request(
        self,
        endpoint: str,
        operation: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append((endpoint, operation, payload))
        idempotency_key = payload["idempotency_key"]
        cache_key = (operation, idempotency_key)
        request_digest = digest(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        retained = self._retained.get(cache_key)
        if retained is not None:
            if retained[0] != request_digest:
                raise ValueError("GUIDANCE_IDEMPOTENCY_CONFLICT")
            return retained[1]

        if operation == "demo.side_master.chat.v1":
            content = payload["content"]
            self.effects.append((operation, idempotency_key))
            if content == "ordinary":
                result = chat_result(content="Continue the current research direction.")
            elif content == "uncertain":
                result = chat_result(content="", state="uncertain")
            elif content == "leak":
                result = {
                    **chat_result(content="unsafe"),
                    "order_request": {"account_id": "broker-secret"},
                }
            else:
                result = chat_result(
                    content="I propose a future research direction change.",
                    proposal_value=proposal(),
                )
        elif operation == "demo.side_master.proposal.decide.v1":
            self.effects.append((operation, idempotency_key))
            decision = payload["decision"]
            decided = proposal("confirmed" if decision == "confirm" else "rejected")
            guidance = None
            if decision == "confirm":
                guidance = {
                    "contract_version": 1,
                    "entity_type": "confirmed_future_research_guidance",
                    "guidance_id": "2a216598-1144-4b21-8714-a711c66f9f31",
                    "proposal_id": PROPOSAL_ID,
                    "proposal_digest": digest("proposal"),
                    "mission_id": "research-mission-1",
                    "guidance": "Research lower-turnover drawdown controls",
                    "operator_identity_digest": digest("operator"),
                    "confirmed_at_ms": 1_000,
                    "scope": "future_research_only",
                    "not_before_safe_boundary_revision": 12,
                    "delivery_id": "3a216598-1144-4b21-8714-a711c66f9f31",
                    "active_campaign_immutable": True,
                    "signer_id": "demo-guidance-signer",
                    "verifying_key": "a" * 64,
                    "guidance_digest": digest("guidance"),
                    "signature": "b" * 128,
                }
            result = {
                "proposal": decided,
                "guidance": guidance,
                "idempotency_key": idempotency_key,
                "decision_digest": digest(f"decision:{decision}"),
            }
        else:
            raise AssertionError(f"unexpected operation: {operation}")

        self._retained[cache_key] = (request_digest, result)
        return result


def app_config() -> DemoWebConfig:
    return DemoWebConfig(
        bind_host="127.0.0.1",
        port=8765,
        allowed_origin=ORIGIN,
        session_token=SESSION_TOKEN,
        csrf_token=CSRF_TOKEN,
    )


def guidance_client(
    transport: RecordingGuidanceTransport,
) -> SideMasterGuidanceClient:
    return SideMasterGuidanceClient(
        GuidanceClientBinding(
            endpoint="tcp://127.0.0.1:18770",
            operator_identity_digest=digest("operator"),
        ),
        transport,
        active_campaign=lambda: True,
        next_safe_boundary_revision=lambda: 12,
        clock_ms=lambda: 1_000,
        proposal_ttl_ms=60_000,
    )


def client_and_transport() -> tuple[TestClient, RecordingGuidanceTransport]:
    transport = RecordingGuidanceTransport()
    app = create_demo_app(app_config(), FakeBackend(), guidance_client(transport))
    client = TestClient(app)
    client.cookies.set("auto_trade_host_session", SESSION_TOKEN)
    return client, transport


def write_headers() -> dict[str, str]:
    return {"Origin": ORIGIN, "X-CSRF-Token": CSRF_TOKEN}


def chat_body(content: str, key: str) -> dict[str, str]:
    return {
        "session_id": "side-session-1",
        "mission_id": "research-mission-1",
        "content": content,
        "idempotency_key": key,
    }


def decision_body(key: str) -> dict[str, str]:
    return {
        "expected_proposal_digest": digest("proposal"),
        "idempotency_key": key,
    }


def test_chat_and_proposal_decisions_require_session_origin_and_csrf() -> None:
    transport = RecordingGuidanceTransport()
    app = create_demo_app(app_config(), FakeBackend(), guidance_client(transport))
    client = TestClient(app)
    body = chat_body("ordinary", "chat-message-000001")

    assert client.post("/api/v1/chat/messages", json=body).status_code == 401
    client.cookies.set("auto_trade_host_session", SESSION_TOKEN)
    assert client.post("/api/v1/chat/messages", json=body).status_code == 403
    assert client.post(
        "/api/v1/chat/messages",
        json=body,
        headers={"Origin": "http://evil.invalid", "X-CSRF-Token": CSRF_TOKEN},
    ).status_code == 403
    assert client.post(
        "/api/v1/chat/messages", json=body, headers=write_headers()
    ).status_code == 202

    decision_path = f"/api/v1/chat/proposals/{PROPOSAL_ID}/confirm"
    assert client.post(decision_path, json=decision_body("decision-00000001")).status_code == 403
    assert client.post(
        decision_path,
        json=decision_body("decision-00000001"),
        headers=write_headers(),
    ).status_code == 200


def test_chat_and_decision_are_replay_safe_and_input_bound() -> None:
    client, transport = client_and_transport()
    body = chat_body("material", "chat-message-000002")

    first = client.post("/api/v1/chat/messages", json=body, headers=write_headers())
    replay = client.post("/api/v1/chat/messages", json=body, headers=write_headers())
    assert first.status_code == replay.status_code == 202
    assert first.json()["data"] == replay.json()["data"]

    path = f"/api/v1/chat/proposals/{PROPOSAL_ID}/confirm"
    decision = decision_body("decision-00000002")
    confirmed = client.post(path, json=decision, headers=write_headers())
    confirmed_replay = client.post(path, json=decision, headers=write_headers())
    assert confirmed.status_code == confirmed_replay.status_code == 200
    assert confirmed.json()["data"] == confirmed_replay.json()["data"]
    assert transport.effects.count(("demo.side_master.chat.v1", body["idempotency_key"])) == 1
    assert transport.effects.count(("demo.side_master.proposal.decide.v1", decision["idempotency_key"])) == 1

    conflict = client.post(
        "/api/v1/chat/messages",
        json={**body, "content": "ordinary"},
        headers=write_headers(),
    )
    assert conflict.status_code == 500
    assert conflict.json()["errors"][0]["code"] == "BACKEND_OPERATION_FAILED"


def test_ordinary_reply_and_uncertain_provider_create_no_proposal_or_guidance() -> None:
    client, _ = client_and_transport()

    ordinary = client.post(
        "/api/v1/chat/messages",
        json=chat_body("ordinary", "chat-message-000003"),
        headers=write_headers(),
    ).json()["data"]
    uncertain = client.post(
        "/api/v1/chat/messages",
        json=chat_body("uncertain", "chat-message-000004"),
        headers=write_headers(),
    ).json()["data"]

    assert ordinary["state"] == "completed"
    assert ordinary["proposal"] is None
    assert uncertain["state"] == "uncertain"
    assert uncertain["provider_outcome"] == "uncertain"
    assert uncertain["reply"] is None
    assert uncertain["proposal"] is None
    assert "guidance" not in uncertain


def test_confirmation_is_future_research_only_and_rejection_creates_no_guidance() -> None:
    client, transport = client_and_transport()
    material = client.post(
        "/api/v1/chat/messages",
        json=chat_body("material", "chat-message-000005"),
        headers=write_headers(),
    ).json()["data"]
    assert material["proposal"]["state"] == "pending"

    confirmed = client.post(
        f"/api/v1/chat/proposals/{PROPOSAL_ID}/confirm",
        json=decision_body("decision-00000003"),
        headers=write_headers(),
    ).json()["data"]
    rejected = client.post(
        f"/api/v1/chat/proposals/{PROPOSAL_ID}/reject",
        json=decision_body("decision-00000004"),
        headers=write_headers(),
    ).json()["data"]

    assert confirmed["proposal"]["state"] == "confirmed"
    assert confirmed["guidance"]["scope"] == "future_research_only"
    assert confirmed["guidance"]["active_campaign_immutable"] is True
    assert rejected["proposal"]["state"] == "rejected"
    assert rejected["guidance"] is None
    decision_payloads = [
        payload
        for _, operation, payload in transport.calls
        if operation == "demo.side_master.proposal.decide.v1"
    ]
    assert all(payload["active_campaign"] is True for payload in decision_payloads)
    assert all(payload["next_safe_boundary_revision"] == 12 for payload in decision_payloads)


def test_guidance_response_redaction_fails_closed_on_account_or_trading_fields() -> None:
    client, _ = client_and_transport()
    response = client.post(
        "/api/v1/chat/messages",
        json=chat_body("leak", "chat-message-000006"),
        headers=write_headers(),
    )

    assert response.status_code == 500
    assert "broker-secret" not in response.text
    assert "account_id" not in response.text
    assert "order_request" not in response.text


def test_guidance_surface_has_no_main_master_generic_rpc_or_trading_capability() -> None:
    client, transport = client_and_transport()
    guidance = guidance_client(transport)
    route_paths = {route.path for route in client.app.routes if route.path.startswith("/api/")}

    assert "/api/v1/chat/messages" in route_paths
    assert "/api/v1/chat/proposals/{proposal_id}/{decision}" in route_paths
    for forbidden_route in (
        "/api/v1/master/chat",
        "/api/v1/rpc",
        "/api/v1/orders",
        "/api/v1/orders/123/cancel",
        "/api/v1/risk",
        "/api/v1/lifecycle",
    ):
        assert client.post(forbidden_route, headers=write_headers()).status_code == 404
    for forbidden_method in (
        "request",
        "call",
        "invoke",
        "send_order",
        "cancel_order",
        "set_risk",
        "apply_lifecycle",
        "chat_with_main_master",
    ):
        assert not hasattr(guidance, forbidden_method)
