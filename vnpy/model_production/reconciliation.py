"""Broker outcome certainty and new-exposure reconciliation authority."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
from threading import RLock


@dataclass(frozen=True)
class BrokerOutcome:
    effect_id: str
    operation_key: str
    state: str
    order_id: str | None = None
    reconciliation_revision: int = 0


class ReconciliationManager:
    def __init__(self, database: str | Path = ":memory:") -> None:
        self._outcomes: dict[str, BrokerOutcome] = {}
        self._operations: dict[str, str] = {}
        self._lock = RLock()
        self._discrepancies: dict[str, int] = {}
        self._connection = sqlite3.connect(str(database), check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute(
            """CREATE TABLE IF NOT EXISTS broker_reconciliation (
                effect_id TEXT PRIMARY KEY, operation_key TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL, order_id TEXT, reconciliation_revision INTEGER NOT NULL
            )"""
        )
        for row in self._connection.execute(
            "SELECT effect_id,operation_key,state,order_id,reconciliation_revision FROM broker_reconciliation"
        ):
            outcome = BrokerOutcome(*row)
            self._outcomes[outcome.effect_id] = outcome
            self._operations[outcome.operation_key] = outcome.effect_id

    @property
    def new_exposure_blocked(self) -> bool:
        with self._lock:
            return bool(self._discrepancies) or any(
                outcome.state in {"unknown", "partial"} for outcome in self._outcomes.values()
            )

    def record_dispatch(self, effect_id: str, operation_key: str) -> None:
        with self._lock:
            if operation_key in self._operations and self._operations[operation_key] != effect_id:
                raise RuntimeError("OPERATION_KEY_COLLISION")
            if effect_id in self._outcomes:
                if self._outcomes[effect_id].operation_key != operation_key:
                    raise RuntimeError("EFFECT_IDENTITY_DRIFT")
                return
            self._operations[operation_key] = effect_id
            outcome = BrokerOutcome(effect_id, operation_key, "dispatched")
            self._outcomes[effect_id] = outcome
            self._persist(outcome)

    def record_outcome(self, effect_id: str, state: str) -> None:
        if state not in {"accepted", "rejected", "unknown", "partial", "cancelled", "cancel_rejected"}:
            raise ValueError("invalid broker outcome")
        with self._lock:
            current = self._outcomes[effect_id]
            outcome = BrokerOutcome(effect_id, current.operation_key, state)
            self._outcomes[effect_id] = outcome
            self._persist(outcome)

    def record_timeout(self, effect_id: str) -> None:
        self.record_outcome(effect_id, "unknown")

    def record_partial_fill(self, effect_id: str, order_id: str) -> None:
        with self._lock:
            current = self._outcomes[effect_id]
            outcome = BrokerOutcome(effect_id, current.operation_key, "partial", order_id)
            self._outcomes[effect_id] = outcome
            self._persist(outcome)

    def record_cancel(self, effect_id: str, cancelled: bool) -> None:
        self.record_outcome(effect_id, "cancelled" if cancelled else "cancel_rejected")

    def record_snapshot(
        self,
        *,
        cash_mismatch: int = 0,
        position_mismatch: int = 0,
        order_mismatch: int = 0,
        trade_mismatch: int = 0,
    ) -> None:
        values = {
            "cash": cash_mismatch,
            "position": position_mismatch,
            "order": order_mismatch,
            "trade": trade_mismatch,
        }
        with self._lock:
            self._discrepancies = {key: value for key, value in values.items() if value != 0}

    def can_dispatch(self, operation_key: str) -> bool:
        with self._lock:
            return operation_key not in self._operations and not self.new_exposure_blocked

    def reconcile(self, effect_id: str, state: str, order_id: str, revision: int) -> None:
        if state not in {"accepted", "rejected"} or revision <= 0:
            raise ValueError("invalid reconciliation")
        with self._lock:
            current = self._outcomes[effect_id]
            self._outcomes[effect_id] = BrokerOutcome(
                effect_id, current.operation_key, state, order_id, revision
            )
            self._persist(self._outcomes[effect_id])

    def _persist(self, outcome: BrokerOutcome) -> None:
        self._connection.execute(
            """INSERT INTO broker_reconciliation(
                effect_id,operation_key,state,order_id,reconciliation_revision
            ) VALUES(?,?,?,?,?) ON CONFLICT(effect_id) DO UPDATE SET
                state=excluded.state,order_id=excluded.order_id,
                reconciliation_revision=excluded.reconciliation_revision""",
            (
                outcome.effect_id, outcome.operation_key, outcome.state,
                outcome.order_id, outcome.reconciliation_revision,
            ),
        )
        self._connection.commit()
