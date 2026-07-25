from __future__ import annotations

from dataclasses import replace

from vnpy.model_production.lifecycle import ProductionEligibility


def eligible() -> ProductionEligibility:
    return ProductionEligibility(10, 200, True, 0, 0, 0, "master-request-2")


def test_production_requires_ten_sessions_two_hundred_decisions_and_new_request() -> None:
    assert eligible().reason_codes("master-request-1") == ()
    cases = [
        (replace(eligible(), gray_sessions=9), "GRAY_SESSIONS_INSUFFICIENT"),
        (replace(eligible(), eligible_decisions=199), "GRAY_DECISIONS_INSUFFICIENT"),
        (replace(eligible(), reconciled=False), "RECONCILIATION_REQUIRED"),
        (replace(eligible(), hard_limit_breaches=1), "HARD_LIMIT_BREACH"),
        (replace(eligible(), unknown_outcomes=1), "UNKNOWN_OUTCOME_BLOCK"),
        (replace(eligible(), safety_vetoes=1), "SAFETY_VETO_ACTIVE"),
        (replace(eligible(), fresh_review=False), "RELEASE_REVIEW_STALE"),
        (replace(eligible(), fresh_exact_gates=False), "PRODUCTION_GATES_STALE"),
        (replace(eligible(), accepted_master_request_id="master-request-1"), "FRESH_MASTER_REQUEST_REQUIRED"),
    ]
    for state, reason in cases:
        assert reason in state.reason_codes("master-request-1")
