"""Concrete fail-closed composition for the loopback investor demo."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from secrets import token_urlsafe
from threading import RLock
from time import time_ns
from typing import Any
from uuid import UUID, uuid4

from .app import DemoWebConfig
from .guidance import GuidanceClientBinding, SideMasterGuidanceClient
from .projection import (
    CandidateProjectionInput,
    DemoProjectionInput,
    DemoProjectionStore,
    GatewayProjectionInput,
    LatencyProjectionInput,
    PositionProjectionInput,
)
from .run_clients import BrokerSimulationRunClient, RunClientBinding
from .transport import LengthPrefixedJsonTransport


_DIGEST = re.compile(r"^(?:sha256|blake3):[0-9a-f]{64}$")
_SYMBOL = re.compile(r"^[0-9]{6}\.(?:SH|SZ|BJ)$")
_GATEWAYS = ("XTP", "TORA")
_UNAVAILABLE_DIGEST = f"sha256:{sha256(b'unavailable').hexdigest()}"


@dataclass(frozen=True)
class DemoCandidate:
    ready: bool
    candidate_digest: str
    author_lineage_digest: str
    package_digest: str
    configuration_digest: str
    policy_digest: str
    symbols: tuple[str, ...]
    calendar_sessions: tuple[date, ...]
    lifecycle_revision: int


@dataclass(frozen=True)
class DemoRuntime:
    config: DemoWebConfig
    backend: ConcreteDemoBackend
    guidance: SideMasterGuidanceClient | None


class ConcreteDemoBackend:
    """Read authoritative local projections and expose bounded controls."""

    def __init__(
        self,
        project_root: Path,
        candidate: DemoCandidate | None,
        clients: dict[str, BrokerSimulationRunClient],
        guidance_available: bool,
    ) -> None:
        self._project_root = project_root
        self._candidate = candidate
        self._clients = dict(clients)
        self._guidance_available = guidance_available
        self._projection_store = DemoProjectionStore(
            project_root / ".demo-state" / "projection.json"
        )
        self._state_store = _RuntimeStateStore(
            project_root / ".demo-state" / "web-runtime.json"
        )

    def readiness(self) -> dict[str, Any]:
        blockers: list[dict[str, str]] = []
        if self._candidate is None or not self._candidate.ready:
            blockers.append(
                {"code": "CANDIDATE_NOT_READY", "detail": "Exact candidate is unavailable."}
            )
        for gateway in _GATEWAYS:
            if gateway not in self._clients:
                blockers.append(
                    {
                        "code": f"RUN_{gateway}_UNAVAILABLE",
                        "detail": f"{gateway} isolated run service is unavailable.",
                    }
                )
                continue
            try:
                status = self._clients[gateway].read_status()
            except Exception:
                blockers.append(
                    {
                        "code": f"RUN_{gateway}_UNAVAILABLE",
                        "detail": f"{gateway} isolated run service did not respond.",
                    }
                )
            else:
                if status["state"] in {"blocked", "unavailable", "uncertain"}:
                    blockers.append(
                        {
                            "code": f"RUN_{gateway}_BLOCKED",
                            "detail": f"{gateway} isolated run service is not ready.",
                        }
                    )
        if not self._guidance_available:
            blockers.append(
                {
                    "code": "SIDE_MASTER_UNAVAILABLE",
                    "detail": "Research conversation service is unavailable.",
                }
            )
        return {
            "state": "ready" if not blockers else "blocked",
            "ready": not blockers,
            "candidate_digest": (
                self._candidate.candidate_digest
                if self._candidate is not None
                else _UNAVAILABLE_DIGEST
            ),
            "components": [
                {
                    "name": f"run-{gateway.lower()}",
                    "state": "configured" if gateway in self._clients else "unavailable",
                }
                for gateway in _GATEWAYS
            ]
            + [
                {
                    "name": "side-master",
                    "state": "configured" if self._guidance_available else "unavailable",
                }
            ],
            "blockers": blockers,
        }

    def projection(self) -> dict[str, Any]:
        state = self._state_store.read()
        candidate = self._candidate
        current = state["current"]
        gateways: tuple[GatewayProjectionInput, ...] = ()
        if isinstance(current, dict):
            gateways = tuple(
                self._gateway_projection(gateway)
                for gateway in current["gateways"]
            )
            self._state_store.observe(_payload_digest([asdict(item) for item in gateways]))
            state = self._state_store.read()
            current = state["current"]
        campaign_state = current["state"] if isinstance(current, dict) else "unavailable"
        actions = ["emergency_stop"]
        if campaign_state in {"starting", "active"}:
            actions.insert(0, "pause")
        elif (
            candidate is not None
            and candidate.ready
            and all(gateway in self._clients for gateway in _GATEWAYS)
        ):
            actions.insert(0, "start")
        projection = self._projection_store.publish(
            DemoProjectionInput(
                source_revision=state["revision"],
                updated_at_ms=state["updated_at_ms"],
                candidate=CandidateProjectionInput(
                    candidate_digest=(
                        candidate.candidate_digest if candidate else _UNAVAILABLE_DIGEST
                    ),
                    author_lineage_digest=(
                        candidate.author_lineage_digest if candidate else _UNAVAILABLE_DIGEST
                    ),
                    package_digest=(
                        candidate.package_digest if candidate else _UNAVAILABLE_DIGEST
                    ),
                    readiness=("ready" if candidate and candidate.ready else "unavailable"),
                ),
                campaign_id=(current["campaign_id"] if isinstance(current, dict) else None),
                campaign_digest=(
                    current["campaign_digest"] if isinstance(current, dict) else None
                ),
                campaign_state=campaign_state,
                current_gateways=gateways,
                historical_evidence=(),
                risk_state=(
                    "normal"
                    if gateways
                    and all(
                        gateway.reconciliation_state == "complete"
                        and gateway.unresolved_outcomes == 0
                        for gateway in gateways
                    )
                    else "blocking"
                ),
                permitted_actions=tuple(actions),
            )
        )
        return projection.to_public_dict()

    def start_campaign(self, command: dict[str, Any]) -> dict[str, Any]:
        candidate = self._candidate
        if candidate is None or not candidate.ready:
            raise RuntimeError("CANDIDATE_NOT_READY")
        if command.get("candidate_digest") != candidate.candidate_digest:
            raise RuntimeError("CANDIDATE_IDENTITY_MISMATCH")
        gateways = command.get("gateways")
        if not isinstance(gateways, list) or any(
            gateway not in self._clients for gateway in gateways
        ):
            raise RuntimeError("RUN_SERVICE_UNAVAILABLE")
        idempotency_key = command.get("idempotency_key")
        if not isinstance(idempotency_key, str):
            raise RuntimeError("IDEMPOTENCY_KEY_REQUIRED")
        request_digest = _payload_digest(command)
        retained = self._state_store.idempotent(idempotency_key, request_digest)
        if retained is not None:
            return retained
        current = self._state_store.read()["current"]
        if isinstance(current, dict) and current["state"] in {"starting", "active"}:
            raise RuntimeError("CAMPAIGN_ALREADY_ACTIVE")
        campaign_id = str(uuid4())
        campaign_digest = _payload_digest(
            {
                "candidate_digest": candidate.candidate_digest,
                "gateways": sorted(gateways),
                "campaign_id": campaign_id,
                "calendar_sessions": [item.isoformat() for item in candidate.calendar_sessions],
            }
        )
        receipts: list[dict[str, Any]] = []
        try:
            for gateway in gateways:
                receipt = self._clients[gateway].prepare_campaign(
                    campaign_id,
                    campaign_digest,
                    candidate.candidate_digest,
                    _child_idempotency("prepare", idempotency_key, gateway),
                )
                _require_run_state(receipt, {"prepared"})
                receipts.append(receipt)
            for gateway in gateways:
                receipt = self._clients[gateway].start_campaign(
                    campaign_id,
                    campaign_digest,
                    _child_idempotency("start", idempotency_key, gateway),
                )
                _require_run_state(receipt, {"active"})
                receipts.append(receipt)
            state = "active"
        except Exception:
            containment = []
            for gateway in gateways:
                try:
                    receipt = self._clients[gateway].emergency_stop(
                        _child_idempotency("start-failure-stop", idempotency_key, gateway)
                    )
                    _require_run_state(receipt, {"contained", "stopped"})
                except Exception:
                    receipt = {"gateway": gateway, "state": "unavailable"}
                containment.append(receipt)
            receipts.extend(containment)
            state = (
                "stopped"
                if all(item.get("state") in {"contained", "stopped"} for item in containment)
                else "uncertain"
            )
        result = {
            "campaign_id": campaign_id,
            "campaign_digest": campaign_digest,
            "state": state,
            "candidate_digest": candidate.candidate_digest,
            "gateways": [
                {"gateway": gateway, "state": state} for gateway in gateways
            ],
            "receipt_digest": _payload_digest(receipts),
        }
        self._state_store.record_campaign(
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            result=result,
            campaign={
                "campaign_id": campaign_id,
                "campaign_digest": campaign_digest,
                "candidate_digest": candidate.candidate_digest,
                "gateways": list(gateways),
                "state": state,
            },
        )
        return result

    def pause_campaign(self, campaign_id: str) -> dict[str, Any]:
        try:
            UUID(campaign_id)
        except ValueError as exc:
            raise RuntimeError("CAMPAIGN_ID_INVALID") from exc
        current = self._state_store.read()["current"]
        if not isinstance(current, dict) or current["campaign_id"] != campaign_id:
            raise RuntimeError("CAMPAIGN_NOT_ACTIVE")
        receipts = []
        for gateway in current["gateways"]:
            receipts.append(
                self._clients[gateway].pause_campaign(
                    current["campaign_digest"],
                    f"pause-campaign-{campaign_id}-{gateway.lower()}",
                )
            )
        state = (
            "paused"
            if all(item["state"] in {"contained", "paused"} for item in receipts)
            else "uncertain"
        )
        result = {
            "campaign_id": campaign_id,
            "campaign_digest": current["campaign_digest"],
            "state": state,
            "gateways": receipts,
            "receipt_digest": _payload_digest(receipts),
        }
        self._state_store.update_current(state)
        return result

    def emergency_stop(self) -> dict[str, Any]:
        receipts: list[dict[str, Any]] = []
        for gateway, client in sorted(self._clients.items()):
            try:
                receipt = client.emergency_stop(f"web-emergency-{token_urlsafe(24)}")
            except Exception:
                receipt = {"gateway": gateway, "state": "unavailable"}
            receipts.append(receipt)
        state = (
            "contained"
            if receipts
            and all(item.get("state") in {"contained", "stopped"} for item in receipts)
            else "uncertain"
        )
        if isinstance(self._state_store.read()["current"], dict):
            self._state_store.update_current("stopped" if state == "contained" else state)
        return {
            "state": state,
            "gateways": receipts,
            "receipt_digest": _payload_digest(receipts),
        }

    def evidence(self, campaign_id: str) -> dict[str, Any]:
        path = self._project_root / ".demo-state" / "evidence" / f"{campaign_id}.json"
        if not path.is_file():
            raise KeyError("EVIDENCE_NOT_FOUND")
        value = _load_unique_json(path)
        if not isinstance(value, dict):
            raise ValueError("DEMO_EVIDENCE_INVALID")
        return value

    def active_campaign(self) -> bool:
        try:
            return self.projection()["current"]["campaign_state"] in {"starting", "active"}
        except (KeyError, TypeError, ValueError):
            return False

    def next_safe_boundary_revision(self) -> int:
        revision = self.projection().get("source_revision", 0)
        return revision + 1 if isinstance(revision, int) and revision >= 0 else 1

    def _gateway_projection(self, gateway: str) -> GatewayProjectionInput:
        client = self._clients[gateway]
        try:
            status = client.read_status()
            data = status.get("data") if isinstance(status.get("data"), dict) else {}
            incident = ()
        except Exception:
            status = {"state": "unavailable"}
            data = {}
            incident = ("RUN_SERVICE_UNAVAILABLE",)
        positions = tuple(
            PositionProjectionInput(
                symbol=item["symbol"],
                quantity=_integer(item.get("quantity")),
                available_quantity=_integer(item.get("available_quantity")),
                marked_value_minor=_integer(item.get("marked_value_minor")),
                unrealized_profit_minor=_integer(item.get("unrealized_profit_minor")),
                t_plus_one_locked_quantity=_integer(
                    item.get("t_plus_one_locked_quantity")
                ),
            )
            for item in data.get("positions", [])
            if isinstance(item, dict) and isinstance(item.get("symbol"), str)
        )
        latency = LatencyProjectionInput(0, 0, 0, 0, 0)
        return GatewayProjectionInput(
            gateway=gateway,
            run_digest=client.binding.run_digest,
            state=str(status["state"]),
            connection_state=str(data.get("connection_state", "disconnected")),
            reconciliation_state=str(data.get("reconciliation_state", "blocked")),
            net_profit_minor=_integer(data.get("net_profit_minor")),
            realized_profit_minor=_integer(data.get("realized_profit_minor")),
            unrealized_profit_minor=_integer(data.get("unrealized_profit_minor")),
            fees_minor=_integer(data.get("fees_minor")),
            return_bps=_integer(data.get("return_bps")),
            max_drawdown_bps=_integer(data.get("max_drawdown_bps")),
            fill_count=_integer(data.get("fill_count")),
            positions=positions,
            gross_exposure_minor=_integer(data.get("gross_exposure_minor")),
            risk_headroom_minor=_integer(data.get("risk_headroom_minor")),
            local_latency_us=latency,
            broker_latency_us=latency,
            incidents=tuple(data.get("incidents", ())) + incident,
            residual_exposure_minor=_integer(data.get("residual_exposure_minor")),
            working_order_count=_integer(data.get("working_order_count")),
            unresolved_outcomes=_integer(data.get("unresolved_outcomes")),
            permitted_next_action=str(data.get("permitted_next_action", "none")),
        )


def build_demo_runtime(project_root: str | Path, *, host: str, port: int) -> DemoRuntime:
    root = Path(project_root).resolve(strict=False)
    if host != "127.0.0.1" or not 1 <= port <= 65_535:
        raise ValueError("DEMO_LOOPBACK_BIND_REQUIRED")
    state_root = root / ".demo-state"
    candidate = _load_candidate(state_root / "ready-candidate.json")
    clients: dict[str, BrokerSimulationRunClient] = {}
    for gateway in _GATEWAYS:
        descriptor_path = state_root / "runs" / gateway / "endpoint.json"
        token_path = root / ".demo-secrets" / f"run-{gateway.lower()}-ipc-token"
        if not descriptor_path.exists() and not token_path.exists():
            continue
        descriptor = _load_endpoint(descriptor_path, gateway=gateway)
        token = _load_token(token_path)
        clients[gateway] = BrokerSimulationRunClient(
            RunClientBinding(
                gateway=gateway,
                run_digest=descriptor["run_digest"],
                endpoint=f"tcp://{descriptor['address']}",
            ),
            LengthPrefixedJsonTransport(token),
        )

    guidance_endpoint = root / ".agent-state" / "guidance-endpoint.json"
    guidance_token_path = root / ".agent-state" / "demo-guidance-ipc-token"
    guidance_descriptor: dict[str, Any] | None = None
    guidance_transport: LengthPrefixedJsonTransport | None = None
    if guidance_endpoint.exists() or guidance_token_path.exists():
        guidance_descriptor = _load_endpoint(guidance_endpoint, gateway=None)
        guidance_transport = LengthPrefixedJsonTransport(
            _load_token(guidance_token_path), timeout_seconds=120
        )

    backend = ConcreteDemoBackend(
        root,
        candidate,
        clients,
        guidance_available=guidance_descriptor is not None,
    )
    guidance = None
    if guidance_descriptor is not None and guidance_transport is not None:
        guidance = SideMasterGuidanceClient(
            GuidanceClientBinding(
                endpoint=f"tcp://{guidance_descriptor['address']}",
                operator_identity_digest=_load_operator_digest(
                    root / ".demo-secrets" / "operator.json"
                ),
            ),
            guidance_transport,
            active_campaign=backend.active_campaign,
            next_safe_boundary_revision=backend.next_safe_boundary_revision,
        )

    config = DemoWebConfig(
        bind_host=host,
        port=port,
        allowed_origin=f"http://{host}:{port}",
        session_token=token_urlsafe(48),
        csrf_token=token_urlsafe(48),
    )
    return DemoRuntime(config=config, backend=backend, guidance=guidance)


def _load_candidate(path: Path) -> DemoCandidate | None:
    if not path.exists():
        return None
    value = _load_unique_json(path)
    required = {
        "contract_version",
        "ready",
        "candidate_digest",
        "author_lineage_digest",
        "package_digest",
        "configuration_digest",
        "policy_digest",
        "symbols",
        "calendar_sessions",
        "lifecycle_revision",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("DEMO_CANDIDATE_INVALID")
    digests = [
        value["candidate_digest"],
        value["author_lineage_digest"],
        value["package_digest"],
        value["configuration_digest"],
        value["policy_digest"],
    ]
    try:
        sessions = tuple(date.fromisoformat(item) for item in value["calendar_sessions"])
    except (TypeError, ValueError) as exc:
        raise ValueError("DEMO_CANDIDATE_INVALID") from exc
    if (
        value["contract_version"] != 1
        or not isinstance(value["ready"], bool)
        or any(not isinstance(item, str) or _DIGEST.fullmatch(item) is None for item in digests)
        or not isinstance(value["symbols"], list)
        or not 1 <= len(value["symbols"]) <= 20
        or len(set(value["symbols"])) != len(value["symbols"])
        or any(
            not isinstance(item, str) or _SYMBOL.fullmatch(item) is None
            for item in value["symbols"]
        )
        or len(sessions) != 5
        or len(set(sessions)) != 5
        or tuple(sorted(sessions)) != sessions
        or not isinstance(value["lifecycle_revision"], int)
        or isinstance(value["lifecycle_revision"], bool)
        or value["lifecycle_revision"] <= 0
    ):
        raise ValueError("DEMO_CANDIDATE_INVALID")
    return DemoCandidate(
        ready=value["ready"],
        candidate_digest=value["candidate_digest"],
        author_lineage_digest=value["author_lineage_digest"],
        package_digest=value["package_digest"],
        configuration_digest=value["configuration_digest"],
        policy_digest=value["policy_digest"],
        symbols=tuple(value["symbols"]),
        calendar_sessions=sessions,
        lifecycle_revision=value["lifecycle_revision"],
    )


class _RuntimeStateStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = RLock()
        with self._lock:
            if not self._path.exists():
                self._persist(
                    {
                        "contract_version": 1,
                        "revision": 1,
                        "updated_at_ms": max(1, time_ns() // 1_000_000),
                        "current": None,
                        "idempotency": {},
                        "observation_digest": None,
                    }
                )

    def read(self) -> dict[str, Any]:
        with self._lock:
            value = _load_unique_json(self._path)
            if not isinstance(value, dict):
                raise ValueError("DEMO_RUNTIME_STATE_INVALID")
            return json.loads(json.dumps(value))

    def idempotent(
        self, idempotency_key: str, request_digest: str
    ) -> dict[str, Any] | None:
        state = self.read()
        retained = state["idempotency"].get(idempotency_key)
        if retained is None:
            return None
        if retained["request_digest"] != request_digest:
            raise RuntimeError("IDEMPOTENCY_COLLISION")
        return retained["result"]

    def record_campaign(
        self,
        *,
        idempotency_key: str,
        request_digest: str,
        result: dict[str, Any],
        campaign: dict[str, Any],
    ) -> None:
        with self._lock:
            state = self.read()
            retained = state["idempotency"].get(idempotency_key)
            if retained is not None:
                if retained["request_digest"] != request_digest:
                    raise RuntimeError("IDEMPOTENCY_COLLISION")
                return
            state["revision"] += 1
            state["updated_at_ms"] = max(1, time_ns() // 1_000_000)
            state["current"] = campaign
            state["idempotency"][idempotency_key] = {
                "request_digest": request_digest,
                "result": result,
            }
            self._persist(state)

    def update_current(self, state_name: str) -> None:
        with self._lock:
            state = self.read()
            current = state["current"]
            if not isinstance(current, dict):
                raise RuntimeError("CAMPAIGN_NOT_ACTIVE")
            state["revision"] += 1
            state["updated_at_ms"] = max(1, time_ns() // 1_000_000)
            current["state"] = state_name
            self._persist(state)

    def observe(self, observation_digest: str) -> None:
        with self._lock:
            state = self.read()
            if state.get("observation_digest") == observation_digest:
                return
            state["revision"] += 1
            state["updated_at_ms"] = max(1, time_ns() // 1_000_000)
            state["observation_digest"] = observation_digest
            self._persist(state)

    def _persist(self, value: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        temporary = self._path.with_name(f".{self._path.name}.tmp")
        with temporary.open("wb") as handle:
            handle.write(encoded)
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self._path)


def _integer(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _payload_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{sha256(encoded).hexdigest()}"


def _child_idempotency(kind: str, key: str, gateway: str) -> str:
    return f"{kind}-{sha256(f'{key}:{gateway}'.encode()).hexdigest()}"


def _require_run_state(receipt: dict[str, Any], allowed: set[str]) -> None:
    if receipt.get("state") not in allowed:
        raise RuntimeError("RUN_OPERATION_BLOCKED")


def _load_endpoint(path: Path, *, gateway: str | None) -> dict[str, Any]:
    value = _load_unique_json(path)
    required = {"contract_version", "transport", "address"}
    if gateway is not None:
        required |= {"gateway", "run_digest"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("DEMO_ENDPOINT_INVALID")
    address = value.get("address")
    if (
        value.get("contract_version") != 1
        or value.get("transport") != "tcp-loopback"
        or not isinstance(address, str)
    ):
        raise ValueError("DEMO_ENDPOINT_INVALID")
    try:
        host, port_text = address.rsplit(":", 1)
        endpoint_port = int(port_text)
    except (ValueError, AttributeError) as exc:
        raise ValueError("DEMO_ENDPOINT_INVALID") from exc
    if host != "127.0.0.1" or not 1 <= endpoint_port <= 65_535:
        raise ValueError("DEMO_ENDPOINT_LOOPBACK_REQUIRED")
    if gateway is not None and (
        value.get("gateway") != gateway
        or not isinstance(value.get("run_digest"), str)
        or _DIGEST.fullmatch(value["run_digest"]) is None
    ):
        raise ValueError("DEMO_ENDPOINT_INVALID")
    return value


def _load_token(path: Path) -> str:
    try:
        token = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError("DEMO_IPC_TOKEN_UNAVAILABLE") from exc
    if not 24 <= len(token) <= 512:
        raise ValueError("DEMO_IPC_TOKEN_INVALID")
    return token


def _load_operator_digest(path: Path) -> str:
    value = _load_unique_json(path)
    if (
        not isinstance(value, dict)
        or set(value) != {"contract_version", "operator_identity_digest"}
        or value.get("contract_version") != 1
        or not isinstance(value.get("operator_identity_digest"), str)
        or _DIGEST.fullmatch(value["operator_identity_digest"]) is None
    ):
        raise ValueError("DEMO_OPERATOR_IDENTITY_INVALID")
    return value["operator_identity_digest"]


def _load_unique_json(path: Path) -> Any:
    try:
        return json.loads(path.read_bytes(), object_pairs_hook=_unique_object)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("DEMO_STATE_INVALID") from exc


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value
