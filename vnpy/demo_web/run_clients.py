"""Strict clients for isolated vn.py broker-simulation run processes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Any, Protocol
from urllib.parse import urlsplit


_DIGEST = re.compile(r"^(?:sha256|blake3):[0-9a-f]{64}$")
_GATEWAYS = frozenset({"XTP", "TORA"})
_RESPONSE_FIELDS = frozenset(
    {
        "contract_version",
        "gateway",
        "run_digest",
        "operation",
        "state",
        "receipt_digest",
        "data",
    }
)
_FORBIDDEN_RESPONSE_KEYS = frozenset(
    {
        "account",
        "account_id",
        "account_fingerprint",
        "cancel",
        "cancel_order",
        "credential",
        "credential_ref",
        "main_engine",
        "order",
        "order_request",
        "password",
        "private_key",
        "rpc",
        "rpc_endpoint",
        "send_order",
        "token",
    }
)


class RunRpcTransport(Protocol):
    """Internal transport boundary; it is never forwarded by the run client."""

    def request(
        self,
        endpoint: str,
        operation: str,
        payload: dict[str, Any],
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class RunClientBinding:
    gateway: str
    run_digest: str
    endpoint: str

    def __post_init__(self) -> None:
        if self.gateway not in _GATEWAYS or _DIGEST.fullmatch(self.run_digest) is None:
            raise ValueError("RUN_CLIENT_BINDING_INVALID")
        parsed = urlsplit(self.endpoint)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("RUN_CLIENT_LOOPBACK_REQUIRED") from exc
        if (
            parsed.scheme != "tcp"
            or parsed.hostname != "127.0.0.1"
            or port is None
            or not 1 <= port <= 65_535
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("RUN_CLIENT_LOOPBACK_REQUIRED")


class BrokerSimulationRunClient:
    """Expose only bounded run lifecycle controls, never broker operations."""

    def __init__(self, binding: RunClientBinding, transport: RunRpcTransport) -> None:
        self._binding = binding
        self._transport = transport

    @property
    def binding(self) -> RunClientBinding:
        return self._binding

    def read_status(self) -> dict[str, Any]:
        return self._send("run.status.v1", {})

    def read_evidence(self, campaign_digest: str) -> dict[str, Any]:
        _require_digest(campaign_digest, "RUN_CLIENT_CAMPAIGN_INVALID")
        return self._send("run.evidence.v1", {"campaign_digest": campaign_digest})

    def prepare_campaign(
        self,
        campaign_digest: str,
        candidate_digest: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        _require_digest(campaign_digest, "RUN_CLIENT_CAMPAIGN_INVALID")
        _require_digest(candidate_digest, "RUN_CLIENT_CANDIDATE_INVALID")
        _require_idempotency_key(idempotency_key)
        return self._send(
            "run.prepare_campaign.v1",
            {
                "campaign_digest": campaign_digest,
                "candidate_digest": candidate_digest,
                "idempotency_key": idempotency_key,
            },
        )

    def pause_campaign(self, campaign_digest: str, idempotency_key: str) -> dict[str, Any]:
        _require_digest(campaign_digest, "RUN_CLIENT_CAMPAIGN_INVALID")
        _require_idempotency_key(idempotency_key)
        return self._send(
            "run.pause_campaign.v1",
            {
                "campaign_digest": campaign_digest,
                "idempotency_key": idempotency_key,
            },
        )

    def emergency_stop(self, idempotency_key: str) -> dict[str, Any]:
        _require_idempotency_key(idempotency_key)
        return self._send(
            "run.emergency_stop.v1",
            {"idempotency_key": idempotency_key},
        )

    def _send(self, operation: str, body: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "contract_version": 1,
            "gateway": self._binding.gateway,
            "run_digest": self._binding.run_digest,
            **body,
        }
        raw = self._transport.request(self._binding.endpoint, operation, payload)
        if not isinstance(raw, Mapping):
            raise RuntimeError("RUN_CLIENT_RESPONSE_INVALID")
        response = dict(raw)
        if not set(response) <= _RESPONSE_FIELDS or not {
            "contract_version",
            "gateway",
            "run_digest",
            "operation",
            "state",
            "receipt_digest",
        } <= set(response):
            raise RuntimeError("RUN_CLIENT_RESPONSE_INVALID")
        if (
            response["contract_version"] != 1
            or response["gateway"] != self._binding.gateway
            or response["run_digest"] != self._binding.run_digest
            or response["operation"] != operation
            or not isinstance(response["state"], str)
            or not response["state"]
            or _DIGEST.fullmatch(response["receipt_digest"]) is None
        ):
            raise RuntimeError("RUN_CLIENT_RESPONSE_IDENTITY_MISMATCH")
        _assert_no_forbidden_response(response)
        return response


def _require_digest(value: str, code: str) -> None:
    if _DIGEST.fullmatch(value) is None:
        raise ValueError(code)


def _require_idempotency_key(value: str) -> None:
    if not isinstance(value, str) or not 16 <= len(value) <= 128:
        raise ValueError("RUN_CLIENT_IDEMPOTENCY_KEY_INVALID")


def _assert_no_forbidden_response(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or key.lower() in _FORBIDDEN_RESPONSE_KEYS:
                raise RuntimeError("RUN_CLIENT_RESPONSE_REDACTION_FAILED")
            _assert_no_forbidden_response(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_forbidden_response(item)
