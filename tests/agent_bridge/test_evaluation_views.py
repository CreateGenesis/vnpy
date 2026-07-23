from time import time_ns

from vnpy.agent_bridge import AgentEvent
from vnpy.agent_console import AgentConsoleEngine, AgentConsoleWidget
from vnpy.agent_console.evaluation import EvaluationViewState


def test_evaluation_projection_covers_progress_budget_score_and_failure_within_sla() -> None:
    now = time_ns() // 1_000_000
    state = EvaluationViewState()
    state = state.apply(
        "evaluation.run",
        {
            "revision": 1,
            "run_id": "run-1",
            "suite_id": "harness-v1",
            "suite_version": "1.0.0",
            "status": "running",
            "completed_cases": 12,
            "total_cases": 50,
            "model": "gpt-5.6-sol",
            "endpoint_fingerprint": "blake3:endpoint",
            "route_digest": "blake3:route",
            "prompt_digest": "blake3:prompt",
            "budget_revision": 7,
            "live_transport_attested": True,
            "raw_prompt": "must-not-project",
            "authorization": "Bearer must-not-project",
        },
        "correlation-1",
        now,
    )
    state = state.apply(
        "evaluation.budget",
        {
            "revision": 1,
            "hard_limit": {"tokens": 100_000},
            "spent": {"tokens": 10_000},
            "reserved": {"tokens": 2_000},
            "remaining": {"tokens": 88_000},
            "ledger_revision": 4,
        },
        "correlation-1",
        now,
    )
    state = state.apply(
        "evaluation.scorecard",
        {
            "revision": 1,
            "score": 94,
            "task_completion": 66,
            "cli_skill_contract": 19,
            "evidence_traceability": 9,
            "safety_pass": True,
            "contract_pass": True,
            "score_producer": "harness",
            "scorer_version": "objective-scorer-v1",
            "evidence_bundle_digest": "blake3:evidence",
            "rubric_digest": "blake3:rubric",
            "model_supplied_score": 100,
        },
        "correlation-1",
        now,
    )
    state = state.apply(
        "evaluation.failure",
        {
            "revision": 1,
            "code": "RATE_LIMITED",
            "disposition": "pending",
            "http_status": 429,
            "diagnostic": "bounded redacted diagnostic",
            "api_key": "must-not-project",
        },
        "correlation-1",
        now,
    )

    assert state.run["completed_cases"] == 12
    assert state.run["live_transport_attested"] is True
    assert state.budget["remaining"]["tokens"] == 88_000
    assert state.scorecard["score_producer"] == "harness"
    assert "model_supplied_score" not in state.scorecard
    assert state.failure["disposition"] == "pending"
    assert state.projection_latency_ms <= 5_000
    encoded = str(state)
    assert "must-not-project" not in encoded
    assert "authorization" not in encoded.lower()
    assert "api_key" not in encoded.lower()


def test_audit_comparison_shadow_and_baseline_views_expose_verified_release_state() -> None:
    now = time_ns() // 1_000_000
    state = EvaluationViewState()
    fixtures = [
        (
            "evaluation.audit",
            {
                "revision": 1,
                "subject_digest": "blake3:candidate",
                "reviewer_identities": ["audit-a", "audit-b", "audit-c"],
                "reviewer_key_fingerprints": ["key-a", "key-b", "key-c"],
                "signature_verified": True,
                "approvals": 2,
                "veto": False,
                "quorum": "approved",
                "private_context": "must-not-project",
            },
        ),
        (
            "evaluation.comparison",
            {
                "revision": 1,
                "candidate_profile_digest": "blake3:candidate",
                "baseline_profile_digest": "blake3:baseline",
                "no_regression": True,
                "regressions": [],
                "resource_deltas": {"tokens": -100},
            },
        ),
        (
            "evaluation.shadow",
            {
                "revision": 1,
                "completed": 10,
                "required": 10,
                "no_regression": True,
                "hard_failures": 0,
            },
        ),
        (
            "evaluation.baseline",
            {
                "revision": 1,
                "active_profile_digest": "blake3:active",
                "previous_profile_digest": "blake3:previous",
                "candidate_state": "active",
                "rollback_available": True,
                "registry_revision": 8,
                "approve": True,
            },
        ),
    ]
    for event_type, payload in fixtures:
        state = state.apply(event_type, payload, "release-correlation", now)

    assert state.audit["approvals"] == 2
    assert state.audit["veto"] is False
    assert state.comparison["no_regression"] is True
    assert state.comparison["resource_deltas"]["tokens"] == -100
    assert state.shadow["completed"] == 10
    assert state.baseline["rollback_available"] is True
    assert "approve" not in state.baseline
    assert "must-not-project" not in str(state)


def test_stale_evaluation_event_cannot_replace_last_verified_projection() -> None:
    now = time_ns() // 1_000_000
    state = EvaluationViewState().apply(
        "evaluation.run",
        {"revision": 3, "run_id": "run-1", "status": "blocked"},
        "new",
        now,
    )
    stale = state.apply(
        "evaluation.run",
        {"revision": 2, "run_id": "run-1", "status": "completed"},
        "old",
        now,
    )
    assert stale.run["status"] == "blocked"
    assert stale.last_error == "stale evaluation event: evaluation.run"


def test_console_widget_exposes_combined_evaluation_panel_without_release_approval() -> None:
    console = AgentConsoleEngine()
    state = console.apply(
        AgentEvent("evaluation.state", {"revision": 1, "status": "pending", "run_id": "r"})
    )
    state = console.apply(
        AgentEvent("score.input", {"revision": 1, "producer": "harness", "score": 91})
    )
    state = console.apply(
        AgentEvent(
            "harness.state",
            {"revision": 1, "active_profile_digest": "blake3:active"},
        )
    )
    panels = AgentConsoleWidget(state).panels()
    assert panels.evaluation["run"]["status"] == "pending"
    assert panels.evaluation["score_inputs"]["producer"] == "harness"
    assert panels.evaluation["baseline"]["active_profile_digest"] == "blake3:active"
    assert not hasattr(panels, "approve_release")
