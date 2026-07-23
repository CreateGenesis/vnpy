from time import time_ns

from vnpy.agent_bridge import AgentEvent
from vnpy.agent_console import AgentConsoleEngine, QualificationViewState
from vnpy.agent_console.controls import research_control


def exact_key() -> dict[str, object]:
    return {
        "role": "worker",
        "endpoint_fingerprint": "blake3:endpoint",
        "model": "deepseek-v4-flash",
        "prompt_digest": "blake3:prompt",
        "skill_digests": ["blake3:skill"],
        "cli_contract_digests": ["blake3:cli"],
        "contract_digest": "blake3:contract",
        "resource_profile_digest": "blake3:resource",
        "task_class": "data_provenance",
        "suite_version": "worker-qualification-v1",
    }


def test_exact_qualification_evidence_and_budget_project_within_sla() -> None:
    now = time_ns() // 1_000_000
    state = QualificationViewState()
    events = [
        (
            "qualification.state",
            {
                "revision": 1,
                "qualification_id": "qualification-1",
                "key": {**exact_key(), "base_url": "must-not-project"},
                "key_digest": "blake3:key",
                "run_ids": ["run-1", "run-2", "run-3"],
                "scores": [91, 89, 92],
                "minimum_score": 85,
                "safety_pass": True,
                "contract_pass": True,
                "live_transport_attested": True,
                "state": "qualified",
                "valid_until_ms": now + 1_000_000,
                "access_key": "must-not-project",
            },
        ),
        (
            "qualification.evidence",
            {
                "revision": 1,
                "qualification_id": "qualification-1",
                "run_ids": ["run-1", "run-2", "run-3"],
                "scores": [91, 89, 92],
                "evidence_digests": ["blake3:a", "blake3:b", "blake3:c"],
                "safety_pass": True,
                "contract_pass": True,
                "live_transport_attested": True,
                "raw_response": "must-not-project",
            },
        ),
        (
            "qualification.budget",
            {
                "revision": 1,
                "hard_limit": {"tokens": 16_000, "workers": 1},
                "reserved": {"tokens": 2_000, "workers": 1},
                "remaining": {"tokens": 14_000, "workers": 0},
                "ledger_revision": 9,
            },
        ),
    ]
    for event_type, payload in events:
        state = state.apply(event_type, payload, "qualification-correlation", now)

    assert state.qualification["key"] == exact_key()
    assert state.qualification["state"] == "qualified"
    assert state.evidence["scores"] == [91, 89, 92]
    assert state.budget["remaining"]["tokens"] == 14_000
    assert state.projection_latency_ms <= 5_000
    assert "must-not-project" not in str(state)
    assert "base_url" not in str(state)
    assert "access_key" not in str(state)


def test_grant_failure_history_and_remediation_are_visible_and_fail_closed() -> None:
    now = time_ns() // 1_000_000
    state = QualificationViewState()
    state = state.apply(
        "grant.state",
        {
            "revision": 1,
            "grant_id": "grant-1",
            "qualification_key_digest": "blake3:key",
            "mission_id": "mission-1",
            "step_id": "step-1",
            "state": "revoked",
            "resource_reservation": {"tokens": 2_000, "workers": 1},
            "budget_revision": 10,
        },
        "grant-correlation",
        now,
    )
    state = state.apply(
        "qualification.failure",
        {
            "revision": 1,
            "qualification_id": "qualification-1",
            "delegated_failures": 2,
            "failures": [
                {"kind": "delegated_failure", "evidence_digest": "blake3:a"},
                {"kind": "delegated_failure", "evidence_digest": "blake3:b"},
            ],
            "state": "requalification_required",
            "revoke_outstanding_grants": True,
        },
        "failure-correlation",
        now,
    )
    state = state.apply(
        "qualification.remediation",
        {
            "revision": 1,
            "qualification_id": "qualification-1",
            "task_class": "data_provenance",
            "action": "refine",
            "reason": "two delegated failures",
            "requires_requalification": True,
        },
        "remediation-correlation",
        now,
    )

    assert state.grant["state"] == "revoked"
    assert state.failure_history["delegated_failures"] == 2
    assert state.failure_history["revoke_outstanding_grants"] is True
    assert state.remediation["action"] == "refine"
    assert state.remediation["requires_requalification"] is True


def test_console_engine_routes_qualification_events_and_exposes_research_only_controls() -> None:
    console = AgentConsoleEngine()
    event = AgentEvent(
        "qualification.state",
        {
            "revision": 1,
            "qualification_id": "qualification-1",
            "key": exact_key(),
            "state": "qualified",
        },
    )
    state = console.apply(event)
    assert state.qualifications["state"] == "qualified"
    assert console.qualification_state.qualification["key"]["task_class"] == "data_provenance"

    for action in ("revoke_qualification", "requalify"):
        control = research_control(action, "qualification-1")
        assert control.payload["action"] == action
        assert control.payload["contract_version"] == 1
        assert "approve" not in control.payload
        assert "order" not in control.payload


def test_stale_qualification_or_grant_update_cannot_replace_last_known_state() -> None:
    now = time_ns() // 1_000_000
    state = QualificationViewState().apply(
        "grant.state",
        {"revision": 4, "grant_id": "grant-1", "state": "revoked"},
        "new",
        now,
    )
    stale = state.apply(
        "grant.state",
        {"revision": 3, "grant_id": "grant-1", "state": "issued"},
        "old",
        now,
    )
    assert stale.grant["state"] == "revoked"
    assert stale.last_error == "stale qualification event: grant.state"
