"""vn.py adapter for research events; intentionally independent of trading engines."""

from collections import deque
from copy import deepcopy
from dataclasses import asdict, dataclass
from enum import Enum
import json
from pathlib import Path
from threading import Lock
from time import time_ns
from typing import Any, Callable

from .events import (
    AgentEvent,
    EventPriority,
    LiveValidationAck,
    LiveValidationContractError,
    LiveValidationEvent,
)
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
        self._live_store_path = self.root / "live-validation-projection-v1.json"
        self._live_backup_path = self.root / "live-validation-projection-v1.backup.json"
        self._live_state = self._empty_live_state()
        self._live_ack_queue: deque[str] = deque()
        self._live_ack_leases: set[str] = set()
        self._live_listeners: list[Callable[[LiveValidationEvent, LiveValidationAck], None]] = []
        self._load_live_validation_state()

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

    @staticmethod
    def _empty_live_state() -> dict[str, Any]:
        return {"contract_version": 1, "campaigns": {}, "acks": {}}

    def _decode_live_state(self, raw: str) -> dict[str, Any]:
        value = json.loads(raw)
        if (
            not isinstance(value, dict)
            or set(value) != {"contract_version", "campaigns", "acks"}
            or value["contract_version"] != 1
            or not isinstance(value["campaigns"], dict)
            or not isinstance(value["acks"], dict)
        ):
            raise ValueError("invalid live-validation projection store")
        for campaign in value["campaigns"].values():
            if (
                not isinstance(campaign, dict)
                or set(campaign) != {"candidate_digest", "producer_epoch", "pages"}
                or not isinstance(campaign["pages"], dict)
            ):
                raise ValueError("invalid live-validation campaign store")
            for event_value in campaign["pages"].values():
                LiveValidationEvent.decode(event_value)
        for record in value["acks"].values():
            if (
                not isinstance(record, dict)
                or set(record) != {"ack", "delivered", "event_identity"}
                or not isinstance(record["delivered"], bool)
            ):
                raise ValueError("invalid live-validation ACK store")
            LiveValidationAck.decode(record["ack"])
        return value

    def _load_live_validation_state(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        loaded: dict[str, Any] | None = None
        for path in (self._live_store_path, self._live_backup_path):
            if not path.exists():
                continue
            try:
                loaded = self._decode_live_state(path.read_text(encoding="utf-8"))
                break
            except (OSError, ValueError, json.JSONDecodeError):
                self._health = BridgeHealth.RECOVERING
                self._last_error = "LIVE_VALIDATION_STORE_RECOVERY"
        if loaded is not None:
            self._live_state = loaded
        self._live_ack_queue = deque(
            event_id
            for event_id, record in self._live_state["acks"].items()
            if not record["delivered"]
        )
        self._live_ack_leases = set()

    def _persist_live_validation_state(self) -> None:
        encoded = json.dumps(
            self._live_state,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        temporary = self._live_store_path.with_suffix(".tmp")
        temporary.write_text(encoded, encoding="utf-8")
        if self._live_store_path.exists():
            try:
                current = self._live_store_path.read_text(encoding="utf-8")
                self._decode_live_state(current)
                backup_temporary = self._live_backup_path.with_suffix(".tmp")
                backup_temporary.write_text(current, encoding="utf-8")
                backup_temporary.replace(self._live_backup_path)
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        temporary.replace(self._live_store_path)

    @staticmethod
    def _event_identity(event: LiveValidationEvent) -> str:
        return ":".join(
            (
                event.campaign_id,
                event.candidate_digest,
                str(event.producer_epoch),
                str(event.revision),
                event.page_key,
                event.payload_digest,
            )
        )

    def _record_live_ack(
        self,
        event: LiveValidationEvent,
        *,
        status: str,
        error_code: str | None,
        received_at_ms: int,
    ) -> LiveValidationAck:
        ack = LiveValidationAck.create(
            event,
            status=status,
            error_code=error_code,
            received_at_ms=received_at_ms,
        )
        self._live_state["acks"][event.event_id] = {
            "ack": asdict(ack),
            "delivered": False,
            "event_identity": self._event_identity(event),
        }
        self._persist_live_validation_state()
        self._live_ack_queue.append(event.event_id)
        return ack

    def subscribe_live_validation(
        self,
        listener: Callable[[LiveValidationEvent, LiveValidationAck], None],
    ) -> None:
        """Subscribe to durable accepted revisions; callbacks cannot mutate providers."""
        with self._state_lock:
            self._live_listeners.append(listener)

    def apply_live_validation_event(
        self,
        value: bytes | str | dict[str, Any] | LiveValidationEvent,
        *,
        received_at_ms: int | None = None,
    ) -> LiveValidationAck:
        """Validate, persist, and ACK one read-only projection event."""
        event = value if isinstance(value, LiveValidationEvent) else LiveValidationEvent.decode(value)
        received = received_at_ms if received_at_ms is not None else time_ns() // 1_000_000
        received = max(1, received)
        listeners: tuple[Callable[[LiveValidationEvent, LiveValidationAck], None], ...] = ()
        with self._state_lock:
            existing_ack = self._live_state["acks"].get(event.event_id)
            if existing_ack is not None:
                if existing_ack["event_identity"] != self._event_identity(event):
                    raise LiveValidationContractError(
                        "EVENT_ID_COLLISION", "event ID reused for different projection content"
                    )
                return LiveValidationAck.decode(existing_ack["ack"])

            campaign = self._live_state["campaigns"].get(event.campaign_id)
            if campaign is not None and campaign["candidate_digest"] != event.candidate_digest:
                return self._record_live_ack(
                    event,
                    status="invalid_rejected",
                    error_code="CANDIDATE_DRIFT",
                    received_at_ms=received,
                )
            if campaign is None:
                campaign = {
                    "candidate_digest": event.candidate_digest,
                    "producer_epoch": event.producer_epoch,
                    "pages": {},
                }
                self._live_state["campaigns"][event.campaign_id] = campaign

            if event.producer_epoch < campaign["producer_epoch"]:
                return self._record_live_ack(
                    event,
                    status="stale_rejected",
                    error_code="STALE_PRODUCER_EPOCH",
                    received_at_ms=received,
                )

            current_value = campaign["pages"].get(event.page_key)
            if current_value is not None:
                current = LiveValidationEvent.decode(current_value)
                if event.revision < current.revision:
                    return self._record_live_ack(
                        event,
                        status="stale_rejected",
                        error_code="STALE_REVISION",
                        received_at_ms=received,
                    )
                if event.revision == current.revision:
                    if event.payload_digest == current.payload_digest:
                        return self._record_live_ack(
                            event,
                            status="duplicate",
                            error_code=None,
                            received_at_ms=received,
                        )
                    return self._record_live_ack(
                        event,
                        status="invalid_rejected",
                        error_code="REVISION_COLLISION",
                        received_at_ms=received,
                    )
                if event.revision != current.revision + 1:
                    return self._record_live_ack(
                        event,
                        status="invalid_rejected",
                        error_code="OUT_OF_ORDER_REVISION",
                        received_at_ms=received,
                    )
                if event.previous_payload_digest != current.payload_digest:
                    return self._record_live_ack(
                        event,
                        status="invalid_rejected",
                        error_code="PROJECTION_CHAIN_MISMATCH",
                        received_at_ms=received,
                    )
            elif event.revision != 1:
                return self._record_live_ack(
                    event,
                    status="invalid_rejected",
                    error_code="PROJECTION_CHAIN_MISMATCH",
                    received_at_ms=received,
                )

            campaign["producer_epoch"] = max(campaign["producer_epoch"], event.producer_epoch)
            campaign["pages"][event.page_key] = asdict(event)
            ack = self._record_live_ack(
                event,
                status="applied",
                error_code=None,
                received_at_ms=received,
            )
            self._revision += 1
            self._health = BridgeHealth.HEALTHY
            self._last_error = None
            listeners = tuple(self._live_listeners)
        for listener in listeners:
            listener(event, ack)
        return ack

    def next_live_validation_ack(self) -> LiveValidationAck | None:
        """Lease each durable ACK once per process until transport confirmation."""
        with self._state_lock:
            while self._live_ack_queue:
                event_id = self._live_ack_queue.popleft()
                record = self._live_state["acks"].get(event_id)
                if record is None or record["delivered"] or event_id in self._live_ack_leases:
                    continue
                self._live_ack_leases.add(event_id)
                return LiveValidationAck.decode(record["ack"])
            return None

    def confirm_live_validation_ack(self, event_id: str, payload_digest: str) -> None:
        """Mark a leased ACK delivered only after durable transport evidence exists."""
        with self._state_lock:
            record = self._live_state["acks"].get(event_id)
            if record is None:
                raise KeyError(event_id)
            ack = LiveValidationAck.decode(record["ack"])
            if ack.payload_digest != payload_digest:
                raise LiveValidationContractError(
                    "ACK_IDENTITY_MISMATCH", "ACK confirmation digest mismatch"
                )
            if event_id not in self._live_ack_leases and not record["delivered"]:
                raise LiveValidationContractError(
                    "ACK_NOT_LEASED", "ACK must be leased before confirmation"
                )
            record["delivered"] = True
            self._live_ack_leases.discard(event_id)
            self._persist_live_validation_state()

    def live_validation_page(
        self,
        campaign_id: str,
        page_kind: str,
        page_index: int = 0,
    ) -> dict[str, Any] | None:
        """Read one last-known-valid projection page without provider access."""
        with self._state_lock:
            campaign = self._live_state["campaigns"].get(campaign_id)
            if campaign is None:
                return None
            event = campaign["pages"].get(f"{page_kind}:{page_index}")
            return deepcopy(event) if event is not None else None

    def live_validation_snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            return deepcopy(self._live_state)

    def recover_live_validation(self) -> dict[str, Any]:
        """Rebuild the read model from disk only; no provider callback is reachable."""
        with self._state_lock:
            self._live_state = self._empty_live_state()
            self._load_live_validation_state()
            return deepcopy(self._live_state)

    def snapshot(self) -> BridgeSnapshot:
        return BridgeSnapshot(self._health, self.critical.depth(), self.routine.depth(), self._revision, self._last_error)

    def close(self) -> None:
        self.critical.close()
        self.routine.close()
