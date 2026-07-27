"""Command-line entry point for the loopback investor demo."""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from .app import create_demo_app
from .runtime import build_demo_runtime


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m vnpy.demo_web")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--project-root", type=Path, required=True)
    args = parser.parse_args()
    runtime = build_demo_runtime(args.project_root, host=args.host, port=args.port)
    app = create_demo_app(runtime.config, runtime.backend, runtime.guidance)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
