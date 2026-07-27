from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from vnpy.agent_bridge.native_bridge import NativeModelBridge
from vnpy.demo_web.run_service import BrokerSimulationRunHost
from vnpy.event import Event
from vnpy.trader.constant import Exchange
from vnpy.trader.event import EVENT_TICK
from vnpy.trader.object import TickData


def digest(label: str) -> str:
    return f"sha256:{sha256(label.encode()).hexdigest()}"


@dataclass
class Account:
    gateway_name: str = "XTP"
    balance: float = 1_000_000.0
    available: float = 1_000_000.0


@dataclass
class Contract:
    min_volume: float = 100


class FakeEventEngine:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}

    def register(self, event_type: str, handler: Any) -> None:
        self.handlers[event_type] = handler

    def unregister(self, event_type: str, handler: Any) -> None:
        if self.handlers.get(event_type) == handler:
            del self.handlers[event_type]

    def emit(self, event_type: str, data: Any) -> None:
        handler = self.handlers.get(event_type)
        if handler is not None:
            handler(Event(event_type, data))


class FakeNativeBridge:
    def __init__(self) -> None:
        self.inputs: list[dict[str, Any]] = []

    def publish_model_input(
        self,
        payload_json: bytes,
        _correlation_id: str,
        _event_time_ms: int,
        _expiry_ms: int,
    ) -> int:
        self.inputs.append(json.loads(payload_json))
        return len(self.inputs)

    def consume_model_decision(self, _now_ms: int) -> None:
        return None

    def ack_model_decision(self, *_args: Any) -> None:
        return None

    def replay_model_input_pending(self) -> int:
        return 0


class FakeMainEngine:
    def __init__(self) -> None:
        self.event_engine = FakeEventEngine()
        self.submissions: list[tuple[Any, str]] = []

    def get_all_accounts(self) -> list[Account]:
        return [Account()]

    def get_all_positions(self) -> list[Any]:
        return []

    def get_all_trades(self) -> list[Any]:
        return []

    def get_all_active_orders(self) -> list[Any]:
        return []

    def get_contract(self, vt_symbol: str) -> Contract | None:
        return Contract() if vt_symbol == "600000.SSE" else None

    def send_order(self, request: Any, gateway_name: str) -> str:
        self.submissions.append((request, gateway_name))
        return f"{gateway_name}.simulation-{len(self.submissions)}"


def test_run_host_owns_prepare_start_pause_and_stop_without_agent_calls(
    tmp_path: Path,
) -> None:
    install_state(tmp_path)
    main_engine = FakeMainEngine()
    native = FakeNativeBridge()
    host = BrokerSimulationRunHost(
        tmp_path,
        "XTP",
        main_engine=main_engine,
        model_bridge=NativeModelBridge(native=native),
    )
    campaign_id = "b53bc59c-c626-4f16-8a3e-a3185c7dad23"
    campaign_digest = digest("campaign")
    common = {
        "contract_version": 1,
        "gateway": "XTP",
        "run_digest": host.run_digest,
    }

    prepared = host.handle(
        "run.prepare_campaign.v1",
        {
            **common,
            "campaign_id": campaign_id,
            "campaign_digest": campaign_digest,
            "candidate_digest": digest("candidate"),
            "idempotency_key": "prepare-campaign-0001",
        },
    )
    started = host.handle(
        "run.start_campaign.v1",
        {
            **common,
            "campaign_id": campaign_id,
            "campaign_digest": campaign_digest,
            "idempotency_key": "start-campaign-0001",
        },
    )
    main_engine.event_engine.emit(EVENT_TICK, tick())
    status = host.handle("run.status.v1", common)
    paused = host.handle(
        "run.pause_campaign.v1",
        {
            **common,
            "campaign_digest": campaign_digest,
            "idempotency_key": "pause-campaign-0001",
        },
    )
    stopped = host.handle(
        "run.emergency_stop.v1",
        {**common, "idempotency_key": "stop-campaign-0001"},
    )

    assert prepared["state"] == "prepared"
    assert started["state"] == "active"
    assert status["data"]["connection_state"] == "connected"
    assert status["data"]["reconciliation_state"] == "blocked"
    assert "SIGNED_FEE_LEDGER_UNAVAILABLE" in status["data"]["incidents"]
    assert status["data"]["model_state"] == "running"
    assert status["data"]["model_inputs"] == 1
    assert status["data"]["model_decisions"] == 0
    assert status["data"]["agent_calls"] == 0
    assert status["data"]["provider_calls"] == 0
    assert status["data"]["model_last_error"] is None
    assert paused["state"] == "contained"
    assert stopped["state"] == "contained"
    main_engine.event_engine.emit(EVENT_TICK, tick())
    assert len(native.inputs) == 1
    serialized = json.dumps([prepared, started, status, paused, stopped]).lower()
    for forbidden in (
        "account_id",
        "credential_ref",
        "order_request",
        "send_order",
        "cancel_order",
        "risk_mutation",
        "lifecycle_apply",
    ):
        assert forbidden not in serialized
    host.close()


def test_run_host_rejects_gateway_settings_that_drift_from_approved_binding(
    tmp_path: Path,
) -> None:
    install_state(tmp_path)
    settings_path = tmp_path / ".demo-secrets" / "xtp-settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings["交易地址"] = "changed.example.invalid"
    settings_path.write_text(json.dumps(settings), encoding="utf-8")

    try:
        BrokerSimulationRunHost(
            tmp_path,
            "XTP",
            main_engine=FakeMainEngine(),
            model_bridge=NativeModelBridge(native=FakeNativeBridge()),
        )
    except ValueError as exc:
        assert str(exc) == "RUN_BINDING_SETTINGS_DRIFT"
    else:
        raise AssertionError("gateway settings drift was accepted")


def test_active_run_restores_model_loop_after_host_restart(tmp_path: Path) -> None:
    install_state(tmp_path)
    campaign_id = "b53bc59c-c626-4f16-8a3e-a3185c7dad23"
    campaign_digest = digest("campaign")

    first_engine = FakeMainEngine()
    first_host = BrokerSimulationRunHost(
        tmp_path,
        "XTP",
        main_engine=first_engine,
        model_bridge=NativeModelBridge(native=FakeNativeBridge()),
    )
    common = {
        "contract_version": 1,
        "gateway": "XTP",
        "run_digest": first_host.run_digest,
    }
    first_host.handle(
        "run.prepare_campaign.v1",
        {
            **common,
            "campaign_id": campaign_id,
            "campaign_digest": campaign_digest,
            "candidate_digest": digest("candidate"),
            "idempotency_key": "prepare-campaign-0001",
        },
    )
    first_host.handle(
        "run.start_campaign.v1",
        {
            **common,
            "campaign_id": campaign_id,
            "campaign_digest": campaign_digest,
            "idempotency_key": "start-campaign-0001",
        },
    )
    first_host.close()

    second_engine = FakeMainEngine()
    second_native = FakeNativeBridge()
    second_host = BrokerSimulationRunHost(
        tmp_path,
        "XTP",
        main_engine=second_engine,
        model_bridge=NativeModelBridge(native=second_native),
    )
    second_engine.event_engine.emit(EVENT_TICK, tick())
    status = second_host.handle("run.status.v1", common)

    assert len(second_native.inputs) == 1
    assert status["data"]["model_state"] == "running"
    assert status["data"]["model_inputs"] == 1
    assert status["data"]["model_last_error"] is None
    second_host.handle(
        "run.pause_campaign.v1",
        {
            **common,
            "campaign_digest": campaign_digest,
            "idempotency_key": "pause-campaign-0001",
        },
    )
    second_host.close()


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


def install_state(root: Path) -> None:
    state = root / ".demo-state"
    secrets = root / ".demo-secrets"
    state.mkdir()
    secrets.mkdir()
    candidate = {
        "contract_version": 1,
        "ready": True,
        "candidate_digest": digest("candidate"),
        "author_lineage_digest": digest("author"),
        "package_digest": digest("package"),
        "configuration_digest": digest("configuration"),
        "policy_digest": digest("policy"),
        "symbols": ["600000.SH"],
        "calendar_sessions": [
            "2026-07-27",
            "2026-07-28",
            "2026-07-29",
            "2026-07-30",
            "2026-07-31",
        ],
        "lifecycle_revision": 1,
    }
    settings = {
        "账号": "simulation-account",
        "行情地址": "quote.simulation.invalid",
        "行情端口": 10001,
        "交易地址": "trade.simulation.invalid",
        "交易端口": 10002,
    }
    bindings = [
        {
            "name": "XTP",
            "environment": "broker_simulation",
            "server_fingerprint": payload_digest(
                {
                    "行情地址": settings["行情地址"],
                    "行情端口": settings["行情端口"],
                    "交易地址": settings["交易地址"],
                    "交易端口": settings["交易端口"],
                }
            ),
            "account_fingerprint": payload_digest({"账号": settings["账号"]}),
            "credential_ref": ".demo-secrets/xtp-settings.json",
        }
    ]
    operator = {"contract_version": 1, "operator_identity_digest": digest("operator")}
    (state / "ready-candidate.json").write_text(json.dumps(candidate), encoding="utf-8")
    (secrets / "gateway-bindings.json").write_text(json.dumps(bindings), encoding="utf-8")
    (secrets / "operator.json").write_text(json.dumps(operator), encoding="utf-8")
    (secrets / "xtp-settings.json").write_text(json.dumps(settings), encoding="utf-8")


def payload_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{sha256(encoded).hexdigest()}"
