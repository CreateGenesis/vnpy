"""The only broker gateway dispatcher for authoritative accepted vn.py effects."""

from __future__ import annotations

from collections.abc import Callable
from threading import RLock
from typing import Any

from .reconciliation import ReconciliationManager
from .risk import RiskDecision


class BrokerInaccessibleError(PermissionError):
    pass


class BrokerEffectDispatcher:
    def __init__(self, gateway_submit: Callable[[Any], str]) -> None:
        self._gateway_submit = gateway_submit
        self._operation_results: dict[str, str] = {}
        self._lock = RLock()

    def dispatch(self, stage: str, request: Any, operation_key: str) -> str:
        if stage not in {"gray", "production"}:
            raise BrokerInaccessibleError("STAGE_BROKER_INACCESSIBLE")
        if not operation_key:
            raise ValueError("operation key is required")
        with self._lock:
            if operation_key in self._operation_results:
                return self._operation_results[operation_key]
            result = self._gateway_submit(request)
            self._operation_results[operation_key] = result
            return result

    def dispatch_accepted(
        self,
        *,
        stage: str,
        request: Any,
        effect_id: str,
        operation_key: str,
        risk: RiskDecision,
        risk_persisted: bool,
        expected_state_matches: bool,
        reconciliation: ReconciliationManager,
    ) -> str:
        if not risk.accepted:
            raise PermissionError("RISK_NOT_ACCEPTED")
        if not risk_persisted:
            raise PermissionError("RISK_NOT_DURABLE")
        if not expected_state_matches:
            raise PermissionError("EXPECTED_STATE_MISMATCH")
        if not reconciliation.can_dispatch(operation_key):
            raise PermissionError("RECONCILIATION_REQUIRED")
        reconciliation.record_dispatch(effect_id, operation_key)
        try:
            result = self.dispatch(stage, request, operation_key)
        except Exception:
            reconciliation.record_timeout(effect_id)
            raise
        reconciliation.record_outcome(effect_id, "accepted")
        return result
