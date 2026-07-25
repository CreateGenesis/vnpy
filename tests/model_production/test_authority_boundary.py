from pathlib import Path


def test_model_production_engine_is_the_only_vnpy_side_lifecycle_shell() -> None:
    source = (
        Path(__file__).parents[2] / "vnpy" / "model_production" / "engine.py"
    ).read_text(encoding="utf-8").lower()
    for prohibited in ("agent.send_order", "audit.send_order", "model.send_order", "clear_breaker"):
        assert prohibited not in source


def test_agent_bridge_exports_no_model_order_or_risk_mutation_api() -> None:
    import vnpy.agent_bridge as bridge

    exported = {name.lower() for name in dir(bridge)}
    assert {"send_order", "cancel_order", "apply_risk", "clear_breaker", "promote"}.isdisjoint(exported)
