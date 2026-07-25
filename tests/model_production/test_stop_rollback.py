from __future__ import annotations

from vnpy.model_production.controls import LifecycleControls


def test_emergency_stop_preserves_residual_exposure_and_requires_exact_rollback() -> None:
    controls = LifecycleControls("blake3:" + "a" * 64, "gray", 7)
    stopped = controls.emergency_stop(
        "operator-stop", {"600000.SH": 100}, {"effect-1"}, {"order-1"}
    )
    assert stopped.admission_enabled is False
    assert stopped.stage == "stopped"
    assert stopped.residual_exposure == {"600000.SH": 100}
    assert stopped.unknown_outcomes == frozenset({"effect-1"})
    assert stopped.draining
    assert controls.drain(frozenset({"order-1"})).working_orders == frozenset()
    assert "UNKNOWN_OUTCOME_BLOCK" in controls.rollback("blake3:" + "b" * 64, 8).reason_codes
    rolled = controls.reconcile_and_rollback(frozenset(), "blake3:" + "b" * 64, 8)
    assert rolled.stage == "rolled_back"
    assert rolled.package_digest == "blake3:" + "b" * 64
    assert controls.retire().stage == "retired"
