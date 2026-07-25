"""vn.py app metadata for authoritative model production."""

from pathlib import Path

from vnpy.trader.app import BaseApp

from .app_engine import ModelProductionEngine


class ModelProductionApp(BaseApp):
    app_name = "ModelProduction"
    app_module = "vnpy.model_production"
    app_path = Path(__file__).parent
    display_name = "Model Production"
    engine_class = ModelProductionEngine
    widget_name = "ModelProductionWidget"
    icon_name = "model.svg"
