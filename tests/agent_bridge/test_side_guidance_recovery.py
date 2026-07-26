from __future__ import annotations

from copy import deepcopy
from time import perf_counter
from typing import Any

import pytest

from vnpy.agent_console.engine import AgentConsoleEngine
from vnpy.agent_console.guidance import seal_projection_pages, validate_consumer_ack


ENTITY_TYPES = (
    "session",
    "notification",
    "acknowledgement",
    "effective_member",
    "auth_binding",
    "retention",
    "recovery_checkpoint",
    "recovery_run",
    "resource_envelope",
    "agent_allocation",
    "required_minimum",
    "starvation_finding",
    "atomic_action",
    "safe_boundary",
    "health",
)


def _digest(marker: int) -> str:
    return f"blake3:{marker:064x}"


def _item(entity_type: str, revision: int, *, state: str = "ready") -> dict[str, Any]:
    return {
        "entity_type": entity_type,
        "entity_id": f"{entity_type}-1",
        "entity_revision": revision,
        "state": state,
        "source_digest": _digest(revision + len(entity_type)),
        "display": {"label": entity_type, "revision": revision},
        "permitted_actions": [],
    }


def _pages(
    revision: int,
    source_revision: int,
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    midpoint = max(1, len(items) // 2)
    chunks = [items[:midpoint], items[midpoint:]]
    pages = []
    for index, chunk in enumerate(chunks):
        pages.append(
            {
                "entity_type": "guidance_projection_page",
                "contract_version": 1,
                "projection_id": "guidance-projection:mission-1",
                "mission_id": "mission-1",
                "projection_revision": revision,
                "source_revision": source_revision,
                "freshness": "fresh",
                "certainty": "known",
                "items": chunk,
                "cursor": None if index == 0 else "cursor-2",
                "next_cursor": "cursor-2" if index == 0 else None,
                "generated_at_ms": 1_000 + revision,
                "projection_digest": "",
            }
        )
    return seal_projection_pages(pages)


def test_authoritative_snapshot_delta_rebuild_and_consumer_ack_finish_within_rto() -> None:
    console = AgentConsoleEngine()
    snapshot = _pages(1, 10, [_item(kind, 1) for kind in ENTITY_TYPES])
    started = perf_counter()

    assert console.apply_guidance_projection(snapshot[1]) == "partial"
    assert console.apply_guidance_projection(snapshot[0]) == "applied"
    assert perf_counter() - started < 10.0
    assert {kind for kind, _ in console.guidance_state.entities} == set(ENTITY_TYPES)
    assert console.guidance_state.pages[None]["next_cursor"] == "cursor-2"

    ack = console.guidance_state.consumer_ack("vnpy", applied_at_ms=1_500)
    validate_consumer_ack(ack)
    assert ack["projection_revision"] == 1
    assert ack["projection_digest"] == snapshot[0]["projection_digest"]

    delta = _pages(
        2,
        11,
        [
            _item("recovery_run", 2, state="completed"),
            _item("resource_envelope", 2, state="conserving"),
            _item("safe_boundary", 2, state="applied"),
        ],
    )
    assert console.apply_guidance_projection(delta[0]) == "partial"
    assert console.apply_guidance_projection(delta[1]) == "applied"
    assert len(console.guidance_state.entities) == len(ENTITY_TYPES)
    assert console.guidance_state.entities[("recovery_run", "recovery_run-1")]["state"] == (
        "completed"
    )
    assert console.guidance_state.entities[("auth_binding", "auth_binding-1")]["state"] == (
        "ready"
    )


def test_stale_secret_and_cursor_failures_preserve_last_known_valid_projection() -> None:
    console = AgentConsoleEngine()
    accepted = _pages(2, 20, [_item(kind, 2) for kind in ENTITY_TYPES])
    for page in accepted:
        console.apply_guidance_projection(page)
    last_entities = deepcopy(console.guidance_state.entities)
    last_digest = console.guidance_state.projection_digest

    stale = _pages(1, 19, [_item("recovery_run", 1)])
    assert console.apply_guidance_projection(stale[0]) in {"partial", "stale"}
    assert console.apply_guidance_projection(stale[1]) == "stale"
    assert console.guidance_state.entities == last_entities
    assert console.guidance_state.projection_digest == last_digest

    secret_items = [_item("health", 3)]
    secret_items[0]["display"]["authorization"] = "Bearer CANARY_SECRET"
    with pytest.raises(ValueError, match="secret-bearing"):
        _pages(3, 21, secret_items)
    assert console.guidance_state.entities == last_entities

    disconnected = deepcopy(accepted[1])
    disconnected["projection_revision"] = 3
    disconnected["source_revision"] = 21
    disconnected["cursor"] = "unknown-cursor"
    disconnected["next_cursor"] = None
    disconnected["projection_digest"] = _digest(999)
    assert console.apply_guidance_projection(disconnected) == "partial"
    assert console.guidance_state.entities == last_entities


class _SessionProvider:
    def __init__(self) -> None:
        self.auth_session_id = "auth-reconnect-1"
        self.operator_id = "operator-reconnect-1"
        self.verified = True

    def refresh(self, _now_ms: int) -> str:
        return "verified" if self.verified else "revoked"


def test_reconnect_reverifies_os_session_rebuilds_snapshot_and_preserves_isolated_draft() -> None:
    console = AgentConsoleEngine()
    initial = _pages(
        1,
        10,
        [_item("session", 1), _item("auth_binding", 1), _item("health", 1)],
    )
    for projection_page in initial:
        console.apply_guidance_projection(projection_page, applied_at_ms=1_100)
    assert console.next_guidance_projection_ack() is not None

    draft = {"free_form": {"focus": ["600000.SH"], "nested": {"limit": "research"}}}
    console.checkpoint_guidance_draft(
        "mission-1",
        "side-1",
        revision=4,
        content=draft,
        checkpointed_at_ms=1_150,
    )
    console.mark_guidance_disconnected()
    provider = _SessionProvider()
    request = console.begin_guidance_reconnect(provider, now_ms=1_200)
    assert request["requires_authoritative_snapshot"] is True
    assert request["last_projection_revision"] == 1
    assert request["auth_session_id"] == provider.auth_session_id

    rebuilt = _pages(2, 11, [_item("session", 2), _item("health", 2)])
    assert console.apply_guidance_projection(rebuilt[1], applied_at_ms=1_250) == "partial"
    assert console.guidance_state.entities[("auth_binding", "auth_binding-1")]
    assert console.apply_guidance_projection(rebuilt[0], applied_at_ms=1_251) == "applied"
    assert ("auth_binding", "auth_binding-1") not in console.guidance_state.entities
    assert console.guidance_draft("mission-1", "side-1")["content"] == draft

    ack = console.next_guidance_projection_ack()
    assert ack is not None
    validate_consumer_ack(ack)
    assert ack["projection_revision"] == 2
    assert console.apply_guidance_projection(rebuilt[0], applied_at_ms=1_252) == "duplicate"
    assert console.next_guidance_projection_ack() is None


def test_reconnect_fails_closed_when_os_session_cannot_be_reverified() -> None:
    console = AgentConsoleEngine()
    accepted = _pages(1, 10, [_item("session", 1)])
    for projection_page in accepted:
        console.apply_guidance_projection(projection_page, applied_at_ms=1_100)
    console.next_guidance_projection_ack()
    previous = deepcopy(console.guidance_state.entities)

    provider = _SessionProvider()
    provider.verified = False
    console.mark_guidance_disconnected()
    with pytest.raises(PermissionError, match="operating-system session"):
        console.begin_guidance_reconnect(provider, now_ms=1_200)
    with pytest.raises(PermissionError, match="operating-system session"):
        console.apply_guidance_projection(accepted[0], applied_at_ms=1_201)
    assert console.guidance_state.entities == previous
