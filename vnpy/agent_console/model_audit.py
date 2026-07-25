"""Read-only stateless model-Audit projection for the vn.py Agent Console."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any


_DIGEST = re.compile(r"^(?:blake3|sha256):[0-9a-f]{64}$")
_QUORUM_STATES = {"approved", "rejected", "blocked", "expired"}
_TERMINAL_STATES = {
    "succeeded", "rejected", "refused", "vetoed", "timed_out", "crashed", "cancelled"
}
_DISCUSSION_STATES = {
    "open", "clarified_upheld", "fresh_review_required", "revision_required",
    "unresolved_blocked", "retired"
}
_ACTIONS = {
    "model.train.request",
    "model.audit.discuss",
    "model.audit.discussion-reply",
    "model.audit.discussion-status",
    "model.candidate.revise",
    "model.candidate.retire",
}


def _digest(value: Any) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError("invalid model Audit digest")
    return value


@dataclass(frozen=True)
class AuditReviewerOutcomeView:
    """One disposable reviewer process and its terminal disposal evidence."""

    session_id: str
    reviewer_identity: str
    process_identity: str
    terminal_state: str
    assessment_id: str | None
    decision: str | None
    disposal_receipt_digest: str
    retained_reviewer_bytes: int
    memory_interface_count: int
    mcp_or_general_tool_count: int
    error_code: str | None = None


@dataclass(frozen=True)
class ModelAuditViewState:
    """Exact Audit state with no review, training, lifecycle, risk, or order authority."""

    review_bundle_id: str
    subject_digest: str
    route_model: str
    route_digest: str
    quorum_state: str
    valid_reviewers: tuple[str, ...]
    approvals: int
    ordinary_rejections: int
    refusals: int
    safety_vetoes: int
    all_disposals_verified: bool
    expires_at_ms: int
    reviewer_outcomes: tuple[AuditReviewerOutcomeView, ...] = field(default_factory=tuple)
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    permitted_next_actions: tuple[str, ...] = field(default_factory=tuple)
    ledger_entry_count: int | None = None
    ledger_head_digest: str | None = None
    discussion_id: str | None = None
    discussion_resolution: str | None = None
    original_decision_digest: str | None = None
    can_apply: bool = False
    can_trade: bool = False
    can_grant_release: bool = False

    @classmethod
    def from_cli_response(cls, payload: dict[str, Any]) -> "ModelAuditViewState":
        """Validate and redact one stable `agentctl model audit` response."""
        if payload.get("contract_version") != 1:
            raise ValueError("incompatible model Audit response")
        authority = payload.get("authority")
        if not isinstance(authority, dict) or authority.get("can_apply") is not False:
            raise ValueError("model Audit projection attempted authority escalation")
        actions = payload.get("permitted_next_actions", [])
        if (
            not isinstance(actions, list)
            or any(not isinstance(item, str) or item not in _ACTIONS for item in actions)
        ):
            raise ValueError("invalid model Audit next action")
        evidence_refs = payload.get("evidence_refs", [])
        if not isinstance(evidence_refs, list):
            raise ValueError("invalid model Audit evidence")
        evidence = tuple(_digest(item) for item in evidence_refs)
        result = payload.get("result")
        if not isinstance(result, dict):
            raise ValueError("model Audit result is missing")

        if "discussion_id" in result:
            return cls._from_discussion(result, evidence, tuple(actions))
        bundle, quorum, outcomes, ledger = cls._normalize_review(result)
        review_bundle_id = bundle.get("review_bundle_id")
        if not isinstance(review_bundle_id, str) or not review_bundle_id:
            raise ValueError("invalid model Audit review identity")
        subject_digest = _digest(bundle.get("subject_digest"))
        route_model = bundle.get("route_model")
        route_digest = _digest(bundle.get("route_digest"))
        if route_model != "gpt-5.6-sol":
            raise ValueError("model Audit route is not the frozen Master model")
        quorum_state = quorum.get("state")
        if quorum_state not in _QUORUM_STATES:
            raise ValueError("invalid model Audit quorum state")
        valid_reviewers = quorum.get("valid_reviewers", [])
        if (
            not isinstance(valid_reviewers, list)
            or any(not isinstance(item, str) or not item for item in valid_reviewers)
            or len(valid_reviewers) != len(set(valid_reviewers))
        ):
            raise ValueError("invalid model Audit reviewer quorum")
        approvals = int(quorum.get("approvals", 0))
        ordinary_rejections = int(quorum.get("ordinary_rejections", 0))
        refusals = int(quorum.get("refusals", 0))
        safety_vetoes = int(quorum.get("safety_vetoes", 0))
        all_disposals_verified = quorum.get("all_disposals_verified") is True
        if quorum_state == "approved" and (
            len(valid_reviewers) < 3
            or approvals < 2
            or safety_vetoes != 0
            or not all_disposals_verified
        ):
            raise ValueError("invalid passing model Audit quorum")
        reviewer_outcomes = tuple(cls._outcome(item) for item in outcomes)
        sessions = [item.session_id for item in reviewer_outcomes]
        processes = [item.process_identity for item in reviewer_outcomes]
        if len(sessions) != len(set(sessions)) or len(processes) != len(set(processes)):
            raise ValueError("duplicate model Audit process or session")
        expires_at_ms = int(quorum.get("expires_at_ms", bundle.get("expires_at_ms", 0)))
        if expires_at_ms <= 0:
            raise ValueError("invalid model Audit expiry")
        ledger_entry_count = None
        ledger_head_digest = None
        if ledger is not None:
            if not isinstance(ledger, dict) or not isinstance(ledger.get("entry_count"), int):
                raise ValueError("invalid model Audit ledger")
            ledger_entry_count = ledger["entry_count"]
            ledger_head_digest = _digest(ledger.get("head_digest"))
        return cls(
            review_bundle_id=review_bundle_id,
            subject_digest=subject_digest,
            route_model=route_model,
            route_digest=route_digest,
            quorum_state=quorum_state,
            valid_reviewers=tuple(valid_reviewers),
            approvals=approvals,
            ordinary_rejections=ordinary_rejections,
            refusals=refusals,
            safety_vetoes=safety_vetoes,
            all_disposals_verified=all_disposals_verified,
            expires_at_ms=expires_at_ms,
            reviewer_outcomes=reviewer_outcomes,
            evidence_refs=evidence,
            permitted_next_actions=tuple(actions),
            ledger_entry_count=ledger_entry_count,
            ledger_head_digest=ledger_head_digest,
        )

    @staticmethod
    def _normalize_review(
        result: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], list[Any], dict[str, Any] | None]:
        if isinstance(result.get("bundle"), dict):
            source_bundle = result["bundle"]
            subject = source_bundle.get("subject", {})
            bundle = {
                "review_bundle_id": source_bundle.get("review_bundle_id"),
                "subject_digest": subject.get("subject_digest"),
                "route_model": source_bundle.get("route_model"),
                "route_digest": source_bundle.get("frozen_model_route_digest"),
                "expires_at_ms": source_bundle.get("expires_at_ms"),
            }
            outcomes = result.get("outcomes", [])
            quorum = result.get("quorum")
        else:
            bundle = {
                "review_bundle_id": result.get("review_bundle_id"),
                "subject_digest": result.get("subject_digest"),
                "route_model": result.get("route_model"),
                "route_digest": result.get("route_digest"),
            }
            outcomes = result.get("reviewer_outcomes", [])
            quorum = result.get("quorum")
        if not isinstance(quorum, dict) or not isinstance(outcomes, list):
            raise ValueError("model Audit quorum or outcomes are missing")
        ledger = result.get("external_ledger")
        return bundle, quorum, outcomes, ledger

    @staticmethod
    def _outcome(value: Any) -> AuditReviewerOutcomeView:
        if not isinstance(value, dict):
            raise ValueError("invalid model Audit reviewer outcome")
        if isinstance(value.get("session"), dict):
            session = value["session"]
            assessment = value.get("assessment")
            receipt = session.get("disposal_receipt", {})
            assessment_id = assessment.get("assessment_id") if isinstance(assessment, dict) else None
            decision = assessment.get("decision") if isinstance(assessment, dict) else None
            receipt_digest = _digest(value.get("disposal_receipt_digest"))
            process_identity = session.get("process_identity")
            retained = receipt.get("retained_reviewer_bytes", 0)
            memory_count = session.get("memory_interface_count", 0)
            tool_count = session.get("mcp_or_general_tool_count", 0)
        else:
            session = value
            assessment_id = value.get("assessment_id")
            decision = value.get("decision")
            receipt_digest = _digest(value.get("disposal_receipt_digest"))
            process_identity = value.get("process_identity", value.get("session_id"))
            retained = int(value.get("retained_reviewer_bytes", 0))
            memory_count = int(value.get("memory_interface_count", 0))
            tool_count = int(value.get("mcp_or_general_tool_count", 0))
        session_id = session.get("session_id")
        reviewer_identity = session.get("reviewer_identity")
        terminal_state = session.get("terminal_state")
        if (
            not isinstance(session_id, str)
            or not isinstance(reviewer_identity, str)
            or not isinstance(process_identity, str)
            or terminal_state not in _TERMINAL_STATES
            or retained != 0
            or memory_count != 0
            or tool_count != 0
        ):
            raise ValueError("invalid or stateful model Audit reviewer outcome")
        return AuditReviewerOutcomeView(
            session_id=session_id,
            reviewer_identity=reviewer_identity,
            process_identity=process_identity,
            terminal_state=terminal_state,
            assessment_id=assessment_id if isinstance(assessment_id, str) else None,
            decision=decision if isinstance(decision, str) else None,
            disposal_receipt_digest=receipt_digest,
            retained_reviewer_bytes=retained,
            memory_interface_count=memory_count,
            mcp_or_general_tool_count=tool_count,
            error_code=value.get("error_code") if isinstance(value.get("error_code"), str) else None,
        )

    @classmethod
    def _from_discussion(
        cls,
        result: dict[str, Any],
        evidence: tuple[str, ...],
        actions: tuple[str, ...],
    ) -> "ModelAuditViewState":
        discussion_id = result.get("discussion_id")
        resolution = result.get("resolution")
        if not isinstance(discussion_id, str) or resolution not in _DISCUSSION_STATES:
            raise ValueError("invalid model Audit discussion")
        return cls(
            review_bundle_id=str(result.get("review_id", "")),
            subject_digest=_digest(result.get("candidate_digest")),
            route_model="gpt-5.6-sol",
            route_digest="blake3:" + "0" * 64,
            quorum_state="blocked",
            valid_reviewers=tuple(),
            approvals=0,
            ordinary_rejections=0,
            refusals=0,
            safety_vetoes=0,
            all_disposals_verified=False,
            expires_at_ms=int(result.get("expires_at_ms", 0)),
            evidence_refs=evidence,
            permitted_next_actions=actions,
            discussion_id=discussion_id,
            discussion_resolution=resolution,
            original_decision_digest=_digest(result.get("original_decision_digest")),
        )
