from __future__ import annotations

from dataclasses import replace

from vnpy.model_production.risk import AuthoritativeRiskContext, ModelIntent, RiskEvaluator
from vnpy.model_production.gray import GrayBudget


def _context() -> AuthoritativeRiskContext:
    return AuthoritativeRiskContext(
        context_id="context-1", revision=3, package_digest="blake3:" + "1" * 64,
        lifecycle_revision=7, stage="gray", now_ns=1_010_000_000,
        expires_at_ns=1_100_000_000, session_open=True, suspended_symbols=frozenset(),
        lower_limit_micros={"600000.SH": 9_000_000},
        upper_limit_micros={"600000.SH": 11_000_000}, lot_sizes={"600000.SH": 100},
        cash_micros=2_000_000_000, positions={"600000.SH": 1_000},
        t1_sellable={"600000.SH": 600}, reconciled=True, unknown_outcomes=frozenset(),
    )


def _intent() -> ModelIntent:
    return ModelIntent(
        "intent-1", "decision-1", "modeld:slot-a", "blake3:" + "1" * 64,
        7, "gray", "context-1", 3, "600000.SH", "buy", 100, 10_000_000,
        1_050_000_000,
    )


def test_live_risk_rejects_stale_market_unhealthy_paths_emergency_and_ineligible_security() -> None:
    cases = [
        (replace(_context(), quote_age_ns=3_000_000_001), "MARKET_DATA_STALE"),
        (replace(_context(), model_healthy=False), "MODEL_UNHEALTHY"),
        (replace(_context(), observer_healthy=False), "OBSERVER_UNHEALTHY"),
        (replace(_context(), hard_risk_healthy=False), "HARD_RISK_UNHEALTHY"),
        (replace(_context(), emergency_stop=True), "EMERGENCY_STOP_ACTIVE"),
        (replace(_context(), eligible_symbols=frozenset()), "SYMBOL_NOT_ELIGIBLE"),
    ]
    for context, reason in cases:
        assert reason in RiskEvaluator().evaluate(_intent(), context).reason_codes


def test_gray_budget_is_enforced_on_the_authoritative_risk_path() -> None:
    budget = GrayBudget(
        frozenset({"600000.SH"}), 1_000_000_000_000, 20_000_000_000,
        {"600000.SH": 0}, 0, 0, 0, 0, 0, 0, 0,
    )
    result = RiskEvaluator().evaluate(_intent(), replace(_context(), gray_budget=budget))
    assert "GRAY_TOTAL_EXPOSURE_LIMIT" in result.reason_codes
