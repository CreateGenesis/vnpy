"""Redacted dynamic Task/Workflow/Skill/CLI projection consumed by vn.py."""

from dataclasses import dataclass
from typing import Any

from .models import projection_validation_error


_DYNAMIC_SECTIONS = (
    "task",
    "workflow",
    "workers",
    "qualifications",
    "skills",
    "tools_and_cli",
    "pattern_similarity",
    "solidification",
    "stateless_audit",
    "resources",
    "recovery",
)


@dataclass(frozen=True)
class WorkflowProjectionView:
    """Exact bounded view shared with the Master; no raw context or Tool payloads."""

    projection_id: str
    projection_revision: int
    projection_digest: str
    correlation_id: str
    created_at_ms: int
    expires_at_ms: int
    sections: dict[str, dict[str, Any]]

    @classmethod
    def from_unified(cls, projection: dict[str, Any]) -> "WorkflowProjectionView":
        error = projection_validation_error(projection)
        if error is not None:
            raise ValueError(error)
        sections = {
            name: {
                "source_revision": projection[name]["source_revision"],
                "source_digest": projection[name]["source_digest"],
                "state": projection[name]["state"],
                "certainty": projection[name]["certainty"],
                "freshness": projection[name]["freshness"],
                "summary": dict(projection[name]["summary"]),
                "evidence_refs": list(projection[name]["evidence_refs"]),
                "permitted_next_actions": list(
                    projection[name]["permitted_next_actions"]
                ),
                "updated_at_ms": projection[name]["updated_at_ms"],
                "stale": projection[name]["stale"],
                "last_error_code": projection[name].get("last_error_code"),
            }
            for name in _DYNAMIC_SECTIONS
        }
        return cls(
            projection_id=projection["projection_id"],
            projection_revision=projection["projection_revision"],
            projection_digest=projection["projection_digest"],
            correlation_id=projection["correlation_id"],
            created_at_ms=projection["created_at_ms"],
            expires_at_ms=projection["expires_at_ms"],
            sections=sections,
        )

    @property
    def permitted_next_actions(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    action
                    for section in self.sections.values()
                    for action in section["permitted_next_actions"]
                }
            )
        )
