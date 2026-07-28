from __future__ import annotations

import os
from pathlib import Path
import sys

import pytest

from vnpy.demo_web.app import _assert_public_response
from vnpy.demo_web.configuration import (
    ConfigurationSecurityError,
    ConfigurationStore,
    CurrentUserDpapi,
    assert_secret_directory_secure,
)


class TestProtector:
    def protect(self, plaintext: bytes, *, context: bytes) -> bytes:
        return b"protected:" + context + b":" + plaintext[::-1]

    def unprotect(self, ciphertext: bytes, *, context: bytes) -> bytes:
        prefix = b"protected:" + context + b":"
        if not ciphertext.startswith(prefix):
            raise ConfigurationSecurityError("CONFIGURATION_OPERATOR_MISMATCH")
        return ciphertext[len(prefix) :][::-1]


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is a Windows boundary")
def test_current_user_dpapi_roundtrip_and_operator_context_denial() -> None:
    protector = CurrentUserDpapi()
    encrypted = protector.protect(b"rqdata-secret", context=b"operator-a")

    assert b"rqdata-secret" not in encrypted
    assert protector.unprotect(encrypted, context=b"operator-a") == b"rqdata-secret"
    with pytest.raises(ConfigurationSecurityError, match="CONFIGURATION_OPERATOR_MISMATCH"):
        protector.unprotect(encrypted, context=b"operator-b")


def test_encrypted_state_contains_no_plaintext_and_responses_expose_flags_only(
    tmp_path: Path,
) -> None:
    store = ConfigurationStore(
        tmp_path / "configuration",
        operator_identity="operator-a",
        protector=TestProtector(),
    )
    secret = "plain-rqdata-token-must-not-leak"
    draft = store.update_draft(
        expected_revision=0,
        sections={"operator": {"sid": "S-1-5-21-demo", "display_name": "演示操作员"}},
        secret_updates={"rqdata.api_key": secret},
    )

    assert draft["secret_status"] == {"rqdata.api_key": {"configured": True}}
    assert secret not in str(draft)
    for path in (tmp_path / "configuration").rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes(), path
    _assert_public_response(draft)
    with pytest.raises(Exception):
        _assert_public_response({"password": secret})


def test_secret_directory_is_owner_restricted(tmp_path: Path) -> None:
    store = ConfigurationStore(
        tmp_path / "configuration",
        operator_identity="operator-a",
        protector=TestProtector(),
    )
    assert store.secret_directory.is_dir()
    assert_secret_directory_secure(store.secret_directory)
    if os.name != "nt":
        assert store.secret_directory.stat().st_mode & 0o077 == 0
