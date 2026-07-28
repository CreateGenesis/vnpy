from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from fastapi.testclient import TestClient

from vnpy.demo_web.app import DemoWebConfig, create_demo_app
from vnpy.demo_web.security import BootstrapSessionManager


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


def config() -> DemoWebConfig:
    return DemoWebConfig(
        bind_host="127.0.0.1",
        port=8765,
        allowed_origin=ORIGIN,
        session_token="s" * 48,
        csrf_token="c" * 48,
    )


def test_one_time_fragment_exchange_binds_cookie_csrf_origin_and_operator() -> None:
    operator = ["S-1-5-21-current"]
    security = BootstrapSessionManager(
        allowed_origin=ORIGIN,
        expected_operator_sid=operator[0],
        current_operator_sid=lambda: operator[0],
        session_token="s" * 48,
        csrf_token="c" * 48,
    )
    fragment_token = security.issue_fragment_token()
    app = create_demo_app(config(), Backend(), security=security)
    client = TestClient(app)

    root = client.get("/")
    assert root.status_code == 200
    assert "auto_trade_host_session" not in root.headers.get("set-cookie", "")
    assert root.headers["cache-control"] == "no-store"
    assert client.post(
        "/api/v1/bootstrap/exchange",
        json={"fragment_token": fragment_token},
        headers={"Origin": "http://evil.invalid"},
    ).status_code == 403

    exchanged = client.post(
        "/api/v1/bootstrap/exchange",
        json={"fragment_token": fragment_token},
        headers={"Origin": ORIGIN},
    )
    assert exchanged.status_code == 200
    assert exchanged.json()["data"]["csrf_token"] == "c" * 48
    assert "HttpOnly" in exchanged.headers["set-cookie"]
    assert "SameSite=strict" in exchanged.headers["set-cookie"]
    assert client.post(
        "/api/v1/bootstrap/exchange",
        json={"fragment_token": fragment_token},
        headers={"Origin": ORIGIN},
    ).status_code == 409
    operator_projection = client.get("/api/v1/operator")
    assert operator_projection.status_code == 200
    assert operator_projection.json()["data"] == {
        "operator_identity_digest": "sha256:" + sha256(operator[0].encode()).hexdigest()
    }


def test_exchange_fails_if_windows_operator_changes_before_use() -> None:
    operator = ["S-1-5-21-current"]
    security = BootstrapSessionManager(
        allowed_origin=ORIGIN,
        expected_operator_sid=operator[0],
        current_operator_sid=lambda: operator[0],
        session_token="s" * 48,
        csrf_token="c" * 48,
    )
    token = security.issue_fragment_token()
    operator[0] = "S-1-5-21-other"

    result = security.exchange(token, origin=ORIGIN)

    assert result.code == "BOOTSTRAP_OPERATOR_MISMATCH"
    assert not result.accepted
