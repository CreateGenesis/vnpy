"""Command-line entry point for the loopback investor demo."""

from __future__ import annotations

import argparse
from base64 import b64decode
import json
import os
from pathlib import Path

import uvicorn

from .app import create_demo_app
from .runtime import DemoRuntime, build_demo_runtime


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m vnpy.demo_web")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--supervisor-address")
    args = parser.parse_args()
    supervisor_address = None
    supervisor_key = None
    if args.supervisor_address:
        supervisor_address = _parse_supervisor_address(args.supervisor_address)
        try:
            supervisor_key = b64decode(
                os.environ.get("AUTO_TRADE_SUPERVISOR_KEY", ""),
                validate=True,
            )
        except ValueError as exc:
            raise ValueError("SUPERVISOR_IPC_KEY_INVALID") from exc
        if len(supervisor_key) < 32:
            raise ValueError("SUPERVISOR_IPC_KEY_INVALID")
    runtime = build_demo_runtime(
        args.project_root,
        host=args.host,
        port=args.port,
        supervisor_address=supervisor_address,
        supervisor_authentication_key=supervisor_key,
    )
    _publish_bootstrap_descriptor(args.project_root, runtime)
    app = create_demo_app(
        runtime.config,
        runtime.backend,
        runtime.guidance,
        security=runtime.security,
        operations=runtime.operations,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


def _publish_bootstrap_descriptor(project_root: Path, runtime: DemoRuntime) -> None:
    root = project_root / ".operations-state"
    root.mkdir(parents=True, exist_ok=True)
    path = root / "bootstrap.json"
    temporary = root / ".bootstrap.tmp"
    config = runtime.config
    payload = {
        "contract_version": 1,
        "url": f"{config.allowed_origin}/#bootstrap={runtime.bootstrap_fragment_token}",
    }
    with temporary.open("x", encoding="ascii", newline="\n") as handle:
        json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _parse_supervisor_address(value: str) -> tuple[str, int]:
    try:
        host, port_text = value.rsplit(":", 1)
        port = int(port_text)
    except (AttributeError, ValueError) as exc:
        raise ValueError("SUPERVISOR_IPC_LOOPBACK_REQUIRED") from exc
    if host != "127.0.0.1" or not 1 <= port <= 65_535:
        raise ValueError("SUPERVISOR_IPC_LOOPBACK_REQUIRED")
    return host, port


if __name__ == "__main__":
    main()
