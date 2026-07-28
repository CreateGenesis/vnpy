from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi.testclient import TestClient

from vnpy.demo_web.app import DemoWebConfig, create_demo_app


SESSION = "s" * 48
CSRF = "c" * 48
ORIGIN = "http://127.0.0.1:8765"


@dataclass
class Backend:
    def readiness(self) -> dict[str, Any]:
        return {"state": "unconfigured"}

    def projection(self) -> dict[str, Any]:
        return {"revision": 0, "state": "unconfigured"}

    def start_campaign(self, command: dict[str, Any]) -> dict[str, Any]:
        return command

    def pause_campaign(self, campaign_id: str) -> dict[str, Any]:
        return {"campaign_id": campaign_id}

    def emergency_stop(self) -> dict[str, Any]:
        return {"state": "stopped"}

    def evidence(self, campaign_id: str) -> dict[str, Any]:
        return {"campaign_id": campaign_id}


@dataclass
class Operations:
    calls: list[tuple[str, Any]] = field(default_factory=list)

    def system(self) -> dict[str, Any]:
        return {"revision": 3, "configuration": {"state": "editing"}, "actions": []}

    def configuration_draft(self) -> dict[str, Any]:
        return {"revision": 1, "sections": {}, "secret_status": {}}

    def update_configuration(self, command: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("update", command))
        return {
            "revision": 2,
            "sections": command["sections"],
            "secret_status": {key: {"configured": True} for key in command["secret_updates"]},
        }

    def test_configuration(self, command: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("test", command))
        return {"revision": command["expected_revision"], "passed": True, "expires_at_ms": 999}

    def activate_configuration(self, command: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("activate", command))
        return {"revision": command["expected_revision"], "state": "active"}

    def control_service(self, service: str, action: str, command: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("service", (service, action, command)))
        return {"revision": command["expected_revision"] + 1, "service": service, "state": "ready"}


def client(operations: Operations) -> TestClient:
    config = DemoWebConfig("127.0.0.1", 8765, ORIGIN, SESSION, CSRF)
    selected = TestClient(create_demo_app(config, Backend(), operations=operations))
    selected.cookies.set("auto_trade_host_session", SESSION)
    return selected


def headers() -> dict[str, str]:
    return {"Origin": ORIGIN, "X-CSRF-Token": CSRF}


def test_configuration_routes_keep_secret_write_only_and_expose_system_projection() -> None:
    operations = Operations()
    api = client(operations)
    secret = "must-never-return"
    body = {
        "expected_revision": 1,
        "sections": {"ports": {"web": 8765}},
        "secret_updates": {"rqdata.api_key": secret},
        "clear_secrets": [],
    }

    assert api.get("/api/v1/system").json()["data"]["configuration"]["state"] == "editing"
    assert api.get("/api/v1/config/draft").status_code == 200
    updated = api.put("/api/v1/config/draft", json=body, headers=headers())
    assert updated.status_code == 200
    assert secret not in updated.text
    assert updated.json()["data"]["secret_status"]["rqdata.api_key"] == {"configured": True}
    assert operations.calls[0] == ("update", body)


def test_section_activation_and_fixed_service_routes_require_write_guards() -> None:
    operations = Operations()
    api = client(operations)
    command = {"section": "ports", "expected_revision": 2, "idempotency_key": "section-test-0001"}

    assert api.post("/api/v1/config/draft/test", json=command).status_code == 403
    assert api.post("/api/v1/config/draft/test", json=command, headers=headers()).status_code == 202
    assert api.post(
        "/api/v1/config/draft/activate",
        json={"expected_revision": 2, "idempotency_key": "activate-config-0001"},
        headers=headers(),
    ).status_code == 202
    assert api.post(
        "/api/v1/services/research/start",
        json={"expected_revision": 3, "idempotency_key": "service-start-0001"},
        headers=headers(),
    ).status_code == 202
    assert api.post(
        "/api/v1/services/web/start",
        json={"expected_revision": 3, "idempotency_key": "service-start-0002"},
        headers=headers(),
    ).status_code == 422
    assert api.post(
        "/api/v1/services/research/run-command",
        json={"expected_revision": 3, "idempotency_key": "service-start-0003"},
        headers=headers(),
    ).status_code == 422
