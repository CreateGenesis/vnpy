"""Independent, durable XTP/TORA control owned by the trusted vn.py host."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from hashlib import sha256
import json
import os
from pathlib import Path
from threading import RLock
from time import monotonic, sleep, time_ns
from typing import Any, Protocol

from .contracts import ServiceName
from .run_clients import BrokerSimulationRunClient, RunClientBinding
from .supervisor import SupervisorError
from .transport import LengthPrefixedJsonTransport


_GATEWAYS = ("XTP", "TORA")
_ACTIONS = frozenset({"start", "stop", "reconnect", "select"})


class GatewayControlError(RuntimeError):
    pass


class ConfigurationReader(Protocol):
    def read_active(self) -> dict[str, Any]: ...

    def read_section_secrets(self, section: str) -> dict[str, str]: ...

    def read_active_section_secrets(self, section: str) -> dict[str, str]: ...


class SupervisorController(Protocol):
    def reconcile(self, service: ServiceName) -> dict[str, Any]: ...

    def handle(self, command: dict[str, Any]) -> dict[str, Any]: ...

    def handle_with_secret(
        self,
        command: dict[str, Any],
        secret_payload: bytes,
    ) -> dict[str, Any]: ...


class GatewayControlService:
    """Start, stop, reconnect, and select gateways without sharing broker secrets."""

    def __init__(
        self,
        project_root: str | Path,
        configuration: ConfigurationReader,
        supervisor: SupervisorController,
        *,
        client_loader: Callable[[str], Any | None] | None = None,
        active_campaign: Callable[[], dict[str, Any] | None] | None = None,
        pause_campaign: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self._root = Path(project_root).resolve(strict=False)
        self._configuration = configuration
        self._supervisor = supervisor
        self._client_loader = client_loader or (
            lambda gateway: _load_run_client(self._root, gateway)
        )
        self._active_campaign = active_campaign or (lambda: None)
        self._pause_campaign = pause_campaign or (
            lambda _campaign_id: {"state": "paused"}
        )
        self._path = self._root / ".operations-state" / "gateways.json"
        self._lock = RLock()
        with self._lock:
            if not self._path.exists():
                self._persist(
                    {
                        "contract_version": 1,
                        "revision": 0,
                        "selected": [],
                        "gateways": {
                            gateway: {
                                "state": "stopped",
                                "error_code": None,
                                "updated_at_ms": _now_ms(),
                            }
                            for gateway in _GATEWAYS
                        },
                        "idempotency": {},
                    }
                )

    def control(
        self,
        gateway: str,
        action: str,
        command: dict[str, Any],
    ) -> dict[str, Any]:
        if gateway not in _GATEWAYS or action not in _ACTIONS:
            raise GatewayControlError("GATEWAY_OPERATION_DENIED")
        expected_keys = {"expected_revision", "idempotency_key"}
        if action == "select":
            expected_keys.add("selected")
        if set(command) != expected_keys:
            raise GatewayControlError("GATEWAY_COMMAND_INVALID")
        expected_revision = command.get("expected_revision")
        idempotency_key = command.get("idempotency_key")
        selected = command.get("selected")
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
            or not isinstance(idempotency_key, str)
            or not 16 <= len(idempotency_key) <= 128
            or (action == "select" and not isinstance(selected, bool))
        ):
            raise GatewayControlError("GATEWAY_COMMAND_INVALID")
        request_digest = _digest(
            {"gateway": gateway, "action": action, "selected": selected}
        )
        with self._lock:
            state = self._read()
            retained = state["idempotency"].get(idempotency_key)
            if retained is not None:
                if retained["request_digest"] != request_digest:
                    raise GatewayControlError("GATEWAY_IDEMPOTENCY_CONFLICT")
                return dict(retained["result"])
            if state["revision"] != expected_revision:
                raise GatewayControlError("GATEWAY_REVISION_CONFLICT")

            if action == "start":
                state_name = self._start(gateway)
            elif action == "stop":
                state_name = self._stop(gateway, idempotency_key)
            elif action == "reconnect":
                state_name = self._reconnect(gateway, idempotency_key)
            else:
                state_name = self._select(gateway, bool(selected))

            if action == "select":
                selected_set = set(state["selected"])
                if selected:
                    selected_set.add(gateway)
                else:
                    selected_set.discard(gateway)
                state["selected"] = sorted(selected_set)
            state["revision"] += 1
            state["gateways"][gateway] = {
                "state": state_name,
                "error_code": None,
                "updated_at_ms": _now_ms(),
            }
            result = {
                "contract_version": 1,
                "gateway": gateway,
                "state": state_name,
                "selected": gateway in state["selected"],
                "revision": state["revision"],
            }
            result["receipt_digest"] = _digest(result)
            state["idempotency"][idempotency_key] = {
                "request_digest": request_digest,
                "result": result,
            }
            self._persist(state)
            return dict(result)

    def selected_gateways(self) -> set[str]:
        return set(self._read()["selected"])

    def clients(self) -> dict[str, Any]:
        clients: dict[str, Any] = {}
        for gateway in _GATEWAYS:
            try:
                client = self._client_loader(gateway)
            except Exception:
                continue
            if client is not None:
                clients[gateway] = client
        return clients

    def projection(self) -> dict[str, Any]:
        state = self._read()
        selected = set(state["selected"])
        gateways = []
        for gateway in _GATEWAYS:
            state_name = self._observed_state(gateway)
            retained = state["gateways"][gateway]
            gateways.append(
                {
                    "gateway": gateway,
                    "state": state_name,
                    "selected": gateway in selected,
                    "error_code": retained.get("error_code"),
                    "updated_at_ms": retained["updated_at_ms"],
                }
            )
        return {
            "contract_version": 1,
            "revision": state["revision"],
            "gateways": gateways,
        }

    def _start(self, gateway: str) -> str:
        service = _service(gateway)
        supervised = self._supervisor.reconcile(service)
        client = self._load_client(gateway)
        if supervised.get("state") == "ready" and client is not None:
            return _require_connected(client.gateway_health())
        active = self._active_configuration(gateway)
        payload = json.dumps(
            active,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        try:
            self._supervisor.handle_with_secret(
                {
                    "service": service.value,
                    "action": "start",
                    "expected_revision": int(supervised.get("revision", 0)),
                },
                payload,
            )
        except SupervisorError as exc:
            raise GatewayControlError(str(exc)) from exc
        finally:
            del payload
        client = self._wait_for_client(gateway)
        if client is None:
            raise GatewayControlError("GATEWAY_RUN_SERVICE_UNAVAILABLE")
        return _require_connected(client.gateway_health())

    def _stop(self, gateway: str, idempotency_key: str) -> str:
        campaign = self._active_campaign()
        if (
            isinstance(campaign, dict)
            and campaign.get("state") in {"starting", "active", "pausing"}
            and gateway in campaign.get("gateways", [])
        ):
            pause = self._pause_campaign(str(campaign.get("campaign_id", "")))
            if pause.get("state") not in {"paused", "contained"}:
                raise GatewayControlError("GATEWAY_PAUSE_REQUIRED")
        service = _service(gateway)
        supervised = self._supervisor.reconcile(service)
        client = self._load_client(gateway)
        if client is not None:
            drained = client.drain_shutdown(
                f"drain-{sha256(f'{idempotency_key}:{gateway}'.encode()).hexdigest()}"
            )
            data = drained.get("data") if isinstance(drained.get("data"), Mapping) else {}
            if (
                drained.get("state") != "stopped"
                or data.get("reconciliation_state") != "complete"
            ):
                raise GatewayControlError("GATEWAY_RECONCILIATION_REQUIRED")
        elif supervised.get("state") == "ready":
            raise GatewayControlError("GATEWAY_RUN_SERVICE_UNAVAILABLE")
        if supervised.get("state") == "ready":
            try:
                stopped = self._supervisor.handle(
                    {
                        "service": service.value,
                        "action": "stop",
                        "expected_revision": int(supervised.get("revision", 0)),
                    }
                )
            except SupervisorError as exc:
                raise GatewayControlError(str(exc)) from exc
            if stopped.get("state") != "stopped":
                raise GatewayControlError("GATEWAY_PROCESS_STOP_UNCERTAIN")
        return "stopped"

    def _reconnect(self, gateway: str, idempotency_key: str) -> str:
        service = _service(gateway)
        supervised = self._supervisor.reconcile(service)
        client = self._load_client(gateway)
        if supervised.get("state") != "ready" or client is None:
            return self._start(gateway)
        response = client.reconnect(
            f"reconnect-{sha256(f'{idempotency_key}:{gateway}'.encode()).hexdigest()}"
        )
        return _require_connected(response)

    def _select(self, gateway: str, selected: bool) -> str:
        campaign = self._active_campaign()
        if isinstance(campaign, dict) and campaign.get("state") in {
            "starting",
            "active",
            "pausing",
        }:
            raise GatewayControlError("GATEWAY_SELECTION_CAMPAIGN_ACTIVE")
        if not selected:
            return self._observed_state(gateway)
        client = self._load_client(gateway)
        if client is None:
            raise GatewayControlError("GATEWAY_NOT_CONNECTED")
        return _require_connected(client.gateway_health())

    def _active_configuration(self, gateway: str) -> dict[str, Any]:
        active = self._configuration.read_active()
        section = gateway.lower()
        public = active.get("sections", {}).get(section)
        active_secret_reader = getattr(
            self._configuration,
            "read_active_section_secrets",
            self._configuration.read_section_secrets,
        )
        try:
            secrets = active_secret_reader(section)
        except Exception as exc:
            raise GatewayControlError("GATEWAY_CONFIGURATION_NOT_ACTIVE") from exc
        if (
            active.get("state") != "active"
            or not isinstance(active.get("version"), int)
            or active["version"] < 1
            or not isinstance(active.get("configuration_digest"), str)
            or not isinstance(active.get("operator_identity_digest"), str)
            or not isinstance(public, dict)
            or not secrets
        ):
            raise GatewayControlError("GATEWAY_CONFIGURATION_NOT_ACTIVE")
        return {
            "contract_version": 1,
            "gateway": gateway,
            "configuration_version": active["version"],
            "configuration_digest": active["configuration_digest"],
            "operator_identity_digest": active["operator_identity_digest"],
            "public": public,
            "secrets": secrets,
        }

    def _observed_state(self, gateway: str) -> str:
        client = self._load_client(gateway)
        if client is not None:
            try:
                return _require_connected(client.gateway_health())
            except Exception:
                return "unavailable"
        try:
            supervised = self._supervisor.reconcile(_service(gateway))
        except Exception:
            return "unavailable"
        return "stopped" if supervised.get("state") == "stopped" else "unavailable"

    def _load_client(self, gateway: str) -> Any | None:
        try:
            return self._client_loader(gateway)
        except Exception:
            return None

    def _wait_for_client(self, gateway: str, timeout_seconds: float = 10.0) -> Any | None:
        deadline = monotonic() + timeout_seconds
        while True:
            client = self._load_client(gateway)
            if client is not None:
                return client
            if monotonic() >= deadline:
                return None
            sleep(0.05)

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self._path.read_bytes(), object_pairs_hook=_unique_object)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise GatewayControlError("GATEWAY_STATE_INVALID") from exc
        if (
            not isinstance(value, dict)
            or value.get("contract_version") != 1
            or not isinstance(value.get("revision"), int)
            or not isinstance(value.get("selected"), list)
            or not isinstance(value.get("gateways"), dict)
            or not isinstance(value.get("idempotency"), dict)
        ):
            raise GatewayControlError("GATEWAY_STATE_INVALID")
        return value

    def _persist(self, value: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(f".{self._path.name}.tmp")
        with temporary.open("xb") as handle:
            handle.write(
                json.dumps(
                    value,
                    ensure_ascii=True,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            )
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self._path)


def _service(gateway: str) -> ServiceName:
    return ServiceName.RUN_XTP if gateway == "XTP" else ServiceName.RUN_TORA


def _require_connected(response: Mapping[str, Any]) -> str:
    if response.get("state") not in {"connected", "ready"}:
        raise GatewayControlError("GATEWAY_NOT_CONNECTED")
    return "connected"


def _load_run_client(root: Path, gateway: str) -> BrokerSimulationRunClient | None:
    descriptor_path = root / ".demo-state" / "runs" / gateway / "endpoint.json"
    token_path = root / ".demo-secrets" / f"run-{gateway.lower()}-ipc-token"
    if not descriptor_path.is_file() or not token_path.is_file():
        return None
    try:
        descriptor = json.loads(
            descriptor_path.read_bytes(), object_pairs_hook=_unique_object
        )
        token = token_path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise GatewayControlError("GATEWAY_ENDPOINT_INVALID") from exc
    if (
        not isinstance(descriptor, dict)
        or set(descriptor)
        != {
            "contract_version",
            "transport",
            "address",
            "gateway",
            "run_digest",
        }
        or descriptor.get("contract_version") != 1
        or descriptor.get("transport") != "tcp-loopback"
        or descriptor.get("gateway") != gateway
        or not isinstance(descriptor.get("address"), str)
        or not isinstance(descriptor.get("run_digest"), str)
        or not 24 <= len(token) <= 512
    ):
        raise GatewayControlError("GATEWAY_ENDPOINT_INVALID")
    return BrokerSimulationRunClient(
        RunClientBinding(
            gateway=gateway,
            run_digest=descriptor["run_digest"],
            endpoint=f"tcp://{descriptor['address']}",
        ),
        LengthPrefixedJsonTransport(token),
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{sha256(encoded).hexdigest()}"


def _now_ms() -> int:
    return max(1, time_ns() // 1_000_000)
