from copy import deepcopy
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

from vnpy.agent_bridge.events import (
    LIVE_VALIDATION_EVENT_TYPES,
    LiveValidationAck,
    LiveValidationContractError,
    LiveValidationEvent,
    compute_live_validation_payload_digest,
)


DIGEST = "blake3:" + "1" * 64


def live_event(
    event_type: str = "live_validation.campaign",
    *,
    event_id: str = "event-1",
    revision: int = 1,
    previous: str | None = None,
    epoch: int = 1,
    candidate: str = DIGEST,
    items: list[dict] | None = None,
    certainty: str = "certain",
    freshness: str = "fresh",
    error_code: str | None = None,
) -> dict:
    payload = {
        "page_kind": LIVE_VALIDATION_EVENT_TYPES[event_type],
        "page_index": 0,
        "page_size": max(1, len(items or [{}])),
        "next_cursor": None,
        "items": items or [{"state": "running"}],
        "certainty": certainty,
        "freshness": freshness,
        "error_code": error_code,
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
        "event_time_ms": 1,
        "payload": payload,
        "previous_payload_digest": previous,
        "payload_digest": compute_live_validation_payload_digest(payload),
    }


def _native_bridge_class() -> type:
    workspace = Path(__file__).resolve().parents[3]
    target_roots = [workspace / "auto-tride-rust" / "target"]
    if configured_target := os.environ.get("CARGO_TARGET_DIR"):
        target_roots.insert(0, Path(configured_target))
    candidates = sorted(
        {
            candidate
            for target_root in target_roots
            for suffix in ("dll", "so", "dylib")
            for candidate in (target_root / "debug" / "deps").glob(
                f"*vnpy_bridge_py*.{suffix}"
            )
        }
    )
    if not candidates:
        raise RuntimeError("build vnpy-bridge-py before cross-language contract tests")
    loader = importlib.machinery.ExtensionFileLoader("vnpy_bridge_py", str(candidates[-1]))
    spec = importlib.util.spec_from_loader("vnpy_bridge_py", loader)
    if spec is None:
        raise RuntimeError("unable to load vnpy-bridge-py extension")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    sys.modules["vnpy_bridge_py"] = module
    return module.NativeBridge


def test_all_event_kinds_pages_cursors_digests_epochs_and_acks_are_strict() -> None:
    for index, event_type in enumerate(LIVE_VALIDATION_EVENT_TYPES, 1):
        value = live_event(event_type, event_id=f"event-{index}")
        event = LiveValidationEvent.decode(json.dumps(value).encode())
        assert event.event_type == event_type
        assert event.payload["page_kind"] == LIVE_VALIDATION_EVENT_TYPES[event_type]
        assert event.producer_epoch == 1
        assert event.candidate_digest == DIGEST
        assert LiveValidationEvent.decode(event.encode()) == event
        ack = LiveValidationAck.create(
            event, status="applied", error_code=None, received_at_ms=2
        )
        assert LiveValidationAck.decode(ack.encode()) == ack
        assert ack.authority == "research_only"
        assert ack.provider_calls == 0


def test_python_golden_digest_is_accepted_by_the_rust_native_boundary(tmp_path: Path) -> None:
    value = live_event(items=[{"state": "running", "evidence_kind": "live"}])
    native = _native_bridge_class()(str(tmp_path), "cross-language", "cross-language", 4, 4)
    validated = json.loads(
        native.validate_live_validation_page(json.dumps(value, separators=(",", ":")).encode())
    )
    assert validated["payload_digest"] == value["payload_digest"]
    assert validated["previous_payload_digest"] is None
    assert validated["candidate_digest"] == DIGEST


def test_unknown_versions_fields_page_overflow_and_digest_tampering_fail_closed() -> None:
    value = live_event()
    for mutation in (
        lambda item: item.update(contract_version=2),
        lambda item: item.update(api_key="forbidden"),
        lambda item: item["payload"].update(page_kind="call"),
        lambda item: item["payload"].update(page_size=0),
        lambda item: item.update(payload_digest="blake3:" + "f" * 64),
    ):
        invalid = deepcopy(value)
        mutation(invalid)
        with pytest.raises(LiveValidationContractError):
            LiveValidationEvent.decode(invalid)

    duplicate = value.copy()
    encoded = json.dumps(duplicate)[:-1] + ',"event_id":"collision"}'
    with pytest.raises(LiveValidationContractError, match="duplicate field"):
        LiveValidationEvent.decode(encoded)


def test_chain_and_redaction_rules_reject_noninitial_null_and_secret_shapes() -> None:
    with pytest.raises(LiveValidationContractError) as missing_previous:
        LiveValidationEvent.decode(live_event(revision=2))
    assert missing_previous.value.code == "PROJECTION_CHAIN_MISMATCH"

    first_with_previous = live_event(previous="blake3:" + "3" * 64)
    with pytest.raises(LiveValidationContractError):
        LiveValidationEvent.decode(first_with_previous)

    secret = live_event(items=[{"authorization": "Bearer hidden"}])
    with pytest.raises(LiveValidationContractError) as redacted:
        LiveValidationEvent.decode(secret)
    assert redacted.value.code == "REDACTION_FAILED"

    model_score = live_event(items=[{"model_supplied_score": 100}])
    with pytest.raises(LiveValidationContractError):
        LiveValidationEvent.decode(model_score)
