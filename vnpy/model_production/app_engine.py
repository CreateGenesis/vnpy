"""vn.py application shell for model-production lifecycle visibility."""

from __future__ import annotations

from dataclasses import dataclass

from vnpy.event import EventEngine
from vnpy.trader.engine import BaseEngine, MainEngine


APP_NAME = "ModelProduction"


@dataclass(frozen=True)
class ModelProductionSnapshot:
    """Minimal immutable setup state exposed before lifecycle implementation."""

    revision: int
    state: str
    authority: str


class ModelProductionEngine(BaseEngine):
    """Own lifecycle and risk application inside the vn.py application process."""

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        super().__init__(main_engine, event_engine, APP_NAME)
        self._revision = 0
        self._state = "setup"

    def snapshot(self) -> ModelProductionSnapshot:
        return ModelProductionSnapshot(
            revision=self._revision,
            state=self._state,
            authority="vnpy_lifecycle_risk_order",
        )
