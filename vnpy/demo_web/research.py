"""Strict loopback client for durable main-Master research tasks."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
import json
import re
from time import time_ns
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


_DIGEST_PATTERN = r"^(?:sha256|blake3):[0-9a-f]{64}$"
_DIGEST = re.compile(_DIGEST_PATTERN)
_LIST_OPERATION = "demo.research.tasks.list.v1"
_CREATE_OPERATION = "demo.research.tasks.create.v1"
_CANCEL_OPERATION = "demo.research.tasks.cancel.v1"
_FORBIDDEN_KEYS = frozenset(
    {
        "account",
        "account_id",
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
        "send_order",
        "token",
    }
)


class ResearchRpcTransport(Protocol):
    def request(
        self,
        endpoint: str,
        operation: str,
        payload: dict[str, Any],
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class ResearchClientBinding:
    endpoint: str
    operator_identity_digest: str

    def __post_init__(self) -> None:
        parsed = urlsplit(self.endpoint)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("RESEARCH_LOOPBACK_REQUIRED") from exc
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
            raise ValueError("RESEARCH_LOOPBACK_REQUIRED")
        if _DIGEST.fullmatch(self.operator_identity_digest) is None:
            raise ValueError("RESEARCH_OPERATOR_IDENTITY_INVALID")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ResearchTaskV1(_StrictModel):
    contract_version: Literal[1]
    task_id: str
    task_digest: str = Field(pattern=_DIGEST_PATTERN)
    source: Literal["operator", "side_master_proposal"]
    source_digest: str = Field(pattern=_DIGEST_PATTERN)
    operator_confirmation_digest: str = Field(pattern=_DIGEST_PATTERN)
    mission_id: str = Field(min_length=1, max_length=128)
    objective: str = Field(min_length=1, max_length=8_000)
    constraints: list[str] = Field(max_length=64)
    data_references: list[str] = Field(max_length=64)
    budget_digest: str = Field(pattern=_DIGEST_PATTERN)
    author_lineage_digest: str = Field(pattern=_DIGEST_PATTERN)
    priority: Literal["routine", "high", "safety"]
    created_at_ms: int = Field(gt=0)
    expires_at_ms: int = Field(gt=0)
    deduplication_key: str = Field(min_length=16, max_length=128)
    not_before_boundary: Literal["immediate_safe_boundary", "campaign_terminal"]
    state: Literal["queued", "running", "completed", "blocked", "failed", "cancelled"]

    @field_validator("task_id")
    @classmethod
    def validate_task_id(cls, value: str) -> str:
        try:
            UUID(value)
        except (ValueError, AttributeError) as exc:
            raise ValueError("research task UUID is invalid") from exc
        return value

    @field_validator("constraints")
    @classmethod
    def validate_constraints(cls, value: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 512 for item in value):
            raise ValueError("research constraints are invalid")
        return value

    @field_validator("data_references")
    @classmethod
    def validate_data_references(cls, value: list[str]) -> list[str]:
        if any(_DIGEST.fullmatch(item) is None for item in value):
            raise ValueError("research data reference is invalid")
        return value

    @model_validator(mode="after")
    def validate_expiry(self) -> ResearchTaskV1:
        if self.expires_at_ms <= self.created_at_ms:
            raise ValueError("research task expiry is invalid")
        return self


class _IpcListResponse(_StrictModel):
    contract_version: Literal[1]
    status: Literal["ok"]
    operation: Literal["demo.research.tasks.list.v1"]
    authority: Literal["research_only"]
    tasks: list[ResearchTaskV1]


class _IpcMutationResponse(_StrictModel):
    contract_version: Literal[1]
    status: Literal["ok"]
    operation: Literal[
        "demo.research.tasks.create.v1",
        "demo.research.tasks.cancel.v1",
    ]
    authority: Literal["research_only"]
    task: ResearchTaskV1


class _IpcErrorDetail(_StrictModel):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,127}$")
    message: str = Field(min_length=1, max_length=512)


class _IpcErrorResponse(_StrictModel):
    contract_version: Literal[1]
    status: Literal["error"]
    error: _IpcErrorDetail
    authority: Literal["research_only"]


class _CreateCommand(_StrictModel):
    mission_id: str = Field(min_length=1, max_length=128)
    objective: str = Field(min_length=1, max_length=8_000)
    constraints: list[str] = Field(max_length=64)
    data_references: list[str] = Field(max_length=64)
    priority: Literal["routine", "high", "safety"]
    expires_at_ms: int = Field(gt=0)
    idempotency_key: str = Field(min_length=16, max_length=128)


class _CancelCommand(_StrictModel):
    expected_task_digest: str = Field(pattern=_DIGEST_PATTERN)
    idempotency_key: str = Field(min_length=16, max_length=128)


class ResearchTaskClient:
    """Expose only list, create, cancel, and read-only projection operations."""

    def __init__(
        self,
        binding: ResearchClientBinding,
        transport: ResearchRpcTransport,
        *,
        active_campaign: Callable[[], bool],
        clock_ms: Callable[[], int] = lambda: time_ns() // 1_000_000,
    ) -> None:
        if not callable(active_campaign) or not callable(clock_ms):
            raise ValueError("RESEARCH_CONFIGURATION_INVALID")
        self._binding = binding
        self._transport = transport
        self._active_campaign = active_campaign
        self._clock_ms = clock_ms

    def list_tasks(self) -> dict[str, Any]:
        raw = self._transport.request(self._binding.endpoint, _LIST_OPERATION, {})
        result = _validate_ipc(
            _IpcListResponse,
            raw,
            "RESEARCH_LIST_RESPONSE_INVALID",
        )
        return _task_list_public(result.tasks)

    def create_task(self, command: Mapping[str, Any]) -> dict[str, Any]:
        parsed = _validate(_CreateCommand, command, "RESEARCH_CREATE_REQUEST_INVALID")
        now_ms = _server_time(self._clock_ms)
        if parsed.expires_at_ms <= now_ms:
            raise ValueError("RESEARCH_EXPIRY_INVALID")
        payload = {
            **parsed.model_dump(mode="json"),
            "operator_identity_digest": self._binding.operator_identity_digest,
            "created_at_ms": now_ms,
            "active_campaign": _campaign_state(self._active_campaign),
        }
        raw = self._transport.request(self._binding.endpoint, _CREATE_OPERATION, payload)
        result = _validate_ipc(
            _IpcMutationResponse,
            raw,
            "RESEARCH_CREATE_RESPONSE_INVALID",
        )
        if (
            result.operation != _CREATE_OPERATION
            or result.task.source != "operator"
            or result.task.mission_id != parsed.mission_id
        ):
            raise RuntimeError("RESEARCH_CREATE_RESPONSE_IDENTITY_MISMATCH")
        return _task_mutation_public(result.task)

    def cancel_task(self, task_id: str, command: Mapping[str, Any]) -> dict[str, Any]:
        try:
            parsed_task_id = str(UUID(task_id))
        except (ValueError, AttributeError) as exc:
            raise ValueError("RESEARCH_TASK_ID_INVALID") from exc
        parsed = _validate(_CancelCommand, command, "RESEARCH_CANCEL_REQUEST_INVALID")
        raw = self._transport.request(
            self._binding.endpoint,
            _CANCEL_OPERATION,
            {
                **parsed.model_dump(mode="json"),
                "task_id": parsed_task_id,
                "operator_identity_digest": self._binding.operator_identity_digest,
            },
        )
        result = _validate_ipc(
            _IpcMutationResponse,
            raw,
            "RESEARCH_CANCEL_RESPONSE_INVALID",
        )
        if (
            result.operation != _CANCEL_OPERATION
            or result.task.task_id != parsed_task_id
            or result.task.task_digest != parsed.expected_task_digest
            or result.task.state != "cancelled"
        ):
            raise RuntimeError("RESEARCH_CANCEL_RESPONSE_IDENTITY_MISMATCH")
        return _task_mutation_public(result.task)

    def projection(self) -> dict[str, Any]:
        return self.list_tasks()


def _validate(model: type[BaseModel], value: Any, error_code: str) -> Any:
    if not isinstance(value, Mapping):
        raise RuntimeError(error_code)
    try:
        return model.model_validate(dict(value), strict=True)
    except ValidationError as exc:
        raise RuntimeError(error_code) from exc


def _validate_ipc(model: type[BaseModel], value: Any, error_code: str) -> Any:
    if isinstance(value, Mapping) and value.get("status") == "error":
        error = _validate(_IpcErrorResponse, value, error_code)
        raise RuntimeError(f"RESEARCH_IPC_{error.error.code}")
    return _validate(model, value, error_code)


def _task_list_public(tasks: list[ResearchTaskV1]) -> dict[str, Any]:
    rendered = [task.model_dump(mode="json") for task in tasks]
    value = {"revision": _projection_revision(rendered), "tasks": rendered}
    _walk_public(value)
    return value


def _task_mutation_public(task: ResearchTaskV1) -> dict[str, Any]:
    rendered = task.model_dump(mode="json")
    value = {"revision": _projection_revision([rendered]), "task": rendered}
    _walk_public(value)
    return value


def _projection_revision(tasks: list[dict[str, Any]]) -> int:
    encoded = json.dumps(
        tasks,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return int.from_bytes(sha256(encoded).digest()[:8], "big") & ((1 << 53) - 1)


def _walk_public(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or key.casefold() in _FORBIDDEN_KEYS:
                raise RuntimeError("RESEARCH_RESPONSE_REDACTION_FAILED")
            _walk_public(item)
    elif isinstance(value, list | tuple):
        for item in value:
            _walk_public(item)


def _server_time(clock: Callable[[], int]) -> int:
    value = clock()
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise RuntimeError("RESEARCH_CLOCK_INVALID")
    return value


def _campaign_state(source: Callable[[], bool]) -> bool:
    value = source()
    if not isinstance(value, bool):
        raise RuntimeError("RESEARCH_CAMPAIGN_STATE_INVALID")
    return value
