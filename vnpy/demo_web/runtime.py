"""Concrete fail-closed composition for the loopback investor demo."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
import re
from secrets import token_urlsafe
from time import time_ns
from typing import Any

from .app import DemoWebConfig
from .guidance import GuidanceClientBinding, SideMasterGuidanceClient
from .projection import CandidateProjectionInput, DemoProjectionInput, DemoProjectionStore
from .run_clients import BrokerSimulationRunClient, RunClientBinding
from .transport import LengthPrefixedJsonTransport


_DIGEST = re.compile(r"^(?:sha256|blake3):[0-9a-f]{64}$")
_SYMBOL = re.compile(r"^[0-9]{6}\.(?:SSE|SZSE|BSE)$")
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
        retained = self._projection_store.current()
        if retained is not None:
            return retained.to_public_dict()
        candidate = self._candidate
        projection = self._projection_store.publish(
            DemoProjectionInput(
                source_revision=1,
                updated_at_ms=max(1, time_ns() // 1_000_000),
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
                campaign_id=None,
                campaign_digest=None,
                campaign_state="unavailable",
                current_gateways=(),
                historical_evidence=(),
                risk_state="blocking",
                permitted_actions=("emergency_stop",),
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
        return {
            "state": "blocked",
            "candidate_digest": candidate.candidate_digest,
            "reason_code": "RUN_CAMPAIGN_COMPOSITION_PENDING",
        }

    def pause_campaign(self, campaign_id: str) -> dict[str, Any]:
        raise RuntimeError(f"CAMPAIGN_NOT_ACTIVE:{campaign_id}")

    def emergency_stop(self) -> dict[str, Any]:
        receipts: list[dict[str, Any]] = []
        for gateway, client in sorted(self._clients.items()):
            try:
                receipt = client.emergency_stop(f"web-emergency-{token_urlsafe(24)}")
            except Exception:
                receipt = {"gateway": gateway, "state": "unavailable"}
            receipts.append(receipt)
        return {
            "state": (
                "contained"
                if receipts
                and all(
                    item.get("state") not in {"unavailable", "uncertain"}
                    for item in receipts
                )
                else "uncertain"
            ),
            "gateways": receipts,
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
