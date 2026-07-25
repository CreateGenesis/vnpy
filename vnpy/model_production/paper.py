"""Authoritative broker-inaccessible paper account."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PaperFill:
    symbol: str
    quantity: int
    price_micros: int
    fee_micros: int
    hypothetical: bool = True


class PaperAccount:
    def __init__(self, initial_cash_micros: int) -> None:
        if initial_cash_micros < 0:
            raise ValueError("paper cash must be nonnegative")
        self.cash_micros = initial_cash_micros
        self.positions: dict[str, int] = {}
        self.fills: list[PaperFill] = []
        self.fees_micros = 0

    def buy(self, symbol: str, quantity: int, price_micros: int, fee_micros: int) -> PaperFill:
        cost = quantity * price_micros + fee_micros
        if quantity <= 0 or price_micros <= 0 or fee_micros < 0 or cost > self.cash_micros:
            raise ValueError("paper order rejected")
        fill = PaperFill(symbol, quantity, price_micros, fee_micros)
        self.cash_micros -= cost
        self.positions[symbol] = self.positions.get(symbol, 0) + quantity
        self.fills.append(fill)
        self.fees_micros += fee_micros
        return fill

    def sell(self, symbol: str, quantity: int, price_micros: int, fee_micros: int) -> PaperFill:
        if quantity <= 0 or quantity > self.positions.get(symbol, 0):
            raise ValueError("paper position insufficient")
        fill = PaperFill(symbol, -quantity, price_micros, fee_micros)
        self.positions[symbol] -= quantity
        self.cash_micros += quantity * price_micros - fee_micros
        self.fees_micros += fee_micros
        self.fills.append(fill)
        return fill

    def reconcile(self) -> bool:
        return self.cash_micros >= 0 and all(quantity >= 0 for quantity in self.positions.values())
