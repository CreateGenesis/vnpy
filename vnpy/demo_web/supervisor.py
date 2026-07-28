"""Fixed-registry process supervision for trusted local services."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
from threading import RLock
from time import monotonic, sleep
from typing import Any, Protocol
from urllib.request import urlopen

from .contracts import ServiceAction, ServiceName


class SupervisorError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    creation_time_ns: int
    executable_digest: str


@dataclass(frozen=True)
class ServiceSpec:
    service: ServiceName
    executable: Path
    executable_digest: str
    argument_template: tuple[str, ...]
    endpoint_template: str
    configuration_version: int
    default_port: int | None = None
    working_directory: Path | None = None
    health_timeout_seconds: float = 0.0

    def validate(self) -> None:
        if (
            not self.executable_digest.startswith("sha256:")
            or len(self.executable_digest) != 71
            or self.configuration_version < 1
            or any(not isinstance(value, str) for value in self.argument_template)
            or ("{port}" in self.endpoint_template and self.default_port is None)
            or not 0 <= self.health_timeout_seconds <= 120
        ):
            raise SupervisorError("SUPERVISOR_SPEC_INVALID")

    def arguments(self, port: int | None = None) -> tuple[str, ...]:
        selected = self.default_port if port is None else port
        if selected is not None and not 1 <= selected <= 65_535:
            raise SupervisorError("SUPERVISOR_PORT_INVALID")
        return tuple(item.format(port=selected) for item in self.argument_template)

    def endpoint(self, port: int | None = None) -> str:
        selected = self.default_port if port is None else port
        return self.endpoint_template.format(port=selected)


class ProcessRuntime(Protocol):
    def spawn(self, spec: ServiceSpec, arguments: tuple[str, ...]) -> ProcessIdentity: ...

    def inspect(self, pid: int) -> ProcessIdentity | None: ...

    def terminate(self, pid: int) -> None: ...

    def healthy(self, spec: ServiceSpec, identity: ProcessIdentity, endpoint: str) -> bool: ...


class LocalProcessRuntime:
    """Spawn only fixed specs and reconstruct exact identities after restart."""

    def __init__(self) -> None:
        self._children: dict[int, subprocess.Popen[bytes]] = {}

    def spawn(self, spec: ServiceSpec, arguments: tuple[str, ...]) -> ProcessIdentity:
        if not spec.executable.is_file() or _file_digest(spec.executable) != spec.executable_digest:
            raise SupervisorError("SUPERVISOR_EXECUTABLE_MISMATCH")
        child = subprocess.Popen(
            [str(spec.executable), *arguments],
            cwd=spec.working_directory,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        self._children[child.pid] = child
        for _ in range(50):
            identity = self.inspect(child.pid)
            if identity is not None:
                return identity
            if child.poll() is not None:
                break
            sleep(0.01)
        child.kill()
        raise SupervisorError("SUPERVISOR_PROCESS_IDENTITY_UNAVAILABLE")

    def inspect(self, pid: int) -> ProcessIdentity | None:
        child = self._children.get(pid)
        if child is not None and child.poll() is not None:
            return None
        try:
            executable, created = _inspect_process(pid)
            return ProcessIdentity(
                pid=pid,
                creation_time_ns=created,
                executable_digest=_file_digest(executable),
            )
        except (OSError, SupervisorError):
            return None

    def terminate(self, pid: int) -> None:
        child = self._children.pop(pid, None)
        if child is not None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill.exe", "/PID", str(pid), "/T"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            os.kill(pid, 15)

    def healthy(self, spec: ServiceSpec, identity: ProcessIdentity, endpoint: str) -> bool:
        deadline = monotonic() + spec.health_timeout_seconds
        while True:
            if self.inspect(identity.pid) != identity:
                return False
            if spec.service is not ServiceName.WEB:
                return True
            try:
                with urlopen(endpoint, timeout=1) as response:  # noqa: S310 - fixed loopback spec
                    if response.status == 200:
                        return True
            except OSError:
                pass
            if monotonic() >= deadline:
                return False
            sleep(0.1)


class FixedServiceSupervisor:
    """Persist identities and accept only enum service/action operations."""

    _REQUEST_KEYS = frozenset({"service", "action", "expected_revision"})

    def __init__(
        self,
        state_directory: Path,
        *,
        specs: dict[ServiceName, ServiceSpec],
        runtime: ProcessRuntime,
    ) -> None:
        self._root = Path(state_directory)
        self._path = self._root / "supervisor.json"
        self._root.mkdir(parents=True, exist_ok=True)
        self._specs = dict(specs)
        self._runtime = runtime
        self._lock = RLock()
        if not self._specs or any(key != spec.service for key, spec in self._specs.items()):
            raise SupervisorError("SUPERVISOR_REGISTRY_INVALID")
        for spec in self._specs.values():
            spec.validate()

    def handle(self, command: dict[str, Any]) -> dict[str, Any]:
        if set(command) != self._REQUEST_KEYS:
            raise SupervisorError("SUPERVISOR_COMMAND_INVALID")
        try:
            service = ServiceName(command["service"])
            action = ServiceAction(command["action"])
        except (ValueError, TypeError) as exc:
            raise SupervisorError("SUPERVISOR_SERVICE_DENIED") from exc
        if service not in self._specs:
            raise SupervisorError("SUPERVISOR_SERVICE_DENIED")
        expected = command["expected_revision"]
        if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
            raise SupervisorError("SUPERVISOR_COMMAND_INVALID")
        with self._lock:
            state = self._load()
            if state["revision"] != expected:
                raise SupervisorError("SUPERVISOR_REVISION_CONFLICT")
            if action is ServiceAction.START:
                return self._start(state, self._specs[service])
            if action is ServiceAction.STOP:
                return self._stop(state, service)
            stopped = self._stop(state, service, increment=False)
            return self._start(self._load(), self._specs[service], prior_revision=stopped["revision"])

    def reconcile(self, service: ServiceName) -> dict[str, Any]:
        with self._lock:
            if service not in self._specs:
                raise SupervisorError("SUPERVISOR_SERVICE_DENIED")
            state = self._load()
            descriptor = state["services"].get(service.value)
            if descriptor is None:
                return self._projection(service, state["revision"], state="stopped")
            spec = self._specs[service]
            if descriptor["configuration_version"] != spec.configuration_version:
                return self._set_failure(
                    state,
                    service,
                    descriptor,
                    "blocked",
                    "SUPERVISOR_CONFIGURATION_STALE",
                )
            observed = self._runtime.inspect(descriptor["pid"])
            expected = ProcessIdentity(
                pid=descriptor["pid"],
                creation_time_ns=descriptor["creation_time_ns"],
                executable_digest=descriptor["executable_digest"],
            )
            if observed != expected:
                return self._set_failure(
                    state,
                    service,
                    descriptor,
                    "orphaned",
                    "SUPERVISOR_PROCESS_IDENTITY_MISMATCH",
                )
            if not self._runtime.healthy(spec, expected, descriptor["endpoint"]):
                return self._set_failure(
                    state,
                    service,
                    descriptor,
                    "blocked",
                    "SUPERVISOR_HEALTH_FAILED",
                )
            descriptor["state"] = "ready"
            descriptor["error_code"] = None
            self._persist(state)
            return dict(descriptor)

    def handoff_web_port(
        self,
        *,
        port: int,
        configuration_version: int,
        expected_revision: int,
    ) -> dict[str, Any]:
        with self._lock:
            state = self._load()
            if state["revision"] != expected_revision:
                raise SupervisorError("SUPERVISOR_REVISION_CONFLICT")
            base = self._specs.get(ServiceName.WEB)
            if base is None:
                raise SupervisorError("SUPERVISOR_SERVICE_DENIED")
            replacement = ServiceSpec(
                service=base.service,
                executable=base.executable,
                executable_digest=base.executable_digest,
                argument_template=base.argument_template,
                endpoint_template=base.endpoint_template,
                configuration_version=configuration_version,
                default_port=port,
                working_directory=base.working_directory,
                health_timeout_seconds=base.health_timeout_seconds,
            )
            replacement.validate()
            identity = self._runtime.spawn(replacement, replacement.arguments())
            endpoint = replacement.endpoint()
            if identity.executable_digest != replacement.executable_digest or not self._runtime.healthy(
                replacement, identity, endpoint
            ):
                self._runtime.terminate(identity.pid)
                raise SupervisorError("SUPERVISOR_REPLACEMENT_HEALTH_FAILED")
            previous = state["services"].get(ServiceName.WEB.value)
            state["revision"] += 1
            descriptor = self._descriptor(replacement, identity, endpoint, state["revision"])
            state["services"][ServiceName.WEB.value] = descriptor
            self._persist(state)
            if previous is not None:
                self._runtime.terminate(previous["pid"])
            return {**descriptor, "next_url": endpoint}

    def _start(
        self,
        state: dict[str, Any],
        spec: ServiceSpec,
        *,
        prior_revision: int | None = None,
    ) -> dict[str, Any]:
        existing = state["services"].get(spec.service.value)
        if existing and existing.get("state") == "ready":
            return self.reconcile(spec.service)
        identity = self._runtime.spawn(spec, spec.arguments())
        endpoint = spec.endpoint()
        if identity.executable_digest != spec.executable_digest:
            self._runtime.terminate(identity.pid)
            raise SupervisorError("SUPERVISOR_EXECUTABLE_MISMATCH")
        if not self._runtime.healthy(spec, identity, endpoint):
            self._runtime.terminate(identity.pid)
            raise SupervisorError("SUPERVISOR_HEALTH_FAILED")
        state["revision"] = max(state["revision"], prior_revision or 0) + 1
        descriptor = self._descriptor(spec, identity, endpoint, state["revision"])
        state["services"][spec.service.value] = descriptor
        self._persist(state)
        return dict(descriptor)

    def _stop(
        self,
        state: dict[str, Any],
        service: ServiceName,
        *,
        increment: bool = True,
    ) -> dict[str, Any]:
        descriptor = state["services"].get(service.value)
        if descriptor is not None:
            observed = self._runtime.inspect(descriptor["pid"])
            expected = ProcessIdentity(
                pid=descriptor["pid"],
                creation_time_ns=descriptor["creation_time_ns"],
                executable_digest=descriptor["executable_digest"],
            )
            if observed != expected:
                return self._set_failure(
                    state,
                    service,
                    descriptor,
                    "orphaned",
                    "SUPERVISOR_PROCESS_IDENTITY_MISMATCH",
                )
            self._runtime.terminate(descriptor["pid"])
        if increment:
            state["revision"] += 1
        state["services"][service.value] = self._projection(service, state["revision"], state="stopped")
        self._persist(state)
        return dict(state["services"][service.value])

    def _set_failure(
        self,
        state: dict[str, Any],
        service: ServiceName,
        descriptor: dict[str, Any],
        state_name: str,
        error_code: str,
    ) -> dict[str, Any]:
        if descriptor.get("state") != state_name or descriptor.get("error_code") != error_code:
            state["revision"] += 1
        descriptor["state"] = state_name
        descriptor["error_code"] = error_code
        descriptor["revision"] = state["revision"]
        state["services"][service.value] = descriptor
        self._persist(state)
        return dict(descriptor)

    @staticmethod
    def _descriptor(
        spec: ServiceSpec,
        identity: ProcessIdentity,
        endpoint: str,
        revision: int,
    ) -> dict[str, Any]:
        return {
            "service": spec.service.value,
            "state": "ready",
            "pid": identity.pid,
            "creation_time_ns": identity.creation_time_ns,
            "executable_digest": identity.executable_digest,
            "argument_template_digest": _digest(list(spec.argument_template)),
            "configuration_version": spec.configuration_version,
            "endpoint": endpoint,
            "endpoint_digest": _digest(endpoint),
            "revision": revision,
            "error_code": None,
        }

    @staticmethod
    def _projection(service: ServiceName, revision: int, *, state: str) -> dict[str, Any]:
        return {"service": service.value, "state": state, "revision": revision, "error_code": None}

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"contract_version": 1, "revision": 0, "services": {}}
        value = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or set(value) != {"contract_version", "revision", "services"}:
            raise SupervisorError("SUPERVISOR_STATE_INVALID")
        return value

    def _persist(self, value: dict[str, Any]) -> None:
        temp = self._path.with_suffix(".tmp")
        with temp.open("x", encoding="ascii", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self._path)


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + sha256(encoded).hexdigest()


def _file_digest(path: Path) -> str:
    hasher = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return "sha256:" + hasher.hexdigest()


def _inspect_process(pid: int) -> tuple[Path, int]:
    if os.name != "nt":
        executable = Path(os.readlink(f"/proc/{pid}/exe"))
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").split()
        return executable, int(fields[21])

    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        raise OSError("process unavailable")
    try:
        class FileTime(ctypes.Structure):
            _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

        creation = FileTime()
        exit_time = FileTime()
        kernel = FileTime()
        user = FileTime()
        if not ctypes.windll.kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            raise OSError("process time unavailable")
        size = wintypes.DWORD(32_768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            raise OSError("process executable unavailable")
        created = (creation.high << 32) | creation.low
        return Path(buffer.value), created * 100
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)
