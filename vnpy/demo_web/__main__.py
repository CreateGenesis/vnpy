"""Command-line entry point for the loopback investor demo."""

from __future__ import annotations

import argparse
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
    args = parser.parse_args()
    runtime = build_demo_runtime(args.project_root, host=args.host, port=args.port)
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


if __name__ == "__main__":
    main()
