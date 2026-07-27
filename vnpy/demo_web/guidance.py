"""Strict loopback client for the isolated demo Side Master service."""

from __future__ import annotations

from base64 import b64decode
from binascii import Error as BinasciiError
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
import re
from time import time_ns
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit
from uuid import UUID

from blake3 import blake3
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)


_DIGEST_PATTERN = r"^(?:sha256|blake3):[0-9a-f]{64}$"
_DIGEST = re.compile(_DIGEST_PATTERN)
_HEX_32 = re.compile(r"^[0-9a-f]{64}$")
_HEX_64 = re.compile(r"^[0-9a-f]{128}$")
_CHAT_OPERATION = "demo.side_master.chat.v1"
_DECISION_OPERATION = "demo.side_master.proposal.decide.v1"
_FORBIDDEN_RESPONSE_KEYS = frozenset(
    {
        "account",
        "account_id",
        "account_fingerprint",
        "cancel",
        "cancel_order",
        "cancel_request",
        "credential",
        "credential_ref",
        "gateway",
        "gateway_handle",
        "main_engine",
        "order",
        "order_request",
        "password",
        "private_key",
        "risk_mutation",
        "rpc",
        "rpc_endpoint",
        "send_order",
        "server_fingerprint",
        "state_store_path",
        "token",
    }
)


class GuidanceRpcTransport(Protocol):
    """Transport used internally by the two fixed guidance operations."""

    def request(
        self,
        endpoint: str,
        operation: str,
        payload: dict[str, Any],
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class GuidanceClientBinding:
    endpoint: str
    operator_identity_digest: str

    def __post_init__(self) -> None:
        parsed = urlsplit(self.endpoint)
        try:
            port = parsed.port
        except (BinasciiError, ValueError) as exc:
            raise ValueError("GUIDANCE_LOOPBACK_REQUIRED") from exc
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or port is None
            or not 1 <= port <= 65_535
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("GUIDANCE_LOOPBACK_REQUIRED")
        if _DIGEST.fullmatch(self.operator_identity_digest) is None:
            raise ValueError("GUIDANCE_OPERATOR_IDENTITY_INVALID")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _DynamicContent(_StrictModel):
    media_type: Literal["text/plain; charset=utf-8", "application/json"]
    body: Any
    canonical_body_base64: str = Field(min_length=1)
    body_digest: str = Field(pattern=r"^blake3:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def verify_exact_content(self) -> _DynamicContent:
        try:
            exact = b64decode(self.canonical_body_base64, validate=True)
        except ValueError as exc:
            raise ValueError("dynamic content base64 is invalid") from exc
        if not exact or f"blake3:{blake3(exact).hexdigest()}" != self.body_digest:
            raise ValueError("dynamic content digest mismatch")
        if self.media_type == "text/plain; charset=utf-8":
            if not isinstance(self.body, str) or self.body.encode() != exact:
                raise ValueError("dynamic text content mismatch")
        else:
            try:
                decoded = json.loads(exact)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("dynamic JSON content is invalid") from exc
            if decoded != self.body:
                raise ValueError("dynamic JSON content mismatch")
        return self


class _Proposal(_StrictModel):
    contract_version: Literal[1]
    entity_type: Literal["side_master_approval_proposal"]
    proposal_id: str
    session_id: str = Field(min_length=1, max_length=128)
    mission_id: str = Field(min_length=1, max_length=128)
    side_master_identity: str = Field(min_length=1, max_length=128)
    source_turn_digest: str = Field(pattern=_DIGEST_PATTERN)
    material_direction_change: Literal[True]
    interpretation: str = Field(min_length=1, max_length=4_000)
    proposed_guidance: str = Field(min_length=1, max_length=8_000)
    provider_outcome: Literal["certain", "uncertain"]
    state: Literal["pending", "confirmed", "rejected", "expired", "uncertain"]
    created_at_ms: int = Field(ge=0)
    expires_at_ms: int = Field(ge=0)
    proposal_digest: str = Field(pattern=_DIGEST_PATTERN)

    @field_validator("proposal_id")
    @classmethod
    def validate_proposal_id(cls, value: str) -> str:
        return _require_uuid(value)

    @model_validator(mode="after")
    def validate_state(self) -> _Proposal:
        if self.expires_at_ms <= self.created_at_ms:
            raise ValueError("proposal expiry is invalid")
        if (self.provider_outcome == "uncertain") != (self.state == "uncertain"):
            raise ValueError("proposal certainty and state disagree")
        return self


class _ChatResult(_StrictModel):
    contract_version: Literal[1]
    entity_type: Literal["demo_side_master_chat_result"]
    session_id: str = Field(min_length=1, max_length=128)
    mission_id: str = Field(min_length=1, max_length=128)
    state: Literal["completed", "uncertain"]
    reply: _DynamicContent | None
    proposal: _Proposal | None
    provider_outcome: Literal["certain", "uncertain"]
    result_digest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def validate_outcome(self) -> _ChatResult:
        if self.state == "uncertain":
            if (
                self.provider_outcome != "uncertain"
                or self.reply is not None
                or self.proposal is not None
            ):
                raise ValueError("uncertain chat result must have no effect")
        elif self.provider_outcome != "certain" or self.reply is None:
            raise ValueError("completed chat result is incomplete")
        if self.proposal is not None and (
            str(self.proposal.session_id) != self.session_id
            or self.proposal.mission_id != self.mission_id
        ):
            raise ValueError("proposal identity mismatch")
        return self


class _GuidanceRevision(_StrictModel):
    contract_version: Literal[1]
    entity_type: Literal["confirmed_future_research_guidance"]
    guidance_id: str
    proposal_id: str
    proposal_digest: str = Field(pattern=_DIGEST_PATTERN)
    mission_id: str = Field(min_length=1, max_length=128)
    guidance: str = Field(min_length=1, max_length=8_000)
    operator_identity_digest: str = Field(pattern=_DIGEST_PATTERN)
    confirmed_at_ms: int = Field(ge=0)
    scope: Literal["future_research_only"]
    not_before_safe_boundary_revision: int = Field(gt=0)
    delivery_id: str
    active_campaign_immutable: bool
    signer_id: str = Field(min_length=1, max_length=128)
    verifying_key: str
    guidance_digest: str = Field(pattern=_DIGEST_PATTERN)
    signature: str

    @field_validator("guidance_id", "proposal_id", "delivery_id")
    @classmethod
    def validate_uuid_fields(cls, value: str) -> str:
        return _require_uuid(value)

    @model_validator(mode="after")
    def validate_signature_shape(self) -> _GuidanceRevision:
        if _HEX_32.fullmatch(self.verifying_key) is None:
            raise ValueError("guidance verifying key is invalid")
        if _HEX_64.fullmatch(self.signature) is None:
            raise ValueError("guidance signature is invalid")
        return self


class _DecisionReceipt(_StrictModel):
    proposal: _Proposal
    guidance: _GuidanceRevision | None
    idempotency_key: str = Field(min_length=16, max_length=128)
    decision_digest: str = Field(pattern=_DIGEST_PATTERN)


class _ChatCommand(_StrictModel):
    session_id: str = Field(min_length=1, max_length=128)
    mission_id: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=8_000)
    idempotency_key: str = Field(min_length=16, max_length=128)

    @model_validator(mode="after")
    def reject_blank_values(self) -> _ChatCommand:
        if not self.session_id.strip() or not self.mission_id.strip() or not self.content.strip():
            raise ValueError("blank chat input")
        return self


class _DecisionCommand(_StrictModel):
    expected_proposal_digest: str = Field(pattern=_DIGEST_PATTERN)
    idempotency_key: str = Field(min_length=16, max_length=128)


class SideMasterGuidanceClient:
    """Expose two fixed research-only operations with server-owned context."""

    def __init__(
        self,
        binding: GuidanceClientBinding,
        transport: GuidanceRpcTransport,
        *,
        active_campaign: Callable[[], bool],
        next_safe_boundary_revision: Callable[[], int],
        clock_ms: Callable[[], int] = lambda: time_ns() // 1_000_000,
        proposal_ttl_ms: int = 15 * 60 * 1_000,
    ) -> None:
        if (
            not callable(active_campaign)
            or not callable(next_safe_boundary_revision)
            or not callable(clock_ms)
            or not 1_000 <= proposal_ttl_ms <= 24 * 60 * 60 * 1_000
        ):
            raise ValueError("GUIDANCE_CONFIGURATION_INVALID")
        self._binding = binding
        self._transport = transport
        self._active_campaign = active_campaign
        self._next_safe_boundary_revision = next_safe_boundary_revision
        self._clock_ms = clock_ms
        self._proposal_ttl_ms = proposal_ttl_ms

    def send_message(self, command: Mapping[str, Any]) -> dict[str, Any]:
        parsed = _validate(_ChatCommand, command, "GUIDANCE_CHAT_REQUEST_INVALID")
        now_ms = _server_time(self._clock_ms)
        active_campaign = _server_campaign_state(self._active_campaign)
        payload = {
            **parsed.model_dump(mode="json"),
            "active_campaign": active_campaign,
            "created_at_ms": now_ms,
            "proposal_expires_at_ms": now_ms + self._proposal_ttl_ms,
        }
        raw = self._transport.request(self._binding.endpoint, _CHAT_OPERATION, payload)
        result = _validate(_ChatResult, raw, "GUIDANCE_CHAT_RESPONSE_INVALID")
        if result.session_id != parsed.session_id or result.mission_id != parsed.mission_id:
            raise RuntimeError("GUIDANCE_CHAT_RESPONSE_IDENTITY_MISMATCH")
        public = result.model_dump(mode="json")
        _assert_no_forbidden_response(public)
        return public

    def decide_proposal(
        self,
        proposal_id: str,
        decision: Literal["confirm", "reject"],
        command: Mapping[str, Any],
    ) -> dict[str, Any]:
        try:
            parsed_proposal_id = UUID(proposal_id)
        except (ValueError, AttributeError) as exc:
            raise ValueError("GUIDANCE_PROPOSAL_ID_INVALID") from exc
        if decision not in {"confirm", "reject"}:
            raise ValueError("GUIDANCE_DECISION_INVALID")
        parsed = _validate(_DecisionCommand, command, "GUIDANCE_DECISION_REQUEST_INVALID")
        now_ms = _server_time(self._clock_ms)
        active_campaign = _server_campaign_state(self._active_campaign)
        safe_boundary = _server_safe_boundary(self._next_safe_boundary_revision)
        payload = {
            "proposal_id": str(parsed_proposal_id),
            "expected_proposal_digest": parsed.expected_proposal_digest,
            "decision": decision,
            "operator_identity_digest": self._binding.operator_identity_digest,
            "idempotency_key": parsed.idempotency_key,
            "decided_at_ms": now_ms,
            "next_safe_boundary_revision": safe_boundary,
            "active_campaign": active_campaign,
        }
        raw = self._transport.request(self._binding.endpoint, _DECISION_OPERATION, payload)
        receipt = _validate(_DecisionReceipt, raw, "GUIDANCE_DECISION_RESPONSE_INVALID")
        expected_state = "confirmed" if decision == "confirm" else "rejected"
        if (
            str(receipt.proposal.proposal_id) != str(parsed_proposal_id)
            or receipt.proposal.proposal_digest != parsed.expected_proposal_digest
            or receipt.proposal.state != expected_state
            or receipt.idempotency_key != parsed.idempotency_key
        ):
            raise RuntimeError("GUIDANCE_DECISION_RESPONSE_IDENTITY_MISMATCH")
        if decision == "confirm":
            guidance = receipt.guidance
            if guidance is None or (
                str(guidance.proposal_id) != str(parsed_proposal_id)
                or guidance.proposal_digest != parsed.expected_proposal_digest
                or guidance.operator_identity_digest
                != self._binding.operator_identity_digest
                or guidance.not_before_safe_boundary_revision != safe_boundary
                or guidance.active_campaign_immutable != active_campaign
            ):
                raise RuntimeError("GUIDANCE_CONFIRMATION_RESPONSE_INVALID")
        elif receipt.guidance is not None:
            raise RuntimeError("GUIDANCE_REJECTION_CREATED_EFFECT")
        public = receipt.model_dump(mode="json")
        _assert_no_forbidden_response(public)
        return public


def _validate(
    model: type[BaseModel],
    value: Any,
    error_code: str,
) -> Any:
    if not isinstance(value, Mapping):
        raise RuntimeError(error_code)
    try:
        return model.model_validate(dict(value), strict=True)
    except ValidationError as exc:
        raise RuntimeError(error_code) from exc


def _require_uuid(value: str) -> str:
    try:
        UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError("UUID is invalid") from exc
    return value


def _server_time(clock: Callable[[], int]) -> int:
    value = clock()
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RuntimeError("GUIDANCE_CLOCK_INVALID")
    return value


def _server_campaign_state(source: Callable[[], bool]) -> bool:
    value = source()
    if not isinstance(value, bool):
        raise RuntimeError("GUIDANCE_CAMPAIGN_STATE_INVALID")
    return value


def _server_safe_boundary(source: Callable[[], int]) -> int:
    value = source()
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise RuntimeError("GUIDANCE_SAFE_BOUNDARY_INVALID")
    return value


def _assert_no_forbidden_response(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or key.lower() in _FORBIDDEN_RESPONSE_KEYS:
                raise RuntimeError("GUIDANCE_RESPONSE_REDACTION_FAILED")
            _assert_no_forbidden_response(item)
    elif isinstance(value, list | tuple):
        for item in value:
            _assert_no_forbidden_response(item)
