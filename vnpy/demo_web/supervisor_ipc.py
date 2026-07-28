"""Authenticated fixed-operation Supervisor IPC and one-use secret leases."""

from __future__ import annotations

from dataclasses import dataclass
from base64 import b64decode, b64encode
from hashlib import sha256
import hmac
import json
from secrets import token_urlsafe
import socket
from socketserver import BaseRequestHandler, ThreadingTCPServer
from threading import RLock
from time import time_ns
from typing import Any, Callable, Protocol

from .contracts import ServiceName
from .supervisor import SupervisorError


class SupervisorIpcError(RuntimeError):
    pass


_OPERATIONS = frozenset(
    {"service_control", "service_secret_control", "health", "secret_lease"}
)
_SERVICE_ACTIONS = frozenset({"start", "stop", "restart"})
_SERVICES = frozenset(
    {"web", "research", "model_xtp", "model_tora", "run_xtp", "run_tora", "rqdata_fetcher"}
)


class AuthenticatedSupervisorIpc:
    def __init__(self, authentication_key: bytes, *, clock_ms: Callable[[], int] | None = None) -> None:
        if len(authentication_key) < 32:
            raise SupervisorIpcError("SUPERVISOR_IPC_KEY_INVALID")
        self._key = bytes(authentication_key)
        self._clock_ms = clock_ms or (lambda: time_ns() // 1_000_000)
        self._seen: set[str] = set()
        self._lock = RLock()

    def sign(
        self,
        operation: str,
        payload: dict[str, Any],
        *,
        nonce: str,
        expires_at_ms: int,
    ) -> dict[str, Any]:
        self._validate_operation(operation, payload)
        if len(nonce) < 16 or expires_at_ms <= self._clock_ms():
            raise SupervisorIpcError("SUPERVISOR_IPC_ENVELOPE_INVALID")
        unsigned = {
            "contract_version": 1,
            "operation": operation,
            "payload": payload,
            "nonce": nonce,
            "expires_at_ms": expires_at_ms,
        }
        return {**unsigned, "authentication": hmac.new(self._key, _canonical(unsigned), sha256).hexdigest()}

    def verify(self, envelope: dict[str, Any]) -> dict[str, Any]:
        required = {
            "contract_version",
            "operation",
            "payload",
            "nonce",
            "expires_at_ms",
            "authentication",
        }
        if set(envelope) != required or envelope.get("contract_version") != 1:
            raise SupervisorIpcError("SUPERVISOR_IPC_ENVELOPE_INVALID")
        operation = envelope.get("operation")
        payload = envelope.get("payload")
        nonce = envelope.get("nonce")
        expires = envelope.get("expires_at_ms")
        authentication = envelope.get("authentication")
        if (
            not isinstance(operation, str)
            or not isinstance(payload, dict)
            or not isinstance(nonce, str)
            or len(nonce) < 16
            or not isinstance(expires, int)
            or isinstance(expires, bool)
            or not isinstance(authentication, str)
        ):
            raise SupervisorIpcError("SUPERVISOR_IPC_ENVELOPE_INVALID")
        self._validate_operation(operation, payload)
        unsigned = {key: envelope[key] for key in required - {"authentication"}}
        expected = hmac.new(self._key, _canonical(unsigned), sha256).hexdigest()
        if not hmac.compare_digest(authentication, expected):
            raise SupervisorIpcError("SUPERVISOR_IPC_AUTHENTICATION_FAILED")
        with self._lock:
            if nonce in self._seen:
                raise SupervisorIpcError("SUPERVISOR_IPC_REPLAY")
            if expires < self._clock_ms():
                raise SupervisorIpcError("SUPERVISOR_IPC_EXPIRED")
            self._seen.add(nonce)
        return dict(payload)

    @staticmethod
    def _validate_operation(operation: str, payload: dict[str, Any]) -> None:
        if operation not in _OPERATIONS:
            raise SupervisorIpcError("SUPERVISOR_IPC_OPERATION_DENIED")
        if operation == "service_control":
            if (
                set(payload) != {"service", "action", "expected_revision"}
                or payload.get("service") not in _SERVICES
                or payload.get("action") not in _SERVICE_ACTIONS
                or not isinstance(payload.get("expected_revision"), int)
                or isinstance(payload.get("expected_revision"), bool)
            ):
                raise SupervisorIpcError("SUPERVISOR_IPC_OPERATION_DENIED")
        elif operation == "service_secret_control":
            secret = payload.get("secret_base64")
            if (
                set(payload) != {"service", "action", "expected_revision", "secret_base64"}
                or payload.get("service") not in {"run_xtp", "run_tora", "rqdata_fetcher"}
                or payload.get("action") not in {"start", "restart"}
                or not isinstance(payload.get("expected_revision"), int)
                or isinstance(payload.get("expected_revision"), bool)
                or not isinstance(secret, str)
                or not 1 <= len(secret) <= 87_384
            ):
                raise SupervisorIpcError("SUPERVISOR_IPC_OPERATION_DENIED")
        elif operation == "health":
            if set(payload) != {"service"} or payload.get("service") not in _SERVICES:
                raise SupervisorIpcError("SUPERVISOR_IPC_OPERATION_DENIED")
        elif operation == "secret_lease":
            lease_id = payload.get("lease_id")
            if (
                set(payload) != {"service", "lease_id"}
                or payload.get("service") not in _SERVICES
                or not isinstance(lease_id, str)
                or not 16 <= len(lease_id) <= 256
            ):
                raise SupervisorIpcError("SUPERVISOR_IPC_OPERATION_DENIED")


@dataclass
class _Lease:
    value: bytearray
    audience: str
    expires_at_ms: int


class SecretLeaseBroker:
    def __init__(self, *, clock_ms: Callable[[], int] | None = None) -> None:
        self._clock_ms = clock_ms or (lambda: time_ns() // 1_000_000)
        self._leases: dict[str, _Lease] = {}
        self._consumed: set[str] = set()
        self._lock = RLock()

    def issue(self, value: bytes, *, audience: str, ttl_ms: int) -> str:
        if not value or audience not in _SERVICES or not 1 <= ttl_ms <= 60_000:
            raise SupervisorIpcError("SECRET_LEASE_INVALID")
        lease_id = token_urlsafe(24)
        with self._lock:
            self._leases[lease_id] = _Lease(
                value=bytearray(value),
                audience=audience,
                expires_at_ms=self._clock_ms() + ttl_ms,
            )
        return lease_id

    def consume(self, lease_id: str, *, audience: str) -> bytes:
        with self._lock:
            if lease_id in self._consumed:
                raise SupervisorIpcError("SECRET_LEASE_CONSUMED")
            lease = self._leases.get(lease_id)
            if lease is None:
                raise SupervisorIpcError("SECRET_LEASE_NOT_FOUND")
            if lease.audience != audience:
                raise SupervisorIpcError("SECRET_LEASE_AUDIENCE_MISMATCH")
            if lease.expires_at_ms < self._clock_ms():
                _zero(lease.value)
                self._leases.pop(lease_id, None)
                self._consumed.add(lease_id)
                raise SupervisorIpcError("SECRET_LEASE_EXPIRED")
            value = bytes(lease.value)
            _zero(lease.value)
            self._leases.pop(lease_id, None)
            self._consumed.add(lease_id)
            return value


def _zero(value: bytearray) -> None:
    for index in range(len(value)):
        value[index] = 0


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


class SupervisorController(Protocol):
    def handle(self, command: dict[str, Any]) -> dict[str, Any]: ...

    def reconcile(self, service: ServiceName) -> dict[str, Any]: ...

    def handle_secret(
        self,
        command: dict[str, Any],
        secret_payload: bytes,
    ) -> dict[str, Any]: ...


class SupervisorIpcServer(ThreadingTCPServer):
    """Bounded loopback RPC server owned by the trusted Supervisor process."""

    allow_reuse_address = False
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        authentication_key: bytes,
        supervisor: SupervisorController,
        secret_leases: SecretLeaseBroker | None = None,
    ) -> None:
        if address[0] != "127.0.0.1":
            raise SupervisorIpcError("SUPERVISOR_IPC_LOOPBACK_REQUIRED")
        self.authentication_key = bytes(authentication_key)
        self.authenticator = AuthenticatedSupervisorIpc(self.authentication_key)
        self.supervisor = supervisor
        self.secret_leases = secret_leases or SecretLeaseBroker()
        super().__init__(address, _SupervisorIpcRequestHandler, bind_and_activate=True)

    def dispatch(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        if operation == "service_control":
            return self.supervisor.handle(payload)
        if operation == "service_secret_control":
            encoded = payload["secret_base64"]
            try:
                secret = b64decode(encoded, validate=True)
            except ValueError as exc:
                raise SupervisorIpcError("SUPERVISOR_SECRET_PAYLOAD_INVALID") from exc
            if not secret or len(secret) > 65_536:
                raise SupervisorIpcError("SUPERVISOR_SECRET_PAYLOAD_INVALID")
            lease_id = self.secret_leases.issue(
                secret,
                audience=payload["service"],
                ttl_ms=10_000,
            )
            consumed = self.secret_leases.consume(lease_id, audience=payload["service"])
            command = {key: payload[key] for key in ("service", "action", "expected_revision")}
            return self.supervisor.handle_secret(command, consumed)
        if operation == "health":
            try:
                service = ServiceName(payload["service"])
            except (KeyError, ValueError, TypeError) as exc:
                raise SupervisorIpcError("SUPERVISOR_IPC_OPERATION_DENIED") from exc
            return self.supervisor.reconcile(service)
        if operation == "secret_lease":
            value = self.secret_leases.consume(
                payload["lease_id"], audience=payload["service"]
            )
            return {"value_base64": b64encode(value).decode("ascii")}
        raise SupervisorIpcError("SUPERVISOR_IPC_OPERATION_DENIED")


class _SupervisorIpcRequestHandler(BaseRequestHandler):
    def handle(self) -> None:
        server = self.server
        if not isinstance(server, SupervisorIpcServer):
            return
        nonce = "invalid-request"
        try:
            self.request.settimeout(5.0)
            encoded = _receive_line(self.request)
            envelope = json.loads(encoded, object_pairs_hook=_unique_object)
            if not isinstance(envelope, dict):
                raise SupervisorIpcError("SUPERVISOR_IPC_ENVELOPE_INVALID")
            raw_nonce = envelope.get("nonce")
            if isinstance(raw_nonce, str) and raw_nonce:
                nonce = raw_nonce
            payload = server.authenticator.verify(envelope)
            result = server.dispatch(envelope["operation"], payload)
            response = _signed_response(
                server.authentication_key,
                request_nonce=nonce,
                ok=True,
                payload=result,
            )
        except (SupervisorIpcError, SupervisorError) as exc:
            code = str(exc).split(":", 1)[0]
            if not code.isupper():
                code = "SUPERVISOR_IPC_REQUEST_FAILED"
            response = _signed_response(
                server.authentication_key,
                request_nonce=nonce,
                ok=False,
                payload={"code": code},
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            response = _signed_response(
                server.authentication_key,
                request_nonce=nonce,
                ok=False,
                payload={"code": "SUPERVISOR_IPC_ENVELOPE_INVALID"},
            )
        try:
            self.request.sendall(_canonical(response) + b"\n")
        except OSError:
            return


class SupervisorIpcClient:
    """Fixed-operation client used by the Web child."""

    def __init__(
        self,
        address: tuple[str, int],
        *,
        authentication_key: bytes,
        timeout_seconds: float = 5.0,
    ) -> None:
        if address[0] != "127.0.0.1" or not 1 <= address[1] <= 65_535:
            raise SupervisorIpcError("SUPERVISOR_IPC_LOOPBACK_REQUIRED")
        self._address = address
        self._key = bytes(authentication_key)
        self._authenticator = AuthenticatedSupervisorIpc(self._key)
        self._timeout_seconds = timeout_seconds

    def request(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        nonce = token_urlsafe(24)
        envelope = self._authenticator.sign(
            operation,
            payload,
            nonce=nonce,
            expires_at_ms=(time_ns() // 1_000_000) + 10_000,
        )
        try:
            with socket.create_connection(self._address, timeout=self._timeout_seconds) as connection:
                connection.settimeout(self._timeout_seconds)
                connection.sendall(_canonical(envelope) + b"\n")
                connection.shutdown(socket.SHUT_WR)
                response = json.loads(
                    _receive_line(connection),
                    object_pairs_hook=_unique_object,
                )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise SupervisorIpcError("SUPERVISOR_IPC_UNAVAILABLE") from exc
        payload_out = _verify_response(self._key, response, request_nonce=nonce)
        if response["ok"] is not True:
            raise SupervisorIpcError(str(payload_out.get("code", "SUPERVISOR_IPC_REQUEST_FAILED")))
        return payload_out

    def handle(self, command: dict[str, Any]) -> dict[str, Any]:
        return self.request("service_control", command)

    def handle_with_secret(
        self,
        command: dict[str, Any],
        secret_payload: bytes,
    ) -> dict[str, Any]:
        if not secret_payload or len(secret_payload) > 65_536:
            raise SupervisorIpcError("SUPERVISOR_SECRET_PAYLOAD_INVALID")
        return self.request(
            "service_secret_control",
            {**command, "secret_base64": b64encode(secret_payload).decode("ascii")},
        )

    def reconcile(self, service: ServiceName) -> dict[str, Any]:
        return self.request("health", {"service": service.value})


def _receive_line(connection: socket.socket) -> str:
    chunks = bytearray()
    while len(chunks) <= 65_536:
        block = connection.recv(min(4096, 65_537 - len(chunks)))
        if not block:
            break
        chunks.extend(block)
        if b"\n" in block:
            break
    if not chunks.endswith(b"\n") or len(chunks) > 65_536 or chunks.count(b"\n") != 1:
        raise SupervisorIpcError("SUPERVISOR_IPC_ENVELOPE_INVALID")
    return chunks[:-1].decode("utf-8")


def _signed_response(
    key: bytes,
    *,
    request_nonce: str,
    ok: bool,
    payload: dict[str, Any],
) -> dict[str, Any]:
    unsigned = {
        "contract_version": 1,
        "request_nonce": request_nonce,
        "ok": ok,
        "payload": payload,
    }
    return {**unsigned, "authentication": hmac.new(key, _canonical(unsigned), sha256).hexdigest()}


def _verify_response(
    key: bytes,
    response: Any,
    *,
    request_nonce: str,
) -> dict[str, Any]:
    required = {"contract_version", "request_nonce", "ok", "payload", "authentication"}
    if (
        not isinstance(response, dict)
        or set(response) != required
        or response.get("contract_version") != 1
        or response.get("request_nonce") != request_nonce
        or not isinstance(response.get("ok"), bool)
        or not isinstance(response.get("payload"), dict)
        or not isinstance(response.get("authentication"), str)
    ):
        raise SupervisorIpcError("SUPERVISOR_IPC_RESPONSE_INVALID")
    unsigned = {key_name: response[key_name] for key_name in required - {"authentication"}}
    expected = hmac.new(key, _canonical(unsigned), sha256).hexdigest()
    if not hmac.compare_digest(response["authentication"], expected):
        raise SupervisorIpcError("SUPERVISOR_IPC_RESPONSE_AUTHENTICATION_FAILED")
    return dict(response["payload"])


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value
