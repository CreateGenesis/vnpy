from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from vnpy.demo_web.app import DemoWebConfig, create_demo_app
from vnpy.demo_web.runtime import ConcreteDemoBackend


SESSION = "s" * 48
CSRF = "c" * 48
ORIGIN = "http://127.0.0.1:8765"


def digest(label: str) -> str:
    return f"sha256:{sha256(label.encode()).hexdigest()}"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def backend(tmp_path: Path) -> ConcreteDemoBackend:
    return ConcreteDemoBackend(tmp_path, None, {}, False)


def test_models_projection_allowlists_candidate_and_run_fields(tmp_path: Path) -> None:
    state = tmp_path / ".demo-state"
    write_json(
        state / "ready-candidate-v2.json",
        {
            "state": "ready",
            "candidate_digest": digest("candidate"),
            "package_digest": digest("package"),
            "revision": 4,
            "signature": "not-projected",
        },
    )
    write_json(
        state / "candidate-family.json",
        {"candidate_digest": digest("candidate"), "family": "lasso"},
    )
    write_json(
        state / "model-runs" / "run-001.projection.json",
        {
            "run_id": "run-001",
            "family": "lasso",
            "state": "evaluated",
            "progress_percent": 100,
            "artifact_digest": digest("artifact"),
            "error_code": None,
        },
    )

    assert backend(tmp_path).models() == {
        "revision": 4,
        "current_candidate": {
            "state": "ready",
            "candidate_digest": digest("candidate"),
            "package_digest": digest("package"),
            "family": "lasso",
            "publication_revision": 4,
        },
        "runs": [
            {
                "run_id": "run-001",
                "family": "lasso",
                "state": "evaluated",
                "progress_percent": 100,
                "artifact_digest": digest("artifact"),
                "error_code": None,
            }
        ],
    }


def test_models_projection_rejects_candidate_family_identity_drift(tmp_path: Path) -> None:
    state = tmp_path / ".demo-state"
    write_json(
        state / "ready-candidate-v2.json",
        {
            "state": "ready",
            "candidate_digest": digest("candidate"),
            "package_digest": digest("package"),
            "revision": 1,
        },
    )
    write_json(
        state / "candidate-family.json",
        {"candidate_digest": digest("other-candidate"), "family": "mlp"},
    )

    with pytest.raises(ValueError, match="DEMO_CANDIDATE_FAMILY_INVALID"):
        backend(tmp_path).models()


def test_models_projection_rejects_non_allowlisted_run_fields(tmp_path: Path) -> None:
    write_json(
        tmp_path / ".demo-state" / "model-runs" / "run-001.projection.json",
        {
            "run_id": "run-001",
            "family": "rule",
            "state": "running",
            "progress_percent": 25,
            "artifact_digest": None,
            "error_code": None,
            "credential": "must-not-project",
        },
    )

    with pytest.raises(ValueError, match="MODEL_RUN_PROJECTION_INVALID"):
        backend(tmp_path).models()


@dataclass
class ApiBackend:
    model_projection: dict[str, Any]

    def readiness(self) -> dict[str, Any]:
        return {"state": "blocked", "ready": False}

    def projection(self) -> dict[str, Any]:
        return {"revision": 0}

    def models(self) -> dict[str, Any]:
        return self.model_projection

    def start_campaign(self, command: dict[str, Any]) -> dict[str, Any]:
        return command

    def pause_campaign(self, campaign_id: str) -> dict[str, Any]:
        return {"campaign_id": campaign_id}

    def emergency_stop(self) -> dict[str, Any]:
        return {"state": "stopped"}

    def evidence(self, campaign_id: str) -> dict[str, Any]:
        return {"campaign_id": campaign_id}


def test_models_route_returns_the_backend_projection() -> None:
    expected = {"revision": 3, "current_candidate": None, "runs": []}
    config = DemoWebConfig("127.0.0.1", 8765, ORIGIN, SESSION, CSRF)
    client = TestClient(create_demo_app(config, ApiBackend(expected)))
    client.cookies.set("auto_trade_host_session", SESSION)

    response = client.get("/api/v1/models")

    assert response.status_code == 200
    assert response.json()["data"] == expected
