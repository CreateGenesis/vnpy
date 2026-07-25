"""The sole accepted-risk to vn.py OrderRequest adapter."""

from __future__ import annotations

from vnpy.trader.constant import Direction, Exchange, Offset, OrderType
from vnpy.trader.object import OrderRequest

from .risk import ModelIntent, RiskDecision


_EXCHANGES = {"SH": Exchange.SSE, "SZ": Exchange.SZSE, "BJ": Exchange.BSE}


def to_order_request(intent: ModelIntent, risk: RiskDecision) -> OrderRequest:
    """Translate only an authoritative accepted disposition to a limit order."""

    if not risk.accepted:
        raise PermissionError("RISK_NOT_ACCEPTED")
    try:
        symbol, suffix = intent.symbol.rsplit(".", 1)
        exchange = _EXCHANGES[suffix]
    except (ValueError, KeyError) as exc:
        raise ValueError("UNSUPPORTED_A_SHARE_SYMBOL") from exc
    if intent.action == "buy":
        direction, offset = Direction.LONG, Offset.OPEN
    elif intent.action in {"sell", "reduce", "close"}:
        direction, offset = Direction.SHORT, Offset.CLOSE
    else:
        raise ValueError("MODEL_ACTION_UNSUPPORTED")
    return OrderRequest(
        symbol=symbol,
        exchange=exchange,
        direction=direction,
        type=OrderType.LIMIT,
        volume=float(risk.normalized_quantity),
        price=intent.limit_price_micros / 1_000_000,
        offset=offset,
        reference=f"model:{intent.intent_id}",
    )
