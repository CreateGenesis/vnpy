"""Loopback-only FastAPI surface for the investor demonstration."""

from __future__ import annotations

from asyncio import sleep
from collections.abc import Mapping
from dataclasses import dataclass
from html import escape
from hmac import compare_digest
from pathlib import Path
import re
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from .security import BootstrapSessionManager
from .contracts import OperationRejected


_SESSION_COOKIE = "auto_trade_host_session"
_CSRF_PLACEHOLDER = "__AUTO_TRADE_CSRF_TOKEN__"
_STATIC_DIR = Path(__file__).with_name("static")
_DIGEST_PATTERN = r"^(?:sha256|blake3):[0-9a-f]{64}$"
_CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
        "object-src 'none'",
        "form-action 'none'",
        "connect-src 'self'",
        "img-src 'self' data:",
        "style-src 'self'",
        "script-src 'self'",
    )
)
_FORBIDDEN_PUBLIC_KEYS = frozenset(
    {
        "cancel_order",
        "cancel_request",
        "credential",
        "credential_ref",
        "credential_value",
        "main_engine",
        "order",
        "order_request",
        "password",
        "private_key",
        "rpc",
        "rpc_endpoint",
        "secret",
        "secret_value",
        "send_order",
        "state_store_path",
        "token",
        "api_key",
        "access_token",
        "refresh_token",
    }
)
_SAFE_SECRET_STATUS_SUFFIXES = ("_configured", "_fingerprint", "_status")
_SECRET_MATERIAL_SUFFIXES = ("_api_key", "_credential", "_password", "_secret", "_token")
_ALLOWED_PUBLIC_TOKEN_KEYS = frozenset({"csrf_token"})


@dataclass(frozen=True)
class DemoWebConfig:
    bind_host: str
    port: int
    allowed_origin: str
    session_token: str
    csrf_token: str

    def validate(self) -> None:
        if self.bind_host != "127.0.0.1" or not 1 <= self.port <= 65_535:
            raise ValueError("DEMO_LOOPBACK_BIND_REQUIRED")
        parsed = urlsplit(self.allowed_origin)
        try:
            origin_port = parsed.port
        except ValueError as exc:
            raise ValueError("DEMO_SAME_ORIGIN_REQUIRED") from exc
        if (
            parsed.scheme != "http"
            or parsed.hostname != self.bind_host
            or origin_port != self.port
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("DEMO_SAME_ORIGIN_REQUIRED")
        if (
            not 32 <= len(self.session_token) <= 256
            or not 32 <= len(self.csrf_token) <= 256
            or self.session_token == self.csrf_token
        ):
            raise ValueError("DEMO_SESSION_CONFIGURATION_INVALID")


class DemoWebBackend(Protocol):
    """Bounded application service; no broker method belongs to this interface."""

    def readiness(self) -> dict[str, Any]: ...

    def projection(self) -> dict[str, Any]: ...

    def start_campaign(self, command: dict[str, Any]) -> dict[str, Any]: ...

    def pause_campaign(self, campaign_id: str) -> dict[str, Any]: ...

    def emergency_stop(self) -> dict[str, Any]: ...

    def evidence(self, campaign_id: str) -> dict[str, Any]: ...


class DemoGuidanceBackend(Protocol):
    """Research-only Side Master boundary; never a main-Master or trading bridge."""

    def send_message(self, command: Mapping[str, Any]) -> dict[str, Any]: ...

    def decide_proposal(
        self,
        proposal_id: str,
        decision: Literal["confirm", "reject"],
        command: Mapping[str, Any],
    ) -> dict[str, Any]: ...


class DemoResearchBackend(Protocol):
    """Research-only task control; this surface has no broker capabilities."""

    def list_tasks(self) -> dict[str, Any]: ...

    def create_task(self, command: dict[str, Any]) -> dict[str, Any]: ...

    def cancel_task(
        self, task_id: str, command: dict[str, Any]
    ) -> dict[str, Any]: ...

    def projection(self) -> dict[str, Any]: ...


class DemoOperationsBackend(Protocol):
    """Configuration and fixed process control, with no generic command surface."""

    def system(self) -> dict[str, Any]: ...

    def configuration_draft(self) -> dict[str, Any]: ...

    def update_configuration(self, command: dict[str, Any]) -> dict[str, Any]: ...

    def test_configuration(self, command: dict[str, Any]) -> dict[str, Any]: ...

    def activate_configuration(self, command: dict[str, Any]) -> dict[str, Any]: ...

    def control_service(
        self, service: str, action: str, command: dict[str, Any]
    ) -> dict[str, Any]: ...

    def control_gateway(
        self, gateway: str, action: str, command: dict[str, Any]
    ) -> dict[str, Any]: ...


class CampaignStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    candidate_digest: str = Field(pattern=_DIGEST_PATTERN)
    gateways: list[Literal["XTP", "TORA"]] | None = Field(
        default=None, min_length=1, max_length=2
    )
    idempotency_key: str = Field(min_length=16, max_length=128)

    @field_validator("gateways")
    @classmethod
    def require_unique_gateways(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and len(set(value)) != len(value):
            raise ValueError("duplicate gateway")
        return value


class SideMasterChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    session_id: str = Field(min_length=1, max_length=128)
    mission_id: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=8_000)
    idempotency_key: str = Field(min_length=16, max_length=128)

    @field_validator("session_id", "mission_id", "content")
    @classmethod
    def require_nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("blank value")
        return value


class SideMasterDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_proposal_digest: str = Field(pattern=_DIGEST_PATTERN)
    idempotency_key: str = Field(min_length=16, max_length=128)


class ResearchTaskCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    mission_id: str = Field(min_length=1, max_length=128)
    objective: str = Field(min_length=1, max_length=8_000)
    constraints: list[str] = Field(default_factory=list, max_length=64)
    data_references: list[str] = Field(default_factory=list, max_length=64)
    priority: Literal["routine", "high", "safety"] = "routine"
    expires_at_ms: int = Field(gt=0)
    idempotency_key: str = Field(min_length=16, max_length=128)

    @field_validator("mission_id", "objective")
    @classmethod
    def require_nonblank_research_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("blank research value")
        return value

    @field_validator("constraints")
    @classmethod
    def validate_constraints(cls, value: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 512 for item in value):
            raise ValueError("invalid research constraint")
        return value

    @field_validator("data_references")
    @classmethod
    def validate_data_references(cls, value: list[str]) -> list[str]:
        if any(re.fullmatch(_DIGEST_PATTERN, item) is None for item in value):
            raise ValueError("invalid research data reference")
        return value


class ResearchTaskCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_task_digest: str = Field(pattern=_DIGEST_PATTERN)
    idempotency_key: str = Field(min_length=16, max_length=128)


class BootstrapExchangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    fragment_token: str = Field(min_length=32, max_length=256)


class ConfigurationUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_revision: int = Field(ge=0)
    sections: dict[str, dict[str, Any]]
    secret_updates: dict[str, str] = Field(default_factory=dict)
    clear_secrets: list[str] = Field(default_factory=list)


class ConfigurationTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    section: Literal["operator", "ports", "rqdata", "master_route", "worker_route", "xtp", "tora"]
    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=16, max_length=128)


class RevisionedOperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=16, max_length=128)


class GatewayOperationRequest(RevisionedOperationRequest):
    selected: bool | None = None


class _ResponseRedactionError(RuntimeError):
    pass


def create_demo_app(
    config: DemoWebConfig,
    backend: DemoWebBackend,
    guidance: DemoGuidanceBackend | None = None,
    *,
    security: BootstrapSessionManager | None = None,
    operations: DemoOperationsBackend | None = None,
    research: DemoResearchBackend | None = None,
) -> FastAPI:
    """Create the exact allowlisted local API surface."""

    config.validate()
    index_template = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
    if _CSRF_PLACEHOLDER not in index_template:
        raise ValueError("DEMO_CSRF_PLACEHOLDER_REQUIRED")
    app = FastAPI(
        title="Auto Trade Investor Demo",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.exception_handler(StarletteHTTPException)
    async def public_http_error(_request: Request, error: StarletteHTTPException) -> JSONResponse:
        detail = error.detail
        code = detail if isinstance(detail, str) and detail.isupper() else "HTTP_REQUEST_FAILED"
        return _error_response(error.status_code, code)

    @app.exception_handler(RequestValidationError)
    async def public_validation_error(
        _request: Request, _error: RequestValidationError
    ) -> JSONResponse:
        return _error_response(422, "REQUEST_VALIDATION_FAILED")

    def require_session(
        request: Request,
        host_session: str | None = Cookie(default=None, alias=_SESSION_COOKIE),
    ) -> None:
        authenticated = (
            security.validate_session(host_session)
            if security is not None
            else host_session is not None and compare_digest(host_session, config.session_token)
        )
        if not authenticated:
            raise HTTPException(status_code=401, detail="HOST_SESSION_REQUIRED")
        origin = request.headers.get("origin")
        if origin is not None and origin != config.allowed_origin:
            raise HTTPException(status_code=403, detail="SAME_ORIGIN_REQUIRED")

    def require_write_guard(
        request: Request,
        csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    ) -> None:
        origin = request.headers.get("origin")
        if origin != config.allowed_origin:
            raise HTTPException(status_code=403, detail="SAME_ORIGIN_REQUIRED")
        if csrf_token is None or not compare_digest(csrf_token, config.csrf_token):
            raise HTTPException(status_code=403, detail="CSRF_TOKEN_REQUIRED")

    read_dependencies = [Depends(require_session)]
    write_dependencies = [Depends(require_session), Depends(require_write_guard)]

    @app.get("/", response_class=HTMLResponse)
    def get_dashboard() -> HTMLResponse:
        csrf = "" if security is not None else config.csrf_token
        content = index_template.replace(_CSRF_PLACEHOLDER, escape(csrf, quote=True))
        response = HTMLResponse(
            content=content,
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": _CONTENT_SECURITY_POLICY,
            },
        )
        if security is None:
            response.set_cookie(
                key=_SESSION_COOKIE,
                value=config.session_token,
                httponly=True,
                samesite="strict",
                path="/",
            )
        return response

    if security is not None:

        @app.post("/api/v1/bootstrap/exchange")
        def exchange_bootstrap(
            command: BootstrapExchangeRequest,
            request: Request,
        ) -> JSONResponse:
            result = security.exchange(
                command.fragment_token,
                origin=request.headers.get("origin", ""),
            )
            if not result.accepted:
                status = 403 if result.code in {"SAME_ORIGIN_REQUIRED", "BOOTSTRAP_OPERATOR_MISMATCH"} else 409
                return _error_response(status, result.code)
            response = _invoke(
                lambda: {"csrf_token": result.csrf_token},
                accepted=False,
            )
            response.set_cookie(
                key=_SESSION_COOKIE,
                value=str(result.session_token),
                httponly=True,
                samesite="strict",
                path="/",
            )
            return response

        @app.get("/api/v1/operator", dependencies=read_dependencies)
        def get_current_operator() -> JSONResponse:
            return _invoke(security.operator_projection, accepted=False)

    if operations is not None:

        @app.get("/api/v1/system", dependencies=read_dependencies)
        def get_system() -> JSONResponse:
            return _invoke(operations.system, accepted=False)

        @app.get("/api/v1/config/draft", dependencies=read_dependencies)
        def get_configuration_draft() -> JSONResponse:
            return _invoke(operations.configuration_draft, accepted=False)

        @app.put("/api/v1/config/draft", dependencies=write_dependencies)
        def update_configuration_draft(command: ConfigurationUpdateRequest) -> JSONResponse:
            return _invoke(
                lambda: operations.update_configuration(command.model_dump(mode="json")),
                accepted=False,
            )

        @app.post(
            "/api/v1/config/draft/test",
            dependencies=write_dependencies,
            status_code=202,
        )
        def test_configuration_draft(command: ConfigurationTestRequest) -> JSONResponse:
            return _invoke(
                lambda: operations.test_configuration(command.model_dump(mode="json")),
                accepted=True,
            )

        @app.post(
            "/api/v1/config/draft/activate",
            dependencies=write_dependencies,
            status_code=202,
        )
        def activate_configuration_draft(command: RevisionedOperationRequest) -> JSONResponse:
            return _invoke(
                lambda: operations.activate_configuration(command.model_dump(mode="json")),
                accepted=True,
            )

        @app.post(
            "/api/v1/services/{service}/{action}",
            dependencies=write_dependencies,
            status_code=202,
        )
        def control_fixed_service(
            service: Literal["research", "model_xtp", "model_tora", "rqdata_fetcher"],
            action: Literal["start", "stop", "restart"],
            command: RevisionedOperationRequest,
        ) -> JSONResponse:
            return _invoke(
                lambda: operations.control_service(
                    service,
                    action,
                    command.model_dump(mode="json"),
                ),
                accepted=True,
            )

        @app.post(
            "/api/v1/gateways/{gateway}/{action}",
            dependencies=write_dependencies,
            status_code=202,
        )
        def control_gateway(
            gateway: Literal["XTP", "TORA"],
            action: Literal["start", "stop", "reconnect", "select"],
            command: GatewayOperationRequest,
        ) -> JSONResponse:
            payload = command.model_dump(mode="json", exclude_none=True)
            if action == "select" and "selected" not in payload:
                return _error_response(422, "GATEWAY_SELECTION_REQUIRED")
            if action != "select" and "selected" in payload:
                return _error_response(422, "GATEWAY_SELECTION_NOT_ALLOWED")
            return _invoke(
                lambda: operations.control_gateway(gateway, action, payload),
                accepted=True,
            )

    @app.get("/api/v1/readiness", dependencies=read_dependencies)
    def get_readiness() -> JSONResponse:
        return _invoke(backend.readiness, accepted=False)

    @app.get("/api/v1/projection", dependencies=read_dependencies)
    def get_projection() -> JSONResponse:
        return _invoke(backend.projection, accepted=False)

    @app.post("/api/v1/campaigns", dependencies=write_dependencies, status_code=202)
    def start_campaign(command: CampaignStartRequest) -> JSONResponse:
        return _invoke(
            lambda: backend.start_campaign(command.model_dump(mode="json")),
            accepted=True,
        )

    @app.post(
        "/api/v1/campaigns/{campaign_id}/pause",
        dependencies=write_dependencies,
        status_code=202,
    )
    def pause_campaign(campaign_id: UUID) -> JSONResponse:
        return _invoke(lambda: backend.pause_campaign(str(campaign_id)), accepted=True)

    @app.post("/api/v1/emergency-stop", dependencies=write_dependencies, status_code=202)
    def emergency_stop() -> JSONResponse:
        return _invoke(backend.emergency_stop, accepted=True)

    @app.get("/api/v1/evidence/{campaign_id}", dependencies=read_dependencies)
    def get_evidence(campaign_id: UUID) -> JSONResponse:
        return _invoke(lambda: backend.evidence(str(campaign_id)), accepted=False)

    @app.get("/api/v1/research/tasks", dependencies=read_dependencies)
    def list_research_tasks() -> JSONResponse:
        if research is None:
            return _error_response(503, "RESEARCH_SERVICE_UNAVAILABLE")
        return _invoke(research.list_tasks, accepted=False)

    @app.post(
        "/api/v1/research/tasks",
        dependencies=write_dependencies,
        status_code=202,
    )
    def create_research_task(command: ResearchTaskCreateRequest) -> JSONResponse:
        if research is None:
            return _error_response(503, "RESEARCH_SERVICE_UNAVAILABLE")
        return _invoke(
            lambda: research.create_task(command.model_dump(mode="json")),
            accepted=True,
        )

    @app.post(
        "/api/v1/research/tasks/{task_id}/cancel",
        dependencies=write_dependencies,
        status_code=202,
    )
    def cancel_research_task(
        task_id: UUID,
        command: ResearchTaskCancelRequest,
    ) -> JSONResponse:
        if research is None:
            return _error_response(503, "RESEARCH_SERVICE_UNAVAILABLE")
        return _invoke(
            lambda: research.cancel_task(
                str(task_id), command.model_dump(mode="json")
            ),
            accepted=True,
        )

    @app.post(
        "/api/v1/chat/messages",
        dependencies=write_dependencies,
        status_code=202,
    )
    def send_side_master_message(command: SideMasterChatRequest) -> JSONResponse:
        if guidance is None:
            return _error_response(503, "SIDE_MASTER_UNAVAILABLE")
        return _invoke(
            lambda: guidance.send_message(command.model_dump(mode="json")),
            accepted=True,
        )

    @app.post(
        "/api/v1/chat/proposals/{proposal_id}/{decision}",
        dependencies=write_dependencies,
    )
    def decide_side_master_proposal(
        proposal_id: UUID,
        decision: Literal["confirm", "reject"],
        command: SideMasterDecisionRequest,
    ) -> JSONResponse:
        if guidance is None:
            return _error_response(503, "SIDE_MASTER_UNAVAILABLE")
        return _invoke(
            lambda: guidance.decide_proposal(
                str(proposal_id),
                decision,
                command.model_dump(mode="json"),
            ),
            accepted=False,
        )

    @app.websocket("/api/v1/events")
    async def stream_events(websocket: WebSocket) -> None:
        session = websocket.cookies.get(_SESSION_COOKIE)
        if session is None or not compare_digest(session, config.session_token):
            await websocket.close(code=4401)
            return
        if websocket.headers.get("origin") != config.allowed_origin:
            await websocket.close(code=4403)
            return
        await websocket.accept()
        while True:
            try:
                projection = backend.projection()
                _assert_public_response(projection)
                await websocket.send_json(
                    {"event": "projection.snapshot", "data": projection}
                )
                if research is not None:
                    research_projection = research.projection()
                    _assert_public_response(research_projection)
                    await websocket.send_json(
                        {"event": "research.snapshot", "data": research_projection}
                    )
                await sleep(1)
            except WebSocketDisconnect:
                return
            except Exception:
                await websocket.close(code=1011)
                return

    app.mount("/assets", StaticFiles(directory=_STATIC_DIR / "assets"), name="assets")

    return app


def _invoke(operation: Any, *, accepted: bool) -> JSONResponse:
    try:
        data = operation()
        _assert_public_response(data)
    except _ResponseRedactionError:
        return _error_response(500, "RESPONSE_REDACTION_FAILED")
    except OperationRejected as error:
        return _error_response(error.status_code, error.code)
    except Exception:
        return _error_response(500, "BACKEND_OPERATION_FAILED")
    revision = data.get("revision", 0) if isinstance(data, Mapping) else 0
    return JSONResponse(
        status_code=202 if accepted else 200,
        content={
            "contract_version": 1,
            "request_id": str(uuid4()),
            "status": "accepted" if accepted else "ok",
            "revision": revision if _is_nonnegative_int(revision) else 0,
            "data": data,
        },
    )


def _error_response(status_code: int, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "contract_version": 1,
            "request_id": str(uuid4()),
            "status": "error",
            "revision": 0,
            "data": {},
            "errors": [{"code": code, "message": "请求未能完成。"}],
        },
    )


def _assert_public_response(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise _ResponseRedactionError
    _walk_public_value(value)


def _walk_public_value(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or _is_forbidden_public_key(key):
                raise _ResponseRedactionError
            _walk_public_value(item)
    elif isinstance(value, list | tuple):
        for item in value:
            _walk_public_value(item)
    elif value is not None and not isinstance(value, str | int | float | bool):
        raise _ResponseRedactionError


def _is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_forbidden_public_key(key: str) -> bool:
    normalized = key.casefold()
    if normalized in _ALLOWED_PUBLIC_TOKEN_KEYS:
        return False
    if normalized in _FORBIDDEN_PUBLIC_KEYS:
        return True
    if normalized.endswith(_SAFE_SECRET_STATUS_SUFFIXES):
        return False
    return normalized.endswith(_SECRET_MATERIAL_SUFFIXES)
