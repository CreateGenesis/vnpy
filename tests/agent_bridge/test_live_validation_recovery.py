from copy import deepcopy
from pathlib import Path

import pytest

from vnpy.agent_bridge.engine import AgentBridgeEngine
from vnpy.agent_bridge.events import (
    LIVE_VALIDATION_EVENT_TYPES,
    LiveValidationContractError,
    compute_live_validation_payload_digest,
)


DIGEST = "blake3:" + "1" * 64


def event(
    event_id: str,
    *,
    event_type: str = "live_validation.campaign",
    revision: int = 1,
    previous: str | None = None,
    epoch: int = 1,
    candidate: str = DIGEST,
    item: dict | None = None,
) -> dict:
    payload = {
        "page_kind": LIVE_VALIDATION_EVENT_TYPES[event_type],
        "page_index": 0,
        "page_size": 1,
        "next_cursor": None,
        "items": [item or {"state": "running"}],
        "certainty": "uncertain" if (item or {}).get("state") == "uncertain" else "certain",
        "freshness": "fresh",
        "error_code": None,
        "evidence_refs": ["sha256:" + "2" * 64],
        "permitted_next_actions": ["inspect"],
    }
    return {
        "contract_version": 1,
        "entity_type": "live_validation_event",
        "event_id": event_id,
        "event_type": event_type,
        "campaign_id": "campaign-1",
        "candidate_digest": candidate,
        "correlation_id": "correlation-1",
        "producer_id": "agentd-1",
        "producer_epoch": epoch,
        "revision": revision,
        "event_time_ms": revision,
        "payload": payload,
        "previous_payload_digest": previous,
        "payload_digest": compute_live_validation_payload_digest(payload),
    }


def test_restart_rebuilds_last_valid_pages_and_delivers_each_durable_ack_once(
    tmp_path: Path,
) -> None:
    first = event("event-1")
    bridge = AgentBridgeEngine(tmp_path)
    assert bridge.apply_live_validation_event(first, received_at_ms=2).status == "applied"
    assert bridge.live_validation_page("campaign-1", "campaign") == first
    bridge.close()

    restarted = AgentBridgeEngine(tmp_path)
    assert restarted.live_validation_page("campaign-1", "campaign") == first
    ack = restarted.next_live_validation_ack()
    assert ack is not None and ack.event_id == "event-1"
    assert ack.provider_calls == 0
    assert restarted.next_live_validation_ack() is None
    restarted.close()

    second_restart = AgentBridgeEngine(tmp_path)
    replayed = second_restart.next_live_validation_ack()
    assert replayed == ack
    second_restart.confirm_live_validation_ack(replayed.event_id, replayed.payload_digest)
    assert second_restart.next_live_validation_ack() is None
    assert second_restart.recover_live_validation()["campaigns"]["campaign-1"]["pages"]
    assert not hasattr(second_restart, "dispatch_provider")
    second_restart.close()

    confirmed_restart = AgentBridgeEngine(tmp_path)
    assert confirmed_restart.next_live_validation_ack() is None
    confirmed_restart.close()


def test_stale_epoch_candidate_drift_duplicates_chain_breaks_and_uncertain_calls_fail_closed(
    tmp_path: Path,
) -> None:
    bridge = AgentBridgeEngine(tmp_path)
    first = event("event-1")
    first_ack = bridge.apply_live_validation_event(first, received_at_ms=2)
    assert first_ack.status == "applied"

    duplicate = bridge.apply_live_validation_event(first, received_at_ms=3)
    assert duplicate == first_ack

    second = event(
        "event-2",
        revision=2,
        previous=first["payload_digest"],
        epoch=2,
        item={"state": "scoring"},
    )
    assert bridge.apply_live_validation_event(second, received_at_ms=4).status == "applied"

    stale = event("event-stale", epoch=2, item={"state": "planned"})
    stale_ack = bridge.apply_live_validation_event(stale, received_at_ms=5)
    assert stale_ack.status == "stale_rejected"
    assert stale_ack.error_code == "STALE_REVISION"

    old_epoch = event(
        "event-old-epoch",
        revision=3,
        previous=second["payload_digest"],
        epoch=1,
    )
    old_epoch_ack = bridge.apply_live_validation_event(old_epoch, received_at_ms=6)
    assert old_epoch_ack.error_code == "STALE_PRODUCER_EPOCH"

    drift = event(
        "event-drift",
        revision=3,
        previous=second["payload_digest"],
        epoch=2,
        candidate="blake3:" + "9" * 64,
    )
    assert bridge.apply_live_validation_event(drift).error_code == "CANDIDATE_DRIFT"

    broken = event(
        "event-broken",
        revision=3,
        previous="blake3:" + "8" * 64,
        epoch=2,
    )
    broken_ack = bridge.apply_live_validation_event(broken)
    assert broken_ack.error_code == "PROJECTION_CHAIN_MISMATCH"
    assert bridge.live_validation_page("campaign-1", "campaign") == second

    uncertain = event(
        "event-uncertain",
        event_type="live_validation.call",
        item={"call_id": "call-1", "state": "uncertain", "retry_disposition": "reconcile_only"},
        epoch=2,
    )
    assert bridge.apply_live_validation_event(uncertain).status == "applied"
    assert bridge.live_validation_page("campaign-1", "call")["payload"]["items"][0][
        "state"
    ] == "uncertain"

    tampered = deepcopy(uncertain)
    tampered["event_id"] = "event-tampered"
    tampered["payload"]["items"][0]["state"] = "completed"
    with pytest.raises(LiveValidationContractError) as mismatch:
        bridge.apply_live_validation_event(tampered)
    assert mismatch.value.code == "DIGEST_MISMATCH"
    bridge.close()
