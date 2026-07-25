"""External emergency stop, residual exposure, and exact rollback authority."""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class ControlSnapshot:
    package_digest: str
    stage: str
    lifecycle_revision: int
    admission_enabled: bool
    residual_exposure: dict[str, int]
    unknown_outcomes: frozenset[str]
    reason_codes: tuple[str, ...] = ()
    working_orders: frozenset[str] = frozenset()
    draining: bool = False


class LifecycleControls:
    def __init__(self, package_digest: str, stage: str, lifecycle_revision: int) -> None:
        self._snapshot = ControlSnapshot(
            package_digest, stage, lifecycle_revision, True, {}, frozenset()
        )

    def emergency_stop(
        self,
        reason: str,
        residual_exposure: dict[str, int],
        unknown_outcomes: set[str],
        working_orders: set[str] | None = None,
    ) -> ControlSnapshot:
        if not reason:
            raise ValueError("stop reason required")
        self._snapshot = replace(
            self._snapshot,
            stage="stopped",
            lifecycle_revision=self._snapshot.lifecycle_revision + 1,
            admission_enabled=False,
            residual_exposure=dict(residual_exposure),
            unknown_outcomes=frozenset(unknown_outcomes),
            working_orders=frozenset(working_orders or ()),
            draining=bool(working_orders),
        )
        return self._snapshot

    def drain(self, cancelled_order_ids: frozenset[str]) -> ControlSnapshot:
        remaining = self._snapshot.working_orders - cancelled_order_ids
        self._snapshot = replace(
            self._snapshot,
            working_orders=remaining,
            draining=bool(remaining),
        )
        return self._snapshot

    def rollback(self, target_package_digest: str, expected_revision: int) -> ControlSnapshot:
        reasons: list[str] = []
        if self._snapshot.unknown_outcomes:
            reasons.append("UNKNOWN_OUTCOME_BLOCK")
        if expected_revision != self._snapshot.lifecycle_revision:
            reasons.append("ROLLBACK_REVISION_MISMATCH")
        return replace(self._snapshot, reason_codes=tuple(reasons))

    def reconcile_and_rollback(
        self,
        unknown_outcomes: frozenset[str],
        target_package_digest: str,
        expected_revision: int,
    ) -> ControlSnapshot:
        self._snapshot = replace(self._snapshot, unknown_outcomes=unknown_outcomes)
        checked = self.rollback(target_package_digest, expected_revision)
        if checked.reason_codes:
            return checked
        self._snapshot = replace(
            checked,
            package_digest=target_package_digest,
            stage="rolled_back",
            lifecycle_revision=expected_revision + 1,
            reason_codes=(),
        )
        return self._snapshot

    def retire(self) -> ControlSnapshot:
        reasons: list[str] = []
        if self._snapshot.stage not in {"stopped", "rolled_back"}:
            reasons.append("RETIRE_STAGE_INVALID")
        if self._snapshot.unknown_outcomes or self._snapshot.working_orders:
            reasons.append("RETIRE_RECONCILIATION_REQUIRED")
        if reasons:
            return replace(self._snapshot, reason_codes=tuple(reasons))
        self._snapshot = replace(
            self._snapshot,
            stage="retired",
            lifecycle_revision=self._snapshot.lifecycle_revision + 1,
            reason_codes=(),
        )
        return self._snapshot
