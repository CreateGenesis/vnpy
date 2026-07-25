"""Redacted Agent-memory projection shared exactly by Master and vn.py."""

from dataclasses import dataclass
from typing import Any

from .models import projection_validation_error


_REQUIRED_SUMMARY = {
    "working_revision",
    "episode_health",
    "temporal_validity",
    "conflict_count",
    "procedure_approval",
    "proposal_count",
    "retrieval_cutoff_ms",
    "consolidation_state",
    "budget_remaining_units",
    "memory_health",
}


@dataclass(frozen=True)
class MemoryProjectionView:
    """Bounded status only; no raw memory content, prompts, paid data, or secrets."""

    projection_revision: int
    projection_digest: str
    source_revision: int
    source_digest: str
    working_revision: int
    episode_health: str
    temporal_validity: str
    conflict_count: int
    procedure_approval: str
    proposal_count: int
    retrieval_cutoff_ms: int
    consolidation_state: str
    budget_remaining_units: int
    memory_health: str
    evidence_refs: tuple[str, ...]
    permitted_next_actions: tuple[str, ...]
    freshness: str
    stale: bool

    @classmethod
    def from_unified(cls, projection: dict[str, Any]) -> "MemoryProjectionView":
        error = projection_validation_error(projection)
        if error is not None:
            raise ValueError(error)
        section = projection["memory"]
        summary = section["summary"]
        if not _REQUIRED_SUMMARY.issubset(summary):
            raise ValueError("MALFORMED_MEMORY_SUMMARY")
        integer_fields = (
            "working_revision",
            "conflict_count",
            "proposal_count",
            "retrieval_cutoff_ms",
            "budget_remaining_units",
        )
        if any(
            not isinstance(summary[name], int)
            or isinstance(summary[name], bool)
            or summary[name] < 0
            for name in integer_fields
        ):
            raise ValueError("MALFORMED_MEMORY_SUMMARY")
        string_fields = (
            "episode_health",
            "temporal_validity",
            "procedure_approval",
            "consolidation_state",
            "memory_health",
        )
        if any(not isinstance(summary[name], str) or not summary[name] for name in string_fields):
            raise ValueError("MALFORMED_MEMORY_SUMMARY")
        return cls(
            projection_revision=projection["projection_revision"],
            projection_digest=projection["projection_digest"],
            source_revision=section["source_revision"],
            source_digest=section["source_digest"],
            working_revision=summary["working_revision"],
            episode_health=summary["episode_health"],
            temporal_validity=summary["temporal_validity"],
            conflict_count=summary["conflict_count"],
            procedure_approval=summary["procedure_approval"],
            proposal_count=summary["proposal_count"],
            retrieval_cutoff_ms=summary["retrieval_cutoff_ms"],
            consolidation_state=summary["consolidation_state"],
            budget_remaining_units=summary["budget_remaining_units"],
            memory_health=summary["memory_health"],
            evidence_refs=tuple(section["evidence_refs"]),
            permitted_next_actions=tuple(section["permitted_next_actions"]),
            freshness=section["freshness"],
            stale=section["stale"],
        )

    def as_master_payload(self) -> dict[str, Any]:
        """Return the exact bounded facts also supplied to the Master harness."""
        return {
            "source_revision": self.source_revision,
            "source_digest": self.source_digest,
            "working_revision": self.working_revision,
            "episode_health": self.episode_health,
            "temporal_validity": self.temporal_validity,
            "conflict_count": self.conflict_count,
            "procedure_approval": self.procedure_approval,
            "proposal_count": self.proposal_count,
            "retrieval_cutoff_ms": self.retrieval_cutoff_ms,
            "consolidation_state": self.consolidation_state,
            "budget_remaining_units": self.budget_remaining_units,
            "memory_health": self.memory_health,
            "evidence_refs": list(self.evidence_refs),
            "permitted_next_actions": list(self.permitted_next_actions),
        }
