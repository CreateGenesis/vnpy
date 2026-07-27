from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

from blake3 import blake3
import pytest

from vnpy.agent_bridge.native_bridge import MODEL_DECISION_SCHEMA_ID, NativeModelBridge
from vnpy.event import Event
from vnpy.model_production.app_engine import BrokerSimulationCoordinator
from vnpy.model_production.broker_simulation import BrokerSimulationAuthority, GatewayBinding
from vnpy.model_production.broker_simulation_model_loop import BrokerSimulationModelLoop
from vnpy.model_production.engine import AuthoritativeDecisionEngine
from vnpy.model_production.execution import BrokerSimulationExecutor
from vnpy.model_production.journal import ModelProductionJournal
from vnpy.model_production.reconciliation import ReconciliationManager
from vnpy.model_production.safety import HardSafetyController
from vnpy.trader.constant import Direction, Exchange, Product
from vnpy.trader.event import EVENT_TICK
from vnpy.trader.object import AccountData, ContractData, PositionData, TickData


def digest(label: str) -> str:
    return f"sha256:{sha256(label.encode()).hexdigest()}"


class FakeNativeBridge:
    def __init__(self) -> None:
        self.inputs: list[dict[str, Any]] = []
        self.decisions: list[str] = []
        self.calls: list[tuple[Any, ...]] = []

    def publish_model_input(
        self,
        payload_json: bytes,
        correlation_id: str,
        event_time_ms: int,
        expiry_ms: int,
    ) -> int:
        payload = json.loads(payload_json)
        self.inputs.append(payload)
        self.calls.append(("publish_input", correlation_id, event_time_ms, expiry_ms))
        return len(self.inputs)

    def consume_model_decision(self, _now_ms: int) -> str | None:
        return self.decisions.pop(0) if self.decisions else None

    def ack_model_decision(self, *args: Any) -> None:
        self.calls.append(("ack_decision", *args))

    def replay_model_input_pending(self) -> int:
        self.calls.append(("replay_inputs",))
        return 0


class EventEngineStub:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}

    def register(self, event_type: str, handler: Any) -> None:
        self.handlers[event_type] = handler

    def unregister(self, event_type: str, handler: Any) -> None:
        if self.handlers.get(event_type) == handler:
            del self.handlers[event_type]


class MainEngineStub:
    def __init__(self, *, fail_submission: bool = False) -> None:
        self.fail_submission = fail_submission
        self.calls: list[tuple[object, str]] = []
        self.account = AccountData(
            gateway_name="XTP",
            accountid="simulation",
            balance=1_000_000,
            frozen=0,
        )
        self.position = PositionData(
            gateway_name="XTP",
            symbol="600000",
            exchange=Exchange.SSE,
            direction=Direction.LONG,
            volume=200,
            yd_volume=100,
            price=10,
        )
        self.contract = ContractData(
            gateway_name="XTP",
            symbol="600000",
            exchange=Exchange.SSE,
            name="Pudong Bank",
            product=Product.EQUITY,
            size=1,
            pricetick=0.01,
            min_volume=100,
        )

    def get_all_accounts(self) -> list[AccountData]:
        return [self.account]

    def get_all_positions(self) -> list[PositionData]:
        return [self.position]

    def get_contract(self, vt_symbol: str) -> ContractData | None:
        return self.contract if vt_symbol == "600000.SSE" else None

    def send_order(self, request: object, gateway_name: str) -> str:
        self.calls.append((request, gateway_name))
        if self.fail_submission:
            raise TimeoutError("broker outcome unknown")
        return f"{gateway_name}.order-{len(self.calls)}"


@dataclass
class Runtime:
    loop: BrokerSimulationModelLoop
    bridge: FakeNativeBridge
    main_engine: MainEngineStub
    events: EventEngineStub
    reconciliation: ReconciliationManager


def build_runtime(
    tmp_path: Path,
    *,
    now_ns: int = 1_000_000_000_000,
    fail_submission: bool = False,
) -> Runtime:
    database = tmp_path / "runtime.sqlite"
    server = digest("xtp-server")
    account = digest("xtp-account")
    binding = GatewayBinding.create(
        gateway="XTP",
        environment="broker_simulation",
        server_fingerprint=server,
        account_fingerprint=account,
        credential_ref="credential:xtp",
        process_identity="vnpy-demo-xtp",
        rpc_endpoint="127.0.0.1:17801",
        state_store_path=str(tmp_path),
        created_at_ms=1,
        allowed_server_fingerprints=frozenset({server}),
        allowed_account_fingerprints=frozenset({account}),
    )
    authority = BrokerSimulationAuthority(database)
    authority.create_campaign(
        campaign_id="campaign-1",
        candidate_digest=digest("candidate"),
        package_digest=digest("package"),
        configuration_digest=digest("configuration"),
        policy_digest=digest("policy"),
        symbol_set=("600000.SH",),
        calendar_sessions=(
            "2026-07-27",
            "2026-07-28",
            "2026-07-29",
            "2026-07-30",
            "2026-07-31",
        ),
        operator_identity_digest=digest("operator"),
        bindings=(binding,),
        lifecycle_revision=8,
        now_ms=1,
    )
    authority.start_campaign("campaign-1", now_ms=2)
    reconciliation = ReconciliationManager(database)
    safety = HardSafetyController()
    main_engine = MainEngineStub(fail_submission=fail_submission)
    coordinator = BrokerSimulationCoordinator(
        campaign_id="campaign-1",
        run_id="campaign-1:xtp",
        binding=binding,
        authority=authority,
        decision_engine=AuthoritativeDecisionEngine(
            journal=ModelProductionJournal(database),
            safety=safety,
            expected_producer_id="modeld:broker-xtp-slot",
            active_package_digest=digest("package"),
            lifecycle_revision=8,
            stage="broker_simulation",
        ),
        executor=BrokerSimulationExecutor(
            main_engine=main_engine,
            binding=binding,
            reconciliation=reconciliation,
        ),
        reconciliation=reconciliation,
        safety=safety,
    )
    native = FakeNativeBridge()
    events = EventEngineStub()
    loop = BrokerSimulationModelLoop(
        bridge=NativeModelBridge(native=native),
        coordinator=coordinator,
        reconciliation=reconciliation,
        event_engine=events,
        main_engine=main_engine,
        database=database,
        gateway="XTP",
        package_digest=digest("package"),
        configuration_digest=digest("configuration"),
        policy_digest=digest("policy"),
        runtime_slot="broker-xtp-slot",
        lifecycle_revision=8,
        symbols=("600000.SH",),
        now_ns=lambda: now_ns,
    )
    return Runtime(loop, native, main_engine, events, reconciliation)


def tick() -> TickData:
    return TickData(
        gateway_name="XTP",
        symbol="600000",
        exchange=Exchange.SSE,
        datetime=datetime(2026, 7, 27, 10, 0),
        last_price=10,
        bid_price_1=9.99,
        ask_price_1=10.01,
        volume=1_000,
        turnover=10_000,
        limit_down=9,
        limit_up=11,
        open_price=9.95,
        high_price=10.05,
        low_price=9.9,
        pre_close=9.9,
    )


def decision_delivery(
    model_input: dict[str, Any],
    *,
    decision_type: str = "order_intent",
    action: str = "buy",
    sequence: int = 1,
    expires_at_ns: int | None = None,
) -> str:
    producer = f"modeld:{model_input['runtime_slot']}"
    quantity = 100 if decision_type == "order_intent" else None
    price = 10_000_000 if decision_type == "order_intent" else None
    payload = {
        "contract_version": 1,
        "decision_id": f"decision-{model_input['input_sequence']}",
        "idempotency_id": f"intent-{model_input['input_sequence']}",
        "producer_id": producer,
        "package_digest": model_input["package_digest"],
        "lifecycle_revision": model_input["lifecycle_revision"],
        "stage": model_input["stage"],
        "input_sequence": model_input["input_sequence"],
        "context_digest": model_input["context_digest"],
        "state_revision": model_input["state_revision"],
        "decision_kind": decision_type,
        "action": action,
        "symbol": model_input["trigger"]["symbol"],
        "quantity": quantity,
        "limit_price_micros": price,
        "score": 0.9 if decision_type == "order_intent" else 0.0,
        "confidence": 0.9 if decision_type == "order_intent" else 0.0,
        "threshold": 0.6,
        "market_time_ns": model_input["features"]["market_time_ns"],
        "inference_latency_ns": 1_000,
        "expires_at_ns": expires_at_ns or model_input["deadline_ns"],
        "correlation_id": model_input["correlation_id"],
        "evidence_digest": digest(f"evidence-{model_input['input_sequence']}"),
    }
    return json.dumps(
        {
            "contract_version": 2,
            "frame_type": "model_decision",
            "schema_id": MODEL_DECISION_SCHEMA_ID,
            "producer_id": blake3(producer.encode()).digest()[:16].hex(),
            "producer_epoch": 11,
            "sequence": sequence,
            "correlation_id": blake3(payload["correlation_id"].encode()).digest()[:16].hex(),
            "event_time_ms": 1_000_000,
            "expiry_ms": payload["expires_at_ns"] // 1_000_000,
            "replayed": sequence > 1,
            "payload": payload,
        }
    )


def test_tick_to_modeld_to_authoritative_gateway_round_trip(tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path)
    runtime.loop.start(start_poller=False)

    runtime.events.handlers[EVENT_TICK](Event(EVENT_TICK, tick()))

    assert len(runtime.bridge.inputs) == 1
    model_input = runtime.bridge.inputs[0]
    assert set(model_input) == {
        "contract_version",
        "package_digest",
        "runtime_slot",
        "lifecycle_revision",
        "stage",
        "trigger",
        "features",
        "context_digest",
        "state_revision",
        "input_sequence",
        "deadline_ns",
        "correlation_id",
        "payload_digest",
    }
    assert model_input["features"]["values"][:3] == [10, 9.99, 10.01]
    assert model_input["trigger"]["symbol"] == "600000.SH"

    runtime.bridge.decisions.append(decision_delivery(model_input))
    delivery = runtime.loop.process_next_decision()

    assert delivery is not None
    assert len(runtime.main_engine.calls) == 1
    request, gateway = runtime.main_engine.calls[0]
    assert gateway == "XTP"
    assert request.reference == "model:intent-1"
    assert runtime.bridge.calls[-1][0] == "ack_decision"
    assert runtime.loop.snapshot().agent_calls == 0
    assert runtime.loop.snapshot().provider_calls == 0


def test_hold_and_stale_decisions_are_durable_and_never_reach_gateway(tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path)
    runtime.loop.on_tick(Event(EVENT_TICK, tick()))
    model_input = runtime.bridge.inputs[0]
    runtime.bridge.decisions.append(
        decision_delivery(model_input, decision_type="hold", action="hold")
    )
    runtime.loop.process_next_decision()

    runtime.loop.on_tick(Event(EVENT_TICK, tick()))
    stale_input = runtime.bridge.inputs[1]
    runtime.bridge.decisions.append(
        decision_delivery(stale_input, expires_at_ns=999_999_999_999, sequence=2)
    )
    runtime.loop.process_next_decision()

    assert runtime.main_engine.calls == []
    assert runtime.loop.decision_status("decision-1") == "hold"
    assert runtime.loop.decision_status("decision-2") == "risk_rejected"
    assert [call[0] for call in runtime.bridge.calls].count("ack_decision") == 2


def test_replay_is_idempotent_and_unknown_outcome_blocks_new_exposure(tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path, fail_submission=True)
    runtime.loop.on_tick(Event(EVENT_TICK, tick()))
    model_input = runtime.bridge.inputs[0]
    runtime.bridge.decisions.append(decision_delivery(model_input))
    runtime.loop.process_next_decision()

    assert len(runtime.main_engine.calls) == 1
    assert runtime.loop.decision_status("decision-1") == "broker_unknown"
    assert runtime.reconciliation.new_exposure_blocked
    assert runtime.bridge.calls[-1][0] == "ack_decision"

    runtime.bridge.decisions.append(decision_delivery(model_input, sequence=2))
    runtime.loop.process_next_decision()
    assert len(runtime.main_engine.calls) == 1

    runtime.loop.on_tick(Event(EVENT_TICK, tick()))
    blocked_input = runtime.bridge.inputs[1]
    runtime.bridge.decisions.append(decision_delivery(blocked_input, sequence=3))
    runtime.loop.process_next_decision()
    assert len(runtime.main_engine.calls) == 1
    assert runtime.loop.decision_status("decision-2") == "risk_rejected"


def test_malformed_or_identity_drifted_decision_is_not_acked(tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path)
    runtime.loop.on_tick(Event(EVENT_TICK, tick()))
    raw = json.loads(decision_delivery(runtime.bridge.inputs[0]))
    raw["payload"]["package_digest"] = digest("other-package")
    runtime.bridge.decisions.append(json.dumps(raw))

    with pytest.raises(ValueError, match="identity drift"):
        runtime.loop.process_next_decision()

    assert runtime.main_engine.calls == []
    assert not any(call[0] == "ack_decision" for call in runtime.bridge.calls)
