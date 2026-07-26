from vnpy.agent_console.engine import AgentConsoleEngine


class FakeEventEngine:
    def __init__(self) -> None:
        self.registered: list[tuple[str, object]] = []

    def register(self, event_type: str, handler: object) -> None:
        self.registered.append((event_type, handler))

    def unregister(self, event_type: str, handler: object) -> None:
        self.registered.remove((event_type, handler))


def test_console_engine_is_vnpy_base_engine_compatible() -> None:
    main_engine = object()
    event_engine = FakeEventEngine()
    engine = AgentConsoleEngine(main_engine, event_engine)
    assert engine.main_engine is main_engine
    assert engine.event_engine is event_engine
    assert engine.engine_name == "agent_console"
    engine.close()
