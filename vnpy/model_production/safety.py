"""Independent in-process hard-safety state for model order admission."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from threading import RLock
from collections.abc import Iterator


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
