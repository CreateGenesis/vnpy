from __future__ import annotations

import pytest

from vnpy.model_production.execution import BrokerEffectDispatcher
from vnpy.model_production.reconciliation import ReconciliationManager
from vnpy.model_production.risk import RiskDecision


def test_gateway_submission_requires_accepted_risk_expected_state_and_reconciliation() -> None:
    calls: list[object] = []
    dispatcher = BrokerEffectDispatcher(lambda request: calls.append(request) or "order-1")
    reconciliation = ReconciliationManager()
    accepted = RiskDecision(True, (), 100)
    result = dispatcher.dispatch_accepted(
        stage="gray", request={"symbol": "600000.SH"}, effect_id="effect-1",
        operation_key="operation-1", risk=accepted, risk_persisted=True, expected_state_matches=True,
        reconciliation=reconciliation,
    )
    assert result == "order-1" and len(calls) == 1

    with pytest.raises(PermissionError, match="RISK_NOT_ACCEPTED"):
        dispatcher.dispatch_accepted(
            stage="gray", request={}, effect_id="effect-2", operation_key="operation-2",
            risk=RiskDecision(False, ("DENIED",), 0), risk_persisted=True, expected_state_matches=True,
            reconciliation=reconciliation,
        )
    with pytest.raises(PermissionError, match="EXPECTED_STATE_MISMATCH"):
        dispatcher.dispatch_accepted(
            stage="gray", request={}, effect_id="effect-3", operation_key="operation-3",
            risk=accepted, risk_persisted=True, expected_state_matches=False, reconciliation=reconciliation,
        )
    with pytest.raises(PermissionError, match="RISK_NOT_DURABLE"):
        dispatcher.dispatch_accepted(
            stage="gray", request={}, effect_id="effect-4", operation_key="operation-4",
            risk=accepted, risk_persisted=False, expected_state_matches=True,
            reconciliation=reconciliation,
        )
