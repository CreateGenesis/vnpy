"""Authoritative A-share pre-trade risk evaluation owned by vn.py."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .gray import GrayBudget


_BROKER_STAGES = frozenset({"gray", "production"})
_ORDER_ACTIONS = frozenset({"buy", "sell", "reduce", "close"})


@dataclass(frozen=True)
class AuthoritativeRiskContext:
    """Immutable account, market, lifecycle, and reconciliation snapshot."""

    context_id: str
    revision: int
    package_digest: str
    lifecycle_revision: int
    stage: str
    now_ns: int
    expires_at_ns: int
    session_open: bool
    suspended_symbols: frozenset[str]
    lower_limit_micros: dict[str, int]
    upper_limit_micros: dict[str, int]
    lot_sizes: dict[str, int]
    cash_micros: int
    positions: dict[str, int]
    t1_sellable: dict[str, int]
    reconciled: bool
    unknown_outcomes: frozenset[str]
    quote_age_ns: int = 0
    model_healthy: bool = True
    observer_healthy: bool = True
    hard_risk_healthy: bool = True
    emergency_stop: bool = False
    eligible_symbols: frozenset[str] | None = None
    gray_budget: GrayBudget | None = None


@dataclass(frozen=True)
class ModelIntent:
    """Bounded order intent produced by the exact active model generation."""

    intent_id: str
    decision_id: str
    producer_id: str
    package_digest: str
    lifecycle_revision: int
    stage: str
    context_id: str
    context_revision: int
    symbol: str
    action: str
    quantity: int
    limit_price_micros: int
    expires_at_ns: int


@dataclass(frozen=True)
class RiskDecision:
    """Deterministic vn.py risk disposition."""

    accepted: bool
    reason_codes: tuple[str, ...]
    normalized_quantity: int

    def with_rejections(self, *reason_codes: str) -> RiskDecision:
        combined = tuple(dict.fromkeys((*reason_codes, *self.reason_codes)))
        return RiskDecision(False, combined, self.normalized_quantity)


class RiskEvaluator:
    """Apply A-share and authoritative-context checks without side effects."""

    def evaluate(
        self,
        intent: ModelIntent,
        context: AuthoritativeRiskContext,
    ) -> RiskDecision:
        reasons: list[str] = []

        if (
            intent.context_id != context.context_id
            or intent.context_revision != context.revision
            or intent.package_digest != context.package_digest
            or intent.lifecycle_revision != context.lifecycle_revision
            or intent.stage != context.stage
        ):
            reasons.append("CONTEXT_IDENTITY_DRIFT")
        if context.stage not in _BROKER_STAGES or intent.stage not in _BROKER_STAGES:
            reasons.append("STAGE_BROKER_INACCESSIBLE")
        if context.now_ns >= context.expires_at_ns or context.now_ns >= intent.expires_at_ns:
            reasons.append("CONTEXT_STALE")
        if not context.session_open:
            reasons.append("MARKET_SESSION_CLOSED")
        if intent.symbol in context.suspended_symbols:
            reasons.append("SYMBOL_SUSPENDED")
        if not context.reconciled:
            reasons.append("RECONCILIATION_REQUIRED")
        if context.unknown_outcomes:
            reasons.append("UNKNOWN_OUTCOME_BLOCK")
        if context.quote_age_ns > 3_000_000_000:
            reasons.append("MARKET_DATA_STALE")
        if not context.model_healthy:
            reasons.append("MODEL_UNHEALTHY")
        if not context.observer_healthy:
            reasons.append("OBSERVER_UNHEALTHY")
        if not context.hard_risk_healthy:
            reasons.append("HARD_RISK_UNHEALTHY")
        if context.emergency_stop:
            reasons.append("EMERGENCY_STOP_ACTIVE")
        if context.eligible_symbols is not None and intent.symbol not in context.eligible_symbols:
            reasons.append("SYMBOL_NOT_ELIGIBLE")
        if context.stage == "gray" and context.gray_budget is not None:
            from .gray import GrayOrder

            gray_order = GrayOrder(
                symbol=intent.symbol,
                quantity=max(intent.quantity, 0),
                price_micros=max(intent.limit_price_micros, 0),
                increases_exposure=intent.action == "buy",
                cancellation=intent.action == "cancel_intent",
            )
            reasons.extend(context.gray_budget.evaluate(gray_order))

        if intent.action not in _ORDER_ACTIONS:
            reasons.append("MODEL_ACTION_UNSUPPORTED")
        if intent.quantity <= 0:
            reasons.append("QUANTITY_INVALID")

        lot_size = context.lot_sizes.get(intent.symbol)
        if lot_size is None or lot_size <= 0:
            reasons.append("LOT_SIZE_UNKNOWN")
        elif intent.quantity > 0 and intent.quantity % lot_size:
            reasons.append("LOT_SIZE_INVALID")

        lower_limit = context.lower_limit_micros.get(intent.symbol)
        upper_limit = context.upper_limit_micros.get(intent.symbol)
        if (
            lower_limit is None
            or upper_limit is None
            or lower_limit <= 0
            or lower_limit > upper_limit
        ):
            reasons.append("PRICE_LIMIT_UNKNOWN")
        elif not lower_limit <= intent.limit_price_micros <= upper_limit:
            reasons.append("PRICE_LIMIT_VIOLATION")

        if intent.action == "buy":
            required_cash = intent.quantity * intent.limit_price_micros
            if required_cash > context.cash_micros:
                reasons.append("CASH_INSUFFICIENT")
        elif intent.action in {"sell", "reduce", "close"}:
            position = context.positions.get(intent.symbol, 0)
            sellable = context.t1_sellable.get(intent.symbol, 0)
            if intent.quantity > position:
                reasons.append("POSITION_INSUFFICIENT")
            if intent.quantity > sellable:
                reasons.append("T1_SELLABLE_INSUFFICIENT")

        unique_reasons = tuple(dict.fromkeys(reasons))
        return RiskDecision(
            accepted=not unique_reasons,
            reason_codes=unique_reasons,
            normalized_quantity=max(intent.quantity, 0),
        )
