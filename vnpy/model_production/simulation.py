"""Broker-inaccessible exact-package replay and backtest evidence adapter."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SimulationDecision:
    decision_id: str
    stage: str
    package_digest: str
    hypothetical: bool = True
    broker_effect_count: int = 0


def record_simulation_decision(
    decision_id: str,
    stage: str,
    package_digest: str = "blake3:" + "0" * 64,
) -> SimulationDecision:
    if stage not in {"replay", "backtest", "simulation"}:
        raise ValueError("unsupported simulation stage")
    return SimulationDecision(decision_id, stage, package_digest)


class SimulationRunner:
    def __init__(self, exact_package_digest: str) -> None:
        self._exact_package_digest = exact_package_digest

    def run(self, decision_id: str, stage: str, package_digest: str) -> SimulationDecision:
        if package_digest != self._exact_package_digest:
            raise ValueError("SIMULATION_PACKAGE_MISMATCH")
        return record_simulation_decision(decision_id, stage, package_digest)
