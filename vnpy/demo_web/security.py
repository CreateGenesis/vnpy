"""One-time browser bootstrap bound to the current Windows operator."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from hmac import compare_digest
from secrets import token_urlsafe
from typing import Callable


@dataclass(frozen=True)
class BootstrapExchangeResult:
    accepted: bool
    code: str
    session_token: str | None = None
    csrf_token: str | None = None


class BootstrapSessionManager:
    def __init__(
        self,
        *,
        allowed_origin: str,
        expected_operator_sid: str,
        current_operator_sid: Callable[[], str],
        session_token: str,
        csrf_token: str,
    ) -> None:
        if (
            not expected_operator_sid
            or len(session_token) < 32
            or len(csrf_token) < 32
            or session_token == csrf_token
        ):
            raise ValueError("BOOTSTRAP_CONFIGURATION_INVALID")
        self.allowed_origin = allowed_origin
        self.expected_operator_sid = expected_operator_sid
        self._current_operator_sid = current_operator_sid
        self.session_token = session_token
        self.csrf_token = csrf_token
        self._fragment_digest: bytes | None = None
        self._consumed = False

    def issue_fragment_token(self) -> str:
        token = token_urlsafe(32)
        self._fragment_digest = sha256(token.encode()).digest()
        self._consumed = False
        return token

    def exchange(self, token: str, *, origin: str) -> BootstrapExchangeResult:
        if origin != self.allowed_origin:
            return BootstrapExchangeResult(False, "SAME_ORIGIN_REQUIRED")
        if self._current_operator_sid() != self.expected_operator_sid:
            return BootstrapExchangeResult(False, "BOOTSTRAP_OPERATOR_MISMATCH")
        if self._consumed:
            return BootstrapExchangeResult(False, "BOOTSTRAP_TOKEN_CONSUMED")
        if self._fragment_digest is None or not compare_digest(
            sha256(token.encode()).digest(), self._fragment_digest
        ):
            return BootstrapExchangeResult(False, "BOOTSTRAP_TOKEN_INVALID")
        self._consumed = True
        self._fragment_digest = None
        return BootstrapExchangeResult(
            True,
            "OK",
            session_token=self.session_token,
            csrf_token=self.csrf_token,
        )

    def validate_session(self, token: str | None) -> bool:
        return (
            self._consumed
            and token is not None
            and compare_digest(token, self.session_token)
            and self._current_operator_sid() == self.expected_operator_sid
        )

    def operator_projection(self) -> dict[str, str]:
        return {
            "operator_identity_digest": "sha256:"
            + sha256(self.expected_operator_sid.encode()).hexdigest()
        }
