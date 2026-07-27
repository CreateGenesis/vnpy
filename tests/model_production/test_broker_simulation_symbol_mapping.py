from types import SimpleNamespace

import pytest

from vnpy.model_production.broker_simulation_model_loop import _model_symbol
from vnpy.model_production.order_intent import to_order_request
from vnpy.model_production.risk import ModelIntent, RiskDecision
from vnpy.trader.constant import Exchange


@pytest.mark.parametrize(
    ("exchange", "canonical_symbol"),
    [
        (Exchange.SSE, "600000.SH"),
        (Exchange.SZSE, "000001.SZ"),
        (Exchange.BSE, "430047.BJ"),
    ],
)
def test_model_symbols_round_trip_only_at_vnpy_boundaries(
    exchange: Exchange, canonical_symbol: str
) -> None:
    ticker = canonical_symbol.split(".", 1)[0]
    assert _model_symbol(SimpleNamespace(symbol=ticker, exchange=exchange)) == canonical_symbol

    request = to_order_request(
        ModelIntent(
            intent_id="intent-1",
            decision_id="decision-1",
            producer_id="modeld:broker-slot",
            package_digest="sha256:" + "1" * 64,
            lifecycle_revision=1,
            stage="broker_simulation",
            context_id="context-1",
            context_revision=1,
            symbol=canonical_symbol,
            action="buy",
            quantity=100,
            limit_price_micros=10_000_000,
            expires_at_ns=2_000_000_000,
        ),
        RiskDecision(True, (), 100),
    )

    assert request.symbol == ticker
    assert request.exchange is exchange
    assert request.vt_symbol == f"{ticker}.{exchange.value}"
