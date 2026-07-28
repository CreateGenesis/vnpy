"""Concrete configuration and fixed-service backend for the operations API."""

from __future__ import annotations

from typing import Any, Callable

from .configuration import (
    ConfigurationConflict,
    ConfigurationError,
    ConfigurationStore,
    ConfigurationTestRequired,
)
from .configuration_tests import ConfigurationSectionTester
from .contracts import OperationRejected, ServiceName, build_action_catalog
from .gateway_control import GatewayControlError, GatewayControlService
from .supervisor import FixedServiceSupervisor, SupervisorError


class OperationsService:
    def __init__(
        self,
        configuration: ConfigurationStore,
        tester: ConfigurationSectionTester,
        supervisor: FixedServiceSupervisor,
        *,
        candidate_ready: Callable[[], bool] | None = None,
        activation_health: Callable[[dict[str, Any]], bool] | None = None,
        gateway_control: GatewayControlService | None = None,
        campaign_state: Callable[[], str] | None = None,
    ) -> None:
        self._configuration = configuration
        self._tester = tester
        self._supervisor = supervisor
        self._candidate_ready = candidate_ready or (lambda: False)
        self._activation_health = activation_health or (lambda _candidate: True)
        self._gateway_control = gateway_control
        self._campaign_state = campaign_state or (lambda: "stopped")

    def system(self) -> dict[str, Any]:
        active = self._configuration.read_active()
        draft = self._configuration.read_draft()
        services = []
        for service in (ServiceName.RESEARCH, ServiceName.MODEL_XTP, ServiceName.MODEL_TORA):
            try:
                services.append(self._supervisor.reconcile(service))
            except SupervisorError:
                services.append(
                    {"service": service.value, "state": "unconfigured", "revision": 0, "error_code": None}
                )
        gateway_projection = (
            self._gateway_control.projection()
            if self._gateway_control is not None
            else {
                "revision": 0,
                "gateways": [
                    {"gateway": "XTP", "state": "unconfigured", "selected": False},
                    {"gateway": "TORA", "state": "unconfigured", "selected": False},
                ],
            }
        )
        gateway_revision = int(gateway_projection.get("revision", 0))
        gateways = gateway_projection["gateways"]
        selected = {
            item["gateway"] for item in gateways if item.get("selected") is True
        }
        gateway_states = {item["gateway"]: item["state"] for item in gateways}
        revision = max(
            [
                draft["revision"],
                gateway_revision,
                *[int(item.get("revision", 0)) for item in services],
            ]
        )
        actions = build_action_catalog(
            revision=revision,
            configuration_active=active.get("state") == "active",
            candidate_ready=self._candidate_ready(),
            selected_gateways=selected,
            gateway_states=gateway_states,
            campaign_state=self._campaign_state(),
        )
        for action in actions:
            if action["action_id"].startswith("gateway."):
                action["expected_revision"] = gateway_revision
        return {
            "contract_version": 2,
            "revision": revision,
            "configuration": {
                "state": active.get("state", "unconfigured"),
                "active_version": active.get("version", 0),
                "draft_revision": draft["revision"],
            },
            "services": services,
            "gateways": gateways,
            "actions": actions,
        }

    def configuration_draft(self) -> dict[str, Any]:
        return self._configuration.read_draft()

    def update_configuration(self, command: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._configuration.update_draft(
                expected_revision=command["expected_revision"],
                sections=command["sections"],
                secret_updates=command.get("secret_updates", {}),
                clear_secrets=command.get("clear_secrets", []),
            )
        except (ConfigurationConflict, ConfigurationError) as exc:
            raise OperationRejected(str(exc)) from exc

    def test_configuration(self, command: dict[str, Any]) -> dict[str, Any]:
        draft = self._configuration.read_draft()
        section = command["section"]
        if draft["revision"] != command["expected_revision"]:
            raise OperationRejected("CONFIGURATION_REVISION_CONFLICT")
        public = draft["sections"].get(section, {})
        secrets = self._configuration.read_section_secrets(section)
        outcome = self._tester.test(section, public, secrets)
        try:
            receipt = self._configuration.record_section_test(
                section,
                expected_revision=command["expected_revision"],
                passed=outcome.passed,
                code=outcome.code,
            )
        except (ConfigurationConflict, ConfigurationError) as exc:
            raise OperationRejected(str(exc)) from exc
        return {**receipt, "fingerprint": outcome.fingerprint}

    def activate_configuration(self, command: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._configuration.activate(
                expected_revision=command["expected_revision"],
                health_check=self._activation_health,
            )
        except (ConfigurationConflict, ConfigurationTestRequired, ConfigurationError) as exc:
            raise OperationRejected(str(exc)) from exc

    def control_service(
        self,
        service: str,
        action: str,
        command: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            return self._supervisor.handle(
                {
                    "service": service,
                    "action": action,
                    "expected_revision": command["expected_revision"],
                }
            )
        except SupervisorError as exc:
            raise OperationRejected(str(exc)) from exc

    def control_gateway(
        self,
        gateway: str,
        action: str,
        command: dict[str, Any],
    ) -> dict[str, Any]:
        if self._gateway_control is None:
            raise OperationRejected("GATEWAY_CONTROL_UNAVAILABLE")
        try:
            return self._gateway_control.control(gateway, action, command)
        except GatewayControlError as exc:
            raise OperationRejected(str(exc)) from exc
