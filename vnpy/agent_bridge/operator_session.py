"""Verified host login-session boundary for Side Master guidance."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import json
import os
from pathlib import Path
import platform
import re
import subprocess
from time import time_ns
from typing import Any, Mapping, Protocol
from uuid import uuid4

import blake3
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


_DIGEST = re.compile(r"^blake3:[0-9a-f]{64}$")
_ANCHOR_FILE = "os-session-trust-anchor.json"
_ANCHOR_TTL_MS = 8 * 60 * 60 * 1_000


class SessionState(Enum):
    """Current guidance authentication state."""

    VERIFIED = "verified"
    REVOKED = "revoked"
    UNVERIFIABLE = "unverifiable"


class HostSessionAdapter(Protocol):
    """Minimal host state required for an interactive operator binding."""

    principal: str
    login_session: str
    peer: str
    locked: bool


@dataclass(frozen=True)
class OsSessionAssertion:
    """Short-lived opaque exact-command assertion forwarded to Rust verification."""

    auth_session_id: str
    peer_identity_digest: str
    command_digest: str
    verification_epoch: int
    revocation_revision: int
    issued_at_ms: int
    expires_at_ms: int
    assertion_digest: str
    signature: str

    def to_dict(self) -> dict[str, str | int]:
        """Return the strict wire representation without host identity fields."""

        return asdict(self)


class OsSessionIdentityProvider:
    """Fail-closed binding to the current verified interactive login session."""

    def __init__(
        self,
        adapter: HostSessionAdapter | None = None,
        *,
        state_dir: str | Path | None = None,
        private_key: Ed25519PrivateKey | None = None,
    ) -> None:
        self._adapter = adapter or _current_host_session()
        self._principal = self._adapter.principal
        self._login_session = self._adapter.login_session
        self._peer = self._adapter.peer
        self._private_key = private_key or Ed25519PrivateKey.generate()
        self._operator_id = _digest(self._principal.encode("utf-8"))
        self._peer_identity_digest = _digest(self._peer.encode("utf-8"))
        self._auth_session_id = f"auth:{uuid4()}"
        self._verification_epoch = 1
        self._revocation_revision = 0
        self._verified_at_ms = time_ns() // 1_000_000
        self._anchor_expires_at_ms = self._verified_at_ms + _ANCHOR_TTL_MS
        self._state_dir = Path(state_dir) if state_dir is not None else _default_state_dir()
        self._anchor_path = self._state_dir / _ANCHOR_FILE
        self._state = self._verify_snapshot()
        self._write_trust_anchor()

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def auth_session_id(self) -> str:
        return self._auth_session_id

    @property
    def operator_id(self) -> str:
        return self._operator_id

    @property
    def verification_epoch(self) -> int:
        return self._verification_epoch

    @property
    def revocation_revision(self) -> int:
        return self._revocation_revision

    @property
    def trust_anchor_path(self) -> Path:
        return self._anchor_path

    def refresh(self, now_ms: int | None = None) -> SessionState:
        """Revoke on lock, logout, user switch, or login-session replacement."""

        if self._state is SessionState.REVOKED:
            return self._state
        state = self._verify_snapshot()
        if state is not SessionState.VERIFIED:
            self.revoke(state.value)
        else:
            self._verified_at_ms = now_ms if now_ms is not None else time_ns() // 1_000_000
        return self._state

    def revoke(self, _reason: str) -> None:
        """Advance the epoch and revision so old assertions cannot be replayed."""

        self._state = SessionState.REVOKED
        self._verification_epoch += 1
        self._revocation_revision += 1
        self._write_trust_anchor()

    def notify_lock(self) -> None:
        self.revoke("session_locked")

    def notify_logout(self) -> None:
        self.revoke("session_logged_out")

    def notify_user_switch(self) -> None:
        self.revoke("user_changed")

    def issue(
        self,
        command_digest: str,
        *,
        now_ms: int | None = None,
        ttl_ms: int = 30_000,
    ) -> OsSessionAssertion:
        """Issue one command-bound assertion; no broker/provider secret is accepted."""

        now = now_ms if now_ms is not None else time_ns() // 1_000_000
        if self.refresh(now) is not SessionState.VERIFIED:
            raise PermissionError("current interactive operating-system session is not verified")
        if _DIGEST.fullmatch(command_digest) is None or ttl_ms <= 0 or ttl_ms > 60_000:
            raise ValueError("invalid command digest or assertion lifetime")
        fields: dict[str, str | int] = {
            "auth_session_id": self._auth_session_id,
            "peer_identity_digest": self._peer_identity_digest,
            "command_digest": command_digest,
            "verification_epoch": self._verification_epoch,
            "revocation_revision": self._revocation_revision,
            "issued_at_ms": now,
            "expires_at_ms": now + ttl_ms,
        }
        encoded = _assertion_bytes(fields)
        return OsSessionAssertion(
            **fields,
            assertion_digest=_digest(encoded),
            signature=self._private_key.sign(encoded).hex(),
        )

    def build_request(
        self,
        action: str,
        *,
        payload: Mapping[str, Any],
        mission_id: str | None = None,
        session_id: str | None = None,
        expected_revision: int = 0,
        now_ms: int | None = None,
        deadline_ms: int | None = None,
        operation_id: str | None = None,
        correlation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Build the strict dynamic command envelope consumed by agentctl/agentd."""

        if action not in {
            "templates", "open", "turn", "prepare", "send", "cancel", "close",
            "inspect", "refresh", "health", "recover", "disable", "running",
            "boundary", "context", "reconcile", "effective",
        }:
            raise ValueError("unknown guidance action")
        if isinstance(expected_revision, bool) or expected_revision < 0:
            raise ValueError("expected revision must be a non-negative integer")
        now = now_ms if now_ms is not None else time_ns() // 1_000_000
        deadline = deadline_ms if deadline_ms is not None else now + 30_000
        if deadline < now:
            raise ValueError("guidance request deadline has already expired")
        operation = operation_id or f"operation:{uuid4()}"
        correlation = correlation_id or f"correlation:{uuid4()}"
        idempotency = idempotency_key or f"idempotency:{uuid4()}"
        if any(not value for value in (operation, correlation, idempotency)):
            raise ValueError("guidance request identities must be non-empty")
        dynamic_payload = dict(payload)
        request: dict[str, Any] = {
            "entity_type": "guidance_cli_request",
            "contract_version": 1,
            "operation_id": operation,
            "correlation_id": correlation,
            "idempotency_key": idempotency,
            "action": action,
            "operator_id": self._operator_id,
            "auth_session_id": self._auth_session_id,
            "mission_id": mission_id,
            "session_id": session_id,
            "expected_revision": expected_revision,
            "deadline_ms": deadline,
            "payload": dynamic_payload,
            "payload_digest": _digest(_canonical_json(dynamic_payload)),
        }
        command_digest = _digest(_canonical_json(request))
        request["os_session_assertion"] = self.issue(
            command_digest,
            now_ms=now,
            ttl_ms=min(30_000, max(1, deadline - now)),
        ).to_dict()
        return request

    def trust_anchor(self) -> dict[str, str | int]:
        """Return the public, opaque verifier record shared with the supervisor."""

        public_key = self._private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return {
            "contract_version": 1,
            "auth_session_id": self._auth_session_id,
            "operator_id": self._operator_id,
            "peer_identity_digest": self._peer_identity_digest,
            "verification_epoch": self._verification_epoch,
            "revocation_revision": self._revocation_revision,
            "state": "active" if self._state is SessionState.VERIFIED else "revoked",
            "verified_at_ms": self._verified_at_ms,
            "expires_at_ms": self._anchor_expires_at_ms,
            "verifying_key": public_key.hex(),
        }

    def _verify_snapshot(self) -> SessionState:
        if not self._adapter.principal or not self._adapter.login_session or not self._adapter.peer:
            return SessionState.UNVERIFIABLE
        if self._adapter.locked:
            return SessionState.REVOKED
        if (
            self._adapter.principal != self._principal
            or self._adapter.login_session != self._login_session
            or self._adapter.peer != self._peer
        ):
            return SessionState.REVOKED
        return SessionState.VERIFIED

    def _write_trust_anchor(self) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        temporary = self._anchor_path.with_name(f".{_ANCHOR_FILE}.{uuid4().hex}.tmp")
        payload = json.dumps(
            self.trust_anchor(), ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True
        )
        try:
            temporary.write_text(payload, encoding="utf-8", newline="\n")
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
            os.replace(temporary, self._anchor_path)
        finally:
            if temporary.exists():
                temporary.unlink()


@dataclass
class _HostSnapshot:
    principal: str
    login_session: str
    peer: str
    locked: bool = False


def _default_state_dir() -> Path:
    root = Path(os.environ.get("AGENT_WORKSPACE_ROOT", Path.cwd()))
    return root / ".agent-state"


def _current_host_session() -> _HostSnapshot:
    system = platform.system()
    if system == "Windows":
        principal = _windows_sid()
        login_session = _windows_login_session()
    else:
        principal = f"uid:{os.getuid()}" if hasattr(os, "getuid") else ""
        login_session = os.environ.get("XDG_SESSION_ID") or os.environ.get("LOGIN_SESSION", "")
    return _HostSnapshot(
        principal=principal,
        login_session=login_session,
        peer=f"pid:{os.getpid()}",
    )


def _windows_sid() -> str:
    try:
        result = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        fields = next(iter(__import__("csv").reader([result.stdout.strip()])), [])
        return fields[1].strip() if len(fields) >= 2 else ""
    except (OSError, subprocess.SubprocessError, IndexError):
        return ""


def _windows_login_session() -> str:
    name = os.environ.get("SESSIONNAME", "").strip()
    session_id = os.environ.get("SESSIONID", "").strip()
    if name:
        return f"{name}:{session_id}" if session_id else name
    return ""


def _assertion_bytes(fields: dict[str, str | int]) -> bytes:
    ordered = (
        fields["auth_session_id"],
        fields["peer_identity_digest"],
        fields["command_digest"],
        fields["verification_epoch"],
        fields["revocation_revision"],
        fields["issued_at_ms"],
        fields["expires_at_ms"],
    )
    return "\0".join(str(value) for value in ordered).encode("utf-8")


def _digest(value: bytes) -> str:
    return f"blake3:{blake3.blake3(value).hexdigest()}"


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
