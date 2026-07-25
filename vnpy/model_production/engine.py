"""vn.py-owned model lifecycle, decision admission, risk, and order authority."""

from dataclasses import asdict, dataclass
from threading import RLock

from vnpy.trader.object import OrderRequest

from .journal import ModelProductionJournal
from .order_intent import to_order_request
from .risk import AuthoritativeRiskContext, ModelIntent, RiskDecision, RiskEvaluator
from .safety import HardSafetyController


@dataclass(frozen=True)
class AuthoritativeDecisionResult:
    risk: RiskDecision
    order_request: OrderRequest | None


class AuthoritativeDecisionEngine:
    """Admit exact active-model intents and persist before exposing broker work."""

    def __init__(
        self,
        *,
        journal: ModelProductionJournal,
        safety: HardSafetyController,
        expected_producer_id: str,
        active_package_digest: str,
        lifecycle_revision: int,
        stage: str,
    ) -> None:
        self._journal = journal
        self._safety = safety
        self._expected_producer_id = expected_producer_id
        self._active_package_digest = active_package_digest
        self._lifecycle_revision = lifecycle_revision
        self._stage = stage
        self._risk_evaluator = RiskEvaluator()
        self._lock = RLock()
        self._broker_effect_count = 0

    @property
    def broker_effect_count(self) -> int:
        with self._lock:
            return self._broker_effect_count

    def apply(
        self,
        intent: ModelIntent,
        context: AuthoritativeRiskContext,
    ) -> AuthoritativeDecisionResult:
        with self._safety.admission_guard() as safety, self._lock:
            self._journal.append_intent(intent)
            risk = self._risk_evaluator.evaluate(intent, context)
            admission_reasons: list[str] = []
            if intent.producer_id != self._expected_producer_id:
                admission_reasons.append("DECISION_PRODUCER_UNTRUSTED")
            if intent.package_digest != self._active_package_digest:
                admission_reasons.append("ACTIVE_PACKAGE_MISMATCH")
            if intent.lifecycle_revision != self._lifecycle_revision:
                admission_reasons.append("LIFECYCLE_REVISION_MISMATCH")
            if intent.stage != self._stage:
                admission_reasons.append("ACTIVE_STAGE_MISMATCH")
            if safety.active:
                admission_reasons.append("HARD_SAFETY_ACTIVE")
            if admission_reasons:
                risk = risk.with_rejections(*admission_reasons)
            self._journal.append_risk(intent.intent_id, risk)
            if not risk.accepted:
                return AuthoritativeDecisionResult(risk, None)

            order_request = to_order_request(intent, risk)
            operation_key = f"model-order:{intent.intent_id}"
            self._journal.append_broker_effect(
                intent.intent_id,
                operation_key,
                {
                    "decision_id": intent.decision_id,
                    "order_request": {
                        "direction": order_request.direction.name,
                        "exchange": order_request.exchange.name,
                        "offset": order_request.offset.name,
                        "price": order_request.price,
                        "symbol": order_request.symbol,
                        "type": order_request.type.name,
                        "volume": order_request.volume,
                    },
                    "risk": asdict(risk),
                    "status": "ready_for_gateway",
                },
            )
            self._broker_effect_count += 1
            return AuthoritativeDecisionResult(risk, order_request)
