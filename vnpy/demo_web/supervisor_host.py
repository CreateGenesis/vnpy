"""Trusted standalone process supervisor for the loopback operations console."""

from __future__ import annotations

import argparse
from base64 import b64decode
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from threading import Thread
from time import sleep
from typing import Any

from .contracts import ServiceName
from .supervisor import FixedServiceSupervisor, LocalProcessRuntime, ServiceSpec
from .supervisor_ipc import SupervisorIpcError, SupervisorIpcServer


_SUPERVISOR_KEY_ENV = "AUTO_TRADE_SUPERVISOR_KEY"


def build_supervisor_specs(
    project_root: Path,
    *,
    python_executable: Path,
    configuration_version: int,
    web_port: int,
    supervisor_address: str,
    ports: dict[str, int] | None = None,
) -> dict[ServiceName, ServiceSpec]:
    root = Path(project_root).resolve(strict=False)
    host, supervisor_port = _address(supervisor_address)
    selected: dict[str, int] = {
        "agentd": 8781,
        "model_xtp": 8782,
        "model_tora": 8783,
        "run_xtp": 8784,
        "run_tora": 8785,
        "rqdata_fetcher": 8786,
    }
    if ports is not None:
        for name in selected:
            if name in ports:
                selected[name] = ports[name]
    all_ports = [web_port, supervisor_port, *selected.values()]
    if (
        configuration_version < 1
        or not python_executable.is_file()
        or any(not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 65_535 for value in all_ports)
        or len(all_ports) != len(set(all_ports))
    ):
        raise ValueError("SUPERVISOR_REGISTRY_INVALID")
    rust = root / "auto-tride-rust"
    agentd = _first_existing(
        rust / "target" / "release" / "agentd.exe",
        rust / "target" / "debug" / "agentd.exe",
    )
    modeld = _first_existing(
        rust / "target" / "release" / "modeld.exe",
        rust / "target" / "debug" / "modeld.exe",
    )
    shared = {
        "configuration_version": configuration_version,
    }
    return {
        ServiceName.WEB: ServiceSpec(
            service=ServiceName.WEB,
            executable=python_executable,
            executable_digest=_executable_digest(python_executable),
            argument_template=(
                "-m",
                "vnpy.demo_web",
                "--host",
                host,
                "--port",
                "{port}",
                "--project-root",
                str(root),
                "--supervisor-address",
                supervisor_address,
            ),
            endpoint_template=f"http://{host}:{{port}}",
            default_port=web_port,
            working_directory=root / "vnpy",
            health_timeout_seconds=20.0,
            **shared,
        ),
        ServiceName.RESEARCH: ServiceSpec(
            service=ServiceName.RESEARCH,
            executable=agentd,
            executable_digest=_executable_digest(agentd),
            argument_template=("serve",),
            endpoint_template=f"tcp://{host}:{selected['agentd']}",
            working_directory=rust,
            **shared,
        ),
        ServiceName.MODEL_XTP: _model_spec(
            ServiceName.MODEL_XTP,
            modeld,
            root / ".demo-state" / "runs" / "XTP" / "modeld.json",
            host,
            selected["model_xtp"],
            rust,
            shared,
        ),
        ServiceName.MODEL_TORA: _model_spec(
            ServiceName.MODEL_TORA,
            modeld,
            root / ".demo-state" / "runs" / "TORA" / "modeld.json",
            host,
            selected["model_tora"],
            rust,
            shared,
        ),
        ServiceName.RUN_XTP: _run_spec(
            ServiceName.RUN_XTP,
            python_executable,
            root,
            "XTP",
            host,
            selected["run_xtp"],
            shared,
        ),
        ServiceName.RUN_TORA: _run_spec(
            ServiceName.RUN_TORA,
            python_executable,
            root,
            "TORA",
            host,
            selected["run_tora"],
            shared,
        ),
        ServiceName.RQDATA_FETCHER: ServiceSpec(
            service=ServiceName.RQDATA_FETCHER,
            executable=python_executable,
            executable_digest=_executable_digest(python_executable),
            argument_template=(
                "-m",
                "vnpy.model_production.rqdata_snapshot",
                "--project-root",
                str(root),
                "serve",
                "--address",
                f"{host}:{selected['rqdata_fetcher']}",
            ),
            endpoint_template=f"tcp://{host}:{selected['rqdata_fetcher']}",
            working_directory=root / "vnpy",
            health_timeout_seconds=20.0,
            **shared,
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m vnpy.demo_web.supervisor_host")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--web-port", type=int, default=8765)
    parser.add_argument("--supervisor-port", type=int, default=8755)
    args = parser.parse_args()
    root = args.project_root.resolve(strict=False)
    key = _authentication_key()
    address = f"127.0.0.1:{args.supervisor_port}"
    active = _active_configuration(root)
    version = max(1, int(active.get("version", 0)))
    ports = active.get("sections", {}).get("ports", {})
    specs = build_supervisor_specs(
        root,
        python_executable=Path(sys.executable),
        configuration_version=version,
        web_port=args.web_port,
        supervisor_address=address,
        ports=ports if isinstance(ports, dict) else None,
    )
    supervisor = FixedServiceSupervisor(
        root / ".operations-state" / "supervisor",
        specs=specs,
        runtime=LocalProcessRuntime(),
    )
    server = SupervisorIpcServer(
        ("127.0.0.1", args.supervisor_port),
        authentication_key=key,
        supervisor=supervisor,
    )
    thread = Thread(target=server.serve_forever, name="supervisor-ipc", daemon=True)
    endpoint_path = root / ".operations-state" / "supervisor" / "endpoint.json"
    thread.start()
    try:
        _publish_endpoint(endpoint_path, args.supervisor_port)
        web = supervisor.reconcile(ServiceName.WEB)
        action = "start" if web["state"] == "stopped" else "restart"
        started = supervisor.handle(
            {
                "service": ServiceName.WEB.value,
                "action": action,
                "expected_revision": web["revision"],
            }
        )
        print(
            json.dumps(
                {
                    "contract_version": 1,
                    "state": "ready",
                    "address": address,
                    "web_pid": started["pid"],
                    "web_url": started["endpoint"],
                },
                ensure_ascii=True,
                separators=(",", ":"),
            ),
            flush=True,
        )
        while thread.is_alive():
            thread.join(timeout=1.0)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            web = supervisor.reconcile(ServiceName.WEB)
            if web["state"] == "ready":
                supervisor.handle(
                    {
                        "service": ServiceName.WEB.value,
                        "action": "stop",
                        "expected_revision": web["revision"],
                    }
                )
        except Exception:
            pass
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
        endpoint_path.unlink(missing_ok=True)


def _model_spec(
    service: ServiceName,
    executable: Path,
    configuration_path: Path,
    host: str,
    port: int,
    working_directory: Path,
    shared: dict[str, int],
) -> ServiceSpec:
    return ServiceSpec(
        service=service,
        executable=executable,
        executable_digest=_executable_digest(executable),
        argument_template=("serve", "--config", str(configuration_path)),
        endpoint_template=f"tcp://{host}:{port}",
        working_directory=working_directory,
        **shared,
    )


def _run_spec(
    service: ServiceName,
    python_executable: Path,
    root: Path,
    gateway: str,
    host: str,
    port: int,
    shared: dict[str, int],
) -> ServiceSpec:
    return ServiceSpec(
        service=service,
        executable=python_executable,
        executable_digest=_executable_digest(python_executable),
        argument_template=(
            "-m",
            "vnpy.demo_web.run_service",
            "--project-root",
            str(root),
            "--gateway",
            gateway,
            "--address",
            f"{host}:{port}",
        ),
        endpoint_template=f"tcp://{host}:{port}",
        working_directory=root / "vnpy",
        **shared,
    )


def _authentication_key() -> bytes:
    encoded = os.environ.get(_SUPERVISOR_KEY_ENV, "")
    try:
        value = b64decode(encoded, validate=True)
    except ValueError as exc:
        raise SupervisorIpcError("SUPERVISOR_IPC_KEY_INVALID") from exc
    if len(value) < 32:
        raise SupervisorIpcError("SUPERVISOR_IPC_KEY_INVALID")
    return value


def _address(value: str) -> tuple[str, int]:
    try:
        host, port_text = value.rsplit(":", 1)
        port = int(port_text)
    except (AttributeError, ValueError) as exc:
        raise ValueError("SUPERVISOR_IPC_LOOPBACK_REQUIRED") from exc
    if host != "127.0.0.1" or not 1 <= port <= 65_535:
        raise ValueError("SUPERVISOR_IPC_LOOPBACK_REQUIRED")
    return host, port


def _active_configuration(root: Path) -> dict[str, Any]:
    path = root / ".operations-state" / "configuration" / "active.json"
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _publish_endpoint(path: Path, port: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".endpoint.tmp")
    value = {
        "contract_version": 1,
        "transport": "tcp-loopback-hmac",
        "address": f"127.0.0.1:{port}",
        "pid": os.getpid(),
    }
    with temporary.open("x", encoding="ascii", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _first_existing(*paths: Path) -> Path:
    return next((path for path in paths if path.is_file()), paths[0])


def _executable_digest(path: Path) -> str:
    if not path.is_file():
        return "sha256:" + sha256(("missing:" + str(path)).encode()).hexdigest()
    hasher = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return "sha256:" + hasher.hexdigest()


if __name__ == "__main__":
    main()
