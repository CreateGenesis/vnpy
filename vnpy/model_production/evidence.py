"""Immutable exact-version stage evidence and deterministic gate validation."""

from __future__ import annotations

from dataclasses import dataclass


_REQUIRED = {
    "simulation": frozenset({"release_review"}),
    "paper": frozenset({"replay", "backtest"}),
    "shadow": frozenset({"paper"}),
    "gray": frozenset({
        "installation_readiness", "observer_convergence", "backtest", "paper", "shadow",
        "reproducibility", "hard_risk", "emergency_stop", "audit", "automated_review",
    }),
    "production": frozenset({"gray_evaluation"}),
}


@dataclass(frozen=True)
class StageEvidence:
    kind: str
    evidence_digest: str
    package_digest: str
    configuration_digest: str
    policy_digest: str
    issued_at_ms: int
    expires_at_ms: int
    passed: bool
    invalidated: bool = False


@dataclass(frozen=True)
class StageEvidenceBundle:
    package_digest: str
    configuration_digest: str
    policy_digest: str
    evidences: tuple[StageEvidence, ...]

    def validate(self, requested_stage: str, now_ms: int) -> tuple[str, ...]:
        reasons: list[str] = []
        by_kind = {item.kind: item for item in self.evidences}
        missing = _REQUIRED.get(requested_stage, frozenset()) - set(by_kind)
        if missing:
            reasons.append("EVIDENCE_MISSING")
        for item in self.evidences:
            if (
                item.package_digest != self.package_digest
                or item.configuration_digest != self.configuration_digest
                or item.policy_digest != self.policy_digest
            ):
                reasons.append("EVIDENCE_IDENTITY_MISMATCH")
            if now_ms < item.issued_at_ms or now_ms >= item.expires_at_ms:
                reasons.append("EVIDENCE_STALE")
            if item.invalidated:
                reasons.append("EVIDENCE_INVALIDATED")
            if not item.passed:
                reasons.append("EVIDENCE_FAILED")
        return tuple(dict.fromkeys(reasons))
