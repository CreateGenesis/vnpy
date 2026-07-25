from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from vnpy.model_production.engine import AuthoritativeDecisionEngine
from vnpy.model_production.journal import ModelProductionJournal
from vnpy.model_production.risk import AuthoritativeRiskContext, ModelIntent
from vnpy.model_production.safety import HardSafetyController


def _digest(char: str) -> str:
    return "blake3:" + char * 64


def _context() -> AuthoritativeRiskContext:
    return AuthoritativeRiskContext(
        context_id="context-1",
        revision=3,
        package_digest=_digest("1"),
        lifecycle_revision=7,
        stage="gray",
        now_ns=1_010_000_000,
        expires_at_ns=1_100_000_000,
        session_open=True,
        suspended_symbols=frozenset(),
        lower_limit_micros={"600000.SH": 9_000_000},
        upper_limit_micros={"600000.SH": 11_000_000},
        lot_sizes={"600000.SH": 100},
        cash_micros=2_000_000_000,
        positions={"600000.SH": 1_000},
        t1_sellable={"600000.SH": 600},
        reconciled=True,
        unknown_outcomes=frozenset(),
    )


def _intent(index: int) -> ModelIntent:
    return ModelIntent(
        intent_id=f"intent-{index}",
        decision_id=f"decision-{index}",
        producer_id="modeld:slot-a",
        package_digest=_digest("1"),
        lifecycle_revision=7,
        stage="gray",
        context_id="context-1",
        context_revision=3,
        symbol="600000.SH",
        action="buy",
        quantity=100,
        limit_price_micros=10_000_000,
        expires_at_ns=1_050_000_000,
    )


def test_active_hard_safety_preempts_every_concurrent_fast_action(tmp_path: Path) -> None:
    safety = HardSafetyController()
    safety.activate("market-data-loss", "critical", _digest("9"), 1_005_000_000)
    engine = AuthoritativeDecisionEngine(
        journal=ModelProductionJournal(tmp_path / "journal.sqlite"),
        safety=safety,
        expected_producer_id="modeld:slot-a",
        active_package_digest=_digest("1"),
        lifecycle_revision=7,
        stage="gray",
    )
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda index: engine.apply(_intent(index), _context()), range(32)))
    assert all(not result.risk.accepted for result in results)
    assert all("HARD_SAFETY_ACTIVE" in result.risk.reason_codes for result in results)
    assert engine.broker_effect_count == 0
    assert safety.snapshot().active
    notifications = safety.notifications()
    assert len(notifications) == 1
    assert notifications[0].event_type == "hard_safety_activated"
    assert notifications[0].reason_code == "market-data-loss"
    assert not hasattr(safety, "clear_by_agent")
    assert not hasattr(safety, "clear_by_model")


def test_intent_is_durable_before_risk_and_risk_before_broker_effect(tmp_path: Path) -> None:
    journal = ModelProductionJournal(tmp_path / "journal.sqlite")
    engine = AuthoritativeDecisionEngine(
        journal=journal,
        safety=HardSafetyController(),
        expected_producer_id="modeld:slot-a",
        active_package_digest=_digest("1"),
        lifecycle_revision=7,
        stage="gray",
    )
    result = engine.apply(_intent(1), _context())
    assert result.risk.accepted
    assert result.order_request is not None
    assert journal.event_kinds("intent-1") == ("intent", "risk", "broker_effect")


def test_identity_drift_is_rejected_before_broker_effect(tmp_path: Path) -> None:
    engine = AuthoritativeDecisionEngine(
        journal=ModelProductionJournal(tmp_path / "journal.sqlite"),
        safety=HardSafetyController(),
        expected_producer_id="modeld:slot-a",
        active_package_digest=_digest("1"),
        lifecycle_revision=7,
        stage="gray",
    )
    result = engine.apply(replace(_intent(1), producer_id="agent:master"), _context())
    assert not result.risk.accepted
    assert "DECISION_PRODUCER_UNTRUSTED" in result.risk.reason_codes
    assert engine.broker_effect_count == 0
