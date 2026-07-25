"""Durable vn.py model intent, risk, and broker-effect ordering journal."""

from __future__ import annotations

from contextlib import closing
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Any

from .risk import ModelIntent, RiskDecision


class ModelProductionJournal:
    """Append idempotent ordered dispositions before any broker side effect."""

    def __init__(self, database: str | Path) -> None:
        self._database = str(database)
        self._lock = RLock()
        self._initialize()

    def append_intent(self, intent: ModelIntent) -> None:
        self._append(intent.intent_id, "intent", asdict(intent), required_previous=None)

    def append_risk(self, intent_id: str, risk: RiskDecision) -> None:
        self._append(intent_id, "risk", asdict(risk), required_previous="intent")

    def append_broker_effect(
        self,
        intent_id: str,
        operation_key: str,
        payload: dict[str, Any],
    ) -> None:
        record = {"operation_key": operation_key, **payload}
        self._append(intent_id, "broker_effect", record, required_previous="risk")

    def event_kinds(self, intent_id: str) -> tuple[str, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT kind FROM model_production_events WHERE intent_id=? ORDER BY sequence",
                (intent_id,),
            ).fetchall()
        return tuple(row[0] for row in rows)

    def _append(
        self,
        intent_id: str,
        kind: str,
        payload: dict[str, Any],
        required_previous: str | None,
    ) -> None:
        if not intent_id:
            raise ValueError("intent_id is required")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT payload_json FROM model_production_events WHERE intent_id=? AND kind=?",
                (intent_id, kind),
            ).fetchone()
            if existing is not None:
                if existing[0] != encoded:
                    raise RuntimeError("JOURNAL_IDENTITY_DRIFT")
                connection.commit()
                return
            if required_previous is not None:
                previous = connection.execute(
                    "SELECT 1 FROM model_production_events WHERE intent_id=? AND kind=?",
                    (intent_id, required_previous),
                ).fetchone()
                if previous is None:
                    raise RuntimeError("JOURNAL_ORDER_VIOLATION")
            sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 FROM model_production_events WHERE intent_id=?",
                (intent_id,),
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO model_production_events(intent_id,sequence,kind,payload_json) VALUES(?,?,?,?)",
                (intent_id, sequence, kind, encoded),
            )
            connection.commit()
    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database, timeout=5.0, isolation_level=None)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS model_production_events (
                    intent_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(intent_id, kind),
                    UNIQUE(intent_id, sequence)
                );
                """
            )
            connection.commit()
