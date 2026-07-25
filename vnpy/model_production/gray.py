"""vn.py-owned gray-stage admission and budget accounting."""

from __future__ import annotations

from dataclasses import dataclass, field
from dataclasses import replace


@dataclass(frozen=True)
class GrayOrder:
    symbol: str
    quantity: int
    price_micros: int
    increases_exposure: bool
    cancellation: bool

    @property
    def notional_micros(self) -> int:
        return self.quantity * self.price_micros


@dataclass(frozen=True)
class GrayBudget:
    allowed_symbols: frozenset[str]
    nav_micros: int
    total_exposure_micros: int
    symbol_exposure_micros: dict[str, int]
    minute_new_exposure: int
    minute_messages: int
    session_new_exposure: int
    session_messages: int
    session_turnover_micros: int
    session_pnl_micros: int
    drawdown_micros: int
    operator_order_cap_micros: int | None = None
    operator_total_exposure_bps: int = 200
    operator_symbol_exposure_bps: int = 50
    reservations: frozenset[str] = field(default_factory=frozenset)

    def evaluate(self, order: GrayOrder) -> tuple[str, ...]:
        reasons: list[str] = []
        notional = order.notional_micros
        if len(self.allowed_symbols) > 5:
            reasons.append("GRAY_SYMBOL_LIMIT")
        if order.symbol not in self.allowed_symbols:
            reasons.append("GRAY_SYMBOL_NOT_ALLOWED")
        if self.nav_micros <= 0:
            reasons.append("GRAY_NAV_UNAVAILABLE")
            return tuple(reasons)
        total_bps = min(200, self.operator_total_exposure_bps)
        symbol_bps = min(50, self.operator_symbol_exposure_bps)
        if order.increases_exposure:
            if self.total_exposure_micros + notional > self.nav_micros * total_bps // 10_000:
                reasons.append("GRAY_TOTAL_EXPOSURE_LIMIT")
            if self.symbol_exposure_micros.get(order.symbol, 0) + notional > self.nav_micros * symbol_bps // 10_000:
                reasons.append("GRAY_SYMBOL_EXPOSURE_LIMIT")
            if notional > self.nav_micros * 25 // 10_000:
                reasons.append("GRAY_ORDER_NOTIONAL_LIMIT")
            if self.operator_order_cap_micros is not None and notional > self.operator_order_cap_micros:
                reasons.append("OPERATOR_ORDER_CAP")
            if self.minute_new_exposure >= 2:
                reasons.append("GRAY_NEW_EXPOSURE_MINUTE_LIMIT")
            if self.session_new_exposure >= 20:
                reasons.append("GRAY_NEW_EXPOSURE_SESSION_LIMIT")
            if self.session_pnl_micros <= -(self.nav_micros * 25 // 10_000):
                reasons.append("GRAY_LOSS_LIMIT")
            if self.drawdown_micros >= self.nav_micros * 50 // 10_000:
                reasons.append("GRAY_DRAWDOWN_LIMIT")
        if self.minute_messages >= 6:
            reasons.append("GRAY_MESSAGE_MINUTE_LIMIT")
        if self.session_messages >= 100:
            reasons.append("GRAY_MESSAGE_SESSION_LIMIT")
        if self.session_turnover_micros + notional > self.nav_micros * 500 // 10_000:
            reasons.append("GRAY_TURNOVER_LIMIT")
        return tuple(dict.fromkeys(reasons))

    def reserve(self, order: GrayOrder, reservation_id: str) -> GrayBudget:
        if not reservation_id:
            raise ValueError("gray reservation identity is required")
        if reservation_id in self.reservations:
            return self
        reasons = self.evaluate(order)
        if reasons:
            raise PermissionError(",".join(reasons))
        notional = order.notional_micros
        return replace(
            self,
            total_exposure_micros=(
                self.total_exposure_micros + notional
                if order.increases_exposure else self.total_exposure_micros
            ),
            symbol_exposure_micros={
                **self.symbol_exposure_micros,
                order.symbol: self.symbol_exposure_micros.get(order.symbol, 0)
                + (notional if order.increases_exposure else 0),
            },
            minute_new_exposure=self.minute_new_exposure + int(order.increases_exposure),
            minute_messages=self.minute_messages + 1,
            session_new_exposure=self.session_new_exposure + int(order.increases_exposure),
            session_messages=self.session_messages + 1,
            session_turnover_micros=self.session_turnover_micros + notional,
            reservations=self.reservations | {reservation_id},
        )


@dataclass(frozen=True)
class GrayAdmission:
    package_digest: str
    symbols: frozenset[str]
    issued_at_ms: int
    expires_at_ms: int
    active: bool = True


class GrayAdmissionRegistry:
    def __init__(self) -> None:
        self._active: GrayAdmission | None = None

    def admit(self, admission: GrayAdmission, now_ms: int) -> GrayAdmission:
        if self._active is not None and self._active.active:
            raise RuntimeError("GRAY_MODEL_ALREADY_ACTIVE")
        if (
            len(admission.symbols) == 0
            or len(admission.symbols) > 5
            or now_ms < admission.issued_at_ms
            or now_ms >= admission.expires_at_ms
        ):
            raise ValueError("INVALID_GRAY_ADMISSION")
        self._active = admission
        return admission

    def expire(self, now_ms: int) -> GrayAdmission | None:
        if self._active is not None and now_ms >= self._active.expires_at_ms:
            self._active = GrayAdmission(
                self._active.package_digest,
                self._active.symbols,
                self._active.issued_at_ms,
                self._active.expires_at_ms,
                False,
            )
        return self._active
