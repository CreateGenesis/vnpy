"""Trusted, point-in-time RQData Tick snapshot publication."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from base64 import b64decode
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from hmac import compare_digest
from importlib import import_module
import json
import math
import os
from pathlib import Path
import re
from secrets import token_urlsafe
import shutil
from socketserver import BaseRequestHandler, ThreadingTCPServer
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.exceptions import InvalidSignature


_SYMBOL = re.compile(r"^[0-9]{6}\.(?:SH|SZ|BJ)$")
_FIELDS = frozenset(
    {
        "datetime",
        "last",
        "volume",
        "total_turnover",
        "open_interest",
        "bid_price1",
        "bid_volume1",
        "ask_price1",
        "ask_volume1",
    }
)
_FORBIDDEN_KEYS = frozenset(
    {"username", "password", "api_key", "token", "credential", "authorization"}
)
_ONE_USE_SECRET_ENV = "AUTO_TRADE_ONE_USE_SECRET"
_RQDATA_FIELDS = {
    "last": "last",
    "volume": "volume",
    "total_turnover": "total_turnover",
    "open_interest": "open_interest",
    "bid_price1": "b1",
    "bid_volume1": "b1_v",
    "ask_price1": "a1",
    "ask_volume1": "a1_v",
}


class RqdataSnapshotError(RuntimeError):
    pass


class RqdataProvider(Protocol):
    def provider_identity(self) -> str: ...

    def tick_entitled(self) -> bool: ...

    def trading_dates(self, start: datetime, end: datetime) -> Sequence[str]: ...

    def corporate_actions(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> Sequence[Mapping[str, Any]]: ...


class RqdatacProvider:
    """Fixed rqdatac adapter; credentials remain process-local and never enter results."""

    def __init__(
        self,
        *,
        username: str,
        password: str,
        endpoint: str,
        module: Any | None = None,
    ) -> None:
        if not username or not password:
            raise RqdataSnapshotError("RQDATA_CREDENTIAL_REQUIRED")
        host, port = _parse_endpoint(endpoint)
        selected = module if module is not None else import_module("rqdatac")
        try:
            selected.init(
                username,
                password,
                (host, port),
                lazy=False,
                connect_timeout=5,
                timeout=30,
                auto_load_plugins=False,
            )
        except Exception:
            raise RqdataSnapshotError("RQDATA_CONNECTION_FAILED") from None
        version = str(getattr(selected, "__version__", "unknown"))
        account_fingerprint = sha256(username.encode("utf-8")).hexdigest()
        self._module = selected
        self._identity = f"sha256:{sha256(_json_bytes({
            'adapter': 'rqdatac',
            'version': version,
            'endpoint': endpoint,
            'account_fingerprint': account_fingerprint,
        })).hexdigest()}"

    def provider_identity(self) -> str:
        return self._identity

    def tick_entitled(self) -> bool:
        return True

    def close(self) -> None:
        reset = getattr(self._module, "reset", None)
        if callable(reset):
            try:
                reset()
            except Exception:
                pass

    def trading_dates(self, start: datetime, end: datetime) -> list[str]:
        try:
            values = self._module.get_trading_dates(start.date(), end.date())
        except Exception:
            raise RqdataSnapshotError("RQDATA_CALENDAR_QUERY_FAILED") from None
        return [_date_text(value) for value in values]

    def corporate_actions(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> list[dict[str, Any]]:
        try:
            frame = self._module.get_split(
                _rqdata_symbol(symbol),
                start_date=start.date(),
                end_date=end.date(),
            )
        except Exception:
            raise RqdataSnapshotError("RQDATA_CORPORATE_ACTION_QUERY_FAILED") from None
        if frame is None or len(frame) == 0:
            return []
        records = frame.reset_index().to_dict(orient="records")
        actions: list[dict[str, Any]] = []
        for raw in records:
            action = {"symbol": symbol}
            for key in (
                "ex_dividend_date",
                "book_closure_date",
                "payable_date",
                "split_coefficient_from",
                "split_coefficient_to",
                "cum_factor",
            ):
                if key in raw and not _missing(raw[key]):
                    action["ex_date" if key == "ex_dividend_date" else key] = _public_scalar(
                        raw[key]
                    )
            actions.append(action)
        return actions

    def ticks(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        fields: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        provider_fields = [_RQDATA_FIELDS[field] for field in fields if field != "datetime"]
        try:
            frame = self._module.get_price(
                _rqdata_symbol(symbol),
                start_date=start,
                end_date=end,
                frequency="tick",
                fields=provider_fields,
                adjust_type="none",
                expect_df=True,
            )
        except Exception as exc:
            lowered = str(exc).lower()
            code = (
                "RQDATA_TICK_NOT_ENTITLED"
                if any(
                    marker in lowered
                    for marker in ("permission", "privilege", "license", "entitle")
                )
                else "RQDATA_TICK_QUERY_FAILED"
            )
            raise RqdataSnapshotError(code) from None
        if frame is None or len(frame) == 0:
            raise RqdataSnapshotError("RQDATA_TICK_DATA_INVALID")
        records = frame.reset_index().to_dict(orient="records")
        rows: list[dict[str, Any]] = []
        for raw in records:
            timestamp = raw.get("datetime")
            if timestamp is None:
                raise RqdataSnapshotError("RQDATA_TICK_SCHEMA_INVALID")
            if hasattr(timestamp, "to_pydatetime"):
                timestamp = timestamp.to_pydatetime()
            if not isinstance(timestamp, datetime):
                try:
                    timestamp = datetime.fromisoformat(str(timestamp))
                except ValueError:
                    raise RqdataSnapshotError("RQDATA_TICK_SCHEMA_INVALID") from None
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                timestamp = timestamp.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            row: dict[str, Any] = {"datetime": timestamp}
            for public_name in fields:
                if public_name == "datetime":
                    continue
                provider_name = _RQDATA_FIELDS[public_name]
                if provider_name not in raw or _missing(raw[provider_name]):
                    raise RqdataSnapshotError("RQDATA_TICK_SCHEMA_INVALID")
                row[public_name] = _public_scalar(raw[provider_name])
            rows.append(row)
        return rows


def consume_one_use_configuration() -> dict[str, Any]:
    """Consume the supervisor-only credential payload exactly once."""

    encoded = os.environ.pop(_ONE_USE_SECRET_ENV, None)
    if not isinstance(encoded, str) or not 1 <= len(encoded) <= 87_384:
        raise RqdataSnapshotError("RQDATA_ONE_USE_SECRET_REQUIRED")
    try:
        raw = b64decode(encoded, validate=True)
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        raise RqdataSnapshotError("RQDATA_ONE_USE_SECRET_INVALID") from None
    finally:
        if "raw" in locals():
            del raw
        del encoded
    required = {
        "contract_version",
        "configuration_version",
        "configuration_digest",
        "operator_identity_digest",
        "public",
        "secrets",
    }
    public = value.get("public") if isinstance(value, dict) else None
    secrets = value.get("secrets") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value.get("contract_version") != 1
        or not isinstance(value.get("configuration_version"), int)
        or isinstance(value.get("configuration_version"), bool)
        or value["configuration_version"] < 1
        or not _valid_digest(value.get("configuration_digest"))
        or not _valid_digest(value.get("operator_identity_digest"))
        or not isinstance(public, dict)
        or set(public) != {"endpoint", "tick_required"}
        or public.get("tick_required") is not True
        or not isinstance(public.get("endpoint"), str)
        or not isinstance(secrets, dict)
        or set(secrets) != {"username", "password"}
        or not all(isinstance(item, str) and item for item in secrets.values())
    ):
        raise RqdataSnapshotError("RQDATA_ONE_USE_SECRET_INVALID")
    _parse_endpoint(public["endpoint"])
    return value


class RqdataSnapshotService:
    """Strict credential-free request boundary around the trusted publisher."""

    def __init__(self, publisher: RqdataSnapshotPublisher) -> None:
        self._publisher = publisher

    def handle(self, value: Mapping[str, Any]) -> dict[str, Any]:
        if (
            not isinstance(value, Mapping)
            or set(value) != {"contract_version", "operation", "request"}
            or value.get("contract_version") != 1
            or value.get("operation") != "publish_snapshot"
            or not isinstance(value.get("request"), Mapping)
        ):
            raise RqdataSnapshotError("RQDATA_SERVICE_REQUEST_INVALID")
        raw = value["request"]
        required = {
            "request_id",
            "symbols",
            "fields",
            "start",
            "end",
            "point_in_time",
            "maximum_staleness_ms",
        }
        if set(raw) != required:
            raise RqdataSnapshotError("RQDATA_SERVICE_REQUEST_INVALID")
        try:
            request = RqdataSnapshotRequest(
                request_id=raw["request_id"],
                symbols=tuple(raw["symbols"]),
                fields=tuple(raw["fields"]),
                start=datetime.fromisoformat(raw["start"]),
                end=datetime.fromisoformat(raw["end"]),
                point_in_time=datetime.fromisoformat(raw["point_in_time"]),
                maximum_staleness_ms=raw["maximum_staleness_ms"],
            )
            request.validate()
        except (KeyError, TypeError, ValueError, RqdataSnapshotError):
            raise RqdataSnapshotError("RQDATA_SERVICE_REQUEST_INVALID") from None
        return self._publisher.publish(request)

    def ticks(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        fields: tuple[str, ...],
    ) -> Sequence[Mapping[str, Any]]: ...


@dataclass(frozen=True)
class RqdataSnapshotRequest:
    request_id: str
    symbols: tuple[str, ...]
    fields: tuple[str, ...]
    start: datetime
    end: datetime
    point_in_time: datetime
    maximum_staleness_ms: int

    def validate(self) -> None:
        if (
            not 8 <= len(self.request_id) <= 128
            or not self.request_id.replace("-", "").isalnum()
        ):
            raise RqdataSnapshotError("RQDATA_REQUEST_ID_INVALID")
        if (
            not 1 <= len(self.symbols) <= 20
            or len(set(self.symbols)) != len(self.symbols)
            or tuple(sorted(self.symbols)) != self.symbols
            or any(_SYMBOL.fullmatch(symbol) is None for symbol in self.symbols)
        ):
            raise RqdataSnapshotError("RQDATA_SYMBOLS_INVALID")
        if (
            not self.fields
            or len(set(self.fields)) != len(self.fields)
            or "datetime" not in self.fields
            or any(field not in _FIELDS for field in self.fields)
        ):
            raise RqdataSnapshotError("RQDATA_FIELDS_DENIED")
        if any(
            value.tzinfo is None or value.utcoffset() is None
            for value in (self.start, self.end, self.point_in_time)
        ):
            raise RqdataSnapshotError("RQDATA_TIMEZONE_REQUIRED")
        if (
            self.start >= self.end
            or self.end > self.point_in_time
            or not 1 <= self.maximum_staleness_ms <= 86_400_000
        ):
            raise RqdataSnapshotError("RQDATA_TIME_RANGE_INVALID")

    def identity(self) -> dict[str, Any]:
        self.validate()
        return {
            "request_id": self.request_id,
            "symbols": list(self.symbols),
            "fields": list(self.fields),
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "point_in_time": self.point_in_time.isoformat(),
            "maximum_staleness_ms": self.maximum_staleness_ms,
        }


class RqdataSnapshotPublisher:
    """Publish an immutable signed snapshot without accepting reusable credentials."""

    def __init__(
        self,
        root: str | Path,
        provider: RqdataProvider,
        *,
        signing_key: Ed25519PrivateKey,
        parquet_writer: Callable[[Path, list[dict[str, Any]]], None] | None = None,
    ) -> None:
        self._root = Path(root)
        self._provider = provider
        self._signing_key = signing_key
        self._parquet_writer = parquet_writer or _write_parquet
        self._root.mkdir(parents=True, exist_ok=True)

    def publish(self, request: RqdataSnapshotRequest) -> dict[str, Any]:
        request_identity = request.identity()
        provider_identity = self._provider.provider_identity()
        if not _valid_digest(provider_identity):
            raise RqdataSnapshotError("RQDATA_PROVIDER_IDENTITY_INVALID")
        snapshot_hex = sha256(
            _json_bytes(
                {
                    "request": request_identity,
                    "provider_identity": provider_identity,
                }
            )
        ).hexdigest()
        snapshot_id = f"rqdata-{snapshot_hex}"
        published = self._root / snapshot_id
        if published.is_dir():
            return _load_manifest(
                published / "manifest.json",
                snapshot_id,
                self._signing_key.public_key(),
                snapshot_root=published,
            )
        if not self._provider.tick_entitled():
            raise RqdataSnapshotError("RQDATA_TICK_NOT_ENTITLED")

        staging = self._root / f".{snapshot_id}.staging"
        if staging.exists():
            raise RqdataSnapshotError("RQDATA_SNAPSHOT_STAGING_CONFLICT")
        staging.mkdir(mode=0o700)
        try:
            calendar = list(self._provider.trading_dates(request.start, request.end))
            _assert_public(calendar)
            calendar_path = staging / "calendar.json"
            _write_json(calendar_path, calendar)
            actions: list[dict[str, Any]] = []
            partitions: list[dict[str, Any]] = []
            for symbol in request.symbols:
                symbol_actions = [
                    dict(item)
                    for item in self._provider.corporate_actions(
                        symbol,
                        request.start,
                        request.end,
                    )
                ]
                _assert_public(symbol_actions)
                actions.extend(symbol_actions)
                rows = _point_in_time_rows(
                    self._provider.ticks(
                        symbol,
                        request.start,
                        request.end,
                        request.fields,
                    ),
                    request,
                )
                relative = Path("ticks") / f"{symbol}.parquet"
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                self._parquet_writer(target, rows)
                partitions.append(
                    {
                        "symbol": symbol,
                        "path": relative.as_posix(),
                        "row_count": len(rows),
                        "first_time": rows[0]["datetime"],
                        "last_time": rows[-1]["datetime"],
                        "content_digest": _file_digest(target),
                        "bytes": target.stat().st_size,
                    }
                )
            actions.sort(
                key=lambda item: (
                    str(item.get("symbol", "")),
                    str(item.get("ex_date", "")),
                )
            )
            actions_path = staging / "corporate-actions.json"
            _write_json(actions_path, actions)
            unsigned = {
                "contract_version": 1,
                "snapshot_id": snapshot_id,
                "state": "ready",
                "request": request_identity,
                "provider_identity": provider_identity,
                "frequency": "tick",
                "point_in_time": request.point_in_time.isoformat(),
                "calendar": {
                    "path": "calendar.json",
                    "session_count": len(calendar),
                    "content_digest": _file_digest(calendar_path),
                },
                "corporate_actions": {
                    "path": "corporate-actions.json",
                    "record_count": len(actions),
                    "content_digest": _file_digest(actions_path),
                },
                "partitions": partitions,
            }
            payload = _json_bytes(unsigned)
            public_key = self._signing_key.public_key().public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            )
            manifest = {
                **unsigned,
                "payload_digest": f"sha256:{sha256(payload).hexdigest()}",
                "publisher_key_fingerprint": f"sha256:{sha256(public_key).hexdigest()}",
                "signature": f"ed25519:{self._signing_key.sign(payload).hex()}",
            }
            _write_json(staging / "manifest.json", manifest)
            os.replace(staging, published)
            return manifest
        except RqdataSnapshotError:
            _remove_staging(staging)
            raise
        except Exception as exc:
            _remove_staging(staging)
            raise RqdataSnapshotError("RQDATA_SNAPSHOT_WRITE_FAILED") from exc


def _point_in_time_rows(
    values: Sequence[Mapping[str, Any]],
    request: RqdataSnapshotRequest,
) -> list[dict[str, Any]]:
    rows: list[tuple[datetime, dict[str, Any]]] = []
    for value in values:
        row = dict(value)
        _assert_public(row)
        if set(row) != set(request.fields):
            raise RqdataSnapshotError("RQDATA_TICK_SCHEMA_INVALID")
        timestamp_value = row.get("datetime")
        try:
            timestamp = (
                timestamp_value
                if isinstance(timestamp_value, datetime)
                else datetime.fromisoformat(str(timestamp_value))
            )
        except ValueError as exc:
            raise RqdataSnapshotError("RQDATA_TICK_SCHEMA_INVALID") from exc
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise RqdataSnapshotError("RQDATA_TICK_SCHEMA_INVALID")
        if timestamp < request.start or timestamp > request.end or timestamp > request.point_in_time:
            continue
        for key, item in row.items():
            if key != "datetime" and (
                isinstance(item, bool)
                or not isinstance(item, (int, float))
                or not math.isfinite(float(item))
            ):
                raise RqdataSnapshotError("RQDATA_TICK_SCHEMA_INVALID")
        row["datetime"] = timestamp.isoformat(timespec="milliseconds")
        rows.append((timestamp, row))
    rows.sort(key=lambda item: item[0])
    if not rows or len({item[0] for item in rows}) != len(rows):
        raise RqdataSnapshotError("RQDATA_TICK_DATA_INVALID")
    staleness_ms = int((request.end - rows[-1][0]).total_seconds() * 1_000)
    if staleness_ms >= request.maximum_staleness_ms:
        raise RqdataSnapshotError("RQDATA_SNAPSHOT_STALE")
    return [item[1] for item in rows]


def _write_parquet(path: Path, rows: list[dict[str, Any]]) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.Table.from_pylist(rows)
    pq.write_table(
        table,
        path,
        compression="zstd",
        use_dictionary=False,
        write_statistics=True,
    )


def _assert_public(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or key.lower() in _FORBIDDEN_KEYS:
                raise RqdataSnapshotError("RQDATA_CREDENTIAL_LEAK_DENIED")
            _assert_public(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _assert_public(item)


def verify_snapshot_manifest(
    manifest: Mapping[str, Any],
    public_key: Ed25519PublicKey,
    *,
    snapshot_root: Path | None = None,
) -> None:
    """Verify publisher identity, signature, and optionally every retained file."""

    try:
        if not isinstance(manifest, Mapping):
            raise ValueError("manifest")
        unsigned = {
            key: value
            for key, value in manifest.items()
            if key not in {"payload_digest", "publisher_key_fingerprint", "signature"}
        }
        payload = _json_bytes(unsigned)
        raw_public = public_key.public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        if manifest.get("payload_digest") != f"sha256:{sha256(payload).hexdigest()}":
            raise ValueError("payload digest")
        if manifest.get("publisher_key_fingerprint") != (
            f"sha256:{sha256(raw_public).hexdigest()}"
        ):
            raise ValueError("publisher fingerprint")
        signature = manifest.get("signature")
        if not isinstance(signature, str) or not signature.startswith("ed25519:"):
            raise ValueError("signature")
        public_key.verify(bytes.fromhex(signature.removeprefix("ed25519:")), payload)
        if snapshot_root is None:
            return
        root = snapshot_root.resolve(strict=True)
        retained = [manifest.get("calendar"), manifest.get("corporate_actions")]
        partitions = manifest.get("partitions")
        if not isinstance(partitions, list):
            raise ValueError("partitions")
        retained.extend(partitions)
        for descriptor in retained:
            if not isinstance(descriptor, Mapping):
                raise ValueError("descriptor")
            relative = descriptor.get("path")
            expected = descriptor.get("content_digest")
            if not isinstance(relative, str) or not _valid_digest(expected):
                raise ValueError("descriptor")
            target = (root / relative).resolve(strict=True)
            if root not in target.parents or _file_digest(target) != expected:
                raise ValueError("content digest")
    except (InvalidSignature, OSError, ValueError, TypeError):
        raise RqdataSnapshotError("RQDATA_SNAPSHOT_INVALID") from None


def _load_manifest(
    path: Path,
    snapshot_id: str,
    public_key: Ed25519PublicKey,
    *,
    snapshot_root: Path,
) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes(), object_pairs_hook=_unique_object)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RqdataSnapshotError("RQDATA_SNAPSHOT_INVALID") from exc
    if not isinstance(value, dict) or value.get("snapshot_id") != snapshot_id:
        raise RqdataSnapshotError("RQDATA_SNAPSHOT_INVALID")
    verify_snapshot_manifest(value, public_key, snapshot_root=snapshot_root)
    return value


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("xb") as handle:
        handle.write(_json_bytes(value))
        handle.write(b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _remove_staging(path: Path) -> None:
    if path.is_dir() and path.name.startswith(".rqdata-") and path.name.endswith(".staging"):
        shutil.rmtree(path)


def _file_digest(path: Path) -> str:
    hasher = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return f"sha256:{hasher.hexdigest()}"


def _valid_digest(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None


def _parse_endpoint(value: str) -> tuple[str, int]:
    try:
        host, port_text = value.rsplit(":", 1)
        port = int(port_text)
    except (AttributeError, ValueError):
        raise RqdataSnapshotError("RQDATA_ENDPOINT_INVALID") from None
    if not host or not 1 <= port <= 65_535:
        raise RqdataSnapshotError("RQDATA_ENDPOINT_INVALID")
    return host, port


def _rqdata_symbol(symbol: str) -> str:
    if _SYMBOL.fullmatch(symbol) is None:
        raise RqdataSnapshotError("RQDATA_SYMBOLS_INVALID")
    code, exchange = symbol.split(".")
    suffix = {"SH": "XSHG", "SZ": "XSHE", "BJ": "XBEI"}[exchange]
    return f"{code}.{suffix}"


def _date_text(value: Any) -> str:
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        raise RqdataSnapshotError("RQDATA_PROVIDER_DATA_INVALID") from None


def _public_scalar(value: Any) -> Any:
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == datetime.min.time() else value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return value
    raise RqdataSnapshotError("RQDATA_PROVIDER_DATA_INVALID")


def _missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        result = value != value
        return bool(result) if isinstance(result, (bool, int)) else False
    except Exception:
        return False


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


class _RqdataTcpServer(ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        service: RqdataSnapshotService,
        authentication: str,
    ) -> None:
        self.snapshot_service = service
        self.authentication = authentication
        super().__init__(address, _RqdataRequestHandler)


class _RqdataRequestHandler(BaseRequestHandler):
    def handle(self) -> None:
        server = self.server
        if not isinstance(server, _RqdataTcpServer):
            return
        try:
            value = json.loads(_receive_line(self.request), object_pairs_hook=_unique_object)
            if not isinstance(value, dict):
                raise RqdataSnapshotError("RQDATA_SERVICE_REQUEST_INVALID")
            authentication = value.pop("authentication", None)
            if not isinstance(authentication, str) or not compare_digest(
                authentication,
                server.authentication,
            ):
                raise RqdataSnapshotError("RQDATA_SERVICE_AUTHENTICATION_FAILED")
            if value == {"contract_version": 1, "operation": "health"}:
                result = {"state": "ready"}
            else:
                result = server.snapshot_service.handle(value)
            response = {"ok": True, "data": result}
        except RqdataSnapshotError as exc:
            response = {"ok": False, "error": {"code": str(exc)}}
        except Exception:
            response = {
                "ok": False,
                "error": {"code": "RQDATA_SERVICE_REQUEST_INVALID"},
            }
        self.request.sendall(_json_bytes(response) + b"\n")


def serve(project_root: Path, address: str) -> None:
    root = project_root.resolve(strict=True)
    host, port = _parse_endpoint(address)
    if host != "127.0.0.1":
        raise RqdataSnapshotError("RQDATA_SERVICE_LOOPBACK_REQUIRED")
    launch = consume_one_use_configuration()
    public = launch["public"]
    secrets = launch["secrets"]
    try:
        provider = RqdatacProvider(
            username=secrets.pop("username"),
            password=secrets.pop("password"),
            endpoint=public["endpoint"],
        )
    finally:
        secrets.clear()
    signing_key = _load_or_create_signing_key(
        root,
        launch["operator_identity_digest"],
    )
    launch.clear()
    service = RqdataSnapshotService(
        RqdataSnapshotPublisher(
            root / ".demo-state" / "rqdata" / "snapshots",
            provider,
            signing_key=signing_key,
        )
    )
    token = _load_or_create_ipc_token(root)
    endpoint_path = root / ".demo-state" / "rqdata" / "endpoint.json"
    server = _RqdataTcpServer((host, port), service, token)
    _write_json(
        endpoint_path,
        {
            "contract_version": 1,
            "transport": "tcp-loopback",
            "address": address,
            "publisher_key_fingerprint": f"sha256:{sha256(signing_key.public_key().public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            )).hexdigest()}",
        },
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
        endpoint_path.unlink(missing_ok=True)
        provider.close()


def _load_or_create_signing_key(
    root: Path,
    operator_identity_digest: str,
) -> Ed25519PrivateKey:
    from vnpy.demo_web.configuration import CurrentUserDpapi, _secure_secret_directory

    directory = root / ".demo-secrets" / "rqdata"
    _secure_secret_directory(directory)
    path = directory / "signing-key.bin"
    protector = CurrentUserDpapi()
    context = b"auto-trade-rqdata-signing:" + operator_identity_digest.encode("ascii")
    if path.is_file():
        try:
            seed = protector.unprotect(path.read_bytes(), context=context)
            if len(seed) != 32:
                raise ValueError("seed")
            return Ed25519PrivateKey.from_private_bytes(seed)
        except Exception:
            raise RqdataSnapshotError("RQDATA_SIGNING_KEY_INVALID") from None
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    encrypted = protector.protect(seed, context=context)
    _atomic_bytes(path, encrypted)
    del seed, encrypted
    return key


def _load_or_create_ipc_token(root: Path) -> str:
    from vnpy.demo_web.configuration import _secure_secret_directory

    directory = root / ".demo-secrets" / "rqdata"
    _secure_secret_directory(directory)
    path = directory / "ipc-token"
    if path.is_file():
        try:
            token = path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeDecodeError):
            raise RqdataSnapshotError("RQDATA_IPC_TOKEN_INVALID") from None
    else:
        token = token_urlsafe(48)
        _atomic_bytes(path, token.encode("ascii"))
    if not 24 <= len(token) <= 512:
        raise RqdataSnapshotError("RQDATA_IPC_TOKEN_INVALID")
    return token


def _atomic_bytes(path: Path, value: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("xb") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _receive_line(connection: Any) -> str:
    chunks = bytearray()
    while len(chunks) <= 65_536:
        block = connection.recv(min(4096, 65_537 - len(chunks)))
        if not block:
            break
        chunks.extend(block)
        if b"\n" in block:
            break
    if not chunks.endswith(b"\n") or chunks.count(b"\n") != 1 or len(chunks) > 65_536:
        raise RqdataSnapshotError("RQDATA_SERVICE_REQUEST_INVALID")
    return chunks[:-1].decode("utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m vnpy.model_production.rqdata_snapshot")
    parser.add_argument("--project-root", type=Path, required=True)
    subcommands = parser.add_subparsers(dest="command", required=True)
    serve_parser = subcommands.add_parser("serve")
    serve_parser.add_argument("--address", required=True)
    arguments = parser.parse_args()
    if arguments.command == "serve":
        serve(arguments.project_root, arguments.address)


if __name__ == "__main__":
    main()
