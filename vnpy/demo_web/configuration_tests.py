"""Fail-closed section test adapters for configuration activation receipts."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import socket
from typing import Any, Callable, Protocol

from .gateway_settings import GatewaySettingsError, map_gateway_settings


@dataclass(frozen=True)
class ProbeOutcome:
    passed: bool
    code: str | None = None
    fingerprint: str | None = None


class SectionProbe(Protocol):
    def __call__(self, public: dict[str, Any], secrets: dict[str, str]) -> ProbeOutcome: ...


class ConfigurationSectionTester:
    """Validate fixed identities before invoking bounded section probes."""

    _SECTIONS = frozenset(
        {"operator", "ports", "rqdata", "master_route", "worker_route", "xtp", "tora"}
    )

    def __init__(
        self,
        *,
        current_operator_sid: Callable[[], str],
        probes: dict[str, SectionProbe] | None = None,
    ) -> None:
        self._current_operator_sid = current_operator_sid
        self._probes = dict(probes or {})

    def test(
        self,
        section: str,
        public: dict[str, Any],
        secrets: dict[str, str],
    ) -> ProbeOutcome:
        if section not in self._SECTIONS:
            return ProbeOutcome(False, "CONFIGURATION_SECTION_DENIED")
        if section == "operator":
            sid = public.get("sid")
            if not isinstance(sid, str) or sid != self._current_operator_sid():
                return ProbeOutcome(False, "OPERATOR_SID_MISMATCH")
            return ProbeOutcome(True, fingerprint=_digest({"sid": sid}))
        if section in {"master_route", "worker_route"}:
            allowed = {"base_url", "model", "retry_count"}
            if set(public) != allowed:
                return ProbeOutcome(False, "MODEL_ROUTE_SHAPE_INVALID")
            required_model = "gpt-5.6-sol" if section == "master_route" else "deepseek-v4-flash"
            code = "MASTER_ROUTE_MODEL_MISMATCH" if section == "master_route" else "WORKER_ROUTE_MODEL_MISMATCH"
            if public.get("model") != required_model:
                return ProbeOutcome(False, code)
            if not isinstance(public.get("retry_count"), int) or not 0 <= public["retry_count"] <= 3:
                return ProbeOutcome(False, "MODEL_ROUTE_RETRY_INVALID")
        if section == "rqdata" and public.get("tick_required") is not True:
            return ProbeOutcome(False, "RQDATA_TICK_REQUIRED")
        if section in {"xtp", "tora"}:
            try:
                map_gateway_settings(section.upper(), public, secrets)
            except GatewaySettingsError as exc:
                return ProbeOutcome(False, str(exc))
        probe = self._probes.get(section)
        if probe is not None:
            try:
                result = probe(dict(public), dict(secrets))
            except Exception:
                return ProbeOutcome(False, "CONFIGURATION_TEST_FAILED")
            if not isinstance(result, ProbeOutcome):
                return ProbeOutcome(False, "CONFIGURATION_TEST_FAILED")
            return result
        if section == "ports":
            return _probe_ports(public)
        return ProbeOutcome(False, "SECTION_TEST_ADAPTER_UNAVAILABLE")


def _probe_ports(public: dict[str, Any], _secrets: dict[str, str] | None = None) -> ProbeOutcome:
    ports = list(public.values())
    if (
        not ports
        or any(not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65_535 for port in ports)
        or len(ports) != len(set(ports))
    ):
        return ProbeOutcome(False, "PORT_CONFIGURATION_INVALID")
    listeners: list[socket.socket] = []
    try:
        for port in ports:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", port))
            listeners.append(listener)
    except OSError:
        return ProbeOutcome(False, "PORT_UNAVAILABLE")
    finally:
        for listener in listeners:
            listener.close()
    return ProbeOutcome(True, fingerprint=_digest(sorted(ports)))


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + sha256(encoded).hexdigest()
