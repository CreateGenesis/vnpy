from vnpy.agent_bridge import AgentEvent
from vnpy.agent_console import AgentConsoleEngine, AgentConsoleWidget
from vnpy.agent_console.controls import tikhub_control
from time import time_ns


def event(kind: str, revision: int, payload: dict, *, correlation: str = "corr-1") -> AgentEvent:
    return AgentEvent(kind, {**payload, "revision": revision}, correlation_id=correlation)


def test_every_tikhub_state_is_correlated_revisioned_and_separate_from_mcp() -> None:
    console = AgentConsoleEngine()
    fixtures = {
        "tikhub.catalog": {"state":"active","catalog_digest":"blake3:" + "1"*64,"version":"1.0.0","platforms":["weibo"],"drift":"none","audit_state":"approved","error_code":None},
        "tikhub.health": {"state":"healthy","route_mode":"required_socks5h","checked_at_ms":1,"latency_ms":2,"provider_request_id":None,"error_code":None},
        "tikhub.account": {"credential_readiness":"ready","scope_verdict":"sufficient","balance":"1","free_credit":"0","observed_usage":"0","unit_price":"0.03","currency":"USD","usage_state":"known","price_state":"known","freshness_ms":1,"unknown":[],"error_code":None},
        "tikhub.budget": {"ledger_revision":1,"status":"normal","mission_cap":"1","global_cap":"10","reserve":"5","reserved":"0","reconciled":"0","uncertain":"0","spendable_after_reserve":"5","currency":"USD","error_code":None},
        "tikhub.mission": {"mission_id":"m1","state":"partial","endpoint_id":"weibo.search.all.v1","pages_requested":2,"pages_completed":1,"items":20,"completeness":"partial","terminal_evidence_ref":"artifact:e","error_code":"PAGINATION_INCOMPLETE"},
        "tikhub.page": {"mission_id":"m1","operation_id":"o1","page_index":0,"state":"complete","outcome_certainty":"known","items":20,"duplicates":0,"pagination_state_digest":"blake3:"+"2"*64,"raw_artifact_ref":"artifact:r","normalized_artifact_ref":"artifact:n","error_code":None},
        "tikhub.result": {"mission_id":"m1","status":"partial","record_count":20,"normalized_artifact_ref":"artifact:n","evidence_ref":"artifact:e","untrusted":True,"completeness":"partial"},
        "tikhub.audit": {"catalog_digest":"blake3:"+"1"*64,"decision":"approved","complexity":"complex","reviewer_count":3,"approvals":2,"safety_vetoes":0,"expires_at_ms":999,"evidence_ref":"artifact:a"},
        "tikhub.route": {"route_policy_ref":"route:tikhub-socks5h-v1","mode":"required_socks5h","state":"ready","remote_dns":"verified","leak_check":"passed","exit_identity":"verified","attested_at_ms":1,"error_code":None},
        "tikhub.security": {"control":"mcp_tripwire","status":"passed","secret_lookup_performed":False,"process_started":False,"network_started":False,"evidence_ref":"artifact:s","error_code":None},
        "tikhub.cutover": {"status":"passed","legacy_assets":0,"legacy_registry":0,"tikhub_mcp_processes":0,"mcp_tikhub_egress":0,"fallback_attempts":0,"generic_mcp_regression":"passed","evidence_ref":"artifact:c"},
    }
    for revision, (kind, payload) in enumerate(fixtures.items(), 1):
        state = console.apply(event(kind, revision, payload))
    assert state.tikhub["correlation_id"] == "corr-1"
    assert state.tikhub["source_revisions"]["cutover"] == 11
    assert state.mcp == {}
    panel = AgentConsoleWidget(state).panels().tikhub
    assert panel["catalog"]["state"] == "active"
    assert panel["budget"]["reserve"] == "5"
    assert panel["mission"]["completeness"] == "partial"
    assert panel["errors"] == ["PAGINATION_INCOMPLETE"]


def test_stale_incompatible_and_forbidden_fields_preserve_last_known_valid_state() -> None:
    console = AgentConsoleEngine()
    valid = event("tikhub.health", 2, {"state":"degraded","route_mode":"required_socks5h","checked_at_ms":1,"latency_ms":2,"provider_request_id":None,"error_code":"UPSTREAM_UNAVAILABLE"})
    assert console.apply(valid).tikhub["health"]["state"] == "degraded"
    stale = event("tikhub.health", 1, {**valid.payload, "state":"healthy"})
    assert console.apply(stale).tikhub["health"]["state"] == "degraded"
    incompatible = AgentEvent("tikhub.health", {**valid.payload, "revision":3}, contract_version=2)
    assert console.apply(incompatible).tikhub["health"]["state"] == "degraded"
    forbidden = event("tikhub.health", 3, {**valid.payload, "authorization":"Bearer secret"})
    state = console.apply(forbidden)
    assert state.tikhub["health"]["state"] == "degraded"
    assert "Bearer" not in str(state.tikhub)


def test_tikhub_controls_are_bounded_and_never_expose_trading_authority() -> None:
    controls = [
        tikhub_control("disable_global", "catalog"),
        tikhub_control("disable_entry", "weibo.search.all.v1"),
        tikhub_control("cancel_mission", "m1"),
        tikhub_control("refresh_status", "tikhub"),
        tikhub_control("get_evidence", "artifact:e"),
    ]
    for control in controls:
        assert control.event_type == "tikhub.control"
        assert "strategy" not in str(control.payload).lower()
        assert "order" not in str(control.payload).lower()


def test_startup_republish_rehydrates_a_fresh_console_within_two_seconds() -> None:
    now_ms = time_ns() // 1_000_000
    republished = AgentEvent(
        "tikhub.health",
        {"revision": 8, "state": "degraded", "route_mode": "required_socks5h",
         "checked_at_ms": now_ms, "latency_ms": 9, "provider_request_id": None,
         "error_code": "UPSTREAM_UNAVAILABLE"},
        correlation_id="corr-restart",
        event_time_ms=now_ms,
    )
    restarted = AgentConsoleEngine()
    state = restarted.apply(republished)
    assert state.tikhub["health"]["state"] == "degraded"
    assert state.tikhub["correlation_id"] == "corr-restart"
    assert state.projection_latency_ms <= 2_000
