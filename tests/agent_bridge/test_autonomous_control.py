from dataclasses import replace

import pytest

from vnpy.agent_bridge.autonomous_control import (
    create_guidance_strategy_lifecycle_receipt,
    validate_guidance_strategy_lifecycle_receipt,
)


def test_guidance_strategy_receipt_is_content_free_and_vnpy_owned() -> None:
    receipt = create_guidance_strategy_lifecycle_receipt(
        "mission-1",
        "notification-1",
        "strategy-1",
        "v1",
        "active",
        1,
        event_time_ms=1_000,
    )
    validate_guidance_strategy_lifecycle_receipt(receipt)
    assert receipt.producer_identity == "vnpy:autonomous-control"
    assert receipt.receipt_digest.startswith("blake3:")
    assert "body" not in receipt.__dict__
    assert "prompt" not in receipt.__dict__


def test_terminal_receipt_binds_exact_strategy_version_and_time() -> None:
    receipt = create_guidance_strategy_lifecycle_receipt(
        "mission-1",
        "notification-1",
        "strategy-1",
        "v2",
        "terminated",
        3,
        event_time_ms=3_000,
        terminated_at_ms=2_900,
    )
    assert receipt.terminated_at_ms == 2_900
    with pytest.raises(ValueError, match="digest mismatch"):
        validate_guidance_strategy_lifecycle_receipt(replace(receipt, strategy_version="v3"))


def test_agent_identity_cannot_substitute_for_vnpy_lifecycle_authority() -> None:
    receipt = create_guidance_strategy_lifecycle_receipt(
        "mission-1",
        "notification-1",
        "strategy-1",
        "v1",
        "active",
        1,
        event_time_ms=1_000,
    )
    with pytest.raises(ValueError, match="untrusted"):
        validate_guidance_strategy_lifecycle_receipt(
            replace(receipt, producer_identity="master-agent")
        )
