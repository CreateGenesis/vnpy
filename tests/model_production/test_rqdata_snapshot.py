from __future__ import annotations

import json
import os
from base64 import b64encode
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.parquet as parquet
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from vnpy.model_production.rqdata_snapshot import (
    RqdatacProvider,
    RqdataSnapshotService,
    RqdataSnapshotError,
    RqdataSnapshotPublisher,
    RqdataSnapshotRequest,
    consume_one_use_configuration,
    verify_snapshot_manifest,
)
from vnpy.demo_web.operations import OperationsService


def digest(label: str) -> str:
    return f"sha256:{sha256(label.encode()).hexdigest()}"


class Provider:
    def __init__(self, *, entitled: bool = True) -> None:
        self.entitled = entitled
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def provider_identity(self) -> str:
        return digest("rqdata-license")

    def tick_entitled(self) -> bool:
        return self.entitled

    def trading_dates(self, _start: datetime, _end: datetime) -> list[str]:
        return ["2026-07-27", "2026-07-28"]

    def corporate_actions(
        self, symbol: str, _start: datetime, _end: datetime
    ) -> list[dict[str, Any]]:
        return [{"symbol": symbol, "ex_date": "2026-07-28", "split_factor": 1.0}]

    def ticks(
        self,
        symbol: str,
        _start: datetime,
        _end: datetime,
        fields: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        self.calls.append((symbol, fields))
        return [
            {
                "datetime": "2026-07-28T09:30:00.000+08:00",
                "last": 10.0,
                "volume": 100,
                "total_turnover": 1_000.0,
            },
            {
                "datetime": "2026-07-28T09:30:00.500+08:00",
                "last": 10.01,
                "volume": 200,
                "total_turnover": 2_002.0,
            },
            {
                "datetime": "2026-07-28T09:30:02.000+08:00",
                "last": 99.0,
                "volume": 999,
                "total_turnover": 99_000.0,
            },
        ]


def request() -> RqdataSnapshotRequest:
    return RqdataSnapshotRequest(
        request_id="rqdata-snapshot-0001",
        symbols=("600000.SH",),
        fields=("datetime", "last", "volume", "total_turnover"),
        start=datetime.fromisoformat("2026-07-28T09:30:00+08:00"),
        end=datetime.fromisoformat("2026-07-28T09:30:01+08:00"),
        point_in_time=datetime.fromisoformat("2026-07-28T09:30:01+08:00"),
        maximum_staleness_ms=1_000,
    )


def test_tick_snapshot_is_point_in_time_atomic_signed_and_contains_no_credentials(
    tmp_path: Path,
) -> None:
    provider = Provider()
    key = Ed25519PrivateKey.from_private_bytes(bytes([31]) * 32)
    publisher = RqdataSnapshotPublisher(tmp_path, provider, signing_key=key)

    manifest = publisher.publish(request())
    replay = publisher.publish(request())

    assert replay == manifest
    assert manifest["state"] == "ready"
    assert manifest["provider_identity"] == digest("rqdata-license")
    assert manifest["point_in_time"] == "2026-07-28T09:30:01+08:00"
    assert manifest["partitions"][0]["row_count"] == 2
    assert manifest["signature"].startswith("ed25519:")
    verify_snapshot_manifest(manifest, key.public_key(), snapshot_root=tmp_path / manifest["snapshot_id"])
    assert provider.calls == [
        ("600000.SH", ("datetime", "last", "volume", "total_turnover"))
    ]

    snapshot_root = tmp_path / manifest["snapshot_id"]
    table = parquet.read_table(snapshot_root / manifest["partitions"][0]["path"])
    assert table.num_rows == 2
    assert table.column("last").to_pylist() == [10.0, 10.01]
    retained = b"\n".join(
        path.read_bytes() for path in snapshot_root.rglob("*") if path.is_file()
    ).lower()
    for forbidden in (b"password", b"username", b"api_key", b"token"):
        assert forbidden not in retained
    assert not list(tmp_path.glob(".*.staging"))


@pytest.mark.parametrize(
    ("symbols", "fields", "code"),
    [
        (("600000.SH", "600000.SH"), request().fields, "RQDATA_SYMBOLS_INVALID"),
        (("600000.SH",), ("datetime", "close"), "RQDATA_FIELDS_DENIED"),
        (tuple(f"{index:06d}.SH" for index in range(21)), request().fields, "RQDATA_SYMBOLS_INVALID"),
    ],
)
def test_request_rejects_noncanonical_unallowlisted_or_oversized_queries(
    symbols: tuple[str, ...],
    fields: tuple[str, ...],
    code: str,
) -> None:
    value = request()
    with pytest.raises(RqdataSnapshotError, match=code):
        RqdataSnapshotRequest(
            request_id=value.request_id,
            symbols=symbols,
            fields=fields,
            start=value.start,
            end=value.end,
            point_in_time=value.point_in_time,
            maximum_staleness_ms=value.maximum_staleness_ms,
        ).validate()


def test_missing_tick_entitlement_and_stale_or_failed_publication_fail_closed(
    tmp_path: Path,
) -> None:
    key = Ed25519PrivateKey.from_private_bytes(bytes([32]) * 32)
    with pytest.raises(RqdataSnapshotError, match="RQDATA_TICK_NOT_ENTITLED"):
        RqdataSnapshotPublisher(tmp_path, Provider(entitled=False), signing_key=key).publish(
            request()
        )

    class Stale(Provider):
        def ticks(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            rows = super().ticks(*args, **kwargs)
            return rows[:1]

    with pytest.raises(RqdataSnapshotError, match="RQDATA_SNAPSHOT_STALE"):
        RqdataSnapshotPublisher(tmp_path, Stale(), signing_key=key).publish(request())

    class FailingWriter:
        def __call__(self, _path: Path, _rows: list[dict[str, Any]]) -> None:
            raise OSError("simulated parquet failure")

    with pytest.raises(RqdataSnapshotError, match="RQDATA_SNAPSHOT_WRITE_FAILED"):
        RqdataSnapshotPublisher(
            tmp_path,
            Provider(),
            signing_key=key,
            parquet_writer=FailingWriter(),
        ).publish(request())
    assert list(tmp_path.iterdir()) == []


def test_request_contract_has_no_credential_surface() -> None:
    fields = set(RqdataSnapshotRequest.__dataclass_fields__)
    assert not fields & {"username", "password", "api_key", "token", "credential"}
    assert request().point_in_time.tzinfo is not None
    assert request().point_in_time.utcoffset() is not None


class FakeRqdatac:
    __version__ = "3.5.6.1"

    def __init__(self, *, denied: bool = False) -> None:
        self.denied = denied
        self.init_calls: list[tuple[Any, ...]] = []
        self.price_calls: list[dict[str, Any]] = []

    def init(self, *args: Any, **kwargs: Any) -> None:
        self.init_calls.append((*args, kwargs))

    def get_trading_dates(self, start: date, end: date) -> list[date]:
        assert start == date(2026, 7, 28)
        assert end == date(2026, 7, 28)
        return [date(2026, 7, 28)]

    def get_split(self, symbol: str, **kwargs: Any) -> pd.DataFrame:
        assert symbol == "600000.XSHG"
        assert kwargs == {"start_date": date(2026, 7, 28), "end_date": date(2026, 7, 28)}
        return pd.DataFrame(
            {
                "book_closure_date": [pd.Timestamp("2026-07-27")],
                "payable_date": [pd.Timestamp("2026-07-28")],
                "split_coefficient_from": [10.0],
                "split_coefficient_to": [11.0],
                "cum_factor": [1.1],
            },
            index=pd.DatetimeIndex(["2026-07-28"], name="ex_dividend_date"),
        )

    def get_price(self, symbol: str, **kwargs: Any) -> pd.DataFrame:
        if self.denied:
            raise PermissionError("tick permission denied for secret-account")
        self.price_calls.append({"symbol": symbol, **kwargs})
        return pd.DataFrame(
            {
                "last": [10.0, 10.01],
                "volume": [100.0, 200.0],
                "total_turnover": [1_000.0, 2_002.0],
                "b1": [9.99, 10.0],
                "b1_v": [10.0, 11.0],
                "a1": [10.01, 10.02],
                "a1_v": [12.0, 13.0],
            },
            index=pd.DatetimeIndex(
                ["2026-07-28 09:30:00", "2026-07-28 09:30:00.500"],
                name="datetime",
            ),
        )


def test_real_rqdatac_adapter_uses_fixed_tick_calendar_and_split_calls() -> None:
    module = FakeRqdatac()
    provider = RqdatacProvider(
        username="operator",
        password="write-only-secret",
        endpoint="rqdatad-pro.ricequant.com:16011",
        module=module,
    )
    value = request()

    assert provider.tick_entitled() is True
    assert provider.trading_dates(value.start, value.end) == ["2026-07-28"]
    actions = provider.corporate_actions("600000.SH", value.start, value.end)
    rows = provider.ticks(
        "600000.SH",
        value.start,
        value.end,
        (
            "datetime",
            "last",
            "volume",
            "total_turnover",
            "bid_price1",
            "bid_volume1",
            "ask_price1",
            "ask_volume1",
        ),
    )

    assert module.init_calls == [
        (
            "operator",
            "write-only-secret",
            ("rqdatad-pro.ricequant.com", 16011),
            {
                "lazy": False,
                "connect_timeout": 5,
                "timeout": 30,
                "auto_load_plugins": False,
            },
        )
    ]
    assert module.price_calls == [
        {
            "symbol": "600000.XSHG",
            "start_date": value.start,
            "end_date": value.end,
            "frequency": "tick",
            "fields": ["last", "volume", "total_turnover", "b1", "b1_v", "a1", "a1_v"],
            "adjust_type": "none",
            "expect_df": True,
        }
    ]
    assert actions == [
        {
            "symbol": "600000.SH",
            "ex_date": "2026-07-28",
            "book_closure_date": "2026-07-27",
            "payable_date": "2026-07-28",
            "split_coefficient_from": 10.0,
            "split_coefficient_to": 11.0,
            "cum_factor": 1.1,
        }
    ]
    assert rows[0] == {
        "datetime": datetime.fromisoformat("2026-07-28T09:30:00+08:00"),
        "last": 10.0,
        "volume": 100.0,
        "total_turnover": 1_000.0,
        "bid_price1": 9.99,
        "bid_volume1": 10.0,
        "ask_price1": 10.01,
        "ask_volume1": 12.0,
    }
    assert "operator" not in provider.provider_identity()
    assert "write-only-secret" not in provider.provider_identity()


def test_real_adapter_maps_tick_permission_failure_without_leaking_credentials() -> None:
    provider = RqdatacProvider(
        username="operator",
        password="write-only-secret",
        endpoint="rqdatad-pro.ricequant.com:16011",
        module=FakeRqdatac(denied=True),
    )

    with pytest.raises(RqdataSnapshotError, match="^RQDATA_TICK_NOT_ENTITLED$") as error:
        provider.ticks("600000.SH", request().start, request().end, request().fields)
    assert "operator" not in str(error.value)
    assert "write-only-secret" not in str(error.value)


def test_one_use_configuration_is_consumed_and_has_no_reusable_credential_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "contract_version": 1,
        "configuration_version": 7,
        "configuration_digest": digest("configuration"),
        "operator_identity_digest": digest("operator"),
        "public": {
            "endpoint": "rqdatad-pro.ricequant.com:16011",
            "tick_required": True,
        },
        "secrets": {"username": "operator", "password": "write-only-secret"},
    }
    monkeypatch.setenv(
        "AUTO_TRADE_ONE_USE_SECRET",
        b64encode(json.dumps(payload).encode()).decode("ascii"),
    )

    consumed = consume_one_use_configuration()

    assert consumed == payload
    assert "AUTO_TRADE_ONE_USE_SECRET" not in os.environ


def test_replay_rejects_tampered_manifest_or_partition(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.from_private_bytes(bytes([33]) * 32)
    publisher = RqdataSnapshotPublisher(tmp_path, Provider(), signing_key=key)
    manifest = publisher.publish(request())
    snapshot_root = tmp_path / manifest["snapshot_id"]
    manifest_path = snapshot_root / "manifest.json"
    altered = json.loads(manifest_path.read_text(encoding="utf-8"))
    altered["state"] = "blocked"
    manifest_path.write_text(json.dumps(altered), encoding="utf-8")
    with pytest.raises(RqdataSnapshotError, match="RQDATA_SNAPSHOT_INVALID"):
        publisher.publish(request())

    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    partition = snapshot_root / manifest["partitions"][0]["path"]
    partition.write_bytes(partition.read_bytes() + b"tampered")
    with pytest.raises(RqdataSnapshotError, match="RQDATA_SNAPSHOT_INVALID"):
        publisher.publish(request())


def test_rqdata_service_start_uses_only_active_configuration_secret() -> None:
    class Configuration:
        def read_active(self) -> dict[str, Any]:
            return {
                "state": "active",
                "version": 7,
                "configuration_digest": digest("configuration"),
                "operator_identity_digest": digest("operator"),
                "sections": {
                    "rqdata": {
                        "endpoint": "rqdatad-pro.ricequant.com:16011",
                        "tick_required": True,
                    }
                },
            }

        def read_active_section_secrets(self, section: str) -> dict[str, str]:
            assert section == "rqdata"
            return {"username": "active-user", "password": "active-secret"}

    class Supervisor:
        def __init__(self) -> None:
            self.secret_payload: bytes | None = None

        def handle_with_secret(
            self, command: dict[str, Any], secret_payload: bytes
        ) -> dict[str, Any]:
            self.secret_payload = secret_payload
            return {**command, "state": "ready"}

    supervisor = Supervisor()
    operations = OperationsService(Configuration(), object(), supervisor)  # type: ignore[arg-type]

    result = operations.control_service(
        "rqdata_fetcher",
        "start",
        {"expected_revision": 4, "idempotency_key": "rqdata-start-0001"},
    )

    assert result["state"] == "ready"
    assert supervisor.secret_payload is not None
    launch = json.loads(supervisor.secret_payload)
    assert launch["configuration_version"] == 7
    assert launch["secrets"] == {
        "username": "active-user",
        "password": "active-secret",
    }


def test_snapshot_service_accepts_only_strict_credential_free_requests(
    tmp_path: Path,
) -> None:
    key = Ed25519PrivateKey.from_private_bytes(bytes([34]) * 32)
    service = RqdataSnapshotService(
        RqdataSnapshotPublisher(tmp_path, Provider(), signing_key=key)
    )
    value = request().identity()

    manifest = service.handle(
        {
            "contract_version": 1,
            "operation": "publish_snapshot",
            "request": value,
        }
    )

    assert manifest["state"] == "ready"
    with pytest.raises(RqdataSnapshotError, match="RQDATA_SERVICE_REQUEST_INVALID"):
        service.handle(
            {
                "contract_version": 1,
                "operation": "publish_snapshot",
                "request": {**value, "password": "denied"},
            }
        )
