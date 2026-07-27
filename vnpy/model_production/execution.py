"""The only broker gateway dispatcher for authoritative accepted vn.py effects."""

from __future__ import annotations

from collections.abc import Callable
from threading import RLock
from typing import Any

from .reconciliation import ReconciliationManager
from .risk import RiskDecision
from .broker_simulation import GatewayBinding


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


class BrokerSimulationExecutor:
    """Dispatch an accepted operation only through its exact bound vn.py MainEngine."""

    def __init__(
        self,
        *,
        main_engine: Any,
        binding: GatewayBinding,
        reconciliation: ReconciliationManager,
    ) -> None:
        if binding.environment != "broker_simulation":
            raise ValueError("SIMULATION_ENVIRONMENT_REQUIRED")
        self._main_engine = main_engine
        self._binding = binding
        self._reconciliation = reconciliation
        self._lock = RLock()

    def submit(
        self,
        *,
        stage: str,
        request: Any,
        effect_id: str,
        operation_key: str,
        gateway_name: str,
        account_fingerprint: str,
        risk: RiskDecision,
        risk_persisted: bool,
        expected_state_matches: bool,
    ) -> str:
        if stage != "broker_simulation":
            raise BrokerInaccessibleError("STAGE_BROKER_INACCESSIBLE")
        if gateway_name != self._binding.gateway:
            raise PermissionError("GATEWAY_BINDING_MISMATCH")
        if account_fingerprint != self._binding.account_fingerprint:
            raise PermissionError("ACCOUNT_BINDING_MISMATCH")
        if not risk.accepted:
            raise PermissionError("RISK_NOT_ACCEPTED")
        if not risk_persisted:
            raise PermissionError("RISK_NOT_DURABLE")
        if not expected_state_matches:
            raise PermissionError("EXPECTED_STATE_MISMATCH")
        if not effect_id or not operation_key:
            raise ValueError("operation identity is required")

        with self._lock:
            existing = self._reconciliation.outcome_for_operation(operation_key)
            if existing is not None:
                if existing.effect_id != effect_id:
                    raise RuntimeError("OPERATION_KEY_COLLISION")
                if existing.state == "accepted" and existing.order_id:
                    return existing.order_id
                raise PermissionError("DUPLICATE_OR_UNCERTAIN_OPERATION")
            if not self._reconciliation.can_dispatch(operation_key):
                raise PermissionError("RECONCILIATION_REQUIRED")
            self._reconciliation.record_dispatch(effect_id, operation_key)
            try:
                order_id = self._main_engine.send_order(request, self._binding.gateway)
            except Exception:
                self._reconciliation.record_timeout(effect_id)
                raise
            if not isinstance(order_id, str) or not order_id.strip():
                self._reconciliation.record_timeout(effect_id)
                raise RuntimeError("BROKER_ORDER_ID_INVALID")
            self._reconciliation.record_outcome(effect_id, "accepted", order_id=order_id)
            return order_id
