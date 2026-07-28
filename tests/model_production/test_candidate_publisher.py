from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any

import pytest
from blake3 import blake3
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from vnpy.model_production.candidate_publisher import (
    CandidateAdmissionError,
    HistoricalCandidateGate,
    ReadyCandidatePublisher,
)
from vnpy.model_production.lifecycle import LifecycleStateV3
from vnpy.model_production.modeld_configuration import (
    ModeldConfigurationError,
    ModeldConfigurationPublisher,
)


def digest(label: str) -> str:
    return f"sha256:{sha256(label.encode()).hexdigest()}"


def lifecycle() -> LifecycleStateV3:
    return LifecycleStateV3(
        candidate_digest=digest("candidate"),
        package_digest=digest("package"),
        configuration_digest=digest("configuration"),
        policy_digest=digest("policy"),
        evidence_bundle_digest=digest("evidence-bundle"),
        stage="broker_simulation",
        revision=4,
        state="ready",
    )


def candidate() -> dict[str, object]:
    return {
        "candidate_digest": digest("candidate"),
        "package_digest": digest("package"),
        "configuration_digest": digest("configuration"),
        "policy_digest": digest("policy"),
        "data_snapshot_digest": digest("data"),
        "feature_schema_digest": digest("features"),
        "thresholds_digest": digest("thresholds"),
        "review_digest": digest("review"),
        "evaluation_digest": digest("evaluation"),
        "runtime_profile_digest": digest("runtime"),
        "rollback_digest": digest("rollback"),
        "author_lineage_digest": digest("author"),
        "symbols": ["600000.SH", "000001.SZ"],
        "valid_until_ms": 2_000_000,
        "lifecycle_revision": 4,
    }


def historical_gate() -> HistoricalCandidateGate:
    return HistoricalCandidateGate(
        contract_version=1,
        metric_owner="harness",
        evaluation_digest=digest("evaluation"),
        eligible=True,
        reason_codes=(),
    )


def publisher(root: Path) -> ReadyCandidatePublisher:
    return ReadyCandidatePublisher(
        root,
        private_key=Ed25519PrivateKey.from_private_bytes(bytes(range(32))),
        publisher_identity="vnpy-admission",
        clock_ms=lambda: 1_000_000,
    )


def test_publication_requires_exact_historical_gate_and_ready_lifecycle(tmp_path: Path) -> None:
    service = publisher(tmp_path)

    record = service.publish(
        candidate(), historical_gate=historical_gate(), lifecycle_state=lifecycle()
    )

    assert record["state"] == "ready"
    assert record["lifecycle_revision"] == lifecycle().revision
    assert service.verify(record, expected_bindings=candidate())

    rejected = (
        replace(historical_gate(), metric_owner="model"),
        replace(historical_gate(), eligible=False, reason_codes=("HELD_OUT_NET_PROFIT_NOT_POSITIVE",)),
        replace(historical_gate(), evaluation_digest=digest("other-evaluation")),
    )
    for gate in rejected:
        with pytest.raises(CandidateAdmissionError, match="HISTORICAL_CANDIDATE_GATE_REQUIRED"):
            service.publish(candidate(), historical_gate=gate, lifecycle_state=lifecycle())

    drifted_states = (
        replace(lifecycle(), stage="shadow", state="active"),
        replace(lifecycle(), candidate_digest=digest("other-candidate")),
        replace(lifecycle(), revision=5),
    )
    for state in drifted_states:
        with pytest.raises(CandidateAdmissionError, match="CANDIDATE_LIFECYCLE_NOT_READY"):
            service.publish(candidate(), historical_gate=historical_gate(), lifecycle_state=state)


def test_invalidation_and_rollback_are_signed_append_only_transitions(tmp_path: Path) -> None:
    service = publisher(tmp_path)
    first = service.publish(
        candidate(), historical_gate=historical_gate(), lifecycle_state=lifecycle()
    )
    second_candidate = {**candidate(), "candidate_digest": digest("candidate-2")}
    second_lifecycle = replace(lifecycle(), candidate_digest=digest("candidate-2"), revision=5)
    second_candidate["lifecycle_revision"] = 5
    second = service.publish(
        second_candidate,
        historical_gate=historical_gate(),
        lifecycle_state=second_lifecycle,
    )

    invalidated = service.invalidate(expected_revision=2)
    assert invalidated["state"] == "invalidated"
    assert invalidated["revision"] == 3
    assert not service.verify(invalidated)

    restored = service.rollback(expected_revision=3)
    assert restored["state"] == "rollback"
    assert restored["candidate_digest"] == first["candidate_digest"]
    assert restored["revision"] == 4
    assert service.verify(restored, expected_bindings=candidate())
    assert service.read_rollback()["publication_id"] == second["publication_id"]


def runtime_fixture(root: Path) -> tuple[dict[str, Any], Path]:
    manifest_path = root / "package-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "manifest": {
                    "candidate_digest": digest("candidate"),
                    "package_digest": digest("package"),
                    "feature_schema_digest": digest("features"),
                    "thresholds_digest": digest("thresholds"),
                    "evaluation_digest": digest("evaluation"),
                }
            }
        ),
        encoding="utf-8",
    )
    model = {
        "engine": {
            "kind": "wasm",
            "profile": {"max_module_bytes": 1, "required_export": "infer", "allow_wasi": False, "allow_host_imports": False},
            "limits": {
                "fuel": 1,
                "epoch_ticks": 1,
                "memory_bytes": 1,
                "table_elements": 1,
                "stack_bytes": 1,
                "tensor_elements": 1,
                "state_bytes": 1,
                "output_bytes": 1,
                "queue_depth": 1,
                "process_count": 1,
                "timeout_ms": 1,
            },
            "warm_instances": 1,
            "feature_count": 2,
        },
        "score_policy": {
            "threshold": 0.8,
            "quantity": 100,
            "limit_price_feature_index": 1,
            "maximum_limit_price_micros": 1_000_000,
            "decision_ttl_ns": 90_000_000,
        },
    }
    profile: dict[str, Any] = {
        "contract_version": 1,
        "package_manifest_path": str(manifest_path),
        "package_blob_root": str(root / "package"),
        "trust_root": {"signer_identity": "release-audit", "public_key_hex": "11" * 32},
        "model": model,
        "bridge_capacity": 8,
        "bridge_slot_size": 65_792,
        "input_producer_id": "vnpy-to-agentd",
        "input_producer_epoch": 1,
        "process_epoch": 1,
        "fast_action_qualified": True,
        "maximum_inference_latency_ns": 50_000_000,
        "poll_interval_ms": 1,
    }
    return profile, manifest_path


def test_modeld_configuration_is_derived_for_each_gateway_from_same_signed_candidate(
    tmp_path: Path,
) -> None:
    service = publisher(tmp_path / "publication")
    profile, _ = runtime_fixture(tmp_path)
    canonical_model = json.dumps(profile["model"], sort_keys=True, separators=(",", ":")).encode()
    admitted = candidate()
    admitted["configuration_digest"] = f"blake3:{blake3(canonical_model).hexdigest()}"
    admitted["runtime_profile_digest"] = "sha256:" + sha256(
        json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    admitted_lifecycle = replace(
        lifecycle(), configuration_digest=str(admitted["configuration_digest"])
    )
    record = service.publish(
        admitted, historical_gate=historical_gate(), lifecycle_state=admitted_lifecycle
    )

    calls: list[list[str]] = []

    def run(command: list[str], **_: object) -> CompletedProcess[bytes]:
        calls.append(command)
        if command[1] == "prepare":
            gateway = command[command.index("--gateway") + 1]
            payload = json.dumps({"gateway": gateway, "candidate_digest": record["candidate_digest"]}).encode()
            return CompletedProcess(command, 0, stdout=payload, stderr=b"")
        return CompletedProcess(command, 0, stdout=b"", stderr=b"")

    profile_path = tmp_path / "runtime-profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    derived = ModeldConfigurationPublisher(
        tmp_path / "derived",
        modeld_executable=Path("modeld.exe"),
        candidate_publisher=service,
        command_runner=run,
    ).derive(
        record,
        runtime_profile_path=profile_path,
        calendar_sessions=(
            "2026-07-27",
            "2026-07-28",
            "2026-07-29",
            "2026-07-30",
            "2026-07-31",
        ),
        gateways=("XTP", "TORA"),
    )

    assert set(derived) == {"XTP", "TORA"}
    assert json.loads(derived["XTP"].read_text(encoding="utf-8"))["gateway"] == "XTP"
    assert json.loads(derived["TORA"].read_text(encoding="utf-8"))["gateway"] == "TORA"
    assert [call[1] for call in calls] == ["prepare", "check", "prepare", "check"]
    assert service.verify(record, expected_bindings=admitted)


def test_modeld_derivation_rejects_tamper_invalidation_profile_drift_and_unknown_gateway(
    tmp_path: Path,
) -> None:
    service = publisher(tmp_path / "publication")
    profile, _ = runtime_fixture(tmp_path)
    canonical_model = json.dumps(profile["model"], sort_keys=True, separators=(",", ":")).encode()
    admitted = candidate()
    admitted["configuration_digest"] = f"blake3:{blake3(canonical_model).hexdigest()}"
    admitted["runtime_profile_digest"] = "sha256:" + sha256(
        json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    record = service.publish(
        admitted,
        historical_gate=historical_gate(),
        lifecycle_state=replace(lifecycle(), configuration_digest=str(admitted["configuration_digest"])),
    )
    profile_path = tmp_path / "runtime-profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    configuration = ModeldConfigurationPublisher(
        tmp_path / "derived",
        modeld_executable=Path("modeld.exe"),
        candidate_publisher=service,
        command_runner=lambda command, **_: CompletedProcess(command, 0, stdout=b"{}", stderr=b""),
    )
    arguments = {
        "runtime_profile_path": profile_path,
        "calendar_sessions": ("2026-07-27", "2026-07-28", "2026-07-29", "2026-07-30", "2026-07-31"),
        "gateways": ("XTP",),
    }

    with pytest.raises(ModeldConfigurationError, match="READY_CANDIDATE_SIGNATURE_INVALID"):
        configuration.derive({**record, "symbols": ["601398.SH"]}, **arguments)
    profile["poll_interval_ms"] = 2
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    with pytest.raises(ModeldConfigurationError, match="RUNTIME_PROFILE_IDENTITY_DRIFT"):
        configuration.derive(record, **arguments)
    with pytest.raises(ModeldConfigurationError, match="GATEWAY_UNSUPPORTED"):
        configuration.derive(record, **{**arguments, "gateways": ("CTP",)})
    service.invalidate(expected_revision=1)
    with pytest.raises(ModeldConfigurationError, match="READY_CANDIDATE_NOT_CURRENT"):
        configuration.derive(record, **arguments)
