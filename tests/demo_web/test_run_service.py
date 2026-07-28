from __future__ import annotations

from base64 import b64encode
import json
import os
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from vnpy.agent_bridge.native_bridge import NativeModelBridge
from vnpy.demo_web.run_service import BrokerSimulationRunHost, _consume_one_use_secret
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
        self.connections: list[tuple[dict[str, str | int], str]] = []
        self.closed_gateways: list[str] = []

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

    def connect(self, settings: dict[str, str | int], gateway_name: str) -> None:
        self.connections.append((settings, gateway_name))

    def close_gateway(self, gateway_name: str) -> None:
        self.closed_gateways.append(gateway_name)


def test_run_host_owns_prepare_start_pause_and_stop_without_agent_calls(
    tmp_path: Path,
) -> None:
    install_state(tmp_path)
    main_engine = FakeMainEngine()
    native = FakeNativeBridge()
    host = make_host(tmp_path, main_engine=main_engine, native=native)
    campaign_id = "b53bc59c-c626-4f16-8a3e-a3185c7dad23"
    campaign_digest = digest("campaign")

    prepared = host.handle(
        "run.prepare_campaign.v1",
        {
            **base(host),
            "campaign_id": campaign_id,
            "campaign_digest": campaign_digest,
            "candidate_digest": digest("candidate"),
            "idempotency_key": "prepare-campaign-0001",
        },
    )
    started = host.handle(
        "run.start_campaign.v1",
        {
            **base(host),
            "campaign_id": campaign_id,
            "campaign_digest": campaign_digest,
            "idempotency_key": "start-campaign-0001",
        },
    )
    main_engine.event_engine.emit(EVENT_TICK, tick())
    status = host.handle("run.status.v1", base(host))
    paused = host.handle(
        "run.pause_campaign.v1",
        {
            **base(host),
            "campaign_digest": campaign_digest,
            "idempotency_key": "pause-campaign-0001",
        },
    )
    stopped = host.handle(
        "run.emergency_stop.v1",
        {**base(host), "idempotency_key": "stop-campaign-0001"},
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


def test_one_use_gateway_secret_is_consumed_without_plaintext_persistence(
    tmp_path: Path,
) -> None:
    install_state(tmp_path, candidate=False)
    payload = one_use_payload()
    os.environ["AUTO_TRADE_ONE_USE_SECRET"] = b64encode(
        json.dumps(payload).encode("utf-8")
    ).decode("ascii")

    launch = _consume_one_use_secret("XTP")
    host = BrokerSimulationRunHost(
        tmp_path,
        "XTP",
        **launch,
        main_engine=FakeMainEngine(),
        model_bridge=NativeModelBridge(native=FakeNativeBridge()),
    )

    assert "AUTO_TRADE_ONE_USE_SECRET" not in os.environ
    assert host.handle("run.gateway_health.v1", base(host))["state"] == "connected"
    retained = b"\n".join(
        path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    )
    assert b"write-only-password" not in retained
    assert b"write-only-auth-code" not in retained
    assert not (tmp_path / ".demo-secrets" / "gateway-bindings.json").exists()
    assert not (tmp_path / ".demo-secrets" / "xtp-settings.json").exists()
    host.close()


def test_gateway_connects_without_candidate_and_campaign_prepare_fails_closed(
    tmp_path: Path,
) -> None:
    install_state(tmp_path, candidate=False)
    host = make_host(tmp_path)

    health = host.handle("run.gateway_health.v1", base(host))
    prepared = host.handle(
        "run.prepare_campaign.v1",
        {
            **base(host),
            "campaign_id": "b53bc59c-c626-4f16-8a3e-a3185c7dad23",
            "campaign_digest": digest("campaign"),
            "candidate_digest": digest("candidate"),
            "idempotency_key": "prepare-campaign-0001",
        },
    )

    assert health["state"] == "connected"
    assert prepared["state"] == "blocked"
    assert prepared["data"] == {"error_code": "RUN_CANDIDATE_NOT_READY"}
    host.close()


def test_gateway_reconnect_and_drain_shutdown_are_bounded_and_do_not_resend(
    tmp_path: Path,
) -> None:
    install_state(tmp_path, candidate=False)
    main_engine = FakeMainEngine()
    host = make_host(tmp_path, main_engine=main_engine)

    reconnected = host.handle(
        "run.reconnect.v1",
        {**base(host), "idempotency_key": "reconnect-gateway-0001"},
    )
    drained = host.handle(
        "run.drain_shutdown.v1",
        {**base(host), "idempotency_key": "drain-shutdown-0001"},
    )

    assert reconnected["state"] == "connected"
    assert main_engine.closed_gateways == ["XTP", "XTP"]
    assert len(main_engine.connections) == 1
    assert main_engine.submissions == []
    assert drained["state"] == "stopped"
    assert drained["data"]["reconciliation_state"] == "complete"
    assert host.shutdown_requested is True
    host.close()


def test_active_run_restores_model_loop_after_host_restart(tmp_path: Path) -> None:
    install_state(tmp_path)
    campaign_id = "b53bc59c-c626-4f16-8a3e-a3185c7dad23"
    campaign_digest = digest("campaign")

    first_engine = FakeMainEngine()
    first_host = make_host(tmp_path, main_engine=first_engine)
    first_host.handle(
        "run.prepare_campaign.v1",
        {
            **base(first_host),
            "campaign_id": campaign_id,
            "campaign_digest": campaign_digest,
            "candidate_digest": digest("candidate"),
            "idempotency_key": "prepare-campaign-0001",
        },
    )
    first_host.handle(
        "run.start_campaign.v1",
        {
            **base(first_host),
            "campaign_id": campaign_id,
            "campaign_digest": campaign_digest,
            "idempotency_key": "start-campaign-0001",
        },
    )
    first_host.close()

    second_engine = FakeMainEngine()
    second_native = FakeNativeBridge()
    second_host = make_host(
        tmp_path,
        main_engine=second_engine,
        native=second_native,
    )
    second_engine.event_engine.emit(EVENT_TICK, tick())
    status = second_host.handle("run.status.v1", base(second_host))

    assert len(second_native.inputs) == 1
    assert status["data"]["model_state"] == "running"
    assert status["data"]["model_inputs"] == 1
    assert status["data"]["model_last_error"] is None
    second_host.handle(
        "run.pause_campaign.v1",
        {
            **base(second_host),
            "campaign_digest": campaign_digest,
            "idempotency_key": "pause-campaign-0001",
        },
    )
    second_host.close()


def make_host(
    root: Path,
    *,
    main_engine: FakeMainEngine | None = None,
    native: FakeNativeBridge | None = None,
) -> BrokerSimulationRunHost:
    return BrokerSimulationRunHost(
        root,
        "XTP",
        **launch_settings(),
        main_engine=main_engine or FakeMainEngine(),
        model_bridge=NativeModelBridge(native=native or FakeNativeBridge()),
    )


def base(host: BrokerSimulationRunHost) -> dict[str, Any]:
    return {
        "contract_version": 1,
        "gateway": "XTP",
        "run_digest": host.run_digest,
    }


def launch_settings() -> dict[str, Any]:
    payload = one_use_payload()
    return {
        "configuration_version": payload["configuration_version"],
        "configuration_digest": payload["configuration_digest"],
        "operator_identity_digest": payload["operator_identity_digest"],
        "gateway_public": payload["public"],
        "gateway_secrets": payload["secrets"],
    }


def one_use_payload() -> dict[str, Any]:
    return {
        "contract_version": 1,
        "gateway": "XTP",
        "configuration_version": 1,
        "configuration_digest": digest("configuration"),
        "operator_identity_digest": digest("operator"),
        "public": {
            "account": "simulation-account",
            "client_id": 7,
            "quote_address": "quote.simulation.invalid",
            "quote_port": 10001,
            "trading_address": "trade.simulation.invalid",
            "trading_port": 10002,
            "quote_protocol": "TCP",
            "log_level": "INFO",
        },
        "secrets": {
            "password": "write-only-password",
            "authorization_code": "write-only-auth-code",
        },
    }


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


def install_state(root: Path, *, candidate: bool = True) -> None:
    state = root / ".demo-state"
    state.mkdir()
    candidate_value = {
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
    if candidate:
        (state / "ready-candidate.json").write_text(
            json.dumps(candidate_value), encoding="utf-8"
        )
