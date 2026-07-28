"""Authenticated fixed-operation Supervisor IPC and one-use secret leases."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import hmac
import json
from secrets import token_urlsafe
from threading import RLock
from time import time_ns
from typing import Any, Callable


class SupervisorIpcError(RuntimeError):
    pass


_OPERATIONS = frozenset({"service_control", "health", "secret_lease"})
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
