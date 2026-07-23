"""Versioned research-event contracts shared with agentd."""

from dataclasses import asdict, dataclass, field
from enum import IntEnum
import json
from time import time_ns
from typing import Any
from uuid import uuid4


class EventPriority(IntEnum):
    ROUTINE = 1
    CRITICAL = 2


@dataclass(frozen=True)
class AgentEvent:
    event_type: str
    payload: dict[str, Any]
    correlation_id: str = field(default_factory=lambda: str(uuid4()))
    event_id: str = field(default_factory=lambda: str(uuid4()))
    producer_id: str = "vnpy"
    producer_epoch: int = 1
    sequence: int = 0
    event_time_ms: int = field(default_factory=lambda: time_ns() // 1_000_000)
    expiry_ms: int = -1
    priority: EventPriority = EventPriority.ROUTINE
    contract_version: int = 1

    def encode(self) -> bytes:
        value = asdict(self)
        value["priority"] = int(self.priority)
        return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")

    @classmethod
    def decode(cls, data: bytes) -> "AgentEvent":
        value = json.loads(data)
        if value.get("contract_version") != 1:
            raise ValueError("unsupported Agent event contract")
        value["priority"] = EventPriority(value["priority"])
        return cls(**value)
