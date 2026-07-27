from __future__ import annotations

from dataclasses import replace
from hashlib import sha256

import pytest

from vnpy.model_production.risk import AuthoritativeRiskContext, ModelIntent, RiskEvaluator


def digest(label: str) -> str:
    return f"sha256:{sha256(label.encode()).hexdigest()}"


def context() -> AuthoritativeRiskContext:
    return AuthoritativeRiskContext(
        context_id="context-1",
        revision=4,
        package_digest=digest("package"),
        lifecycle_revision=8,
        stage="broker_simulation",
        now_ns=1_010_000_000,
        expires_at_ns=1_100_000_000,
        session_open=True,
        suspended_symbols=frozenset(),
        lower_limit_micros={"600000.SH": 1_000_000},
        upper_limit_micros={"600000.SH": 50_000_000},
        lot_sizes={"600000.SH": 100},
        cash_micros=1_000_000_000_000,
        positions={"600000.SH": 10_000},
        t1_sellable={"600000.SH": 10_000},
        reconciled=True,
        unknown_outcomes=frozenset(),
        eligible_symbols=frozenset({"600000.SH"}),
        nav_micros=1_000_000_000_000,
        gross_exposure_micros=90_000_000_000,
        symbol_exposure_micros={"600000.SH": 7_000_000_000},
        operations_last_second=4,
        operations_this_session=999,
    )


def intent(price_micros: int = 25_000_000, quantity: int = 100) -> ModelIntent:
    return ModelIntent(
        intent_id="intent-1",
        decision_id="decision-1",
        producer_id="modeld:slot-a",
        package_digest=digest("package"),
        lifecycle_revision=8,
        stage="broker_simulation",
        context_id="context-1",
        context_revision=4,
        symbol="600000.SH",
        action="buy",
        quantity=quantity,
        limit_price_micros=price_micros,
        expires_at_ns=1_100_000_000,
    )


def test_exact_broker_simulation_exposure_and_operation_boundaries_are_accepted() -> None:
    decision = RiskEvaluator().evaluate(intent(), context())
    assert decision.accepted
    assert decision.reason_codes == ()


@pytest.mark.parametrize(
    ("changed_intent", "changed_context", "reason"),
    [
        (intent(price_micros=25_000_001), context(), "ORDER_NOTIONAL_LIMIT"),
        (intent(), replace(context(), gross_exposure_micros=98_000_000_000), "TOTAL_EXPOSURE_LIMIT"),
        (
            intent(),
            replace(context(), symbol_exposure_micros={"600000.SH": 8_000_000_000}),
            "SYMBOL_EXPOSURE_LIMIT",
        ),
        (intent(), replace(context(), operations_last_second=5), "OPERATION_RATE_LIMIT"),
        (intent(), replace(context(), operations_this_session=1_000), "SESSION_OPERATION_LIMIT"),
    ],
)
def test_10_1_point25_and_5_per_second_1000_per_session_limits_fail_closed(
    changed_intent: ModelIntent,
    changed_context: AuthoritativeRiskContext,
    reason: str,
) -> None:
    assert reason in RiskEvaluator().evaluate(changed_intent, changed_context).reason_codes


@pytest.mark.parametrize(
    ("changed_intent", "changed_context", "reason"),
    [
        (replace(intent(), symbol="AAPL.US"), context(), "A_SHARE_SYMBOL_INVALID"),
        (replace(intent(), quantity=50), context(), "LOT_SIZE_INVALID"),
        (replace(intent(), limit_price_micros=51_000_000), context(), "PRICE_LIMIT_VIOLATION"),
        (replace(intent(), action="sell"), replace(context(), t1_sellable={"600000.SH": 0}), "T1_SELLABLE_INSUFFICIENT"),
        (intent(), replace(context(), cash_micros=1), "CASH_INSUFFICIENT"),
        (intent(), replace(context(), quote_age_ns=3_000_000_001), "MARKET_DATA_STALE"),
    ],
)
def test_a_share_cash_t1_lot_price_and_freshness_rules_remain_authoritative(
    changed_intent: ModelIntent,
    changed_context: AuthoritativeRiskContext,
    reason: str,
) -> None:
    assert reason in RiskEvaluator().evaluate(changed_intent, changed_context).reason_codes
