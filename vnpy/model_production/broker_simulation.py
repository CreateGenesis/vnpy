"""Durable broker-simulation identities and campaign state owned by vn.py."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from threading import RLock
from typing import Any


_GATEWAYS = frozenset({"XTP", "TORA"})
_TERMINAL_CAMPAIGN_STATES = frozenset({"paused", "invalid", "completed", "ready", "stopped"})
_SYMBOL = re.compile(r"^[0-9]{6}\.(SH|SZ|BJ)$")


def _digest(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
    return f"sha256:{sha256(payload).hexdigest()}"


def _valid_fingerprint(value: str) -> bool:
    try:
        algorithm, encoded = value.split(":", 1)
    except ValueError:
        return False
    return algorithm in {"sha256", "blake3"} and len(encoded) == 64 and all(
        character in "0123456789abcdefABCDEF" for character in encoded
    )


@dataclass(frozen=True)
class GatewayBinding:
    """One immutable simulation gateway/account/process binding."""

    binding_digest: str
    gateway: str
    environment: str
    server_fingerprint: str
    account_fingerprint: str
    credential_ref: str
    process_identity: str
    rpc_endpoint: str
    state_store_path: str
    created_at_ms: int

    @classmethod
    def create(
        cls,
        *,
        gateway: str,
        environment: str,
        server_fingerprint: str,
        account_fingerprint: str,
        credential_ref: str,
        process_identity: str,
        rpc_endpoint: str,
        state_store_path: str,
        created_at_ms: int,
        allowed_server_fingerprints: frozenset[str],
        allowed_account_fingerprints: frozenset[str],
        production_server_fingerprints: frozenset[str] = frozenset(),
        production_account_fingerprints: frozenset[str] = frozenset(),
    ) -> GatewayBinding:
        if gateway not in _GATEWAYS:
            raise ValueError("GATEWAY_UNSUPPORTED")
        if environment != "broker_simulation":
            raise ValueError("SIMULATION_ENVIRONMENT_REQUIRED")
        if server_fingerprint in production_server_fingerprints:
            raise ValueError("PRODUCTION_SERVER_DENIED")
        if account_fingerprint in production_account_fingerprints:
            raise ValueError("PRODUCTION_ACCOUNT_DENIED")
        if server_fingerprint not in allowed_server_fingerprints:
            raise ValueError("SERVER_NOT_ALLOWLISTED")
        if account_fingerprint not in allowed_account_fingerprints:
            raise ValueError("ACCOUNT_NOT_ALLOWLISTED")
        if not _valid_fingerprint(server_fingerprint) or not _valid_fingerprint(account_fingerprint):
            raise ValueError("BINDING_FINGERPRINT_INVALID")
        try:
            host, port = rpc_endpoint.rsplit(":", 1)
        except ValueError as exc:
            raise ValueError("LOOPBACK_RPC_REQUIRED") from exc
        if host != "127.0.0.1" or not port.isdigit() or not 1 <= int(port) <= 65_535:
            raise ValueError("LOOPBACK_RPC_REQUIRED")
        if (
            not credential_ref.strip()
            or not process_identity.strip()
            or not state_store_path.strip()
            or created_at_ms <= 0
        ):
            raise ValueError("BINDING_IDENTITY_INVALID")
        identity = {
            "gateway": gateway,
            "environment": environment,
            "server_fingerprint": server_fingerprint,
            "account_fingerprint": account_fingerprint,
            "credential_ref_digest": _digest(credential_ref),
            "process_identity": process_identity,
            "rpc_endpoint": rpc_endpoint,
            "state_store_path": state_store_path,
            "created_at_ms": created_at_ms,
        }
        return cls(
            binding_digest=_digest(identity),
            gateway=gateway,
            environment=environment,
            server_fingerprint=server_fingerprint,
            account_fingerprint=account_fingerprint,
            credential_ref=credential_ref,
            process_identity=process_identity,
            rpc_endpoint=rpc_endpoint,
            state_store_path=state_store_path,
            created_at_ms=created_at_ms,
        )

    def redacted(self) -> dict[str, str | int]:
        """Return a projection without credentials, raw account identity, or local state paths."""

        return {
            "binding_digest": self.binding_digest,
            "gateway": self.gateway,
            "environment": self.environment,
            "server_fingerprint": self.server_fingerprint,
            "account_fingerprint": self.account_fingerprint,
            "process_identity_digest": _digest(self.process_identity),
            "rpc_scope": "loopback",
            "created_at_ms": self.created_at_ms,
        }


@dataclass(frozen=True)
class BrokerSimulationCampaign:
    campaign_id: str
    candidate_digest: str
    package_digest: str
    configuration_digest: str
    policy_digest: str
    symbol_set: tuple[str, ...]
    calendar_sessions: tuple[str, ...]
    operator_identity_digest: str
    state: str
    revision: int
    evidence_digest: str | None
    created_at_ms: int
    updated_at_ms: int


@dataclass(frozen=True)
class BrokerSimulationRun:
    run_id: str
    campaign_id: str
    candidate_digest: str
    package_digest: str
    gateway_binding_digest: str
    gateway: str
    lifecycle_revision: int
    stage: str
    state: str
    session_progress: dict[str, Any]
    risk_budget: dict[str, Any]
    ledger_revision: int
    reconciliation_revision: int
    created_at_ms: int
    updated_at_ms: int


class BrokerSimulationAuthority:
    """Persist campaign and run transitions in the authoritative vn.py database."""

    def __init__(self, database: str | Path) -> None:
        self._database = Path(database)
        if str(database) != ":memory:":
            self._database.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(database), check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        migration = Path(__file__).with_name("migrations") / "0002_broker_simulation.sql"
        self._connection.executescript(migration.read_text(encoding="utf-8"))
        self._lock = RLock()

    def create_campaign(
        self,
        *,
        campaign_id: str,
        candidate_digest: str,
        package_digest: str,
        configuration_digest: str,
        policy_digest: str,
        symbol_set: tuple[str, ...],
        calendar_sessions: tuple[str, ...],
        operator_identity_digest: str,
        bindings: tuple[GatewayBinding, ...],
        lifecycle_revision: int,
        now_ms: int,
    ) -> BrokerSimulationCampaign:
        symbols = tuple(symbol_set)
        sessions = tuple(calendar_sessions)
        if not campaign_id.strip() or now_ms <= 0 or lifecycle_revision <= 0:
            raise ValueError("CAMPAIGN_IDENTITY_INVALID")
        if not 1 <= len(symbols) <= 20 or len(set(symbols)) != len(symbols):
            raise ValueError("SYMBOL_SET_INVALID")
        if any(_SYMBOL.fullmatch(symbol) is None for symbol in symbols):
            raise ValueError("SYMBOL_SET_INVALID")
        if len(sessions) != 5 or len(set(sessions)) != 5 or tuple(sorted(sessions)) != sessions:
            raise ValueError("FIVE_SESSION_WINDOW_REQUIRED")
        if not 1 <= len(bindings) <= 2:
            raise ValueError("GATEWAY_BINDING_COUNT_INVALID")
        if len({binding.gateway for binding in bindings}) != len(bindings):
            raise ValueError("ONE_RUN_PER_GATEWAY")
        if any(binding.environment != "broker_simulation" for binding in bindings):
            raise ValueError("SIMULATION_ENVIRONMENT_REQUIRED")

        with self._lock:
            existing = self.campaign(campaign_id, required=False)
            if existing is not None:
                existing_runs = self.runs(campaign_id)
                requested = (
                    candidate_digest,
                    package_digest,
                    configuration_digest,
                    policy_digest,
                    symbols,
                    sessions,
                    operator_identity_digest,
                    tuple(sorted(binding.binding_digest for binding in bindings)),
                    lifecycle_revision,
                )
                retained = (
                    existing.candidate_digest,
                    existing.package_digest,
                    existing.configuration_digest,
                    existing.policy_digest,
                    existing.symbol_set,
                    existing.calendar_sessions,
                    existing.operator_identity_digest,
                    tuple(sorted(run.gateway_binding_digest for run in existing_runs)),
                    existing_runs[0].lifecycle_revision,
                )
                if requested != retained:
                    raise RuntimeError("CAMPAIGN_IDENTITY_DRIFT")
                return existing

            session_progress = json.dumps(
                {"expected": sessions, "opened": [], "completed": [], "invalid": []},
                separators=(",", ":"),
                sort_keys=True,
            )
            risk_budget = json.dumps(
                {
                    "gross_exposure_bps": 1_000,
                    "symbol_exposure_bps": 100,
                    "order_notional_bps": 25,
                    "operations_per_second": 5,
                    "operations_per_session": 1_000,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            with self._connection:
                for binding in bindings:
                    self._insert_binding(binding)
                self._connection.execute(
                    """INSERT INTO broker_simulation_campaigns(
                        campaign_id,candidate_digest,package_digest,configuration_digest,
                        policy_digest,symbol_set_json,calendar_sessions_json,
                        operator_identity_digest,state,revision,evidence_digest,created_at_ms,updated_at_ms
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        campaign_id,
                        candidate_digest,
                        package_digest,
                        configuration_digest,
                        policy_digest,
                        json.dumps(symbols, separators=(",", ":")),
                        json.dumps(sessions, separators=(",", ":")),
                        operator_identity_digest,
                        "prepared",
                        1,
                        None,
                        now_ms,
                        now_ms,
                    ),
                )
                for binding in bindings:
                    self._connection.execute(
                        """INSERT INTO broker_simulation_runs(
                            run_id,campaign_id,gateway_binding_digest,lifecycle_revision,stage,
                            session_progress_json,risk_budget_json,ledger_revision,
                            reconciliation_revision,state,created_at_ms,updated_at_ms
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            f"{campaign_id}:{binding.gateway.lower()}",
                            campaign_id,
                            binding.binding_digest,
                            lifecycle_revision,
                            "broker_simulation",
                            session_progress,
                            risk_budget,
                            0,
                            0,
                            "prepared",
                            now_ms,
                            now_ms,
                        ),
                    )
            return self.campaign(campaign_id)

    def campaign(
        self, campaign_id: str, *, required: bool = True
    ) -> BrokerSimulationCampaign | None:
        row = self._connection.execute(
            """SELECT campaign_id,candidate_digest,package_digest,configuration_digest,
                policy_digest,symbol_set_json,calendar_sessions_json,operator_identity_digest,
                state,revision,evidence_digest,created_at_ms,updated_at_ms
                FROM broker_simulation_campaigns WHERE campaign_id=?""",
            (campaign_id,),
        ).fetchone()
        if row is None:
            if required:
                raise KeyError("CAMPAIGN_NOT_FOUND")
            return None
        return BrokerSimulationCampaign(
            campaign_id=row[0],
            candidate_digest=row[1],
            package_digest=row[2],
            configuration_digest=row[3],
            policy_digest=row[4],
            symbol_set=tuple(json.loads(row[5])),
            calendar_sessions=tuple(json.loads(row[6])),
            operator_identity_digest=row[7],
            state=row[8],
            revision=row[9],
            evidence_digest=row[10],
            created_at_ms=row[11],
            updated_at_ms=row[12],
        )

    def runs(self, campaign_id: str) -> tuple[BrokerSimulationRun, ...]:
        rows = self._connection.execute(
            """SELECT r.run_id,r.campaign_id,c.candidate_digest,c.package_digest,
                r.gateway_binding_digest,b.gateway,r.lifecycle_revision,r.stage,r.state,
                r.session_progress_json,r.risk_budget_json,r.ledger_revision,
                r.reconciliation_revision,r.created_at_ms,r.updated_at_ms
                FROM broker_simulation_runs r
                JOIN broker_simulation_campaigns c ON c.campaign_id=r.campaign_id
                JOIN broker_simulation_gateway_bindings b
                  ON b.binding_digest=r.gateway_binding_digest
                WHERE r.campaign_id=? ORDER BY b.gateway""",
            (campaign_id,),
        ).fetchall()
        return tuple(
            BrokerSimulationRun(
                run_id=row[0],
                campaign_id=row[1],
                candidate_digest=row[2],
                package_digest=row[3],
                gateway_binding_digest=row[4],
                gateway=row[5],
                lifecycle_revision=row[6],
                stage=row[7],
                state=row[8],
                session_progress=json.loads(row[9]),
                risk_budget=json.loads(row[10]),
                ledger_revision=row[11],
                reconciliation_revision=row[12],
                created_at_ms=row[13],
                updated_at_ms=row[14],
            )
            for row in rows
        )

    def start_campaign(self, campaign_id: str, *, now_ms: int) -> BrokerSimulationCampaign:
        with self._lock, self._connection:
            campaign = self.campaign(campaign_id)
            assert campaign is not None
            if campaign.state == "active":
                return campaign
            if campaign.state in _TERMINAL_CAMPAIGN_STATES:
                raise RuntimeError("CAMPAIGN_TERMINAL")
            if campaign.state != "prepared":
                raise RuntimeError("CAMPAIGN_TRANSITION_INVALID")
            self._connection.execute(
                "UPDATE broker_simulation_campaigns SET state='active',revision=revision+1,updated_at_ms=? WHERE campaign_id=?",
                (now_ms, campaign_id),
            )
            self._connection.execute(
                "UPDATE broker_simulation_runs SET state='active',updated_at_ms=? WHERE campaign_id=?",
                (now_ms, campaign_id),
            )
        result = self.campaign(campaign_id)
        assert result is not None
        return result

    def pause_campaign(self, campaign_id: str, *, now_ms: int) -> BrokerSimulationCampaign:
        with self._lock, self._connection:
            campaign = self.campaign(campaign_id)
            assert campaign is not None
            if campaign.state == "paused":
                return campaign
            if campaign.state in _TERMINAL_CAMPAIGN_STATES:
                raise RuntimeError("CAMPAIGN_TERMINAL")
            if campaign.state not in {"prepared", "active"}:
                raise RuntimeError("CAMPAIGN_TRANSITION_INVALID")
            self._connection.execute(
                "UPDATE broker_simulation_campaigns SET state='paused',revision=revision+1,updated_at_ms=? WHERE campaign_id=?",
                (now_ms, campaign_id),
            )
            self._connection.execute(
                "UPDATE broker_simulation_runs SET state='invalid',updated_at_ms=? WHERE campaign_id=?",
                (now_ms, campaign_id),
            )
        result = self.campaign(campaign_id)
        assert result is not None
        return result

    def require_active_run(
        self, run_id: str, gateway_binding_digest: str
    ) -> BrokerSimulationRun:
        row = self._connection.execute(
            "SELECT campaign_id FROM broker_simulation_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise KeyError("RUN_NOT_FOUND")
        run = next(run for run in self.runs(row[0]) if run.run_id == run_id)
        campaign = self.campaign(run.campaign_id)
        assert campaign is not None
        if (
            run.state != "active"
            or campaign.state != "active"
            or run.gateway_binding_digest != gateway_binding_digest
        ):
            raise PermissionError("RUN_NOT_ACTIVE_OR_BOUND")
        return run

    def _insert_binding(self, binding: GatewayBinding) -> None:
        values = (
            binding.binding_digest,
            binding.gateway,
            binding.environment,
            binding.server_fingerprint,
            binding.account_fingerprint,
            binding.credential_ref,
            binding.process_identity,
            binding.rpc_endpoint,
            binding.state_store_path,
            binding.created_at_ms,
        )
        self._connection.execute(
            """INSERT OR IGNORE INTO broker_simulation_gateway_bindings(
                binding_digest,gateway,environment,server_fingerprint,account_fingerprint,
                credential_ref,process_identity,rpc_endpoint,state_store_path,created_at_ms
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            values,
        )
        retained = self._connection.execute(
            """SELECT binding_digest,gateway,environment,server_fingerprint,account_fingerprint,
                credential_ref,process_identity,rpc_endpoint,state_store_path,created_at_ms
                FROM broker_simulation_gateway_bindings WHERE binding_digest=?""",
            (binding.binding_digest,),
        ).fetchone()
        if retained != values:
            raise RuntimeError("GATEWAY_BINDING_IDENTITY_DRIFT")
