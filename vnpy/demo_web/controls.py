"""Direct Web control orchestration across isolated vn.py run clients."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, wait
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from threading import RLock
from time import monotonic_ns
from typing import Any

from vnpy.model_production.contracts import canonical_json_v1

from .run_clients import BrokerSimulationRunClient


_DIGEST = re.compile(r"^(?:sha256|blake3):[0-9a-f]{64}$")
_ACTIONS = frozenset({"pause", "emergency_stop"})


class DemoCampaignControls:
    """Fan out bounded pause/stop calls without any Agent dependency."""

    def __init__(
        self,
        *,
        clients: tuple[BrokerSimulationRunClient, ...],
        database: str | Path,
        clock_ns: Callable[[], int] = monotonic_ns,
        timeout_seconds: float = 0.9,
    ) -> None:
        if not 1 <= len(clients) <= 2:
            raise ValueError("CONTROL_CLIENT_COUNT_INVALID")
        gateways = [client.binding.gateway for client in clients]
        if len(set(gateways)) != len(gateways):
            raise ValueError("CONTROL_GATEWAY_DUPLICATE")
        if not callable(clock_ns) or not 0 < timeout_seconds <= 1:
            raise ValueError("CONTROL_CONFIGURATION_INVALID")
        self._clients = tuple(clients)
        self._clock_ns = clock_ns
        self._timeout_seconds = timeout_seconds
        self._lock = RLock()
        database_path = Path(database)
        if str(database) != ":memory:":
            database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(database_path), check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute(
            """CREATE TABLE IF NOT EXISTS demo_control_receipts (
                idempotency_digest TEXT PRIMARY KEY,
                action TEXT NOT NULL,
                request_digest TEXT NOT NULL,
                receipt_json TEXT NOT NULL,
                receipt_digest TEXT NOT NULL
            )"""
        )
        self._connection.commit()

    def pause_campaign(
        self,
        *,
        campaign_digest: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if _DIGEST.fullmatch(campaign_digest) is None:
            raise ValueError("CONTROL_CAMPAIGN_INVALID")
        return self._dispatch(
            action="pause",
            campaign_digest=campaign_digest,
            idempotency_key=idempotency_key,
        )

    def emergency_stop(self, *, idempotency_key: str) -> dict[str, Any]:
        return self._dispatch(
            action="emergency_stop",
            campaign_digest=None,
            idempotency_key=idempotency_key,
        )

    def _dispatch(
        self,
        *,
        action: str,
        campaign_digest: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if action not in _ACTIONS:
            raise ValueError("CONTROL_ACTION_INVALID")
        if not isinstance(idempotency_key, str) or not 16 <= len(idempotency_key) <= 128:
            raise ValueError("CONTROL_IDEMPOTENCY_KEY_INVALID")
        idempotency_digest = _text_digest(idempotency_key)
        request = {
            "action": action,
            "campaign_digest": campaign_digest,
            "idempotency_digest": idempotency_digest,
            "run_bindings": [
                {
                    "gateway": client.binding.gateway,
                    "run_digest": client.binding.run_digest,
                }
                for client in sorted(
                    self._clients,
                    key=lambda item: item.binding.gateway,
                )
            ],
        }
        request_digest = _payload_digest(request)

        with self._lock:
            retained = self._read(idempotency_digest)
            if retained is not None:
                retained_request_digest, receipt = retained
                if retained_request_digest != request_digest:
                    raise ValueError("CONTROL_IDEMPOTENCY_CONFLICT")
                return receipt

            started_at_ns = self._clock_ns()
            executor = ThreadPoolExecutor(
                max_workers=len(self._clients),
                thread_name_prefix="demo-control",
            )
            futures: dict[Future[dict[str, Any]], BrokerSimulationRunClient] = {}
            for client in self._clients:
                if action == "pause":
                    assert campaign_digest is not None
                    future = executor.submit(
                        client.pause_campaign,
                        campaign_digest,
                        idempotency_key,
                    )
                else:
                    future = executor.submit(client.emergency_stop, idempotency_key)
                futures[future] = client

            done, pending = wait(futures, timeout=self._timeout_seconds)
            gateway_results: list[dict[str, Any]] = []
            for future, client in futures.items():
                if future in pending:
                    future.cancel()
                    gateway_results.append(
                        _unavailable_result(client.binding.gateway, "RUN_CONTROL_TIMEOUT")
                    )
                    continue
                try:
                    response = future.result()
                    gateway_results.append(_public_run_result(response))
                except Exception:
                    gateway_results.append(
                        _unavailable_result(client.binding.gateway, "RUN_CONTROL_UNAVAILABLE")
                    )
            executor.shutdown(wait=not pending, cancel_futures=True)
            gateway_results.sort(key=lambda item: item["gateway"])

            completed_at_ns = self._clock_ns()
            local_deadline_met = completed_at_ns - started_at_ns <= 1_000_000_000
            all_available = len(done) == len(futures) and all(
                result["state"] != "unavailable" for result in gateway_results
            )
            remote_deadline_met = all(
                isinstance(result.get("data"), Mapping)
                and result["data"].get("hard_stop_deadline_met") is True
                for result in gateway_results
            )
            deadline_met = (
                local_deadline_met
                and all_available
                and (action != "emergency_stop" or remote_deadline_met)
            )
            expected_state = "paused" if action == "pause" else "stopped"
            successful = deadline_met and all(
                result["state"] == expected_state for result in gateway_results
            )
            aggregate_state = expected_state if successful else "uncertain"
            unsigned = {
                "contract_version": 1,
                "action": action,
                "state": aggregate_state,
                "request_digest": request_digest,
                "started_at_ns": started_at_ns,
                "completed_at_ns": completed_at_ns,
                "hard_stop_deadline_met": deadline_met,
                "gateways": gateway_results,
            }
            receipt = {**unsigned, "receipt_digest": _payload_digest(unsigned)}
            self._persist(
                idempotency_digest,
                action,
                request_digest,
                receipt,
            )
            return receipt

    def _persist(
        self,
        idempotency_digest: str,
        action: str,
        request_digest: str,
        receipt: dict[str, Any],
    ) -> None:
        encoded = canonical_json_v1(receipt).decode("utf-8")
        self._connection.execute(
            """INSERT INTO demo_control_receipts(
                idempotency_digest,action,request_digest,receipt_json,receipt_digest
            ) VALUES(?,?,?,?,?)""",
            (
                idempotency_digest,
                action,
                request_digest,
                encoded,
                receipt["receipt_digest"],
            ),
        )
        self._connection.commit()

    def _read(
        self,
        idempotency_digest: str,
    ) -> tuple[str, dict[str, Any]] | None:
        row = self._connection.execute(
            "SELECT request_digest,receipt_json,receipt_digest FROM demo_control_receipts WHERE idempotency_digest=?",
            (idempotency_digest,),
        ).fetchone()
        if row is None:
            return None
        try:
            receipt = json.loads(row[1])
        except json.JSONDecodeError as exc:
            raise ValueError("CONTROL_RECEIPT_INVALID") from exc
        if not isinstance(receipt, dict):
            raise ValueError("CONTROL_RECEIPT_INVALID")
        unsigned = dict(receipt)
        retained_digest = unsigned.pop("receipt_digest", None)
        if retained_digest != row[2] or retained_digest != _payload_digest(unsigned):
            raise ValueError("CONTROL_RECEIPT_INVALID")
        return row[0], receipt


def _public_run_result(response: dict[str, Any]) -> dict[str, Any]:
    result = {
        "gateway": response["gateway"],
        "state": response["state"],
        "receipt_digest": response["receipt_digest"],
    }
    data = response.get("data")
    if isinstance(data, Mapping):
        result["data"] = dict(data)
    return result


def _unavailable_result(gateway: str, error_code: str) -> dict[str, Any]:
    return {
        "gateway": gateway,
        "state": "unavailable",
        "error_code": error_code,
    }


def _text_digest(value: str) -> str:
    return f"sha256:{sha256(value.encode()).hexdigest()}"


def _payload_digest(value: dict[str, Any]) -> str:
    return f"sha256:{sha256(canonical_json_v1(value)).hexdigest()}"
