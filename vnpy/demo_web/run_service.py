"""Isolated vn.py broker-simulation run process with a fixed local IPC surface."""

from __future__ import annotations

import argparse
from hashlib import sha256
from importlib import import_module
import json
import os
from pathlib import Path
import re
from secrets import token_urlsafe
import socket
import struct
from time import monotonic_ns, time_ns
from typing import Any
from uuid import UUID

from vnpy.event import EventEngine
from vnpy.trader.engine import MainEngine

from vnpy.model_production.broker_simulation import (
    BrokerSimulationAuthority,
    GatewayBinding,
)
from vnpy.model_production.safety import BrokerSimulationContainment, HardSafetyController

from .runtime import DemoCandidate, _load_candidate, _load_operator_digest, _load_unique_json


_DIGEST = re.compile(r"^(?:sha256|blake3):[0-9a-f]{64}$")
_OPERATIONS = frozenset(
    {
        "run.status.v1",
        "run.evidence.v1",
        "run.prepare_campaign.v1",
        "run.start_campaign.v1",
        "run.pause_campaign.v1",
        "run.emergency_stop.v1",
    }
)
_MAXIMUM_FRAME = 1_048_576


class BrokerSimulationRunHost:
    """Own one gateway, campaign database, and direct vn.py containment path."""

    def __init__(
        self,
        project_root: str | Path,
        gateway: str,
        *,
        main_engine: Any | None = None,
    ) -> None:
        if gateway not in {"XTP", "TORA"}:
            raise ValueError("RUN_GATEWAY_INVALID")
        self._root = Path(project_root).resolve(strict=True)
        self.gateway = gateway
        candidate = _load_candidate(self._root / ".demo-state/ready-candidate.json")
        if candidate is None or not candidate.ready:
            raise ValueError("RUN_CANDIDATE_NOT_READY")
        self._candidate: DemoCandidate = candidate
        self._binding = _load_binding(self._root, gateway)
        self.run_digest = _digest(
            {
                "gateway": gateway,
                "binding_digest": self._binding.binding_digest,
                "candidate_digest": candidate.candidate_digest,
                "process_identity": self._binding.process_identity,
            }
        )
        run_root = self._root / ".demo-state" / "runs" / gateway
        run_root.mkdir(parents=True, exist_ok=True)
        self._host_state_path = run_root / "host-state.json"
        self._authority = BrokerSimulationAuthority(run_root / "authority.db")
        self._main_engine = main_engine or _connect_gateway(
            self._root, gateway, self._binding.credential_ref
        )
        self._containment = BrokerSimulationContainment(
            main_engine=self._main_engine,
            gateway_name=gateway,
            database=run_root / "containment.db",
        )
        self._safety = HardSafetyController()
        self._operator_identity_digest = _load_operator_digest(
            self._root / ".demo-secrets/operator.json"
        )

    def handle(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        if operation not in _OPERATIONS:
            return self._response(operation, "blocked", {"error_code": "OPERATION_NOT_ALLOWED"})
        if not self._valid_base(payload):
            return self._response(operation, "blocked", {"error_code": "REQUEST_INVALID"})
        try:
            if operation == "run.status.v1":
                return self._response(operation, self._state_name(), self._public_status())
            if operation == "run.prepare_campaign.v1":
                return self._prepare(operation, payload)
            if operation == "run.start_campaign.v1":
                return self._start(operation, payload)
            if operation == "run.pause_campaign.v1":
                return self._pause(operation, payload)
            if operation == "run.emergency_stop.v1":
                return self._stop(operation, payload)
            return self._evidence(operation, payload)
        except Exception:
            return self._response(operation, "blocked", {"error_code": "OPERATION_FAILED"})

    def _prepare(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        campaign_id = _uuid(payload.get("campaign_id"))
        campaign_digest = _required_digest(payload.get("campaign_digest"))
        if payload.get("candidate_digest") != self._candidate.candidate_digest:
            raise ValueError("candidate mismatch")
        _idempotency(payload.get("idempotency_key"))
        retained = self._read_host_state()
        if retained is not None:
            if (
                retained["campaign_id"] != campaign_id
                or retained["campaign_digest"] != campaign_digest
            ):
                raise RuntimeError("campaign drift")
            return self._response(operation, retained["state"], {"campaign_id": campaign_id})
        now_ms = _now_ms()
        self._authority.create_campaign(
            campaign_id=campaign_id,
            candidate_digest=self._candidate.candidate_digest,
            package_digest=self._candidate.package_digest,
            configuration_digest=self._candidate.configuration_digest,
            policy_digest=self._candidate.policy_digest,
            symbol_set=self._candidate.symbols,
            calendar_sessions=tuple(item.isoformat() for item in self._candidate.calendar_sessions),
            operator_identity_digest=self._operator_identity_digest,
            bindings=(self._binding,),
            lifecycle_revision=self._candidate.lifecycle_revision,
            now_ms=now_ms,
        )
        starting_equity = self._equity_minor()
        self._write_host_state(
            {
                "contract_version": 1,
                "campaign_id": campaign_id,
                "campaign_digest": campaign_digest,
                "state": "prepared",
                "starting_equity_minor": starting_equity,
                "updated_at_ms": now_ms,
            }
        )
        return self._response(operation, "prepared", {"campaign_id": campaign_id})

    def _start(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        state = self._require_campaign(payload)
        _idempotency(payload.get("idempotency_key"))
        campaign = self._authority.start_campaign(state["campaign_id"], now_ms=_now_ms())
        state["state"] = campaign.state
        state["updated_at_ms"] = _now_ms()
        self._write_host_state(state)
        return self._response(operation, campaign.state, {"campaign_id": campaign.campaign_id})

    def _pause(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        state = self._require_campaign(payload, require_id=False)
        _idempotency(payload.get("idempotency_key"))
        self._authority.pause_campaign(state["campaign_id"], now_ms=_now_ms())
        receipt = self._containment.contain(
            action="pause",
            campaign_id=state["campaign_id"],
            detected_at_ns=monotonic_ns(),
        )
        state["state"] = "paused"
        state["updated_at_ms"] = _now_ms()
        self._write_host_state(state)
        return self._response(operation, receipt.state, receipt.to_public_dict())

    def _stop(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        _idempotency(payload.get("idempotency_key"))
        state = self._read_host_state()
        detected = monotonic_ns()
        evidence_digest = _digest({"reason": "OPERATOR_EMERGENCY_STOP", "detected": detected})
        self._safety.activate("OPERATOR_EMERGENCY_STOP", "critical", evidence_digest, detected)
        if state is None:
            return self._response(operation, "contained", {"campaign_state": "absent"})
        campaign = self._authority.campaign(state["campaign_id"])
        if campaign is None:
            raise RuntimeError("campaign absent")
        if campaign.state not in {"paused", "invalid", "completed", "ready", "stopped"}:
            self._authority.stop_campaign(state["campaign_id"], now_ms=_now_ms())
        receipt = self._containment.contain(
            action="emergency_stop",
            campaign_id=state["campaign_id"],
            detected_at_ns=detected,
            exposure_blocked_at_ns=detected,
        )
        state["state"] = "stopped"
        state["updated_at_ms"] = _now_ms()
        self._write_host_state(state)
        return self._response(operation, receipt.state, receipt.to_public_dict())

    def _evidence(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        campaign_digest = _required_digest(payload.get("campaign_digest"))
        path = self._root / ".demo-state/evidence" / f"{campaign_digest[7:]}.json"
        if not path.is_file():
            return self._response(operation, "blocked", {"error_code": "EVIDENCE_NOT_FOUND"})
        value = _load_unique_json(path)
        if not isinstance(value, dict):
            raise ValueError("evidence invalid")
        return self._response(operation, "ready", value)

    def _public_status(self) -> dict[str, Any]:
        state = self._read_host_state()
        equity = self._equity_minor()
        starting = state.get("starting_equity_minor") if state else None
        incidents = ["SIGNED_FEE_LEDGER_UNAVAILABLE"]
        if equity is None:
            incidents.append("ACCOUNT_STATE_UNAVAILABLE")
        if starting is None:
            incidents.append("STARTING_EQUITY_UNAVAILABLE")
        positions = []
        gross = 0
        for position in self._main_engine.get_all_positions():
            if getattr(position, "gateway_name", None) != self.gateway:
                continue
            quantity = max(0, round(float(getattr(position, "volume", 0))))
            available = max(0, round(float(getattr(position, "yd_volume", 0))))
            value = round(quantity * float(getattr(position, "price", 0)) * 100)
            gross += value
            positions.append(
                {
                    "symbol": str(getattr(position, "vt_symbol", "")),
                    "quantity": quantity,
                    "available_quantity": min(quantity, available),
                    "marked_value_minor": value,
                    "unrealized_profit_minor": round(
                        float(getattr(position, "pnl", 0)) * 100
                    ),
                    "t_plus_one_locked_quantity": max(0, quantity - available),
                }
            )
        active_orders = [
            order
            for order in self._main_engine.get_all_active_orders()
            if getattr(order, "gateway_name", None) == self.gateway
        ]
        profit = 0 if equity is None or starting is None else equity - starting
        nav = equity or 0
        return {
            "connection_state": "connected" if equity is not None else "connecting",
            "reconciliation_state": "blocked" if incidents else "complete",
            "net_profit_minor": profit,
            "realized_profit_minor": 0,
            "unrealized_profit_minor": sum(item["unrealized_profit_minor"] for item in positions),
            "fees_minor": 0,
            "return_bps": 0 if not starting else profit * 10_000 // starting,
            "max_drawdown_bps": 0,
            "fill_count": len(self._main_engine.get_all_trades()),
            "positions": positions,
            "gross_exposure_minor": gross,
            "risk_headroom_minor": max(0, nav // 10 - gross),
            "incidents": incidents,
            "residual_exposure_minor": gross,
            "working_order_count": len(active_orders),
            "unresolved_outcomes": 0,
            "permitted_next_action": "pause" if state and state["state"] == "active" else "none",
            "updated_at_ms": _now_ms(),
        }

    def _equity_minor(self) -> int | None:
        balances = [
            float(getattr(account, "balance", 0))
            for account in self._main_engine.get_all_accounts()
            if getattr(account, "gateway_name", None) == self.gateway
        ]
        return round(sum(balances) * 100) if balances else None

    def _state_name(self) -> str:
        state = self._read_host_state()
        if state is not None:
            return str(state["state"])
        return "ready" if self._equity_minor() is not None else "blocked"

    def _require_campaign(
        self, payload: dict[str, Any], *, require_id: bool = True
    ) -> dict[str, Any]:
        state = self._read_host_state()
        if state is None:
            raise RuntimeError("campaign absent")
        if _required_digest(payload.get("campaign_digest")) != state["campaign_digest"]:
            raise RuntimeError("campaign mismatch")
        if require_id and _uuid(payload.get("campaign_id")) != state["campaign_id"]:
            raise RuntimeError("campaign mismatch")
        return state

    def _valid_base(self, payload: dict[str, Any]) -> bool:
        return (
            set(payload) <= {
                "contract_version",
                "gateway",
                "run_digest",
                "campaign_id",
                "campaign_digest",
                "candidate_digest",
                "idempotency_key",
            }
            and payload.get("contract_version") == 1
            and payload.get("gateway") == self.gateway
            and payload.get("run_digest") == self.run_digest
        )

    def _response(self, operation: str, state: str, data: dict[str, Any]) -> dict[str, Any]:
        unsigned = {
            "contract_version": 1,
            "gateway": self.gateway,
            "run_digest": self.run_digest,
            "operation": operation,
            "state": state,
            "data": data,
        }
        return {**unsigned, "receipt_digest": _digest(unsigned)}

    def _read_host_state(self) -> dict[str, Any] | None:
        if not self._host_state_path.exists():
            return None
        value = _load_unique_json(self._host_state_path)
        if not isinstance(value, dict):
            raise ValueError("run state invalid")
        return value

    def _write_host_state(self, value: dict[str, Any]) -> None:
        _atomic_json(self._host_state_path, value)


class RunIpcServer:
    def __init__(self, host: BrokerSimulationRunHost, address: str, token: str) -> None:
        parsed_host, port = _address(address)
        self._host = host
        self._token = token
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind((parsed_host, port))
        self._listener.listen(32)
        self._listener.settimeout(1.0)
        actual = self._listener.getsockname()
        self.endpoint_path = (
            host._root / ".demo-state" / "runs" / host.gateway / "endpoint.json"
        )
        _atomic_json(
            self.endpoint_path,
            {
                "contract_version": 1,
                "transport": "tcp-loopback",
                "address": f"127.0.0.1:{actual[1]}",
                "gateway": host.gateway,
                "run_digest": host.run_digest,
            },
        )

    def serve_forever(self) -> None:
        try:
            while True:
                try:
                    connection, peer = self._listener.accept()
                except socket.timeout:
                    continue
                with connection:
                    connection.settimeout(5)
                    if peer[0] != "127.0.0.1":
                        continue
                    response = self._serve_connection(connection)
                    encoded = _json_bytes(response)
                    connection.sendall(struct.pack(">I", len(encoded)) + encoded)
        finally:
            self._listener.close()
            self.endpoint_path.unlink(missing_ok=True)

    def _serve_connection(self, connection: socket.socket) -> dict[str, Any]:
        try:
            size = struct.unpack(">I", _read_exact(connection, 4))[0]
            if not 1 <= size <= _MAXIMUM_FRAME:
                raise ValueError("frame invalid")
            request = json.loads(_read_exact(connection, size), object_pairs_hook=_unique_object)
            if (
                not isinstance(request, dict)
                or set(request)
                != {"kind", "transport_version", "transport_token", "operation", "payload"}
                or request["kind"] != "demo_command"
                or request["transport_version"] != 1
                or request["transport_token"] != self._token
                or not isinstance(request["operation"], str)
                or not isinstance(request["payload"], dict)
            ):
                raise ValueError("request invalid")
            return self._host.handle(request["operation"], request["payload"])
        except Exception:
            return self._host._response(
                "run.rejected.v1", "blocked", {"error_code": "UNAUTHENTICATED_OR_MALFORMED"}
            )


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m vnpy.demo_web.run_service")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--gateway", choices=("XTP", "TORA"), required=True)
    parser.add_argument("--address", required=True)
    args = parser.parse_args()
    root = args.project_root.resolve(strict=True)
    token_path = root / ".demo-secrets" / f"run-{args.gateway.lower()}-ipc-token"
    token_path.parent.mkdir(parents=True, exist_ok=True)
    if token_path.exists():
        token = token_path.read_text(encoding="ascii").strip()
    else:
        token = token_urlsafe(48)
        token_path.write_text(token, encoding="ascii")
    if not 24 <= len(token) <= 512:
        raise ValueError("RUN_IPC_TOKEN_INVALID")
    RunIpcServer(
        BrokerSimulationRunHost(root, args.gateway), args.address, token
    ).serve_forever()


def _load_binding(root: Path, gateway: str) -> GatewayBinding:
    values = _load_unique_json(root / ".demo-secrets/gateway-bindings.json")
    if not isinstance(values, list):
        raise ValueError("RUN_BINDINGS_INVALID")
    matches = [value for value in values if isinstance(value, dict) and value.get("name") == gateway]
    if len(matches) != 1:
        raise ValueError("RUN_BINDING_NOT_UNIQUE")
    value = matches[0]
    required = {
        "name",
        "environment",
        "server_fingerprint",
        "account_fingerprint",
        "credential_ref",
    }
    if set(value) != required or value["environment"] != "broker_simulation":
        raise ValueError("RUN_BINDING_INVALID")
    server_fingerprint, account_fingerprint = _settings_fingerprints(
        root, gateway, value["credential_ref"]
    )
    if (
        server_fingerprint != value["server_fingerprint"]
        or account_fingerprint != value["account_fingerprint"]
    ):
        raise ValueError("RUN_BINDING_SETTINGS_DRIFT")
    return GatewayBinding.create(
        gateway=gateway,
        environment=value["environment"],
        server_fingerprint=value["server_fingerprint"],
        account_fingerprint=value["account_fingerprint"],
        credential_ref=value["credential_ref"],
        process_identity=f"vnpy-demo-{gateway.lower()}",
        rpc_endpoint=f"127.0.0.1:{17801 if gateway == 'XTP' else 17802}",
        state_store_path=str(root / ".demo-state" / "runs" / gateway),
        allowed_server_fingerprints=frozenset({value["server_fingerprint"]}),
        allowed_account_fingerprints=frozenset({value["account_fingerprint"]}),
        created_at_ms=_now_ms(),
    )


def _settings_fingerprints(
    root: Path, gateway: str, credential_ref: str
) -> tuple[str, str]:
    settings_path = Path(credential_ref)
    if not settings_path.is_absolute():
        settings_path = root / settings_path
    settings_path = settings_path.resolve(strict=True)
    settings = _load_unique_json(settings_path)
    if not isinstance(settings, dict):
        raise ValueError("RUN_GATEWAY_SETTINGS_INVALID")
    account_key = "账号"
    server_keys = (
        ("行情地址", "行情端口", "交易地址", "交易端口")
        if gateway == "XTP"
        else ("行情服务器", "交易服务器")
    )
    if account_key not in settings or any(key not in settings for key in server_keys):
        raise ValueError("RUN_GATEWAY_SETTINGS_INVALID")
    account = settings[account_key]
    if not isinstance(account, str) or not account.strip():
        raise ValueError("RUN_GATEWAY_SETTINGS_INVALID")
    server_identity = {key: settings[key] for key in server_keys}
    return _digest(server_identity), _digest({account_key: account})


def _connect_gateway(root: Path, gateway: str, credential_ref: str) -> MainEngine:
    settings_path = Path(credential_ref)
    if not settings_path.is_absolute():
        settings_path = root / settings_path
    settings_path = settings_path.resolve(strict=True)
    value = _load_unique_json(settings_path)
    if not isinstance(value, dict):
        raise ValueError("RUN_GATEWAY_SETTINGS_INVALID")
    module_name, class_names = (
        ("vnpy_xtp", ("XtpGateway",))
        if gateway == "XTP"
        else ("vnpy_tora", ("ToraStockGateway", "ToraGateway"))
    )
    module = import_module(module_name)
    gateway_class = next(
        (getattr(module, name) for name in class_names if hasattr(module, name)), None
    )
    if gateway_class is None:
        raise RuntimeError("RUN_GATEWAY_CLASS_UNAVAILABLE")
    main_engine = MainEngine(EventEngine())
    main_engine.add_gateway(gateway_class)
    main_engine.connect(value, gateway)
    return main_engine


def _address(value: str) -> tuple[str, int]:
    try:
        host, port_text = value.rsplit(":", 1)
        port = int(port_text)
    except (ValueError, AttributeError) as exc:
        raise ValueError("RUN_LOOPBACK_REQUIRED") from exc
    if host != "127.0.0.1" or not 1 <= port <= 65_535:
        raise ValueError("RUN_LOOPBACK_REQUIRED")
    return host, port


def _uuid(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("UUID invalid")
    UUID(value)
    return value


def _required_digest(value: Any) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError("digest invalid")
    return value


def _idempotency(value: Any) -> str:
    if not isinstance(value, str) or not 16 <= len(value) <= 128:
        raise ValueError("idempotency invalid")
    return value


def _digest(value: Any) -> str:
    return f"sha256:{sha256(_json_bytes(value)).hexdigest()}"


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("wb") as handle:
        handle.write(_json_bytes(value))
        handle.write(b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _read_exact(connection: socket.socket, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        chunk = connection.recv(size - len(result))
        if not chunk:
            raise ValueError("frame truncated")
        result.extend(chunk)
    return bytes(result)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _now_ms() -> int:
    return max(1, time_ns() // 1_000_000)


if __name__ == "__main__":
    main()
