import importlib
import importlib.machinery
import importlib.util
import gc
import json
from pathlib import Path
from struct import unpack_from
import sys

from vnpy.agent_bridge import MmapRing


SLOT_SIZE = 65_536 + 256


def _native_bridge_class() -> type:
    try:
        return importlib.import_module("vnpy_bridge_py").NativeBridge
    except ImportError:
        workspace = Path(__file__).resolve().parents[3]
        candidates = sorted(
            list((workspace / "auto-tride-rust" / "target" / "debug" / "deps").glob("*vnpy_bridge_py*.dll"))
            + list((workspace / "auto-tride-rust" / "target" / "debug" / "deps").glob("*vnpy_bridge_py*.so"))
        )
        if not candidates:
            raise RuntimeError("build vnpy-bridge-py before running native bridge tests")
        loader = importlib.machinery.ExtensionFileLoader("vnpy_bridge_py", str(candidates[-1]))
        spec = importlib.util.spec_from_loader("vnpy_bridge_py", loader)
        if spec is None:
            raise RuntimeError("unable to load vnpy-bridge-py extension")
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        sys.modules["vnpy_bridge_py"] = module
        return module.NativeBridge


def test_rust_python_golden_vector_and_bidirectional_mmap(tmp_path: Path) -> None:
    native = _native_bridge_class()(str(tmp_path), "loop", "loop", 4, 4)
    python_ring = MmapRing(tmp_path / "loop-critical.ring", 4, SLOT_SIZE)
    payload = {
        "event_type": "observer.wakeup",
        "artifact": {"digest": "a" * 64, "media_type": "application/json"},
    }

    assert native.publish(
        json.dumps(payload),
        "correlation-001",
        1_000,
        2_000,
        True,
        "wakeup",
    ) == 1
    raw_frame = python_ring.try_consume()
    assert raw_frame is not None
    assert raw_frame[:4] == b"ATF2"
    assert unpack_from("<H", raw_frame, 4)[0] == 2
    assert raw_frame[6:10] == bytes((2, 1, 2, 1))
    assert unpack_from("<Q", raw_frame, 18)[0] == 1

    python_ring.try_publish(raw_frame)
    consumed = json.loads(native.consume(1_001))
    assert consumed["payload"] == payload
    assert consumed["lane"] == "critical"
    assert consumed["lifecycle"] == "published"
    assert len(consumed["payload_hash"]) == 64

    assert native.ack(1, "correlation-001", 1_002) == 2
    ack_frame = python_ring.try_consume()
    assert ack_frame is not None
    assert ack_frame[9] == 2
    python_ring.try_publish(ack_frame)
    ack = json.loads(native.consume(1_003))
    assert ack["payload"]["event_type"] == "bridge.ack"
    assert ack["payload"]["acknowledged_sequence"] == 1
    assert ack["lifecycle"] == "acked"
    python_ring.close()


def test_diagnostics_artifact_references_health_and_gate_are_research_only(tmp_path: Path) -> None:
    native = _native_bridge_class()(str(tmp_path), "diagnostic", "diagnostic", 4, 4)
    native.publish(
        json.dumps({"event_type": "bridge.diagnostic", "artifact_digest": "b" * 64}),
        "diagnostic-001",
        2_000,
        -1,
        True,
        "diagnostic",
    )
    diagnostic = json.loads(native.consume(2_001))
    assert diagnostic["payload"]["artifact_digest"] == "b" * 64

    health = json.loads(native.health())
    assert health["network_calls"] == 0
    assert health["authority"] == "research_only"

    gate = json.loads(
        native.gate(
            json.dumps(
                {
                    "source": "vnpy.market-data.v1",
                    "kind": "tick",
                    "symbol": "000001.sz",
                    "timestamp_ms": 3_000,
                    "price": 10.0,
                    "volume": 100_000.0,
                    "payload": {"fixture": "observer-closed-loop-v1"},
                }
            ),
            3_001,
            "gate-001",
        )
    )
    assert gate["status"] == "completed"
    assert gate["trading_side_effects"] == 0


def test_native_model_channel_replays_unacked_then_emits_recovery_complete(tmp_path: Path) -> None:
    native_class = _native_bridge_class()
    native = native_class(str(tmp_path), "model-loop-out", "model-loop-in", 4, 4)
    payload = json.dumps(
        {"contract_version": 1, "entity_type": "model_input", "symbol": "000001.SZ"}
    ).encode()
    assert native.publish_model_input(payload, "model-correlation-1", 1_000, 2_000) == 1
    first = json.loads(native.consume_model_input(1_001))
    assert first["frame_type"] == "model_input"
    assert first["sequence"] == 1
    assert first["replayed"] is False
    assert first["payload"]["symbol"] == "000001.SZ"

    del native
    gc.collect()
    recovered = native_class(str(tmp_path), "model-loop-out", "model-loop-in", 4, 4)
    replayed = json.loads(recovered.consume_model_input(1_002))
    assert replayed["sequence"] == 1
    assert replayed["replayed"] is True
    recovered.ack_model_input(
        replayed["producer_id"],
        replayed["producer_epoch"],
        replayed["sequence"],
        1_003,
    )

    assert recovered.model_input_recovery_complete("model-recovery-1", 1_004) == 2
    marker = json.loads(recovered.consume_model_input(1_005))
    assert marker["frame_type"] == "recovery_complete"
    assert marker["sequence"] == 2
    assert marker["payload"] is None
    recovered.ack_model_input(
        marker["producer_id"], marker["producer_epoch"], marker["sequence"], 1_006
    )
    assert recovered.replay_model_pending() == 0
