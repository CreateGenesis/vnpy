from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from io import BytesIO
import json
from typing import Any

import pytest

from vnpy.demo_web.configuration_tests import (
    ConfigurationSectionTester,
    ProbeOutcome,
    probe_gateway_connection,
    probe_model_route,
    probe_rqdata_tick,
)


@dataclass
class RecordingProbe:
    outcome: ProbeOutcome
    calls: list[tuple[dict[str, Any], dict[str, str]]] = field(default_factory=list)

    def __call__(self, public: dict[str, Any], secrets: dict[str, str]) -> ProbeOutcome:
        self.calls.append((public, secrets))
        return self.outcome


def test_master_worker_routes_are_fixed_and_retry_has_no_fallback_route() -> None:
    master = RecordingProbe(ProbeOutcome(passed=True, fingerprint="sha256:" + "a" * 64))
    worker = RecordingProbe(ProbeOutcome(passed=True, fingerprint="sha256:" + "b" * 64))
    tester = ConfigurationSectionTester(
        current_operator_sid=lambda: "S-1-5-21-current",
        probes={"master_route": master, "worker_route": worker},
    )

    master_result = tester.test(
        "master_route",
        {"base_url": "https://model.invalid/v1", "model": "gpt-5.6-sol", "retry_count": 1},
        {"api_key": "master-secret"},
    )
    worker_result = tester.test(
        "worker_route",
        {"base_url": "https://worker.invalid/v1", "model": "deepseek-v4-flash", "retry_count": 1},
        {"api_key": "worker-secret"},
    )

    assert master_result.passed and worker_result.passed
    assert master.calls[0][0] == {
        "base_url": "https://model.invalid/v1",
        "model": "gpt-5.6-sol",
        "retry_count": 1,
    }
    assert tester.test(
        "master_route",
        {"base_url": "https://model.invalid/v1", "model": "other", "retry_count": 1},
        {},
    ).code == "MASTER_ROUTE_MODEL_MISMATCH"
    assert tester.test(
        "worker_route",
        {"base_url": "https://worker.invalid/v1", "model": "other", "retry_count": 1},
        {},
    ).code == "WORKER_ROUTE_MODEL_MISMATCH"


def test_uncertain_model_rqdata_tick_operator_and_ports_fail_closed() -> None:
    rqdata = RecordingProbe(ProbeOutcome(passed=False, code="RQDATA_TICK_NOT_ENTITLED"))
    uncertain = RecordingProbe(ProbeOutcome(passed=False, code="MODEL_OUTCOME_UNCERTAIN"))
    ports = RecordingProbe(ProbeOutcome(passed=True, fingerprint="sha256:" + "c" * 64))
    tester = ConfigurationSectionTester(
        current_operator_sid=lambda: "S-1-5-21-current",
        probes={"rqdata": rqdata, "master_route": uncertain, "ports": ports},
    )

    assert tester.test("operator", {"sid": "S-1-5-21-other"}, {}).code == "OPERATOR_SID_MISMATCH"
    assert tester.test("rqdata", {"tick_required": True}, {"api_key": "secret"}).code == "RQDATA_TICK_NOT_ENTITLED"
    assert tester.test(
        "master_route",
        {"base_url": "https://model.invalid/v1", "model": "gpt-5.6-sol", "retry_count": 1},
        {"api_key": "secret"},
    ).code == "MODEL_OUTCOME_UNCERTAIN"
    assert tester.test("ports", {"web": 8765, "agentd": 8781}, {}).passed
    assert ports.calls == [({"web": 8765, "agentd": 8781}, {})]


class HttpResponse(BytesIO):
    status = 200

    def __enter__(self) -> HttpResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def test_default_model_probe_binds_endpoint_model_and_terminal_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, str], dict[str, Any]]] = []

    def open_request(request: Any, *, timeout: float) -> HttpResponse:
        calls.append(
            (
                request.full_url,
                dict(request.header_items()),
                json.loads(request.data),
            )
        )
        return HttpResponse(
            json.dumps(
                {
                    "id": "chatcmpl-configuration-test",
                    "model": "gpt-5.6-sol",
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "OK"},
                            "finish_reason": "stop",
                        }
                    ],
                }
            ).encode()
        )

    monkeypatch.setattr("vnpy.demo_web.configuration_tests.urlopen", open_request)
    outcome = probe_model_route(
        {
            "base_url": "https://model.invalid/v1/",
            "model": "gpt-5.6-sol",
            "retry_count": 1,
        },
        {"api_key": "write-only-key"},
    )

    assert outcome.passed
    assert outcome.fingerprint and outcome.fingerprint.startswith("sha256:")
    assert calls[0][0] == "https://model.invalid/v1/chat/completions"
    assert calls[0][1]["Authorization"] == "Bearer write-only-key"
    assert calls[0][2]["model"] == "gpt-5.6-sol"


def test_model_probe_rejects_returned_model_drift_and_uncertain_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter(
        [
            {"model": "other", "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]},
            {"model": "gpt-5.6-sol", "choices": [{"message": {"content": ""}, "finish_reason": None}]},
        ]
    )

    def open_request(_request: Any, *, timeout: float) -> HttpResponse:
        return HttpResponse(json.dumps(next(responses)).encode())

    monkeypatch.setattr("vnpy.demo_web.configuration_tests.urlopen", open_request)
    public = {
        "base_url": "https://model.invalid/v1",
        "model": "gpt-5.6-sol",
        "retry_count": 0,
    }
    assert probe_model_route(public, {"api_key": "secret"}).code == "MODEL_ROUTE_IDENTITY_MISMATCH"
    assert probe_model_route(public, {"api_key": "secret"}).code == "MODEL_OUTCOME_UNCERTAIN"


def test_rqdata_probe_requires_tick_license_without_returning_credentials() -> None:
    calls: list[tuple[str, str, tuple[str, int]]] = []

    class Rqdata:
        @staticmethod
        def init(
            username: str,
            password: str,
            address: tuple[str, int],
            **_kwargs: Any,
        ) -> None:
            calls.append((username, password, address))

        @staticmethod
        def get_previous_trading_date(_today: date, n: int) -> date:
            assert n == 1
            return date(2026, 7, 27)

        @staticmethod
        def get_price(*_args: Any, **kwargs: Any) -> list[float]:
            assert kwargs["frequency"] == "tick"
            return [10.5]

    outcome = probe_rqdata_tick(
        {"endpoint": "rqdatad-pro.ricequant.com:16011", "tick_required": True},
        {"username": "operator", "password": "write-only"},
        module=Rqdata(),
    )

    assert outcome.passed
    assert calls == [("operator", "write-only", ("rqdatad-pro.ricequant.com", 16011))]
    assert "operator" not in (outcome.fingerprint or "")
    assert "write-only" not in (outcome.fingerprint or "")


def test_gateway_probe_is_bounded_and_requires_both_market_and_trading_login() -> None:
    calls: list[tuple[str, dict[str, str | int], float]] = []

    def runner(gateway: str, settings: dict[str, str | int], timeout_seconds: float) -> dict[str, Any]:
        calls.append((gateway, settings, timeout_seconds))
        return {"market_data": True, "trading": True, "server_fingerprint": "sha256:" + "d" * 64}

    public = {
        "account": "xtp-demo",
        "client_id": 11,
        "quote_address": "quote.sim.invalid",
        "quote_port": 6001,
        "trading_address": "trade.sim.invalid",
        "trading_port": 6002,
        "log_level": "INFO",
        "quote_protocol": "TCP",
    }
    outcome = probe_gateway_connection(
        "XTP",
        public,
        {"password": "write-only", "authorization_code": "write-only-auth"},
        runner=runner,
    )

    assert outcome.passed
    assert calls[0][0] == "XTP"
    assert calls[0][2] == 15.0
    assert calls[0][1]["账号"] == "xtp-demo"
    failed = probe_gateway_connection(
        "XTP",
        public,
        {"password": "write-only", "authorization_code": "write-only-auth"},
        runner=lambda *_args: {"market_data": True, "trading": False},
    )
    assert failed.code == "GATEWAY_CONNECTION_UNCERTAIN"
