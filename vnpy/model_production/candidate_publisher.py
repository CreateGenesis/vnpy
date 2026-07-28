"""Deterministic vn.py-only ReadyCandidateV2 publication."""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
from time import time_ns
from typing import Any, Callable
from uuid import uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from vnpy.demo_web.contracts import ReadyCandidateV2


class CandidateAdmissionError(RuntimeError):
    pass


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
        self._private_key = private_key
        self._public_key = private_key.public_key()
        self._publisher_identity = publisher_identity
        self._clock_ms = clock_ms or (lambda: time_ns() // 1_000_000)

    def publish(
        self,
        candidate: dict[str, Any],
        *,
        caller_authority: str = "vnpy",
    ) -> dict[str, Any]:
        if caller_authority != "vnpy":
            raise CandidateAdmissionError("CANDIDATE_PUBLISHER_AUTHORITY_DENIED")
        missing = [key for key in _BINDINGS if key not in candidate]
        if missing:
            raise CandidateAdmissionError("READY_CANDIDATE_BINDING_MISSING")
        previous = self.read_current(optional=True)
        created_at = self._clock_ms()
        public_bytes = self._public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        material: dict[str, Any] = {
            "contract_version": 2,
            "publication_id": str(uuid4()),
            "revision": (int(previous["revision"]) + 1) if previous else 1,
            "state": "ready",
            **{key: candidate[key] for key in _BINDINGS},
            "publisher_identity": self._publisher_identity,
            "publisher_key_fingerprint": "sha256:" + sha256(public_bytes).hexdigest(),
            "created_at_ms": created_at,
        }
        material["payload_digest"] = _digest(material)
        material["signature"] = self._private_key.sign(_canonical(material)).hex()
        try:
            record = ReadyCandidateV2.model_validate(material).model_dump(mode="json")
        except ValidationError as exc:
            raise CandidateAdmissionError("READY_CANDIDATE_INVALID") from exc
        if previous:
            _atomic_json(self._rollback, previous)
        _atomic_json(self._current, record)
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
        if parsed["state"] != "ready" or parsed["valid_until_ms"] < (now_ms or self._clock_ms()):
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


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def _digest(value: dict[str, Any]) -> str:
    return "sha256:" + sha256(_canonical(value)).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise CandidateAdmissionError("READY_CANDIDATE_STATE_INVALID")
    return value


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("x", encoding="ascii", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temp, 0o600)
    os.replace(temp, path)
