from __future__ import annotations

from copy import deepcopy

import pytest

from vnpy.agent_console.model_audit import ModelAuditViewState


def response() -> dict:
    outcomes = []
    for index, terminal in ((1, "succeeded"), (2, "succeeded"), (3, "rejected")):
        outcomes.append(
            {
                "session_id": f"session-{index}",
                "reviewer_identity": f"audit-{index}",
                "process_identity": f"process-{index}",
                "terminal_state": terminal,
                "disposal_receipt_digest": "blake3:" + f"{index}" * 64,
                "assessment_id": f"assessment-{index}",
                "decision": "approve" if index < 3 else "reject",
                "retained_reviewer_bytes": 0,
                "memory_interface_count": 0,
                "mcp_or_general_tool_count": 0,
                "error_code": None,
            }
        )
    return {
        "contract_version": 1,
        "authority": {
            "requester": "master",
            "owner": "stateless_audit_supervisor",
            "can_apply": False,
        },
        "result": {
            "review_bundle_id": "review-1",
            "subject_digest": "blake3:" + "a" * 64,
            "route_model": "gpt-5.6-sol",
            "route_digest": "blake3:" + "b" * 64,
            "reviewer_outcomes": outcomes,
            "quorum": {
                "state": "approved",
                "valid_reviewers": ["audit-1", "audit-2", "audit-3"],
                "approvals": 2,
                "ordinary_rejections": 1,
                "refusals": 0,
                "safety_vetoes": 0,
                "all_disposals_verified": True,
                "expires_at_ms": 2_000_000_000_000,
            },
        },
        "evidence_refs": ["blake3:" + "c" * 64],
        "permitted_next_actions": ["model.train.request"],
    }


def test_audit_projection_exposes_process_quorum_and_disposal_state() -> None:
    view = ModelAuditViewState.from_cli_response(response())
    assert view.quorum_state == "approved"
    assert view.route_model == "gpt-5.6-sol"
    assert view.approvals == 2
    assert view.safety_vetoes == 0
    assert view.all_disposals_verified is True
    assert len(view.reviewer_outcomes) == 3
    assert all(item.retained_reviewer_bytes == 0 for item in view.reviewer_outcomes)
    assert view.can_apply is False
    assert view.can_trade is False


@pytest.mark.parametrize("mutation", ["veto", "duplicate_process", "memory", "authority"])
def test_audit_projection_rejects_false_pass_or_authority_escalation(mutation: str) -> None:
    payload = deepcopy(response())
    if mutation == "veto":
        payload["result"]["quorum"]["safety_vetoes"] = 1
    elif mutation == "duplicate_process":
        payload["result"]["reviewer_outcomes"][2]["process_identity"] = "process-1"
    elif mutation == "memory":
        payload["result"]["reviewer_outcomes"][0]["memory_interface_count"] = 1
    else:
        payload["authority"]["can_apply"] = True
    with pytest.raises(ValueError):
        ModelAuditViewState.from_cli_response(payload)
