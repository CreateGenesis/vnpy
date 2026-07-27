from __future__ import annotations

from dataclasses import replace
from datetime import date
from hashlib import sha256
import json
from pathlib import Path

import pytest

from vnpy.demo_web.projection import (
    CandidateProjectionInput,
    DemoProjectionInput,
    DemoProjectionStore,
    GatewayProjectionInput,
    HistoricalEvidenceInput,
    HistoricalGatewayInput,
    LatencyProjectionInput,
    PositionProjectionInput,
)


def digest(label: str) -> str:
    return f"sha256:{sha256(label.encode()).hexdigest()}"


def gateway(gateway_name: str, net_profit_minor: int) -> GatewayProjectionInput:
    return GatewayProjectionInput(
        gateway=gateway_name,
        run_digest=digest(f"run:{gateway_name}"),
        state="active",
        connection_state="connected",
        reconciliation_state="complete",
        net_profit_minor=net_profit_minor,
        realized_profit_minor=net_profit_minor + 500,
        unrealized_profit_minor=250,
        fees_minor=750,
        return_bps=32,
        max_drawdown_bps=41,
        fill_count=17,
        positions=(
            PositionProjectionInput(
                symbol="600000.SSE",
                quantity=100,
                available_quantity=0,
                marked_value_minor=102_300,
                unrealized_profit_minor=2_300,
            ),
        ),
        gross_exposure_minor=102_300,
        risk_headroom_minor=897_700,
        local_latency_us=LatencyProjectionInput(10_000, 1_200, 3_400, 5_600, 7_800),
        broker_latency_us=LatencyProjectionInput(17, 18_000, 31_000, 45_000, 52_000),
        incidents=("QUOTE_STALE_RECOVERED",),
    )


def historical(campaign: str, *, ready: bool, profit: int) -> HistoricalEvidenceInput:
    return HistoricalEvidenceInput(
        campaign_digest=digest(campaign),
        candidate_digest=digest("candidate"),
        evidence_digest=digest(f"evidence:{campaign}"),
        sessions=(
            date(2026, 7, 20),
            date(2026, 7, 21),
            date(2026, 7, 22),
            date(2026, 7, 23),
            date(2026, 7, 24),
        ),
        ready=ready,
        gateways=(
            HistoricalGatewayInput("XTP", profit, True, 0, 0),
            HistoricalGatewayInput("TORA", profit, ready, 0, 0 if ready else 1),
        ),
        retained_at_ms=1_722_000_000_000,
    )


def projection_input(source_revision: int = 7) -> DemoProjectionInput:
    return DemoProjectionInput(
        source_revision=source_revision,
        updated_at_ms=1_722_000_001_000,
        candidate=CandidateProjectionInput(
            candidate_digest=digest("candidate"),
            author_lineage_digest=digest("agent-lineage-with-a-very-long-identity"),
            package_digest=digest("package"),
            readiness="ready",
        ),
        campaign_id="b53bc59c-c626-4f16-8a3e-a3185c7dad23",
        campaign_digest=digest("current-campaign"),
        campaign_state="active",
        current_gateways=(gateway("XTP", 3_200), gateway("TORA", 2_800)),
        historical_evidence=(
            historical("successful", ready=True, profit=10_000),
            historical("failed", ready=False, profit=-1_000),
        ),
        risk_state="normal",
        permitted_actions=("pause", "emergency_stop"),
    )


def test_projection_keeps_current_and_historical_simulation_values_distinct(
    tmp_path: Path,
) -> None:
    projection = DemoProjectionStore(tmp_path / "projection.json").publish(projection_input())
    public = projection.to_public_dict()

    assert public["performance_scope"] == "broker_simulation"
    assert public["current"]["label"] == "current_broker_simulation"
    assert public["current"]["campaign_id"] == "b53bc59c-c626-4f16-8a3e-a3185c7dad23"
    assert public["history"][0]["label"] == "historical_broker_simulation_evidence"
    assert public["current"]["gateways"][0]["net_profit_minor"] == 3_200
    assert public["history"][0]["gateways"][0]["net_profit_minor"] == 10_000
    assert public["history"][1]["ready"] is False
    assert public["history"][1]["gateways"][1]["unresolved_outcomes"] == 1


def test_projection_is_redacted_and_preserves_long_digest_values(tmp_path: Path) -> None:
    projection = DemoProjectionStore(tmp_path / "projection.json").publish(projection_input())
    public = projection.to_public_dict()
    encoded = json.dumps(public, sort_keys=True)

    assert public["candidate"]["author_lineage_digest"] == digest(
        "agent-lineage-with-a-very-long-identity"
    )
    for forbidden in (
        "credential",
        "account_id",
        "account_fingerprint",
        "server_fingerprint",
        "rpc_endpoint",
        "state_store_path",
        "order_request",
        "cancel_request",
        "main_engine",
    ):
        assert forbidden not in encoded.lower()


def test_projection_revision_is_idempotent_monotonic_and_restart_durable(tmp_path: Path) -> None:
    path = tmp_path / "projection.json"
    store = DemoProjectionStore(path)
    first = store.publish(projection_input())
    duplicate = store.publish(projection_input())

    assert duplicate == first
    assert first.revision == 1
    assert first.previous_projection_digest is None

    second = store.publish(
        replace(projection_input(8), current_gateways=(gateway("XTP", 3_500), gateway("TORA", 3_100)))
    )
    assert second.revision == 2
    assert second.previous_projection_digest == first.projection_digest
    assert DemoProjectionStore(path).current() == second

    with pytest.raises(ValueError, match="PROJECTION_SOURCE_STALE"):
        store.publish(projection_input(7))
    with pytest.raises(ValueError, match="PROJECTION_SOURCE_REVISION_COLLISION"):
        store.publish(replace(projection_input(8), risk_state="blocked"))
