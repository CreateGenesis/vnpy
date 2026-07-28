from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import importlib
import json
from pathlib import Path
import subprocess
from time import monotonic, sleep, time_ns
from typing import Any

from blake3 import blake3
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from vnpy.agent_bridge.native_bridge import NativeModelBridge
from vnpy.event import Event
from vnpy.model_production.app_engine import BrokerSimulationCoordinator
from vnpy.model_production.broker_simulation import BrokerSimulationAuthority, GatewayBinding
from vnpy.model_production.broker_simulation_model_loop import BrokerSimulationModelLoop
from vnpy.model_production.engine import AuthoritativeDecisionEngine
from vnpy.model_production.execution import BrokerSimulationExecutor
from vnpy.model_production.journal import ModelProductionJournal
from vnpy.model_production.reconciliation import ReconciliationManager
from vnpy.model_production.safety import HardSafetyController
from vnpy.trader.constant import Direction, Exchange, Product
from vnpy.trader.event import EVENT_TICK
from vnpy.trader.object import AccountData, ContractData, PositionData, TickData


IDENTITY_WASM = bytes.fromhex(
    "0061736d0d00010001a6020061736d01000000010f0260027f7f017c60047f7f7f7f017f"
    "030302000105040101010107210305696e66657200000c636162695f7265616c6c6f6300"
    "01066d656d6f727902000adc0102d401002001410a470440000b44000000000000000020"
    "0041006a2b030044000000000000f03fa2a0200041086a2b0300440000000000000000a2"
    "a0200041106a2b0300440000000000000000a2a0200041186a2b03004400000000000000"
    "00a2a0200041206a2b0300440000000000000000a2a0200041286a2b0300440000000000"
    "000000a2a0200041306a2b0300440000000000000000a2a0200041386a2b030044000000"
    "0000000000a2a0200041c0006a2b0300440000000000000000a2a0200041c8006a2b0300"
    "440000000000000000a2a00b040041000b0204010000000627030000010005696e666572"
    "000001000c636162695f7265616c6c6f6300020100066d656d6f72790711027075400108"
    "6665617475726573000075080a010000000203000401010b0b010005696e666572010000"
)
FEATURE_NAMES = (
    "last_price",
    "bid_price_1",
    "ask_price_1",
    "volume",
    "turnover",
    "open_interest",
    "open_price",
    "high_price",
    "low_price",
    "pre_close",
)


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def digest_bytes(value: bytes) -> str:
    return f"blake3:{blake3(value).hexdigest()}"


def named_digest(value: str) -> str:
    return digest_bytes(value.encode())


def sha_digest(value: str) -> str:
    return f"sha256:{sha256(value.encode()).hexdigest()}"


class EventEngineStub:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}

    def register(self, event_type: str, handler: Any) -> None:
        self.handlers[event_type] = handler

    def unregister(self, event_type: str, handler: Any) -> None:
        if self.handlers.get(event_type) == handler:
            del self.handlers[event_type]


class MainEngineStub:
    def __init__(self) -> None:
        self.calls: list[tuple[object, str]] = []
        self.account = AccountData(
            gateway_name="XTP",
            accountid="simulation",
            balance=1_000_000,
            frozen=0,
        )
        self.position = PositionData(
            gateway_name="XTP",
            symbol="600000",
            exchange=Exchange.SSE,
            direction=Direction.LONG,
            volume=200,
            yd_volume=100,
            price=10,
        )
        self.contract = ContractData(
            gateway_name="XTP",
            symbol="600000",
            exchange=Exchange.SSE,
            name="Pudong Bank",
            product=Product.EQUITY,
            size=1,
            pricetick=0.01,
            min_volume=100,
        )

    def get_all_accounts(self) -> list[AccountData]:
        return [self.account]

    def get_all_positions(self) -> list[PositionData]:
        return [self.position]

    def get_contract(self, vt_symbol: str) -> ContractData | None:
        return self.contract if vt_symbol == "600000.SSE" else None

    def send_order(self, request: object, gateway_name: str) -> str:
        self.calls.append((request, gateway_name))
        return f"{gateway_name}.order-{len(self.calls)}"


def create_signed_runtime_fixture(
    root: Path, modeld: Path
) -> tuple[Path, Path, str, str, str]:
    package_root = root / "package"
    model_path = package_root / "model" / "model.wasm"
    model_path.parent.mkdir(parents=True)
    model_path.write_bytes(IDENTITY_WASM)

    private_key = Ed25519PrivateKey.from_private_bytes(bytes([17]) * 32)
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    feature_schema_digest = f"sha256:{sha256(json_bytes({'contract_version': 1, 'features': list(FEATURE_NAMES)})).hexdigest()}"
    blob_identity = {
        "role": "executable",
        "path": "model/model.wasm",
        "media_type": "application/wasm",
        "bytes": len(IDENTITY_WASM),
        "digest": digest_bytes(IDENTITY_WASM),
    }
    manifest = {
        "contract_version": 1,
        "package_id": "release-cross-process-fixture",
        "package_digest": "",
        "candidate_digest": named_digest("candidate"),
        "engine": "wasm-component-v1",
        "runtime_abi_digest": named_digest("runtime-abi"),
        "runtime_digest": named_digest("runtime"),
        "cpu_feature_class": "x86-64-v2",
        "feature_schema_digest": feature_schema_digest,
        "context_schema_digest": named_digest("context-schema"),
        "decision_schema_digest": named_digest("decision-schema"),
        "thresholds_digest": named_digest("thresholds"),
        "state_schema_digest": named_digest("state-schema"),
        "max_state_bytes": 1024 * 1024,
        "training_or_calibration_digest": named_digest("training"),
        "evaluation_digest": named_digest("evaluation"),
        "pre_training_review_digest": named_digest("pre-training-review"),
        "release_policy_digest": named_digest("release-policy"),
        "sbom_digest": named_digest("sbom"),
        "dependency_lock_digest": named_digest("dependency-lock"),
        "build_environment_digest": named_digest("build-environment"),
        "rollback_targets": [named_digest("rollback")],
        "blobs": [blob_identity],
        "signer_identity": "release-test",
        "key_fingerprint": digest_bytes(public_key),
        "signature": "",
    }
    signable = json_bytes(manifest)
    manifest["package_digest"] = digest_bytes(signable)
    manifest["signature"] = private_key.sign(signable).hex()
    manifest_path = root / "manifest.json"
    manifest_path.write_bytes(json_bytes({"manifest": manifest}))

    model = {
        "engine": {
            "kind": "wasm",
            "profile": {
                "max_module_bytes": 16 * 1024 * 1024,
                "required_export": "infer",
                "allow_wasi": False,
                "allow_host_imports": False,
            },
            "limits": {
                "fuel": 1_000_000,
                "epoch_ticks": 10,
                "memory_bytes": 64 * 1024 * 1024,
                "table_elements": 1024,
                "stack_bytes": 2 * 1024 * 1024,
                "tensor_elements": 65_536,
                "state_bytes": 8 * 1024 * 1024,
                "output_bytes": 64 * 1024,
                "queue_depth": 1024,
                "process_count": 1,
                "timeout_ms": 50,
            },
            "warm_instances": 1,
            "feature_count": len(FEATURE_NAMES),
        },
        "score_policy": {
            "threshold": 0.8,
            "quantity": 100,
            "limit_price_feature_index": 2,
            "maximum_limit_price_micros": 1_000_000_000,
            "decision_ttl_ns": 90_000_000,
        },
    }
    configuration_digest = digest_bytes(json_bytes(model))
    policy_digest = named_digest("broker-simulation-policy")
    profile = {
        "contract_version": 1,
        "package_manifest_path": str(manifest_path),
        "package_blob_root": str(package_root),
        "trust_root": {
            "signer_identity": "release-test",
            "public_key_hex": public_key.hex(),
        },
        "model": model,
        "bridge_capacity": 8,
        "bridge_slot_size": 65_536 + 256,
        "input_producer_id": "vnpy-to-agentd",
        "input_producer_epoch": 1,
        "process_epoch": 1,
        "fast_action_qualified": True,
        "maximum_inference_latency_ns": 50_000_000,
        "poll_interval_ms": 1,
    }
    candidate = {
        "contract_version": 1,
        "ready": True,
        "candidate_digest": manifest["candidate_digest"],
        "author_lineage_digest": named_digest("author-lineage"),
        "package_digest": manifest["package_digest"],
        "configuration_digest": configuration_digest,
        "policy_digest": policy_digest,
        "symbols": ["600000.SH"],
        "calendar_sessions": [
            "2026-07-27",
            "2026-07-28",
            "2026-07-29",
            "2026-07-30",
            "2026-07-31",
        ],
        "lifecycle_revision": 8,
    }
    profile_path = root / "modeld-runtime-profile.json"
    candidate_path = root / "ready-candidate.json"
    profile_path.write_bytes(json_bytes(profile))
    candidate_path.write_bytes(json_bytes(candidate))
    bridge_root = root / "runs" / "XTP" / "model-bridge"
    prepared = subprocess.run(
        [
            modeld,
            "prepare",
            "--profile",
            profile_path,
            "--candidate",
            candidate_path,
            "--gateway",
            "XTP",
            "--bridge-root",
            bridge_root,
        ],
        check=True,
        capture_output=True,
    )
    configuration_path = root / "modeld.json"
    configuration_path.write_bytes(prepared.stdout)
    subprocess.run(
        [modeld, "check", "--config", configuration_path],
        check=True,
        capture_output=True,
    )
    return (
        configuration_path,
        bridge_root,
        manifest["package_digest"],
        configuration_digest,
        policy_digest,
    )


def test_release_modeld_round_trip_keeps_broker_authority_in_vnpy(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[3]
    rust_root = project_root / "auto-tride-rust"
    modeld = rust_root / "target" / "release" / "modeld.exe"
    release_bridge = rust_root / "target" / "release" / "vnpy_bridge_py.dll"
    native_module = Path(importlib.import_module("vnpy_bridge_py").__file__).resolve()
    assert modeld.is_file()
    assert release_bridge.read_bytes() == native_module.read_bytes()

    config, bridge_root, package_digest, configuration_digest, policy_digest = (
        create_signed_runtime_fixture(tmp_path, modeld)
    )
    process = subprocess.Popen(
        [modeld, "serve", "--config", config],
        cwd=rust_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    loop: BrokerSimulationModelLoop | None = None
    market_time = datetime.now()
    try:
        health_line = process.stdout.readline() if process.stdout is not None else ""
        assert health_line, process.stderr.read() if process.stderr is not None else ""
        health = json.loads(health_line)
        assert health["status"] == "ready"
        assert health["broker_authority"] is False
        assert health["risk_authority"] is False
        assert health["account_authority"] is False

        database = tmp_path / "runtime.sqlite"
        server = sha_digest("xtp-server")
        account = sha_digest("xtp-account")
        binding = GatewayBinding.create(
            gateway="XTP",
            environment="broker_simulation",
            server_fingerprint=server,
            account_fingerprint=account,
            credential_ref="credential:xtp",
            process_identity="vnpy-demo-xtp",
            rpc_endpoint="127.0.0.1:17801",
            state_store_path=str(tmp_path),
            created_at_ms=1,
            allowed_server_fingerprints=frozenset({server}),
            allowed_account_fingerprints=frozenset({account}),
        )
        authority = BrokerSimulationAuthority(database)
        authority.create_campaign(
            campaign_id="campaign-1",
            candidate_digest=named_digest("candidate"),
            package_digest=package_digest,
            configuration_digest=configuration_digest,
            policy_digest=policy_digest,
            symbol_set=("600000.SH",),
            calendar_sessions=(
                "2026-07-27",
                "2026-07-28",
                "2026-07-29",
                "2026-07-30",
                "2026-07-31",
            ),
            operator_identity_digest=sha_digest("operator"),
            bindings=(binding,),
            lifecycle_revision=8,
            now_ms=1,
        )
        authority.start_campaign("campaign-1", now_ms=2)
        reconciliation = ReconciliationManager(database)
        safety = HardSafetyController()
        main_engine = MainEngineStub()
        coordinator = BrokerSimulationCoordinator(
            campaign_id="campaign-1",
            run_id="campaign-1:xtp",
            binding=binding,
            authority=authority,
            decision_engine=AuthoritativeDecisionEngine(
                journal=ModelProductionJournal(database),
                safety=safety,
                expected_producer_id="modeld:broker-xtp-slot",
                active_package_digest=package_digest,
                lifecycle_revision=8,
                stage="broker_simulation",
            ),
            executor=BrokerSimulationExecutor(
                main_engine=main_engine,
                binding=binding,
                reconciliation=reconciliation,
            ),
            reconciliation=reconciliation,
            safety=safety,
        )
        events = EventEngineStub()
        loop = BrokerSimulationModelLoop(
            bridge=NativeModelBridge(bridge_root, critical_capacity=8),
            coordinator=coordinator,
            reconciliation=reconciliation,
            event_engine=events,
            main_engine=main_engine,
            database=database,
            gateway="XTP",
            package_digest=package_digest,
            configuration_digest=configuration_digest,
            policy_digest=policy_digest,
            runtime_slot="broker-xtp-slot",
            lifecycle_revision=8,
            symbols=("600000.SH",),
            now_ns=time_ns,
            session_open=lambda _timestamp: True,
        )
        loop.start()
        events.handlers[EVENT_TICK](
            Event(
                EVENT_TICK,
                TickData(
                    gateway_name="XTP",
                    symbol="600000",
                    exchange=Exchange.SSE,
                    datetime=market_time,
                    last_price=10,
                    bid_price_1=9.99,
                    ask_price_1=10.01,
                    volume=1_000,
                    turnover=10_000,
                    limit_down=9,
                    limit_up=11,
                    open_price=9.95,
                    high_price=10.05,
                    low_price=9.9,
                    pre_close=9.9,
                ),
            )
        )
        deadline = monotonic() + 5
        snapshot = loop.snapshot()
        while (
            not main_engine.calls or snapshot.broker_submission_count != 1
        ) and monotonic() < deadline:
            exit_code = process.poll()
            assert exit_code is None, (
                process.stderr.read() if process.stderr is not None else f"modeld exited {exit_code}"
            )
            sleep(0.01)
            snapshot = loop.snapshot()

        assert len(main_engine.calls) == 1
        request, gateway = main_engine.calls[0]
        assert gateway == "XTP"
        assert request.reference.startswith("model:intent-")
        assert snapshot.broker_submission_count == 1
        assert snapshot.agent_calls == 0
        assert snapshot.provider_calls == 0
    finally:
        if loop is not None:
            loop.close()
        process.terminate()
        process.wait(timeout=5)
