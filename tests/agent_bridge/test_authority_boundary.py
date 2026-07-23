import inspect

import vnpy.agent_bridge as bridge


def test_bridge_exports_no_trading_authority() -> None:
    prohibited = {"order", "cancel", "position", "risk", "gateway", "broker", "clear_breaker", "strategy_control"}
    exported = {name.lower() for name in dir(bridge)}
    assert prohibited.isdisjoint(exported)
    source = inspect.getsource(bridge.AgentBridgeEngine).lower()
    assert all(f"def {name}" not in source for name in prohibited)
