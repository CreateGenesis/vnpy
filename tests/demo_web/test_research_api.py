from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

from fastapi.testclient import TestClient

from vnpy.demo_web.app import DemoWebConfig, create_demo_app


SESSION = "s" * 48
CSRF = "c" * 48
ORIGIN = "http://127.0.0.1:8765"
TASK_ID = "7c216598-1144-4b21-8714-a711c66f9f31"
PROPOSAL_ID = "8c216598-1144-4b21-8714-a711c66f9f31"


def digest(label: str) -> str:
    return f"sha256:{sha256(label.encode()).hexdigest()}"


def task(state: str = "queued") -> dict[str, Any]:
    return {
        "contract_version": 1,
        "task_id": TASK_ID,
        "task_digest": digest("task"),
        "source": "side_master_proposal",
        "source_digest": digest("proposal"),
        "operator_confirmation_digest": digest("confirmation"),
        "mission_id": "research-mission-1",
        "objective": "Evaluate a lower-turnover drawdown factor",
        "constraints": ["research_only", "no_trading_authority"],
        "data_references": [digest("snapshot")],
        "budget_digest": digest("budget"),
        "author_lineage_digest": digest("lineage"),
        "priority": "routine",
        "created_at_ms": 1_000,
        "expires_at_ms": 61_000,
        "deduplication_key": "research-task-fixture-0001",
        "not_before_boundary": "campaign_terminal",
        "state": state,
    }


@dataclass
class Backend:
    def readiness(self) -> dict[str, Any]:
        return {"state": "ready"}

    def projection(self) -> dict[str, Any]:
        return {"revision": 4, "state": "ready"}

    def start_campaign(self, command: dict[str, Any]) -> dict[str, Any]:
        return command

    def pause_campaign(self, campaign_id: str) -> dict[str, Any]:
        return {"campaign_id": campaign_id, "state": "paused"}

    def emergency_stop(self) -> dict[str, Any]:
        return {"state": "stopped"}

    def evidence(self, campaign_id: str) -> dict[str, Any]:
        return {"campaign_id": campaign_id, "state": "retained"}


@dataclass
class Research:
    calls: list[tuple[str, Any]] = field(default_factory=list)
    leak: bool = False

    def list_tasks(self) -> dict[str, Any]:
        result = {"revision": 4, "tasks": [task()]}
        if self.leak:
            result["credential"] = "must-never-return"
        return result

    def create_task(self, command: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("create", command))
        return {"revision": 5, "task": task()}

    def cancel_task(self, task_id: str, command: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("cancel", (task_id, command)))
        return {"revision": 6, "task": task("cancelled")}

    def projection(self) -> dict[str, Any]:
        return self.list_tasks()


class Guidance:
    def send_message(self, _command: dict[str, Any]) -> dict[str, Any]:
        return {"state": "completed", "reply": None, "proposal": None}

    def decide_proposal(
        self,
        proposal_id: str,
        decision: str,
        command: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "proposal": {
                "proposal_id": proposal_id,
                "proposal_digest": command["expected_proposal_digest"],
                "state": "confirmed" if decision == "confirm" else "rejected",
            },
            "guidance": None,
            "research_task": task() if decision == "confirm" else None,
            "idempotency_key": command["idempotency_key"],
            "decision_digest": digest(f"decision:{decision}"),
        }


def client(research: Research, *, guidance: Guidance | None = None) -> TestClient:
    config = DemoWebConfig("127.0.0.1", 8765, ORIGIN, SESSION, CSRF)
    selected = TestClient(
        create_demo_app(
            config,
            Backend(),
            guidance,
            research=research,
        )
    )
    selected.cookies.set("auto_trade_host_session", SESSION)
    return selected


def headers() -> dict[str, str]:
    return {"Origin": ORIGIN, "X-CSRF-Token": CSRF}


def create_body() -> dict[str, Any]:
    return {
        "mission_id": "research-mission-1",
        "objective": "Evaluate a lower-turnover drawdown factor",
        "constraints": ["research_only"],
        "data_references": [digest("snapshot")],
        "priority": "routine",
        "expires_at_ms": 61_000,
        "idempotency_key": "operator-research-task-0001",
    }


def cancel_body() -> dict[str, str]:
    return {
        "expected_task_digest": digest("task"),
        "idempotency_key": "cancel-research-task-0001",
    }


def test_research_task_list_create_and_cancel_are_explicit_and_guarded() -> None:
    research = Research()
    api = client(research)

    listed = api.get("/api/v1/research/tasks")
    assert listed.status_code == 200
    assert listed.json()["data"]["tasks"] == [task()]
    assert api.post("/api/v1/research/tasks", json=create_body()).status_code == 403

    created = api.post(
        "/api/v1/research/tasks", json=create_body(), headers=headers()
    )
    cancelled = api.post(
        f"/api/v1/research/tasks/{TASK_ID}/cancel",
        json=cancel_body(),
        headers=headers(),
    )

    assert created.status_code == cancelled.status_code == 202
    assert created.json()["data"]["task"]["state"] == "queued"
    assert cancelled.json()["data"]["task"]["state"] == "cancelled"
    assert research.calls == [
        ("create", create_body()),
        ("cancel", (TASK_ID, cancel_body())),
    ]


def test_confirmed_proposal_projects_the_created_research_task() -> None:
    api = client(Research(), guidance=Guidance())
    response = api.post(
        f"/api/v1/chat/proposals/{PROPOSAL_ID}/confirm",
        json={
            "expected_proposal_digest": digest("proposal"),
            "idempotency_key": "confirm-proposal-task-0001",
        },
        headers=headers(),
    )

    assert response.status_code == 200
    assert response.json()["data"]["research_task"] == task()


def test_research_responses_fail_closed_on_secrets_and_trading_fields() -> None:
    api = client(Research(leak=True))
    response = api.get("/api/v1/research/tasks")

    assert response.status_code == 500
    assert response.json()["errors"][0]["code"] == "RESPONSE_REDACTION_FAILED"
    assert "must-never-return" not in response.text
    assert "credential" not in response.text


def test_event_stream_includes_a_redacted_research_projection() -> None:
    api = client(Research())
    with api.websocket_connect("/api/v1/events", headers={"Origin": ORIGIN}) as socket:
        system_event = socket.receive_json()
        research_event = socket.receive_json()

    assert system_event["event"] == "projection.snapshot"
    assert research_event == {
        "event": "research.snapshot",
        "data": {"revision": 4, "tasks": [task()]},
    }

