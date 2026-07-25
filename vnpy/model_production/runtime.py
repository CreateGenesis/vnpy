"""vn.py-owned two-phase model activation authority."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from contextlib import closing
import hashlib
import hmac
import json
from pathlib import Path
import re
import sqlite3
from threading import RLock

from .contracts import canonical_json_v1


_DIGEST = re.compile(r"^(?:blake3|sha256):[0-9a-f]{64}$")
_SYMBOL = re.compile(r"^[0-9]{6}\.(?:SH|SZ|BJ)$")
_STAGES = frozenset({"simulation", "paper", "shadow", "gray", "production"})


@dataclass(frozen=True)
class LoadPreparation:
    preparation_id: str
    package_digest: str
    expected_old_package_digest: str
    configuration_digest: str
    stage: str
    symbols: tuple[str, ...]
    feature_schema_digest: str
    context_schema_digest: str
    policy_digest: str
    evidence_bundle_digest: str
    ready_token: str
    expected_old_revision: int
    cutover_input_sequence: int
    created_at_ms: int
    expires_at_ms: int


@dataclass(frozen=True)
class ActivationCommit:
    contract_version: int
    entity_type: str
    commit_id: str
    preparation_id: str
    package_digest: str
    expected_old_package_digest: str
    configuration_digest: str
    stage: str
    symbols: tuple[str, ...]
    feature_schema_digest: str
    context_schema_digest: str
    policy_digest: str
    evidence_bundle_digest: str
    ready_token: str
    expected_old_revision: int
    applied_revision: int
    activation_epoch: int
    cutover_input_sequence: int
    created_at_ms: int
    expires_at_ms: int
    producer_identity: str
    commit_digest: str
    signature: str

    def as_contract(self) -> dict[str, object]:
        value = asdict(self)
        value["symbols"] = list(self.symbols)
        return value


@dataclass(frozen=True)
class ActivationAck:
    commit_id: str
    old_package_digest: str
    new_package_digest: str
    applied_revision: int
    activation_epoch: int
    cutover_sequence: int


@dataclass(frozen=True)
class RuntimeAuthoritySnapshot:
    revision: int
    active_package_digest: str
    pending_preparation_id: str | None
    last_failure_code: str | None
    authority: str = "vnpy:model-lifecycle"


class RuntimeAuthorityError(RuntimeError):
    """Fail-closed activation authority error with a stable reason code."""


class LifecycleRuntimeAuthority:
    """Persist preparation, issue signed commits, and apply only exact ACKs."""

    def __init__(self, database: str | Path, activation_key: bytes) -> None:
        if len(activation_key) < 32:
            raise ValueError("activation key must contain at least 32 bytes")
        self._database = str(database)
        self._activation_key = bytes(activation_key)
        self._lock = RLock()
        self._initialize()

    def initialize_active(self, package_digest: str, revision: int) -> None:
        _require_digest(package_digest, "package_digest")
        if revision < 0:
            raise ValueError("revision must be nonnegative")
        with self._lock, closing(self._connect()) as connection:
            current = connection.execute(
                "SELECT revision FROM runtime_authority WHERE singleton=1"
            ).fetchone()
            if current is not None and current[0] != 0:
                raise RuntimeAuthorityError("AUTHORITY_ALREADY_INITIALIZED")
            connection.execute(
                "UPDATE runtime_authority SET revision=?,active_package_digest=? WHERE singleton=1",
                (revision, package_digest),
            )
            connection.commit()

    def accept_preparation(self, request: LoadPreparation, now_ms: int) -> LoadPreparation:
        _validate_preparation(request, now_ms)
        payload = _canonical_record(request)
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT revision,active_package_digest,pending_preparation_id FROM runtime_authority WHERE singleton=1"
            ).fetchone()
            if row is None:
                raise RuntimeAuthorityError("AUTHORITY_UNAVAILABLE")
            revision, active_package, pending = row
            if revision != request.expected_old_revision or active_package != request.expected_old_package_digest:
                raise RuntimeAuthorityError("STALE_ACTIVE_GENERATION")
            existing = connection.execute(
                "SELECT preparation_json FROM runtime_preparations WHERE preparation_id=?",
                (request.preparation_id,),
            ).fetchone()
            if existing is not None:
                if existing[0] != payload:
                    raise RuntimeAuthorityError("PREPARATION_IDENTITY_DRIFT")
                connection.commit()
                return request
            if pending not in (None, request.preparation_id):
                raise RuntimeAuthorityError("ANOTHER_PREPARATION_PENDING")
            connection.execute(
                "INSERT INTO runtime_preparations(preparation_id,preparation_json,state,created_at_ms,expires_at_ms) VALUES(?,?,'accepted',?,?)",
                (request.preparation_id, payload, request.created_at_ms, request.expires_at_ms),
            )
            connection.execute(
                "UPDATE runtime_authority SET pending_preparation_id=?,last_failure_code=NULL WHERE singleton=1",
                (request.preparation_id,),
            )
            connection.commit()
        return request

    def issue_activation_commit(self, preparation_id: str, now_ms: int) -> ActivationCommit:
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT commit_json FROM runtime_commits WHERE preparation_id=?",
                (preparation_id,),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return _decode_commit(existing[0])
            row = connection.execute(
                "SELECT preparation_json,state,expires_at_ms FROM runtime_preparations WHERE preparation_id=?",
                (preparation_id,),
            ).fetchone()
            if row is None or row[1] != "accepted":
                raise RuntimeAuthorityError("PREPARATION_NOT_ACCEPTED")
            if now_ms >= row[2]:
                connection.execute(
                    "UPDATE runtime_preparations SET state='expired' WHERE preparation_id=?",
                    (preparation_id,),
                )
                connection.commit()
                raise RuntimeAuthorityError("PREPARATION_EXPIRED")
            request = _decode_preparation(row[0])
            if now_ms < request.created_at_ms:
                raise RuntimeAuthorityError("INVALID_ACTIVATION_TIME")
            unsigned: dict[str, object] = {
                "activation_epoch": request.expected_old_revision + 1,
                "applied_revision": request.expected_old_revision + 1,
                "commit_id": f"activation-{request.preparation_id}",
                "configuration_digest": request.configuration_digest,
                "context_schema_digest": request.context_schema_digest,
                "contract_version": 2,
                "created_at_ms": now_ms,
                "cutover_input_sequence": request.cutover_input_sequence,
                "entity_type": "model_activation_commit",
                "evidence_bundle_digest": request.evidence_bundle_digest,
                "expected_old_package_digest": request.expected_old_package_digest,
                "expected_old_revision": request.expected_old_revision,
                "expires_at_ms": request.expires_at_ms,
                "feature_schema_digest": request.feature_schema_digest,
                "package_digest": request.package_digest,
                "policy_digest": request.policy_digest,
                "preparation_id": request.preparation_id,
                "producer_identity": "vnpy:model-lifecycle",
                "ready_token": request.ready_token,
                "stage": request.stage,
                "symbols": list(request.symbols),
            }
            commit_digest = "sha256:" + hashlib.sha256(canonical_json_v1(unsigned)).hexdigest()
            signature = hmac.new(
                self._activation_key, commit_digest.encode("ascii"), hashlib.sha512
            ).hexdigest()
            commit_fields = dict(unsigned)
            commit_fields["symbols"] = tuple(request.symbols)
            commit = ActivationCommit(
                **commit_fields, commit_digest=commit_digest, signature=signature  # type: ignore[arg-type]
            )
            connection.execute(
                "INSERT INTO runtime_commits(commit_id,preparation_id,commit_json,state,created_at_ms,expires_at_ms) VALUES(?,?,?,'issued',?,?)",
                (
                    commit.commit_id,
                    preparation_id,
                    _canonical_record(commit),
                    now_ms,
                    commit.expires_at_ms,
                ),
            )
            connection.execute(
                "UPDATE runtime_preparations SET state='activation_pending' WHERE preparation_id=?",
                (preparation_id,),
            )
            connection.commit()
            return commit

    def acknowledge(self, commit_id: str, ack: ActivationAck, now_ms: int) -> RuntimeAuthoritySnapshot:
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT commit_json,state,expires_at_ms FROM runtime_commits WHERE commit_id=?",
                (commit_id,),
            ).fetchone()
            if row is None:
                raise RuntimeAuthorityError("COMMIT_NOT_FOUND")
            commit = _decode_commit(row[0])
            if row[1] == "applied":
                connection.commit()
                return self.snapshot()
            if now_ms >= row[2]:
                raise RuntimeAuthorityError("COMMIT_EXPIRED")
            if (
                ack.commit_id != commit.commit_id
                or ack.old_package_digest != commit.expected_old_package_digest
                or ack.new_package_digest != commit.package_digest
                or ack.applied_revision != commit.applied_revision
                or ack.activation_epoch != commit.activation_epoch
                or ack.cutover_sequence != commit.cutover_input_sequence
            ):
                raise RuntimeAuthorityError("ACTIVATION_ACK_MISMATCH")
            updated = connection.execute(
                "UPDATE runtime_authority SET revision=?,active_package_digest=?,pending_preparation_id=NULL,last_failure_code=NULL WHERE singleton=1 AND revision=? AND active_package_digest=?",
                (
                    commit.applied_revision,
                    commit.package_digest,
                    commit.expected_old_revision,
                    commit.expected_old_package_digest,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeAuthorityError("ACTIVATION_CAS_FAILED")
            connection.execute(
                "UPDATE runtime_commits SET state='applied',ack_json=? WHERE commit_id=?",
                (_canonical_record(ack), commit_id),
            )
            connection.execute(
                "UPDATE runtime_preparations SET state='active' WHERE preparation_id=?",
                (commit.preparation_id,),
            )
            connection.commit()
        return self.snapshot()

    def record_failure(self, preparation_id: str, reason_code: str) -> RuntimeAuthoritySnapshot:
        if not reason_code or len(reason_code) > 128:
            raise ValueError("invalid failure reason")
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE runtime_preparations SET state='failed' WHERE preparation_id=?",
                (preparation_id,),
            )
            connection.execute(
                "UPDATE runtime_authority SET pending_preparation_id=NULL,last_failure_code=? WHERE singleton=1 AND pending_preparation_id=?",
                (reason_code, preparation_id),
            )
            connection.commit()
        return self.snapshot()

    def snapshot(self) -> RuntimeAuthoritySnapshot:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT revision,active_package_digest,pending_preparation_id,last_failure_code FROM runtime_authority WHERE singleton=1"
            ).fetchone()
        if row is None:
            raise RuntimeAuthorityError("AUTHORITY_UNAVAILABLE")
        return RuntimeAuthoritySnapshot(*row)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database, timeout=5.0, isolation_level=None)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runtime_authority (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    revision INTEGER NOT NULL,
                    active_package_digest TEXT NOT NULL,
                    pending_preparation_id TEXT,
                    last_failure_code TEXT
                );
                INSERT OR IGNORE INTO runtime_authority(singleton,revision,active_package_digest)
                VALUES(1,0,'blake3:0000000000000000000000000000000000000000000000000000000000000000');
                CREATE TABLE IF NOT EXISTS runtime_preparations (
                    preparation_id TEXT PRIMARY KEY,
                    preparation_json BLOB NOT NULL,
                    state TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    expires_at_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime_commits (
                    commit_id TEXT PRIMARY KEY,
                    preparation_id TEXT NOT NULL UNIQUE,
                    commit_json BLOB NOT NULL,
                    state TEXT NOT NULL,
                    ack_json BLOB,
                    created_at_ms INTEGER NOT NULL,
                    expires_at_ms INTEGER NOT NULL
                );
                """
            )
            connection.commit()


def _validate_preparation(request: LoadPreparation, now_ms: int) -> None:
    if (
        not request.preparation_id
        or request.stage not in _STAGES
        or not 1 <= len(request.symbols) <= 5
        or len(set(request.symbols)) != len(request.symbols)
        or any(_SYMBOL.fullmatch(symbol) is None for symbol in request.symbols)
        or request.expected_old_revision < 0
        or request.cutover_input_sequence < 0
        or request.created_at_ms <= 0
        or now_ms < request.created_at_ms
        or now_ms >= request.expires_at_ms
    ):
        raise RuntimeAuthorityError("INVALID_PREPARATION")
    for name in (
        "package_digest",
        "expected_old_package_digest",
        "configuration_digest",
        "feature_schema_digest",
        "context_schema_digest",
        "policy_digest",
        "evidence_bundle_digest",
        "ready_token",
    ):
        _require_digest(getattr(request, name), name)


def _require_digest(value: str, name: str) -> None:
    if _DIGEST.fullmatch(value) is None:
        raise ValueError(f"invalid {name}")


def _canonical_record(value: object) -> bytes:
    payload = asdict(value)  # type: ignore[arg-type]
    for key, item in tuple(payload.items()):
        if isinstance(item, tuple):
            payload[key] = list(item)
    return canonical_json_v1(payload)


def _decode_preparation(raw: bytes) -> LoadPreparation:
    payload = json.loads(raw)
    payload["symbols"] = tuple(payload["symbols"])
    return LoadPreparation(**payload)


def _decode_commit(raw: bytes) -> ActivationCommit:
    payload = json.loads(raw)
    payload["symbols"] = tuple(payload["symbols"])
    return ActivationCommit(**payload)
