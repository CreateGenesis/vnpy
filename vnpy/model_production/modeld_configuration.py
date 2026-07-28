"""Derive per-gateway modeld configurations from one admitted candidate."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
from typing import Any
from uuid import uuid4

from blake3 import blake3

from .candidate_publisher import ReadyCandidatePublisher


class ModeldConfigurationError(RuntimeError):
    pass


_PROFILE_KEYS = {
    "contract_version",
    "package_manifest_path",
    "package_blob_root",
    "trust_root",
    "model",
    "bridge_capacity",
    "bridge_slot_size",
    "input_producer_id",
    "input_producer_epoch",
    "process_epoch",
    "fast_action_qualified",
    "maximum_inference_latency_ns",
    "poll_interval_ms",
}


class ModeldConfigurationPublisher:
    """Use Rust modeld as the final strict package/configuration verifier."""

    def __init__(
        self,
        root: Path,
        *,
        modeld_executable: Path,
        candidate_publisher: ReadyCandidatePublisher,
        command_runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._modeld = Path(modeld_executable)
        self._candidate_publisher = candidate_publisher
        self._run = command_runner

    def derive(
        self,
        ready_candidate: dict[str, Any],
        *,
        runtime_profile_path: Path,
        calendar_sessions: Iterable[str],
        gateways: Iterable[str],
    ) -> dict[str, Path]:
        if not self._candidate_publisher.verify(ready_candidate):
            raise ModeldConfigurationError("READY_CANDIDATE_SIGNATURE_INVALID")
        current = self._candidate_publisher.read_current(optional=True)
        if (
            current is None
            or current.get("publication_id") != ready_candidate.get("publication_id")
            or current.get("payload_digest") != ready_candidate.get("payload_digest")
        ):
            raise ModeldConfigurationError("READY_CANDIDATE_NOT_CURRENT")

        gateway_list = tuple(gateways)
        if (
            not gateway_list
            or len(set(gateway_list)) != len(gateway_list)
            or any(gateway not in {"XTP", "TORA"} for gateway in gateway_list)
        ):
            raise ModeldConfigurationError("GATEWAY_UNSUPPORTED")
        sessions = tuple(calendar_sessions)
        self._validate_sessions(sessions)

        runtime_profile_path = Path(runtime_profile_path)
        profile = _read_json(runtime_profile_path)
        if set(profile) != _PROFILE_KEYS or profile.get("contract_version") != 1:
            raise ModeldConfigurationError("RUNTIME_PROFILE_INVALID")
        profile_digest = "sha256:" + sha256(_canonical(profile)).hexdigest()
        if profile_digest != ready_candidate.get("runtime_profile_digest"):
            raise ModeldConfigurationError("RUNTIME_PROFILE_IDENTITY_DRIFT")
        model = profile.get("model")
        if not isinstance(model, dict):
            raise ModeldConfigurationError("RUNTIME_PROFILE_INVALID")
        configuration_digest = "blake3:" + blake3(_canonical(model)).hexdigest()
        if configuration_digest != ready_candidate.get("configuration_digest"):
            raise ModeldConfigurationError("MODEL_CONFIGURATION_IDENTITY_DRIFT")
        self._verify_package(profile, ready_candidate)

        candidate_projection = {
            "contract_version": 1,
            "ready": True,
            "candidate_digest": ready_candidate["candidate_digest"],
            "author_lineage_digest": ready_candidate["author_lineage_digest"],
            "package_digest": ready_candidate["package_digest"],
            "configuration_digest": ready_candidate["configuration_digest"],
            "policy_digest": ready_candidate["policy_digest"],
            "symbols": ready_candidate["symbols"],
            "calendar_sessions": list(sessions),
            "lifecycle_revision": ready_candidate["lifecycle_revision"],
        }
        candidate_path = self.root / "ready-candidate.json"
        _atomic_json(candidate_path, candidate_projection)

        derived: dict[str, Path] = {}
        for gateway in gateway_list:
            destination = self.root / "runs" / gateway / "modeld.json"
            bridge_root = self.root / "runs" / gateway / "model-bridge"
            command = [
                str(self._modeld),
                "prepare",
                "--profile",
                str(runtime_profile_path),
                "--candidate",
                str(candidate_path),
                "--gateway",
                gateway,
                "--bridge-root",
                str(bridge_root),
            ]
            prepared = self._run(command, check=False, capture_output=True)
            if prepared.returncode != 0:
                raise ModeldConfigurationError("MODELD_CONFIGURATION_PREPARE_FAILED")
            configuration = _decode_object(prepared.stdout)
            pending = destination.with_name(f".{destination.name}.{uuid4().hex}.pending")
            _write_json(pending, configuration)
            checked = self._run(
                [str(self._modeld), "check", "--config", str(pending)],
                check=False,
                capture_output=True,
            )
            if checked.returncode != 0:
                pending.unlink(missing_ok=True)
                raise ModeldConfigurationError("MODELD_CONFIGURATION_CHECK_FAILED")
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(pending, destination)
            configuration_digest_value = "sha256:" + sha256(
                _canonical(configuration)
            ).hexdigest()
            receipt = self._candidate_publisher.sign_attestation(
                {
                    "contract_version": 1,
                    "entity_type": "modeld_configuration_derivation",
                    "gateway": gateway,
                    "ready_candidate_payload_digest": ready_candidate["payload_digest"],
                    "runtime_profile_digest": profile_digest,
                    "configuration_digest": configuration_digest_value,
                }
            )
            _atomic_json(destination.with_suffix(".receipt.json"), receipt)
            derived[gateway] = destination
        return derived

    @staticmethod
    def _validate_sessions(sessions: tuple[str, ...]) -> None:
        try:
            parsed = tuple(date.fromisoformat(value) for value in sessions)
        except (TypeError, ValueError) as exc:
            raise ModeldConfigurationError("CALENDAR_SESSIONS_INVALID") from exc
        if len(parsed) != 5 or len(set(parsed)) != 5 or parsed != tuple(sorted(parsed)):
            raise ModeldConfigurationError("CALENDAR_SESSIONS_INVALID")

    @staticmethod
    def _verify_package(profile: dict[str, Any], candidate: dict[str, Any]) -> None:
        manifest_path = profile.get("package_manifest_path")
        if not isinstance(manifest_path, str) or not manifest_path:
            raise ModeldConfigurationError("PACKAGE_MANIFEST_INVALID")
        envelope = _read_json(Path(manifest_path))
        manifest = envelope.get("manifest")
        if not isinstance(manifest, dict):
            raise ModeldConfigurationError("PACKAGE_MANIFEST_INVALID")
        bindings = {
            "candidate_digest": "candidate_digest",
            "package_digest": "package_digest",
            "feature_schema_digest": "feature_schema_digest",
            "thresholds_digest": "thresholds_digest",
            "evaluation_digest": "evaluation_digest",
        }
        if any(manifest.get(source) != candidate.get(target) for source, target in bindings.items()):
            raise ModeldConfigurationError("PACKAGE_IDENTITY_DRIFT")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes(), object_pairs_hook=_unique_object)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ModeldConfigurationError("JSON_STATE_INVALID") from exc
    if not isinstance(value, dict):
        raise ModeldConfigurationError("JSON_STATE_INVALID")
    return value


def _decode_object(value: bytes) -> dict[str, Any]:
    try:
        decoded = json.loads(value, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ModeldConfigurationError("MODELD_CONFIGURATION_INVALID") from exc
    if not isinstance(decoded, dict) or not decoded:
        raise ModeldConfigurationError("MODELD_CONFIGURATION_INVALID")
    return decoded


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(_canonical(value))
        handle.write(b"\n")
        handle.flush()
        os.fsync(handle.fileno())


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    _write_json(temporary, value)
    os.replace(temporary, path)
