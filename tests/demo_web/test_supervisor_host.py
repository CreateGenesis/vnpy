from __future__ import annotations

from pathlib import Path
import sys

from vnpy.demo_web.contracts import ServiceName
from vnpy.demo_web.supervisor_host import build_supervisor_specs


def test_supervisor_registry_contains_only_fixed_service_templates(tmp_path: Path) -> None:
    specs = build_supervisor_specs(
        tmp_path,
        python_executable=Path(sys.executable),
        configuration_version=3,
        web_port=8765,
        supervisor_address="127.0.0.1:8755",
    )

    assert set(specs) == set(ServiceName)
    assert specs[ServiceName.WEB].arguments() == (
        "-m",
        "vnpy.demo_web",
        "--host",
        "127.0.0.1",
        "--port",
        "8765",
        "--project-root",
        str(tmp_path),
        "--supervisor-address",
        "127.0.0.1:8755",
    )
    assert specs[ServiceName.RUN_XTP].arguments()[-2:] == ("--address", "127.0.0.1:8784")
    assert specs[ServiceName.RUN_TORA].arguments()[-2:] == ("--address", "127.0.0.1:8785")
    assert specs[ServiceName.RQDATA_FETCHER].arguments()[-2:] == (
        "--address",
        "127.0.0.1:8786",
    )
    for spec in specs.values():
        assert spec.configuration_version == 3
        rendered = " ".join(spec.arguments()).lower()
        assert "password" not in rendered
        assert "api_key" not in rendered
        assert "token" not in rendered


def test_supervisor_registry_rejects_non_loopback_or_aliased_ports(tmp_path: Path) -> None:
    for address in ("0.0.0.0:8755", "localhost:8755", "127.0.0.1:not-a-port"):
        try:
            build_supervisor_specs(
                tmp_path,
                python_executable=Path(sys.executable),
                configuration_version=1,
                web_port=8765,
                supervisor_address=address,
            )
        except ValueError as exc:
            assert str(exc) == "SUPERVISOR_IPC_LOOPBACK_REQUIRED"
        else:
            raise AssertionError(f"unsafe supervisor address accepted: {address}")
