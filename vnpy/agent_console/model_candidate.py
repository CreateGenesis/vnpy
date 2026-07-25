"""Read-only model-candidate projection for the vn.py Agent Console."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


_FAMILIES = {"mathematical", "rule", "statistical", "ml", "dl"}
_STATES = {"draft", "validated", "quarantined", "submitted_for_review", "retired"}
_ACTIONS = {
    "model.candidate.validate",
    "model.candidate.submit",
    "model.candidate.revise",
    "model.candidate.retire",
    "audit.pre_training.start",
}


@dataclass(frozen=True)
class ModelCandidateViewState:
    """Redacted candidate state with no training, lifecycle, risk, or order control."""

    candidate_digest: str
    candidate_id: str
    revision: int
    family: str
    state: str
    author_identity: str
    author_ancestors: tuple[str, ...] = field(default_factory=tuple)
    validation_status: str | None = None
    quarantine_codes: tuple[str, ...] = field(default_factory=tuple)
    resource_summary: dict[str, int | bool] = field(default_factory=dict)
    expires_at_ms: int = 0
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    permitted_next_actions: tuple[str, ...] = field(default_factory=tuple)
    training_started: bool = False
    runtime_loaded: bool = False
    broker_authority: bool = False
    order_authority: bool = False

    @classmethod
    def from_cli_response(cls, payload: dict[str, Any]) -> "ModelCandidateViewState":
        """Validate and redact one stable `agentctl model candidate` response."""
        if payload.get("contract_version") != 1:
            raise ValueError("incompatible model candidate response")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise ValueError("model candidate result is missing")
        family = result.get("family")
        state = result.get("state")
        if family not in _FAMILIES or state not in _STATES:
            raise ValueError("invalid model candidate family or state")
        digest = result.get("candidate_digest")
        if not isinstance(digest, str) or not digest.startswith("blake3:") or len(digest) != 71:
            raise ValueError("invalid model candidate digest")
        lineage = result.get("author_lineage", {})
        if not isinstance(lineage, dict) or not isinstance(lineage.get("author_id"), str):
            raise ValueError("invalid model candidate author lineage")
        ancestors = lineage.get("ancestors", [])
        if not isinstance(ancestors, list) or any(not isinstance(item, str) for item in ancestors):
            raise ValueError("invalid model candidate ancestry")
        findings = result.get("validation_findings", [])
        if not isinstance(findings, list):
            raise ValueError("invalid model candidate findings")
        quarantine_codes = tuple(
            item["code"]
            for item in findings
            if isinstance(item, dict) and isinstance(item.get("code"), str)
        )
        resources = result.get("resources", {})
        if not isinstance(resources, dict):
            raise ValueError("invalid model candidate resources")
        allowed_resource_keys = {
            "cpu_time_ms", "memory_bytes", "output_bytes", "threads", "network"
        }
        resource_summary = {
            key: value
            for key, value in resources.items()
            if key in allowed_resource_keys and isinstance(value, (int, bool))
        }
        actions = payload.get("permitted_next_actions", [])
        if (
            not isinstance(actions, list)
            or any(not isinstance(item, str) or item not in _ACTIONS for item in actions)
        ):
            raise ValueError("invalid model candidate next action")
        evidence = payload.get("evidence_refs", [])
        if not isinstance(evidence, list) or any(not isinstance(item, str) for item in evidence):
            raise ValueError("invalid model candidate evidence")
        # The candidate projection cannot represent a positive authority or execution claim.
        if any(
            result.get(key) is not False
            for key in ("training_started", "runtime_loaded", "broker_authority", "order_authority")
        ):
            raise ValueError("candidate projection attempted authority escalation")
        return cls(
            candidate_digest=digest,
            candidate_id=str(result.get("candidate_id", "")),
            revision=int(result.get("revision", 0)),
            family=family,
            state=state,
            author_identity=lineage["author_id"],
            author_ancestors=tuple(ancestors),
            validation_status=result.get("validation_status"),
            quarantine_codes=quarantine_codes,
            resource_summary=resource_summary,
            expires_at_ms=int(result.get("expires_at_ms", 0)),
            evidence_refs=tuple(evidence),
            permitted_next_actions=tuple(actions),
        )
