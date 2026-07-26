from pathlib import Path
from time import time_ns

from vnpy.agent_bridge.engine import AgentBridgeEngine
from vnpy.agent_bridge.events import (
    LIVE_VALIDATION_EVENT_TYPES,
    compute_live_validation_payload_digest,
)


DIGEST = "blake3:" + "1" * 64


def event(
    event_type: str,
    revision: int,
    previous: str | None,
    item: dict,
) -> dict:
    payload = {
        "page_kind": LIVE_VALIDATION_EVENT_TYPES[event_type],
        "page_index": 0,
        "page_size": 1,
        "next_cursor": None,
        "items": [item],
        "certainty": "certain",
        "freshness": "fresh",
        "error_code": None,
        "evidence_refs": ["sha256:" + "2" * 64],
        "permitted_next_actions": ["inspect"],
    }
    return {
        "contract_version": 1,
        "entity_type": "live_validation_event",
        "event_id": f"{payload['page_kind']}-{revision}",
        "event_type": event_type,
        "campaign_id": "campaign-1",
        "candidate_digest": DIGEST,
        "correlation_id": "correlation-1",
        "producer_id": "agentd-1",
        "producer_epoch": 1,
        "revision": revision,
        "event_time_ms": time_ns() // 1_000_000,
        "payload": payload,
        "previous_payload_digest": previous,
        "payload_digest": compute_live_validation_payload_digest(payload),
    }


def test_all_accepted_status_and_budget_revisions_are_visible_within_ten_seconds(
    tmp_path: Path,
) -> None:
    bridge = AgentBridgeEngine(tmp_path)
    notifications: list[tuple[str, int, int]] = []
    bridge.subscribe_live_validation(
        lambda update, ack: notifications.append(
            (update.payload["page_kind"], update.event_time_ms, ack.received_at_ms)
        )
    )
    previous = {"campaign": None, "budget": None}
    for revision in range(1, 21):
        for event_type, item in (
            ("live_validation.campaign", {"state": "running", "completed_cases": revision}),
            (
                "live_validation.budget",
                {"remaining_tokens": 50_000 - revision, "low_watermark_state": "normal"},
            ),
        ):
            kind = LIVE_VALIDATION_EVENT_TYPES[event_type]
            value = event(event_type, revision, previous[kind], item)
            acknowledged = bridge.apply_live_validation_event(value)
            assert acknowledged.status == "applied"
            previous[kind] = value["payload_digest"]

    assert len(notifications) == 40
    assert all(received - emitted < 10_000 for _, emitted, received in notifications)
    assert bridge.live_validation_page("campaign-1", "campaign")["revision"] == 20
    assert bridge.live_validation_page("campaign-1", "budget")["revision"] == 20
    bridge.close()
