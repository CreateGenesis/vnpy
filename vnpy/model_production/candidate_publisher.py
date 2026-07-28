"""Deterministic vn.py-only ReadyCandidateV2 publication."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
from time import time_ns
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from vnpy.demo_web.contracts import ReadyCandidateV2
from vnpy.model_production.lifecycle import LifecycleStateV3


class CandidateAdmissionError(RuntimeError):
    pass


@dataclass(frozen=True)
class HistoricalCandidateGate:
    """Harness-owned historical decision accepted by deterministic vn.py admission."""

    contract_version: int
    metric_owner: str
    evaluation_digest: str
    eligible: bool
    reason_codes: tuple[str, ...]

    def is_valid(self) -> bool:
        return (
            self.contract_version == 1
            and self.metric_owner == "harness"
            and _valid_digest(self.evaluation_digest)
            and self.eligible
            and not self.reason_codes
        )


_BINDINGS = (
    "candidate_digest",
    "package_digest",
    "configuration_digest",
    "policy_digest",
    "data_snapshot_digest",
    "feature_schema_digest",
    "thresholds_digest",
    "review_digest",
    "evaluation_digest",
    "runtime_profile_digest",
    "rollback_digest",
    "author_lineage_digest",
    "symbols",
    "valid_until_ms",
    "lifecycle_revision",
)


class ReadyCandidatePublisher:
    def __init__(
        self,
        root: Path,
        *,
        private_key: Ed25519PrivateKey,
        publisher_identity: str,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if publisher_identity != "vnpy-admission":
            raise CandidateAdmissionError("CANDIDATE_PUBLISHER_AUTHORITY_DENIED")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._current = self.root / "ready-candidate-v2.json"
        self._rollback = self.root / "ready-candidate-v2.rollback.json"
        self._history = self.root / "ready-candidate-v2.history"
        self._history.mkdir(parents=True, exist_ok=True)
        self._private_key = private_key
        self._public_key = private_key.public_key()
        self._publisher_identity = publisher_identity
        self._clock_ms = clock_ms or (lambda: time_ns() // 1_000_000)

    def publish(
        self,
        candidate: dict[str, Any],
        *,
        historical_gate: HistoricalCandidateGate,
        lifecycle_state: LifecycleStateV3,
        caller_authority: str = "vnpy",
    ) -> dict[str, Any]:
        if caller_authority != "vnpy":
            raise CandidateAdmissionError("CANDIDATE_PUBLISHER_AUTHORITY_DENIED")
        missing = [key for key in _BINDINGS if key not in candidate]
        if missing:
            raise CandidateAdmissionError("READY_CANDIDATE_BINDING_MISSING")
        if (
            not historical_gate.is_valid()
            or historical_gate.evaluation_digest != candidate["evaluation_digest"]
        ):
            raise CandidateAdmissionError("HISTORICAL_CANDIDATE_GATE_REQUIRED")
        if not self._lifecycle_matches(candidate, lifecycle_state):
            raise CandidateAdmissionError("CANDIDATE_LIFECYCLE_NOT_READY")
        previous = self.read_current(optional=True)
        created_at = self._clock_ms()
        if (
            not isinstance(candidate.get("valid_until_ms"), int)
            or isinstance(candidate.get("valid_until_ms"), bool)
            or int(candidate["valid_until_ms"]) <= created_at
        ):
            raise CandidateAdmissionError("READY_CANDIDATE_EXPIRED")
        revision = (int(previous["revision"]) + 1) if previous else 1
        record = self._create_record(
            candidate,
            revision=revision,
            state="ready",
            created_at_ms=created_at,
        )
        if previous and self.verify(previous):
            _atomic_json(self._rollback, previous)
        self._persist_transition(record)
        return record

    def invalidate(
        self,
        *,
        expected_revision: int,
        caller_authority: str = "vnpy",
    ) -> dict[str, Any]:
        if caller_authority != "vnpy":
            raise CandidateAdmissionError("CANDIDATE_PUBLISHER_AUTHORITY_DENIED")
        current = self._require_revision(expected_revision)
        record = self._create_record(
            _bindings(current),
            revision=expected_revision + 1,
            state="invalidated",
            created_at_ms=self._clock_ms(),
        )
        self._persist_transition(record)
        return record

    def expire(
        self,
        *,
        expected_revision: int,
        caller_authority: str = "vnpy",
    ) -> dict[str, Any]:
        if caller_authority != "vnpy":
            raise CandidateAdmissionError("CANDIDATE_PUBLISHER_AUTHORITY_DENIED")
        current = self._require_revision(expected_revision)
        if self._clock_ms() <= int(current["valid_until_ms"]):
            raise CandidateAdmissionError("READY_CANDIDATE_NOT_EXPIRED")
        record = self._create_record(
            _bindings(current),
            revision=expected_revision + 1,
            state="expired",
            created_at_ms=self._clock_ms(),
        )
        self._persist_transition(record)
        return record

    def rollback(
        self,
        *,
        expected_revision: int,
        caller_authority: str = "vnpy",
    ) -> dict[str, Any]:
        if caller_authority != "vnpy":
            raise CandidateAdmissionError("CANDIDATE_PUBLISHER_AUTHORITY_DENIED")
        current = self._require_revision(expected_revision)
        target = self.read_rollback()
        if not self.verify(target):
            raise CandidateAdmissionError("READY_CANDIDATE_ROLLBACK_INVALID")
        prior_ready = self._latest_ready_for_candidate(str(current["candidate_digest"]))
        record = self._create_record(
            _bindings(target),
            revision=expected_revision + 1,
            state="rollback",
            created_at_ms=self._clock_ms(),
        )
        if prior_ready is not None:
            _atomic_json(self._rollback, prior_ready)
        self._persist_transition(record)
        return record

    def read_current(self, *, optional: bool = False) -> dict[str, Any] | None:
        if not self._current.exists():
            if optional:
                return None
            raise CandidateAdmissionError("READY_CANDIDATE_UNAVAILABLE")
        return _read_json(self._current)

    def read_rollback(self) -> dict[str, Any]:
        if not self._rollback.exists():
            raise CandidateAdmissionError("READY_CANDIDATE_ROLLBACK_UNAVAILABLE")
        return _read_json(self._rollback)

    def verify(
        self,
        record: dict[str, Any],
        *,
        expected_bindings: dict[str, Any] | None = None,
        now_ms: int | None = None,
    ) -> bool:
        try:
            parsed = ReadyCandidateV2.model_validate(record).model_dump(mode="json")
        except ValidationError:
            return False
        if (
            parsed["state"] not in {"ready", "rollback"}
            or parsed["valid_until_ms"] < (now_ms or self._clock_ms())
        ):
            return False
        if expected_bindings and any(parsed[key] != expected_bindings.get(key) for key in _BINDINGS):
            return False
        unsigned = dict(parsed)
        signature = bytes.fromhex(str(unsigned.pop("signature")))
        payload_digest = unsigned.pop("payload_digest")
        if payload_digest != _digest(unsigned):
            return False
        unsigned["payload_digest"] = payload_digest
        try:
            self._public_key.verify(signature, _canonical(unsigned))
        except (InvalidSignature, ValueError):
            return False
        return True

    def sign_attestation(self, value: dict[str, Any]) -> dict[str, Any]:
        """Sign a derived non-authority record with the vn.py admission key."""

        material = dict(value)
        material["publisher_identity"] = self._publisher_identity
        material["publisher_key_fingerprint"] = self._key_fingerprint()
        material["payload_digest"] = _digest(material)
        material["signature"] = self._private_key.sign(_canonical(material)).hex()
        return material

    def _lifecycle_matches(
        self,
        candidate: dict[str, Any],
        lifecycle_state: LifecycleStateV3,
    ) -> bool:
        return (
            lifecycle_state.is_valid()
            and lifecycle_state.stage == "broker_simulation"
            and lifecycle_state.state == "ready"
            and lifecycle_state.candidate_digest == candidate["candidate_digest"]
            and lifecycle_state.package_digest == candidate["package_digest"]
            and lifecycle_state.configuration_digest == candidate["configuration_digest"]
            and lifecycle_state.policy_digest == candidate["policy_digest"]
            and lifecycle_state.revision == candidate["lifecycle_revision"]
        )

    def _create_record(
        self,
        candidate: dict[str, Any],
        *,
        revision: int,
        state: str,
        created_at_ms: int,
    ) -> dict[str, Any]:
        identity = _digest(
            {
                "revision": revision,
                "state": state,
                "created_at_ms": created_at_ms,
                **{key: candidate[key] for key in _BINDINGS},
            }
        )
        material: dict[str, Any] = {
            "contract_version": 2,
            "publication_id": str(uuid5(NAMESPACE_URL, identity)),
            "revision": revision,
            "state": state,
            **{key: candidate[key] for key in _BINDINGS},
            "publisher_identity": self._publisher_identity,
            "publisher_key_fingerprint": self._key_fingerprint(),
            "created_at_ms": created_at_ms,
        }
        material["payload_digest"] = _digest(material)
        material["signature"] = self._private_key.sign(_canonical(material)).hex()
        try:
            return ReadyCandidateV2.model_validate(material).model_dump(mode="json")
        except ValidationError as exc:
            raise CandidateAdmissionError("READY_CANDIDATE_INVALID") from exc

    def _key_fingerprint(self) -> str:
        public_bytes = self._public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return "sha256:" + sha256(public_bytes).hexdigest()

    def _persist_transition(self, record: dict[str, Any]) -> None:
        history = self._history / (
            f"{int(record['revision']):020d}-{record['publication_id']}.json"
        )
        _atomic_json(history, record)
        _atomic_json(self._current, record)

    def _require_revision(self, expected_revision: int) -> dict[str, Any]:
        current = self.read_current()
        if current is None or current.get("revision") != expected_revision:
            raise CandidateAdmissionError("READY_CANDIDATE_REVISION_CONFLICT")
        return current

    def _latest_ready_for_candidate(self, candidate_digest: str) -> dict[str, Any] | None:
        for path in sorted(self._history.glob("*.json"), reverse=True):
            value = _read_json(path)
            if (
                value.get("candidate_digest") == candidate_digest
                and value.get("state") in {"ready", "rollback"}
                and self.verify(value)
            ):
                return value
        return None


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def _digest(value: dict[str, Any]) -> str:
    return "sha256:" + sha256(_canonical(value)).hexdigest()


def _valid_digest(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        algorithm, hexadecimal = value.split(":", 1)
    except ValueError:
        return False
    return (
        algorithm in {"sha256", "blake3"}
        and len(hexadecimal) == 64
        and all(character in "0123456789abcdef" for character in hexadecimal)
    )


def _bindings(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record[key] for key in _BINDINGS}


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise CandidateAdmissionError("READY_CANDIDATE_STATE_INVALID")
    return value


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    with temp.open("x", encoding="ascii", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temp, 0o600)
    os.replace(temp, path)
