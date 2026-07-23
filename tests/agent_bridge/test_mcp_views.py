from time import time_ns

from vnpy.agent_console.mcp import McpViewState


def test_import_view_is_redacted_offline_and_visible_within_five_seconds() -> None:
    now = time_ns() // 1_000_000
    payload = {
        "status": "candidate",
        "mode": "candidate",
        "source_digest": "blake3:source",
        "semantic_digest": "blake3:semantic",
        "normalizer_version": "v1",
        "adapter_manifest_digest": "blake3:adapter",
        "candidate_set_digest": "blake3:candidate",
        "entry_count": 16,
        "process_started": False,
        "network_started": False,
        "secret_lookup_performed": False,
        "activated": False,
        "audit_state": "pending",
        "command": "npx",
        "args": ["Authorization: Bearer forbidden"],
        "raw_source": "forbidden",
        "TIKHUB_API_KEY": "forbidden",
    }
    state = McpViewState().apply_import(payload, "correlation", now)
    assert state.import_status["entry_count"] == 16
    assert state.import_status["audit_state"] == "pending"
    assert state.projection_latency_ms <= 5_000
    encoded = str(state)
    assert "Authorization" not in encoded
    assert "TIKHUB_API_KEY" not in encoded
    assert "raw_source" not in encoded


def test_online_import_assertion_is_rejected_without_overwriting_last_good_state() -> None:
    now = time_ns() // 1_000_000
    state = McpViewState().apply_import(
        {"status": "validated", "entry_count": 16, "process_started": False, "network_started": False},
        "good",
        now,
    )
    blocked = state.apply_import(
        {"status": "candidate", "process_started": True, "network_started": False},
        "bad",
        now,
    )
    assert blocked.import_status == state.import_status
    assert blocked.last_error is not None


def test_call_projection_keeps_operational_evidence_and_drops_untrusted_payload() -> None:
    now = time_ns() // 1_000_000
    state = McpViewState().apply_call(
        {
            "status": "ok",
            "implicit_context": False,
            "result": {
                "method": "mcp.tools.list",
                "server_id": "tikhub-weibo",
                "correlation_id": "call-1",
                "status": "ok",
                "payload": {"tools": ["untrusted"], "Authorization": "forbidden"},
                "untrusted": True,
                "provenance": "blake3:component",
                "complete": True,
                "usage_wall_ms": 25,
                "evidence_refs": ["blake3:evidence"],
                "implicit_context_injected": False,
            },
            "secret_delivery": {
                "grant_id": "grant-1",
                "peer_verified": True,
                "consumed_once": True,
                "descriptor_closed": True,
                "mechanism": "protected_fd",
                "outcome": "delivered",
                "secret": "forbidden",
            },
            "sandbox": {
                "profile": "mcp-bwrap-v1",
                "seccomp": True,
                "read_only_runtime": True,
                "workspace_mounted": False,
            },
        },
        "call-1",
        now,
    )
    assert state.calls["status"] == "ok"
    assert state.calls["provenance"] == "blake3:component"
    assert state.calls["sandbox"]["seccomp"] is True
    assert state.secret_broker["peer_verified"] is True
    encoded = str(state)
    assert "Authorization" not in encoded
    assert "untrusted\"" not in encoded
    assert "forbidden" not in encoded


def test_failed_health_and_audit_are_redacted_for_vnpy() -> None:
    now = time_ns() // 1_000_000
    state = McpViewState().apply_call(
        {
            "status": "blocked",
            "implicit_context": False,
            "error": {
                "code": "UPSTREAM_UNAVAILABLE",
                "message": "credential-shaped upstream detail",
            },
        },
        "health-1",
        now,
    )
    state = state.apply_audit(
        {
            "catalog_digest": "blake3:catalog",
            "route_model": "gpt-5.6-sol",
            "approvals": 3,
            "ordinary_rejections": 0,
            "safety_vetoes": 0,
            "quorum": "approved",
            "private_key": "forbidden",
        },
        "audit-1",
        now,
    )
    assert state.calls["error_code"] == "UPSTREAM_UNAVAILABLE"
    assert state.audit["quorum"] == "approved"
    assert "credential-shaped" not in str(state)
    assert "private_key" not in str(state)
