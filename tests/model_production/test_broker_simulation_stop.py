from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from vnpy.model_production.app_engine import BrokerSimulationCoordinator
from vnpy.model_production.broker_simulation import BrokerSimulationAuthority, GatewayBinding
from vnpy.model_production.safety import (
    BrokerSimulationContainment,
    HardSafetyController,
)


def digest(label: str) -> str:
    return f"sha256:{sha256(label.encode()).hexdigest()}"


class WorkingOrder:
    def __init__(self, gateway: str, identity: str, *, cancellable: bool = True) -> None:
        self.gateway_name = gateway
        self.vt_orderid = identity
        self._cancellable = cancellable
        self.cancel_requests = 0

    def create_cancel_request(self) -> object:
        self.cancel_requests += 1
        if not self._cancellable:
            raise RuntimeError("cancel request unavailable")
        return {"identity": self.vt_orderid}


class Position:
    def __init__(
        self,
        gateway: str,
        symbol: str,
        *,
        volume: float,
        yd_volume: float,
        frozen: float,
        price: float,
    ) -> None:
        self.gateway_name = gateway
        self.vt_symbol = symbol
        self.volume = volume
        self.yd_volume = yd_volume
        self.frozen = frozen
        self.price = price


class MainEngineStub:
    def __init__(
        self,
        *,
        orders: list[WorkingOrder],
        positions: list[Position],
        safety: HardSafetyController | None = None,
    ) -> None:
        self.orders = orders
        self.positions = positions
        self.safety = safety
        self.cancel_calls: list[tuple[object, str]] = []

    def get_all_active_orders(self) -> list[WorkingOrder]:
        return self.orders

    def cancel_order(self, request: object, gateway_name: str) -> None:
        if self.safety is not None:
            assert self.safety.snapshot().active
        self.cancel_calls.append((request, gateway_name))

    def get_all_positions(self) -> list[Position]:
        return self.positions


class ReconciliationStub:
    new_exposure_blocked = False


def binding(database: Path) -> GatewayBinding:
    server = digest("xtp-server")
    account = digest("xtp-account")
    return GatewayBinding.create(
        gateway="XTP",
        environment="broker_simulation",
        server_fingerprint=server,
        account_fingerprint=account,
        credential_ref="credential:xtp",
        process_identity="vnpy-xtp-stop-1",
        rpc_endpoint="127.0.0.1:19601",
        state_store_path=str(database),
        created_at_ms=1_000,
        allowed_server_fingerprints=frozenset({server}),
        allowed_account_fingerprints=frozenset({account}),
    )


def coordinator(
    tmp_path: Path,
    main_engine: MainEngineStub,
    safety: HardSafetyController,
) -> tuple[BrokerSimulationCoordinator, BrokerSimulationAuthority]:
    database = tmp_path / "campaign.sqlite"
    gateway_binding = binding(database)
    authority = BrokerSimulationAuthority(database)
    authority.create_campaign(
        campaign_id="campaign-stop-1",
        candidate_digest=digest("candidate"),
        package_digest=digest("package"),
        configuration_digest=digest("configuration"),
        policy_digest=digest("policy"),
        symbol_set=("600000.SH",),
        calendar_sessions=(
            "2026-07-27",
            "2026-07-28",
            "2026-07-29",
            "2026-07-30",
            "2026-07-31",
        ),
        operator_identity_digest=digest("operator"),
        bindings=(gateway_binding,),
        lifecycle_revision=9,
        now_ms=1_000,
    )
    authority.start_campaign("campaign-stop-1", now_ms=1_100)
    containment = BrokerSimulationContainment(
        main_engine=main_engine,
        gateway_name="XTP",
        database=tmp_path / "containment.sqlite",
        clock_ns=lambda: 1_250_000_000,
    )
    return (
        BrokerSimulationCoordinator(
            campaign_id="campaign-stop-1",
            run_id="campaign-stop-1:xtp",
            binding=gateway_binding,
            authority=authority,
            decision_engine=object(),  # type: ignore[arg-type]
            executor=object(),  # type: ignore[arg-type]
            reconciliation=ReconciliationStub(),  # type: ignore[arg-type]
            safety=safety,
            containment=containment,
        ),
        authority,
    )


def residual_position() -> Position:
    return Position(
        "XTP",
        "600000.SSE",
        volume=150,
        yd_volume=50,
        frozen=0,
        price=10.25,
    )


def test_agent_down_pause_invalidates_campaign_cancels_and_reports_residual(
    tmp_path: Path,
) -> None:
    safety = HardSafetyController()
    xtp_order = WorkingOrder("XTP", "XTP.order-1")
    other_gateway_order = WorkingOrder("TORA", "TORA.order-1")
    main_engine = MainEngineStub(
        orders=[xtp_order, other_gateway_order],
        positions=[residual_position()],
    )
    control, authority = coordinator(tmp_path, main_engine, safety)

    campaign, receipt = control.pause_with_receipt(
        now_ms=1_200,
        detected_at_ns=1_100_000_000,
    )

    assert campaign.state == "paused"
    assert authority.runs("campaign-stop-1")[0].state == "invalid"
    assert main_engine.cancel_calls == [({"identity": "XTP.order-1"}, "XTP")]
    assert xtp_order.cancel_requests == 1
    assert other_gateway_order.cancel_requests == 0
    assert receipt.state == "contained"
    assert receipt.action == "pause"
    assert receipt.working_order_count == 1
    assert receipt.unresolved_outcomes == 0
    assert receipt.residual_positions[0].quantity == 150
    assert receipt.residual_positions[0].available_quantity == 50
    assert receipt.residual_positions[0].t_plus_one_locked_quantity == 100
    assert receipt.residual_positions[0].marked_value_minor == 153_750


def test_emergency_stop_latches_before_cancellation_and_blocks_within_one_second(
    tmp_path: Path,
) -> None:
    safety = HardSafetyController()
    main_engine = MainEngineStub(
        orders=[WorkingOrder("XTP", "XTP.order-2")],
        positions=[residual_position()],
        safety=safety,
    )
    control, authority = coordinator(tmp_path, main_engine, safety)

    campaign, receipt = control.emergency_stop_with_receipt(
        reason_code="operator-emergency-stop",
        evidence_digest=digest("stop"),
        detected_at_ns=1_000_000_000,
        now_ns=1_100_000_000,
        now_ms=1_200,
    )

    assert campaign.state == "stopped"
    assert authority.runs("campaign-stop-1")[0].state == "stopped"
    assert safety.snapshot().active
    assert receipt.action == "emergency_stop"
    assert receipt.exposure_blocked_at_ns == 1_100_000_000
    assert receipt.exposure_blocked_at_ns - receipt.detected_at_ns <= 1_000_000_000
    assert receipt.hard_stop_deadline_met is True
    with pytest.raises(PermissionError, match="HARD_SAFETY_ACTIVE"):
        control.submit_intent(object(), object())  # type: ignore[arg-type]


def test_uncertain_containment_receipt_survives_restart_without_repeat_cancel(
    tmp_path: Path,
) -> None:
    database = tmp_path / "containment.sqlite"
    broken = WorkingOrder("XTP", "XTP.order-unknown", cancellable=False)
    main_engine = MainEngineStub(orders=[broken], positions=[residual_position()])
    containment = BrokerSimulationContainment(
        main_engine=main_engine,
        gateway_name="XTP",
        database=database,
        clock_ns=lambda: 1_300_000_000,
    )
    first = containment.contain(
        action="emergency_stop",
        campaign_id="campaign-stop-1",
        detected_at_ns=1_000_000_000,
        exposure_blocked_at_ns=1_100_000_000,
    )

    restarted = BrokerSimulationContainment(
        main_engine=main_engine,
        gateway_name="XTP",
        database=database,
        clock_ns=lambda: 1_400_000_000,
    )
    retained = restarted.receipt("emergency_stop", "campaign-stop-1")
    replay = restarted.contain(
        action="emergency_stop",
        campaign_id="campaign-stop-1",
        detected_at_ns=1_000_000_000,
        exposure_blocked_at_ns=1_100_000_000,
    )

    assert retained == first == replay
    assert retained.state == "uncertain"
    assert retained.unresolved_outcomes == 1
    assert retained.cancellations[0].state == "failed"
    assert broken.cancel_requests == 1
    assert main_engine.cancel_calls == []
