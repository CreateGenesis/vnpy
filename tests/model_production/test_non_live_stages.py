from __future__ import annotations

import pytest

from vnpy.model_production.execution import BrokerEffectDispatcher, BrokerInaccessibleError
from vnpy.model_production.paper import PaperAccount
from vnpy.model_production.shadow import ShadowRuntime
from vnpy.model_production.simulation import SimulationRunner


@pytest.mark.parametrize("stage", ["replay", "backtest", "simulation", "paper", "shadow"])
def test_non_live_stages_are_broker_inaccessible(stage: str) -> None:
    calls: list[object] = []
    dispatcher = BrokerEffectDispatcher(lambda request: calls.append(request) or "order-1")
    with pytest.raises(BrokerInaccessibleError):
        dispatcher.dispatch(stage, object(), "effect-1")
    assert calls == []


def test_paper_and_shadow_keep_hypothetical_state_separate() -> None:
    paper = PaperAccount(initial_cash_micros=2_000_000_000)
    fill = paper.buy("600000.SH", 100, 10_000_000, fee_micros=1_000)
    assert fill.hypothetical
    assert paper.cash_micros == 2_000_000_000 - 1_000_000_000 - 1_000
    assert paper.positions["600000.SH"] == 100
    shadow = ShadowRuntime()
    decision = shadow.record("decision-1", "600000.SH", "buy", 100)
    assert decision.hypothetical
    assert shadow.broker_effect_count == 0
    exact = "blake3:" + "a" * 64
    replay = SimulationRunner(exact).run("decision-2", "replay", exact)
    assert replay.hypothetical and replay.package_digest == exact
    with pytest.raises(ValueError, match="PACKAGE_MISMATCH"):
        SimulationRunner(exact).run("decision-3", "backtest", "blake3:" + "b" * 64)
