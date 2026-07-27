from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from vnpy.demo_web.run_service import BrokerSimulationRunHost


def digest(label: str) -> str:
    return f"sha256:{sha256(label.encode()).hexdigest()}"


@dataclass
class Account:
    gateway_name: str = "XTP"
    balance: float = 1_000_000.0


class FakeMainEngine:
    def get_all_accounts(self) -> list[Account]:
        return [Account()]

    def get_all_positions(self) -> list[Any]:
        return []

    def get_all_trades(self) -> list[Any]:
        return []

    def get_all_active_orders(self) -> list[Any]:
        return []


def test_run_host_owns_prepare_start_pause_and_stop_without_agent_calls(
    tmp_path: Path,
) -> None:
    install_state(tmp_path)
    host = BrokerSimulationRunHost(tmp_path, "XTP", main_engine=FakeMainEngine())
    campaign_id = "b53bc59c-c626-4f16-8a3e-a3185c7dad23"
    campaign_digest = digest("campaign")
    common = {
        "contract_version": 1,
        "gateway": "XTP",
        "run_digest": host.run_digest,
    }

    prepared = host.handle(
        "run.prepare_campaign.v1",
        {
            **common,
            "campaign_id": campaign_id,
            "campaign_digest": campaign_digest,
            "candidate_digest": digest("candidate"),
            "idempotency_key": "prepare-campaign-0001",
        },
    )
    started = host.handle(
        "run.start_campaign.v1",
        {
            **common,
            "campaign_id": campaign_id,
            "campaign_digest": campaign_digest,
            "idempotency_key": "start-campaign-0001",
        },
    )
    status = host.handle("run.status.v1", common)
    paused = host.handle(
        "run.pause_campaign.v1",
        {
            **common,
            "campaign_digest": campaign_digest,
            "idempotency_key": "pause-campaign-0001",
        },
    )
    stopped = host.handle(
        "run.emergency_stop.v1",
        {**common, "idempotency_key": "stop-campaign-0001"},
    )

    assert prepared["state"] == "prepared"
    assert started["state"] == "active"
    assert status["data"]["connection_state"] == "connected"
    assert status["data"]["reconciliation_state"] == "blocked"
    assert "SIGNED_FEE_LEDGER_UNAVAILABLE" in status["data"]["incidents"]
    assert paused["state"] == "contained"
    assert stopped["state"] == "contained"
    serialized = json.dumps([prepared, started, status, paused, stopped]).lower()
    for forbidden in (
        "account_id",
        "credential_ref",
        "order_request",
        "send_order",
        "cancel_order",
        "risk_mutation",
        "lifecycle_apply",
    ):
        assert forbidden not in serialized


def test_run_host_rejects_gateway_settings_that_drift_from_approved_binding(
    tmp_path: Path,
) -> None:
    install_state(tmp_path)
    settings_path = tmp_path / ".demo-secrets" / "xtp-settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings["交易地址"] = "changed.example.invalid"
    settings_path.write_text(json.dumps(settings), encoding="utf-8")

    try:
        BrokerSimulationRunHost(tmp_path, "XTP", main_engine=FakeMainEngine())
    except ValueError as exc:
        assert str(exc) == "RUN_BINDING_SETTINGS_DRIFT"
    else:
        raise AssertionError("gateway settings drift was accepted")


def install_state(root: Path) -> None:
    state = root / ".demo-state"
    secrets = root / ".demo-secrets"
    state.mkdir()
    secrets.mkdir()
    candidate = {
        "contract_version": 1,
        "ready": True,
        "candidate_digest": digest("candidate"),
        "author_lineage_digest": digest("author"),
        "package_digest": digest("package"),
        "configuration_digest": digest("configuration"),
        "policy_digest": digest("policy"),
        "symbols": ["600000.SH"],
        "calendar_sessions": [
            "2026-07-27",
            "2026-07-28",
            "2026-07-29",
            "2026-07-30",
            "2026-07-31",
        ],
        "lifecycle_revision": 1,
    }
    settings = {
        "账号": "simulation-account",
        "行情地址": "quote.simulation.invalid",
        "行情端口": 10001,
        "交易地址": "trade.simulation.invalid",
        "交易端口": 10002,
    }
    bindings = [
        {
            "name": "XTP",
            "environment": "broker_simulation",
            "server_fingerprint": payload_digest(
                {
                    "行情地址": settings["行情地址"],
                    "行情端口": settings["行情端口"],
                    "交易地址": settings["交易地址"],
                    "交易端口": settings["交易端口"],
                }
            ),
            "account_fingerprint": payload_digest({"账号": settings["账号"]}),
            "credential_ref": ".demo-secrets/xtp-settings.json",
        }
    ]
    operator = {"contract_version": 1, "operator_identity_digest": digest("operator")}
    (state / "ready-candidate.json").write_text(json.dumps(candidate), encoding="utf-8")
    (secrets / "gateway-bindings.json").write_text(json.dumps(bindings), encoding="utf-8")
    (secrets / "operator.json").write_text(json.dumps(operator), encoding="utf-8")
    (secrets / "xtp-settings.json").write_text(json.dumps(settings), encoding="utf-8")


def payload_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{sha256(encoded).hexdigest()}"
