"""Loopback-only FastAPI surface for the investor demonstration."""

from __future__ import annotations

from asyncio import sleep
from collections.abc import Mapping
from dataclasses import dataclass
from html import escape
from hmac import compare_digest
from pathlib import Path
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
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator


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
        "account",
        "account_id",
        "account_fingerprint",
        "cancel",
        "cancel_order",
        "cancel_request",
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
        "server_fingerprint",
        "state_store_path",
        "token",
    }
)


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


class CampaignStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    candidate_digest: str = Field(pattern=_DIGEST_PATTERN)
    gateways: list[Literal["XTP", "TORA"]] = Field(min_length=1, max_length=2)
    idempotency_key: str = Field(min_length=16, max_length=128)

    @field_validator("gateways")
    @classmethod
    def require_unique_gateways(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("duplicate gateway")
        return value


class _ResponseRedactionError(RuntimeError):
    pass


def create_demo_app(config: DemoWebConfig, backend: DemoWebBackend) -> FastAPI:
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

    def require_session(
        request: Request,
        host_session: str | None = Cookie(default=None, alias=_SESSION_COOKIE),
    ) -> None:
        if host_session is None or not compare_digest(host_session, config.session_token):
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
        content = index_template.replace(
            _CSRF_PLACEHOLDER,
            escape(config.csrf_token, quote=True),
        )
        response = HTMLResponse(
            content=content,
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": _CONTENT_SECURITY_POLICY,
            },
        )
        response.set_cookie(
            key=_SESSION_COOKIE,
            value=config.session_token,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return response

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
            "errors": [{"code": code, "message": "Request could not be completed."}],
        },
    )


def _assert_public_response(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise _ResponseRedactionError
    _walk_public_value(value)


def _walk_public_value(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or key.lower() in _FORBIDDEN_PUBLIC_KEYS:
                raise _ResponseRedactionError
            _walk_public_value(item)
    elif isinstance(value, list | tuple):
        for item in value:
            _walk_public_value(item)
    elif value is not None and not isinstance(value, str | int | float | bool):
        raise _ResponseRedactionError


def _is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
