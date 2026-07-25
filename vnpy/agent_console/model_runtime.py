"""Read-only market-model runtime projection for the vn.py Agent Console."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any


_STAGES = {"simulation", "paper", "shadow", "gray", "production", "stopped"}
_ACTIONS = {"buy", "sell", "hold", "reduce", "close", "cancel_intent", "no_action"}
_DISPOSITIONS = {"accepted", "rejected", "hypothetical", "no_action"}
_INTENT_KEYS = {
    "intent_id",
    "decision_id",
    "symbol",
    "action",
    "disposition",
    "reason_codes",
    "latency_ns",
    "evidence_digest",
}


@dataclass(frozen=True)
class RedactedModelIntentView:
    intent_id: str
    decision_id: str
    symbol: str
    action: str
    disposition: str
    reason_codes: tuple[str, ...]
    latency_ns: int
    evidence_digest: str


@dataclass(frozen=True)
class ModelRuntimeViewState:
    """Monotonic projection with no raw market, account, or mutation surface."""

    revision: int = 0
    raw_interest_count: int = 0
    qualified_wakeup_count: int = 0
    fast_action_count: int = 0
    package_digest: str = ""
    runtime_slot: str = ""
    lifecycle_revision: int = 0
    stage: str = "stopped"
    inference_p999_ms: float = 0.0
    end_to_end_p999_ms: float = 0.0
    risk_accept_count: int = 0
    risk_reject_count: int = 0
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    intents: tuple[RedactedModelIntentView, ...] = field(default_factory=tuple)
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)

    def apply_projection(self, payload: dict[str, Any]) -> ModelRuntimeViewState:
        """Validate one exact newer projection and retain only redacted fields."""

        allowed = {
            "contract_version",
            "entity_type",
            "revision",
            "raw_interest_count",
            "qualified_wakeup_count",
            "fast_action_count",
            "model_identity",
            "latency",
            "risk",
            "redacted_intents",
            "evidence_refs",
        }
        if set(payload) - allowed:
            raise ValueError("model runtime projection contains unredacted fields")
        if payload.get("contract_version") != 1 or payload.get("entity_type") != "model_runtime_projection":
            raise ValueError("incompatible model runtime projection")
        revision = _nonnegative_int(payload.get("revision"), "revision")
        if revision <= self.revision:
            raise ValueError("stale model runtime projection")
        counts = {
            name: _nonnegative_int(payload.get(name), name)
            for name in ("raw_interest_count", "qualified_wakeup_count", "fast_action_count")
        }

        identity = payload.get("model_identity")
        if not isinstance(identity, dict) or set(identity) != {
            "package_digest", "runtime_slot", "lifecycle_revision", "stage"
        }:
            raise ValueError("invalid model runtime identity")
        package_digest = _digest(identity.get("package_digest"), "package_digest")
        runtime_slot = identity.get("runtime_slot")
        stage = identity.get("stage")
        if not isinstance(runtime_slot, str) or not runtime_slot or stage not in _STAGES:
            raise ValueError("invalid model runtime identity")
        lifecycle_revision = _nonnegative_int(
            identity.get("lifecycle_revision"), "lifecycle_revision"
        )

        latency = payload.get("latency")
        if not isinstance(latency, dict) or set(latency) != {
            "inference_p999_ms", "end_to_end_p999_ms"
        }:
            raise ValueError("invalid model runtime latency")
        inference_p999_ms = _nonnegative_float(
            latency.get("inference_p999_ms"), "inference_p999_ms"
        )
        end_to_end_p999_ms = _nonnegative_float(
            latency.get("end_to_end_p999_ms"), "end_to_end_p999_ms"
        )

        risk = payload.get("risk")
        if not isinstance(risk, dict) or set(risk) != {
            "accepted_count", "rejected_count", "reason_counts"
        }:
            raise ValueError("invalid model runtime risk summary")
        accepted_count = _nonnegative_int(risk.get("accepted_count"), "accepted_count")
        rejected_count = _nonnegative_int(risk.get("rejected_count"), "rejected_count")
        reason_counts = risk.get("reason_counts")
        if not isinstance(reason_counts, dict) or any(
            not isinstance(code, str) or not code or not isinstance(count, int) or count < 0
            for code, count in reason_counts.items()
        ):
            raise ValueError("invalid model runtime risk reasons")

        raw_intents = payload.get("redacted_intents")
        if not isinstance(raw_intents, list) or len(raw_intents) > 100:
            raise ValueError("invalid redacted model intents")
        intents = tuple(_redacted_intent(item) for item in raw_intents)
        evidence_refs = payload.get("evidence_refs")
        if not isinstance(evidence_refs, list) or len(evidence_refs) > 64:
            raise ValueError("invalid model runtime evidence")
        evidence = tuple(_digest(item, "evidence_ref") for item in evidence_refs)

        return ModelRuntimeViewState(
            revision=revision,
            raw_interest_count=counts["raw_interest_count"],
            qualified_wakeup_count=counts["qualified_wakeup_count"],
            fast_action_count=counts["fast_action_count"],
            package_digest=package_digest,
            runtime_slot=runtime_slot,
            lifecycle_revision=lifecycle_revision,
            stage=stage,
            inference_p999_ms=inference_p999_ms,
            end_to_end_p999_ms=end_to_end_p999_ms,
            risk_accept_count=accepted_count,
            risk_reject_count=rejected_count,
            rejection_reasons=dict(sorted(reason_counts.items())),
            intents=intents,
            evidence_refs=evidence,
        )


def _redacted_intent(value: Any) -> RedactedModelIntentView:
    if not isinstance(value, dict) or set(value) != _INTENT_KEYS:
        raise ValueError("model intent is not redacted")
    action = value.get("action")
    disposition = value.get("disposition")
    reason_codes = value.get("reason_codes")
    if action not in _ACTIONS or disposition not in _DISPOSITIONS:
        raise ValueError("invalid redacted model intent")
    if not isinstance(reason_codes, list) or any(
        not isinstance(reason, str) or not reason for reason in reason_codes
    ):
        raise ValueError("invalid redacted model intent reasons")
    strings = {name: value.get(name) for name in ("intent_id", "decision_id", "symbol")}
    if any(not isinstance(item, str) or not item for item in strings.values()):
        raise ValueError("invalid redacted model intent identity")
    return RedactedModelIntentView(
        intent_id=strings["intent_id"],
        decision_id=strings["decision_id"],
        symbol=strings["symbol"],
        action=action,
        disposition=disposition,
        reason_codes=tuple(reason_codes),
        latency_ns=_nonnegative_int(value.get("latency_ns"), "latency_ns"),
        evidence_digest=_digest(value.get("evidence_digest"), "evidence_digest"),
    )


def _nonnegative_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"invalid {name}")
    return value

def _nonnegative_float(value: Any, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"invalid {name}")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"invalid {name}")
    return result


def _digest(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("blake3:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise ValueError(f"invalid {name}")
    return value
