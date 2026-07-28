"""Fail-closed section test adapters for configuration activation receipts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from importlib import import_module
import json
from multiprocessing import get_context
import socket
from time import monotonic, sleep
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

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
        if section in {"master_route", "worker_route"}:
            return probe_model_route(public, secrets)
        if section == "rqdata":
            return probe_rqdata_tick(public, secrets)
        if section in {"xtp", "tora"}:
            return probe_gateway_connection(section.upper(), public, secrets)
        return ProbeOutcome(False, "CONFIGURATION_TEST_FAILED")


def probe_model_route(public: dict[str, Any], secrets: dict[str, str]) -> ProbeOutcome:
    """Issue a minimal same-route request and bind the returned model identity."""

    base_url = public.get("base_url")
    model = public.get("model")
    retry_count = public.get("retry_count")
    api_key = secrets.get("api_key")
    if (
        not isinstance(base_url, str)
        or not isinstance(model, str)
        or not isinstance(retry_count, int)
        or isinstance(retry_count, bool)
        or not isinstance(api_key, str)
        or not api_key
    ):
        return ProbeOutcome(False, "MODEL_ROUTE_CONFIGURATION_INVALID")
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.query or parsed.fragment:
        return ProbeOutcome(False, "MODEL_ROUTE_ENDPOINT_INVALID")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return ProbeOutcome(False, "MODEL_ROUTE_TLS_REQUIRED")
    endpoint = base_url.rstrip("/") + "/chat/completions"
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": "Reply exactly OK."}],
            "max_tokens": 8,
            "stream": False,
        },
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode()
    request = Request(
        endpoint,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    for attempt in range(retry_count + 1):
        try:
            with urlopen(request, timeout=15.0) as response:  # noqa: S310 - configured fixed route
                encoded = response.read(1_048_577)
                if len(encoded) > 1_048_576:
                    return ProbeOutcome(False, "MODEL_ROUTE_RESPONSE_TOO_LARGE")
                value = json.loads(encoded)
        except HTTPError as exc:
            if exc.code in {401, 403}:
                return ProbeOutcome(False, "MODEL_ROUTE_AUTHENTICATION_FAILED")
            if attempt == retry_count:
                return ProbeOutcome(False, "MODEL_ROUTE_UNAVAILABLE")
        except (OSError, URLError, UnicodeDecodeError, json.JSONDecodeError):
            if attempt == retry_count:
                return ProbeOutcome(False, "MODEL_ROUTE_UNAVAILABLE")
        else:
            if not isinstance(value, dict) or value.get("model") != model:
                return ProbeOutcome(False, "MODEL_ROUTE_IDENTITY_MISMATCH")
            choices = value.get("choices")
            if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
                return ProbeOutcome(False, "MODEL_OUTCOME_UNCERTAIN")
            choice = choices[0]
            message = choice.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if choice.get("finish_reason") != "stop" or not isinstance(content, str) or not content.strip():
                return ProbeOutcome(False, "MODEL_OUTCOME_UNCERTAIN")
            return ProbeOutcome(
                True,
                fingerprint=_digest(
                    {
                        "scheme": parsed.scheme,
                        "host": parsed.hostname,
                        "port": parsed.port,
                        "path": parsed.path.rstrip("/"),
                        "model": model,
                    }
                ),
            )
    return ProbeOutcome(False, "MODEL_ROUTE_UNAVAILABLE")


def probe_rqdata_tick(
    public: dict[str, Any],
    secrets: dict[str, str],
    *,
    module: Any | None = None,
) -> ProbeOutcome:
    """Authenticate with RQData and perform one bounded historical Tick query."""

    endpoint = public.get("endpoint")
    username = secrets.get("username")
    password = secrets.get("password")
    if public.get("tick_required") is not True or not isinstance(endpoint, str):
        return ProbeOutcome(False, "RQDATA_CONFIGURATION_INVALID")
    try:
        host, port_text = endpoint.rsplit(":", 1)
        port = int(port_text)
    except (AttributeError, ValueError):
        return ProbeOutcome(False, "RQDATA_ENDPOINT_INVALID")
    if not host or not 1 <= port <= 65_535:
        return ProbeOutcome(False, "RQDATA_ENDPOINT_INVALID")
    if not isinstance(username, str) or not username or not isinstance(password, str) or not password:
        return ProbeOutcome(False, "RQDATA_CREDENTIAL_REQUIRED")
    if module is None:
        try:
            module = import_module("rqdatac")
        except ImportError:
            return ProbeOutcome(False, "RQDATA_PACKAGE_UNAVAILABLE")
    try:
        module.init(
            username,
            password,
            (host, port),
            lazy=False,
            connect_timeout=5,
            timeout=15,
            auto_load_plugins=False,
        )
        session = module.get_previous_trading_date(date.today(), n=1)
        ticks = module.get_price(
            "000001.XSHE",
            start_date=session,
            end_date=session,
            frequency="tick",
            fields=["last"],
            expect_df=False,
        )
        if ticks is None or len(ticks) == 0:
            return ProbeOutcome(False, "RQDATA_TICK_EMPTY")
    except Exception as exc:
        lowered = str(exc).lower()
        code = (
            "RQDATA_TICK_NOT_ENTITLED"
            if any(marker in lowered for marker in ("permission", "privilege", "license", "entitle"))
            else "RQDATA_CONNECTION_FAILED"
        )
        return ProbeOutcome(False, code)
    finally:
        reset = getattr(module, "reset", None)
        if callable(reset):
            try:
                reset()
            except Exception:
                pass
    return ProbeOutcome(
        True,
        fingerprint=_digest({"host": host, "port": port, "session": str(session), "frequency": "tick"}),
    )


GatewayRunner = Callable[[str, dict[str, str | int], float], dict[str, Any]]


def probe_gateway_connection(
    gateway: str,
    public: dict[str, Any],
    secrets: dict[str, str],
    *,
    runner: GatewayRunner | None = None,
) -> ProbeOutcome:
    """Connect market-data and trading sessions in a bounded trusted child process."""

    try:
        settings = map_gateway_settings(gateway, public, secrets)
    except GatewaySettingsError as exc:
        return ProbeOutcome(False, str(exc))
    try:
        result = (runner or _run_gateway_probe)(gateway, settings, 15.0)
    except Exception:
        return ProbeOutcome(False, "GATEWAY_CONNECTION_FAILED")
    if not isinstance(result, dict) or result.get("market_data") is not True or result.get("trading") is not True:
        return ProbeOutcome(False, "GATEWAY_CONNECTION_UNCERTAIN")
    return ProbeOutcome(True, fingerprint=_gateway_server_fingerprint(gateway, public))


def _run_gateway_probe(
    gateway: str,
    settings: dict[str, str | int],
    timeout_seconds: float,
) -> dict[str, Any]:
    context = get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(
        target=_gateway_probe_worker,
        args=(child, gateway, settings, timeout_seconds),
        daemon=True,
    )
    process.start()
    child.close()
    try:
        if parent.poll(timeout_seconds + 2.0):
            value = parent.recv()
        else:
            value = {"market_data": False, "trading": False}
    finally:
        parent.close()
        process.join(timeout=1.0)
        if process.is_alive():
            process.terminate()
            process.join(timeout=2.0)
    return value if isinstance(value, dict) else {"market_data": False, "trading": False}


def _gateway_probe_worker(
    connection: Any,
    gateway: str,
    settings: dict[str, str | int],
    timeout_seconds: float,
) -> None:
    result = {"market_data": False, "trading": False}
    selected: Any | None = None
    try:
        from vnpy.event import EventEngine

        if gateway == "XTP":
            from vnpy_xtp import XtpGateway as Gateway
        elif gateway == "TORA":
            from vnpy_tora import ToraStockGateway as Gateway
        else:
            connection.send(result)
            return
        selected = Gateway(EventEngine(), gateway)
        selected.connect(settings)
        deadline = monotonic() + timeout_seconds
        while monotonic() < deadline:
            market_data = bool(getattr(selected.md_api, "login_status", False))
            trading = bool(getattr(selected.td_api, "login_status", False))
            if market_data and trading:
                result = {"market_data": True, "trading": True}
                break
            sleep(0.1)
    except Exception:
        result = {"market_data": False, "trading": False}
    finally:
        if selected is not None:
            try:
                selected.close()
            except Exception:
                pass
        try:
            connection.send(result)
        except (BrokenPipeError, EOFError, OSError):
            pass
        connection.close()


def _gateway_server_fingerprint(gateway: str, public: dict[str, Any]) -> str:
    keys = (
        ("quote_address", "quote_port", "trading_address", "trading_port")
        if gateway == "XTP"
        else ("quote_server", "trading_server")
    )
    return _digest({key: public[key] for key in keys})


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
