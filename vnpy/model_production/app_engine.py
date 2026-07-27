"""vn.py application shell and authoritative broker-simulation coordinator."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock
from time import monotonic_ns

from vnpy.event import EventEngine
from vnpy.trader.engine import BaseEngine, MainEngine

from .broker_simulation import (
    BrokerSimulationAuthority,
    BrokerSimulationCampaign,
    GatewayBinding,
)
from .engine import AuthoritativeDecisionEngine
from .execution import BrokerSimulationExecutor
from .reconciliation import BrokerOutcome, BrokerQueryResult, ReconciliationManager
from .risk import AuthoritativeRiskContext, ModelIntent, RiskDecision
from .safety import (
    BrokerSimulationContainment,
    ContainmentReceipt,
    HardSafetyController,
    HardSafetySnapshot,
)


APP_NAME = "ModelProduction"


@dataclass(frozen=True)
class ModelProductionSnapshot:
    """Minimal immutable setup state exposed before lifecycle implementation."""

    revision: int
    state: str
    authority: str


@dataclass(frozen=True)
class BrokerSimulationDispatchResult:
    """Authoritative disposition and optional broker identity for one intent."""

    risk: RiskDecision
    order_request: object | None
    order_id: str | None


class BrokerSimulationCoordinator:
    """Join campaign, risk, reconciliation, safety, and broker authority in vn.py."""

    def __init__(
        self,
        *,
        campaign_id: str,
        run_id: str,
        binding: GatewayBinding,
        authority: BrokerSimulationAuthority,
        decision_engine: AuthoritativeDecisionEngine,
        executor: BrokerSimulationExecutor,
        reconciliation: ReconciliationManager,
        safety: HardSafetyController,
        containment: BrokerSimulationContainment | None = None,
    ) -> None:
        if not campaign_id or not run_id:
            raise ValueError("BROKER_SIMULATION_RUN_IDENTITY_REQUIRED")
        self.campaign_id = campaign_id
        self.run_id = run_id
        self._binding = binding
        self._authority = authority
        self._decision_engine = decision_engine
        self._executor = executor
        self._reconciliation = reconciliation
        self._safety = safety
        self._containment = containment
        self._last_containment_receipt: ContainmentReceipt | None = None
        self._lock = RLock()

    def submit_intent(
        self,
        intent: ModelIntent,
        context: AuthoritativeRiskContext,
    ) -> BrokerSimulationDispatchResult:
        """Persist and evaluate an intent before the exact bound MainEngine call."""

        with self._lock, self._safety.admission_guard() as safety:
            if safety.active:
                raise PermissionError("HARD_SAFETY_ACTIVE")
            try:
                run = self._authority.require_active_run(
                    self.run_id,
                    self._binding.binding_digest,
                )
            except (KeyError, PermissionError) as exc:
                raise PermissionError("CAMPAIGN_NOT_ACCEPTING_EXPOSURE") from exc
            if self._reconciliation.new_exposure_blocked:
                raise PermissionError("RECONCILIATION_REQUIRED")

            decision = self._decision_engine.apply(intent, context)
            if not decision.risk.accepted or decision.order_request is None:
                return BrokerSimulationDispatchResult(
                    risk=decision.risk,
                    order_request=None,
                    order_id=None,
                )

            expected_state_matches = (
                run.stage == "broker_simulation"
                and run.lifecycle_revision == intent.lifecycle_revision
                and run.package_digest == intent.package_digest
            )
            order_id = self._executor.submit(
                stage=run.stage,
                request=decision.order_request,
                effect_id=f"broker-effect:{intent.intent_id}",
                operation_key=f"model-order:{intent.intent_id}",
                gateway_name=run.gateway,
                account_fingerprint=self._binding.account_fingerprint,
                risk=decision.risk,
                risk_persisted=True,
                expected_state_matches=expected_state_matches,
            )
            return BrokerSimulationDispatchResult(
                risk=decision.risk,
                order_request=decision.order_request,
                order_id=order_id,
            )

    def recover_uncertain(
        self,
        effect_id: str,
        query: Callable[[str, str | None], BrokerQueryResult | None],
    ) -> BrokerOutcome:
        """Recover only by querying the original operation identity."""

        if not callable(query):
            raise TypeError("broker query must be callable")
        with self._lock:
            return self._reconciliation.recover_uncertain(effect_id, query)

    def pause(self, *, now_ms: int) -> BrokerSimulationCampaign:
        with self._lock:
            campaign = self._authority.pause_campaign(self.campaign_id, now_ms=now_ms)
            if self._containment is not None:
                self._last_containment_receipt = self._containment.contain(
                    action="pause",
                    campaign_id=self.campaign_id,
                    detected_at_ns=monotonic_ns(),
                )
            return campaign

    def pause_with_receipt(
        self,
        *,
        now_ms: int,
        detected_at_ns: int,
    ) -> tuple[BrokerSimulationCampaign, ContainmentReceipt]:
        if self._containment is None:
            raise RuntimeError("CONTAINMENT_NOT_CONFIGURED")
        with self._lock:
            campaign = self._authority.pause_campaign(self.campaign_id, now_ms=now_ms)
            receipt = self._containment.contain(
                action="pause",
                campaign_id=self.campaign_id,
                detected_at_ns=detected_at_ns,
            )
            self._last_containment_receipt = receipt
            return campaign, receipt

    def emergency_stop(
        self,
        *,
        reason_code: str,
        evidence_digest: str,
        now_ns: int,
        now_ms: int,
    ) -> BrokerSimulationCampaign:
        with self._lock:
            safety = self._safety.activate(reason_code, "critical", evidence_digest, now_ns)
            campaign = self._authority.stop_campaign(self.campaign_id, now_ms=now_ms)
            if self._containment is not None:
                self._last_containment_receipt = self._containment.contain(
                    action="emergency_stop",
                    campaign_id=self.campaign_id,
                    detected_at_ns=now_ns,
                    exposure_blocked_at_ns=safety.activated_at_ns,
                )
            return campaign

    def emergency_stop_with_receipt(
        self,
        *,
        reason_code: str,
        evidence_digest: str,
        detected_at_ns: int,
        now_ns: int,
        now_ms: int,
    ) -> tuple[BrokerSimulationCampaign, ContainmentReceipt]:
        if self._containment is None:
            raise RuntimeError("CONTAINMENT_NOT_CONFIGURED")
        with self._lock:
            safety = self._safety.activate(reason_code, "critical", evidence_digest, now_ns)
            campaign = self._authority.stop_campaign(self.campaign_id, now_ms=now_ms)
            receipt = self._containment.contain(
                action="emergency_stop",
                campaign_id=self.campaign_id,
                detected_at_ns=detected_at_ns,
                exposure_blocked_at_ns=safety.activated_at_ns,
            )
            self._last_containment_receipt = receipt
            return campaign, receipt

    def last_containment_receipt(self) -> ContainmentReceipt | None:
        return self._last_containment_receipt

    def safety_snapshot(self) -> HardSafetySnapshot:
        return self._safety.snapshot()


class ModelProductionEngine(BaseEngine):
    """Own lifecycle and risk application inside the vn.py application process."""

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        super().__init__(main_engine, event_engine, APP_NAME)
        self._revision = 0
        self._state = "setup"
        self._broker_simulation: BrokerSimulationCoordinator | None = None

    def bind_broker_simulation(self, coordinator: BrokerSimulationCoordinator) -> None:
        """Install the single isolated run coordinator for this vn.py process."""

        if self._broker_simulation is not None:
            raise RuntimeError("BROKER_SIMULATION_ALREADY_BOUND")
        self._broker_simulation = coordinator
        self._revision += 1
        self._state = "broker_simulation_bound"

    def broker_simulation(self) -> BrokerSimulationCoordinator:
        if self._broker_simulation is None:
            raise RuntimeError("BROKER_SIMULATION_NOT_BOUND")
        return self._broker_simulation

    def snapshot(self) -> ModelProductionSnapshot:
        return ModelProductionSnapshot(
            revision=self._revision,
            state=self._state,
            authority="vnpy_lifecycle_risk_order",
        )
