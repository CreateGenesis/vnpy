from typing import Any

import pytest

from vnpy.agent_console.engine import AgentConsoleEngine
from vnpy.agent_console.models import compute_projection_digest
from vnpy.agent_console.workflow import WorkflowProjectionView


SECTIONS = (
    "task",
    "workflow",
    "workers",
    "qualifications",
    "skills",
    "tools_and_cli",
    "pattern_similarity",
    "solidification",
    "stateless_audit",
    "memory",
    "resources",
    "recovery",
    "model_pipeline",
    "vnpy_authority",
)


def digest(index: int) -> str:
    return f"blake3:{index:064x}"


def projection(revision: int, previous: str | None = None) -> dict[str, Any]:
    section = lambda name, index: {
        "source_revision": revision,
        "source_digest": digest(index),
        "state": "ready",
        "certainty": "certain",
        "freshness": "fresh",
        "summary": {"identity": name, "revision": revision},
        "evidence_refs": [digest(index + 100)],
        "permitted_next_actions": [f"{name}.inspect"],
        "updated_at_ms": 1_000 + revision,
        "stale": False,
    }
    value: dict[str, Any] = {
        "contract_version": 1,
        "entity_type": "unified_workflow_projection",
        "projection_id": "workflow-console",
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


def test_master_and_vnpy_consume_identical_dynamic_sections_and_ack_within_two_seconds() -> None:
    value = projection(1)
    console = AgentConsoleEngine()
    ack = console.apply_projection(value, received_at_ms=1_100, rendered_at_ms=1_500)
    view = WorkflowProjectionView.from_unified(console.state.unified_projection)
    assert ack.status == "applied"
    assert ack.latency_ms < 2_000
    assert view.projection_digest == value["projection_digest"]
    assert view.projection_revision == value["projection_revision"]
    for name in view.sections:
        assert view.sections[name]["source_digest"] == value[name]["source_digest"]
        assert view.sections[name]["summary"] == value[name]["summary"]


def test_stale_secret_and_restart_projection_fail_closed() -> None:
    first = projection(2)
    console = AgentConsoleEngine()
    assert console.apply_projection(first, received_at_ms=1_100, rendered_at_ms=1_200).status == (
        "applied"
    )
    stale = projection(1, first["projection_digest"])
    assert console.apply_projection(stale).error_code == "STALE_PROJECTION"

    secret = projection(3, first["projection_digest"])
    secret["workflow"]["summary"]["secret"] = "SECRET_CANARY"
    secret["projection_digest"] = compute_projection_digest(secret)
    assert console.apply_projection(secret).error_code == "REDACTION_FAILED"
    with pytest.raises(ValueError, match="REDACTION_FAILED"):
        WorkflowProjectionView.from_unified(secret)

    restarted = AgentConsoleEngine()
    assert restarted.apply_projection(first).status == "applied"
    assert restarted.state.unified_projection_digest == first["projection_digest"]
