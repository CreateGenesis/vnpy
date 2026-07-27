"""Independent in-process hard-safety state for model order admission."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
import math
from pathlib import Path
import sqlite3
from threading import RLock
from time import monotonic_ns
from typing import Any

from .contracts import canonical_json_v1


_CONTAINMENT_ACTIONS = frozenset({"pause", "emergency_stop"})


@dataclass(frozen=True)
class HardSafetySnapshot:
    active: bool
    revision: int
    reason_code: str | None
    severity: str | None
    evidence_digest: str | None
    activated_at_ns: int | None


@dataclass(frozen=True)
class HardSafetyNotification:
    revision: int
    event_type: str
    reason_code: str
    severity: str
    evidence_digest: str
    activated_at_ns: int


@dataclass(frozen=True)
class CancellationDisposition:
    order_digest: str
    state: str


@dataclass(frozen=True)
class ResidualPosition:
    symbol: str
    quantity: int
    available_quantity: int
    t_plus_one_locked_quantity: int
    marked_value_minor: int


@dataclass(frozen=True)
class ContainmentReceipt:
    action: str
    campaign_id: str
    gateway: str
    state: str
    detected_at_ns: int
    exposure_blocked_at_ns: int
    completed_at_ns: int
    hard_stop_deadline_met: bool
    working_order_count: int
    cancellations: tuple[CancellationDisposition, ...]
    residual_positions: tuple[ResidualPosition, ...]
    unresolved_outcomes: int
    receipt_digest: str

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "campaign_id": self.campaign_id,
            "gateway": self.gateway,
            "state": self.state,
            "detected_at_ns": self.detected_at_ns,
            "exposure_blocked_at_ns": self.exposure_blocked_at_ns,
            "completed_at_ns": self.completed_at_ns,
            "hard_stop_deadline_met": self.hard_stop_deadline_met,
            "working_order_count": self.working_order_count,
            "cancellations": [
                {
                    "order_digest": item.order_digest,
                    "state": item.state,
                }
                for item in self.cancellations
            ],
            "residual_positions": [
                {
                    "symbol": item.symbol,
                    "quantity": item.quantity,
                    "available_quantity": item.available_quantity,
                    "t_plus_one_locked_quantity": item.t_plus_one_locked_quantity,
                    "marked_value_minor": item.marked_value_minor,
                }
                for item in self.residual_positions
            ],
            "residual_exposure_minor": sum(
                item.marked_value_minor for item in self.residual_positions
            ),
            "unresolved_outcomes": self.unresolved_outcomes,
            "receipt_digest": self.receipt_digest,
        }


class HardSafetyController:
    """Latch hard safety active; no Agent or model clearance API exists."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._snapshot = HardSafetySnapshot(False, 0, None, None, None, None)
        self._notifications: list[HardSafetyNotification] = []

    def activate(
        self,
        reason_code: str,
        severity: str,
        evidence_digest: str,
        activated_at_ns: int,
    ) -> HardSafetySnapshot:
        if not reason_code or not severity or not evidence_digest or activated_at_ns <= 0:
            raise ValueError("hard-safety activation requires complete evidence")
        with self._lock:
            current = self._snapshot
            if current.active:
                return current
            self._snapshot = HardSafetySnapshot(
                active=True,
                revision=current.revision + 1,
                reason_code=reason_code,
                severity=severity,
                evidence_digest=evidence_digest,
                activated_at_ns=activated_at_ns,
            )
            self._notifications.append(
                HardSafetyNotification(
                    revision=self._snapshot.revision,
                    event_type="hard_safety_activated",
                    reason_code=reason_code,
                    severity=severity,
                    evidence_digest=evidence_digest,
                    activated_at_ns=activated_at_ns,
                )
            )
            return self._snapshot

    def snapshot(self) -> HardSafetySnapshot:
        with self._lock:
            return self._snapshot

    def notifications(self, after_revision: int = 0) -> tuple[HardSafetyNotification, ...]:
        """Return immutable transition notifications for console/evidence publication."""

        if after_revision < 0:
            raise ValueError("after_revision must be nonnegative")
        with self._lock:
            return tuple(
                notification
                for notification in self._notifications
                if notification.revision > after_revision
            )

    @contextmanager
    def admission_guard(self) -> Iterator[HardSafetySnapshot]:
        """Serialize activation with the final local risk/broker admission boundary."""

        with self._lock:
            yield self._snapshot


class BrokerSimulationContainment:
    """Contain one gateway directly inside vn.py and retain an immutable receipt."""

    def __init__(
        self,
        *,
        main_engine: Any,
        gateway_name: str,
        database: str | Path,
        clock_ns: Callable[[], int] = monotonic_ns,
    ) -> None:
        if gateway_name not in {"XTP", "TORA"}:
            raise ValueError("CONTAINMENT_GATEWAY_INVALID")
        if not callable(clock_ns):
            raise TypeError("CONTAINMENT_CLOCK_INVALID")
        self._main_engine = main_engine
        self._gateway_name = gateway_name
        self._clock_ns = clock_ns
        self._lock = RLock()
        database_path = Path(database)
        if str(database) != ":memory:":
            database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(database_path), check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute(
            """CREATE TABLE IF NOT EXISTS broker_simulation_containment_receipts (
                receipt_key TEXT PRIMARY KEY,
                action TEXT NOT NULL,
                campaign_id TEXT NOT NULL,
                gateway TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                receipt_digest TEXT NOT NULL,
                UNIQUE(action, campaign_id, gateway)
            )"""
        )
        self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def contain(
        self,
        *,
        action: str,
        campaign_id: str,
        detected_at_ns: int,
        exposure_blocked_at_ns: int | None = None,
    ) -> ContainmentReceipt:
        if action not in _CONTAINMENT_ACTIONS:
            raise ValueError("CONTAINMENT_ACTION_INVALID")
        if not campaign_id.strip() or detected_at_ns <= 0:
            raise ValueError("CONTAINMENT_IDENTITY_INVALID")
        receipt_key = _receipt_key(action, campaign_id, self._gateway_name)
        with self._lock:
            retained = self._read_receipt(receipt_key)
            if retained is not None:
                return retained

            blocked_at_ns = (
                self._clock_ns()
                if exposure_blocked_at_ns is None
                else exposure_blocked_at_ns
            )
            if blocked_at_ns < detected_at_ns:
                raise ValueError("CONTAINMENT_BLOCK_TIME_INVALID")

            cancellations: list[CancellationDisposition] = []
            residual_positions: list[ResidualPosition] = []
            unresolved_outcomes = 0
            working_order_count = 0

            try:
                working_orders = tuple(self._main_engine.get_all_active_orders())
            except Exception:
                working_orders = ()
                unresolved_outcomes += 1
            for index, order in enumerate(working_orders):
                if getattr(order, "gateway_name", None) != self._gateway_name:
                    continue
                working_order_count += 1
                identity = str(getattr(order, "vt_orderid", f"missing:{index}"))
                order_digest = _text_digest(identity)
                try:
                    request = order.create_cancel_request()
                    self._main_engine.cancel_order(request, self._gateway_name)
                    cancellation_state = "requested"
                except Exception:
                    cancellation_state = "failed"
                    unresolved_outcomes += 1
                cancellations.append(
                    CancellationDisposition(order_digest, cancellation_state)
                )

            try:
                positions = tuple(self._main_engine.get_all_positions())
            except Exception:
                positions = ()
                unresolved_outcomes += 1
            for position in positions:
                if getattr(position, "gateway_name", None) != self._gateway_name:
                    continue
                try:
                    residual = _residual_position(position)
                except (TypeError, ValueError):
                    unresolved_outcomes += 1
                    continue
                if residual.quantity > 0:
                    residual_positions.append(residual)

            completed_at_ns = self._clock_ns()
            if completed_at_ns < blocked_at_ns:
                raise ValueError("CONTAINMENT_COMPLETION_TIME_INVALID")
            deadline_met = (
                action != "emergency_stop"
                or blocked_at_ns - detected_at_ns <= 1_000_000_000
            )
            state = (
                "contained"
                if unresolved_outcomes == 0 and deadline_met
                else "uncertain"
            )
            unsigned = {
                "action": action,
                "campaign_id": campaign_id,
                "gateway": self._gateway_name,
                "state": state,
                "detected_at_ns": detected_at_ns,
                "exposure_blocked_at_ns": blocked_at_ns,
                "completed_at_ns": completed_at_ns,
                "hard_stop_deadline_met": deadline_met,
                "working_order_count": working_order_count,
                "cancellations": [
                    {"order_digest": item.order_digest, "state": item.state}
                    for item in cancellations
                ],
                "residual_positions": [
                    {
                        "symbol": item.symbol,
                        "quantity": item.quantity,
                        "available_quantity": item.available_quantity,
                        "t_plus_one_locked_quantity": item.t_plus_one_locked_quantity,
                        "marked_value_minor": item.marked_value_minor,
                    }
                    for item in residual_positions
                ],
                "unresolved_outcomes": unresolved_outcomes,
            }
            receipt = ContainmentReceipt(
                action=action,
                campaign_id=campaign_id,
                gateway=self._gateway_name,
                state=state,
                detected_at_ns=detected_at_ns,
                exposure_blocked_at_ns=blocked_at_ns,
                completed_at_ns=completed_at_ns,
                hard_stop_deadline_met=deadline_met,
                working_order_count=working_order_count,
                cancellations=tuple(cancellations),
                residual_positions=tuple(residual_positions),
                unresolved_outcomes=unresolved_outcomes,
                receipt_digest=_payload_digest(unsigned),
            )
            self._persist(receipt_key, receipt)
            return receipt

    def receipt(self, action: str, campaign_id: str) -> ContainmentReceipt:
        if action not in _CONTAINMENT_ACTIONS or not campaign_id.strip():
            raise ValueError("CONTAINMENT_IDENTITY_INVALID")
        with self._lock:
            retained = self._read_receipt(
                _receipt_key(action, campaign_id, self._gateway_name)
            )
            if retained is None:
                raise KeyError("CONTAINMENT_RECEIPT_NOT_FOUND")
            return retained

    def _persist(self, receipt_key: str, receipt: ContainmentReceipt) -> None:
        payload = receipt.to_public_dict()
        encoded = canonical_json_v1(payload).decode("utf-8")
        self._connection.execute(
            """INSERT INTO broker_simulation_containment_receipts(
                receipt_key,action,campaign_id,gateway,payload_json,receipt_digest
            ) VALUES(?,?,?,?,?,?)""",
            (
                receipt_key,
                receipt.action,
                receipt.campaign_id,
                receipt.gateway,
                encoded,
                receipt.receipt_digest,
            ),
        )
        self._connection.commit()

    def _read_receipt(self, receipt_key: str) -> ContainmentReceipt | None:
        row = self._connection.execute(
            "SELECT payload_json,receipt_digest FROM broker_simulation_containment_receipts WHERE receipt_key=?",
            (receipt_key,),
        ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row[0])
            receipt = _receipt_from_dict(payload)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("CONTAINMENT_RECEIPT_INVALID") from exc
        if receipt.receipt_digest != row[1]:
            raise ValueError("CONTAINMENT_RECEIPT_INVALID")
        return receipt


def _residual_position(position: Any) -> ResidualPosition:
    symbol = getattr(position, "vt_symbol", None)
    if not isinstance(symbol, str) or not symbol:
        raise ValueError("CONTAINMENT_POSITION_INVALID")
    quantity = _whole_nonnegative(getattr(position, "volume", None))
    yesterday = min(
        quantity,
        _whole_nonnegative(getattr(position, "yd_volume", None)),
    )
    frozen = min(
        yesterday,
        _whole_nonnegative(getattr(position, "frozen", None)),
    )
    available = yesterday - frozen
    locked = quantity - yesterday
    price = getattr(position, "price", None)
    if (
        isinstance(price, bool)
        or not isinstance(price, int | float)
        or not math.isfinite(price)
        or price < 0
    ):
        raise ValueError("CONTAINMENT_POSITION_INVALID")
    return ResidualPosition(
        symbol=symbol,
        quantity=quantity,
        available_quantity=available,
        t_plus_one_locked_quantity=locked,
        marked_value_minor=round(quantity * price * 100),
    )


def _whole_nonnegative(value: Any) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value < 0
        or not float(value).is_integer()
    ):
        raise ValueError("CONTAINMENT_QUANTITY_INVALID")
    return int(value)


def _receipt_from_dict(value: dict[str, Any]) -> ContainmentReceipt:
    unsigned = dict(value)
    receipt_digest = unsigned.pop("receipt_digest")
    residual_exposure = unsigned.pop("residual_exposure_minor")
    if receipt_digest != _payload_digest(unsigned):
        raise ValueError("CONTAINMENT_RECEIPT_DIGEST_INVALID")
    cancellations = tuple(
        CancellationDisposition(item["order_digest"], item["state"])
        for item in unsigned["cancellations"]
    )
    positions = tuple(
        ResidualPosition(
            item["symbol"],
            item["quantity"],
            item["available_quantity"],
            item["t_plus_one_locked_quantity"],
            item["marked_value_minor"],
        )
        for item in unsigned["residual_positions"]
    )
    if residual_exposure != sum(item.marked_value_minor for item in positions):
        raise ValueError("CONTAINMENT_RESIDUAL_EXPOSURE_INVALID")
    return ContainmentReceipt(
        action=unsigned["action"],
        campaign_id=unsigned["campaign_id"],
        gateway=unsigned["gateway"],
        state=unsigned["state"],
        detected_at_ns=unsigned["detected_at_ns"],
        exposure_blocked_at_ns=unsigned["exposure_blocked_at_ns"],
        completed_at_ns=unsigned["completed_at_ns"],
        hard_stop_deadline_met=unsigned["hard_stop_deadline_met"],
        working_order_count=unsigned["working_order_count"],
        cancellations=cancellations,
        residual_positions=positions,
        unresolved_outcomes=unsigned["unresolved_outcomes"],
        receipt_digest=receipt_digest,
    )


def _receipt_key(action: str, campaign_id: str, gateway: str) -> str:
    return _text_digest(f"{action}:{campaign_id}:{gateway}")


def _text_digest(value: str) -> str:
    return f"sha256:{sha256(value.encode()).hexdigest()}"


def _payload_digest(value: dict[str, Any]) -> str:
    return f"sha256:{sha256(canonical_json_v1(value)).hexdigest()}"
