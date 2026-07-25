from typing import Any

import pytest

from vnpy.agent_console.engine import AgentConsoleEngine
from vnpy.agent_console.memory import MemoryProjectionView
from vnpy.agent_console.models import compute_projection_digest


SECTIONS = (
    "task", "workflow", "workers", "qualifications", "skills", "tools_and_cli",
    "pattern_similarity", "solidification", "stateless_audit", "memory", "resources",
    "recovery", "model_pipeline", "vnpy_authority",
)


def digest(index: int) -> str:
    return f"blake3:{index:064x}"


def projection(revision: int, previous: str | None = None) -> dict[str, Any]:
    def section(name: str, index: int) -> dict[str, Any]:
        summary: dict[str, Any] = {"identity": name, "revision": revision}
        if name == "memory":
            summary = {
                "working_revision": 7,
                "episode_health": "append_only_verified",
                "temporal_validity": "historical_cutoff_applied",
                "conflict_count": 2,
                "procedure_approval": "stateless_audit_required",
                "proposal_count": 3,
                "retrieval_cutoff_ms": 1_700,
                "consolidation_state": "candidate_only",
                "budget_remaining_units": 42,
                "memory_health": "ready",
            }
        return {
            "source_revision": revision,
            "source_digest": digest(index),
            "state": "ready",
            "certainty": "certain",
            "freshness": "fresh",
            "summary": summary,
            "evidence_refs": [digest(index + 100)],
            "permitted_next_actions": [f"{name}.inspect"],
            "updated_at_ms": 1_000 + revision,
            "stale": False,
        }

    value: dict[str, Any] = {
        "contract_version": 1,
        "entity_type": "unified_workflow_projection",
        "projection_id": "memory-console",
        "projection_revision": revision,
        "projection_digest": digest(999),
        "correlation_id": f"correlation-{revision}",
        "authoritative_source_revisions": {name: revision for name in SECTIONS},
        "created_at_ms": 1_000 + revision,
        "expires_at_ms": 5_000 + revision,
        **{name: section(name, index + 1) for index, name in enumerate(SECTIONS)},
    }
    if previous is not None:
        value["previous_projection_digest"] = previous
    value["projection_digest"] = compute_projection_digest(value)
    return value


def test_master_and_vnpy_consume_identical_memory_revision_and_bounded_status() -> None:
    value = projection(1)
    console = AgentConsoleEngine()
    assert console.apply_projection(value, received_at_ms=1_100, rendered_at_ms=1_200).status == (
        "applied"
    )
    view = MemoryProjectionView.from_unified(console.state.unified_projection)
    master_payload = view.as_master_payload()
    assert master_payload["source_revision"] == value["memory"]["source_revision"]
    assert master_payload["source_digest"] == value["memory"]["source_digest"]
    for key, item in value["memory"]["summary"].items():
        assert master_payload[key] == item
    assert master_payload["permitted_next_actions"] == value["memory"]["permitted_next_actions"]
    assert view.conflict_count == 2
    assert view.procedure_approval == "stateless_audit_required"
    assert view.budget_remaining_units == 42


def test_stale_or_secret_memory_projection_is_rejected_without_overwrite() -> None:
    first = projection(2)
    console = AgentConsoleEngine()
    assert console.apply_projection(first).status == "applied"
    stale = projection(1, first["projection_digest"])
    assert console.apply_projection(stale).error_code == "STALE_PROJECTION"
    secret = projection(3, first["projection_digest"])
    secret["memory"]["summary"]["secret"] = "bearer secret-canary"
    secret["projection_digest"] = compute_projection_digest(secret)
    assert console.apply_projection(secret).error_code == "REDACTION_FAILED"
    with pytest.raises(ValueError, match="REDACTION_FAILED"):
        MemoryProjectionView.from_unified(secret)
    assert console.state.unified_projection_digest == first["projection_digest"]
