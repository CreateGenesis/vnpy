from __future__ import annotations

from pathlib import Path

import pytest

from vnpy.demo_web.configuration import (
    ConfigurationConflict,
    ConfigurationStore,
    ConfigurationTestRequired,
)
from vnpy.demo_web.configuration import ConfigurationSecurityError


class TestProtector:
    def protect(self, plaintext: bytes, *, context: bytes) -> bytes:
        return b"protected:" + context + b":" + plaintext[::-1]

    def unprotect(self, ciphertext: bytes, *, context: bytes) -> bytes:
        prefix = b"protected:" + context + b":"
        if not ciphertext.startswith(prefix):
            raise ConfigurationSecurityError("CONFIGURATION_OPERATOR_MISMATCH")
        return ciphertext[len(prefix) :][::-1]


def build_store(tmp_path: Path, now: list[int], campaign: list[bool]) -> ConfigurationStore:
    return ConfigurationStore(
        tmp_path / "configuration",
        operator_identity="operator-a",
        protector=TestProtector(),
        clock_ms=lambda: now[0],
        campaign_active=lambda: campaign[0],
    )


def test_draft_revision_retains_omitted_secret_and_supports_explicit_clear(
    tmp_path: Path,
) -> None:
    now = [1_000]
    store = build_store(tmp_path, now, [False])
    first = store.update_draft(
        expected_revision=0,
        sections={"ports": {"web": 8765, "agentd": 8781}},
        secret_updates={"rqdata.api_key": "first-secret"},
    )
    second = store.update_draft(
        expected_revision=first["revision"],
        sections={"ports": {"web": 8877, "agentd": 8781}},
    )

    assert second["secret_status"]["rqdata.api_key"]["configured"] is True
    assert store.read_secret("rqdata.api_key") == "first-secret"
    with pytest.raises(ConfigurationConflict, match="CONFIGURATION_REVISION_CONFLICT"):
        store.update_draft(expected_revision=0, sections={})

    cleared = store.update_draft(
        expected_revision=second["revision"],
        sections={},
        clear_secrets=["rqdata.api_key"],
    )
    assert cleared["secret_status"]["rqdata.api_key"]["configured"] is False


def test_changed_sections_require_current_receipts_and_campaign_freezes_mutation(
    tmp_path: Path,
) -> None:
    now = [10_000]
    campaign = [False]
    store = build_store(tmp_path, now, campaign)
    draft = store.update_draft(
        expected_revision=0,
        sections={"ports": {"web": 8765, "agentd": 8781}},
    )
    with pytest.raises(ConfigurationTestRequired, match="CONFIGURATION_TEST_REQUIRED"):
        store.activate(expected_revision=draft["revision"], health_check=lambda _: True)

    store.record_section_test("ports", expected_revision=draft["revision"], passed=True)
    now[0] += 600_001
    with pytest.raises(ConfigurationTestRequired, match="CONFIGURATION_TEST_EXPIRED"):
        store.activate(expected_revision=draft["revision"], health_check=lambda _: True)

    campaign[0] = True
    with pytest.raises(ConfigurationConflict, match="CONFIGURATION_CAMPAIGN_ACTIVE"):
        store.update_draft(expected_revision=draft["revision"], sections={})


def test_activation_is_atomic_and_failed_health_restores_previous_version(
    tmp_path: Path,
) -> None:
    now = [20_000]
    store = build_store(tmp_path, now, [False])
    first = store.update_draft(expected_revision=0, sections={"ports": {"web": 8765}})
    store.record_section_test("ports", expected_revision=first["revision"], passed=True)
    active_one = store.activate(
        expected_revision=first["revision"], health_check=lambda _: True
    )
    second = store.update_draft(
        expected_revision=first["revision"], sections={"ports": {"web": 8877}}
    )
    store.record_section_test("ports", expected_revision=second["revision"], passed=True)
    failed = store.activate(
        expected_revision=second["revision"], health_check=lambda _: False
    )

    assert active_one["state"] == "active"
    assert failed["state"] == "activation_failed"
    assert failed["restored_version"] == active_one["version"]
    assert store.read_active()["version"] == active_one["version"]
