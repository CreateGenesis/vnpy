from __future__ import annotations

from copy import deepcopy

from vnpy.agent_console.guidance import (
    GuidanceViewState,
    seal_projection_pages,
    seal_unified_guidance_summary,
    validate_consumer_ack,
)


def item(entity_type: str, entity_id: str, revision: int = 1) -> dict:
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "entity_revision": revision,
        "state": "ready",
        "source_digest": "blake3:" + "a" * 64,
        "display": {"label": entity_id},
        "exact_content": None,
        "permitted_actions": ["inspect"],
    }


def page(
    revision: int,
    cursor: str | None,
    next_cursor: str | None,
    items: list[dict],
) -> dict:
    return {
        "entity_type": "guidance_projection_page",
        "contract_version": 1,
        "projection_id": "guidance-projection:m-1",
        "mission_id": "m-1",
        "projection_revision": revision,
        "source_revision": revision,
        "freshness": "fresh",
        "certainty": "known",
        "items": items,
        "cursor": cursor,
        "next_cursor": next_cursor,
        "generated_at_ms": 100 + revision,
        "projection_digest": "",
    }


def test_projection_assembles_canonical_pages_and_all_entity_families() -> None:
    entity_types = [
        "session", "turn", "draft", "notification", "acknowledgement", "effective_member",
        "auth_binding", "retention", "recovery_checkpoint", "recovery_run", "resource_envelope",
        "agent_allocation", "required_minimum", "resource_decision", "starvation_finding",
        "atomic_action", "safe_boundary", "template", "health",
    ]
    pages = seal_projection_pages([
        page(1, None, "page-2", [item(kind, f"entity-{index}") for index, kind in enumerate(entity_types[:10])]),
        page(1, "page-2", None, [item(kind, f"entity-{index}") for index, kind in enumerate(entity_types[10:], 10)]),
    ])
    state = GuidanceViewState()
    assert state.apply(pages[1]) == "partial"
    assert state.apply(pages[0]) == "applied"
    assert state.projection_revision == 1
    assert len(state.entities) == len(entity_types)
    assert state.apply(pages[0]) == "duplicate"


def test_digest_stale_secret_and_incomplete_updates_keep_last_known_valid() -> None:
    state = GuidanceViewState()
    first = seal_projection_pages([page(1, None, None, [item("session", "session-1", 2)])])[0]
    assert state.apply(first) == "applied"

    incomplete = seal_projection_pages([
        page(2, None, "page-2", [item("turn", "turn-2")]),
        page(2, "page-2", None, []),
    ])
    assert state.apply(incomplete[0]) == "partial"
    assert state.projection_revision == 1

    tampered = seal_projection_pages([page(3, None, None, [item("health", "health-1")])])[0]
    tampered["items"][0]["display"] = {"label": "changed-after-seal"}
    try:
        state.apply(tampered)
    except ValueError as error:
        assert "digest" in str(error)
    else:
        raise AssertionError("tampered complete page set must fail closed")

    stale = seal_projection_pages([page(3, None, None, [item("session", "session-1", 1)])])[0]
    assert state.apply(stale) == "stale"

    secret = page(3, None, None, [item("health", "health-2")])
    secret["items"][0]["display"] = {"authorization": "Bearer forbidden"}
    try:
        seal_projection_pages([secret])
    except ValueError:
        pass
    else:
        raise AssertionError("secret-bearing projection must fail closed")
    assert state.projection_revision == 1
    assert state.entities[("session", "session-1")]["entity_revision"] == 2


def test_canonical_consumer_ack_binds_current_projection() -> None:
    state = GuidanceViewState()
    current = seal_projection_pages([page(7, None, None, [])])[0]
    state.apply(current)
    ack = state.consumer_ack("vnpy", applied_at_ms=500)
    assert ack["entity_type"] == "guidance_projection_consumer_ack"
    assert ack["projection_id"] == "guidance-projection:m-1"
    assert ack["projection_revision"] == 7
    assert ack["projection_digest"] == current["projection_digest"]
    assert ack["ack_digest"].startswith("blake3:")

    changed = deepcopy(ack)
    changed["applied_at_ms"] = 501
    try:
        validate_consumer_ack(changed)
    except ValueError as error:
        assert "digest" in str(error)
    else:
        raise AssertionError("consumer ACK digest must bind the application timestamp")


def test_redacted_unified_summary_is_digest_bound_and_monotonic() -> None:
    vector = {
        "input_tokens": 0,
        "output_tokens": 0,
        "model_calls": 0,
        "tool_calls": 0,
        "cli_calls": 0,
        "subagent_dispatches": 0,
        "wall_time_ms": 0,
        "cost_microunits": 0,
    }
    summary = seal_unified_guidance_summary({
        "entity_type": "unified_guidance_summary",
        "contract_version": 1,
        "mission_id": "m-1",
        "source_revision": 1,
        "session_counts": {"active": 1},
        "notification_counts": {"pending": 1},
        "effective_guidance_revision": 0,
        "effective_guidance_digest": "blake3:" + "e" * 64,
        "oldest_queue_age_ms": 0,
        "auth": {
            "auth_session_id": "auth-1",
            "state": "active",
            "verification_epoch": 1,
            "expires_at_ms": 1_000,
        },
        "recovery": {
            "state": "ready",
            "checkpoint_age_ms": 0,
            "state_visible_elapsed_ms": None,
            "resume_elapsed_ms": None,
        },
        "retention": {
            "waiting_count": 0,
            "retaining_count": 0,
            "eligible_count": 0,
            "blocked_count": 0,
            "next_delete_after_ms": None,
        },
        "resources": {
            "state": "healthy",
            "ceiling": vector,
            "allocated": vector,
            "protected": vector,
            "reserved": vector,
            "consumed": vector,
            "remaining": vector,
            "burn_rate": vector,
            "projected_usage": vector,
            "forecast_horizon_ms": 60_000,
        },
        "starvation": {
            "open_warning_count": 0,
            "open_blocking_count": 0,
            "blocked_operation_count": 0,
        },
        "health": "ready",
        "last_error_code": None,
        "permitted_actions": ["inspect"],
        "summary_digest": "",
    })
    state = GuidanceViewState(mission_id="m-1")
    assert state.apply_summary(summary) == "applied"
    assert state.apply_summary(summary) == "duplicate"

    tampered = deepcopy(summary)
    tampered["notification_counts"]["pending"] = 2
    try:
        state.apply_summary(tampered)
    except ValueError as error:
        assert "digest" in str(error)
    else:
        raise AssertionError("summary digest must bind every redacted count")
    assert state.redacted_summary()["notification_counts"] == {"pending": 1}
