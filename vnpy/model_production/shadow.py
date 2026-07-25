"""Broker-inaccessible current-market shadow decision ledger."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ShadowDecision:
    decision_id: str
    symbol: str
    action: str
    quantity: int
    hypothetical: bool = True


class ShadowRuntime:
    def __init__(self) -> None:
        self.decisions: list[ShadowDecision] = []
        self.broker_effect_count = 0
        self.divergences: list[dict[str, object]] = []

    def record(self, decision_id: str, symbol: str, action: str, quantity: int) -> ShadowDecision:
        decision = ShadowDecision(decision_id, symbol, action, quantity)
        self.decisions.append(decision)
        return decision

    def compare(self, decision_id: str, production_action: str) -> dict[str, object]:
        decision = next(item for item in self.decisions if item.decision_id == decision_id)
        evidence = {
            "decision_id": decision_id,
            "shadow_action": decision.action,
            "production_action": production_action,
            "diverged": decision.action != production_action,
            "broker_effect_count": 0,
        }
        self.divergences.append(evidence)
        return evidence
