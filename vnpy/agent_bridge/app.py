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
