from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from vnpy.demo_web.contracts import ServiceName
from vnpy.demo_web.supervisor import (
    FixedServiceSupervisor,
    ProcessIdentity,
    ServiceSpec,
    SupervisorError,
)


def digest(value: str) -> str:
    return f"sha256:{sha256(value.encode()).hexdigest()}"


class FakeRuntime:
    def __init__(self) -> None:
        self.next_pid = 100
        self.identities: dict[int, ProcessIdentity] = {}
        self.events: list[tuple[str, int]] = []
        self.healthy_result = True

    def spawn(self, spec: ServiceSpec, arguments: tuple[str, ...]) -> ProcessIdentity:
        self.next_pid += 1
        identity = ProcessIdentity(
            pid=self.next_pid,
            creation_time_ns=self.next_pid * 1_000,
            executable_digest=spec.executable_digest,
        )
        self.identities[identity.pid] = identity
        self.events.append(("spawn", identity.pid))
        return identity

    def inspect(self, pid: int) -> ProcessIdentity | None:
        return self.identities.get(pid)

    def terminate(self, pid: int) -> None:
        self.events.append(("terminate", pid))
        self.identities.pop(pid, None)

    def healthy(self, spec: ServiceSpec, identity: ProcessIdentity, endpoint: str) -> bool:
        return self.healthy_result and identity.pid in self.identities


def web_spec() -> ServiceSpec:
    return ServiceSpec(
        service=ServiceName.WEB,
        executable=Path("C:/Python312/python.exe"),
        executable_digest=digest("python"),
        argument_template=("-m", "vnpy.demo_web", "--port", "{port}"),
        endpoint_template="http://127.0.0.1:{port}",
        configuration_version=1,
        default_port=8765,
    )


def supervisor(tmp_path: Path, runtime: FakeRuntime) -> FixedServiceSupervisor:
    return FixedServiceSupervisor(tmp_path, specs={ServiceName.WEB: web_spec()}, runtime=runtime)


def test_fixed_allowlist_rejects_arbitrary_service_command_path_and_arguments(
    tmp_path: Path,
) -> None:
    service = supervisor(tmp_path, FakeRuntime())

    with pytest.raises(SupervisorError, match="SUPERVISOR_SERVICE_DENIED"):
        service.handle({"service": "cmd", "action": "start", "expected_revision": 0})
    for forbidden in ("command", "executable", "arguments", "path"):
        with pytest.raises(SupervisorError, match="SUPERVISOR_COMMAND_INVALID"):
            service.handle(
                {
                    "service": "web",
                    "action": "start",
                    "expected_revision": 0,
                    forbidden: "calc.exe",
                }
            )


def test_pid_reuse_and_stale_configuration_descriptors_fail_closed(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    service = supervisor(tmp_path, runtime)
    started = service.handle({"service": "web", "action": "start", "expected_revision": 0})
    pid = started["pid"]
    runtime.identities[pid] = ProcessIdentity(
        pid=pid,
        creation_time_ns=999_999,
        executable_digest=digest("other"),
    )

    reused = service.reconcile(ServiceName.WEB)
    assert reused["state"] == "orphaned"
    assert reused["error_code"] == "SUPERVISOR_PROCESS_IDENTITY_MISMATCH"

    runtime = FakeRuntime()
    first = supervisor(tmp_path / "stale", runtime)
    first.handle({"service": "web", "action": "start", "expected_revision": 0})
    newer = ServiceSpec(**{**web_spec().__dict__, "configuration_version": 2})
    restored = FixedServiceSupervisor(
        tmp_path / "stale", specs={ServiceName.WEB: newer}, runtime=runtime
    )
    assert restored.reconcile(ServiceName.WEB)["state"] == "blocked"
    assert restored.reconcile(ServiceName.WEB)["error_code"] == "SUPERVISOR_CONFIGURATION_STALE"


def test_restart_recovery_and_web_port_handoff_start_replacement_first(
    tmp_path: Path,
) -> None:
    runtime = FakeRuntime()
    first = supervisor(tmp_path, runtime)
    started = first.handle({"service": "web", "action": "start", "expected_revision": 0})
    recovered = supervisor(tmp_path, runtime)

    assert recovered.reconcile(ServiceName.WEB)["state"] == "ready"
    handoff = recovered.handoff_web_port(
        port=8877,
        configuration_version=2,
        expected_revision=started["revision"],
    )

    assert handoff["next_url"] == "http://127.0.0.1:8877"
    assert handoff["pid"] != started["pid"]
    assert runtime.events[-2:] == [("spawn", handoff["pid"]), ("terminate", started["pid"])]
