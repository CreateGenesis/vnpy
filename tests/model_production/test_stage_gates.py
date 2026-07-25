from __future__ import annotations

from dataclasses import replace

from vnpy.model_production.evidence import StageEvidence, StageEvidenceBundle


def digest(char: str) -> str:
    return "blake3:" + char * 64


def bundle() -> StageEvidenceBundle:
    kinds = (
        "installation_readiness", "observer_convergence", "backtest", "paper", "shadow",
        "reproducibility", "hard_risk", "emergency_stop", "audit", "automated_review",
    )
    return StageEvidenceBundle(
        package_digest=digest("a"),
        configuration_digest=digest("b"),
        policy_digest=digest("c"),
        evidences=tuple(
            StageEvidence(kind, digest(str(index % 10)), digest("a"), digest("b"), digest("c"), 1_000, 2_000, True)
            for index, kind in enumerate(kinds)
        ),
    )


def test_exact_fresh_gray_gates_pass_and_stale_mismatched_or_invalidated_fail() -> None:
    assert bundle().validate("gray", 1_500) == ()
    stale = replace(bundle(), evidences=tuple(replace(item, expires_at_ms=1_500) for item in bundle().evidences))
    assert "EVIDENCE_STALE" in stale.validate("gray", 1_500)
    mismatch_items = list(bundle().evidences)
    mismatch_items[0] = replace(mismatch_items[0], package_digest=digest("f"))
    mismatch = replace(bundle(), evidences=tuple(mismatch_items))
    assert "EVIDENCE_IDENTITY_MISMATCH" in mismatch.validate("gray", 1_500)
    invalid_items = list(bundle().evidences)
    invalid_items[0] = replace(invalid_items[0], invalidated=True)
    invalid = replace(bundle(), evidences=tuple(invalid_items))
    assert "EVIDENCE_INVALIDATED" in invalid.validate("gray", 1_500)
