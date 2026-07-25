from __future__ import annotations

import pytest

from vnpy.agent_console.model_candidate import ModelCandidateViewState


def response() -> dict:
    return {
        "contract_version": 1,
        "result": {
            "candidate_digest": "blake3:" + "a" * 64,
            "candidate_id": "candidate-a-share-v1",
            "revision": 1,
            "family": "mathematical",
            "state": "quarantined",
            "author_lineage": {
                "author_id": "worker-7",
                "ancestors": ["master-1"],
            },
            "validation_status": "quarantined",
            "validation_findings": [
                {"code": "FORBIDDEN_AUTHORITY", "message": "blocked"}
            ],
            "resources": {
                "cpu_time_ms": 1000,
                "memory_bytes": 67108864,
                "output_bytes": 1024,
                "threads": 1,
                "network": False,
            },
            "expires_at_ms": 2_000_000_000_000,
            "training_started": False,
            "runtime_loaded": False,
            "broker_authority": False,
            "order_authority": False,
        },
        "evidence_refs": ["blake3:" + "b" * 64],
        "permitted_next_actions": ["model.candidate.revise"],
    }


def test_candidate_projection_exposes_lineage_quarantine_resources_and_next_action() -> None:
    view = ModelCandidateViewState.from_cli_response(response())
    assert view.family == "mathematical"
    assert view.author_identity == "worker-7"
    assert view.author_ancestors == ("master-1",)
    assert view.quarantine_codes == ("FORBIDDEN_AUTHORITY",)
    assert view.resource_summary["network"] is False
    assert view.permitted_next_actions == ("model.candidate.revise",)


@pytest.mark.parametrize(
    "field",
    ["training_started", "runtime_loaded", "broker_authority", "order_authority"],
)
def test_candidate_projection_rejects_positive_execution_or_authority_claim(field: str) -> None:
    payload = response()
    payload["result"][field] = True
    with pytest.raises(ValueError, match="authority escalation"):
        ModelCandidateViewState.from_cli_response(payload)
