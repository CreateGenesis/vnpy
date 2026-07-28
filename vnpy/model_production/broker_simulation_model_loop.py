"""Durable Tick-to-modeld-to-vn.py broker-simulation processing loop."""

from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path
import sqlite3
from threading import Event as ThreadEvent, RLock, Thread
from time import sleep, time_ns
from typing import Any, Callable

from blake3 import blake3

from vnpy.agent_bridge.native_bridge import ModelTransportDelivery, NativeModelBridge
from vnpy.event import Event
from vnpy.trader.event import EVENT_TICK

from .app_engine import BrokerSimulationCoordinator
from .reconciliation import ReconciliationManager
from .risk import AuthoritativeRiskContext, ModelIntent


_DECISION_FIELDS = frozenset(
    {
        "contract_version",
        "decision_id",
        "idempotency_id",
        "producer_id",
        "package_digest",
        "lifecycle_revision",
        "stage",
        "input_sequence",
        "context_digest",
        "state_revision",
        "decision_kind",
        "action",
        "symbol",
        "quantity",
        "limit_price_micros",
        "score",
        "confidence",
        "threshold",
        "market_time_ns",
        "inference_latency_ns",
        "expires_at_ns",
        "correlation_id",
        "evidence_digest",
    }
)
_EXCHANGE_SUFFIX = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}
_FEATURE_NAMES = (
    "last_price",
    "bid_price_1",
    "ask_price_1",
    "volume",
    "turnover",
    "open_interest",
    "open_price",
    "high_price",
    "low_price",
    "pre_close",
)
@dataclass(frozen=True)
class BrokerSimulationModelLoopSnapshot:
    """Redacted loop health with explicit zero-Agent fast-path counters."""

    state: str
    input_count: int
    decision_count: int
    broker_submission_count: int
    agent_calls: int
    provider_calls: int
    last_error: str | None


class BrokerSimulationModelLoop:
    """Route market data through modeld while retaining all authority in vn.py."""

    def __init__(
        self,
        *,
        bridge: NativeModelBridge,
        coordinator: BrokerSimulationCoordinator,
        reconciliation: ReconciliationManager,
        event_engine: EventEngine | Any,
        main_engine: Any,
        database: str | Path,
        gateway: str,
        package_digest: str,
        configuration_digest: str,
        policy_digest: str,
        runtime_slot: str,
        lifecycle_revision: int,
        symbols: tuple[str, ...],
        now_ns: Callable[[], int] = time_ns,
        session_open: Callable[[Any], bool] | None = None,
        poll_interval_seconds: float = 0.001,
    ) -> None:
        if gateway not in {"XTP", "TORA"}:
            raise ValueError("MODEL_LOOP_GATEWAY_INVALID")
        if (
            not _valid_digest(package_digest)
            or not _valid_digest(configuration_digest)
            or not _valid_digest(policy_digest)
            or not runtime_slot.strip()
            or lifecycle_revision <= 0
            or not symbols
            or any(not _valid_symbol(symbol) for symbol in symbols)
            or poll_interval_seconds <= 0
        ):
            raise ValueError("MODEL_LOOP_BINDING_INVALID")
        self._bridge = bridge
        self._coordinator = coordinator
        self._reconciliation = reconciliation
        self._event_engine = event_engine
        self._main_engine = main_engine
        self._database = str(database)
        self._gateway = gateway
        self._package_digest = package_digest
        self._configuration_digest = configuration_digest
        self._policy_digest = policy_digest
        self._runtime_slot = runtime_slot
        self._lifecycle_revision = lifecycle_revision
        self._symbols = frozenset(symbols)
        self._now_ns = now_ns
        self._session_open = session_open or _session_open
        self._poll_interval_seconds = poll_interval_seconds
        self._stop = ThreadEvent()
        self._thread: Thread | None = None
        self._started = False
        self._last_error: str | None = None
        self._lock = RLock()
        self._initialize()

    def start(self, *, start_poller: bool = True) -> None:
        """Recover the input outbox, register Tick handling, and poll decisions."""

        with self._lock:
            if self._started:
                return
            self._replay_unpublished_inputs()
            self._bridge.replay_input_pending()
            self._event_engine.register(EVENT_TICK, self.on_tick)
            self._started = True
            if start_poller:
                self._stop.clear()
                self._thread = Thread(
                    target=self._poll,
                    name=f"model-loop-{self._gateway.lower()}",
                    daemon=True,
                )
                self._thread.start()

    def close(self) -> None:
        with self._lock:
            if not self._started:
                return
            self._event_engine.unregister(EVENT_TICK, self.on_tick)
            self._started = False
            self._stop.set()
            thread = self._thread
            self._thread = None
        if thread is not None:
            thread.join(timeout=2)

    def on_tick(self, event: Event) -> None:
        """Persist and publish one eligible gateway Tick as a strict ModelInput."""

        tick = event.data
        if getattr(tick, "gateway_name", None) != self._gateway:
            return
        symbol = _model_symbol(tick)
        if symbol not in self._symbols:
            return
        now_ns = self._now_ns()
        if now_ns <= 0:
            raise ValueError("MODEL_LOOP_CLOCK_INVALID")
        input_payload, context_payload = self._build_input(tick, symbol, now_ns)
        encoded_input = _json_bytes(input_payload).decode("utf-8")
        encoded_context = _json_bytes(context_payload).decode("utf-8")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO broker_model_inputs(
                    input_sequence,context_digest,correlation_id,input_json,context_json,
                    publication_sequence,created_at_ns
                ) VALUES(?,?,?,?,?,NULL,?)""",
                (
                    input_payload["input_sequence"],
                    input_payload["context_digest"],
                    input_payload["correlation_id"],
                    encoded_input,
                    encoded_context,
                    now_ns,
                ),
            )
            connection.commit()
        publication_sequence = self._bridge.publish_model_input(
            input_payload,
            input_payload["correlation_id"],
            now_ns // 1_000_000,
            input_payload["deadline_ns"] // 1_000_000,
        )
        self._mark_input_published(input_payload["input_sequence"], publication_sequence)

    def process_next_decision(self) -> ModelTransportDelivery | None:
        """Apply one decision durably and ACK it only after final disposition."""

        now_ns = self._now_ns()
        return self._bridge.consume_model_decision(
            now_ns // 1_000_000,
            lambda delivery: self._apply_decision(delivery, now_ns),
            applied_at_ms=now_ns // 1_000_000,
        )

    def decision_status(self, decision_id: str) -> str | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT status FROM broker_model_decisions WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
        return str(row[0]) if row else None

    def snapshot(self) -> BrokerSimulationModelLoopSnapshot:
        with closing(self._connect()) as connection:
            input_count = connection.execute(
                "SELECT COUNT(*) FROM broker_model_inputs"
            ).fetchone()[0]
            decision_count = connection.execute(
                "SELECT COUNT(*) FROM broker_model_decisions"
            ).fetchone()[0]
            broker_count = connection.execute(
                "SELECT COUNT(*) FROM broker_model_decisions WHERE status='broker_submitted'"
            ).fetchone()[0]
        return BrokerSimulationModelLoopSnapshot(
            state="running" if self._started else "ready",
            input_count=input_count,
            decision_count=decision_count,
            broker_submission_count=broker_count,
            agent_calls=0,
            provider_calls=0,
            last_error=self._last_error,
        )

    def _poll(self) -> None:
        while not self._stop.is_set():
            try:
                delivery = self.process_next_decision()
                if delivery is None:
                    sleep(self._poll_interval_seconds)
                else:
                    self._last_error = None
            except Exception as exc:
                self._last_error = type(exc).__name__
                sleep(self._poll_interval_seconds)

    def _build_input(
        self, tick: Any, symbol: str, now_ns: int
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        with closing(self._connect()) as connection:
            input_sequence = connection.execute(
                "SELECT COALESCE(MAX(input_sequence),0)+1 FROM broker_model_inputs"
            ).fetchone()[0]
        deadline_ns = now_ns + 100_000_000
        market_time_ns = min(_datetime_ns(getattr(tick, "datetime", None), now_ns), now_ns)
        observation = {
            "gateway": self._gateway,
            "symbol": symbol,
            "market_time_ns": market_time_ns,
            "receive_time_ns": now_ns,
            "last_price": _finite_number(getattr(tick, "last_price", 0)),
            "volume": _finite_number(getattr(tick, "volume", 0)),
        }
        observation_digest = _digest_json(observation)
        feature_values = [
            _finite_number(getattr(tick, feature, 0)) for feature in _FEATURE_NAMES
        ]
        features = {
            "contract_version": 1,
            "schema_digest": _FEATURE_SCHEMA_DIGEST,
            "registry_digest": self._configuration_digest,
            "observation_digest": observation_digest,
            "symbol": symbol,
            "values": feature_values,
            "market_time_ns": market_time_ns,
            "expires_at_ns": deadline_ns,
            "evidence_digest": _digest_json(
                {"observation_digest": observation_digest, "values": feature_values}
            ),
        }
        feature_digest = _blake3_json(features)
        correlation_id = f"{self._gateway.lower()}-tick-{input_sequence}"
        trigger = {
            "contract_version": 1,
            "trigger_id": f"fast-action-{self._gateway.lower()}-{input_sequence}",
            "kind": "fast_action",
            "producer_id": f"vnpy:{self._gateway.lower()}:tick",
            "producer_epoch": 1,
            "sequence": input_sequence,
            "rule_version": "broker-simulation-fast-action-v1",
            "threshold_digest": self._policy_digest,
            "observation_digest": observation_digest,
            "feature_digest": feature_digest,
            "symbol": symbol,
            "priority": 255,
            "correlation_id": correlation_id,
            "deduplication_id": f"{self._gateway.lower()}:{input_sequence}",
            "deadline_ns": deadline_ns,
            "expires_at_ns": deadline_ns,
            "evidence_digest": _digest_json(
                {"observation_digest": observation_digest, "feature_digest": feature_digest}
            ),
        }
        context_payload = self._context_payload(
            tick=tick,
            symbol=symbol,
            input_sequence=input_sequence,
            market_time_ns=market_time_ns,
            now_ns=now_ns,
            expires_at_ns=deadline_ns,
        )
        context_digest = _digest_json(context_payload)
        payload_digest = _blake3_text(
            ":".join(
                (
                    self._package_digest,
                    str(self._lifecycle_revision),
                    trigger["trigger_id"],
                    feature_digest,
                    context_digest,
                    str(input_sequence),
                )
            )
        )
        model_input = {
            "contract_version": 1,
            "package_digest": self._package_digest,
            "runtime_slot": self._runtime_slot,
            "lifecycle_revision": self._lifecycle_revision,
            "stage": "broker_simulation",
            "trigger": trigger,
            "features": features,
            "context_digest": context_digest,
            "state_revision": input_sequence,
            "input_sequence": input_sequence,
            "deadline_ns": deadline_ns,
            "correlation_id": correlation_id,
            "payload_digest": payload_digest,
        }
        return model_input, context_payload

    def _context_payload(
        self,
        *,
        tick: Any,
        symbol: str,
        input_sequence: int,
        market_time_ns: int,
        now_ns: int,
        expires_at_ns: int,
    ) -> dict[str, Any]:
        accounts = [
            account
            for account in self._main_engine.get_all_accounts()
            if getattr(account, "gateway_name", None) == self._gateway
        ]
        positions: dict[str, int] = {}
        sellable: dict[str, int] = {}
        symbol_exposure: dict[str, int] = {}
        for position in self._main_engine.get_all_positions():
            if getattr(position, "gateway_name", None) != self._gateway:
                continue
            position_symbol = _model_symbol(position)
            quantity = max(0, round(_finite_number(getattr(position, "volume", 0))))
            positions[position_symbol] = positions.get(position_symbol, 0) + quantity
            available = max(0, round(_finite_number(getattr(position, "yd_volume", 0))))
            sellable[position_symbol] = sellable.get(position_symbol, 0) + min(
                quantity, available
            )
            price = _finite_number(getattr(position, "price", 0))
            symbol_exposure[position_symbol] = (
                symbol_exposure.get(position_symbol, 0)
                + quantity * _price_micros(price)
            )
        vt_symbol = str(getattr(tick, "vt_symbol", ""))
        contract = self._main_engine.get_contract(vt_symbol)
        lot_size = max(1, round(_finite_number(getattr(contract, "min_volume", 100))))
        cash_micros = sum(
            _price_micros(_finite_number(getattr(account, "available", 0)))
            for account in accounts
        )
        nav_micros = sum(
            _price_micros(_finite_number(getattr(account, "balance", 0)))
            for account in accounts
        )
        return {
            "context_id": f"{self._gateway.lower()}-context-{input_sequence}",
            "revision": input_sequence,
            "package_digest": self._package_digest,
            "lifecycle_revision": self._lifecycle_revision,
            "stage": "broker_simulation",
            "created_at_ns": now_ns,
            "expires_at_ns": expires_at_ns,
            "market_time_ns": market_time_ns,
            "session_open": self._session_open(getattr(tick, "datetime", None)),
            "suspended_symbols": [],
            "lower_limit_micros": {symbol: _price_micros(getattr(tick, "limit_down", 0))},
            "upper_limit_micros": {symbol: _price_micros(getattr(tick, "limit_up", 0))},
            "lot_sizes": {symbol: lot_size},
            "cash_micros": cash_micros,
            "positions": positions,
            "t1_sellable": sellable,
            "reconciled": not self._reconciliation.new_exposure_blocked,
            "unknown_outcomes": list(self._reconciliation.unresolved_effect_ids()),
            "eligible_symbols": sorted(self._symbols),
            "nav_micros": nav_micros,
            "gross_exposure_micros": sum(symbol_exposure.values()),
            "symbol_exposure_micros": symbol_exposure,
            "operations_last_second": self._operation_count(now_ns - 1_000_000_000),
            "operations_this_session": self._operation_count(0),
        }

    def _apply_decision(self, delivery: ModelTransportDelivery, now_ns: int) -> bool:
        if delivery.recovery_complete:
            self._persist_decision(
                decision_id=f"recovery-{delivery.producer_id}-{delivery.sequence}",
                payload={"recovery_complete": True},
                decision_kind="recovery_complete",
                status="recovery_complete",
                risk=None,
                order_id=None,
                error_code=None,
                applied_at_ns=now_ns,
            )
            return True
        payload = delivery.payload
        if not isinstance(payload, dict):
            raise ValueError("model decision payload missing")
        _, context = self._validate_decision(delivery, payload, now_ns)
        decision_id = payload["decision_id"]
        existing = self._existing_decision(decision_id)
        encoded = _json_bytes(payload).decode("utf-8")
        if existing is not None:
            if existing[0] != encoded:
                raise ValueError("model decision identity drift")
            return True
        kind = payload["decision_kind"]
        if kind in {"hold", "no_action", "agent_interest"} or payload["action"] in {
            "hold",
            "no_action",
            "cancel_intent",
        }:
            self._persist_decision(
                decision_id=decision_id,
                payload=payload,
                decision_kind=kind,
                status="hold" if kind == "hold" else "no_effect",
                risk=None,
                order_id=None,
                error_code=None,
                applied_at_ns=now_ns,
            )
            return True

        intent = ModelIntent(
            intent_id=payload["idempotency_id"],
            decision_id=decision_id,
            producer_id=payload["producer_id"],
            package_digest=payload["package_digest"],
            lifecycle_revision=payload["lifecycle_revision"],
            stage=payload["stage"],
            context_id=context.context_id,
            context_revision=context.revision,
            symbol=payload["symbol"],
            action=payload["action"],
            quantity=payload["quantity"],
            limit_price_micros=payload["limit_price_micros"],
            expires_at_ns=payload["expires_at_ns"],
        )
        try:
            result = self._coordinator.submit_intent(intent, context)
        except PermissionError as exc:
            self._persist_decision(
                decision_id=decision_id,
                payload=payload,
                decision_kind=kind,
                status="risk_rejected",
                risk=None,
                order_id=None,
                error_code=str(exc),
                applied_at_ns=now_ns,
            )
            return True
        except Exception:
            outcome = self._reconciliation.outcome_for_operation(
                f"model-order:{intent.intent_id}"
            )
            if outcome is None or outcome.state not in {"unknown", "dispatched"}:
                raise
            self._persist_decision(
                decision_id=decision_id,
                payload=payload,
                decision_kind=kind,
                status="broker_unknown",
                risk=None,
                order_id=outcome.order_id,
                error_code="BROKER_OUTCOME_UNKNOWN",
                applied_at_ns=now_ns,
            )
            return True
        self._persist_decision(
            decision_id=decision_id,
            payload=payload,
            decision_kind=kind,
            status="broker_submitted" if result.order_id else "risk_rejected",
            risk=asdict(result.risk),
            order_id=result.order_id,
            error_code=None,
            applied_at_ns=now_ns,
        )
        return True

    def _validate_decision(
        self,
        delivery: ModelTransportDelivery,
        payload: dict[str, Any],
        now_ns: int,
    ) -> tuple[dict[str, Any], AuthoritativeRiskContext]:
        if set(payload) != _DECISION_FIELDS or payload.get("contract_version") != 1:
            raise ValueError("model decision contract invalid")
        for field in (
            "lifecycle_revision",
            "input_sequence",
            "state_revision",
            "market_time_ns",
            "inference_latency_ns",
            "expires_at_ns",
        ):
            _strict_int(payload[field], field, positive=True)
        for field in ("score", "confidence", "threshold"):
            value = payload[field]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
                raise ValueError(f"model decision {field} invalid")
        if not 0 <= payload["confidence"] <= 1 or not 0 <= payload["threshold"] <= 1:
            raise ValueError("model decision score bounds invalid")
        for field in ("decision_id", "idempotency_id", "correlation_id"):
            if not isinstance(payload[field], str) or not payload[field].strip():
                raise ValueError(f"model decision {field} invalid")
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT input_json,context_json FROM broker_model_inputs
                   WHERE context_digest=? AND input_sequence=?""",
                (payload["context_digest"], payload["input_sequence"]),
            ).fetchone()
        if row is None:
            raise ValueError("model decision context unknown")
        model_input = json.loads(row[0])
        expected_producer = f"modeld:{self._runtime_slot}"
        identity_matches = (
            payload["producer_id"] == expected_producer
            and payload["package_digest"] == self._package_digest
            and payload["lifecycle_revision"] == self._lifecycle_revision
            and payload["stage"] == "broker_simulation"
            and payload["state_revision"] == model_input["state_revision"]
            and payload["symbol"] == model_input["trigger"]["symbol"]
            and payload["market_time_ns"] == model_input["features"]["market_time_ns"]
            and payload["correlation_id"] == model_input["correlation_id"]
            and _valid_digest(payload["evidence_digest"])
            and delivery.producer_id == blake3(expected_producer.encode()).digest()[:16].hex()
            and delivery.correlation_id
            == blake3(payload["correlation_id"].encode()).digest()[:16].hex()
            and delivery.expiry_ms == payload["expires_at_ns"] // 1_000_000
        )
        if not identity_matches:
            raise ValueError("model decision identity drift")
        if payload["expires_at_ns"] > model_input["deadline_ns"]:
            raise ValueError("model decision expiry drift")
        kind = payload["decision_kind"]
        action = payload["action"]
        if kind == "order_intent":
            if action not in {"buy", "sell", "reduce", "close", "cancel_intent"}:
                raise ValueError("model decision action invalid")
            if action == "cancel_intent":
                if payload["quantity"] is not None:
                    raise ValueError("model decision quantity invalid")
            else:
                _strict_int(payload["quantity"], "quantity", positive=True)
                _strict_int(
                    payload["limit_price_micros"],
                    "limit_price_micros",
                    positive=True,
                )
        elif kind == "hold":
            if action != "hold" or payload["quantity"] is not None:
                raise ValueError("model decision hold invalid")
        elif kind == "no_action":
            if action != "no_action" or payload["quantity"] is not None:
                raise ValueError("model decision no-action invalid")
        elif kind == "agent_interest":
            if action != "no_action" or payload["quantity"] is not None:
                raise ValueError("model decision interest invalid")
        else:
            raise ValueError("model decision kind invalid")
        context_payload = json.loads(row[1])
        context = _risk_context(context_payload, now_ns)
        return model_input, context

    def _existing_decision(self, decision_id: str) -> tuple[str, str] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT payload_json,status FROM broker_model_decisions WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
        return (str(row[0]), str(row[1])) if row else None

    def _persist_decision(
        self,
        *,
        decision_id: str,
        payload: dict[str, Any],
        decision_kind: str,
        status: str,
        risk: dict[str, Any] | None,
        order_id: str | None,
        error_code: str | None,
        applied_at_ns: int,
    ) -> None:
        encoded = _json_bytes(payload).decode("utf-8")
        risk_json = _json_bytes(risk).decode("utf-8") if risk is not None else None
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT payload_json FROM broker_model_decisions WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
            if existing is not None:
                if existing[0] != encoded:
                    raise ValueError("model decision identity drift")
                connection.commit()
                return
            connection.execute(
                """INSERT INTO broker_model_decisions(
                    decision_id,payload_json,decision_kind,status,risk_json,order_id,
                    error_code,applied_at_ns
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    decision_id,
                    encoded,
                    decision_kind,
                    status,
                    risk_json,
                    order_id,
                    error_code,
                    applied_at_ns,
                ),
            )
            connection.commit()

    def _operation_count(self, since_ns: int) -> int:
        with closing(self._connect()) as connection:
            return int(
                connection.execute(
                    """SELECT COUNT(*) FROM broker_model_decisions
                       WHERE decision_kind='order_intent' AND applied_at_ns>=?""",
                    (since_ns,),
                ).fetchone()[0]
            )

    def _replay_unpublished_inputs(self) -> None:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT input_sequence,input_json,correlation_id,created_at_ns
                   FROM broker_model_inputs WHERE publication_sequence IS NULL
                   ORDER BY input_sequence"""
            ).fetchall()
        for input_sequence, encoded, correlation_id, created_at_ns in rows:
            payload = json.loads(encoded)
            publication_sequence = self._bridge.publish_model_input(
                payload,
                correlation_id,
                created_at_ns // 1_000_000,
                payload["deadline_ns"] // 1_000_000,
            )
            self._mark_input_published(input_sequence, publication_sequence)

    def _mark_input_published(self, input_sequence: int, publication_sequence: int) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """UPDATE broker_model_inputs SET publication_sequence=?
                   WHERE input_sequence=? AND publication_sequence IS NULL""",
                (publication_sequence, input_sequence),
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database, timeout=5)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS broker_model_inputs (
                    input_sequence INTEGER PRIMARY KEY CHECK(input_sequence > 0),
                    context_digest TEXT NOT NULL UNIQUE,
                    correlation_id TEXT NOT NULL UNIQUE,
                    input_json TEXT NOT NULL,
                    context_json TEXT NOT NULL,
                    publication_sequence INTEGER,
                    created_at_ns INTEGER NOT NULL CHECK(created_at_ns > 0)
                );
                CREATE TABLE IF NOT EXISTS broker_model_decisions (
                    decision_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    decision_kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    risk_json TEXT,
                    order_id TEXT,
                    error_code TEXT,
                    applied_at_ns INTEGER NOT NULL CHECK(applied_at_ns > 0)
                );
                """
            )
            connection.commit()


def _risk_context(payload: dict[str, Any], now_ns: int) -> AuthoritativeRiskContext:
    return AuthoritativeRiskContext(
        context_id=payload["context_id"],
        revision=payload["revision"],
        package_digest=payload["package_digest"],
        lifecycle_revision=payload["lifecycle_revision"],
        stage=payload["stage"],
        now_ns=now_ns,
        expires_at_ns=payload["expires_at_ns"],
        session_open=payload["session_open"],
        suspended_symbols=frozenset(payload["suspended_symbols"]),
        lower_limit_micros=payload["lower_limit_micros"],
        upper_limit_micros=payload["upper_limit_micros"],
        lot_sizes=payload["lot_sizes"],
        cash_micros=payload["cash_micros"],
        positions=payload["positions"],
        t1_sellable=payload["t1_sellable"],
        reconciled=payload["reconciled"],
        unknown_outcomes=frozenset(payload["unknown_outcomes"]),
        quote_age_ns=max(0, now_ns - payload["market_time_ns"]),
        eligible_symbols=frozenset(payload["eligible_symbols"]),
        nav_micros=payload["nav_micros"],
        gross_exposure_micros=payload["gross_exposure_micros"],
        symbol_exposure_micros=payload["symbol_exposure_micros"],
        operations_last_second=payload["operations_last_second"],
        operations_this_session=payload["operations_this_session"],
    )


def _model_symbol(value: Any) -> str:
    ticker = str(getattr(value, "symbol", ""))
    exchange = getattr(getattr(value, "exchange", None), "value", "")
    try:
        suffix = _EXCHANGE_SUFFIX[exchange]
    except KeyError as exc:
        raise ValueError("MODEL_LOOP_EXCHANGE_INVALID") from exc
    symbol = f"{ticker}.{suffix}"
    if not _valid_symbol(symbol):
        raise ValueError("MODEL_LOOP_SYMBOL_INVALID")
    return symbol


def _session_open(value: Any) -> bool:
    if not isinstance(value, datetime) or value.weekday() >= 5:
        return False
    minute = value.hour * 60 + value.minute
    return 570 <= minute <= 690 or 780 <= minute <= 900


def _datetime_ns(value: Any, fallback: int) -> int:
    if not isinstance(value, datetime):
        return fallback
    try:
        result = int(value.timestamp() * 1_000_000_000)
    except (OSError, OverflowError, ValueError):
        return fallback
    return result if result > 0 else fallback


def _finite_number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise ValueError("MODEL_LOOP_NONFINITE_MARKET_VALUE")
    return float(value)


def _price_micros(value: Any) -> int:
    number = _finite_number(value)
    if number <= 0:
        return 0
    result = round(number * 1_000_000)
    if result <= 0 or result > 9_223_372_036_854_775_807:
        raise ValueError("MODEL_LOOP_PRICE_INVALID")
    return result


def _strict_int(value: Any, field: str, *, positive: bool) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"model decision {field} invalid")
    if positive and value <= 0:
        raise ValueError(f"model decision {field} invalid")
    return value


def _valid_digest(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        algorithm, encoded = value.split(":", 1)
    except ValueError:
        return False
    return (
        algorithm in {"sha256", "blake3"}
        and len(encoded) == 64
        and all(character in "0123456789abcdefABCDEF" for character in encoded)
    )


def _valid_symbol(value: str) -> bool:
    try:
        ticker, exchange = value.split(".", 1)
    except ValueError:
        return False
    return len(ticker) == 6 and ticker.isascii() and ticker.isdigit() and exchange in {
        "SH",
        "SZ",
        "BJ",
    }


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=False,
    ).encode("utf-8")


def _digest_json(value: Any) -> str:
    return f"sha256:{sha256(_json_bytes(value)).hexdigest()}"


def _blake3_json(value: Any) -> str:
    return f"blake3:{blake3(_json_bytes(value)).hexdigest()}"


def _blake3_text(value: str) -> str:
    return f"blake3:{blake3(value.encode()).hexdigest()}"


_FEATURE_SCHEMA_DIGEST = _digest_json(
    {"contract_version": 1, "features": list(_FEATURE_NAMES)}
)
