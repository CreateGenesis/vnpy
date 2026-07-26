from dataclasses import dataclass

import pytest

from vnpy.agent_bridge import operator_session
from vnpy.agent_bridge.operator_session import OsSessionIdentityProvider, SessionState


@dataclass
class FakeHostSession:
    principal: str = "S-1-5-21-100"
    login_session: str = "desktop-4"
    peer: str = "peer-7"
    locked: bool = False


def test_provider_revokes_on_lock_and_user_switch() -> None:
    host = FakeHostSession()
    provider = OsSessionIdentityProvider(host)
    assertion = provider.issue("blake3:" + "a" * 64, now_ms=100, ttl_ms=100)
    assert assertion.auth_session_id == provider.auth_session_id
    assert assertion.peer_identity_digest.startswith("blake3:")
    assert not hasattr(assertion, "principal")
    assert not hasattr(assertion, "login_session")
    assert not hasattr(assertion, "peer_identity")
    assert provider.trust_anchor()["verifying_key"]
    host.locked = True
    assert provider.refresh(150) is SessionState.REVOKED
    with pytest.raises(PermissionError):
        provider.issue("blake3:" + "a" * 64, now_ms=151, ttl_ms=100)


@pytest.mark.parametrize("change", ["logout", "user_switch", "login_replacement", "peer_replacement"])
def test_provider_revokes_every_host_session_replacement(change: str, tmp_path) -> None:
    host = FakeHostSession()
    provider = OsSessionIdentityProvider(host, state_dir=tmp_path)
    if change == "logout":
        provider.notify_logout()
    elif change == "user_switch":
        provider.notify_user_switch()
    elif change == "login_replacement":
        host.login_session = "desktop-5"
        provider.refresh(150)
    else:
        host.peer = "peer-8"
        provider.refresh(150)
    assert provider.state is SessionState.REVOKED
    with pytest.raises(PermissionError):
        provider.issue("blake3:" + "a" * 64, now_ms=151, ttl_ms=100)


def test_process_identity_without_desktop_session_fails_closed() -> None:
    host = FakeHostSession(login_session="")
    provider = OsSessionIdentityProvider(host)
    with pytest.raises(PermissionError):
        provider.issue("blake3:" + "a" * 64, now_ms=100, ttl_ms=100)


def test_trust_anchor_is_written_atomically_without_raw_host_identity(tmp_path) -> None:
    host = FakeHostSession()
    provider = OsSessionIdentityProvider(host, state_dir=tmp_path)
    anchor = (tmp_path / "os-session-trust-anchor.json").read_text(encoding="utf-8")
    assert host.principal not in anchor
    assert host.login_session not in anchor
    assert host.peer not in anchor
    assert provider.auth_session_id in anchor


def test_windows_snapshot_uses_sid_desktop_session_and_process_peer(monkeypatch) -> None:
    monkeypatch.setattr(operator_session.platform, "system", lambda: "Windows")
    monkeypatch.setattr(operator_session, "_windows_sid", lambda: "S-1-5-21-100")
    monkeypatch.setattr(operator_session, "_windows_login_session", lambda: "Console:4")
    snapshot = operator_session._current_host_session()
    assert snapshot.principal == "S-1-5-21-100"
    assert snapshot.login_session == "Console:4"
    assert snapshot.peer == f"pid:{operator_session.os.getpid()}"


def test_linux_snapshot_uses_uid_login_session_and_process_peer(monkeypatch) -> None:
    monkeypatch.setattr(operator_session.platform, "system", lambda: "Linux")
    monkeypatch.setattr(operator_session.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setenv("XDG_SESSION_ID", "session-7")
    snapshot = operator_session._current_host_session()
    assert snapshot.principal == "uid:1000"
    assert snapshot.login_session == "session-7"
    assert snapshot.peer == f"pid:{operator_session.os.getpid()}"


def test_unlock_requires_a_new_verified_auth_session(tmp_path) -> None:
    host = FakeHostSession()
    provider = OsSessionIdentityProvider(host, state_dir=tmp_path / "old")
    old_auth_session_id = provider.auth_session_id
    host.locked = True
    assert provider.refresh(150) is SessionState.REVOKED
    host.locked = False
    assert provider.refresh(151) is SessionState.REVOKED

    replacement = OsSessionIdentityProvider(host, state_dir=tmp_path / "new")
    assert replacement.state is SessionState.VERIFIED
    assert replacement.auth_session_id != old_auth_session_id
    assert replacement.issue("blake3:" + "a" * 64, now_ms=152, ttl_ms=100)


@pytest.mark.parametrize("field", ["principal", "login_session", "peer"])
def test_every_missing_os_session_component_is_unverifiable(field: str, tmp_path) -> None:
    host = FakeHostSession()
    setattr(host, field, "")
    provider = OsSessionIdentityProvider(host, state_dir=tmp_path)
    assert provider.state is SessionState.UNVERIFIABLE
    with pytest.raises(PermissionError):
        provider.issue("blake3:" + "a" * 64, now_ms=100, ttl_ms=100)


def test_windows_and_linux_identity_discovery_fail_closed_when_desktop_is_unavailable(
    monkeypatch,
) -> None:
    monkeypatch.setattr(operator_session.platform, "system", lambda: "Windows")
    monkeypatch.setattr(operator_session, "_windows_sid", lambda: "")
    monkeypatch.setattr(operator_session, "_windows_login_session", lambda: "")
    windows = operator_session._current_host_session()
    assert not windows.principal
    assert not windows.login_session

    monkeypatch.setattr(operator_session.platform, "system", lambda: "Linux")
    monkeypatch.setattr(operator_session.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.delenv("XDG_SESSION_ID", raising=False)
    monkeypatch.delenv("LOGIN_SESSION", raising=False)
    linux = operator_session._current_host_session()
    assert linux.principal == "uid:1000"
    assert not linux.login_session


def test_restart_rotates_auth_session_and_old_provider_stays_revoked(tmp_path) -> None:
    host = FakeHostSession()
    old = OsSessionIdentityProvider(host, state_dir=tmp_path / "old")
    old.notify_logout()
    restarted = OsSessionIdentityProvider(host, state_dir=tmp_path / "restarted")
    assert old.state is SessionState.REVOKED
    assert restarted.state is SessionState.VERIFIED
    assert restarted.auth_session_id != old.auth_session_id
    assert restarted.trust_anchor()["verification_epoch"] == 1


def test_exact_command_replay_keeps_identity_and_command_digest_stable(tmp_path) -> None:
    provider = OsSessionIdentityProvider(FakeHostSession(), state_dir=tmp_path)
    arguments = dict(
        action="send",
        payload={"free_form": {"focus": ["600000.SH", "000001.SZ"]}},
        mission_id="mission-1",
        session_id="side-1",
        expected_revision=4,
        now_ms=1_000,
        deadline_ms=20_000,
        operation_id="operation-replay-1",
        correlation_id="correlation-replay-1",
        idempotency_key="idempotency-replay-1",
    )
    first = provider.build_request(**arguments)
    replay = provider.build_request(**arguments)
    assert replay["operation_id"] == first["operation_id"]
    assert replay["idempotency_key"] == first["idempotency_key"]
    assert (
        replay["os_session_assertion"]["command_digest"]
        == first["os_session_assertion"]["command_digest"]
    )


def test_provider_builds_a_complete_dynamic_command_bound_request(tmp_path) -> None:
    provider = OsSessionIdentityProvider(FakeHostSession(), state_dir=tmp_path)
    request = provider.build_request(
        "open",
        payload={"free_form": {"symbols": ["600000.SH"], "focus": "分红"}},
        mission_id="mission-1",
        session_id="side-1",
        expected_revision=0,
        now_ms=1_000,
        deadline_ms=20_000,
        operation_id="operation-1",
        correlation_id="correlation-1",
        idempotency_key="idempotency-1",
    )
    assert request["auth_session_id"] == provider.auth_session_id
    assert request["operator_id"] == provider.operator_id
    assert request["os_session_assertion"]["command_digest"].startswith("blake3:")
    assert request["payload_digest"].startswith("blake3:")
    assert "principal" not in str(request)
    assert request["payload"]["free_form"]["focus"] == "分红"
