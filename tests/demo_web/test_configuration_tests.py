from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from vnpy.demo_web.configuration_tests import (
    ConfigurationSectionTester,
    ProbeOutcome,
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
