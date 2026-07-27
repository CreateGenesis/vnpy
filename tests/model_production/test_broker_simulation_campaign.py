from __future__ import annotations

from dataclasses import FrozenInstanceError
from hashlib import sha256
from pathlib import Path

import pytest

from vnpy.model_production.broker_simulation import BrokerSimulationAuthority, GatewayBinding


def digest(label: str) -> str:
    return f"sha256:{sha256(label.encode()).hexdigest()}"


def binding(gateway: str, database: Path) -> GatewayBinding:
    server = digest(f"{gateway}-server")
    account = digest(f"{gateway}-account")
    return GatewayBinding.create(
        gateway=gateway,
        environment="broker_simulation",
        server_fingerprint=server,
        account_fingerprint=account,
        credential_ref=f"credential:{gateway.lower()}",
        process_identity=f"process:{gateway.lower()}:1",
        rpc_endpoint="127.0.0.1:19101" if gateway == "XTP" else "127.0.0.1:19102",
        state_store_path=str(database.with_name(f"{gateway.lower()}.sqlite")),
        created_at_ms=1_000,
        allowed_server_fingerprints=frozenset({server}),
        allowed_account_fingerprints=frozenset({account}),
    )


def create_campaign(authority: BrokerSimulationAuthority, database: Path):
    return authority.create_campaign(
        campaign_id="campaign-1",
        candidate_digest=digest("candidate"),
        package_digest=digest("package"),
        configuration_digest=digest("configuration"),
        policy_digest=digest("policy"),
        symbol_set=("600000.SH", "000001.SZ"),
        calendar_sessions=("2026-07-27", "2026-07-28", "2026-07-29", "2026-07-30", "2026-07-31"),
        operator_identity_digest=digest("operator"),
        bindings=(binding("XTP", database), binding("TORA", database)),
        lifecycle_revision=8,
        now_ms=1_000,
    )


def test_dual_gateway_campaign_freezes_one_candidate_symbols_and_five_sessions(tmp_path: Path) -> None:
    database = tmp_path / "campaign.sqlite"
    authority = BrokerSimulationAuthority(database)
    campaign = create_campaign(authority, database)
    runs = authority.runs(campaign.campaign_id)
    assert campaign.state == "prepared"
    assert len(campaign.calendar_sessions) == 5
    assert campaign.symbol_set == ("600000.SH", "000001.SZ")
    assert {run.gateway for run in runs} == {"XTP", "TORA"}
    assert {run.candidate_digest for run in runs} == {campaign.candidate_digest}
    assert len({run.gateway_binding_digest for run in runs}) == 2
    with pytest.raises(FrozenInstanceError):
        campaign.state = "ready"  # type: ignore[misc]

    restarted = BrokerSimulationAuthority(database)
    assert restarted.campaign("campaign-1") == campaign
    assert restarted.runs("campaign-1") == runs


def test_campaign_creation_is_idempotent_but_identity_drift_and_duplicate_gateway_fail(
    tmp_path: Path,
) -> None:
    database = tmp_path / "campaign.sqlite"
    authority = BrokerSimulationAuthority(database)
    first = create_campaign(authority, database)
    assert create_campaign(authority, database) == first
    with pytest.raises(RuntimeError, match="CAMPAIGN_IDENTITY_DRIFT"):
        authority.create_campaign(
            campaign_id="campaign-1",
            candidate_digest=digest("different-candidate"),
            package_digest=digest("package"),
            configuration_digest=digest("configuration"),
            policy_digest=digest("policy"),
            symbol_set=("600000.SH",),
            calendar_sessions=first.calendar_sessions,
            operator_identity_digest=digest("operator"),
            bindings=(binding("XTP", database),),
            lifecycle_revision=8,
            now_ms=1_000,
        )
    xtp = binding("XTP", database)
    with pytest.raises(ValueError, match="ONE_RUN_PER_GATEWAY"):
        authority.create_campaign(
            campaign_id="campaign-2",
            candidate_digest=digest("candidate"),
            package_digest=digest("package"),
            configuration_digest=digest("configuration"),
            policy_digest=digest("policy"),
            symbol_set=("600000.SH",),
            calendar_sessions=first.calendar_sessions,
            operator_identity_digest=digest("operator"),
            bindings=(xtp, xtp),
            lifecycle_revision=8,
            now_ms=1_000,
        )


def test_pause_invalidates_runs_and_never_resumes_the_evidence_window(tmp_path: Path) -> None:
    database = tmp_path / "campaign.sqlite"
    authority = BrokerSimulationAuthority(database)
    create_campaign(authority, database)
    active = authority.start_campaign("campaign-1", now_ms=1_100)
    assert active.state == "active"
    assert {run.state for run in authority.runs("campaign-1")} == {"active"}
    paused = authority.pause_campaign("campaign-1", now_ms=1_200)
    assert paused.state == "paused"
    assert {run.state for run in authority.runs("campaign-1")} == {"invalid"}
    with pytest.raises(RuntimeError, match="CAMPAIGN_TERMINAL"):
        authority.start_campaign("campaign-1", now_ms=1_300)

