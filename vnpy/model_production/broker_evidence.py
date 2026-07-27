"""Signed, append-only evidence for broker-simulation runs."""

from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from threading import RLock
from typing import Any
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .contracts import canonical_json_v1


_DIGEST = re.compile(r"^(?:sha256|blake3):[0-9a-f]{64}$")
_GATEWAYS = frozenset({"XTP", "TORA"})
_ZERO_DIGEST = "sha256:" + "0" * 64


@dataclass(frozen=True)
class BrokerRunEvidenceInput:
    """Authoritative terminal values used to seal one run."""

    campaign_id: str
    run_id: str
    candidate_digest: str
    gateway: str
    gateway_binding_digest: str
    sessions: tuple[date, ...]
    starting_equity_minor: int
    ending_equity_minor: int
    external_cashflow_minor: int
    realized_profit_minor: int
    unrealized_profit_minor: int
    fees_minor: int
    reconciled: bool
    hard_limit_breaches: int
    unresolved_outcomes: int
    created_at_ms: int


@dataclass(frozen=True)
class BrokerRunEvidenceManifest:
    """Strict signed projection matching broker-simulation-evidence-v1."""

    contract_version: int
    entity_type: str
    campaign_id: str
    run_id: str
    candidate_digest: str
    gateway: str
    gateway_binding_digest: str
    sessions: tuple[date, ...]
    starting_equity_minor: int
    ending_equity_minor: int
    external_cashflow_minor: int
    realized_profit_minor: int
    unrealized_profit_minor: int
    fees_minor: int
    net_profit_minor: int
    reconciled: bool
    hard_limit_breaches: int
    unresolved_outcomes: int
    event_chain_root: str
    signer_id: str
    signature: str
    created_at_ms: int


@dataclass(frozen=True)
class BrokerEvidenceRecord:
    """One retained manifest and its position in the append-only record chain."""

    manifest: BrokerRunEvidenceManifest
    previous_record_digest: str | None
    record_digest: str


@dataclass(frozen=True)
class DualGatewayReadiness:
    ready: bool
    reason_codes: tuple[str, ...]
    gateway_net_profit_minor: dict[str, int]


class BrokerEvidenceLedger:
    """Durably seal and recover signed broker-simulation evidence."""

    def __init__(
        self,
        directory: str | Path,
        signer_id: str,
        private_key: Ed25519PrivateKey,
    ) -> None:
        if not signer_id.strip() or len(signer_id) > 128:
            raise ValueError("EVIDENCE_SIGNER_INVALID")
        self._directory = Path(directory)
        self._directory.mkdir(parents=True, exist_ok=True)
        self._path = self._directory / "broker-simulation-evidence.jsonl"
        self._signer_id = signer_id
        self._private_key = private_key
        self._lock = RLock()

    @property
    def public_key(self) -> Ed25519PublicKey:
        return self._private_key.public_key()

    def append(
        self,
        evidence_input: BrokerRunEvidenceInput,
        *,
        events: Iterable[Mapping[str, Any]],
    ) -> BrokerEvidenceRecord:
        _validate_input(evidence_input)
        event_chain_root = _event_chain_root(events)
        net_profit_minor = (
            evidence_input.ending_equity_minor
            - evidence_input.starting_equity_minor
            - evidence_input.external_cashflow_minor
        )
        component_profit = (
            evidence_input.realized_profit_minor
            + evidence_input.unrealized_profit_minor
            - evidence_input.fees_minor
        )
        if net_profit_minor != component_profit:
            raise ValueError("EVIDENCE_PROFIT_RECONCILIATION_FAILED")

        unsigned: dict[str, Any] = {
            "contract_version": 1,
            "entity_type": "broker_simulation_evidence",
            "campaign_id": evidence_input.campaign_id,
            "run_id": evidence_input.run_id,
            "candidate_digest": evidence_input.candidate_digest,
            "gateway": evidence_input.gateway,
            "gateway_binding_digest": evidence_input.gateway_binding_digest,
            "sessions": [session.isoformat() for session in evidence_input.sessions],
            "starting_equity_minor": evidence_input.starting_equity_minor,
            "ending_equity_minor": evidence_input.ending_equity_minor,
            "external_cashflow_minor": evidence_input.external_cashflow_minor,
            "realized_profit_minor": evidence_input.realized_profit_minor,
            "unrealized_profit_minor": evidence_input.unrealized_profit_minor,
            "fees_minor": evidence_input.fees_minor,
            "net_profit_minor": net_profit_minor,
            "reconciled": evidence_input.reconciled,
            "hard_limit_breaches": evidence_input.hard_limit_breaches,
            "unresolved_outcomes": evidence_input.unresolved_outcomes,
            "event_chain_root": event_chain_root,
            "signer_id": self._signer_id,
            "created_at_ms": evidence_input.created_at_ms,
        }
        signature = _encode_signature(self._private_key.sign(canonical_json_v1(unsigned)))
        manifest = _manifest_from_dict({**unsigned, "signature": signature})

        with self._lock:
            retained = self.records()
            if any(record.manifest.run_id == manifest.run_id for record in retained):
                raise ValueError("EVIDENCE_RUN_ALREADY_RETAINED")
            previous_record_digest = retained[-1].record_digest if retained else None
            record_payload = {
                "manifest": _manifest_to_dict(manifest),
                "previous_record_digest": previous_record_digest,
            }
            record_digest = _sha256_digest(canonical_json_v1(record_payload))
            stored = {**record_payload, "record_digest": record_digest}
            with self._path.open("ab") as handle:
                handle.write(canonical_json_v1(stored) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
        return BrokerEvidenceRecord(manifest, previous_record_digest, record_digest)

    def records(self) -> tuple[BrokerEvidenceRecord, ...]:
        with self._lock:
            if not self._path.exists():
                return ()
            records: list[BrokerEvidenceRecord] = []
            previous: str | None = None
            with self._path.open("rb") as handle:
                for line_number, raw_line in enumerate(handle, start=1):
                    if not raw_line.endswith(b"\n"):
                        raise ValueError(f"EVIDENCE_LEDGER_TRUNCATED:{line_number}")
                    try:
                        stored = json.loads(raw_line)
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise ValueError(f"EVIDENCE_LEDGER_INVALID:{line_number}") from exc
                    if not isinstance(stored, dict) or set(stored) != {
                        "manifest",
                        "previous_record_digest",
                        "record_digest",
                    }:
                        raise ValueError(f"EVIDENCE_LEDGER_INVALID:{line_number}")
                    if stored["previous_record_digest"] != previous:
                        raise ValueError(f"EVIDENCE_RECORD_CHAIN_INVALID:{line_number}")
                    payload = {
                        "manifest": stored["manifest"],
                        "previous_record_digest": stored["previous_record_digest"],
                    }
                    expected = _sha256_digest(canonical_json_v1(payload))
                    if stored["record_digest"] != expected:
                        raise ValueError(f"EVIDENCE_RECORD_DIGEST_INVALID:{line_number}")
                    manifest = _manifest_from_dict(stored["manifest"])
                    if manifest.signer_id != self._signer_id or not verify_evidence_signature(
                        manifest, self.public_key
                    ):
                        raise ValueError(f"EVIDENCE_SIGNATURE_INVALID:{line_number}")
                    records.append(
                        BrokerEvidenceRecord(
                            manifest=manifest,
                            previous_record_digest=previous,
                            record_digest=expected,
                        )
                    )
                    previous = expected
            return tuple(records)


def verify_evidence_signature(
    manifest: BrokerRunEvidenceManifest,
    public_key: Ed25519PublicKey,
) -> bool:
    """Verify the signature over the strict unsigned evidence projection."""

    try:
        signature = _decode_signature(manifest.signature)
        public_key.verify(signature, canonical_json_v1(_manifest_unsigned_dict(manifest)))
    except (InvalidSignature, ValueError, TypeError):
        return False
    return True


def evaluate_dual_gateway_readiness(
    manifests: Iterable[BrokerRunEvidenceManifest],
) -> DualGatewayReadiness:
    """Require independently positive, certain XTP and TORA evidence."""

    retained = tuple(manifests)
    reasons: list[str] = []
    by_gateway: dict[str, BrokerRunEvidenceManifest] = {}
    if len(retained) != 2:
        reasons.append("DUAL_GATEWAY_EVIDENCE_REQUIRED")
    for manifest in retained:
        if manifest.gateway not in _GATEWAYS or manifest.gateway in by_gateway:
            reasons.append("GATEWAY_EVIDENCE_IDENTITY_INVALID")
        else:
            by_gateway[manifest.gateway] = manifest
        if manifest.net_profit_minor <= 0:
            reasons.append("GATEWAY_NET_PROFIT_NOT_POSITIVE")
        if manifest.net_profit_minor != (
            manifest.ending_equity_minor
            - manifest.starting_equity_minor
            - manifest.external_cashflow_minor
        ) or manifest.net_profit_minor != (
            manifest.realized_profit_minor
            + manifest.unrealized_profit_minor
            - manifest.fees_minor
        ):
            reasons.append("EVIDENCE_PROFIT_RECONCILIATION_FAILED")
        if not manifest.reconciled:
            reasons.append("GATEWAY_RECONCILIATION_INCOMPLETE")
        if manifest.hard_limit_breaches != 0:
            reasons.append("HARD_LIMIT_BREACH_RETAINED")
        if manifest.unresolved_outcomes != 0:
            reasons.append("BROKER_OUTCOME_UNRESOLVED")
    if set(by_gateway) != _GATEWAYS:
        reasons.append("XTP_TORA_EVIDENCE_REQUIRED")
    if len(retained) == 2:
        first, second = retained
        if (
            first.campaign_id != second.campaign_id
            or first.candidate_digest != second.candidate_digest
            or first.sessions != second.sessions
        ):
            reasons.append("DUAL_GATEWAY_PARENT_IDENTITY_MISMATCH")
        if first.gateway_binding_digest == second.gateway_binding_digest:
            reasons.append("GATEWAY_BINDINGS_NOT_INDEPENDENT")
    return DualGatewayReadiness(
        ready=not reasons,
        reason_codes=tuple(dict.fromkeys(reasons)),
        gateway_net_profit_minor={
            gateway: by_gateway[gateway].net_profit_minor for gateway in sorted(by_gateway)
        },
    )


def _validate_input(value: BrokerRunEvidenceInput) -> None:
    for identifier in (value.campaign_id, value.run_id):
        try:
            UUID(identifier)
        except (ValueError, TypeError) as exc:
            raise ValueError("EVIDENCE_IDENTITY_INVALID") from exc
    if value.gateway not in _GATEWAYS:
        raise ValueError("EVIDENCE_GATEWAY_INVALID")
    if not _DIGEST.fullmatch(value.candidate_digest) or not _DIGEST.fullmatch(
        value.gateway_binding_digest
    ):
        raise ValueError("EVIDENCE_DIGEST_INVALID")
    if not _is_five_session_window(value.sessions):
        raise ValueError("EVIDENCE_SESSION_WINDOW_INVALID")
    if value.starting_equity_minor < 0 or value.ending_equity_minor < 0:
        raise ValueError("EVIDENCE_EQUITY_INVALID")
    if value.fees_minor < 0:
        raise ValueError("EVIDENCE_FEES_INVALID")
    if value.hard_limit_breaches < 0 or value.unresolved_outcomes < 0:
        raise ValueError("EVIDENCE_SAFETY_COUNT_INVALID")
    if value.created_at_ms <= 0:
        raise ValueError("EVIDENCE_TIMESTAMP_INVALID")


def _is_five_session_window(sessions: tuple[date, ...]) -> bool:
    if len(sessions) != 5 or len(set(sessions)) != 5:
        return False
    if any(not isinstance(session, date) or session.weekday() >= 5 for session in sessions):
        return False
    return all(
        current == _next_weekday(previous)
        for previous, current in zip(sessions, sessions[1:], strict=False)
    )


def _next_weekday(value: date) -> date:
    candidate = value + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def _event_chain_root(events: Iterable[Mapping[str, Any]]) -> str:
    previous = _ZERO_DIGEST
    count = 0
    last_sequence: int | None = None
    for event in events:
        if not isinstance(event, Mapping):
            raise ValueError("EVIDENCE_EVENT_INVALID")
        payload = dict(event)
        sequence = payload.get("sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence <= 0:
            raise ValueError("EVIDENCE_EVENT_SEQUENCE_INVALID")
        if last_sequence is not None and sequence <= last_sequence:
            raise ValueError("EVIDENCE_EVENT_SEQUENCE_INVALID")
        previous = _sha256_digest(
            canonical_json_v1({"event": payload, "previous_event_digest": previous})
        )
        last_sequence = sequence
        count += 1
    if count == 0:
        raise ValueError("EVIDENCE_EVENTS_REQUIRED")
    return previous


def _manifest_unsigned_dict(manifest: BrokerRunEvidenceManifest) -> dict[str, Any]:
    value = _manifest_to_dict(manifest)
    del value["signature"]
    return value


def _manifest_to_dict(manifest: BrokerRunEvidenceManifest) -> dict[str, Any]:
    return {
        "contract_version": manifest.contract_version,
        "entity_type": manifest.entity_type,
        "campaign_id": manifest.campaign_id,
        "run_id": manifest.run_id,
        "candidate_digest": manifest.candidate_digest,
        "gateway": manifest.gateway,
        "gateway_binding_digest": manifest.gateway_binding_digest,
        "sessions": [session.isoformat() for session in manifest.sessions],
        "starting_equity_minor": manifest.starting_equity_minor,
        "ending_equity_minor": manifest.ending_equity_minor,
        "external_cashflow_minor": manifest.external_cashflow_minor,
        "realized_profit_minor": manifest.realized_profit_minor,
        "unrealized_profit_minor": manifest.unrealized_profit_minor,
        "fees_minor": manifest.fees_minor,
        "net_profit_minor": manifest.net_profit_minor,
        "reconciled": manifest.reconciled,
        "hard_limit_breaches": manifest.hard_limit_breaches,
        "unresolved_outcomes": manifest.unresolved_outcomes,
        "event_chain_root": manifest.event_chain_root,
        "signer_id": manifest.signer_id,
        "signature": manifest.signature,
        "created_at_ms": manifest.created_at_ms,
    }


def _manifest_from_dict(value: Mapping[str, Any]) -> BrokerRunEvidenceManifest:
    expected = {
        "contract_version",
        "entity_type",
        "campaign_id",
        "run_id",
        "candidate_digest",
        "gateway",
        "gateway_binding_digest",
        "sessions",
        "starting_equity_minor",
        "ending_equity_minor",
        "external_cashflow_minor",
        "realized_profit_minor",
        "unrealized_profit_minor",
        "fees_minor",
        "net_profit_minor",
        "reconciled",
        "hard_limit_breaches",
        "unresolved_outcomes",
        "event_chain_root",
        "signer_id",
        "signature",
        "created_at_ms",
    }
    if set(value) != expected:
        raise ValueError("EVIDENCE_MANIFEST_FIELDS_INVALID")
    try:
        sessions = tuple(date.fromisoformat(item) for item in value["sessions"])
        manifest = BrokerRunEvidenceManifest(
            contract_version=value["contract_version"],
            entity_type=value["entity_type"],
            campaign_id=value["campaign_id"],
            run_id=value["run_id"],
            candidate_digest=value["candidate_digest"],
            gateway=value["gateway"],
            gateway_binding_digest=value["gateway_binding_digest"],
            sessions=sessions,
            starting_equity_minor=value["starting_equity_minor"],
            ending_equity_minor=value["ending_equity_minor"],
            external_cashflow_minor=value["external_cashflow_minor"],
            realized_profit_minor=value["realized_profit_minor"],
            unrealized_profit_minor=value["unrealized_profit_minor"],
            fees_minor=value["fees_minor"],
            net_profit_minor=value["net_profit_minor"],
            reconciled=value["reconciled"],
            hard_limit_breaches=value["hard_limit_breaches"],
            unresolved_outcomes=value["unresolved_outcomes"],
            event_chain_root=value["event_chain_root"],
            signer_id=value["signer_id"],
            signature=value["signature"],
            created_at_ms=value["created_at_ms"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("EVIDENCE_MANIFEST_INVALID") from exc
    if manifest.contract_version != 1 or manifest.entity_type != "broker_simulation_evidence":
        raise ValueError("EVIDENCE_CONTRACT_INVALID")
    _validate_input(
        BrokerRunEvidenceInput(
            campaign_id=manifest.campaign_id,
            run_id=manifest.run_id,
            candidate_digest=manifest.candidate_digest,
            gateway=manifest.gateway,
            gateway_binding_digest=manifest.gateway_binding_digest,
            sessions=manifest.sessions,
            starting_equity_minor=manifest.starting_equity_minor,
            ending_equity_minor=manifest.ending_equity_minor,
            external_cashflow_minor=manifest.external_cashflow_minor,
            realized_profit_minor=manifest.realized_profit_minor,
            unrealized_profit_minor=manifest.unrealized_profit_minor,
            fees_minor=manifest.fees_minor,
            reconciled=manifest.reconciled,
            hard_limit_breaches=manifest.hard_limit_breaches,
            unresolved_outcomes=manifest.unresolved_outcomes,
            created_at_ms=manifest.created_at_ms,
        )
    )
    if not _DIGEST.fullmatch(manifest.event_chain_root):
        raise ValueError("EVIDENCE_EVENT_CHAIN_INVALID")
    if not manifest.signer_id.strip() or len(manifest.signer_id) > 128:
        raise ValueError("EVIDENCE_SIGNER_INVALID")
    if manifest.net_profit_minor != (
        manifest.ending_equity_minor
        - manifest.starting_equity_minor
        - manifest.external_cashflow_minor
    ) or manifest.net_profit_minor != (
        manifest.realized_profit_minor + manifest.unrealized_profit_minor - manifest.fees_minor
    ):
        raise ValueError("EVIDENCE_PROFIT_RECONCILIATION_FAILED")
    return manifest


def _sha256_digest(value: bytes) -> str:
    return f"sha256:{sha256(value).hexdigest()}"


def _encode_signature(value: bytes) -> str:
    return urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_signature(value: str) -> bytes:
    if len(value) != 86 or not re.fullmatch(r"[A-Za-z0-9_-]{86}", value):
        raise ValueError("EVIDENCE_SIGNATURE_ENCODING_INVALID")
    return urlsafe_b64decode(value + "==")
