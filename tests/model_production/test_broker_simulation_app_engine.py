from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from vnpy.model_production.app_engine import BrokerSimulationCoordinator
from vnpy.model_production.broker_simulation import BrokerSimulationAuthority, GatewayBinding
from vnpy.model_production.engine import AuthoritativeDecisionEngine
from vnpy.model_production.execution import BrokerSimulationExecutor
from vnpy.model_production.journal import ModelProductionJournal
from vnpy.model_production.reconciliation import ReconciliationManager
from vnpy.model_production.risk import AuthoritativeRiskContext, ModelIntent
from vnpy.model_production.safety import HardSafetyController


def digest(label: str) -> str:
    return f"sha256:{sha256(label.encode()).hexdigest()}"


class MainEngineStub:
    def __init__(self) -> None:
        self.calls: list[tuple[object, str]] = []

    def send_order(self, request: object, gateway_name: str) -> str:
        self.calls.append((request, gateway_name))
        return f"{gateway_name}.order-{len(self.calls)}"


def build_coordinator(
    tmp_path: Path,
    *,
    main_engine: MainEngineStub | None = None,
) -> tuple[BrokerSimulationCoordinator, BrokerSimulationAuthority, MainEngineStub]:
    database = tmp_path / "runtime.sqlite"
    server = digest("xtp-server")
    account = digest("xtp-account")
    binding = GatewayBinding.create(
        gateway="XTP",
        environment="broker_simulation",
        server_fingerprint=server,
        account_fingerprint=account,
        credential_ref="credential:xtp",
        process_identity="vnpy-xtp-1",
        rpc_endpoint="127.0.0.1:19401",
        state_store_path=str(database),
        created_at_ms=1_000,
        allowed_server_fingerprints=frozenset({server}),
        allowed_account_fingerprints=frozenset({account}),
    )
    authority = BrokerSimulationAuthority(database)
    authority.create_campaign(
        campaign_id="campaign-1",
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
        bindings=(binding,),
        lifecycle_revision=8,
        now_ms=1_000,
    )
    authority.start_campaign("campaign-1", now_ms=1_100)
    safety = HardSafetyController()
    reconciliation = ReconciliationManager(database)
    engine = main_engine or MainEngineStub()
    coordinator = BrokerSimulationCoordinator(
        campaign_id="campaign-1",
        run_id="campaign-1:xtp",
        binding=binding,
        authority=authority,
        decision_engine=AuthoritativeDecisionEngine(
            journal=ModelProductionJournal(database),
            safety=safety,
            expected_producer_id="modeld:slot-a:epoch-1",
            active_package_digest=digest("package"),
            lifecycle_revision=8,
            stage="broker_simulation",
        ),
        executor=BrokerSimulationExecutor(
            main_engine=engine,
            binding=binding,
            reconciliation=reconciliation,
        ),
        reconciliation=reconciliation,
        safety=safety,
    )
    return coordinator, authority, engine


def context() -> AuthoritativeRiskContext:
    return AuthoritativeRiskContext(
        context_id="context-1",
        revision=3,
        package_digest=digest("package"),
        lifecycle_revision=8,
        stage="broker_simulation",
        now_ns=1_010_000_000,
        expires_at_ns=1_100_000_000,
        session_open=True,
        suspended_symbols=frozenset(),
        lower_limit_micros={"600000.SH": 9_000_000},
        upper_limit_micros={"600000.SH": 11_000_000},
        lot_sizes={"600000.SH": 100},
        cash_micros=2_000_000_000_000,
        positions={"600000.SH": 0},
        t1_sellable={"600000.SH": 0},
        reconciled=True,
        unknown_outcomes=frozenset(),
        eligible_symbols=frozenset({"600000.SH"}),
        nav_micros=2_000_000_000_000,
        gross_exposure_micros=0,
        symbol_exposure_micros={"600000.SH": 0},
    )


def intent(index: int) -> ModelIntent:
    return ModelIntent(
        intent_id=f"intent-{index}",
        decision_id=f"decision-{index}",
        producer_id="modeld:slot-a:epoch-1",
        package_digest=digest("package"),
        lifecycle_revision=8,
        stage="broker_simulation",
        context_id="context-1",
        context_revision=3,
        symbol="600000.SH",
        action="buy",
        quantity=100,
        limit_price_micros=10_000_000,
        expires_at_ns=1_050_000_000,
    )


def test_active_bound_run_persists_risk_before_exact_gateway_dispatch(tmp_path: Path) -> None:
    coordinator, _, main_engine = build_coordinator(tmp_path)

    result = coordinator.submit_intent(intent(1), context())

    assert result.risk.accepted
    assert result.order_id == "XTP.order-1"
    assert len(main_engine.calls) == 1
    assert main_engine.calls[0][1] == "XTP"


def test_pause_blocks_new_exposure_without_agent_or_rust_availability(tmp_path: Path) -> None:
    coordinator, authority, main_engine = build_coordinator(tmp_path)

    paused = coordinator.pause(now_ms=1_200)

    assert paused.state == "paused"
    assert authority.runs("campaign-1")[0].state == "invalid"
    with pytest.raises(PermissionError, match="CAMPAIGN_NOT_ACCEPTING_EXPOSURE"):
        coordinator.submit_intent(intent(2), context())
    assert main_engine.calls == []


def test_emergency_stop_is_durable_and_restart_cannot_resume_submission(tmp_path: Path) -> None:
    coordinator, authority, main_engine = build_coordinator(tmp_path)

    stopped = coordinator.emergency_stop(
        reason_code="operator-emergency-stop",
        evidence_digest=digest("stop-evidence"),
        now_ns=1_020_000_000,
        now_ms=1_200,
    )

    assert stopped.state == "stopped"
    assert coordinator.safety_snapshot().active
    restarted = BrokerSimulationAuthority(tmp_path / "runtime.sqlite")
    assert restarted.campaign("campaign-1").state == "stopped"
    with pytest.raises(PermissionError, match="HARD_SAFETY_ACTIVE"):
        coordinator.submit_intent(
            intent(3),
            replace(context(), now_ns=1_021_000_000),
        )
    assert authority.runs("campaign-1")[0].state == "stopped"
    assert main_engine.calls == []
