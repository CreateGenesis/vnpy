from __future__ import annotations

from pathlib import Path

from vnpy.model_production.reconciliation import ReconciliationManager


def test_unknown_broker_outcome_blocks_duplicate_and_new_exposure_until_reconciled() -> None:
    manager = ReconciliationManager()
    manager.record_dispatch("effect-1", "operation-1")
    manager.record_outcome("effect-1", "unknown")
    assert manager.new_exposure_blocked
    assert manager.can_dispatch("operation-1") is False
    assert manager.can_dispatch("operation-2") is False
    manager.reconcile("effect-1", "accepted", "order-1", revision=2)
    assert manager.new_exposure_blocked is False
    assert manager.can_dispatch("operation-2") is True
    assert manager.can_dispatch("operation-1") is False


def test_timeout_partial_fill_duplicate_and_restart_preserve_reconciliation_state(tmp_path: Path) -> None:
    database = tmp_path / "broker.sqlite"
    manager = ReconciliationManager(database)
    manager.record_dispatch("effect-timeout", "operation-timeout")
    manager.record_dispatch("effect-timeout", "operation-timeout")
    manager.record_timeout("effect-timeout")
    assert manager.new_exposure_blocked

    restarted = ReconciliationManager(database)
    assert restarted.new_exposure_blocked
    assert restarted.can_dispatch("operation-timeout") is False
    restarted.reconcile("effect-timeout", "rejected", "timeout-reconciled", revision=2)
    restarted.record_dispatch("effect-partial", "operation-partial")
    restarted.record_partial_fill("effect-partial", "order-partial")
    assert restarted.new_exposure_blocked
    restarted.reconcile("effect-partial", "accepted", "order-partial", revision=3)
    restarted.record_dispatch("effect-cancel", "operation-cancel")
    restarted.record_cancel("effect-cancel", True)
    assert restarted.new_exposure_blocked is False
    restarted.record_snapshot(position_mismatch=1)
    assert restarted.new_exposure_blocked
    restarted.record_snapshot()
    assert restarted.new_exposure_blocked is False
