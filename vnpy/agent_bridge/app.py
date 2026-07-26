"""vn.py app metadata for the research-only Agent bridge."""

from pathlib import Path

from vnpy.trader.app import BaseApp

from .engine import AgentBridgeEngine


class AgentBridgeApp(BaseApp):
    app_name = "AgentBridge"
    app_module = "vnpy.agent_bridge"
    app_path = Path(__file__).parent
    display_name = "Agent Bridge"
    engine_class = AgentBridgeEngine
    widget_name = "AgentBridgeWidget"
    icon_name = "agent.svg"
    live_validation_contract_version = 1
    live_validation_projection_store = "live-validation-projection-v1.json"

    @classmethod
    def open_research_bridge(cls, root: Path) -> AgentBridgeEngine:
        """Open the bridge and rebuild only its durable read model."""
        engine = cls.engine_class(root)
        engine.recover_live_validation()
        return engine
