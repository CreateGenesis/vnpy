"""vn.py adapter for research events; intentionally independent of trading engines."""

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from threading import Lock

from .events import AgentEvent, EventPriority
from .mmap_ring import MmapRing, RingFull


class BridgeHealth(str, Enum):
    HEALTHY = "healthy"
    BACKPRESSURED = "backpressured"
    RECOVERING = "recovering"
    FAILED = "failed"


@dataclass(frozen=True)
class BridgeSnapshot:
    health: BridgeHealth
    critical_depth: int
    routine_depth: int
    received_revision: int
    last_error: str | None


class AgentBridgeEngine:
    """Publishes approved observations and consumes research-only results."""

    def __init__(self, root: Path, direction: str = "vnpy-to-agentd") -> None:
        self.root = Path(root)
        self.critical = MmapRing(self.root / f"{direction}-critical.ring", 8_192)
        self.routine = MmapRing(self.root / f"{direction}-routine.ring", 57_344)
        self._health = BridgeHealth.HEALTHY
        self._last_error: str | None = None
        self._revision = 0
        self._sequence = 0
        self._state_lock = Lock()

    def publish_observation(self, event: AgentEvent) -> int:
        with self._state_lock:
            self._sequence += 1
            encoded = AgentEvent(**{**event.__dict__, "sequence": self._sequence}).encode()
        ring = self.critical if event.priority is EventPriority.CRITICAL else self.routine
        try:
            sequence = ring.try_publish(encoded)
            self._health = BridgeHealth.HEALTHY
            return sequence
        except RingFull as error:
            self._health = BridgeHealth.BACKPRESSURED
            self._last_error = str(error)
            raise

    def consume_research_update(self) -> AgentEvent | None:
        payload = self.critical.try_consume()
        if payload is None:
            payload = self.routine.try_consume()
        if payload is None:
            return None
        event = AgentEvent.decode(payload)
        self._revision += 1
        return event

    def snapshot(self) -> BridgeSnapshot:
        return BridgeSnapshot(self._health, self.critical.depth(), self.routine.depth(), self._revision, self._last_error)

    def close(self) -> None:
        self.critical.close()
        self.routine.close()
