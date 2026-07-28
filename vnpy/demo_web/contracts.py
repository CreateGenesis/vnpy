"""Strict contracts shared by the trusted operations console boundary."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


_DIGEST = r"^(?:sha256|blake3):[0-9a-f]{64}$"
_SYMBOL = r"^[0-9]{6}\.(?:SH|SZ|BJ)$"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ServiceName(str, Enum):
    WEB = "web"
    RESEARCH = "research"
    MODEL_XTP = "model_xtp"
    MODEL_TORA = "model_tora"
    RUN_XTP = "run_xtp"
    RUN_TORA = "run_tora"
    RQDATA_FETCHER = "rqdata_fetcher"


class ServiceAction(str, Enum):
    START = "start"
    STOP = "stop"
    RESTART = "restart"


class GatewayName(str, Enum):
    XTP = "XTP"
    TORA = "TORA"


class GatewayAction(str, Enum):
    START = "start"
    STOP = "stop"
    RECONNECT = "reconnect"
    SELECT = "select"


class OperatorSection(StrictModel):
    sid: str = Field(min_length=3, max_length=256)
    display_name: str = Field(min_length=1, max_length=256)


class PortsSection(StrictModel):
    web: int = Field(ge=1, le=65_535)
    agentd: int = Field(default=8781, ge=1, le=65_535)
    model_xtp: int = Field(default=8782, ge=1, le=65_535)
    model_tora: int = Field(default=8783, ge=1, le=65_535)
    run_xtp: int = Field(default=8784, ge=1, le=65_535)
    run_tora: int = Field(default=8785, ge=1, le=65_535)

    @field_validator("run_tora")
    @classmethod
    def ports_are_unique(cls, value: int, info: Any) -> int:
        values = [item for item in info.data.values() if isinstance(item, int)] + [value]
        if len(values) != len(set(values)):
            raise ValueError("CONFIGURATION_PORT_DUPLICATE")
        return value


class RqdataSection(StrictModel):
    endpoint: str = Field(default="https://rqdatac.com", min_length=8, max_length=512)
    tick_required: bool = True


class ModelRouteSection(StrictModel):
    base_url: str = Field(min_length=8, max_length=512)
    model: str = Field(min_length=1, max_length=256)
    retry_count: int = Field(default=1, ge=0, le=3)


class XtpSection(StrictModel):
    account: str = Field(min_length=1, max_length=128)
    client_id: int = Field(ge=1, le=255)
    quote_address: str = Field(min_length=1, max_length=512)
    quote_port: int = Field(ge=1, le=65_535)
    trading_address: str = Field(min_length=1, max_length=512)
    trading_port: int = Field(ge=1, le=65_535)
    quote_protocol: Literal["TCP", "UDP"] = "TCP"
    log_level: Literal["FATAL", "ERROR", "WARNING", "INFO", "DEBUG", "TRACE"] = "INFO"


class ToraSection(StrictModel):
    account: str = Field(min_length=1, max_length=128)
    product_id: str = Field(min_length=1, max_length=128)
    account_type: Literal["用户代码", "资金账号"] = "资金账号"
    address_type: Literal["前置地址", "FENS地址"] = "前置地址"
    quote_server: str = Field(min_length=1, max_length=512)
    trading_server: str = Field(min_length=1, max_length=512)


class ConfigurationDraftUpdate(StrictModel):
    expected_revision: int = Field(ge=0)
    sections: dict[str, dict[str, Any]]
    secret_updates: dict[str, str] = Field(default_factory=dict)
    clear_secrets: list[str] = Field(default_factory=list)


class Blocker(StrictModel):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,127}$")
    parameters: dict[str, str | int | bool | None] = Field(default_factory=dict)


class ActionStateV2(StrictModel):
    contract_version: Literal[2] = 2
    action_id: str = Field(min_length=1, max_length=128)
    target: str = Field(min_length=1, max_length=128)
    state: Literal["enabled", "blocked", "running"]
    blockers: list[Blocker] = Field(max_length=32)
    remediation: list[str] = Field(max_length=16)
    expected_revision: int = Field(ge=0)


class ReadyCandidateV2(StrictModel):
    contract_version: Literal[2] = 2
    publication_id: str
    revision: int = Field(ge=1)
    state: Literal["ready", "expired", "invalidated", "rollback"]
    candidate_digest: str = Field(pattern=_DIGEST)
    package_digest: str = Field(pattern=_DIGEST)
    configuration_digest: str = Field(pattern=_DIGEST)
    policy_digest: str = Field(pattern=_DIGEST)
    data_snapshot_digest: str = Field(pattern=_DIGEST)
    feature_schema_digest: str = Field(pattern=_DIGEST)
    thresholds_digest: str = Field(pattern=_DIGEST)
    review_digest: str = Field(pattern=_DIGEST)
    evaluation_digest: str = Field(pattern=_DIGEST)
    runtime_profile_digest: str = Field(pattern=_DIGEST)
    rollback_digest: str = Field(pattern=_DIGEST)
    author_lineage_digest: str = Field(pattern=_DIGEST)
    symbols: list[str] = Field(min_length=1, max_length=20)
    valid_until_ms: int = Field(ge=1)
    lifecycle_revision: int = Field(ge=1)
    publisher_identity: str = Field(min_length=1, max_length=128)
    publisher_key_fingerprint: str = Field(pattern=_DIGEST)
    created_at_ms: int = Field(ge=1)
    payload_digest: str = Field(pattern=_DIGEST)
    signature: str = Field(pattern=r"^[0-9a-f]{128}$")

    @field_validator("symbols")
    @classmethod
    def symbols_are_unique_and_canonical(cls, value: list[str]) -> list[str]:
        import re

        if len(value) != len(set(value)) or any(re.fullmatch(_SYMBOL, item) is None for item in value):
            raise ValueError("READY_CANDIDATE_SYMBOL_INVALID")
        return value


SUPPORTED_ACTIONS: tuple[str, ...] = (
    "configuration.save",
    "configuration.test",
    "configuration.activate",
    "service.research.start",
    "service.research.stop",
    "service.research.restart",
    "service.model_xtp.start",
    "service.model_xtp.stop",
    "service.model_xtp.restart",
    "service.model_tora.start",
    "service.model_tora.stop",
    "service.model_tora.restart",
    "gateway.xtp.start",
    "gateway.xtp.stop",
    "gateway.xtp.reconnect",
    "gateway.xtp.select",
    "gateway.tora.start",
    "gateway.tora.stop",
    "gateway.tora.reconnect",
    "gateway.tora.select",
    "campaign.start",
    "campaign.pause",
    "campaign.emergency_stop",
)


def build_action_catalog(
    *,
    revision: int,
    configuration_active: bool,
    candidate_ready: bool,
    selected_gateways: set[str],
    gateway_states: dict[str, str],
    campaign_state: str,
) -> list[dict[str, Any]]:
    """Return every supported action, including blocked controls."""

    states: list[ActionStateV2] = []
    for action_id in SUPPORTED_ACTIONS:
        blockers: list[Blocker] = []
        remediation: list[str] = []
        state: Literal["enabled", "blocked", "running"] = "enabled"
        if action_id.startswith(("gateway.", "service.")) and not configuration_active:
            blockers.append(Blocker(code="CONFIGURATION_NOT_ACTIVE"))
            remediation.append("open_settings")
        if action_id == "configuration.activate" and campaign_state in {"starting", "active", "pausing"}:
            blockers.append(Blocker(code="CAMPAIGN_ACTIVE"))
            remediation.append("pause_campaign")
        if action_id == "campaign.start":
            if not configuration_active:
                blockers.append(Blocker(code="CONFIGURATION_NOT_ACTIVE"))
                remediation.append("open_settings")
            if not candidate_ready:
                blockers.append(Blocker(code="CANDIDATE_NOT_READY"))
                remediation.append("open_models")
            if not selected_gateways:
                blockers.append(Blocker(code="GATEWAY_NOT_SELECTED"))
                remediation.append("select_gateway")
            for gateway in sorted(selected_gateways):
                if gateway_states.get(gateway) != "connected":
                    blockers.append(
                        Blocker(code="SELECTED_GATEWAY_NOT_READY", parameters={"gateway": gateway})
                    )
                    remediation.append(f"start_{gateway.lower()}")
        if action_id == "campaign.pause" and campaign_state not in {"starting", "active"}:
            blockers.append(Blocker(code="CAMPAIGN_NOT_ACTIVE"))
        if action_id == "campaign.emergency_stop":
            blockers.clear()
            remediation.clear()
        if blockers:
            state = "blocked"
        target = action_id.rsplit(".", 1)[0]
        states.append(
            ActionStateV2(
                action_id=action_id,
                target=target,
                state=state,
                blockers=blockers,
                remediation=list(dict.fromkeys(remediation)),
                expected_revision=revision,
            )
        )
    return [item.model_dump(mode="json") for item in states]
