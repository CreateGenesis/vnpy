from __future__ import annotations

from dataclasses import replace

from vnpy.model_production.risk import (
    AuthoritativeRiskContext,
    ModelIntent,
    RiskEvaluator,
)


def _context() -> AuthoritativeRiskContext:
    return AuthoritativeRiskContext(
        context_id="context-1",
        revision=3,
        package_digest="blake3:" + "1" * 64,
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


def _intent(action: str = "buy", quantity: int = 100) -> ModelIntent:
    return ModelIntent(
        intent_id="intent-1",
        decision_id="decision-1",
        producer_id="modeld:slot-a",
        package_digest="blake3:" + "1" * 64,
        lifecycle_revision=7,
        stage="gray",
        context_id="context-1",
        context_revision=3,
        symbol="600000.SH",
        action=action,
        quantity=quantity,
        limit_price_micros=10_000_000,
        expires_at_ns=1_050_000_000,
    )


def test_vnpy_alone_accepts_exact_a_share_risk_and_normalizes_order_semantics() -> None:
    result = RiskEvaluator().evaluate(_intent(), _context())
    assert result.accepted
    assert result.normalized_quantity == 100
    assert result.reason_codes == ()


def test_session_t1_cash_position_lot_limit_stage_freshness_and_reconciliation_fail_closed() -> None:
    cases = [
        (_intent(), replace(_context(), session_open=False), "MARKET_SESSION_CLOSED"),
        (_intent("sell", 700), _context(), "T1_SELLABLE_INSUFFICIENT"),
        (_intent("sell", 1_100), _context(), "POSITION_INSUFFICIENT"),
        (_intent(), replace(_context(), cash_micros=1), "CASH_INSUFFICIENT"),
        (_intent(quantity=99), _context(), "LOT_SIZE_INVALID"),
        (replace(_intent(), limit_price_micros=11_000_001), _context(), "PRICE_LIMIT_VIOLATION"),
        (_intent(), replace(_context(), stage="paper"), "STAGE_BROKER_INACCESSIBLE"),
        (_intent(), replace(_context(), now_ns=1_100_000_000), "CONTEXT_STALE"),
        (_intent(), replace(_context(), reconciled=False), "RECONCILIATION_REQUIRED"),
        (_intent(), replace(_context(), unknown_outcomes=frozenset({"effect-1"})), "UNKNOWN_OUTCOME_BLOCK"),
    ]
    evaluator = RiskEvaluator()
    for intent, context, expected in cases:
        result = evaluator.evaluate(intent, context)
        assert not result.accepted
        assert expected in result.reason_codes


def test_suspension_and_exact_context_identity_are_mandatory() -> None:
    suspended = replace(_context(), suspended_symbols=frozenset({"600000.SH"}))
    assert "SYMBOL_SUSPENDED" in RiskEvaluator().evaluate(_intent(), suspended).reason_codes
    drifted = replace(_intent(), context_revision=4)
    assert "CONTEXT_IDENTITY_DRIFT" in RiskEvaluator().evaluate(drifted, _context()).reason_codes
