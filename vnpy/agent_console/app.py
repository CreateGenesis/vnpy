"""vn.py app metadata for the Agent Console read model."""

from pathlib import Path

from vnpy.trader.app import BaseApp

from .engine import AgentConsoleEngine
from .tikhub import TikHubViewState


class AgentConsoleApp(BaseApp):
    app_name = "AgentConsole"
    app_module = "vnpy.agent_console"
    app_path = Path(__file__).parent
    display_name = "Agent Console"
    engine_class = AgentConsoleEngine
    widget_name = "AgentConsoleWidget"
    icon_name = "agent.svg"
    tikhub_read_model_class = TikHubViewState
    tikhub_panel_name = "TikHub"
