"""vn.py application registration for the authenticated Agent Console."""

from pathlib import Path

from vnpy.trader.app import BaseApp

from .engine import AgentConsoleEngine
from .ui import AgentConsoleWidget


class AgentConsoleApp(BaseApp):
    """Expose the real Qt guidance workspace through vn.py's MainWindow."""

    app_name: str = "AgentConsole"
    app_module: str = __module__
    app_path: Path = Path(__file__).parent
    display_name: str = "Agent Console"
    engine_class = AgentConsoleEngine
    widget_name: str = "AgentConsoleWidget"
    icon_name: str = ""


__all__ = ["AgentConsoleApp", "AgentConsoleEngine", "AgentConsoleWidget"]
