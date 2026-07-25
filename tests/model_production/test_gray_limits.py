from __future__ import annotations

from dataclasses import replace

from vnpy.model_production.gray import GrayBudget, GrayOrder


def budget() -> GrayBudget:
    return GrayBudget(
        allowed_symbols=frozenset({"600000.SH", "000001.SZ"}),
        nav_micros=1_000_000_000_000,
        total_exposure_micros=1_000_000_000,
        symbol_exposure_micros={"600000.SH": 100_000_000},
        minute_new_exposure=0,
        minute_messages=0,
        session_new_exposure=0,
        session_messages=0,
        session_turnover_micros=0,
        session_pnl_micros=0,
        drawdown_micros=0,
    )


def test_gray_limits_accept_bounded_order_and_reject_every_hard_maximum() -> None:
    order = GrayOrder("600000.SH", 100, 10_000_000, True, False)
    assert budget().evaluate(order) == ()
    cases = [
        (replace(budget(), allowed_symbols=frozenset({"600000.SH", "000001.SZ", "000002.SZ", "000003.SZ", "000004.SZ", "000005.SZ"})), "GRAY_SYMBOL_LIMIT"),
        (replace(budget(), total_exposure_micros=20_000_000_000), "GRAY_TOTAL_EXPOSURE_LIMIT"),
        (replace(budget(), symbol_exposure_micros={"600000.SH": 5_000_000_000}), "GRAY_SYMBOL_EXPOSURE_LIMIT"),
        (budget(), "GRAY_ORDER_NOTIONAL_LIMIT"),
        (replace(budget(), minute_new_exposure=2), "GRAY_NEW_EXPOSURE_MINUTE_LIMIT"),
        (replace(budget(), minute_messages=6), "GRAY_MESSAGE_MINUTE_LIMIT"),
        (replace(budget(), session_new_exposure=20), "GRAY_NEW_EXPOSURE_SESSION_LIMIT"),
        (replace(budget(), session_messages=100), "GRAY_MESSAGE_SESSION_LIMIT"),
        (replace(budget(), session_turnover_micros=50_000_000_000), "GRAY_TURNOVER_LIMIT"),
        (replace(budget(), session_pnl_micros=-2_500_000_000), "GRAY_LOSS_LIMIT"),
        (replace(budget(), drawdown_micros=5_000_000_000), "GRAY_DRAWDOWN_LIMIT"),
    ]
    for context, reason in cases:
        candidate = order if reason != "GRAY_ORDER_NOTIONAL_LIMIT" else replace(order, quantity=26_000)
        assert reason in context.evaluate(candidate)


def test_stricter_operator_envelope_always_wins() -> None:
    restricted = replace(budget(), operator_order_cap_micros=500_000_000)
    assert "OPERATOR_ORDER_CAP" in restricted.evaluate(GrayOrder("600000.SH", 100, 10_000_000, True, False))


def test_gray_reservation_is_idempotent_and_consumes_all_message_budgets() -> None:
    order = GrayOrder("600000.SH", 100, 10_000_000, True, False)
    reserved = budget().reserve(order, "reservation-1")
    assert reserved.minute_new_exposure == 1
    assert reserved.minute_messages == 1
    assert reserved.session_new_exposure == 1
    assert reserved.session_messages == 1
    assert reserved.session_turnover_micros == order.notional_micros
    assert reserved.reserve(order, "reservation-1") == reserved
